#!/usr/bin/env python3
"""The topic queue (local mode, 2026-11-01): plans the person approved run one after another, as many at
once as the limits allow, each started from the main of that moment.

Usage:
  wt_queue.py tick <main checkout>     start whatever may start now (run-worker.sh after every run's
                                       exit file, and worker-control.py after `wt queue`); prints JSON
  wt_queue.py status <main checkout>   the queue with each item's state and why it waits; prints JSON
worker-control.py handles the person's `wt queue ...` prompts with add() / drop() (UserPromptSubmit: the
same trust as `wt launch`; agents can't write .hook-only/, so only the person's prompt fills the queue).

The planning session writes, per topic, in the main checkout (the person approves them by queueing):
  plans/queue/<topic>.md     the worktree plan, as handoff.md "Hand off" step 2 writes it
  plans/queue/<topic>.json   {"branch": "<type>/<scope>/<topic>", "after": [topics], "resources": [tags],
                              "order": <int, optional: the planned order for the pane's "queue all">,
                              "estimate": {the plans/worker/<topic>.json content cost-history.py printed}}
status() also lists them as "planned" while they are neither in the queue nor have a worktree, so the
pane offers a button per topic (and one for all, by "order") instead of the person typing names.
State: <main>/.hook-only/queue/queue.json {"items": [{"topic", "added", "started"}]}, under an exclusive
flock on <main>/.hook-only/queue/lock; every start and refusal to start is appended to log.jsonl.

An item waits while, in order:
  - it is started already (its run, then auto-merge and auto-finish take over)
  - the subscription's usage is at a limit: <home>/quota/usage.json (quota-snapshot.py, the status line)
    says the 5-hour or the weekly window is at queue_max_five_hour_pct / queue_max_seven_day_pct or
    above and hasn't reset yet; a window past its resets_at counts as fresh. With no snapshot, or one
    older than queue_quota_max_age_minutes with a window not reset since (usage only grows, and the
    workers report none), queue_quota_unknown decides: "wait" (the default) or "start". These are the
    person's, not the repo's: budget-defaults.json only
  - a topic in "after" has no branch merged into the local main yet (a stuck one blocks only its dependents)
  - its estimate alone is above queue_max_concurrent_usd: stuck, the person raises the limit or starts it
  - the busy topics (a run going, or an automatic resume or merge run decided and not yet started) already
    number queue_max_workers, or their estimates plus its own exceed queue_max_concurrent_usd
  - it shares a resource tag with a busy topic (tags are the repo's: opaque words such as an API quota's
    name; two topics with the same tag never run together)
Items go in the person's order; a waiting item doesn't hold back a later one that may start.
Limits: budget-defaults.json "queue_max_workers", "queue_max_concurrent_usd"; a repo overrides them in
.claude/flow.json {"queue": {...}}, read from the local main's commit (git show), not the working tree.
Starting one: new-worktree.sh <branch> from the main checkout (the branch from the main of that moment),
the plan copied to <worktree>/plans/<topic>.md, the estimate written to plans/worker/<topic>.json, then
worker-control.py with the prompt `wt launch <topic>`, the same path as a typed launch.
Local mode only (<main>/.hook-only/flow-mode "local"): "after" needs the local main to say what is merged.
Done items leave: once a started topic's branch is merged and its worktree removed (auto-finish, or
/finish-worktree), a tick marks it "done" and moves plans/queue/<topic>.md and .json to
<main>/.hook-only/archive/<topic>/queue/; status shows it as merged for queue_done_linger_minutes
(budget-defaults.json), then it is removed. status() prunes too (under the lock; skipped where .hook-only/ is
read-only, e.g. the planning session's sandboxed Bash, which then only reads), so a merged topic leaves
the pane, which polls status, even when no run ends and nobody types `wt queue` (2026-11-01: two merged
topics stayed listed because no tick came after the merge). Starts and failed starts are events (wt_events.py).
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wt_controls  # noqa: E402
import wt_events  # noqa: E402

DEFAULTS_FILE = HERE / "budget-defaults.json"
USAGE_FILE = HERE.parent / "quota" / "usage.json"
NEW_WORKTREE = HERE.parent / "skills" / "new-worktree" / "new-worktree.sh"
WORKER_CONTROL = HERE / "worker-control.py"
BRANCH = re.compile(r"^(feature|fix)/[a-z0-9]+/([a-z0-9]+(?:-[a-z0-9]+){1,3})$")
TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class Refused(Exception):
	pass


def queueDir(mainDir):
	return Path(mainDir) / ".hook-only" / "queue"


def loadState(mainDir):
	return wt_controls.loadJson(queueDir(mainDir) / "queue.json") or {"items": []}


def saveState(mainDir, state):
	queueDir(mainDir).mkdir(parents=True, exist_ok=True)
	path = queueDir(mainDir) / "queue.json"
	path.with_suffix(".tmp").write_text(json.dumps(state, indent=1), encoding="utf-8")
	path.with_suffix(".tmp").replace(path)


def log(mainDir, **entry):
	queueDir(mainDir).mkdir(parents=True, exist_ok=True)
	with open(queueDir(mainDir) / "log.jsonl", "a", encoding="utf-8") as f:
		f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")


class Locked:
	def __init__(self, mainDir):
		queueDir(mainDir).mkdir(parents=True, exist_ok=True)
		self.file = open(queueDir(mainDir) / "lock", "w")

	def __enter__(self):
		fcntl.flock(self.file, fcntl.LOCK_EX)
		return self

	def __exit__(self, *exc):
		self.file.close()


def isLocal(mainDir):
	try:
		return (Path(mainDir) / ".hook-only" / "flow-mode").read_text().strip() == "local"
	except OSError:
		return False


def git(mainDir, *args):
	return subprocess.run(["git", "-C", str(mainDir), *args], capture_output=True, text=True, timeout=30)


def integration(mainDir):
	for branch in ("main", "master"):
		if git(mainDir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
			return branch
	return None


def limits(mainDir):
	defaults = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
	out = {"workers": int(defaults["queue_max_workers"]), "usd": float(defaults["queue_max_concurrent_usd"])}
	branch = integration(mainDir)
	shown = git(mainDir, "show", f"{branch}:.claude/flow.json") if branch else None
	if shown is not None and shown.returncode == 0:
		try:
			repo = json.loads(shown.stdout).get("queue") or {}
		except ValueError:
			repo = {}
		if "queue_max_workers" in repo:
			out["workers"] = int(repo["queue_max_workers"])
		if "queue_max_concurrent_usd" in repo:
			out["usd"] = float(repo["queue_max_concurrent_usd"])
	return out


def spec(mainDir, topic):
	"""The planning session's queue entry, checked; raises Refused with what is wrong."""
	folder = Path(mainDir) / "plans" / "queue"
	data = wt_controls.loadJson(folder / f"{topic}.json")
	if data is None:
		raise Refused(f"no plans/queue/{topic}.json")
	if not (folder / f"{topic}.md").is_file():
		raise Refused(f"no plans/queue/{topic}.md")
	match = BRANCH.match(str(data.get("branch") or ""))
	if not match or match.group(2) != topic:
		raise Refused(f"plans/queue/{topic}.json: branch must be <feature|fix>/<scope>/{topic}")
	estimate = data.get("estimate")
	if not isinstance(estimate, dict) or not isinstance(estimate.get("minutes"), (int, float)) \
			or not isinstance(estimate.get("est_usd"), (int, float)):
		raise Refused(f"plans/queue/{topic}.json: estimate needs minutes and est_usd (cost-history.py estimate)")
	after, resources = data.get("after") or [], data.get("resources") or []
	if not all(isinstance(t, str) and wt_controls.TOPIC.match(t) for t in after):
		raise Refused(f"plans/queue/{topic}.json: after must list topic names")
	if not all(isinstance(t, str) and TAG.match(t) for t in resources):
		raise Refused(f"plans/queue/{topic}.json: resources must be short lowercase tags")
	order = data.get("order")
	if order is not None and not isinstance(order, int):
		raise Refused(f"plans/queue/{topic}.json: order must be a whole number")
	return {"branch": data["branch"], "after": after, "resources": resources, "estimate": estimate, "order": order}


def lastRun(mainDir, topic):
	state = wt_controls.loadJson(Path(mainDir) / ".hook-only" / "workers" / f"{topic}.json") or {}
	return state.get("run") if isinstance(state.get("run"), int) else None


def busyUsd(mainDir, topic):
	"""(busy, estimate USD) for a topic: a run going, or an automatic resume or merge run decided and not
	started yet (its run's exit file is written before the next one starts)."""
	worktree = wt_controls.worktreeOf(mainDir, topic)
	run = lastRun(mainDir, topic)
	if run is None:
		return False, 0.0
	runs = worktree / ".hook-only" / "runs"
	# est-<n>.json is written at the launch itself, start-<n> only once the wrapper runs: a topic started a
	# moment ago is busy already, or one tick could start more than the limits allow.
	est = wt_controls.loadJson(runs / f"est-{run}.json")
	try:
		ended = (runs / f"exit-{run}").read_text(encoding="utf-8").strip() != ""
	except OSError:
		ended = False
	if est is not None and not ended:
		return True, float(est.get("usd") or 0)
	decision = wt_controls.loadJson(runs / f"auto-{run}.json") or {}
	if decision.get("decision") in ("resume", "merge") and not (runs / f"start-{run + 1}").exists():
		return True, float(decision.get("usd") or 0)
	return False, 0.0


def recordedMerge(mainDir, topic):
	"""Whether auto-merge.py (or `wt merge`) recorded merging topic (<main>/.hook-only/auto-merge/merges.jsonl)."""
	try:
		lines = (Path(mainDir) / ".hook-only" / "auto-merge" / "merges.jsonl").read_text(encoding="utf-8").splitlines()
	except OSError:
		return False
	for line in lines:
		try:
			if json.loads(line).get("topic") == topic:
				return True
		except ValueError:
			continue
	return False


def isMerged(mainDir, topic, branchOf):
	"""Whether topic's branch is merged into the local main (its branch from the queue spec, else any local
	branch named */*/<topic>): a merge auto-merge.py or `wt merge` recorded, or a branch tip that main contains
	off its first-parent line, i.e. brought in by a merge commit. Being an ancestor alone is not enough: a branch
	just created from main, with no commit yet, is an ancestor too (2026-11-01: a queued topic showed "merged"
	the moment it started, and a topic waiting `after` it would have started at its first run's end). A
	fast-forward merge nobody recorded counts as not merged; local mode always merges --no-ff."""
	target = integration(mainDir)
	if not target:
		return False
	if recordedMerge(mainDir, topic):
		return True
	branches = [branchOf(topic)] if branchOf(topic) else [
		line.strip() for line in git(mainDir, "for-each-ref", "--format=%(refname:short)",
			f"refs/heads/*/*/{topic}").stdout.splitlines() if line.strip()]
	firstParent = None
	for b in branches:
		tip = git(mainDir, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}")
		if tip.returncode != 0 or git(mainDir, "merge-base", "--is-ancestor", b, target).returncode != 0:
			continue
		if firstParent is None:
			firstParent = set(git(mainDir, "rev-list", "--first-parent", target).stdout.split())
		if tip.stdout.strip() not in firstParent:
			return True
	return False


def busyTopics(mainDir):
	"""{topic: (usd, resources)} for every busy topic with a worktree, queued or not."""
	busy = {}
	for topic in wt_controls.topics(mainDir):
		isBusy, usd = busyUsd(mainDir, topic)
		if isBusy:
			try:
				resources = spec(mainDir, topic)["resources"]
			except Refused:
				resources = []
			busy[topic] = (usd, set(resources))
	return busy


def quotaReason(now=None, usageFile=None):
	"""Why the subscription's usage stops new starts now (module doc), or None."""
	now = time.time() if now is None else now
	defaults = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
	snap = wt_controls.loadJson(usageFile or USAGE_FILE)
	limitOf = {"five_hour": float(defaults["queue_max_five_hour_pct"]),
		"seven_day": float(defaults["queue_max_seven_day_pct"])}
	unknown = None
	if snap is None:
		unknown = "no usage snapshot (quota-snapshot.py in the status line writes it)"
	else:
		live = {w: snap[w] for w in limitOf if isinstance(snap.get(w), dict) and snap[w].get("resets_at", 0) > now}
		for window, w in live.items():
			if float(w.get("used_percentage", 0)) >= limitOf[window]:
				until = time.strftime("%Y-%m-%d %H:%M", time.localtime(w["resets_at"]))
				return f"{window} usage {w['used_percentage']:.0f}% >= {limitOf[window]:.0f}% until {until}"
		age = (now - float(snap.get("at", 0))) / 60
		if live and age > float(defaults["queue_quota_max_age_minutes"]):
			unknown = f"usage snapshot is {age:.0f} min old (queue_quota_max_age_minutes {defaults['queue_quota_max_age_minutes']})"
	if unknown and defaults.get("queue_quota_unknown", "wait") != "start":
		return f"usage unknown: {unknown}; an interactive session's status line refreshes it (queue_quota_unknown wait)"
	return None


def waitReason(mainDir, item, entry, busy, cap, quota=None):
	"""Why item can't start now (module doc, the order of the checks), or None."""
	def branchOf(topic):
		try:
			return spec(mainDir, topic)["branch"]
		except Refused:
			return None
	if quota:
		return quota
	pending = [t for t in entry["after"] if not isMerged(mainDir, t, branchOf)]
	if pending:
		return f"waits for {', '.join(pending)} to be merged into the local main"
	usd = float(entry["estimate"]["est_usd"])
	if usd > cap["usd"]:
		return f"stuck: its estimate ${usd} alone is above queue_max_concurrent_usd ${cap['usd']}"
	if len(busy) >= cap["workers"]:
		return f"{len(busy)} worker(s) busy (queue_max_workers {cap['workers']})"
	total = sum(u for u, _ in busy.values())
	if total + usd > cap["usd"]:
		return f"busy estimates ${total:.2f} + ${usd} exceed queue_max_concurrent_usd ${cap['usd']}"
	shared = sorted({tag for _, tags in busy.values() for tag in tags} & set(entry["resources"]))
	if shared:
		return f"shares {', '.join(shared)} with a busy topic"
	return None


def materialize(mainDir, topic, entry, newWorktree=None):
	"""Worktree from the main of now, the plan inside, the estimate file: ready for `wt launch`."""
	env = {**os.environ, "FLOW_INTEGRATION": "local"}
	result = subprocess.run(["bash", str(newWorktree or NEW_WORKTREE), entry["branch"]], cwd=str(mainDir), env=env,
		capture_output=True, text=True, timeout=120)
	if result.returncode != 0:
		raise Refused(f"new-worktree.sh: {(result.stderr or result.stdout).strip()[-300:]}")
	worktree = wt_controls.worktreeOf(mainDir, topic)
	(worktree / "plans").mkdir(parents=True, exist_ok=True)
	shutil.copyfile(Path(mainDir) / "plans" / "queue" / f"{topic}.md", worktree / "plans" / f"{topic}.md")
	estimateFile = Path(mainDir) / "plans" / "worker" / f"{topic}.json"
	estimateFile.parent.mkdir(parents=True, exist_ok=True)
	estimateFile.write_text(json.dumps(entry["estimate"]), encoding="utf-8")


def launch(mainDir, topic, workerControl=None):
	"""`wt launch <topic>` through worker-control.py, as typed; returns its context text."""
	result = subprocess.run([sys.executable, str(workerControl or WORKER_CONTROL)], input=json.dumps(
		{"prompt": f"wt launch {topic}", "cwd": str(mainDir)}), capture_output=True, text=True, timeout=60,
		env={**os.environ, "FLOW_INTEGRATION": "local"})
	try:
		out = json.loads(result.stdout or "{}")
	except ValueError:
		out = {}
	if out.get("decision") == "block":
		raise Refused(out.get("reason") or "worker-control refused")
	context = (out.get("hookSpecificOutput") or {}).get("additionalContext")
	if result.returncode != 0 or not context:
		raise Refused(f"worker-control.py gave no launch: {(result.stderr or result.stdout).strip()[-300:]}")
	return context


def prune(mainDir, state, now=None):
	"""Module doc, done items: mark, archive the queue files, and drop those done longer than the linger."""
	now = time.time() if now is None else now
	linger = float(json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))["queue_done_linger_minutes"]) * 60
	before = json.dumps(state["items"], sort_keys=True)
	kept = []
	for item in state["items"]:
		topic = item["topic"]
		if item.get("started") and not item.get("error") and not item.get("done") \
				and not wt_controls.worktreeOf(mainDir, topic).exists():
			branch = None
			try:
				branch = spec(mainDir, topic)["branch"]
			except Refused:
				pass
			if isMerged(mainDir, topic, lambda t: branch if t == topic else None):
				item["done"] = now
				archived = Path(mainDir) / ".hook-only" / "archive" / topic / "queue"
				archived.mkdir(parents=True, exist_ok=True)
				for suffix in (".md", ".json"):
					source = Path(mainDir) / "plans" / "queue" / f"{topic}{suffix}"
					if source.exists():
						shutil.move(str(source), str(archived / source.name))
				log(mainDir, topic=topic, event="done")
		if item.get("done") and now - float(item["done"]) > linger:
			log(mainDir, topic=topic, event="removed")
			continue
		kept.append(item)
	state["items"] = kept
	return json.dumps(kept, sort_keys=True) != before


def tick(mainDir, start=None, quota=quotaReason):
	"""Start every item that may start now, in order. Returns [{"topic", "started"|"waits"|"error"}]."""
	mainDir = Path(mainDir)
	if not isLocal(mainDir):
		return []
	start = start or (lambda topic, entry: (materialize(mainDir, topic, entry), launch(mainDir, topic))[1])
	out = []
	with Locked(mainDir):
		state = loadState(mainDir)
		if prune(mainDir, state):
			saveState(mainDir, state)
		cap = limits(mainDir)
		for item in state["items"]:
			if item.get("started"):
				continue
			topic = item["topic"]
			try:
				entry = spec(mainDir, topic)
			except Refused as error:
				out.append({"topic": topic, "error": str(error)})
				continue
			busy = busyTopics(mainDir)
			why = waitReason(mainDir, item, entry, busy, cap, quota())
			if why:
				out.append({"topic": topic, "waits": why})
				continue
			try:
				context = start(topic, entry)
			except Refused as error:
				item["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
				item["error"] = str(error)
				log(mainDir, topic=topic, event="start failed", why=str(error))
				wt_events.emit(mainDir, "queue-failed", topic, str(error))
				out.append({"topic": topic, "error": str(error)})
				saveState(mainDir, state)
				continue
			item["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
			log(mainDir, topic=topic, event="started", context=context)
			wt_events.emit(mainDir, "queue-started", topic, context.removeprefix("wt: ")[:200])
			out.append({"topic": topic, "started": context})
			saveState(mainDir, state)
	return out


def add(mainDir, topics):
	"""The person's `wt queue <topic>...`: appended in that order, each spec checked first."""
	mainDir = Path(mainDir)
	if not isLocal(mainDir):
		raise Refused("the queue runs in local mode only (wt local on): it needs the local main to know what is merged")
	with Locked(mainDir):
		state = loadState(mainDir)
		queued = {item["topic"] for item in state["items"] if not item.get("started")}
		for topic in topics:
			if not wt_controls.TOPIC.match(topic):
				raise Refused(f"not a topic name: {topic}")
			spec(mainDir, topic)
			if topic in queued:
				raise Refused(f"{topic} is queued already")
			if wt_controls.worktreeOf(mainDir, topic).exists():
				raise Refused(f"{topic} has a worktree already: start it with wt launch")
		for topic in topics:
			state["items"].append({"topic": topic, "added": time.strftime("%Y-%m-%dT%H:%M:%S")})
			log(mainDir, topic=topic, event="queued")
		saveState(mainDir, state)


def drop(mainDir, topic):
	"""The person's `wt queue drop <topic>`: an item not started yet, or one whose start failed."""
	with Locked(Path(mainDir)):
		state = loadState(mainDir)
		kept = [i for i in state["items"] if i["topic"] != topic or (i.get("started") and not i.get("error"))]
		if len(kept) == len(state["items"]):
			raise Refused(f"{topic} is not waiting in the queue")
		state["items"] = kept
		log(mainDir, topic=topic, event="dropped")
		saveState(mainDir, state)


def status(mainDir):
	"""Every item with its state: waiting (and why), started (its run), merged, or error."""
	mainDir = Path(mainDir)
	try:
		with Locked(mainDir):
			state = loadState(mainDir)
			if prune(mainDir, state):
				saveState(mainDir, state)
	except OSError:
		# Read-only here (the planning session's sandboxed Bash can't write .hook-only/): show without pruning;
		# the pane and the hooks, outside the sandbox, prune on their next call.
		state = loadState(mainDir)
	cap = limits(mainDir) if integration(mainDir) else {"workers": 0, "usd": 0.0}
	busy = busyTopics(mainDir)
	rows = []
	linger = float(json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))["queue_done_linger_minutes"]) * 60
	for item in state["items"]:
		topic = item["topic"]
		row = {"topic": topic}
		if item.get("done"):
			if time.time() - float(item["done"]) <= linger:
				rows.append({"topic": topic, "state": "merged", "run": lastRun(mainDir, topic)})
			continue
		try:
			entry = spec(mainDir, topic)
		except Refused as error:
			entry, row["error"] = None, str(error)
		if item.get("error"):
			row["error"] = item["error"]
		elif item.get("started"):
			merged = entry is not None and isMerged(mainDir, topic, lambda t: entry["branch"] if t == topic else None)
			row["state"] = "merged" if merged else "started"
			row["run"] = lastRun(mainDir, topic)
		elif entry is not None:
			row["state"] = "waiting"
			row["why"] = waitReason(mainDir, item, entry, busy, cap, quotaReason()) or "starts at the next tick"
		rows.append(row)
	return {"local": isLocal(mainDir), "limits": cap, "items": rows, "planned": planned(mainDir, state)}


def planned(mainDir, state):
	"""plans/queue/<topic>.json not in the queue and without a worktree, by "order" then name: what the
	pane offers to queue. A broken spec is listed with its error, so the planning session sees it."""
	folder = Path(mainDir) / "plans" / "queue"
	try:
		names = sorted(n[:-5] for n in os.listdir(folder) if n.endswith(".json"))
	except OSError:
		return []
	known = {item["topic"] for item in state["items"]}
	rows = []
	for topic in names:
		if not wt_controls.TOPIC.match(topic) or topic in known or wt_controls.worktreeOf(mainDir, topic).exists():
			continue
		try:
			entry = spec(mainDir, topic)
			rows.append({"topic": topic, "order": entry["order"], "after": entry["after"],
				"est_usd": entry["estimate"]["est_usd"]})
		except Refused as error:
			rows.append({"topic": topic, "error": str(error)})
	big = 10 ** 9
	return sorted(rows, key=lambda r: (r.get("order") if isinstance(r.get("order"), int) else big, r["topic"]))


def main(argv):
	if len(argv) != 3 or argv[1] not in ("tick", "status"):
		sys.exit("usage: wt_queue.py tick|status <main checkout>")
	print(json.dumps(tick(argv[2]) if argv[1] == "tick" else status(argv[2])))


if __name__ == "__main__":
	main(sys.argv)

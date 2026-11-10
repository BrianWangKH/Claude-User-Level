#!/usr/bin/env python3
"""The topic queue (local mode): plans the person approved run one after another, as many at
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
  - the subscription's usage is at a limit: the state kind quota-usage (<home>/state/quota/usage.json, written by
    quota-snapshot.py from the status line, read through statefile) says the 5-hour or the weekly window is at queue_max_five_hour_pct / queue_max_seven_day_pct or
    above and hasn't reset yet; a window past its resets_at counts as fresh. With no snapshot (a file without
    "schema": 1, as older versions wrote the snapshot, reads as none), or one
    older than queue_quota_max_age_minutes with a window not reset since (usage only grows, and the
    workers report none), queue_quota_unknown decides: "wait" (the default) or "start". These are the
    person's, not the repo's: home.json budget only
  - a topic in "after" has no branch merged into the local main yet (a stuck one blocks only its dependents);
    one still waiting in the queue is not merged, and a merge record counts only from that topic's start in
    the queue and only for its current branch, so a topic name planned again doesn't inherit an old merge
  - its estimate alone is above queue_max_concurrent_usd: stuck, the person raises the limit or starts it
  - the busy topics already number queue_workers_max, or their estimates plus its own exceed
    queue_max_concurrent_usd. Busy is hooks/lib/runstate.py status "busy" (busy_usd its USD): a run launching,
    running or finishing, one whose state can't be read, a suspected orphan (it may still run), or an ended
    run whose automatic resume, merge or fix run is decided and not started yet. tick (outside the sandbox)
    checks orphans' wrapper pids (runstate.verifyProcess): one found gone is confirmed (recorded once in its run
    record, a run-orphaned event) and no longer busy; the queue never starts that topic again (it started
    already), the person resumes it
  - it shares a resource tag with a busy topic (tags are the repo's: opaque words such as an API quota's
    name; two topics with the same tag never run together)
Items go in the person's order; a waiting item doesn't hold back a later one that may start.
Limits: home.json budget "queue_workers_max", "queue_max_concurrent_usd" (local.json may override them); a repo
may set its own in .claude/flow.json {"queue": {"queue_workers_max": n, "queue_max_concurrent_usd": x}}, read
from the local main's commit (git show), not the working tree, and the smaller of the two applies (limits(),
with the source of each: home, local or repo); a flow.json that doesn't parse, or has an unknown or old key,
is an error, never ignored.
Starting one: new-worktree.sh <branch> from the main checkout (the branch from the main of that moment),
the plan copied to <worktree>/plans/<topic>.md, the estimate written to plans/worker/<topic>.json, then
worker-control.py with the prompt `wt launch <topic>`, the same path as a typed launch.
Config mode TEST (hooks/lib/config_mode_guard.py) outside a rehearsal repo: tick starts nothing and creates no
worktree; every item waits with the refusal (before materialize, so nothing is made for a run that can't start).
Local mode only (homeconfig.flowMode of the main checkout): "after" needs the local main to say what is merged;
the local main is homeconfig.integrationBranch in local mode whatever the main checkout's mode, since the queue
reads it only there (limits, isMerged).
Done items leave: once a started topic's branch is merged and its worktree removed (auto-finish, or
/finish-worktree), a tick marks it "done" and moves plans/queue/<topic>.md and .json to
<main>/.hook-only/archive/<topic>/queue/; status shows it as merged for queue_done_linger_minutes
(home.json budget), then it is removed. status() prunes too (under the lock; skipped where .hook-only/ is
read-only, e.g. the planning session's sandboxed Bash, which then only reads), so a merged topic leaves
the pane, which polls status, even when no run ends and nobody types `wt queue` (otherwise a merged
topic stays listed until the next tick). Starts and failed starts are events (wt_events.py).
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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import config_mode_guard  # noqa: E402
import homeconfig  # noqa: E402
import statefile  # noqa: E402
import wt_events  # noqa: E402
import wt_topics  # noqa: E402

QUOTA_KIND = "quota-usage"  # state-registry, location home: quota-snapshot.py's file
NEW_WORKTREE = HERE.parent.parent / "skills" / "new-worktree" / "new-worktree.sh"
WORKER_CONTROL = HERE / "worker-control.py"
BRANCH = re.compile(r"^(feature|fix)/[a-z0-9]+/([a-z0-9]+(?:-[a-z0-9]+){1,3})$")
TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class Refused(Exception):
	pass


def queueDir(mainDir):
	return Path(mainDir) / ".hook-only" / "queue"


def loadState(mainDir):
	return wt_topics.loadJson(queueDir(mainDir) / "queue.json") or {"items": []}


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


def git(mainDir, *args):
	return subprocess.run(["git", "-C", str(mainDir), *args], capture_output=True, text=True, timeout=30)


FLOW_FILE = ".claude/flow.json"
# The queue limits a repo may lower in .claude/flow.json "queue": limit -> (home.json budget key, type).
LIMIT_KEYS = {"workers": ("queue_workers_max", int), "usd": ("queue_max_concurrent_usd", float)}


def repoQueue(mainDir, branch):
	"""The "queue" object of .claude/flow.json at the local main's commit ({} without the file); ConfigError
	for broken JSON, a non-object, an unknown or old (renamed) key or a value that isn't a number above 0."""
	shown = git(mainDir, "show", f"{branch}:{FLOW_FILE}") if branch else None
	if shown is None or shown.returncode != 0:
		return {}
	where = f"{FLOW_FILE} at {branch}"
	try:
		data = json.loads(shown.stdout)
	except ValueError as error:
		raise homeconfig.ConfigError(f"{where}: not valid JSON ({error})") from error
	queue = data.get("queue") if isinstance(data, dict) else None
	if queue is None:
		return {}
	if not isinstance(queue, dict):
		raise homeconfig.ConfigError(f"{where}: \"queue\" must be an object")
	known = {key for key, _ in LIMIT_KEYS.values()}
	for key, value in queue.items():
		if key.startswith("_"):
			continue
		new = homeconfig.renamedKey(("budget", key))
		if new:
			raise homeconfig.ConfigError(f"{where}: queue.{key} was renamed to queue.{new.split('.', 1)[1]}; rename it")
		if key not in known:
			raise homeconfig.ConfigError(f"{where}: unknown key queue.{key} (known: {', '.join(sorted(known))})")
		if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
			raise homeconfig.ConfigError(f"{where}: queue.{key} must be a number above 0, got {value!r}")
	return queue


def limits(mainDir):
	"""{"workers", "usd", "source": {limit: "home" | "local" | "repo"}}: each limit is the smaller of the
	person's (home.json budget, local.json applied) and the repo's .claude/flow.json "queue" (read from the
	local main's commit, not the working tree); the source names where the effective value comes from (the
	person's on a tie). ConfigError when flow.json can't be read (repoQueue)."""
	branch = homeconfig.integrationBranch(str(mainDir), "local")
	repo = repoQueue(mainDir, branch)
	out, source = {}, {}
	for limit, (key, kind) in LIMIT_KEYS.items():
		personal = kind(homeconfig.get("budget", key))
		out[limit], source[limit] = personal, homeconfig.source("budget", key)
		if key in repo and kind(repo[key]) < personal:
			out[limit], source[limit] = kind(repo[key]), "repo"
	out["source"] = source
	return out


def spec(mainDir, topic):
	"""The planning session's queue entry, checked; raises Refused with what is wrong."""
	folder = Path(mainDir) / "plans" / "queue"
	data = wt_topics.loadJson(folder / f"{topic}.json")
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
	if not all(isinstance(t, str) and wt_topics.TOPIC.match(t) for t in after):
		raise Refused(f"plans/queue/{topic}.json: after must list topic names")
	if not all(isinstance(t, str) and TAG.match(t) for t in resources):
		raise Refused(f"plans/queue/{topic}.json: resources must be short lowercase tags")
	order = data.get("order")
	if order is not None and not isinstance(order, int):
		raise Refused(f"plans/queue/{topic}.json: order must be a whole number")
	return {"branch": data["branch"], "after": after, "resources": resources, "estimate": estimate, "order": order}


def runStatus(mainDir, topic, pidAlive=None):
	"""runstate.status of the topic's newest run: in its worktree when there is one, else where runstate finds
	it (the archive); the error text when it can't be read."""
	import runstate

	worktree = wt_topics.worktreeOf(mainDir, topic)
	try:
		return runstate.status(str(mainDir), topic, root=str(worktree) if worktree.is_dir() else None,
			pidAlive=pidAlive), None
	except Exception as error:  # a state nobody can read: busy, so nothing starts on top of it
		return None, f"{type(error).__name__}: {error}"


def lastRun(mainDir, topic):
	"""The topic's newest run number (runstate.status), None without one or when it can't be read."""
	status, _error = runStatus(mainDir, topic)
	return (status or {}).get("run")


def busyUsd(mainDir, topic, pidAlive=None):
	"""(busy, estimate USD) for a topic (module doc, busy). A launched run is busy from its launch (runstate
	reads the launch before the wrapper starts), so one tick can't start more than the limits allow. pidAlive
	(tick only, outside the sandbox): runstate.verifyProcess; a confirmed orphan is recorded (confirmOrphan,
	once)."""
	status, _error = runStatus(mainDir, topic, pidAlive)
	if status is None:
		return True, 0.0
	if status["phase"] == "orphan" and status["confidence"] == "confirmed" and status["source"] == "record":
		recordOrphan(status)
	return status["busy"], float(status["busy_usd"] or 0) if status["busy"] else 0.0


def recordOrphan(status):
	"""A pid check found an orphan's wrapper gone: record it in the run record (runstate.confirmOrphan writes
	orphan.confirmed_at and the run-orphaned event only the first time)."""
	import runstate

	root = os.path.dirname(os.path.dirname(status["runs_dir"]))
	evidence = {"wrapper_pid": (status["process"] or {}).get("wrapper_pid"), "checked": "gone"}
	runstate.dualWrite(root, "wt-queue", runstate.confirmOrphan, status["run"], "wt-queue", evidence)


def recordedMerge(mainDir, topic, since=None, tips=()):
	"""Whether auto-merge.py (or `wt merge`) recorded merging topic (<main>/.hook-only/auto-merge/merges.jsonl).

	A topic name can be planned again after its merge, and the old record must not count for the new one:
	a record counts only when it is not older than `since` (the queue item's start, local time like the
	record's "t") and, when the topic's branch exists (`tips`, its commit ids), only when its merge commit
	contains one of those tips."""
	try:
		lines = (Path(mainDir) / ".hook-only" / "auto-merge" / "merges.jsonl").read_text(encoding="utf-8").splitlines()
	except OSError:
		return False
	for line in lines:
		try:
			record = json.loads(line)
		except ValueError:
			continue
		if not isinstance(record, dict) or record.get("topic") != topic:
			continue
		if since and str(record.get("t") or "") < since:
			continue
		commit = record.get("commit")
		if tips and not (isinstance(commit, str) and any(
				git(mainDir, "merge-base", "--is-ancestor", tip, commit).returncode == 0 for tip in tips)):
			continue
		return True
	return False


def isMerged(mainDir, topic, branchOf, since=None):
	"""Whether topic's branch is merged into the local main (its branch from the queue spec, else any local
	branch named */*/<topic>): a merge auto-merge.py or `wt merge` recorded (recordedMerge: from `since` on,
	for the branch that exists now), or a branch tip that main contains off its first-parent line, i.e.
	brought in by a merge commit. Being an ancestor alone is not enough: a branch just created from main,
	with no commit yet, is an ancestor too (a queued topic would show "merged" the moment it started,
	and a topic waiting `after` it would have started at its first run's end). A fast-forward merge nobody
	recorded counts as not merged; local mode always merges --no-ff."""
	target = homeconfig.integrationBranch(str(mainDir), "local")
	if not target:
		return False
	branches = [branchOf(topic)] if branchOf(topic) else [
		line.strip() for line in git(mainDir, "for-each-ref", "--format=%(refname:short)",
			f"refs/heads/*/*/{topic}").stdout.splitlines() if line.strip()]
	tips = []
	for b in branches:
		tip = git(mainDir, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}")
		if tip.returncode == 0:
			tips.append(tip.stdout.strip())
	if recordedMerge(mainDir, topic, since, tips):
		return True
	firstParent = None
	for tip in tips:
		if git(mainDir, "merge-base", "--is-ancestor", tip, target).returncode != 0:
			continue
		if firstParent is None:
			firstParent = set(git(mainDir, "rev-list", "--first-parent", target).stdout.split())
		if tip not in firstParent:
			return True
	return False


def dependencyMerged(mainDir, topic, branchOf, queued):
	"""isMerged for a topic in "after", given the queue's items by topic: one still waiting in the queue is
	not merged, whatever an older record of the same name says; one the queue started counts from its start."""
	item = queued.get(topic)
	if item is not None and not item.get("started"):
		return False
	return isMerged(mainDir, topic, branchOf, since=(item or {}).get("started"))


def byTopic(state):
	"""The queue's items by topic; a topic queued again after a done item maps to its newest item."""
	return {item["topic"]: item for item in state["items"]}


def busyTopics(mainDir, pidAlive=None):
	"""{topic: (usd, resources)} for every busy topic with a worktree, queued or not."""
	busy = {}
	for topic in wt_topics.topics(mainDir):
		isBusy, usd = busyUsd(mainDir, topic, pidAlive)
		if isBusy:
			try:
				resources = spec(mainDir, topic)["resources"]
			except Refused:
				resources = []
			busy[topic] = (usd, set(resources))
	return busy


def quotaReason(now=None):
	"""Why the subscription's usage stops new starts now (module doc), or None. A snapshot of another schema
	(the schema-less file of older versions included) reads as none: usage unknown."""
	now = time.time() if now is None else now
	defaults = homeconfig.load("budget")
	snap = statefile.read(None, QUOTA_KIND)
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
	if unknown and defaults["queue_quota_unknown"] != "start":
		return f"usage unknown: {unknown}; an interactive session's status line refreshes it (queue_quota_unknown wait)"
	return None


def sourceOf(cap, limit):
	"""", from <source>" for a limit's message (limits()), or "" when the caller gave no source."""
	source = (cap.get("source") or {}).get(limit)
	return f", from {source}" if source else ""


def waitReason(mainDir, item, entry, busy, cap, quota=None, queued=None):
	"""Why item can't start now (module doc, the order of the checks), or None."""
	def branchOf(topic):
		try:
			return spec(mainDir, topic)["branch"]
		except Refused:
			return None
	if quota:
		return quota
	pending = [t for t in entry["after"] if not dependencyMerged(mainDir, t, branchOf, queued or {})]
	if pending:
		return f"waits for {', '.join(pending)} to be merged into the local main"
	usd = float(entry["estimate"]["est_usd"])
	if usd > cap["usd"]:
		return f"stuck: its estimate ${usd} alone is above queue_max_concurrent_usd ${cap['usd']}{sourceOf(cap, 'usd')}"
	if len(busy) >= cap["workers"]:
		return f"{len(busy)} worker(s) busy (queue_workers_max {cap['workers']}{sourceOf(cap, 'workers')})"
	total = sum(u for u, _ in busy.values())
	if total + usd > cap["usd"]:
		return f"busy estimates ${total:.2f} + ${usd} exceed queue_max_concurrent_usd ${cap['usd']}{sourceOf(cap, 'usd')}"
	shared = sorted({tag for _, tags in busy.values() for tag in tags} & set(entry["resources"]))
	if shared:
		return f"shares {', '.join(shared)} with a busy topic"
	return None


def materialize(mainDir, topic, entry, newWorktree=None):
	"""Worktree from the main of now, the plan inside, the estimate file: ready for `wt launch`."""
	result = subprocess.run(["bash", str(newWorktree or NEW_WORKTREE), entry["branch"]], cwd=str(mainDir),
		capture_output=True, text=True, timeout=120)
	if result.returncode != 0:
		raise Refused(f"new-worktree.sh: {(result.stderr or result.stdout).strip()[-300:]}")
	worktree = wt_topics.worktreeOf(mainDir, topic)
	(worktree / "plans").mkdir(parents=True, exist_ok=True)
	shutil.copyfile(Path(mainDir) / "plans" / "queue" / f"{topic}.md", worktree / "plans" / f"{topic}.md")
	estimateFile = Path(mainDir) / "plans" / "worker" / f"{topic}.json"
	estimateFile.parent.mkdir(parents=True, exist_ok=True)
	estimateFile.write_text(json.dumps(entry["estimate"]), encoding="utf-8")


def launch(mainDir, topic, workerControl=None):
	"""`wt launch <topic>` through worker-control.py, as typed; returns its context text. "started_by" tells
	worker-control the queue started it (the run record's started_by)."""
	result = subprocess.run([sys.executable, str(workerControl or WORKER_CONTROL)], input=json.dumps(
		{"prompt": f"wt launch {topic}", "cwd": str(mainDir), "started_by": "queue"}), capture_output=True, text=True, timeout=60)
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
	linger = float(homeconfig.load("budget")["queue_done_linger_minutes"]) * 60
	before = json.dumps(state["items"], sort_keys=True)
	kept = []
	for item in state["items"]:
		topic = item["topic"]
		if item.get("started") and not item.get("error") and not item.get("done") \
				and not wt_topics.worktreeOf(mainDir, topic).exists():
			branch = None
			try:
				branch = spec(mainDir, topic)["branch"]
			except Refused:
				pass
			if isMerged(mainDir, topic, lambda t: branch if t == topic else None, since=item["started"]):
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


def checkPid():
	"""tick's pid check (it runs outside the sandbox: run-worker.sh, worker-control.py, auto-finish.py)."""
	import runstate

	return runstate.verifyProcess


def tick(mainDir, start=None, quota=quotaReason):
	"""Start every item that may start now, in order. Returns [{"topic", "started"|"waits"|"error"}]."""
	mainDir = Path(mainDir)
	if homeconfig.flowMode(str(mainDir)) != "local":
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
			refused = config_mode_guard.refusal(mainDir)
			if refused:  # before materialize: a TEST mode tick creates no worktree for a topic it can't start
				out.append({"topic": topic, "waits": refused})
				continue
			busy = busyTopics(mainDir, pidAlive=checkPid())
			why = waitReason(mainDir, item, entry, busy, cap, quota(), queued=byTopic(state))
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
	if homeconfig.flowMode(str(mainDir)) != "local":
		raise Refused("the queue runs in local mode only (wt local on): it needs the local main to know what is merged")
	with Locked(mainDir):
		state = loadState(mainDir)
		queued = {item["topic"] for item in state["items"] if not item.get("started")}
		for topic in topics:
			if not wt_topics.TOPIC.match(topic):
				raise Refused(f"not a topic name: {topic}")
			spec(mainDir, topic)
			if topic in queued:
				raise Refused(f"{topic} is queued already")
			if wt_topics.worktreeOf(mainDir, topic).exists():
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
	cap = limits(mainDir) if homeconfig.integrationBranch(str(mainDir), "local") else {"workers": 0, "usd": 0.0, "source": {}}
	busy = busyTopics(mainDir)
	rows = []
	linger = float(homeconfig.load("budget")["queue_done_linger_minutes"]) * 60
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
			merged = entry is not None and isMerged(mainDir, topic, lambda t: entry["branch"] if t == topic else None,
				since=item["started"])
			row["state"] = "merged" if merged else "started"
			row["run"] = lastRun(mainDir, topic)
		elif entry is not None:
			row["state"] = "waiting"
			row["why"] = waitReason(mainDir, item, entry, busy, cap, quotaReason(), queued=byTopic(state)) \
				or "starts at the next tick"
		rows.append(row)
	return {"local": homeconfig.flowMode(str(mainDir)) == "local", "limits": cap, "items": rows, "planned": planned(mainDir, state)}


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
		if not wt_topics.TOPIC.match(topic) or topic in known or wt_topics.worktreeOf(mainDir, topic).exists():
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

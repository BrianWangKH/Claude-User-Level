#!/usr/bin/env python3
"""What a person (the wt-pane buttons), auto mode or `wt ...` may do for each topic: the one place that decides.

Usage: wt_controls.py <main checkout> [--local on|off]
Prints {"schema": 1, "controls": [row, ...], "plans_visible": bool, "queue": [item, ...], "planned": [...]}
and, with --local, "local_blocker": why the flow mode can't
switch that way now, or null. A row: {"topic", "action": "launch"|"resume"|"stop" (absent: nothing to
send), "why" (absent with an action), "run", "held", "auto": {"ok": bool, "why"}, and, when the last run's
automatic merge was refused for a reason a person can approve (not "merge_run"), "merge": {"reason",
"files": [changed paths]}: the pane's `merge` button (`wt merge <topic>`); while that approval runs,
"approving": {"run", "since": "HH:MM"} instead of "merge" (no button: a second approval is refused)}. The wt-pane mod runs
this and only draws the rows (its buttons still submit `wt <action> <topic>`); worker-control.py imports
the same functions, so a typed `wt resume` and a button agree. Schema 1 is the contract: a field
removed or renamed means schema 2. "plans_visible" is false when <main>/plans is missing or empty: Claude
Code's sandbox mounts an empty folder over it (2026-11-01), and a planning checkout always holds at least
plans/repo-plan.md, so a caller can say so instead of showing every topic as
"no estimate yet".

Files read (the same worker-control.py writes or reads):
  <main>/../worktrees/<topic>/.git              the worktree
  <main>/plans/worker/<topic>.json              the planning session's estimate (needed to start)
  <main>/.hook-only/workers/<topic>.json        wt's state: pid, session_id, run
  <worktree>/.hook-only/runs/start-<n> exit-<n> est-<n>.json result-<n>.json
  <worktree>/.hook-only/agent-guard/HOLD        an ALERT the person has not decided
  <worktree>/.hook-only/runs/approve-<n>.json   a `wt merge` approval of run n in progress
Decisions:
  running   the state's run has its start file and no exit file yet (or an empty one), as run-worker.sh
            writes them. Files, not the pid: a sandboxed caller (the pane's process may be one) can't see
            the worker's pid, and would show a running worker as resumable. A run killed without an exit
            file reads as running; worker-control.py, outside the sandbox, checks the real process and
            refuses its stop with "no running worker"
  done      the last run ended `STATUS: done` and the estimate file is older than the est-<k>.json of the
            last run the user started (written at its start): nothing to resume until the planning session
            writes a new estimate (a merge run: estimate branch --merge). By time, not by numbers: two merge
            estimates can be equal (revenue-derived, 2026-11-01: a resume pressed after done ran a run 4)
  auto      never stop, never across a HOLD (resume lifts it, and that is the person's decision)
  approving `wt merge` wrote runs/approve-<run>.json ({"run", "pid", "started", "started_epoch"}) before it
            started auto-merge.py --person, which removes it when it ends (merged, refused or error). Files,
            not the pid, as for running: the record counts while merge-<run>.json still says refused and
            is not newer than the record. A record left by a killed auto-merge reads as approving here;
            worker-control.py, outside the sandbox, checks the pid and lets a typed `wt merge` start again
  local     no switch while a worker runs or a topic is planned but not launched (its branch is from the
            other mode's base); not on while the local main lags origin/main
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "run-worker.sh"
TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HOLD_WHY = "the worktree has an agent-guard HOLD, and the user decides the ALERT first"


def loadJson(path):
	try:
		value = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None
	return value if isinstance(value, dict) else None


def alive(pid):
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		return True
	return True


def isOurWrapper(pid, topic, wrapper=WRAPPER):
	try:
		argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
	except OSError:
		return False
	return str(wrapper).encode() in argv and topic.encode() in argv


def isRunning(worktree, run):
	"""Module doc, running (files only)."""
	if not isinstance(run, int):
		return False
	runs = Path(worktree) / ".hook-only" / "runs"
	if not (runs / f"start-{run}").exists():
		return False
	try:
		return (runs / f"exit-{run}").read_text(encoding="utf-8").strip() == ""
	except OSError:
		return True


def worktreeOf(mainDir, topic):
	return Path(mainDir).parent / "worktrees" / topic


def firstLine(path):
	text = str((loadJson(path) or {}).get("result") or "").strip()
	return (text.splitlines() or [""])[0].strip()


def lastUserEstimate(runs, run):
	"""est-<k>.json of the last run the user started (automatic runs write "auto": true), or None."""
	for k in range(run, 0, -1):
		path = Path(runs) / f"est-{k}.json"
		est = loadJson(path)
		if est is not None and not est.get("auto"):
			return path
	return None


def doneWithoutNewEstimate(worktree, run, estimateFile):
	"""Module doc, done."""
	runs = Path(worktree) / ".hook-only" / "runs"
	if not isinstance(run, int) or not firstLine(runs / f"result-{run}.json").startswith("STATUS: done"):
		return False
	last = lastUserEstimate(runs, run)
	try:
		return last is not None and Path(estimateFile).stat().st_mtime <= last.stat().st_mtime
	except OSError:
		return False


def mergeHeld(worktree, run):
	"""{"reason", "files"} when run ended and its automatic merge was refused for the person to approve."""
	runs = Path(worktree) / ".hook-only" / "runs"
	decision = loadJson(runs / f"merge-{run}.json") if isinstance(run, int) else None
	if not decision or decision.get("decision") != "refused" or decision.get("merge_run") or isRunning(worktree, run):
		return None
	files = []
	for branch in ("main", "master"):
		result = subprocess.run(["git", "-C", str(worktree), "diff", "--name-only", f"{branch}...HEAD"],
			capture_output=True, text=True, timeout=10)
		if result.returncode == 0:
			files = [line for line in result.stdout.splitlines() if line][:12]
			break
	return {"reason": str(decision.get("reason", ""))[:300], "files": files}


def approvalPath(worktree, run):
	return Path(worktree) / ".hook-only" / "runs" / f"approve-{run}.json"


def approving(worktree, run):
	"""Module doc, approving: {"run", "since": "HH:MM"} while a `wt merge` of run is in progress, else None."""
	if not isinstance(run, int):
		return None
	path = approvalPath(worktree, run)
	record = loadJson(path)
	decisionFile = Path(worktree) / ".hook-only" / "runs" / f"merge-{run}.json"
	decision = loadJson(decisionFile)
	if not record or not decision or decision.get("decision") != "refused":
		return None
	try:
		if decisionFile.stat().st_mtime > float(record.get("started_epoch") or 0):
			return None  # auto-merge wrote its decision after the record: finished, about to remove it
	except (OSError, TypeError, ValueError):
		return None
	return {"run": run, "since": str(record.get("started") or "")[11:16]}


def releaseApproval(worktree, run, pid, isAlive=alive):
	"""auto-merge.py, when it ends: remove run's approval record if it is this process's (pid) or its process
	is gone; a record of another live process stays. Under runs/approve.lock, which worker-control.py holds
	while it starts auto-merge.py and writes the record, so the record always carries the pid first."""
	import fcntl
	path = approvalPath(worktree, run)
	if not path.exists():
		return
	with open(path.parent / "approve.lock", "w") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		record = loadJson(path)
		owner = (record or {}).get("pid")
		if record is None or owner == pid or not isinstance(owner, int) or not isAlive(owner):
			try:
				path.unlink()
			except FileNotFoundError:
				pass


def autoOf(row):
	"""Module doc, auto: whether auto mode may submit this row's action."""
	if row.get("action") == "stop":
		return {"ok": False, "why": f"already running (run {row.get('run')})"}
	if not row.get("action"):
		return {"ok": False, "why": row.get("why") or "nothing to start"}
	if row["held"]:
		return {"ok": False, "why": HOLD_WHY}
	return {"ok": True}


def control(mainDir, topic):
	"""The row for one topic, or None when it has no worktree (removed, or never made)."""
	if not TOPIC.match(topic):
		return None
	worktree = worktreeOf(mainDir, topic)
	if not (worktree / ".git").is_file():
		return None
	state = loadJson(Path(mainDir) / ".hook-only" / "workers" / f"{topic}.json") or {}
	estimateFile = Path(mainDir) / "plans" / "worker" / f"{topic}.json"
	plan = loadJson(estimateFile)
	run = state.get("run") if isinstance(state.get("run"), int) else None
	row = {"topic": topic, "held": (worktree / ".hook-only" / "agent-guard" / "HOLD").exists()}
	if run is not None:
		row["run"] = run
	session = next((s for s in (state.get("session_id"), (plan or {}).get("session_id"))
		if isinstance(s, str) and UUID.match(s)), None)
	if isRunning(worktree, run):
		row["action"] = "stop"
	elif plan is None:
		row["why"] = f"no estimate yet (plans/worker/{topic}.json)"
	elif session and doneWithoutNewEstimate(worktree, run, estimateFile):
		row["why"] = "done (STATUS: done); nothing to resume until a new estimate (a merge run: --merge)"
	else:
		row["action"] = "resume" if session else "launch"
	row["auto"] = autoOf(row)
	inProgress = approving(worktree, run)
	if inProgress:
		row["approving"] = inProgress
	else:
		held = mergeHeld(worktree, run)
		if held:
			row["merge"] = held
	return row


def topics(mainDir):
	"""Every topic with a worktree, an estimate or a wt state, by name."""
	names = set()
	for folder, suffix in ((worktreeOf(mainDir, "x").parent, ""), (Path(mainDir) / "plans" / "worker", ".json"),
			(Path(mainDir) / ".hook-only" / "workers", ".json")):
		try:
			entries = os.listdir(folder)
		except OSError:
			continue
		names |= {n[:len(n) - len(suffix)] for n in entries if n.endswith(suffix)}
	return sorted(n for n in names if TOPIC.match(n))


def controls(mainDir):
	return [row for row in (control(mainDir, t) for t in topics(mainDir)) if row]


def behindOrigin(mainDir):
	"""Commits origin's default branch has that the local one lacks, or None when git can't tell."""
	for branch in ("main", "master"):
		result = subprocess.run(["git", "-C", str(mainDir), "rev-list", "--count", f"{branch}..origin/{branch}"],
			capture_output=True, text=True, timeout=10)
		if result.returncode == 0:
			return int(result.stdout.strip() or 0)
	return None


def localBlocker(rows, wantOn, behind):
	"""Module doc, local: why the flow mode can't switch now, or None."""
	running = [r["topic"] for r in rows if r.get("action") == "stop"]
	if running:
		return f"a worker is running ({', '.join(running)}); switch when none runs"
	unstarted = [r["topic"] for r in rows if r.get("action") == "launch"]
	if unstarted:
		return (f"topic(s) planned but not launched ({', '.join(unstarted)}) started from the other mode's base; "
			"launch or remove them first")
	if wantOn and behind is None:
		return "can't compare the local main with origin/main (git rev-list failed)"
	if wantOn and behind:
		return f"the local main is {behind} commit(s) behind origin/main; git pull first"
	return None


def plansVisible(mainDir):
	try:
		return bool(os.listdir(Path(mainDir) / "plans"))
	except OSError:
		return False


def main(argv):
	if len(argv) not in (2, 4) or (len(argv) == 4 and (argv[2] != "--local" or argv[3] not in ("on", "off"))):
		sys.exit("usage: wt_controls.py <main checkout> [--local on|off]")
	mainDir = Path(argv[1]).resolve()
	out = {"schema": SCHEMA, "controls": controls(mainDir), "plans_visible": plansVisible(mainDir)}
	try:
		import wt_queue  # the queue's own state (wt_queue.py status): topic, state, why, run, error
		queue = wt_queue.status(mainDir)
		out["queue"], out["planned"] = queue["items"], queue["planned"]
	except Exception as error:  # the controls still print; the pane shows what failed
		out["queue"], out["planned"], out["queue_error"] = [], [], f"wt_queue.py status failed: {error}"
	if len(argv) == 4:
		wantOn = argv[3] == "on"
		out["local_blocker"] = localBlocker(out["controls"], wantOn, behindOrigin(mainDir) if wantOn else 0)
	print(json.dumps(out))


if __name__ == "__main__":
	main(sys.argv)

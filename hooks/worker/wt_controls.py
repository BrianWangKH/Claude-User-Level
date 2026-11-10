#!/usr/bin/env python3
"""What a person (the wt-pane buttons), auto mode or `wt ...` may do for each topic: the one place that decides.

Usage: wt_controls.py <main checkout> [--local on|off]
Prints {"schema": 1, "controls": [row, ...], "plans_visible": bool, "queue": [item, ...], "planned": [...],
"flow_mode": "origin"|"local" (the main checkout's, homeconfig.flowMode) or null with "flow_mode_error": why
it can't be read (an invalid <main>/.hook-only/flow-mode record, a broken config/registry/env.json), so the pane shows
the error instead of a mode, "config_mode": "DEV"|"TEST" (homeconfig.configMode, its overrides file loaded) or null
with "config_mode_error": why (configMode), "queue_limits": wt_queue.limits ({"workers", "usd", "source": {limit: home |
local | repo}}, the pane shows each effective limit with its source) or "queue_error"} and, with --local,
"local_blocker": why the flow mode can't
switch that way now, or null. A row: {"topic", "action": "launch"|"resume"|"stop"|"wrapup" (absent: nothing
to send; wrapup: the run's worker ended STATUS done and its wrapper ended before the wrap-up, status
"resumable" code wrapup-left, never sent by auto), "why" (absent with an action), "run", "held",
"auto": {"ok": bool, "why"}, and, when the last run's
automatic merge was refused for a reason a person can approve (not "merge_run"), "merge": {"reason",
"files": [changed paths]}: the pane's `merge` button (`wt merge <topic>`); while that approval runs,
"approving": {"run", "since": "HH:MM"} instead of "merge" (no button: a second approval is refused); "phase"
(hooks/lib/runstate.py status: none, launching, running, finishing, ended, orphan, unknown; the pane counts a
worker running by it) and "human" (runstate.human: {"schema", "topic", "run", "phase", "line", "todo": null |
{"text", "command", "button"}}, in the status_language setting), or "status_error" when the run state can't be
read}. The wt-pane mod runs this and only draws the rows (its buttons still submit `wt <action> <topic>`);
worker-control.py reads the same runstate.status, so a typed `wt resume` and a button agree. Each run is also a
trigger of the reconciler (reconciler.triggerFrom: at most once per reconcile_wt_interval_minutes per main checkout,
detached; inside Claude Code's sandbox it can't write its state and starts nothing, and a reconcile started there
could check no process and would act on nothing); the output never depends on it. Schema 1 is the
contract: a field removed or renamed means schema 2. "plans_visible" is false when <main>/plans is missing or empty: Claude
Code's sandbox mounts an empty folder over it, and a planning checkout always holds at least
plans/repo-plan.md, so a caller can say so instead of showing every topic as
"no estimate yet".

Files read (the same worker-control.py writes or reads):
  <main>/../worktrees/<topic>/.git              the worktree
  <main>/plans/worker/<topic>.json              the planning session's estimate (needed to start)
  <main>/.hook-only/workers/<topic>.json        wt's state: session_id
  the run's state                               runstate.status(main, topic, root=<worktree>): the run record,
                                                else today's runs/ files through its legacy adapter; agent-guard
                                                ALERT / HOLD
Decisions (migration step 4 of the run record: every one from runstate.status, none from runs/ files here):
  action    running or finishing: "stop"; launching: none ("run <n> is starting"); otherwise, with an
            estimate, status "resumable": refused (ok false) gives its why (unknown state, STATUS done and no
            estimate newer than the last run a person or the queue started: by time, not by numbers, two merge
            estimates can be equal); else "resume" with a session, "launch" without. An orphan
            (the run may have died, or did) offers "resume": worker-control.py checks the wrapper's pid outside
            the sandbox, closes the run if it is gone (orphan-closed, never a merge) and resumes, and refuses
            while it runs or can't be checked. An orphan whose worker ended STATUS done with its wrap-up
            unfinished (resumable code wrapup-left) offers "wrapup" instead, estimate or not: `wt wrapup`
            checks the wrapper and finishes the kept steps. No pid check here: this process may be sandboxed
  held      an agent-guard ALERT or HOLD the person has not decided (status "held", Q6)
  auto      never stop, never across an ALERT or HOLD (resume lifts a HOLD, and that is the person's decision),
            never an orphan (closing a run is the person's call)
  merge     status "merge": the last run ended and its automatic merge was refused for the person, not as a
            merge run; the changed files come from git
  approving status "approving": `wt merge` recorded an approval of the run (approve) that no merge decision
            is newer than. A record left by a killed approval (run-steps.py) reads as approving here; worker-control.py,
            outside the sandbox, checks the pid and lets a typed `wt merge` start again
  local     no switch while a topic is planned but not launched (its branch is from the other mode's base);
            not on while the local main lags origin/main. A running worker never blocks it: a topic's mode is
            fixed at its first launch (worker-control.py record_flow_mode), so the switch only changes topics
            launched after it
  display   "phase" and "human" show what the run is doing, from the same status. A status that fails gives
            "status_error" and a row without an action (its why says the state can't be read)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
from wt_topics import TOPIC, UUID, loadJson, topics, worktreeOf  # noqa: E402

SCHEMA = 1
LOOSENED_KIND = "config-loosened"
INVALID_KIND = "config-invalid"
HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "run-worker.sh"
HOLD_WHY = "the worktree has an agent-guard ALERT or HOLD, and the user decides it first"


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


def approvalPath(worktree, run):
	return Path(worktree) / ".hook-only" / "runs" / f"approve-{run}.json"


def releaseApproval(worktree, run, pid, isAlive=alive):
	"""run-steps.py step merge --person (`wt merge`) when its steps end, and auto-merge.py when it ends: remove run's
	approval record if it is this process's (pid) or its process is gone; a record of another live process stays.
	Under runs/approve.lock, which worker-control.py holds while it starts run-steps.py and writes the record, so
	the record always carries the pid first. Removing it
	also clears the run record's approve (runstate.dualWrite, migration step 2)."""
	import fcntl

	import runstate
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
			else:
				runstate.dualWrite(str(worktree), "wt-controls", runstate.clearApprove, run)


def autoOf(row, phase=None):
	"""Module doc, auto: whether auto mode may submit this row's action."""
	if row.get("action") == "stop":
		return {"ok": False, "why": f"already running (run {row.get('run')})"}
	if not row.get("action"):
		return {"ok": False, "why": row.get("why") or "nothing to start"}
	if row["action"] == "wrapup":
		return {"ok": False, "why": f"run {row.get('run')}'s wrap-up is kept for the person: wt wrapup"}
	if row["held"]:
		return {"ok": False, "why": HOLD_WHY}
	if phase == "orphan":
		return {"ok": False, "why": f"run {row.get('run')} has no end (orphan): the person resumes it, and wt resume "
			"closes it first"}
	return {"ok": True}


def actionOf(status, plan, session, topic):
	"""Module doc, action: ({"action": ...} or {"why": ...}) from the run's status."""
	phase, run = status["phase"], status["run"]
	if phase in ("running", "finishing"):
		return {"action": "stop"}
	if phase == "launching":
		return {"why": f"run {run} is starting"}
	resumable = status["resumable"] or {}
	if resumable.get("code") == "wrapup-left":
		return {"action": "wrapup"}
	if plan is None:
		return {"why": f"no estimate yet (plans/worker/{topic}.json)"}
	if resumable.get("ok") is False:
		return {"why": resumable.get("why")}
	return {"action": "resume" if session else "launch"}


def changedFiles(worktree):
	"""The branch's changed paths against the integration branch (homeconfig.integrationBranch), at most 12; []
	when git can't tell."""
	try:
		branch = homeconfig.integrationBranch(str(worktree))
	except homeconfig.ConfigError:
		return []
	if branch is None:
		return []
	result = subprocess.run(["git", "-C", str(worktree), "diff", "--name-only", f"{branch}...HEAD"],
		capture_output=True, text=True, timeout=10)
	return [line for line in result.stdout.splitlines() if line][:12] if result.returncode == 0 else []


def heldFiles(worktree):
	"""An agent-guard ALERT or HOLD file exists (the row's held when the run state can't be read)."""
	import hookonly

	guard = Path(hookonly.stateDir(str(worktree), "agent-guard"))
	return (guard / "ALERT").exists() or (guard / "HOLD").exists()


def control(mainDir, topic):
	"""The row for one topic, or None when it has no worktree (removed, or never made)."""
	if not TOPIC.match(topic):
		return None
	worktree = worktreeOf(mainDir, topic)
	if not (worktree / ".git").is_file():
		return None
	state = loadJson(Path(mainDir) / ".hook-only" / "workers" / f"{topic}.json") or {}
	plan = loadJson(Path(mainDir) / "plans" / "worker" / f"{topic}.json")
	session = next((s for s in (state.get("session_id"), (plan or {}).get("session_id"))
		if isinstance(s, str) and UUID.match(s)), None)
	try:
		import runstate

		status = runstate.status(str(mainDir), topic, root=str(worktree))
		view = runstate.human(status)
	except Exception as error:  # no state, no decision: the row says why and offers nothing
		why = f"run status unreadable: {error}"
		row = {"topic": topic, "held": heldFiles(worktree), "why": why, "status_error": why}
		row["auto"] = autoOf(row)
		return row
	row = {"topic": topic, "held": status["held"]["state"] != "clear"}
	if status["run"] is not None:
		row["run"] = status["run"]
	row.update(actionOf(status, plan, session, topic))
	row["auto"] = autoOf(row, status["phase"])
	if status["approving"]:
		row["approving"] = {"run": status["approving"]["run"], "since": status["approving"]["since"]}
	elif status["merge"]:
		row["merge"] = {"reason": str(status["merge"].get("reason") or "")[:300], "files": changedFiles(worktree)}
	row.update({"phase": status["phase"], "human": view})
	return row


def controls(mainDir):
	return [row for row in (control(mainDir, t) for t in topics(mainDir)) if row]


def behindOrigin(mainDir):
	"""Commits origin's copy of the local integration branch (homeconfig.integrationBranch in local mode) has
	that the local one lacks, or None when git can't tell."""
	branch = homeconfig.integrationBranch(str(mainDir), "local")
	if branch is None:
		return None
	result = subprocess.run(["git", "-C", str(mainDir), "rev-list", "--count", f"{branch}..origin/{branch}"],
		capture_output=True, text=True, timeout=10)
	return int(result.stdout.strip() or 0) if result.returncode == 0 else None


def localBlocker(rows, wantOn, behind):
	"""Module doc, local: why the flow mode can't switch now, or None (a running worker is no reason)."""
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


def flowMode(mainDir):
	"""{"flow_mode": mode} or {"flow_mode": None, "flow_mode_error": why} (module doc)."""
	try:
		return {"flow_mode": homeconfig.flowMode(str(mainDir))}
	except (homeconfig.ConfigError, OSError) as error:
		return {"flow_mode": None, "flow_mode_error": f"flow mode unreadable: {error}"}


def configMode():
	"""{"config_mode": DEV or TEST} or {"config_mode": None, "config_mode_error": why}: the home's config mode
	(homeconfig.configMode) with its overrides file loaded, so a TEST without a valid local.test.json shows as
	the error it is (the pane draws one status line from it)."""
	try:
		mode = homeconfig.configMode()
		homeconfig.load("budget")
		return {"config_mode": mode}
	except (homeconfig.ConfigError, OSError) as error:
		return {"config_mode": None, "config_mode_error": f"config mode unreadable: {error}"}


def announceLoosened(mainDir):
	"""Once per distinct set of safety values config/local.json loosens (homeconfig.loosened): one
	setting-loosened event (wt_events, topic "config"), which the pane shows once as a toast and the next prompt
	gets as context. The set is recorded first (state kind config-loosened), so a failed write (a sandboxed
	caller) sends nothing and an unchanged set stays quiet; never refuses anything. Errors are swallowed: a
	broken config shows through the controls themselves."""
	import clock
	import statefile
	import wt_events

	try:
		found = sorted(f"{item['key']} {item['change']}" for item in homeconfig.loosened())
		record = statefile.read(str(mainDir), LOOSENED_KIND)
		if (record or {}).get("loosened", []) == found:
			return
		statefile.write(str(mainDir), LOOSENED_KIND, {"schema": 1, "loosened": found, "seen_at": clock.isoNow()})
	except (homeconfig.ConfigError, statefile.StateError, OSError):
		return
	if found:
		wt_events.emit(mainDir, "setting-loosened", "config", "config/local.json loosens a safety setting: "
			+ "; ".join(found) + " (allowed; shown once per change)")


def invalidText(problem):
	"""The once-warning for one local.json problem (homeconfig.problems)."""
	return f"local {problem['key']} invalid ({problem['why']}), using home value"


def announceInvalid(mainDir):
	"""Once per distinct local.json problem (homeconfig.problems: the reader already uses home.json's value):
	one setting-invalid event (wt_events, topic "config") with invalidText, which the pane shows once as a toast
	and the next prompt gets as context. Every problem seen is kept (state kind config-invalid) and recorded
	before its event, so it never repeats; when the record can't be written (a sandboxed caller) the problem
	goes to stderr instead and nothing is emitted. Never refuses anything; a broken home.json shows through
	the controls themselves."""
	import clock
	import statefile
	import wt_events

	try:
		found = [invalidText(p) for p in homeconfig.problems()]
	except (homeconfig.ConfigError, OSError):
		return
	if not found:
		return
	try:
		record = statefile.read(str(mainDir), INVALID_KIND) or {}
		seen = list(record.get("seen", []))
		new = [text for text in found if text not in seen]
		if not new:
			return
		statefile.write(str(mainDir), INVALID_KIND, {"schema": 1, "seen": seen + new, "seen_at": clock.isoNow()})
	except (statefile.StateError, OSError) as error:
		print(f"wt_controls: config/local.json: {'; '.join(found)} (not recorded: {error})", file=sys.stderr)
		return
	for text in new:
		wt_events.emit(mainDir, "setting-invalid", "config", text)


def main(argv):
	if len(argv) not in (2, 4) or (len(argv) == 4 and (argv[2] != "--local" or argv[3] not in ("on", "off"))):
		sys.exit("usage: wt_controls.py <main checkout> [--local on|off]")
	mainDir = Path(argv[1]).resolve()
	out = {"schema": SCHEMA, "controls": controls(mainDir), "plans_visible": plansVisible(mainDir), **flowMode(mainDir),
		**configMode()}
	try:
		import wt_queue  # the queue's own state (wt_queue.py status): topic, state, why, run, error
		queue = wt_queue.status(mainDir)
		out["queue"], out["planned"], out["queue_limits"] = queue["items"], queue["planned"], queue["limits"]
	except Exception as error:  # the controls still print; the pane shows what failed
		out["queue"], out["planned"], out["queue_error"] = [], [], f"wt_queue.py status failed: {error}"
	announceLoosened(mainDir)
	announceInvalid(mainDir)
	import reconciler  # the pane's poll is one of the reconciler's triggers (throttled; nothing in the output)

	reconciler.triggerFrom(mainDir)
	if len(argv) == 4:
		wantOn = argv[3] == "on"
		out["local_blocker"] = localBlocker(out["controls"], wantOn, behindOrigin(mainDir) if wantOn else 0)
	print(json.dumps(out))


if __name__ == "__main__":
	main(sys.argv)

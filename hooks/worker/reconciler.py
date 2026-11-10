"""The reconciler (worker self-repair stages 3a and 3b): what each worktree's latest run still owes, from the files
and the process checks this process can make, and the action that finishes it. run-steps.py reconcile (`wt
reconcile`, worker-control.py; the triggers below) calls it; --dry-run only judges and prints.

Classes (CLASSES), decided in this order; a class with an action (ACTING) is given only when every process check
says gone, so a run that may still run, or can't be checked, is never classed for one:
  unreadable      the run record exists but can't be read, or the run state is unknown: nothing is done; the
                  person checks the file
  nothing         no run; a run of today's files only (before the run record); a run that ended without a wrap-up
                  to finish (orphan-closed, launch-failed, not-logged-in, config-error, superseded); an orphan whose
                  worker didn't end `STATUS: done` (wt resume is the person's); an ended run whose steps are final;
                  a stopped run (runstate.stoppedRun: wt stop, a stop of claude's part or of its wrap-up) that every
                  process check says gone and that would otherwise be classed for an action: a person's stop
                  leaves it to the person (`wt wrapup` continues a wrap-up that was stopped)
  hands-off       a process of the run may still run (handsOff): its wrapper (runstate.verifyProcess True, or None:
                  no pid recorded, /proc unreadable, a caller in another pid namespace than the wrapper's), a
                  heartbeat younger than the orphan limit while the pid check says gone (a check that can't see the
                  wrapper isn't trusted), a heartbeat that can't be read, or a live run-steps.py, auto-finish.py or
                  worker-control.py auto-start naming this worktree and run (liveHelpers; unknown when /proc can't
                  be listed), or a running step's own program (programAlive: its program_pid and start ticks, so a
                  verify or merge whose run-steps.py was killed outright still holds the run); also a run
                  launching, running or finishing
  at-limit        (a run not stopped) a step failed (or left running by a runner that is gone) with all
                  step_attempts_max attempts used, the reconciler's own wrap-up starts used up
                  (runstate.claimReconcileStart counts them in reconcile.wrapup_starts: a start that dies before it
                  counts an attempt is still bounded), or a decided next run started step_attempts_max times
                  without starting: told to the person
                  once per run (event step-at-limit; runstate.recordNotice at_limit), and such a next step is closed
                  failed (runstate.closeNextUnstarted, so the decision no longer holds the topic busy); no further
                  attempt
  wrapup-owed     the worker ended `STATUS: done` and its wrap-up never finished (runstate.wrapupLeft: an orphan, or
                  an exit wrapup-failed), the run not stopped: starts run-steps.py wrapup
  step-retryable  the run ended completed or wrapup-failed, not stopped, with a step failed (or left running by a
                  runner that is gone) and attempts left: starts run-steps.py wrapup, which retries it
  next-unstarted  the run ended (not stopped), its next step decided a resume, merge or fix run, and no later run
                  exists: starts worker-control.py auto-start again (runstate.claimNextStart counts it in
                  steps.next.start_attempts)
Reported with the class, never acted on: B1, a step running at least its report threshold (runstate.longStep);
B2, a wrapper the pid check finds alive whose heartbeat is older than the orphan limit, which is also the person's
event heartbeat-stalled (one that needs attention: wt_events.ATTENTION), once per run (runstate.recordNotice
remembers it under the record's lock, so two callers at once tell it once).
Process checks are made only from the wrapper's own pid namespace (sameNamespace: the run record's process.pid_ns,
recorded when the wrapper started, equals this process's); from any other (a sandboxed caller, where other
processes are invisible), or when either can't be read, every check is unknown, so nothing is classed for an action.
Acting (reconcile without dryRun), per worktree: its claim lock first (reconcile-claim, a non-blocking flock; held
by another reconcile, or not writable: the worktree is skipped), then the judgment, then, for an acting class, the
same judgment again under the run record's lock just before the action (a run that changed meanwhile is left
alone), and the start: the program detached in its own session with WT_MAIN_DIR, its output appended to the run's
stderr log. Two reconciles, a `wt wrapup` (which checks and starts under the same record lock) or a wrap-up's own
step claims (runstate.startStep) never run a step twice. claim.json records the last reconcile of the worktree.
Triggers (startReconcile, detached, `run-steps.py reconcile --all [--exclude <worktree>]`): run-steps.py after a
wrap-up's queue tick (the repository's other worktrees); run-worker.sh's heartbeat every
reconcile_heartbeat_beats_count beats (the others, its output in the run's stderr log); every `wt` command
(worker-control.py) and the pane's poll (wt_controls.py), at most once per reconcile_wt_interval_minutes per main
checkout (triggerThrottled, state kind reconcile-trigger; one that can't be written starts nothing).
Reads: the worktrees (`git worktree list` of the main checkout), each latest run's record and heartbeat, the topic's
run state (runstate.status), /proc.
Writes: dry run: only B2's event (wt_events.py, main checkout) and its notice. Acting also: claim.json, the
notices and events of at-limit, steps.next (start_attempts, or closed failed at the limit), and the programs it
starts write their own. No step state of a run is changed otherwise, and no signal is ever sent.
Output: reconcile returns a header and one block per worktree: `<topic> run <n>: <class>: <why>`, `  would: <action>`
(dry run) or `  did: <what was done>`, and a `  note:` line per B1 or B2 finding.
Fails: closed — whatever can't be read or checked is unreadable or hands-off, never a class that acts.
"""

import contextlib
import errno
import fcntl
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import homeconfig  # noqa: E402
import hookio  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402

NOTHING = "nothing"
HANDS_OFF = "hands-off"
UNREADABLE = "unreadable"
AT_LIMIT = "at-limit"
WRAPUP_OWED = "wrapup-owed"
STEP_RETRYABLE = "step-retryable"
NEXT_UNSTARTED = "next-unstarted"
CLASSES = (UNREADABLE, NOTHING, HANDS_OFF, AT_LIMIT, WRAPUP_OWED, STEP_RETRYABLE, NEXT_UNSTARTED)
ACTING = (AT_LIMIT, WRAPUP_OWED, STEP_RETRYABLE, NEXT_UNSTARTED)
WRAPUP_ENDS = ("completed", "stopped", runstate.WRAPUP_FAILED)
# The programs that act on a run after its wrapper (their command line names the worktree, then the run or the topic
# and the run): a wrap-up or step, a finish, an automatic start.
HELPERS = ("run-steps.py", "auto-finish.py", "worker-control.py")
STALE_EVENT = "heartbeat-stalled"
AT_LIMIT_EVENT = "step-at-limit"
CLAIM = "reconcile-claim"  # <worktree>/.hook-only/reconcile/claim.json (config/registry/state-registry.json)
TRIGGER = "reconcile-trigger"  # <main>/.hook-only/reconcile/trigger.json
RUN_STEPS = os.path.join(homepath.HOOKS, "worker", "run-steps.py")
WORKER_CONTROL = os.path.join(homepath.HOOKS, "worker", "worker-control.py")


class Refused(Exception):
	"""The reconcile can't run as asked (no main checkout, a path that is no worktree of it)."""


# -- process checks (the edge) -------------------------------------------------------------------------------


def sameNamespace(record, procDir="/proc"):
	"""Whether this process shares the pid namespace the run's wrapper recorded (process.pid_ns): only then can it
	see the wrapper's pid. False when either can't be read (an older record without pid_ns included)."""
	recorded = ((record or {}).get("process") or {}).get("pid_ns")
	return isinstance(recorded, str) and bool(recorded) and recorded == runstate.pidNamespace("self", procDir)


def processCheck(record, procDir="/proc"):
	"""runstate.verifyProcess from the wrapper's pid namespace; None from any other (no check is possible)."""
	return runstate.verifyProcess if sameNamespace(record, procDir) else None


GONE_ERRNOS = (errno.ENOENT, errno.ESRCH)  # /proc's answers for a process that exited (also mid-read)


def isGone(error):
	"""Whether an OSError reading /proc/<pid> means the process is gone, not that /proc can't tell."""
	return isinstance(error, (FileNotFoundError, ProcessLookupError)) or error.errno in GONE_ERRNOS


def readArgv(pid, procDir="/proc"):
	"""The command line of pid as a list of bytes; [] when the process is gone (also one exiting while it is
	read); None when /proc can't tell."""
	try:
		with open(os.path.join(procDir, str(pid), "cmdline"), "rb") as f:
			return f.read().split(b"\0")
	except OSError as error:
		return [] if isGone(error) else None


def namesRun(argv, root, run, offsets=(1,)):
	"""Whether argv names the worktree root (by real path) followed, at one of offsets after it, by the run."""
	wanted = str(run).encode()
	for index, arg in enumerate(argv):
		if not arg or os.path.realpath(os.fsdecode(arg)) != root:
			continue
		if any(index + k < len(argv) and argv[index + k] == wanted for k in offsets):
			return True
	return False


def startTicks(pid, procDir="/proc"):
	"""The process's start time (stat field 22), None when it can't be read."""
	try:
		with open(os.path.join(procDir, str(pid), "stat"), encoding="utf-8") as f:
			return int(f.read().rsplit(")", 1)[1].split()[19])
	except (OSError, IndexError, ValueError):
		return None


def programAlive(step, procDir="/proc"):
	"""Whether a wrap-up step's own program (program_pid, started in its own session by run-steps.py) still runs:
	the pid exists with the recorded program_start_ticks (any start time when none was recorded). True when /proc
	can't tell; False without a recorded program or for a pid that is gone or reused."""
	pid = (step or {}).get("program_pid")
	if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
		return False
	try:
		with open(os.path.join(procDir, str(pid), "stat"), encoding="utf-8") as f:
			found = int(f.read().rsplit(")", 1)[1].split()[19])
	except OSError as error:
		return not isGone(error)
	except (IndexError, ValueError):
		return True
	ticks = step.get("program_start_ticks")
	return ticks is None or found == ticks


def runnerAlive(root, run, step, procDir="/proc"):
	"""Whether a wrap-up step is still being run: its runner (runnerProcessAlive) or its own program (programAlive,
	which outlives a runner killed outright, since it has its own session). A step never runs twice."""
	return runnerProcessAlive(root, run, step, procDir) or programAlive(step, procDir)


def heldSteps(root, run, steps, names, procDir="/proc"):
	"""The steps among names (in their order) that run now: recorded running with their runner or their own program
	alive (runnerAlive). A step decided after them must wait for them (run-steps.py waitForEarlierSteps, `wt merge`)."""
	return [
		name for name in names
		if (steps.get(name) or {}).get("state") == "running" and runnerAlive(root, run, steps[name], procDir)
	]


def runnerProcessAlive(root, run, step, procDir="/proc"):
	"""Whether a wrap-up step's recorded runner still runs: its runner_pid is a live run-steps.py whose command line
	names this worktree followed by this run, started at the recorded runner_start_ticks (when recorded). True when
	/proc can't tell (a step never runs twice); False for a pid that is gone or a reused one."""
	pid = (step or {}).get("runner_pid")
	if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
		return False
	argv = readArgv(pid, procDir)
	if argv is None:
		return True
	if not any(os.path.basename(os.fsdecode(a)) == "run-steps.py" for a in argv) or not namesRun(argv, root, run):
		return False
	ticks = step.get("runner_start_ticks")
	if ticks is None:
		return True
	found = startTicks(pid, procDir)
	return True if found is None else found == ticks


def verifiedRunner(root, run, step, procDir="/proc"):
	"""The pid of a wrap-up step's recorded runner when it is that very process now: a live run-steps.py whose command
	line names this worktree followed by this run, started at the recorded runner_start_ticks; None otherwise,
	also when anything can't be read or no start ticks were recorded (a pid that may be another process is never
	named: wt stop signals only what this returns)."""
	pid, ticks = (step or {}).get("runner_pid"), (step or {}).get("runner_start_ticks")
	if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(ticks, int):
		return None
	argv = readArgv(pid, procDir)
	if not argv or not any(os.path.basename(os.fsdecode(a)) == "run-steps.py" for a in argv):
		return None
	if not namesRun(argv, root, run) or liveTicks(pid, procDir) != ticks:
		return None
	return pid


# The program each wrap-up step runs whose command line names the worktree, the topic and the run (run-steps.py);
# queue_tick's (wt_queue.py tick <main>) names neither, so it is never confirmed.
STEP_PROGRAMS = {"verify": "verify.py", "verifier": "verifier.py", "merge": "auto-merge.py", "next": "worker-control.py"}


def verifiedProgram(root, run, name, step, procDir="/proc"):
	"""The pid (its own session and process group) of running wrap-up step name's program when it is that very
	process now: program_pid alive with the recorded program_start_ticks, its command line naming the step's program
	(STEP_PROGRAMS) and this worktree followed by the topic and this run; None otherwise, also when no start ticks
	were recorded or /proc can't tell."""
	pid, ticks = (step or {}).get("program_pid"), (step or {}).get("program_start_ticks")
	if (step or {}).get("state") != "running" or not isinstance(ticks, int) or name not in STEP_PROGRAMS:
		return None
	if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
		return None
	argv = readArgv(pid, procDir)
	if not argv or not any(os.path.basename(os.fsdecode(a)) == STEP_PROGRAMS[name] for a in argv):
		return None
	if not namesRun(argv, root, run, offsets=(2,)):
		return None
	return pid if liveTicks(pid, procDir) == ticks else None


def liveTicks(pid, procDir="/proc"):
	"""startTicks of a process that has not exited (a zombie, state Z, has: it is gone), None otherwise."""
	try:
		with open(os.path.join(procDir, str(pid), "stat"), encoding="utf-8") as f:
			fields = f.read().rsplit(")", 1)[1].split()
		return None if fields[0] == "Z" else int(fields[19])
	except (OSError, IndexError, ValueError):
		return None


def wrapupProcesses(root, run, record, procDir="/proc"):
	"""The processes of run's wrap-up still running, as wt stop may signal them: ("runner", pid) for every recorded
	step runner that verifiedRunner confirms (a step done keeps its runner, so the wrap-up between two steps is
	found too), else ("program", pid) for each running step's program verifiedProgram confirms (its runner gone).
	Nothing else is ever named."""
	steps = {name: step for name, step in ((record or {}).get("steps") or {}).items() if isinstance(step, dict)}
	runners = sorted({pid for pid in (verifiedRunner(root, run, step, procDir) for step in steps.values()) if pid})
	if runners:
		return [("runner", pid) for pid in runners]
	programs = sorted({pid for pid in (verifiedProgram(root, run, name, step, procDir) for name, step in steps.items())
		if pid})
	return [("program", pid) for pid in programs]


def liveHelpers(root, run, procDir="/proc"):
	"""The live processes acting on this run after its wrapper (HELPERS, their command line naming root and then the
	run, or the topic and the run), as "<pid> <program>"; None when /proc can't be listed or a process can't be
	read."""
	try:
		pids = [int(name) for name in os.listdir(procDir) if name.isdigit()]
	except OSError:
		return None
	found = []
	for pid in sorted(pids):
		if pid == os.getpid():
			continue
		argv = readArgv(pid, procDir)
		if argv is None:
			return None
		program = next((p for a in argv for p in HELPERS if os.path.basename(os.fsdecode(a)) == p), None)
		if program and namesRun(argv, root, run, offsets=(1, 2)):
			found.append(f"{pid} {program}")
	return found


# -- the facts of one worktree ---------------------------------------------------------------------------------


def heartbeatOf(root, run, now, limits):
	"""(state, age_seconds): state fresh, stale, missing or unreadable, against the orphan limit
	(heartbeat_interval_seconds x orphan_missed_beats_count, the heartbeat's own interval first)."""
	data, error = runstate.readObject(statefile.pathOf(root, runstate.HEARTBEAT, run))
	if data is None:
		return ("unreadable" if error else "missing"), None
	beat = runstate.parseStamp(data.get("beat_at"))
	if data.get("schema") != runstate.SCHEMA or beat is None:
		return "unreadable", None
	interval = data.get("interval_seconds") or limits["heartbeat_interval_seconds"]
	age = int((now - beat).total_seconds())
	return ("fresh" if age <= interval * limits["orphan_missed_beats_count"] else "stale"), age


def factsOf(mainDir, root, *, now=None, check=None, listHelpers=None, procDir="/proc"):
	"""What classify needs about root's latest run. check: the wrapper check (processCheck(record) when not given:
	None from another pid namespace than the wrapper's); listHelpers(root, run): liveHelpers when not given (tests
	replace both)."""
	now = now or clock.now()
	limits = runstate.settings()
	records, legacy = runstate.runNumbers(root)
	facts = {"root": root, "topic": os.path.basename(root), "run": None, "status": None, "record": None,
		"problem": None, "wrapper": None, "heartbeat": "missing", "heartbeat_age": None, "helpers": None,
		"limits": limits}
	if not records | legacy:
		return facts
	run = max(records | legacy)
	facts["run"] = run
	if run in records:
		record, problem = runstate.readRecord(root, run)
		facts.update(record=record, problem=problem)
		if record is not None:
			facts["topic"] = record["topic"]
	if check is None:
		check = processCheck(facts["record"], procDir)
	pidAlive = check if check is not False else None
	facts["status"] = runstate.status(mainDir, facts["topic"], root=root, run=run, pidAlive=pidAlive or None,
		clock=lambda: now)
	record = facts["record"]
	if record is not None and pidAlive and isinstance((record.get("process") or {}).get("wrapper_pid"), int):
		facts["wrapper"] = pidAlive(record["process"], facts["topic"], run)
	facts["heartbeat"], facts["heartbeat_age"] = heartbeatOf(root, run, now, limits)
	if pidAlive:
		facts["helpers"] = (listHelpers or (lambda r, n: liveHelpers(r, n, procDir)))(root, run)
	facts["runner_alive"] = (lambda step: True) if not pidAlive else (lambda step: runnerAlive(root, run, step, procDir))
	steps = (facts["record"] or {}).get("steps") or {}
	facts["programs"] = None if not pidAlive else [f"{name} (pid {step.get('program_pid')})"
		for name, step in sorted(steps.items()) if isinstance(step, dict) and step.get("state") == "running"
		and programAlive(step, procDir)]
	return facts


# -- the judgment (no I/O) -------------------------------------------------------------------------------------


def handsOff(facts):
	"""Why no action may touch the run now, None when every process check says gone (module doc, hands-off)."""
	wrapper, heartbeat, helpers = facts["wrapper"], facts["heartbeat"], facts["helpers"]
	if wrapper is None:
		return "its wrapper can't be checked (no pid recorded, /proc unreadable, or a sandboxed caller)"
	if wrapper:
		return "its wrapper still runs"
	if heartbeat == "fresh":
		return "the pid check finds the wrapper gone but its heartbeat is fresh: not trusted yet"
	if heartbeat == "unreadable":
		return "its heartbeat can't be read"
	if helpers is None:
		return "its processes can't be listed"
	if helpers:
		return "a process acting on this run still runs (" + ", ".join(helpers) + ")"
	if facts.get("programs"):
		return "a wrap-up step's program still runs (" + ", ".join(facts["programs"]) + ")"
	return None


def startsOf(step):
	"""How often the reconciler started the next run a next step decided (steps.next.start_attempts)."""
	return step.get("start_attempts") if runstate.isNumber(step.get("start_attempts")) else 0


def stepsOver(record, attemptsMax, runnerAlive):
	"""(retryable, limited): the steps failed, or running under a runner that is gone, with attempts left, and
	those whose attempts are used up (a next step closed after step_attempts_max starts of its run counts as used
	up); each [(name, attempt, reason)] in STEP_ORDER."""
	retryable, limited = [], []
	for name in runstate.STEP_ORDER:
		step = (record.get("steps") or {}).get(name) or {}
		state = step.get("state")
		if state not in ("failed", "running") or (state == "running" and runnerAlive(step)):
			continue
		attempt = step.get("attempt") if runstate.isNumber(step.get("attempt")) else 0
		reason = step.get("reason") or ("its runner is gone" if state == "running" else "failed")
		usedUp = attempt >= attemptsMax or (name == "next" and startsOf(step) >= attemptsMax)
		(limited if usedUp else retryable).append((name, attempt, str(reason)[:200]))
	return retryable, limited


def nextUnstarted(record, status):
	"""The next run the ended run's next step decided (resume, merge, fix) when no later run exists, else None."""
	step = (record.get("steps") or {}).get("next") or {}
	if step.get("state") != "done" or step.get("outcome") not in runstate.NEXT_RUNS:
		return None
	return None if (status.get("next") or {}).get("started") else step["outcome"]


def verdict(kind, why, action="nothing"):
	return {"class": kind, "why": why, "action": action}


def candidate(facts, attemptsMax, mainDir):
	"""The class the record asks for before the process checks (classify gates the acting ones)."""
	status, record = facts["status"], facts["record"]
	topic, run, root = facts["topic"], facts["run"], facts["root"]
	end = record.get("exit") or {}
	if end and end.get("reason") not in WRAPUP_ENDS:
		return verdict(NOTHING, f"ended {end.get('reason')}: no wrap-up step runs")
	left = runstate.wrapupLeft(record)
	if not end and not left:
		if status["phase"] == "orphan":
			return verdict(NOTHING, "an orphan whose worker didn't end STATUS done: wt resume is the person's")
		return verdict(HANDS_OFF, f"the run is {status['phase']}")
	retryable, limited = stepsOver(record, attemptsMax, facts["runner_alive"])
	command = runstate.wrapupCommand(mainDir, root, topic, run)
	starts = runstate.reconcileStarts(record)
	if (left or retryable) and not limited and starts >= attemptsMax:
		return verdict(AT_LIMIT, f"the reconciler started its wrap-up {starts} time(s) (step_attempts_max "
			f"{attemptsMax}) and steps are still owed: {', '.join(left or [n for n, _a, _r in retryable])}",
			"tell the person once (no further start); the person decides")
	if limited:
		named = "; ".join(f"{n} after {a} attempt(s): {r}" for n, a, r in limited)
		return verdict(AT_LIMIT, f"step_attempts_max {attemptsMax} used: {named}",
			"tell the person once (no further attempt); the person decides")
	if left:
		return verdict(WRAPUP_OWED, f"its worker ended STATUS done; wrap-up steps left: {', '.join(left)}",
			f"run the wrap-up: {command}")
	if retryable:
		named = "; ".join(f"{n} (attempt {a} of {attemptsMax}): {r}" for n, a, r in retryable)
		return verdict(STEP_RETRYABLE, f"failed step with attempts left: {named}", f"run the wrap-up: {command}")
	decided = nextUnstarted(record, status)
	if decided:
		starts = startsOf(record["steps"]["next"])
		if starts >= attemptsMax:
			return verdict(AT_LIMIT, f"its next step decided a {decided} run, which never started after {starts} "
				f"start(s) (step_attempts_max {attemptsMax})", "close the next step failed and tell the person once")
		return verdict(NEXT_UNSTARTED, f"its next step decided a {decided} run, which never started "
			f"({starts} start(s) so far)", "start it again (worker-control.py auto-start)")
	return verdict(NOTHING, f"ended {end.get('reason')}; every step is final")


def notes(facts):
	"""B1 and B2 (module doc), as note lines; report only."""
	found = []
	slow = runstate.longStep(facts["status"] or {}, facts["limits"])
	if slow:
		found.append(f"{slow['name']} has run {slow['minutes']} min (report limit {slow['limit_minutes']} min); "
			"nothing is stopped")
	if staleAlive(facts):
		found.append(f"its wrapper runs but its heartbeat is {facts['heartbeat_age']} s old; nothing is stopped")
	return found


def staleAlive(facts):
	"""B2: no exit, the pid check finds the wrapper alive, its heartbeat older than the orphan limit."""
	record = facts["record"] or {}
	return not record.get("exit") and facts["wrapper"] is True and facts["heartbeat"] == "stale"


def classify(facts, attemptsMax, mainDir):
	"""{"topic", "run", "class", "why", "action", "notes"} for one worktree (module doc, the order of the classes)."""
	status = facts["status"]
	base = {"topic": facts["topic"], "run": facts["run"]}
	if facts["run"] is None:
		return {**base, **verdict(NOTHING, "no run"), "notes": []}
	if facts["problem"] or (status or {}).get("phase") == "unknown":
		why = f"the run state can't be read ({facts['problem'] or status.get('error')})"
		return {**base, **verdict(UNREADABLE, why, "nothing; the person checks the run record"), "notes": []}
	if facts["record"] is None:
		return {**base, **verdict(NOTHING, "a run of today's files only (before the run record): not reconciled"),
			"notes": []}
	found = candidate(facts, attemptsMax, mainDir)
	if found["class"] in ACTING:
		why = handsOff(facts)
		if why:
			found = verdict(HANDS_OFF, f"{why} (else: {found['class']}, {found['why']})")
		elif runstate.stoppedRun(facts["record"]):
			found = verdict(NOTHING, stoppedWhy(facts, found))
	return {**base, **found, "notes": notes(facts)}


def stoppedWhy(facts, found):
	"""Why a stopped run is left to the person (module doc, nothing), with what it would otherwise have been."""
	left = runstate.wrapupLeft(facts["record"])
	owed = f"; `wt wrapup {facts['topic']}` continues its wrap-up ({', '.join(left)})" if left else ""
	return f"stopped: no automatic action on a stopped run (else: {found['class']}, {found['why']}){owed}"


def render(judged):
	"""One worktree's block: the class and why, then what it would do (dry run) or did (acting), then the notes."""
	acted = f"  did: {judged['did']}" if "did" in judged else f"  would: {judged['action']}"
	lines = [f"{judged['topic']} run {judged['run'] if judged['run'] is not None else 'none'}: {judged['class']}: "
		f"{judged['why']}", acted]
	return "\n".join(lines + [f"  note: {n}" for n in judged["notes"]])


# -- the command ----------------------------------------------------------------------------------------------


def mainOf(path):
	"""The main checkout of the repository holding path (the parent of git's common dir), None outside git."""
	common = hookio.git(str(path), "rev-parse", "--path-format=absolute", "--git-common-dir")
	return os.path.realpath(os.path.dirname(common)) if common else None


def worktreesOf(mainDir):
	"""Every linked worktree of the main checkout, by real path (`git worktree list`), the main checkout aside."""
	listing = hookio.git(mainDir, "worktree", "list", "--porcelain")
	if listing is None:
		raise Refused(f"{mainDir}: `git worktree list` failed (not a git checkout?)")
	found = []
	for line in listing.splitlines():
		if line.startswith("worktree "):
			path = os.path.realpath(line[len("worktree "):])
			if path != mainDir:
				found.append(path)
	return sorted(found)


def targets(worktree, everyWorktree, cwd, exclude=()):
	"""(main checkout, [worktree roots]): the main checkout from WT_MAIN_DIR, else the one holding the worktree
	(or cwd for --all); a worktree must be one of its linked worktrees by real path (a `..` or symlink that leads
	elsewhere is refused); exclude: worktrees left out (by real path)."""
	named = homeconfig.env("WT_MAIN_DIR")
	mainDir = os.path.realpath(named) if named else mainOf(worktree or cwd)
	if not mainDir or not os.path.isdir(mainDir):
		raise Refused("no main checkout (WT_MAIN_DIR, or a git checkout here)")
	known = worktreesOf(mainDir)
	left = {os.path.realpath(path) for path in exclude}
	if everyWorktree:
		return mainDir, [root for root in known if root not in left]
	root = os.path.realpath(worktree)
	if root not in known:
		raise Refused(f"{worktree} is not a worktree of {mainDir}")
	return mainDir, [root]


def tellStaleHeartbeat(mainDir, facts):
	"""B2's event, once per run: the notice recorded first (runstate.recordNotice), the event only when it is new.
	Returns a note about a notice that couldn't be recorded, else None."""
	if not staleAlive(facts):
		return None
	try:
		first = runstate.recordNotice(facts["root"], facts["run"], "heartbeat_stale")
	except (runstate.RecordError, OSError, ValueError) as error:
		return f"heartbeat-stalled not told: the notice can't be recorded ({error})"
	if first:
		import wt_events

		wt_events.emit(mainDir, STALE_EVENT, facts["topic"], f"run {facts['run']}: its wrapper runs but its heartbeat "
			f"is {facts['heartbeat_age']} s old (wt status {facts['topic']}); nothing was stopped")
	return None


def judge(mainDir, root):
	"""(facts, judged) of root's latest run now."""
	facts = factsOf(mainDir, root)
	return facts, classify(facts, runstate.settings()["step_attempts_max"], mainDir)


def runLog(root, run):
	"""The run's stderr log, where every program started for the run appends its output."""
	return os.path.join(runstate.runsDir(root), f"stderr-{run}.log")


def startDetached(argv, mainDir, log=None, cwd=None):
	"""Start argv in its own session (no signal sent to the caller's group reaches it, it waits for nobody), with
	WT_MAIN_DIR, its output appended to log (DEVNULL without one)."""
	out = open(log, "ab") if log else open(os.devnull, "wb")
	with out:
		subprocess.Popen(argv, start_new_session=True, env={**os.environ, "WT_MAIN_DIR": mainDir},
			stdin=subprocess.DEVNULL, stdout=out, stderr=out, cwd=cwd or mainDir)


def startWrapup(mainDir, facts, judged):
	"""wrapup-owed and step-retryable: the same judgment under the run record's lock, then run-steps.py wrapup."""
	root, run = facts["root"], facts["run"]
	with statefile.locked(root, runstate.RECORD, run):
		_fresh, again = judge(mainDir, root)
		if again["class"] != judged["class"] or again["run"] != run:
			return f"nothing: the run is now {again['class']} ({again['why']})"
		claimed = runstate.claimReconcileStart(root, run, runstate.settings()["step_attempts_max"])
		if "attempt" not in claimed:
			return f"nothing: {claimed.get('limit')} wrap-up start(s) made already"
		startDetached([sys.executable, RUN_STEPS, "wrapup", root, str(run), "--topic", facts["topic"]], mainDir,
			runLog(root, run))
	return f"started the wrap-up (run-steps.py wrapup {root} {run}; start {claimed['attempt']})"


def startNext(mainDir, facts, judged):
	"""next-unstarted: the same judgment again, one start counted under the record's lock (claimNextStart), then
	worker-control.py auto-start."""
	root, run = facts["root"], facts["run"]
	_fresh, again = judge(mainDir, root)
	if again["class"] != judged["class"] or again["run"] != run:
		return f"nothing: the run is now {again['class']} ({again['why']})"
	claimed = runstate.claimNextStart(root, run, runstate.settings()["step_attempts_max"])
	if "attempt" not in claimed:
		return "nothing: " + (claimed.get("gone") or f"{claimed.get('limit')} start(s) made already")
	startDetached([sys.executable, WORKER_CONTROL, "auto-start", root, facts["topic"], str(run)], mainDir,
		runLog(root, run))
	return f"started worker-control.py auto-start (start {claimed['attempt']})"


def tellAtLimit(mainDir, facts, judged):
	"""at-limit: a next step that never started its run is closed failed; the person is told once per run."""
	root, run, topic = facts["root"], facts["run"], facts["topic"]
	closed = runstate.closeNextUnstarted(root, run, judged["why"]) if "next step decided" in judged["why"] else False
	if not tellLimitOnce(mainDir, root, run, topic, judged["why"]):
		return "nothing: told already" + ("; the next step closed failed" if closed else "")
	return "told the person (step-at-limit)" + ("; the next step closed failed" if closed else "")


def tellLimitOnce(mainDir, root, run, topic, why):
	"""The person's step-at-limit event for run, once per run (runstate.recordNotice at_limit, under the record's
	lock: the reconciler, a continued wrap-up and `wt wrapup` share it). True when told now."""
	if not runstate.recordNotice(root, run, "at_limit"):
		return False
	import wt_events

	wt_events.emit(mainDir, AT_LIMIT_EVENT, topic, f"run {run}: {why}; nothing retries it (wt status {topic}); the "
		"person decides")
	return True


ACTIONS = {WRAPUP_OWED: startWrapup, STEP_RETRYABLE: startWrapup, NEXT_UNSTARTED: startNext, AT_LIMIT: tellAtLimit}


@contextlib.contextmanager
def claimed(root):
	"""The worktree's reconcile claim (a non-blocking flock): yields True when held, False when another reconcile
	holds it or the lock can't be opened (a sandboxed caller: .hook-only is read-only there)."""
	try:
		path = statefile.lockPath(root, CLAIM)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
	except OSError:
		yield False
		return
	try:
		try:
			fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError:
			yield False
			return
		yield True
	finally:
		os.close(handle)


def actOn(mainDir, root):
	"""One worktree, acting (module doc): its claim, the judgment, the action; returns the judged block."""
	with claimed(root) as held:
		if not held:
			return {"topic": os.path.basename(root), "run": None, "class": HANDS_OFF,
				"why": "another reconcile holds this worktree (or its claim can't be taken)", "action": "nothing",
				"notes": [], "did": "nothing"}
		facts, judged = judge(mainDir, root)
		problem = tellStaleHeartbeat(mainDir, facts)
		if problem:
			judged["notes"].append(problem)
		action = ACTIONS.get(judged["class"])
		judged["did"] = action(mainDir, facts, judged) if action else "nothing"
		if facts["run"] is not None and judged["class"] in ACTING:
			statefile.write(root, CLAIM, {"schema": statefile.kind(CLAIM)["schema"], "reconciled_at": clock.isoNow(),
				"run": facts["run"], "class": judged["class"], "did": judged["did"][:300]})
	return judged


def reconcile(worktree, everyWorktree, cwd, exclude=(), dryRun=False):
	"""Judge every target (targets) and, unless dryRun, act on each (actOn); return the text (module doc)."""
	mainDir, roots = targets(worktree, everyWorktree, cwd, exclude)
	blocks = []
	for root in roots:
		if dryRun:
			facts, judged = judge(mainDir, root)
			problem = tellStaleHeartbeat(mainDir, facts)
			if problem:
				judged["notes"].append(problem)
		else:
			judged = actOn(mainDir, root)
		blocks.append(render(judged))
	head = (f"reconcile --dry-run ({mainDir}): no step was run or changed, nothing started or signalled; "
		"notices may be recorded (heartbeat-stalled)") if dryRun else (f"reconcile ({mainDir}): each acting class "
		"checked again under the run record's lock before its action; no signal sent")
	return "\n".join([head] + (blocks or ["no worktree"]))


def dryRun(worktree, everyWorktree, cwd):
	"""The judgment only (reconcile with dryRun)."""
	return reconcile(worktree, everyWorktree, cwd, dryRun=True)


# -- triggers --------------------------------------------------------------------------------------------------


def reconcileArgv(exclude=None):
	return [sys.executable, RUN_STEPS, "reconcile", "--all"] + (["--exclude", exclude] if exclude else [])


def startReconcile(mainDir, exclude=None, log=None):
	"""Start `run-steps.py reconcile --all [--exclude <worktree>]` detached for the main checkout (module doc)."""
	startDetached(reconcileArgv(exclude), mainDir, log)


def triggerFrom(cwd):
	"""triggerThrottled for a `wt` command or the pane's poll in cwd, only when cwd is a main checkout; whatever
	fails (not a git checkout, the state not writable in the sandbox, a setting unreadable) starts nothing and is
	reported as False, never raised: the command or the poll goes on."""
	try:
		mainDir = os.path.realpath(str(cwd))
		if mainOf(mainDir) != mainDir:
			return False
		return triggerThrottled(mainDir)
	except (OSError, ValueError, KeyError, statefile.StateError, homeconfig.ConfigError):
		return False


def triggerThrottled(mainDir, now=None):
	"""A `wt` command's or the pane poll's trigger: startReconcile at most once per reconcile_wt_interval_minutes
	per main checkout (reconcile-trigger, under its lock). True when it started one. Raises OSError when the state
	can't be written (a sandboxed caller): then nothing starts."""
	now = now or clock.now()
	interval = runstate.settings()["reconcile_wt_interval_minutes"] * 60
	with statefile.locked(mainDir, TRIGGER):
		last = statefile.read(mainDir, TRIGGER) or {}
		then = runstate.parseStamp(last.get("started_at"))
		if then is not None and 0 <= (now - then).total_seconds() < interval:
			return False
		statefile.write(mainDir, TRIGGER, {"schema": statefile.kind(TRIGGER)["schema"],
			"started_at": clock.isoOf(now)})
		startReconcile(mainDir)
	return True

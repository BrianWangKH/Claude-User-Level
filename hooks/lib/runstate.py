"""Worker run state (hooks/RULES.md S1-S9, X1-X3): the run record, its heartbeat, and the one function that
says what a topic's run is doing (design S1+S2). Today's writers also write the record (migration step 2, dual
write); since migration step 4 every decision about a run reads status() (or activeSince, current,
leftOfEstimate below), never today's runs/ files directly; since step 5 every gate (what lets something
irreversible or outward happen: auto-merge.py's merge, auto-finish.py's removal, verifier.py's start,
worker-control.py's automatic runs) reads gateView() or status() as well; since step 6 the wrap-up (verify,
verifier, merge, next run, finish, queue tick) is run-steps.py's: each step marks itself running here (startStep:
attempt, runner_pid, started_at), its program writes the outcome (recordStep keeps that bookkeeping), and a step
whose program left no outcome is closed by run-steps.py (failed, or skipped when the program found nothing to do).

Files in the worktree's runs component (config/registry/state-registry.json, written only here through statefile):
- run-record `<run>.json` (schema 1): the facts of one run. More than one program will call this module, so
  every write is a read-modify-write under the record's lock (statefile.locked). The phase is never
  stored: phaseOf() derives it from the facts each time.
- run-heartbeat `<run>-heartbeat.json` (schema 1): {beat_at, interval_seconds, count}, one writer (the
  wrapper's heartbeat loop through beat()), a file of its own so a beat needs no lock (Q1).
- Today's files (est-, start-, exit-, stopped-, main-, auto-resume-, auto-, approve-<run>, result-<run>.json,
  verify-, verifier-, merge-<run>.json): legacyRecord() reads them into the same record shape, read-only, so
  status() works before any writer changes and for archived topics, which keep the old files forever.

status(mainDir, topic, *, root, clock, bootId, pidAlive) is read-only (it writes nothing, anywhere) and depends
only on the files and its injections (root: the topic's checkout when the caller found it already, else
`git worktree list` and the archive find it): clock (a function returning an aware datetime), bootId (this boot's
id; None reads /proc/sys/kernel/random/boot_id, and an empty or unreadable one skips the boot rule) and pidAlive (a function (process, topic, run)
-> True/False/None; None means no pid check, the only correct choice inside the sandbox, where other
processes are invisible). verifyProcess is the real check for callers outside the sandbox.
Phases: none, launching, running, finishing, ended, orphan (confidence suspected or confirmed), unknown.
- unknown: a record exists but can't be read, has another schema, misses a required field or holds a
  value outside its enum. Never guessed as running or ended.
- orphan: no exit, and the wrapper exited, or the boot changed, or the last sign of life (heartbeat, else
  wrapper start, else launch) is older than heartbeat_interval_seconds x orphan_missed_beats_count.
  confirmed only by a recorded orphan.confirmed_at or a pid check that says the process is gone; a pid
  check that finds it alive turns a stale heartbeat back into its phase with a warning. A run whose
  heartbeat goes stale and then fresh again (the machine slept) reads as running again.
merge_allowed is True only when every fact a merge gate needs is present, readable and known; anything
missing, unknown or contradicting refuses (merge_blocks: one {fact, reason} per refusal; merge_refusals: the
reasons). Since migration step 5 these facts are never looser than auto-merge.py's checks of today's files:
the worker's STATUS must be done and lead its header written exactly `STATUS: <value>` (claude.lead_status),
and a real-data step passes only by a PASS or by the topic's verify spec as it reads now (no spec: verify and
verifier skip; a spec without a "verifier" key: the verifier skips; a spec that can't be read or seen: nothing
skips). A stop that came only after claude's part completed (stopAfterClaude) is the block wrapup-stop instead
of stop and exit: the automatic merge refuses it like any block, the person's approval (auto-merge --person, `wt
merge`) is not refused by it; every other block still holds for both.
gateView(mainDir, topic, run, root) is what a gate reads (migration step 5): while today's files are still
written they are still the truth, so it gives both stories of the run (status from the record, status from
today's files) and every merge block of either, plus a block for a run without a usable record (T-1) and for a
main head the two hold differently; a gate goes on only as far as both stories allow. status() also carries the
facts a gate acts on: main_head_at_start, steps (the record's wrap-up steps as they are) and dispatch_stop.
Decision facts (migration step 4): busy (launching, running, finishing, unknown and a suspected orphan; an
ended run whose next step decided a resume, merge or fix run that has no run yet, busy_usd that decision's
USD), resumable ({ok, why, prompt_kind, code}: what `wt resume` does, Q3: a confirmed orphan is resumable
and wt resume closes it first, closeVerifiedOrphan; a suspected one only after a pid check outside the
sandbox), held (agent-guard ALERT or HOLD, Q6), merge and approving (a refused automatic merge the person
may approve, an approval in progress), stop, approve (the approving process: pid, start_ticks), process (the
wrapper facts verifyProcess checks) and outcome (with the merge step's flags).
activeSince(root, since) says whether any run of a checkout was active since a moment (an orphan only until
its last sign of life), current(root) which run's wrapper started last and when, leftOfEstimate what is left
of the estimate of the last run a person or the queue started.
human(status, lang) renders the line and to-do for people (config text per language, budget
run_state); agent(status, detail) renders KEY lines plus the evidence for an agent to quote, and with
detail(status) what the run's evidence files say (the worker's result, the verify, verifier and merge
decisions, the next run, every run's result line), so a display reader never opens runs/ files itself
(migration step 3: the display readers run-result.py, worker-status.py, `wt status` and the pane's line).
Dual write (migration step 2): every program that writes one of today's runs/ files also writes the matching
record fields through the record* functions below, called through dualWrite(): a record write that fails
never stops what the caller does (today's file is written first); the person learns of it once per distinct
error (a hook-failed event, hookio.recordFailureAt) and the record is never half written (update() validates,
statefile replaces the file atomically). Since step 4 status() reads a run's record wherever its file exists,
so a write that failed leaves the record behind today's files until a later write succeeds (a missing exit
reads as a run going on, then as a suspected orphan; an unusable record as unknown), and compareStories lists
the difference. A run without a record file (started before step 2, or by hand) is
skipped quietly: the legacy adapter reads it. compareStories() derives the status from the record and from
today's files for one run and lists the differences; reportDivergence() writes each difference once per run
as a status-diverged event (state kind run-divergence remembers what was reported).
A verifier step whose verifier could not judge (verdict error) is failed with outcome ERROR (legacyVerifierStep),
so a wrap-up run again retries it like any failed step; PASS and FAIL are done. Each step started records its time
limit (limit_seconds, step_history.py: from the history of earlier runs, else the step's default); longStep(status)
names a step running at least step_running_report_factor of that limit (report only). recordNotice(root, run, name)
remembers a notice told once per run (notices.<name>_at, under the record's lock; the reconciler's
heartbeat-stalled and at-limit). The wrapper's process facts include its pid namespace (pid_ns), so a process check
from another namespace (a sandboxed caller, where the wrapper is invisible) is known to be impossible
(the reconciler's sameNamespace). claimNextStart and closeNextUnstarted count and end the reconciler's starts of a
next run that was decided but never started (steps.next.start_attempts).
Fails: closed — a state it can't read is phase unknown and merge_allowed False, never a guess.
"""

import hashlib
import json
import os
import re
import shlex
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock as homeClock  # noqa: E402
import eventlog  # noqa: E402
import homeconfig  # noqa: E402
import hookio  # noqa: E402
import hookonly  # noqa: E402
import report_header  # noqa: E402
import statefile  # noqa: E402

SCHEMA = 1
RECORD = "run-record"
HEARTBEAT = "run-heartbeat"
SOURCE = "runstate"
SETTINGS = "budget"
SECTION = "run_state"
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
WRAPPER_NAME = "run-worker.sh"

PHASES = ("none", "launching", "running", "finishing", "ended", "orphan", "unknown")
ACTIVE = ("launching", "running", "finishing")
STARTED_BY = ("person", "queue", "auto-resume", "auto-merge", "auto-fix", "manual")
PURPOSES = ("build", "merge", "fix")
SESSION_MODES = ("new", "resume")
INTEGRATION_MODES = ("local", "origin")
FLOWS = ("written", "scripted")
PROMPT_KINDS = ("feature-flow", "resume", "scripted", "scripted-resume", "scripted-auto", "merge", "fix", "auto")
EXIT_REASONS = (
	"completed",
	"stopped",
	"not-logged-in",
	"config-error",
	"launch-failed",
	"orphan-closed",
	"superseded",
	"wrapup-failed",
)
# The exit run-steps.py writes when its wrap-up crashed outside its steps (recordWrapupFailed): the run ended, its
# steps left stay owed (wrapupLeft) and the wrap-up run again replaces it with its real end (endRecord).
WRAPUP_FAILED = "wrapup-failed"
CLAUDE_OUTCOMES = ("status", "phase-limit", "budget-used", "error", "stopped", "no-status")
INVOCATION_KINDS = ("main", "continue", "compact", "final-compact")
STEP_ORDER = ("verify", "verifier", "merge", "next", "finish", "queue_tick")
STEP_STATES = ("pending", "running", "done", "skipped", "failed", "abandoned")
STEP_OUTCOMES = {
	"verify": ("PASS", "FAIL", "ERROR"),
	"verifier": ("PASS", "FAIL", "ERROR"),
	"merge": ("merged", "already-merged", "refused", "error"),
	"next": ("resume", "merge", "fix", "none", "refused"),
	"finish": ("launched",),
	"queue_tick": ("ticked", "error"),
}
SKIP_REASONS = (
	"not-done",
	"no-spec",
	"no-verifier-section",
	"origin-mode",
	"stopped",
	"verify-not-pass",
	"not-merged",
	"no-follow-up",
	"no-main-checkout",
	"verifier-retry",
)
# What a wrap-up step keeps across its attempts (startStep writes it; recordStep keeps it under the outcome).
STEP_BOOKKEEPING = ("attempt", "runner_pid", "runner_start_ticks", "started_at", "limit_seconds", "start_attempts",
	"program_pid", "program_start_ticks")
# Step states startStep never runs again (unless forced: the person's merge approval).
STEP_FINAL = ("done", "skipped", "abandoned")
UNFINISHED_STEP = ("pending", "running")
# Who starts a run on purpose (its estimate is the plan's); the rest are automatic follow-ups of a run.
HUMAN_STARTS = ("person", "queue", "manual")
NEXT_RUNS = ("resume", "merge", "fix")
# The worker's STATUS values, in any case (hooks/RULES.md R2 has workers write the upper-case enum; earlier
# workers wrote lower case): every comparison of a worker's STATUS goes through workerStatus. The record keeps
# the value as the worker wrote it. Only a done value lets a merge gate go on.
WORKER_VALUES = ("done", "needs-decision", "blocked")
MERGED = ("merged", "already-merged")
BUDGET_EXIT_CODE = 99
NOT_LOGGED_IN_CODE = 90
CONFIG_ERROR_CODE = 1
FLOW_SCHEMA = 1
FLOW_PHASES = ("running", "wrapup", "stopped", "off")
WAIT_ENDS = ("ended", "orphan", "unknown", "none")

RECORD_FILE = re.compile(r"^(\d+)\.json$")
LEGACY_FILE = re.compile(
	r"^(est|start|exit|stopped|main|auto-resume|auto|approve|result|phase|compact|verify|verifier|merge|auth|stderr)"
	r"-(\d+)(?:-\d+)?(?:\.json|\.log)?$"
)


class RecordError(Exception):
	pass


# -- settings, places ---------------------------------------------------------------------------------


def settings():
	"""The run_state section of budget (local.json may override it)."""
	return homeconfig.get(SETTINGS, SECTION)


def runsDir(root):
	return hookonly.stateDir(root, statefile.kind(RECORD)["component"])


def legacyPath(root, name, run, suffix=""):
	return os.path.join(runsDir(root), f"{name}-{run}{suffix}")


def readBootId():
	try:
		with open(BOOT_ID_FILE, encoding="utf-8") as f:
			return f.read().strip() or None
	except OSError:
		return None


def worktreeOf(mainDir, topic):
	"""The topic's worktree from `git worktree list` (X3): the one whose directory or branch's last segment is
	the topic, the main checkout aside; None when there is none."""
	listing = hookio.git(str(mainDir), "worktree", "list", "--porcelain") or ""
	main = os.path.realpath(str(mainDir))
	for block in listing.split("\n\n"):
		fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
		path = fields.get("worktree")
		branch = fields.get("branch") or ""
		if not path or os.path.realpath(path) == main:
			continue
		if os.path.basename(path) == topic or branch.rsplit("/", 1)[-1] == topic:
			return os.path.realpath(path)
	return None


def archiveOf(mainDir, topic):
	return os.path.join(hookonly.stateDir(str(mainDir), "archive"), topic)


def locate(mainDir, topic):
	"""(checkout root holding the topic's runs, archived?): the worktree, else its archive copy, else (None, False)."""
	worktree = worktreeOf(mainDir, topic)
	if worktree:
		return worktree, False
	archived = archiveOf(mainDir, topic)
	if os.path.isdir(runsDir(archived)):
		return archived, True
	return None, False


# -- time and files ----------------------------------------------------------------------------------


def parseStamp(value):
	"""An aware datetime from a stored stamp (or an old %z one), None for anything else."""
	if not isinstance(value, str):
		return None
	try:
		return homeClock.parse(value.strip())
	except ValueError:
		return None


def mtimeOf(path):
	try:
		return datetime.fromtimestamp(os.stat(path).st_mtime).astimezone()
	except OSError:
		return None


def readText(path):
	try:
		with open(path, encoding="utf-8") as f:
			return f.read()
	except (OSError, UnicodeDecodeError):
		return None


def readObject(path):
	"""(object, error): a JSON object, or None with why (missing: error None)."""
	if not os.path.exists(path):
		return None, None
	text = readText(path)
	if text is None:
		return None, "unreadable"
	try:
		value = json.loads(text)
	except ValueError as error:
		return None, f"not JSON ({error})"
	if not isinstance(value, dict):
		return None, "not a JSON object"
	return value, None


def hhmm(value):
	"""HH:MM of a stored stamp, for people: the hour and minute of its stored form (in the writer's offset)."""
	moment = parseStamp(value)
	return homeClock.isoOf(moment)[11:16] if moment else "?"


# -- record validation -------------------------------------------------------------------------------


def stampProblems(node, where):
	"""Every *_at value anywhere in node that isn't a stamp with an offset (X1)."""
	problems = []
	if isinstance(node, dict):
		for key, value in node.items():
			here = f"{where}.{key}" if where else key
			if key.endswith("_at") and value is not None and not homeClock.isStamp(value):
				problems.append(f"{here} is not a timestamp with an offset")
			problems += stampProblems(value, here)
	elif isinstance(node, list):
		for index, value in enumerate(node):
			problems += stampProblems(value, f"{where}[{index}]")
	return problems


def workerStatus(value):
	"""A worker's STATUS value as its word of WORKER_VALUES, whatever its case ("DONE", "Done", "done" -> "done");
	None for anything else (no value, more text, another word)."""
	word = value.lower() if isinstance(value, str) else None
	return word if word in WORKER_VALUES else None


def isWorkerDone(value):
	"""Whether a worker's STATUS value is done, in any case (workerStatus)."""
	return workerStatus(value) == "done"


def enumProblem(value, allowed, where, optional=True):
	if value is None and optional:
		return []
	return [] if value in allowed else [f"{where}={value!r} is not one of {', '.join(map(str, allowed))}"]


def stepProblems(steps):
	if steps is None:
		return []
	if not isinstance(steps, dict):
		return ["steps is not an object"]
	problems = []
	for name, step in steps.items():
		if name not in STEP_ORDER:
			problems.append(f"steps.{name} is not a known step")
			continue
		if not isinstance(step, dict):
			problems.append(f"steps.{name} is not an object")
			continue
		problems += enumProblem(step.get("state"), STEP_STATES, f"steps.{name}.state", optional=False)
		problems += enumProblem(step.get("outcome"), STEP_OUTCOMES[name], f"steps.{name}.outcome")
		problems += enumProblem(step.get("skip_reason"), SKIP_REASONS, f"steps.{name}.skip_reason")
	return problems


def validate(record):
	"""Problems that make a record unusable ([] = fine): schema, required fields, enums, timestamps."""
	if not isinstance(record, dict):
		return ["the record is not a JSON object"]
	if record.get("schema") != SCHEMA:
		return [f"schema {record.get('schema')!r} is not {SCHEMA}"]
	problems = []
	if not isinstance(record.get("topic"), str) or not record.get("topic"):
		problems.append("topic is missing")
	if not isinstance(record.get("run"), int) or isinstance(record.get("run"), bool) or record.get("run") < 1:
		problems.append("run is missing or not a positive int")
	problems += enumProblem(record.get("started_by"), STARTED_BY, "started_by", optional=False)
	problems += enumProblem(record.get("purpose"), PURPOSES, "purpose", optional=False)
	problems += enumProblem(record.get("flow"), FLOWS, "flow", optional=False)
	problems += enumProblem(record.get("session_mode"), SESSION_MODES, "session_mode")
	problems += enumProblem(record.get("integration_mode"), INTEGRATION_MODES, "integration_mode")
	problems += enumProblem(record.get("prompt_kind"), PROMPT_KINDS, "prompt_kind")
	launch = record.get("launch")
	if not isinstance(launch, dict) or not homeClock.isStamp(launch.get("launched_at")):
		problems.append("launch.launched_at is missing")
	for name in ("process", "claude", "stop", "dispatch_stop", "approve", "orphan", "exit", "estimate", "notices",
			"reconcile"):
		if record.get(name) is not None and not isinstance(record.get(name), dict):
			problems.append(f"{name} is not an object")
	claude = record.get("claude") if isinstance(record.get("claude"), dict) else {}
	problems += enumProblem(claude.get("outcome"), CLAUDE_OUTCOMES, "claude.outcome")
	if claude.get("status") is not None and workerStatus(claude.get("status")) is None:
		problems.append(f"claude.status={claude.get('status')!r} is not one of {', '.join(WORKER_VALUES)} (any case)")
	for index, call in enumerate(claude.get("invocations") or []):
		problems += enumProblem((call or {}).get("kind"), INVOCATION_KINDS, f"claude.invocations[{index}].kind", False)
	end = record.get("exit") if isinstance(record.get("exit"), dict) else None
	if end is not None:
		problems += enumProblem(end.get("reason"), EXIT_REASONS, "exit.reason", optional=False)
	orphan = record.get("orphan") if isinstance(record.get("orphan"), dict) else None
	if orphan is not None and not homeClock.isStamp(orphan.get("confirmed_at")):
		problems.append("orphan.confirmed_at is missing")
	problems += stepProblems(record.get("steps"))
	return problems + stampProblems(record, "")


# -- writing (step 2 callers) ------------------------------------------------------------------------


def runNumbers(root):
	"""Run numbers present in the runs directory: records and today's files (est- included, P4)."""
	return runNumbersIn(runsDir(root))


def runNumbersIn(runs):
	"""(record run numbers, today's-file run numbers) of one runs directory."""
	try:
		names = os.listdir(runs)
	except OSError:
		return set(), set()
	records, legacy = set(), set()
	for name in names:
		match = RECORD_FILE.match(name)
		if match:
			records.add(int(match.group(1)))
			continue
		match = LEGACY_FILE.match(name)
		if match:
			legacy.add(int(match.group(2)))
	return records, legacy


def nextRun(root):
	records, legacy = runNumbers(root)
	return max(records | legacy, default=0) + 1


def writeRecord(root, record):
	problems = validate(record)
	if problems:
		raise RecordError("runstate: refusing to write an invalid record: " + "; ".join(problems))
	return statefile.write(root, RECORD, record, record["run"])


def create(root, fields):
	"""Allocate the next run number (records and today's files counted) and write its record; returns the run.
	The number is taken under that number's lock and only when no file of that run exists, so two callers
	never get the same run."""
	while True:
		run = nextRun(root)
		with statefile.locked(root, RECORD, run):
			records, legacy = runNumbers(root)
			if run in records or run in legacy:
				continue
			record = {"schema": SCHEMA, **fields, "run": run}
			writeRecord(root, record)
			return run


def readRecord(root, run):
	"""(record, problem): the run's record, None with no problem when there is none, None with why when it
	can't be used."""
	record, error = readObject(statefile.pathOf(root, RECORD, run))
	if error:
		return None, error
	if record is None:
		return None, None
	problems = validate(record)
	return (record, None) if not problems else (None, "; ".join(problems))


def update(root, run, change):
	"""change(record) edits the record in place under its lock; the result is validated before it is written."""
	with statefile.locked(root, RECORD, run):
		return updateHeld(root, run, change)


def updateHeld(root, run, change):
	"""update() for a caller that holds the record's lock already (statefile.locked)."""
	record, problem = readRecord(root, run)
	if record is None:
		raise RecordError(f"runstate: run {run} has no usable record ({problem or 'missing'})")
	change(record)
	writeRecord(root, record)
	return record


def beat(root, run, intervalSeconds):
	"""One heartbeat of run's wrapper (single writer, no lock)."""
	previous = statefile.read(root, HEARTBEAT, run)
	count = (previous["count"] if previous else 0) + 1
	body = {"schema": SCHEMA, "beat_at": homeClock.isoNow(), "interval_seconds": intervalSeconds, "count": count}
	return statefile.write(root, HEARTBEAT, body, run)


def confirmOrphan(root, run, by, evidence):
	"""Record that a caller outside the sandbox verified the run's process is gone (Q3); the run-orphaned event
	is written once. Returns False when it was already confirmed. Refuses a run that has ended."""
	first = {}

	def change(record):
		if record.get("exit"):
			raise RecordError(f"runstate: run {run} has ended; it can't be an orphan")
		if record.get("orphan"):
			return
		first["orphan_of"] = basePhase(record)
		record["orphan"] = {"confirmed_at": homeClock.isoNow(), "by": by, "evidence": evidence}

	record = update(root, run, change)
	if not first:
		return False
	eventlog.emit(
		root, "run-orphaned", SOURCE, {"by": by, "orphan_of": first["orphan_of"]}, topic=record["topic"], run=run
	)
	return True


def wrapupLeft(record):
	"""The wrap-up steps (STEP_ORDER) a run without an exit still owes after its worker ended `STATUS: done` with a
	known exit code: each step missing or not in STEP_FINAL (failed included: a wrap-up run again retries it).
	[] for any other run. `run-steps.py wrapup` run by hand can finish them (it needs claude.exit_code), so such a
	run is kept for it instead of closed. A run whose exit is wrapup-failed (its wrap-up crashed outside its steps,
	recordWrapupFailed) owes them the same way. A run stopped during its wrap-up (stopDuringWrapup: claude's part
	ended unstopped) owes them too, also after its exit stopped, and its steps skipped as stopped count as owed:
	`wt wrapup` continues it (run-steps.py wrapup --continue-stopped). A run whose claude part was stopped owes
	nothing once it has an exit."""
	claude = record.get("claude") or {}
	code = claude.get("exit_code")
	end = record.get("exit") or {}
	stopOwed = stopDuringWrapup(record)
	owingEnds = (WRAPUP_FAILED, "stopped") if stopOwed else (WRAPUP_FAILED,)
	if (end and end.get("reason") not in owingEnds) or not isWorkerDone(claude.get("status")) or type(code) is not int:
		return []
	steps = record.get("steps") or {}
	return [name for name in STEP_ORDER if stepOwed(steps.get(name) or {}, stopOwed)]


def stepOwed(step, stopOwed):
	"""Whether a wrap-up step is still owed: not final, or (stopOwed: the stop came during the wrap-up) skipped as
	stopped."""
	return step.get("state") not in STEP_FINAL or (stopOwed and skippedAsStopped(step))


def skippedAsStopped(step):
	"""Whether a recorded wrap-up step was skipped because the run was stopped."""
	return isinstance(step, dict) and step.get("state") == "skipped" and step.get("skip_reason") == "stopped"


def stepsUsedUp(record, attemptsMax):
	"""The steps wrapupLeft owes whose attempts reached attemptsMax: no wrap-up claims them again (startStep)."""
	steps = record.get("steps") or {}
	return [name for name in wrapupLeft(record)
		if isNumber((steps.get(name) or {}).get("attempt")) and steps[name]["attempt"] >= attemptsMax]


def stoppedRun(record):
	"""Whether the run was stopped: the person's `wt stop` (stop), a stop of claude's part (claude.stopped) or of its
	wrap-up (an exit stopped, a step skipped as stopped). Automatic triggers never continue such a run
	(reconciler.classify); a wrap-up run again without --continue-stopped skips as stopped (run-steps.py Wrap)."""
	claude = record.get("claude") or {}
	steps = record.get("steps") or {}
	return bool(
		record.get("stop")
		or claude.get("stopped")
		or (record.get("exit") or {}).get("reason") == "stopped"
		or any(skippedAsStopped(step) for step in steps.values())
	)


def stopDuringWrapup(record):
	"""Whether the run was stopped (stoppedRun) only during its wrap-up: claude's part ended unstopped, as the
	wrapper recorded it (claude.stopped False; missing counts as unknown, never as unstopped)."""
	return (record.get("claude") or {}).get("stopped") is False and stoppedRun(record)


def wrapupCommand(mainDir, root, topic, run, continueStopped=False):
	"""The command that finishes run's wrap-up by hand, outside the sandbox (run-steps.py wrapup: the record's
	claude.exit_code and claude.stopped decide; WT_MAIN_DIR names the main checkout as worker-control sets it).
	continueStopped: the person's `wt wrapup`, which continues a wrap-up that was stopped (--continue-stopped)."""
	program = os.path.join(homepath.HOOKS, "worker", "run-steps.py")
	argv = ["python3", program, "wrapup", str(root), str(run), "--topic", topic]
	if continueStopped:
		argv.append("--continue-stopped")
	return f"WT_MAIN_DIR={shlex.quote(str(mainDir))} {shlex.join(argv)}"


def closeOrphan(root, run, by):
	"""Close a confirmed orphan (Q3, `wt resume` after confirmOrphan): exit reason orphan-closed, unfinished
	steps abandoned. It never marks a step done, never adds one, so a closed orphan can't read as merged.
	Refuses a run whose worker ended `STATUS: done` with wrap-up steps left (wrapupLeft): its wrap-up is kept for
	`run-steps.py wrapup`, never abandoned."""

	def change(record):
		if not (record.get("orphan") or {}).get("confirmed_at"):
			raise RecordError(f"runstate: run {run} is not a confirmed orphan")
		if record.get("exit"):
			raise RecordError(f"runstate: run {run} has ended already")
		left = wrapupLeft(record)
		if left:
			raise RecordError(
				f"runstate: run {run} ended STATUS done with its wrap-up unfinished ({', '.join(left)}); "
				"it is kept for run-steps.py wrapup, not closed"
			)
		for step in (record.get("steps") or {}).values():
			if step.get("state") in UNFINISHED_STEP:
				step["state"] = "abandoned"
		record["exit"] = {"ended_at": homeClock.isoNow(), "code": None, "reason": "orphan-closed", "closed_by": by}

	record = update(root, run, change)
	eventlog.emit(root, "run-ended", SOURCE, {"reason": "orphan-closed", "code": None}, topic=record["topic"], run=run)
	return record


def closeVerifiedOrphan(root, run, by, evidence):
	"""`wt resume` on a run whose wrapper a pid check outside the sandbox found gone (Q3): record the confirmation
	(run-orphaned, once) and close the run (closeOrphan). RecordError when the run has no usable record or has
	ended; the caller starts nothing then."""
	confirmOrphan(root, run, by, evidence)
	return closeOrphan(root, run, by)


# -- today's files -> the record shape ---------------------------------------------------------------


def legacyFiles(root, run):
	"""The run's files of today's layout, by name (phase-/compact- files listed together)."""
	found = {}
	try:
		names = sorted(os.listdir(runsDir(root)))
	except OSError:
		return found
	for name in names:
		match = LEGACY_FILE.match(name)
		if match and int(match.group(2)) == run:
			found.setdefault(match.group(1), []).append(os.path.join(runsDir(root), name))
	extra = os.path.join(runsDir(root), f"verifier-{run}")
	if os.path.isdir(extra):
		found.setdefault("verifier-dir", []).append(extra)
	return found


def legacyStartedBy(estimate):
	if not estimate.get("auto"):
		return "person"
	return {"merge": "auto-merge", "fix": "auto-fix"}.get(estimate.get("mode")) or "auto-resume"


def legacyClaude(root, run, files, exitCode, stopped):
	"""The claude part from result-<run>.json; None while the worker still runs (no result yet, or a PHASE
	turn the wrapper is about to continue)."""
	path = legacyPath(root, "result", run, ".json")
	data, _error = readObject(path)
	if data is None:
		return None
	text = str(data.get("result") or "")
	written = report_header.value(text, "STATUS")
	status = written if workerStatus(written) else None
	phaseTurn = report_header.value(text, "PHASE") is not None
	if phaseTurn and exitCode is None:
		return None
	if data.get("is_error") or (exitCode not in (None, 0, BUDGET_EXIT_CODE)):
		outcome = "error"
	elif exitCode == BUDGET_EXIT_CODE:
		outcome = "budget-used"
	elif status is not None:
		outcome = "status"
	elif stopped:
		outcome = "stopped"
	elif phaseTurn:
		outcome = "phase-limit"
	else:
		outcome = "no-status"
	return {
		"ended_at": homeClock.isoOf(mtimeOf(path)),
		"outcome": outcome,
		"status": status,
		"header_file": os.path.basename(path),
	}


def legacyExit(root, run, files, later):
	"""(exit object or None, exit code or None) from exit-<run>; a later run that started ends this one (Q9)."""
	text = (readText(legacyPath(root, "exit", run)) or "").strip()
	if text:
		code, _sep, when = text.partition(" ")
		code = int(code) if code.lstrip("-").isdigit() else None
		if code == NOT_LOGGED_IN_CODE:
			reason = "not-logged-in"
		elif "stopped" in files:
			reason = "stopped"
		elif code == CONFIG_ERROR_CODE and not files.get("result") and not files.get("phase"):
			reason = "config-error"
		else:
			reason = "completed"
		return {"ended_at": when if homeClock.isStamp(when) else None, "code": code, "reason": reason}, code
	if later:
		return {"ended_at": None, "code": None, "reason": "superseded"}, None
	return None, None


def legacyVerifyStep(root, run, spec):
	data, error = readObject(legacyPath(root, "verify", run, ".json"))
	if data is None and error is None:
		return {"state": "skipped", "skip_reason": "no-spec"} if spec == "absent" else None
	if data is None:
		return {"state": "failed", "reason": error}
	outcome = "PASS" if data.get("passed") is True else "ERROR" if data.get("error") else "FAIL"
	return {"state": "done", "outcome": outcome, "detail": f"verify-{run}.json"}


def legacyVerifierStep(root, run, spec):
	data, error = readObject(legacyPath(root, "verifier", run, ".json"))
	if data is None and error is None:
		if spec == "absent":
			return {"state": "skipped", "skip_reason": "no-spec"}
		if isinstance(spec, dict) and "verifier" not in spec:
			return {"state": "skipped", "skip_reason": "no-verifier-section"}
		return None
	if data is None:
		return {"state": "failed", "reason": error}
	outcome = {"passed": "PASS", "failed": "FAIL"}.get(data.get("verdict")) or "ERROR"
	if outcome == "ERROR":
		# The verifier could not judge (its own failure, not the branch's): a failed step, so the wrap-up run again
		# retries it under step_attempts_max; PASS and FAIL are final.
		why = str(data.get("why") or "the verifier could not judge")[:300]
		return {"state": "failed", "outcome": outcome, "reason": why, "detail": f"verifier-{run}.json"}
	return {"state": "done", "outcome": outcome, "detail": f"verifier-{run}.json"}


def legacyMergeStep(root, run):
	data, error = readObject(legacyPath(root, "merge", run, ".json"))
	if data is None:
		return None if error is None else {"state": "failed", "reason": error}
	decision = data.get("decision")
	if decision == "merged":
		outcome = "already-merged" if data.get("review") == "already-merged" else "merged"
	elif decision == "refused":
		outcome = "refused"
	else:
		outcome = "error"
	step = {
		"state": "done",
		"outcome": outcome,
		"detail": f"merge-{run}.json",
		"ended_at": homeClock.isoOf(mtimeOf(legacyPath(root, "merge", run, ".json"))),
	}
	if data.get("reason"):
		step["reason"] = str(data.get("reason"))
	if data.get("commit"):
		step["commit"] = str(data.get("commit"))
	step["flags"] = {"merge_run": bool(data.get("merge_run")), "fix_run": bool(data.get("fix_run"))}
	return step


def legacyNextStep(root, run):
	data, error = readObject(legacyPath(root, "auto", run, ".json"))
	if data is None:
		return None if error is None else {"state": "failed", "reason": error}
	outcome = data.get("decision") if data.get("decision") in STEP_OUTCOMES["next"] else "refused"
	step = {"state": "done", "outcome": outcome, "detail": f"auto-{run}.json"}
	if data.get("reason"):
		step["reason"] = str(data.get("reason"))
	for key in ("minutes", "usd"):
		if isNumber(data.get(key)):
			step[key] = data[key]
	return step


def isNumber(value):
	return isinstance(value, (int, float)) and not isinstance(value, bool)


def verifySpec(mainDir, topic):
	"""The topic's verify spec as today's runs see it: the object, "absent", or None when plans can't be seen."""
	if mainDir is None or not plansVisible(mainDir):
		return None
	data, error = readObject(os.path.join(str(mainDir), "plans", "verify", f"{topic}.json"))
	if data is None and error is None:
		return "absent"
	return data


def workerState(mainDir, topic):
	if mainDir is None:
		return {}
	data, _error = readObject(os.path.join(hookonly.stateDir(str(mainDir), "workers"), f"{topic}.json"))
	return data or {}


def legacyIntegration(root):
	"""legacyRecord's integration mode, read as before S4 (also for an archived topic, which is no checkout):
	the topic's flow-mode text, "local" or else "origin", None without one. Every live decision asks
	homeconfig.flowMode instead."""
	text = (readText(hookonly.stateDir(root, "flow-mode")) or "").strip()
	return "local" if text == "local" else "origin" if text else None


def recordFileOf(recordFile):
	"""fileOf for a run record: every field comes from the record file."""
	return lambda field: recordFile


def legacySources(root, run, mainDir, topic):
	"""fileOf for a record legacyRecord made: the file of today's layout each field came from."""
	files = {
		"exit": legacyPath(root, "exit", run),
		"launch.launched_at": legacyPath(root, "est", run, ".json"),
		"process.started_at": legacyPath(root, "start", run),
		"claude.ended_at": legacyPath(root, "result", run, ".json"),
		"process.wrapper_pid": os.path.join(hookonly.stateDir(str(mainDir), "workers"), f"{topic}.json")
		if mainDir
		else None,
	}
	return lambda field: files.get(field) or runsDir(root)


def legacyRecord(root, topic, run, *, later=False, mainDir=None):
	"""The record today's files of run imply (module doc), or None when the run has no file at all. Read-only."""
	files = legacyFiles(root, run)
	if not files:
		return None
	estimate, _error = readObject(legacyPath(root, "est", run, ".json"))
	estimate = estimate or {}
	times = [m for m in (mtimeOf(p) for group in files.values() for p in group) if m]
	launchedAt = mtimeOf(legacyPath(root, "est", run, ".json")) or min(times)
	started = (readText(legacyPath(root, "start", run)) or "").strip()
	end, code = legacyExit(root, run, files, later)
	stopped = "stopped" in files
	record = {
		"schema": SCHEMA,
		"topic": topic,
		"run": run,
		"started_by": legacyStartedBy(estimate),
		"purpose": estimate.get("mode") if estimate.get("mode") in PURPOSES else "build",
		"flow": "scripted" if os.path.isfile(os.path.join(hookonly.stateDir(root, "flow"), "scripted")) else "written",
		"integration_mode": legacyIntegration(root),
		"launch": {"launched_at": homeClock.isoOf(launchedAt)},
		"estimate": {
			"minutes": estimate.get("minutes"),
			"usd": estimate.get("usd"),
			"cap_minutes": estimate.get("cap_minutes"),
			"cap_usd": estimate.get("cap"),
		}
		if estimate
		else None,
		"process": {"started_at": started if homeClock.isStamp(started) else None},
		"main_head_at_start": (readText(legacyPath(root, "main", run)) or "").strip() or None,
		"claude": legacyClaude(root, run, files, code if end else None, stopped),
		"stop": None,
		"dispatch_stop": None,
		"approve": None,
		"orphan": None,
		"steps": {},
		"exit": end,
	}
	state = workerState(mainDir, topic)
	if state.get("run") == run and isinstance(state.get("pid"), int):
		record["process"]["wrapper_pid"] = state["pid"]
	if stopped:
		when = (readText(legacyPath(root, "stopped", run)) or "").strip()
		moment = parseStamp(when)
		record["stop"] = {"requested_at": homeClock.isoOf(moment) if moment else None, "by": "person"}
	resume, _error = readObject(legacyPath(root, "auto-resume", run, ".json"))
	if resume:
		record["dispatch_stop"] = {"minutes": resume.get("minutes"), "usd": resume.get("usd")}
	approval, _error = readObject(legacyPath(root, "approve", run, ".json"))
	if approval:
		moment = parseStamp(approval.get("started"))
		record["approve"] = {"pid": approval.get("pid"), "started_at": homeClock.isoOf(moment) if moment else None}
	spec = verifySpec(mainDir, topic)
	for name, step in (
		("verify", legacyVerifyStep(root, run, spec)),
		("verifier", legacyVerifierStep(root, run, spec)),
		("merge", legacyMergeStep(root, run)),
		("next", legacyNextStep(root, run)),
	):
		if step is not None:
			record["steps"][name] = step
	return record


# -- phase -------------------------------------------------------------------------------------------


def basePhase(record):
	"""The phase the facts imply before any orphan rule."""
	if record.get("exit"):
		return "ended"
	if not (record.get("process") or {}).get("started_at"):
		return "launching"
	if (record.get("claude") or {}).get("ended_at"):
		return "finishing"
	return "running"


def evidenceOf(claim, file, field, value, **extra):
	return {"claim": claim, "file": file, "field": field, "value": value, **extra}


def recordLiveness(record, heartbeat, files, limits):
	"""The last sign of life of a run with a record and the age limit: the heartbeat, else the wrapper's start,
	else the launch; limit = the heartbeat's interval (else the setting) x orphan_missed_beats_count."""
	interval = (heartbeat or {}).get("interval_seconds") or limits["heartbeat_interval_seconds"]
	limit = interval * limits["orphan_missed_beats_count"]
	if heartbeat and heartbeat.get("beat_at"):
		return {"at": heartbeat["beat_at"], "file": files["heartbeat"], "field": "beat_at", "limit_seconds": limit}
	started = (record.get("process") or {}).get("started_at")
	if started:
		return {"at": started, "file": files["record"], "field": "process.started_at", "limit_seconds": limit}
	return {
		"at": record["launch"]["launched_at"],
		"file": files["record"],
		"field": "launch.launched_at",
		"limit_seconds": limit,
	}


def legacyLiveness(record, root, run, limits):
	"""Today's files have no heartbeat: launching counts from est-<run>.json like a record; a running worker
	from its start plus its minute limit plus the wrap-up allowance; a finishing one from its newest file."""
	base = basePhase(record)
	wrapup = limits["legacy_wrapup_stale_minutes"] * 60
	if base == "launching":
		limit = limits["heartbeat_interval_seconds"] * limits["orphan_missed_beats_count"]
		return {
			"at": record["launch"]["launched_at"],
			"file": legacyPath(root, "est", run, ".json"),
			"field": "mtime",
			"limit_seconds": limit,
		}
	if base == "running":
		cap = (record.get("estimate") or {}).get("cap_minutes")
		if not isinstance(cap, (int, float)):
			return None
		return {
			"at": record["process"]["started_at"],
			"file": legacyPath(root, "start", run),
			"field": "text",
			"limit_seconds": cap * 60 + wrapup,
		}
	dated = [(m, p) for m, p in ((mtimeOf(p), p) for group in legacyFiles(root, run).values() for p in group) if m]
	newest, path = max(dated)
	return {"at": homeClock.isoOf(newest), "file": path, "field": "mtime", "limit_seconds": wrapup}


def phaseOf(record, liveness, *, now, bootId, alive, fileOf):
	"""{phase, orphan_of, confidence, warnings, evidence} from the facts (module doc). alive: the pid check's
	answer, None when none was made; fileOf(field): the file a field was read from."""
	base = basePhase(record)
	evidence, warnings = [], []
	if base == "ended":
		end = record["exit"]
		evidence.append(
			evidenceOf("phase", fileOf("exit"), "exit", {k: end.get(k) for k in ("ended_at", "code", "reason")})
		)
		return {"phase": "ended", "orphan_of": None, "confidence": None, "warnings": warnings, "evidence": evidence}
	reasons = []
	process = record.get("process") or {}
	confirmed = (record.get("orphan") or {}).get("confirmed_at")
	if confirmed:
		reasons.append("recorded")
		evidence.append(evidenceOf("orphan", fileOf("orphan"), "orphan.confirmed_at", confirmed))
	if process.get("exited_at"):
		reasons.append("wrapper-exited")
		evidence.append(evidenceOf("orphan", fileOf("process"), "process.exited_at", process["exited_at"]))
	if bootId and process.get("boot_id") and process["boot_id"] != bootId:
		reasons.append("boot")
		evidence.append(evidenceOf("orphan", fileOf("process"), "process.boot_id", process["boot_id"], current=bootId))
	stale = False
	if liveness is not None:
		last = parseStamp(liveness["at"])
		age = int((now - last).total_seconds()) if last else None
		stale = age is None or age > liveness["limit_seconds"]
		evidence.append(
			evidenceOf(
				"alive",
				liveness["file"],
				liveness["field"],
				liveness["at"],
				age_seconds=age,
				threshold_seconds=liveness["limit_seconds"],
			)
		)
	if stale and alive is True and not reasons:
		warnings.append("heartbeat-stale-process-alive")
		stale = False
	if stale:
		reasons.append("stale")
	if alive is False:
		reasons.append("process-gone")
		evidence.append(
			evidenceOf(
				"orphan",
				fileOf("process.wrapper_pid"),
				"process.wrapper_pid",
				process.get("wrapper_pid"),
				checked="gone",
			)
		)
	if alive is True:
		evidence.append(
			evidenceOf(
				"alive",
				fileOf("process.wrapper_pid"),
				"process.wrapper_pid",
				process.get("wrapper_pid"),
				checked="alive",
			)
		)
	if not reasons:
		evidence.append(evidenceOf("phase", fileOf(phaseField(base)), phaseField(base), phaseValue(record, base)))
		return {"phase": base, "orphan_of": None, "confidence": None, "warnings": warnings, "evidence": evidence}
	confidence = "confirmed" if confirmed or alive is False else "suspected"
	return {
		"phase": "orphan",
		"orphan_of": base,
		"confidence": confidence,
		"reasons": reasons,
		"warnings": warnings,
		"evidence": evidence,
	}


def phaseField(base):
	return {"launching": "launch.launched_at", "running": "process.started_at", "finishing": "claude.ended_at"}[base]


def phaseValue(record, base):
	if base == "launching":
		return record["launch"]["launched_at"]
	if base == "running":
		return record["process"]["started_at"]
	return record["claude"]["ended_at"]


# -- the rest of the status --------------------------------------------------------------------------


def plansVisible(mainDir):
	try:
		return bool(os.listdir(os.path.join(str(mainDir), "plans")))
	except OSError:
		return False


def heldOf(root):
	"""agent-guard ALERT or HOLD pending (Q6: ALERT counts now)."""
	guard = hookonly.stateDir(root, "agent-guard")
	files = [os.path.join(guard, n) for n in ("ALERT", "HOLD") if os.path.exists(os.path.join(guard, n))]
	state = "hold" if any(f.endswith("HOLD") for f in files) else "alert" if files else "clear"
	return {"state": state, "files": files}


def flowOf(root, record):
	"""The flow of this run: written, or scripted with the flow hook's state (state.json with or without its
	schema, Q8), the next step's kind and the last stop (log.jsonl)."""
	if record is not None and record.get("flow") == "written":
		return {"kind": "written"}, []
	folder = hookonly.stateDir(root, "flow")
	marker = os.path.isfile(os.path.join(folder, "scripted"))
	state, error = readObject(os.path.join(folder, "state.json"))
	if state is None and not marker:
		return {"kind": "written"}, []
	warnings = []
	if state is not None and "schema" not in state:
		warnings.append("flow-state-legacy")
	elif state is not None and state.get("schema") != FLOW_SCHEMA:
		return {
			"kind": "scripted",
			"phase": None,
			"error": f"flow state schema {state.get('schema')!r} unknown",
			"resume": None,
		}, warnings
	if state is None:
		return {"kind": "scripted", "phase": None, "error": error or "no flow state", "resume": None}, warnings
	phase = state.get("phase") if marker else "off"
	if phase not in FLOW_PHASES:
		return {"kind": "scripted", "phase": None, "error": f"flow phase {phase!r} unknown", "resume": None}, warnings
	waiting = [
		{"agent_id": agentId, "agent_type": (meta or {}).get("role")}
		for agentId, meta in sorted((state.get("waiting") or {}).items())
	]
	lines = []
	for line in (readText(os.path.join(folder, "log.jsonl")) or "").splitlines():
		try:
			entry = json.loads(line)
		except ValueError:
			continue
		if isinstance(entry, dict):
			lines.append(entry)
	stops = [e for e in lines if e.get("do") == "status"]
	resume = {"running": "scripted", "wrapup": "scripted", "stopped": "retry-decides", "off": "written"}[phase]
	return {
		"kind": "scripted",
		"phase": phase,
		"seq": state.get("seq"),
		"waiting": waiting,
		"next": lines[-1].get("do") if lines else None,
		"stop_reason": stops[-1].get("event") if stops and phase == "stopped" else None,
		"resume": resume,
		"files": [os.path.join(folder, n) for n in ("state.json", "next.md", "log.jsonl")],
	}, warnings


def runningStep(record):
	for name in STEP_ORDER:
		step = (record.get("steps") or {}).get(name)
		if step and step.get("state") == "running":
			return {"name": name, "state": "running", "since": step.get("started_at"), "attempt": step.get("attempt"),
				"limit_seconds": step.get("limit_seconds")}
	return None


def block(fact, reason, **extra):
	return {"fact": fact, "reason": reason, **extra}


# The merge block of a stop that came only after claude's part completed (mergeBlocks): auto-merge.py refuses it,
# except for the person's approval (--person).
WRAPUP_STOP = "wrapup-stop"


def stopAfterClaude(record):
	"""Whether the run's stop came only after claude's part completed, so the person's merge approval may pass it
	(mergeBlocks): the worker ended with a STATUS done and claude.stopped is False (the record); a story without
	that flag (today's files) needs the person's stop recorded strictly after claude's end, both in whole seconds,
	so a stop in the same second as the end counts as one during claude's part."""
	claude = record.get("claude") or {}
	if claude.get("outcome") != "status" or not isWorkerDone(claude.get("status")):
		return False
	if "stopped" in claude:
		return claude["stopped"] is False
	stopAt = parseStamp((record.get("stop") or {}).get("requested_at"))
	endedAt = parseStamp(claude.get("ended_at"))
	return stopAt is not None and endedAt is not None and stopAt.replace(microsecond=0) > endedAt.replace(microsecond=0)


def verifyBlock(step, spec):
	"""The verify step's block, None when it passes (module doc): a PASS, or no spec and no step that ran."""
	if (step or {}).get("state") == "done" and step.get("outcome") == "PASS":
		return None
	if spec == "absent" and (not step or (step.get("state") == "skipped" and step.get("skip_reason") == "no-spec")):
		return None
	outcome = step.get("outcome") if (step or {}).get("state") == "done" else None
	return block("verify", f"verify step {describeStep(step)}", outcome=outcome)


def verifierBlock(step, spec):
	"""The verifier step's block, None when it passes (module doc): no spec, a spec without a "verifier" key, or
	a PASS under a spec that reads as an object."""
	if spec == "absent" or (isinstance(spec, dict) and "verifier" not in spec):
		return None
	if not isinstance(spec, dict):
		return block("verifier", "the verify spec can't be read or seen", outcome=None)
	if (step or {}).get("state") == "done" and step.get("outcome") == "PASS":
		return None
	# A failed step carries an outcome only when its verifier could not judge (ERROR, legacyVerifierStep).
	outcome = step.get("outcome") if (step or {}).get("state") in ("done", "failed") else None
	return block("verifier", f"verifier step {describeStep(step)}", outcome=outcome)


def mergeBlocks(record, phase, held, spec, lead):
	"""Why a merge gate must refuse this run, one {fact, reason} each ([] = every fact it needs is there and known;
	module doc). spec: the topic's verify spec as verifySpec reads it now; lead: claude.lead_status (leadStatus).
	The verify and verifier blocks carry the step's outcome (None unless the step is done)."""
	if phase in ("unknown", "none"):
		return [block("phase", "the run state is unknown" if phase == "unknown" else "no run")]
	if phase == "orphan":
		return [block("phase", "the run is an orphan: its wrap-up never finished")]
	if phase not in ("finishing", "ended"):
		return [block("phase", f"the run is {phase}")]
	blocks = []
	claude = record.get("claude") or {}
	steps = record.get("steps") or {}
	end = record.get("exit")
	afterWork = stopAfterClaude(record)
	if record.get("stop"):
		blocks.append(block(WRAPUP_STOP if afterWork else "stop", "the run was stopped"))
	if end and end.get("reason") != "completed":
		stoppedAfterWork = afterWork and end.get("reason") == "stopped"
		blocks.append(block(WRAPUP_STOP if stoppedAfterWork else "exit", f"the run ended {end.get('reason')}"))
	if claude.get("outcome") != "status":
		blocks.append(block("claude", f"the worker's outcome is {claude.get('outcome')!r}, not a STATUS"))
	if not isWorkerDone(claude.get("status")):
		blocks.append(block("claude", f"the worker's STATUS is {claude.get('status')!r}, not done"))
	elif lead != claude.get("status"):
		blocks.append(block("claude", "the worker's header doesn't start with its STATUS line written exactly"))
	if record.get("integration_mode") != "local":
		blocks.append(
			block("integration", f"integration mode {record.get('integration_mode')!r}: only local merges here")
		)
	if not record.get("main_head_at_start"):
		blocks.append(block("main_head", "no main head recorded at the start"))
	for found in (verifyBlock(steps.get("verify"), spec), verifierBlock(steps.get("verifier"), spec)):
		if found:
			blocks.append(found)
	if held["state"] != "clear":
		blocks.append(block("held", f"agent-guard {held['state'].upper()} pending"))
	return blocks


def leadStatus(root, claude):
	"""The STATUS value when it is the first line of the worker's header (claude.header_file in root's runs),
	written exactly `STATUS: <value>`; None for any other first line or a file that can't be read."""
	name = (claude or {}).get("header_file")
	if not isinstance(name, str) or os.path.basename(name) != name:
		return None
	data, _error = readObject(os.path.join(runsDir(root), name))
	found = report_header.leading(str((data or {}).get("result") or ""))
	return found[1] if found and found[0] == "STATUS" else None


def describeStep(step):
	if not step:
		return "missing"
	return f"{step.get('state')} {step.get('outcome') or step.get('skip_reason') or ''}".strip()


def recordOrLegacy(root, topic, run, records, mainDir):
	"""Run's record, else (no record file) what today's files imply; None when neither can be read."""
	if run in records:
		record, _problem = readRecord(root, run)
		return record
	return legacyRecord(root, topic, run, later=True, mainDir=mainDir)


def lastPersonLaunch(root, run, records, mainDir, topic):
	"""When the last run a person (or the queue, on the person's behalf) started, run or before, was launched;
	automatic follow-up runs (auto-resume, auto-merge, auto-fix) are skipped. None without one."""
	for k in range(run, 0, -1):
		record = recordOrLegacy(root, topic, k, records, mainDir)
		if record is not None and record.get("started_by") in HUMAN_STARTS:
			return parseStamp(record["launch"]["launched_at"])
	return None


def resumable(ok, why, kind, code):
	return {"ok": ok, "why": why, "prompt_kind": kind, "code": code}


def resumableOf(result, record, root, mainDir, topic, records):
	"""What `wt resume` does (worker-control.py, migration step 4): {ok, why, prompt_kind, code}, checked in this
	order: active, unknown, a kept wrap-up (wrapup-left, below: it needs neither plans/ nor an estimate),
	plans-hidden, no-estimate, then the rest.
	Refused (ok False): a run launching, running or finishing (code active), an unknown state (unknown), no
	estimate (no-estimate), STATUS done and no estimate newer than the launch of the last run a person or the
	queue started (done). ok None: plans/ can't be seen here (plans-hidden), or a suspected orphan
	(orphan-suspected): wt resume checks its wrapper first (outside the sandbox) and closes and resumes only when
	it is gone. A confirmed orphan (orphan-confirmed) is resumable: wt resume closes it first (closeVerifiedOrphan:
	orphan-closed, unfinished wrap-up steps abandoned, never a merge), then starts the next run. An orphan whose
	worker ended `STATUS: done` with wrap-up steps left (wrapupLeft; code wrapup-left), or a run that ended
	wrapup-failed or stopped with steps left (its wrap-up crashed outside its steps, or was stopped after claude's part
	ended unstopped: confirmed, the run ended), is never resumed or closed:
	the work is done and only the wrap-up is missing, and a new run beside a wrap-up still owed would put two runs
	on one branch; `wt wrapup <topic>` finishes it. Confirmed: refused (ok False); suspected: ok None (wt wrapup
	checks the wrapper first). Its why is for the person; it also carries steps_left and command (wrapupCommand:
	what wt wrapup starts, for agent()'s evidence view only)."""
	phase, run = result["phase"], result["run"]
	if phase in ACTIVE:
		return resumable(False, f"run {run} is {phase}", None, "active")
	if phase == "unknown":
		return resumable(False, f"run {run}'s state can't be read", None, "unknown")
	# A kept wrap-up needs neither plans/ nor an estimate, so it is named before either is looked at.
	endReason = (record.get("exit") or {}).get("reason")
	crashed = phase == "ended" and endReason == WRAPUP_FAILED
	stopped = phase == "ended" and endReason == "stopped"
	left = wrapupLeft(record) if phase == "orphan" or crashed or stopped else []
	if left:
		confirmed = crashed or stopped or result["confidence"] == "confirmed"
		ended = "wrap-up failed" if crashed else "wrapper " + ("ended" if confirmed else "may have ended")
		before = f"before the wrap-up ({', '.join(left)})"
		if stopped:
			ended, before = "wrap-up was stopped", f"with steps left ({', '.join(left)})"
		why = (
			f"run {run} finished its work (STATUS done), but its {ended} {before}; those steps are kept: type "
			f"`wt wrapup {topic}` (or press the button) to finish them"
			+ ("" if confirmed else ", after it checks the wrapper is gone")
		)
		kept = resumable(False if confirmed else None, why, None, "wrapup-left")
		return {**kept, "steps_left": left, "command": wrapupCommand(mainDir, root, topic, run, continueStopped=True)}
	if not plansVisible(mainDir):
		return resumable(None, "plans/ is not visible here (sandbox)", None, "plans-hidden")
	estimate = os.path.join(str(mainDir), "plans", "worker", f"{topic}.json")
	if not os.path.exists(estimate):
		return resumable(False, f"no estimate yet (plans/worker/{topic}.json)", None, "no-estimate")
	if phase == "none":
		return resumable(True, "no run yet", "feature-flow", None)
	resume = (result["flow"] or {}).get("resume")
	kind = {"scripted": "scripted-resume", "written": "resume"}.get(resume or "written")
	if isWorkerDone((record.get("claude") or {}).get("status")):
		launched = lastPersonLaunch(root, run, records, mainDir, topic)
		if launched is not None and mtimeOf(estimate) <= launched:
			why = "done (STATUS done); nothing to resume until a new estimate (a merge run: --merge)"
			return resumable(False, why, None, "done")
	if phase == "orphan" and result["confidence"] != "confirmed":
		why = (
			f"run {run} may have died: wt resume checks its wrapper process first, closes run {run} and starts "
			f"run {run + 1} only if it is gone, and refuses while it runs or can't be checked (wt status {topic} "
			"shows the check)"
		)
		return resumable(None, why, kind, "orphan-suspected")
	if phase == "orphan":
		why = (
			f"its process is gone: wt resume closes run {run} (orphan-closed: its unfinished wrap-up abandoned, "
			f"never merged), then starts run {run + 1}"
		)
		return resumable(True, why, kind, "orphan-confirmed")
	return resumable(True, None, kind, None)


def approvingOf(record):
	approve = record.get("approve") or {}
	merge = (record.get("steps") or {}).get("merge") or {}
	if not approve or merge.get("outcome") != "refused":
		return None
	started, decided = parseStamp(approve.get("started_at")), parseStamp(merge.get("ended_at"))
	# Stored stamps hold whole seconds: a decision of the same second as the approval's start is older.
	if started is None or (decided is not None and decided.replace(microsecond=0) > started):
		return None
	return {"run": record["run"], "since": hhmm(approve.get("started_at"))}


def busyOf(result, record, laterExists):
	"""(busy, USD it holds) for the queue's limits: module doc, decision facts. A decided next run holds the
	decision's USD (a step recorded without it: the ended run's estimate)."""
	phase = result["phase"]
	usd = ((record or {}).get("estimate") or {}).get("usd")
	if phase in ACTIVE or phase == "unknown" or (phase == "orphan" and result["confidence"] == "suspected"):
		return True, usd
	nextStep = ((record or {}).get("steps") or {}).get("next") or {}
	if phase == "ended" and nextStep.get("outcome") in NEXT_RUNS and not laterExists:
		return True, nextStep["usd"] if isNumber(nextStep.get("usd")) else usd
	return False, None


def status(mainDir, topic, *, root=None, clock=None, bootId=None, pidAlive=None, run=None, fromLegacy=False):
	"""What topic's run is doing (module doc); read-only. root: the topic's checkout when the caller found it
	already (its worktree); else locate() finds it. fromLegacy: read today's files even where a record exists
	(compareStories)."""
	now = (clock or homeClock.now)()
	limits = settings()
	root, archived = (str(root), False) if root else locate(mainDir, topic)
	records, legacy = runNumbers(root) if root else (set(), set())
	numbers = records | legacy
	result = {
		"schema": SCHEMA,
		"topic": topic,
		"run": None,
		"phase": "none",
		"orphan_of": None,
		"confidence": None,
		"pid_checked": False,
		"busy": False,
		"busy_usd": None,
		"step": None,
		"claude": None,
		"outcome": None,
		"next": None,
		"resumable": None,
		"held": heldOf(root) if root else {"state": "clear", "files": []},
		"merge": None,
		"approving": None,
		"stop": None,
		"approve": None,
		"process": None,
		"flow": None,
		"integration_mode": None,
		"purpose": None,
		"estimate": None,
		"plans_visible": plansVisible(mainDir),
		"main_head_at_start": None,
		"steps": None,
		"dispatch_stop": None,
		"merge_allowed": False,
		"merge_blocks": [],
		"merge_refusals": [],
		"source": None,
		"archived": archived,
		"runs_dir": runsDir(root) if root else None,
		"warnings": [],
		"evidence": [],
		"computed_at": homeClock.isoOf(now),
	}
	if not numbers:
		result["merge_blocks"] = mergeBlocks({}, "none", result["held"], None, None)
		result["merge_refusals"] = [b["reason"] for b in result["merge_blocks"]]
		result["resumable"] = resumableOf(result, {}, root, mainDir, topic, records)
		return result
	n = run if run is not None else max(numbers)
	result["run"] = n
	laterExists = any(k > n for k in numbers)
	facts = runFacts(root, topic, n, n in records and not fromLegacy, laterExists, mainDir, limits)
	result["source"] = facts["source"]
	result["warnings"] += facts["warnings"]
	recordFile, record, liveness, fileOf = facts["file"], facts["record"], facts["liveness"], facts["fileOf"]
	if record is None:
		return unknown(result, recordFile, facts["problem"])
	alive = None
	if (
		pidAlive is not None
		and basePhase(record) != "ended"
		and isinstance((record.get("process") or {}).get("wrapper_pid"), int)
	):
		alive = pidAlive(record["process"], topic, n)
	result["pid_checked"] = alive is not None
	derived = phaseOf(
		record, liveness, now=now, bootId=bootId if bootId is not None else readBootId(), alive=alive, fileOf=fileOf
	)
	result.update({k: derived[k] for k in ("phase", "orphan_of", "confidence")})
	result["warnings"] += derived["warnings"]
	result["evidence"] += derived["evidence"]
	if derived.get("reasons"):
		result["orphan_reasons"] = derived["reasons"]
	claude = record.get("claude") or {}
	lead = leadStatus(root, claude) if claude else None
	result["claude"] = (
		{
			"outcome": claude.get("outcome"),
			"status": claude.get("status"),
			"lead_status": lead,
			"ended_at": claude.get("ended_at"),
		}
		if claude
		else None
	)
	result["main_head_at_start"] = record.get("main_head_at_start")
	result["steps"] = record.get("steps")
	result["dispatch_stop"] = record.get("dispatch_stop")
	result["integration_mode"] = record.get("integration_mode")
	result["purpose"] = record.get("purpose")
	result["stop"] = record.get("stop")
	result["approve"] = record.get("approve")
	result["process"] = record.get("process")
	result["estimate"] = record.get("estimate")
	flow, flowWarnings = flowOf(root, record)
	result["flow"] = flow
	result["warnings"] += flowWarnings
	result["step"] = runningStep(record) if result["phase"] == "finishing" else None
	steps = record.get("steps") or {}
	merge = steps.get("merge") or {}
	if result["phase"] == "ended":
		end = record["exit"]
		result["outcome"] = {
			"reason": end.get("reason"),
			"code": end.get("code"),
			"error": end.get("error"),
			"ended_at": end.get("ended_at"),
			"merge": merge.get("outcome"),
			"merge_flags": merge.get("flags"),
			"commit": merge.get("commit"),
			# A wrap-up finished after the wrapper died (wt wrapup): auto-merge refused it as an orphan.
			"after_orphan": bool((record.get("orphan") or {}).get("confirmed_at")),
		}
		result["next"] = {"decision": (steps.get("next") or {}).get("outcome"), "started": laterExists}
		if merge.get("outcome") == "refused" and not (merge.get("flags") or {}).get("merge_run"):
			result["merge"] = {"reason": merge.get("reason"), "detail": merge.get("detail")}
	result["approving"] = approvingOf(record)
	result["busy"], result["busy_usd"] = busyOf(result, record, laterExists)
	result["resumable"] = resumableOf(result, record, root, mainDir, topic, records)
	spec = verifySpec(mainDir, topic)
	result["merge_blocks"] = mergeBlocks(record, result["phase"], result["held"], spec, lead)
	result["merge_refusals"] = [b["reason"] for b in result["merge_blocks"]]
	result["merge_allowed"] = not result["merge_blocks"]
	result["evidence"].append(evidenceOf("files", recordFile, None, None))
	return result


def runFacts(root, topic, run, recorded, later, mainDir, limits):
	"""One run's facts for phaseOf: {"source": record|legacy, "file", "record", "problem", "liveness", "fileOf",
	"warnings"}; record None with the problem when the run can't be read (phase unknown). recorded: read the
	run's record (else today's files through legacyRecord); later: a later run exists (Q9)."""
	if recorded:
		recordFile = statefile.pathOf(root, RECORD, run)
		facts = {"source": "record", "file": recordFile, "warnings": [], "liveness": None, "fileOf": None}
		record, problem = readRecord(root, run)
		facts.update(record=record, problem=problem)
		if record is None:
			return facts
		heartbeatFile = statefile.pathOf(root, HEARTBEAT, run)
		heartbeat, heartbeatError = readObject(heartbeatFile)
		if heartbeat is not None and (
			heartbeat.get("schema") != SCHEMA or not homeClock.isStamp(heartbeat.get("beat_at"))
		):
			heartbeat, heartbeatError = None, "unknown schema or no beat_at"
		if heartbeatError:
			facts["warnings"].append("heartbeat-unreadable")
		facts["liveness"] = recordLiveness(
			record, heartbeat, {"record": recordFile, "heartbeat": heartbeatFile}, limits
		)
		facts["fileOf"] = recordFileOf(recordFile)
		return facts
	facts = {"source": "legacy", "file": runsDir(root), "warnings": ["legacy-files"], "liveness": None, "fileOf": None}
	record = legacyRecord(root, topic, run, later=later, mainDir=mainDir)
	if record is None:
		return {**facts, "record": None, "problem": f"run {run} has no files"}
	problems = validate(record)
	if problems:
		return {**facts, "record": None, "problem": "; ".join(problems)}
	facts.update(
		record=record,
		problem=None,
		liveness=legacyLiveness(record, root, run, limits),
		fileOf=legacySources(root, run, mainDir, topic),
	)
	return facts


def unknown(result, file, problem):
	"""Module doc, unknown: busy (nothing may start on top of it), no merge, the reason as evidence."""
	result.update(
		{
			"phase": "unknown",
			"busy": True,
			"merge_allowed": False,
			"merge_blocks": [block("phase", "the run state is unknown")],
			"merge_refusals": ["the run state is unknown"],
			"error": problem,
			"resumable": resumable(False, f"run {result['run']}'s state can't be read", None, "unknown"),
		}
	)
	result["evidence"].append(evidenceOf("unknown", file, None, problem))
	return result


# -- process check (outside the sandbox only) ---------------------------------------------------------


def verifyProcess(process, topic, run):
	"""Whether the recorded wrapper still runs: /proc/<pid> exists, its command line is run-worker.sh with the
	topic and run, and its start time (stat field 22) is the recorded start_ticks when one is recorded.
	True / False; None when the pid isn't recorded or /proc can't be read. Inside the sandbox other
	processes are invisible, so a caller there must not pass this to status()."""
	pid = process.get("wrapper_pid")
	if not isinstance(pid, int) or isinstance(pid, bool):
		return None
	try:
		with open(f"/proc/{pid}/cmdline", "rb") as f:
			argv = f.read().split(b"\0")
	except FileNotFoundError:
		return False
	except OSError:
		return None
	if not any(os.path.basename(a.decode(errors="replace")) == WRAPPER_NAME for a in argv):
		return False
	if topic.encode() not in argv or str(run).encode() not in argv:
		return False
	ticks = process.get("start_ticks")
	if ticks is None:
		return True
	try:
		with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
			fields = f.read().rsplit(")", 1)[1].split()
	except (OSError, IndexError):
		return None
	return int(fields[19]) == ticks


# -- facts across a checkout's runs (migration step 4 readers) ---------------------------------------


def lastLife(record, liveness):
	"""An orphan's (or an ended run's without an end time) last sign of life: the wrapper's exit, else the
	liveness the phase was judged by (heartbeat, start, launch, or a legacy run's newest file); None unknown."""
	exited = parseStamp((record.get("process") or {}).get("exited_at"))
	if exited is not None:
		return exited
	return parseStamp((liveness or {}).get("at"))


def activeSince(root, since, *, clock=None, bootId=None):
	"""Whether any worker run of the checkout root was active at a moment at or after since (an aware datetime):
	agent-guard's explained ref moves (design T-6). Active: launching, running, finishing, or unknown (a run that
	can't be read can't be ruled out); an ended run whose exit is at or after since. An orphan counts only until
	its last sign of life (lastLife), so a branch move after the run died is never explained by it. Read-only,
	files only (no pid check)."""
	now = (clock or homeClock.now)()
	limits = settings()
	boot = bootId if bootId is not None else readBootId()
	records, legacy = runNumbers(root)
	numbers = records | legacy
	topic = os.path.basename(os.path.realpath(str(root)))
	for run in sorted(numbers):
		later = any(k > run for k in numbers)
		facts = runFacts(str(root), topic, run, run in records, later, None, limits)
		record = facts["record"]
		if record is None:
			return True
		phase = phaseOf(record, facts["liveness"], now=now, bootId=boot, alive=None, fileOf=facts["fileOf"])["phase"]
		if phase in ACTIVE:
			return True
		ended = parseStamp((record.get("exit") or {}).get("ended_at")) if phase == "ended" else None
		last = ended if ended is not None else lastLife(record, facts["liveness"])
		if last is None or last >= since:
			return True
	return False


def current(root):
	"""The checkout's newest run whose wrapper started: {"run", "started_at"} from its record's
	process.started_at, else (no usable record, or no start in it) today's start-<run> file; started_at None
	when that file can't be read as a time. None when no run has started. The run's clock for worker-budget
	and dispatch-guard, its number for worker-progress and dispatch-guard's records (design D2)."""
	records, legacy = runNumbers(root)
	for run in sorted(records | legacy, reverse=True):
		if run in records:
			record, _problem = readRecord(root, run)
			started = ((record or {}).get("process") or {}).get("started_at")
			if started:
				return {"run": run, "started_at": started}
		text = readText(legacyPath(root, "start", run))
		if text is not None:
			return {"run": run, "started_at": text.strip() if homeClock.isStamp(text.strip()) else None}
	return None


def startedSeconds(root):
	"""current(root)'s start as epoch seconds, only for a duration (worker-budget, dispatch-guard); None when no
	run has started. ValueError when the newest started run's start can't be read (the callers are guard steps of
	hook-entry.py's pre entry: the exception denies the call, fail closed)."""
	found = current(root)
	if found is None:
		return None
	moment = parseStamp(found["started_at"])
	if moment is None:
		raise ValueError(f"run {found['run']}'s start can't be read")
	return moment.timestamp()


def lastResultFile(root, run):
	"""Run's newest claude -p output: of result-, compact- and phase- files the one with the highest
	total_cost_usd (cumulative for the session); None without one."""
	best, top = None, None
	files = legacyFiles(root, run)
	for path in files.get("result", []) + files.get("compact", []) + files.get("phase", []):
		data, _error = readObject(path)
		cost = (data or {}).get("total_cost_usd")
		if isNumber(cost) and (top is None or cost > top):
			best, top = path, cost
	return best


def spentUsd(root, run, record):
	"""What run spent: its record's last invocation total (cost_usd is the run's spend so far), else today's
	files: the newest output's total less the same session's total at the end of the run before."""
	calls = (record.get("claude") or {}).get("invocations") or []
	costs = [call.get("cost_usd") for call in calls if isNumber(call.get("cost_usd"))]
	if costs:
		return float(max(costs))
	path = lastResultFile(root, run)
	if path is None:
		return 0.0
	data = readObject(path)[0] or {}
	total = float(data.get("total_cost_usd") or 0)
	for earlier in range(run - 1, 0, -1):
		before = lastResultFile(root, earlier)
		if before is not None:
			previous = readObject(before)[0] or {}
			same = previous.get("session_id") == data.get("session_id")
			return total - (float(previous.get("total_cost_usd") or 0) if same else 0.0)
	return total


def leftOfEstimate(root, topic, run, *, mainDir=None, clock=None):
	"""(minutes, usd) left of the estimate of the last run a person or the queue started (run or before), after
	the runs from that one to run (each from its wrapper's start to its exit, or now; its spend), from the
	records and the legacy adapter; never below 0. None when a run's start or the estimate can't be read."""
	now = (clock or homeClock.now)()
	records, _legacy = runNumbers(root)
	first = run
	while first > 1:
		record = recordOrLegacy(root, topic, first, records, mainDir)
		if record is None or record.get("started_by") in HUMAN_STARTS:
			break
		first -= 1
	head = recordOrLegacy(root, topic, first, records, mainDir)
	if head is None:
		return None
	estimate = head.get("estimate") or {}
	minutes = usd = 0.0
	for k in range(first, run + 1):
		record = recordOrLegacy(root, topic, k, records, mainDir)
		started = parseStamp(((record or {}).get("process") or {}).get("started_at"))
		if started is None:
			return None
		ended = parseStamp((record.get("exit") or {}).get("ended_at")) or now
		minutes += (ended - started).total_seconds() / 60
		usd += spentUsd(root, k, record)
	return max(float(estimate.get("minutes") or 0) - minutes, 0.0), max(float(estimate.get("usd") or 0) - usd, 0.0)


# -- renderers ---------------------------------------------------------------------------------------


def texts(lang):
	table = settings()["status_text"]
	if lang not in table:
		raise homeconfig.ConfigError(
			f"runstate: no status text for language {lang!r} (budget run_state.status_text)"
		)
	return table[lang]


def minutesBetween(start, end):
	first, last = parseStamp(start), parseStamp(end)
	return int((last - first).total_seconds() // 60) if first and last else "?"


def orphanWhy(status, words):
	reasons = status.get("orphan_reasons") or []
	alive = next((e for e in status["evidence"] if e["claim"] == "alive" and "age_seconds" in e), None)
	if "boot" in reasons:
		return words["why_boot"]
	if "wrapper-exited" in reasons:
		exited = next((e["value"] for e in status["evidence"] if e["field"] == "process.exited_at"), None)
		return words["why_wrapper_exited"].format(since=hhmm(exited))
	if alive is not None:
		age = alive["age_seconds"]
		return words["why_stale"].format(
			since=hhmm(alive["value"]),
			age_minutes=age // 60 if isinstance(age, int) else "?",
			threshold_minutes=alive["threshold_seconds"] // 60,
		)
	return words["why_stale"].format(since="?", age_minutes="?", threshold_minutes="?")


def lineAndTodo(status, words):
	"""(line, todo) for one status in one language (human's table)."""
	phase, topic, run = status["phase"], status["topic"], status["run"]
	fill = {"topic": topic, "run": run}
	resume = {"command": f"wt resume {topic}", "button": "resume"}
	if phase == "none":
		ok = (status.get("resumable") or {}).get("ok")
		return words["none"].format(**fill), (
			{"text": None, "command": f"wt launch {topic}", "button": "launch"} if ok else None
		)
	if phase == "unknown":
		files = [e["file"] for e in status["evidence"] if e["claim"] == "unknown"]
		todo = {"text": words["todo_unknown"].format(file=files[0] if files else "?"), "command": None, "button": None}
		return words["unknown"].format(error=status.get("error"), **fill), todo
	if phase == "launching":
		launched = next((e["value"] for e in status["evidence"] if e["field"] in ("launch.launched_at", "mtime")), None)
		return words["launching"].format(since=hhmm(launched), **fill), None
	if phase == "running":
		flow = status.get("flow") or {}
		if flow.get("kind") == "scripted" and flow.get("waiting"):
			waiting = ", ".join(w["agent_type"] or w["agent_id"] for w in flow["waiting"])
			line = words["running_scripted"].format(seq=flow.get("seq"), waiting=waiting, **fill)
		else:
			started = next((e["value"] for e in status["evidence"] if e["field"] == "process.started_at"), None)
			line = words["running"].format(
				since=hhmm(started),
				elapsed_minutes=minutesBetween(started, status["computed_at"]),
				cap_minutes=(status.get("estimate") or {}).get("cap_minutes") or "?",
				**fill,
			)
		return line, {"text": None, "command": f"wt stop {topic}", "button": "stop"}
	if phase == "finishing":
		step = status.get("step")
		if step:
			return words["finishing"].format(step=step["name"], since=hhmm(step.get("since")), **fill), None
		return words["finishing_claude"].format(since=hhmm((status.get("claude") or {}).get("ended_at")), **fill), None
	if phase == "orphan":
		confirmed = status["confidence"] == "confirmed"
		line = words["orphan_confirmed" if confirmed else "orphan_suspected"].format(
			why=orphanWhy(status, words), **fill
		)
		# resumableOf: wt resume closes a confirmed orphan and resumes, checks a suspected one's process first;
		# a done worker's unfinished wrap-up is finished by hand; refused (no or an old estimate after STATUS
		# done) it waits for the planning session.
		kept = status.get("resumable") or {}
		if kept.get("code") == "wrapup-left":
			wrapup = {"command": f"wt wrapup {topic}", "button": "wrapup"}
			if confirmed:
				line = words["orphan_wrapup"].format(steps=", ".join(kept["steps_left"]), **fill)
				return line, {"text": words["todo_orphan_wrapup"].format(**fill), **wrapup}
			return line, {"text": words["todo_orphan_wrapup_check"].format(**fill), **wrapup}
		if (status.get("resumable") or {}).get("ok") is False:
			return line, {"text": words["todo_orphan_no_resume"].format(**fill), "command": None, "button": None}
		text = words["todo_orphan_confirmed" if confirmed else "todo_orphan_suspected"].format(**fill)
		return line, {"text": text, **resume}
	outcome = status.get("outcome") or {}
	claude = status.get("claude") or {}
	if outcome.get("reason") == WRAPUP_FAILED:
		return wrapupFailedLine(status, words, fill, resume)
	kept = status.get("resumable") or {}
	if outcome.get("reason") == "stopped" and kept.get("code") == "wrapup-left":  # stopped during its wrap-up
		line = words["ended_stopped"].format(**fill) + words["steps_kept"].format(steps=", ".join(kept["steps_left"]))
		return line, {"text": words["todo_orphan_wrapup"].format(**fill), "command": f"wt wrapup {topic}",
			"button": "wrapup"}
	if outcome.get("merge") in ("merged", "already-merged"):
		return words["ended_merged"].format(merge=(outcome.get("commit") or outcome["merge"])[:12], **fill), None
	if status.get("merge"):
		todo = {"text": words["todo_merge"], "command": f"wt merge {topic}", "button": "merge"}
		if outcome.get("after_orphan"):
			todo["text"] = words["todo_merge_after_wrapup"].format(**fill)
			return words["ended_after_wrapup"].format(reason=status["merge"].get("reason"), **fill), todo
		return words["ended_refused"].format(reason=status["merge"].get("reason"), **fill), todo
	if outcome.get("reason") == "stopped":
		return words["ended_stopped"].format(**fill), None
	if claude.get("status") and not isWorkerDone(claude["status"]):
		return words["ended_status"].format(status=claude["status"], **fill), {"text": words["todo_resume"], **resume}
	if claude.get("status"):
		return words["ended_status"].format(status=claude["status"], **fill), None
	return words["ended_other"].format(reason=outcome.get("reason"), code=outcome.get("code"), **fill), None


def wrapupFailedLine(status, words, fill, resume):
	"""(line, todo) of a run that ended wrapup-failed: its steps left are finished with wt wrapup; without any (the
	worker didn't end done) a worker that asks something is resumed."""
	topic = status["topic"]
	kept = status.get("resumable") or {}
	line = words["ended_wrapup_failed"].format(error=(status.get("outcome") or {}).get("error"), **fill)
	if kept.get("code") == "wrapup-left":
		line += words["steps_kept"].format(steps=", ".join(kept["steps_left"]))
		todo = {"text": words["todo_orphan_wrapup"].format(**fill), "command": f"wt wrapup {topic}", "button": "wrapup"}
		return line, todo
	claude = status.get("claude") or {}
	if claude.get("status") and not isWorkerDone(claude["status"]):
		return line, {"text": words["todo_resume"], **resume}
	return line, None


def reportMinutes(step, limits):
	"""B1's threshold for a running step: step_running_report_factor of the time limit it started with
	(limit_seconds), or of its default (step_limit_default_seconds) when it recorded none; whole minutes, at least 1."""
	limitSeconds = step.get("limit_seconds")
	if not isNumber(limitSeconds) or limitSeconds <= 0:
		limitSeconds = limits["step_limit_default_seconds"][step["name"]]
	return max(1, int(limitSeconds * limits["step_running_report_factor"] // 60))


def longStep(status, settingsNow=None):
	"""B1: the wrap-up step a finishing run has been running for at least its report threshold (reportMinutes):
	{"name", "minutes", "limit_minutes"}, None otherwise. Report only: nothing reads it to act."""
	step = status.get("step") if status.get("phase") == "finishing" else None
	if not step:
		return None
	limit = reportMinutes(step, settingsNow or settings())
	minutes = minutesBetween(step.get("since"), status.get("computed_at"))
	if not isinstance(minutes, int) or minutes < limit:
		return None
	return {"name": step["name"], "minutes": minutes, "limit_minutes": limit}


def failedSteps(status):
	"""The record's wrap-up steps closed failed, in STEP_ORDER: [(name, reason)]."""
	steps = status.get("steps") or {}
	return [(name, (steps[name] or {}).get("reason")) for name in STEP_ORDER if (steps.get(name) or {}).get("state") == "failed"]


def human(status, lang=None):
	"""The person's view: {"schema", "topic", "run", "phase", "line", "todo": None | {"text", "command",
	"button"}}; button is one of launch, resume, stop, merge, wrapup or None. The line names a step running longer
	than its report threshold (longStep, report only) and every wrap-up step the record holds failed
	(failedSteps), whatever the phase. lang: a key of status_text, default
	the status_language setting."""
	words = texts(lang or settings()["status_language"])
	line, todo = lineAndTodo(status, words)
	slow = longStep(status)
	if slow:
		line += words["step_long"].format(step=slow["name"], minutes=slow["minutes"], limit=slow["limit_minutes"])
	failed = failedSteps(status)
	if failed:
		line += words["steps_failed"].format(steps="; ".join(f"{name}: {str(reason)[:120]}" for name, reason in failed))
	held = (status.get("held") or {}).get("state")
	if held and held != "clear":
		line += words["held"].format(held=held.upper())
		todo = {"text": words["todo_held"].format(held=held.upper()), "command": None, "button": None}
	return {
		"schema": SCHEMA,
		"topic": status["topic"],
		"run": status["run"],
		"phase": status["phase"],
		"line": line,
		"todo": todo,
	}


def yesNo(value):
	return "unknown" if value is None else "yes" if value else "no"


# -- detail: what a run's evidence files say (display only) -------------------------------------------

VERIFIER_FAIL = re.compile(r"^\s*V\d+\s+fail")


def evidenceFile(runs, name, run):
	"""(path, object or None, problem or None) of one evidence file of run; missing: (path, None, None)."""
	path = os.path.join(runs, f"{name}-{run}.json")
	data, error = readObject(path)
	return path, data, error


def resultDetail(runs, run, withText):
	"""The worker's claude -p output of run (kept verbatim, design S6), or None when there is none."""
	path, data, error = evidenceFile(runs, "result", run)
	if data is None:
		return {"file": path, "error": error} if error else None
	found = {
		"file": path,
		"num_turns": data.get("num_turns"),
		"is_error": data.get("is_error"),
		"terminal_reason": data.get("terminal_reason"),
		"total_cost_usd": data.get("total_cost_usd"),
		"permission_denials": len(data.get("permission_denials") or []),
	}
	if withText:
		found["text"] = str(data.get("result") or "").strip()
	return found


def verifyDetail(runs, run):
	path, data, error = evidenceFile(runs, "verify", run)
	if data is None:
		return {"file": path, "error": error} if error else None
	checks = data.get("checks") or []
	return {
		"file": path,
		"passed": data.get("passed"),
		"checks": len(checks),
		"failed": [{"name": c.get("name"), "why": c.get("why")} for c in checks if not c.get("ok")],
		"error": data.get("error"),
	}


def verifierDetail(runs, run):
	path, data, error = evidenceFile(runs, "verifier", run)
	if data is None:
		return {"file": path, "error": error} if error else None
	report = str(data.get("report") or "")
	return {
		"file": path,
		"verdict": data.get("verdict"),
		"failed": [line.strip() for line in report.splitlines() if VERIFIER_FAIL.match(line)],
		"why": data.get("why"),
	}


def mergeDetail(runs, run):
	path, data, error = evidenceFile(runs, "merge", run)
	if data is None:
		return {"file": path, "error": error} if error else None
	keys = ("decision", "into", "commit", "review", "reason", "merge_run", "fix_run")
	return {"file": path, **{k: data.get(k) for k in keys}}


def nextDetail(runs, run):
	path, data, error = evidenceFile(runs, "auto", run)
	if data is None:
		return {"file": path, "error": error} if error else None
	return {
		"file": path,
		"decision": data.get("decision"),
		"run": data.get("next_run"),
		"minutes": data.get("minutes"),
		"usd": data.get("usd"),
		"reason": data.get("reason"),
	}


def detail(status, text=True):
	"""What the status's run's evidence files say, for display (the gates decide from gateView; auto-merge.py takes
	only a fix run's findings from verifyDetail and verifierDetail, and asks for no fix run they don't name):
	{"schema", "run",
	"result" (with the worker's text unless text is False), "verify", "verifier", "merge", "next", "results":
	[one per run of the topic with a result file, without the text]}. A part is None when its file doesn't
	exist and {"file", "error"} when it can't be read; an absent value is None, never zero. Read-only."""
	runs, run = status.get("runs_dir"), status.get("run")
	found = {
		"schema": SCHEMA,
		"run": run,
		"results": [],
		**dict.fromkeys(("result", "verify", "verifier", "merge", "next")),
	}
	if not runs or run is None:
		return found
	found["result"] = resultDetail(runs, run, text)
	found["verify"] = verifyDetail(runs, run)
	found["verifier"] = verifierDetail(runs, run)
	found["merge"] = mergeDetail(runs, run)
	found["next"] = nextDetail(runs, run)
	records, legacy = runNumbersIn(runs)
	for n in sorted(records | legacy):
		summary = resultDetail(runs, n, False)
		if summary is not None:
			found["results"].append({"run": n, **summary})
	return found


def money(value):
	return f"${value:.2f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "$?"


def resultLine(part):
	if part.get("error"):
		return f"unreadable ({part['error']})"
	turns = part.get("num_turns")
	text = (
		f"{part.get('terminal_reason') or 'no terminal_reason'} · {'?' if turns is None else turns} turns"
		f" · {money(part.get('total_cost_usd'))}"
	)
	if part.get("permission_denials"):
		text += f" · {part['permission_denials']} denials"
	return text + (" · ERROR" if part.get("is_error") else "")


def verifyLine(part):
	if part.get("passed") is True:
		return f"passed ({part['checks']} checks)"
	if part.get("error"):
		return f"FAILED: {part['error']}"
	return "FAILED: " + (
		"; ".join(f"{c['name']}: {c['why']}" for c in part.get("failed") or []) or "no failed check named"
	)


def verifierLine(part):
	if part.get("verdict") == "passed":
		return "passed"
	if part.get("verdict") == "failed":
		return "FAILED: " + ("; ".join(part.get("failed") or []) or "no fail line in the report")[:500]
	return f"could not judge ({part.get('why')}): the verifier failed, not the branch"


def mergeLine(part):
	if part.get("decision") == "merged":
		text = f"merged into {part.get('into')} as {str(part.get('commit'))[:10]}"
		return text + (f" (review {part['review']})" if part.get("review") else "")
	flags = [k for k in ("merge_run", "fix_run") if part.get(k)]
	return f"{part.get('decision')}: {part.get('reason')}" + (f" ({', '.join(flags)})" if flags else "")


def nextLine(part):
	if part.get("decision") in ("resume", "merge", "fix"):
		return (
			f"automatic {part['decision']} run {part.get('run')} started "
			f"(estimate {part.get('minutes')} min, {money(part.get('usd'))})"
		)
	return f"{part.get('decision')}: {part.get('reason')}"


def detailLines(found):
	"""The detail section of agent(): one line per evidence file, every run's result line, the worker's text."""
	lines = ["", "detail:"]
	for name, render in (
		("result", resultLine),
		("verify", verifyLine),
		("verifier", verifierLine),
		("merge", mergeLine),
		("next", nextLine),
	):
		part = found.get(name)
		if part is None:
			lines.append(f"- {name}: absent")
		elif part.get("error"):
			lines.append(f"- {part['file']}: unreadable ({part['error']})")
		else:
			lines.append(f"- {part['file']}: {render(part)}")
	if found.get("results"):
		lines += ["runs:"] + [f"- run {r['run']}: {resultLine(r)}" for r in found["results"]]
	text = (found.get("result") or {}).get("text")
	if text:
		lines += [f"worker result (run {found['run']}):", text]
	return lines


def describeEvidence(entry):
	value = entry["value"] if entry["value"] is not None else "absent"
	if isinstance(value, (dict, list)):
		value = json.dumps(value, ensure_ascii=False)
	text = f"- {entry['file']}" + (
		f" {entry['field']} = {value}" if entry["field"] else f": {value}" if entry["value"] is not None else ""
	)
	if entry.get("age_seconds") is not None or "threshold_seconds" in entry:
		text += f" (age {entry.get('age_seconds')} s, threshold {entry.get('threshold_seconds')} s)"
	if entry.get("checked"):
		text += f" (pid check: {entry['checked']})"
	if entry.get("current"):
		text += f" (this boot: {entry['current']})"
	return text


def agent(status, found=None):
	"""KEY lines (hooks/RULES.md R1 shape) and the evidence, for an agent to quote instead of reading files;
	with found (detail(status)) also what the run's evidence files say."""
	view = human(status, "en")
	resumable = status.get("resumable") or {}
	resumeWord = yesNo(resumable.get("ok"))
	lines = [
		f"TOPIC: {status['topic']}",
		f"RUN: {status['run'] if status['run'] is not None else 'none'}",
		f"PHASE: {status['phase']}",
	]
	if status["phase"] == "orphan":
		lines += [f"ORPHAN-OF: {status['orphan_of']}", f"CONFIDENCE: {status['confidence']}"]
	lines += [
		f"PURPOSE: {status.get('purpose') or 'none'}",
		f"WORKER-STATUS: {(status.get('claude') or {}).get('status') or 'none'}",
		f"PID-CHECKED: {yesNo(status['pid_checked'])}",
		f"BUSY: {yesNo(status['busy'])}",
		f"RESUMABLE: {resumeWord}",
		f"MERGE-ALLOWED: {yesNo(status['merge_allowed'])}",
		f"SOURCE: {status.get('source') or 'none'}",
	]
	todo = view["todo"]
	if todo:
		lines.append(
			f"TODO: {todo.get('text') or todo.get('command')}"
			+ (f" ({todo['command']})" if todo.get("text") and todo.get("command") else "")
		)
	else:
		lines.append("TODO: none")
	lines += ["", view["line"]]
	if resumable.get("why"):
		lines.append(f"wt resume: {resumable['why']}")
	if resumable.get("command"):
		lines.append(f"wt wrapup starts: {resumable['command']}")
	lines += ["", "evidence:"] + [describeEvidence(e) for e in status["evidence"]]
	if status.get("merge_refusals"):
		lines += ["merge refusals:"] + [f"- {r}" for r in status["merge_refusals"]]
	if status.get("warnings"):
		lines.append("warnings: " + ", ".join(status["warnings"]))
	if found is not None:
		lines += detailLines(found)
	return "\n".join(lines)


# -- dual write (migration step 2): today's writers also write the record ------------------------------

DIVERGENCE = "run-divergence"
DIVERGED_EVENT = "status-diverged"
# What compareStories compares: the facts a reader acts on, never times or file names, which the two
# stories hold differently by construction (an mtime against the moment of the write).
COMPARED = (
	("phase",),
	("busy",),
	("claude", "outcome"),
	("claude", "status"),
	("outcome", "reason"),
	("outcome", "code"),
	("outcome", "merge"),
	("next", "decision"),
	("merge", "reason"),
	("merge_allowed",),
	("integration_mode",),
	("flow", "kind"),
	("estimate", "usd"),
	("estimate", "cap_minutes"),
)


def dualWrite(root, source, action, *args):
	"""Call action(root, *args) for one record write of migration step 2 (module doc); its result, or None when
	it failed (told once per distinct error, then the caller goes on as before)."""
	try:
		return action(root, *args)
	except Exception as error:
		text = f"{type(error).__name__}: {error}"
		try:
			first = hookio.recordFailureAt(root, source, error)
		except Exception as inner:
			first, text = True, f"{text}; recording that failed too ({type(inner).__name__}: {inner})"
		if first:
			print(f"{source}: the run record was not updated ({text}); today's files are unaffected", file=sys.stderr)
		return None


def recordExists(root, run):
	return os.path.exists(statefile.pathOf(root, RECORD, run))


def updateIfRecorded(root, run, change):
	"""update(), or None without writing when the run has no record file (module doc: before step 2, by hand)."""
	if not recordExists(root, run):
		return None
	return update(root, run, change)


def launchRecord(root, run, fields):
	"""The record of a run whose number today's allocator chose (worker-control next_run): written under the
	run's lock. A record of that number is replaced only when its wrapper never started, as est-<run>.json is
	when a launch is retried; a started one raises."""
	with statefile.locked(root, RECORD, run):
		old, _problem = readObject(statefile.pathOf(root, RECORD, run))
		if old and (old.get("process") or {}).get("started_at"):
			raise RecordError(f"runstate: run {run} already has a started record")
		record = {"schema": SCHEMA, **fields, "run": run}
		writeRecord(root, record)
	return record


def pidNamespace(pid="self", procDir="/proc"):
	"""The pid namespace a process lives in (the /proc/<pid>/ns/pid link, e.g. "pid:[4026531836]"), None when it
	can't be read. A process sees another's pid only from the same namespace (a sandboxed command has its own)."""
	try:
		return os.readlink(os.path.join(procDir, str(pid), "ns", "pid"))
	except OSError:
		return None


def processFacts(pid):
	"""The wrapper's process facts for verifyProcess: pid, process group, start time (stat field 22), boot id and
	pid namespace (pidNamespace); a fact that can't be read stays None."""
	facts = {"wrapper_pid": pid, "pgid": None, "start_ticks": None, "boot_id": readBootId(),
		"pid_ns": pidNamespace(pid)}
	try:
		facts["pgid"] = os.getpgid(pid)
	except OSError:
		pass
	try:
		with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
			facts["start_ticks"] = int(f.read().rsplit(")", 1)[1].split()[19])
	except (OSError, IndexError, ValueError):
		pass
	return facts


def recordSpawned(root, run, wrapperPid):
	"""worker-control started run's wrapper (it wrote the wt state's pid): the process facts, without
	started_at (the run reads launching until the wrapper says it started), so `wt stop` can check the wrapper
	(verifyProcess) from the moment it exists."""
	facts = processFacts(wrapperPid)
	return updateIfRecorded(root, run, lambda record: record.update(process={**(record.get("process") or {}), **facts}))


def recordStarted(root, run, wrapperPid):
	"""run-worker.sh started (it wrote start-<run>): the process facts and process.started_at."""
	facts = processFacts(wrapperPid)

	def change(record):
		record["process"] = {**(record.get("process") or {}), **facts, "started_at": homeClock.isoNow()}

	return updateIfRecorded(root, run, change)


def recordMainHead(root, run):
	"""run-worker.sh wrote main-<run> (local mode): the same head as main_head_at_start."""
	head = (readText(legacyPath(root, "main", run)) or "").strip() or None
	return updateIfRecorded(root, run, lambda record: record.update(main_head_at_start=head))


def recordInvocation(root, run, kind, file, startedAt, exitCode, baseUsd):
	"""One claude invocation of run-worker.sh: its kind, output file, times, exit code and what this run has
	spent by its end (the file's cumulative total_cost_usd minus the session's spend before the run). file is
	the name claude wrote to; a PHASE turn's result-<run>.json is renamed phase-<run>-<k>.json afterwards."""
	data, _error = readObject(os.path.join(runsDir(root), file))
	total = (data or {}).get("total_cost_usd")
	known = isinstance(total, (int, float)) and not isinstance(total, bool)
	cost = round(float(total) - float(baseUsd), 4) if known else None

	def change(record):
		claude = record.get("claude") or {}
		calls = claude.setdefault("invocations", [])
		calls.append(
			{
				"k": len(calls),
				"kind": kind,
				"file": file,
				"started_at": startedAt,
				"ended_at": homeClock.isoNow(),
				"exit_code": exitCode,
				"cost_usd": cost,
			}
		)
		record["claude"] = claude

	return updateIfRecorded(root, run, change)


def recordClaudeEnded(root, run, exitCode, stopped):
	"""claude's last invocation of the run ended: claude.ended_at, and the outcome and STATUS read from
	result-<run>.json exactly as the legacy adapter reads them (none without a result file); exit_code and
	stopped as the wrapper holds them, so a wrap-up run again later (run-steps.py wrapup without the wrapper)
	decides from the same facts."""
	ended = legacyClaude(root, run, {}, exitCode, stopped) or {}

	def change(record):
		claude = record.get("claude") or {"invocations": []}
		claude["ended_at"] = homeClock.isoNow()
		claude.update({key: ended.get(key) for key in ("outcome", "status", "header_file")})
		claude.update(exit_code=exitCode, stopped=bool(stopped))
		record["claude"] = claude

	return updateIfRecorded(root, run, change)


def recordStop(root, run, by):
	"""`wt stop` wrote stopped-<run>: stop.requested_at. The first stop is kept, never moved: its time against
	claude's end says whether the stop came during claude's part (stopAfterClaude, the merge gate), so a later stop
	(of a continued wrap-up) never changes it."""

	def change(record):
		if not record.get("stop"):
			record["stop"] = {"requested_at": homeClock.isoNow(), "by": by}

	return updateIfRecorded(root, run, change)


def recordDispatchStop(root, run, minutes, usd):
	"""dispatch-guard wrote auto-resume-<run>.json: what a resume needs."""
	stop = {"minutes": minutes, "usd": usd, "written_at": homeClock.isoNow()}
	return updateIfRecorded(root, run, lambda record: record.update(dispatch_stop=stop))


def clearDispatchStop(root, run):
	"""dispatch-guard removed auto-resume-<run>.json (a later dispatch fitted)."""
	return updateIfRecorded(root, run, lambda record: record.update(dispatch_stop=None))


def recordApprove(root, run, pid):
	"""`wt merge` wrote approve-<run>.json: the approving process (run-steps.py step merge --person)."""
	facts = processFacts(pid)
	approve = {"pid": pid, "start_ticks": facts["start_ticks"], "started_at": homeClock.isoNow()}
	return updateIfRecorded(root, run, lambda record: record.update(approve=approve))


def clearApprove(root, run):
	"""wt_controls.releaseApproval removed approve-<run>.json."""
	return updateIfRecorded(root, run, lambda record: record.update(approve=None))


def exitText(code):
	"""A program's exit code as people read it (negative: the signal that ended it)."""
	return f"signal {-code}" if code < 0 else f"exit {code}"


def queueTickOutcome(code, detail=""):
	"""The queue_tick step's outcome from `wt_queue.py tick`'s exit code: run-steps.py's step in the run record and
	auto-finish.py's steps.queue_tick in finish.json. code None: the tick never started; detail: what the tick
	said (auto-finish.py's last stderr line; run-steps.py's tick writes to the run's own log instead)."""
	if code == 0:
		return {"outcome": "ticked"}
	why = "not started" if code is None else exitText(code)
	return {"outcome": "error", "reason": f"{why}: {detail}" if detail else why}


def recordStep(root, run, name, step):
	"""A wrap-up step's result (verify.py, verifier.py, auto-merge.py, worker-control.py auto*, run-steps.py):
	steps.<name>, state done unless given, ended_at now. The step's bookkeeping (STEP_BOOKKEEPING: its attempt,
	runner and start, written by startStep) is kept; every other field of an earlier attempt is replaced."""
	if name not in STEP_ORDER:
		raise RecordError(f"runstate: {name!r} is not a step")
	body = {"state": "done", **step, "ended_at": homeClock.isoNow()}

	def change(record):
		steps = record.get("steps") or {}
		kept = {key: value for key, value in (steps.get(name) or {}).items() if key in STEP_BOOKKEEPING}
		record["steps"] = {**steps, name: {**kept, **body}}

	return updateIfRecorded(root, run, change)


def startStep(root, run, name, runnerPid, attemptsMax, runnerAlive, force=False, again=(), runnerTicks=None,
		limitSeconds=None):
	"""Claim wrap-up step name of run for the runner runnerPid (run-steps.py, migration step 6), under the record's
	lock: {"attempt": n} when it may run now (state running, attempt n, runner_pid, runner_start_ticks: the
	runner's start time, runnerTicks; started_at; limit_seconds: the step's time limit, limitSeconds; the fields of an earlier attempt stay until its program writes
	the outcome, so a refused merge being approved still reads refused), else {"skip": why}. Never runs: a step
	running under a live runner (runnerAlive(step): the step as recorded, its runner_pid and runner_start_ticks),
	and unless force (the person's merge approval) a step done, skipped or abandoned (a state in again excepted: a skip
	run-steps.py evaluates again because an earlier step ran again), or one whose attempts reached attemptsMax (a
	running one whose runner is gone is then closed failed). A step running under a dead runner, or failed, runs
	again as attempt n+1. Raises RecordError for a run without a usable record."""
	if name not in STEP_ORDER:
		raise RecordError(f"runstate: {name!r} is not a step")
	decided = {}

	def change(record):
		steps = record.get("steps") or {}
		old = steps.get(name) or {}
		state, attempt = old.get("state"), old.get("attempt") if isNumber(old.get("attempt")) else 0
		if state == "running" and runnerAlive(old):
			decided["skip"] = f"running under runner {old.get('runner_pid')}"
			return
		if not force and state in STEP_FINAL and state not in again:
			decided["skip"] = state
			return
		if not force and attempt >= attemptsMax:
			decided["skip"] = f"{attempt} attempt(s) used (step_attempts_max {attemptsMax})"
			if state == "running":
				steps[name] = {
					**{k: v for k, v in old.items() if k in STEP_BOOKKEEPING},
					"state": "failed",
					"reason": f"its runner {old.get('runner_pid')} is gone and no attempt is left",
					"ended_at": homeClock.isoNow(),
				}
				record["steps"] = steps
			return
		steps[name] = {
			**old,
			"state": "running",
			"attempt": attempt + 1,
			"runner_pid": runnerPid,
			"runner_start_ticks": runnerTicks,
			"started_at": homeClock.isoNow(),
			"limit_seconds": limitSeconds,
		}
		record["steps"] = steps
		decided["attempt"] = attempt + 1

	update(root, run, change)
	return decided


def recordStepFromFile(root, run, name):
	"""A wrap-up step whose program just wrote its file of today (verify-, verifier-, merge-, auto-<run>.json):
	the step read back from that file exactly as the legacy adapter reads it, so both stories use one mapping."""
	readers = {
		"verify": lambda: legacyVerifyStep(root, run, None),
		"verifier": lambda: legacyVerifierStep(root, run, None),
		"merge": lambda: legacyMergeStep(root, run),
		"next": lambda: legacyNextStep(root, run),
	}
	if name not in readers:
		raise RecordError(f"runstate: step {name!r} has no file of today's layout")
	step = readers[name]()
	if step is None:
		raise RecordError(f"runstate: run {run} has no {name} file to record")
	return recordStep(root, run, name, step)


def skippedSteps(steps, spec):
	"""The verify and verifier steps a run skips for its spec, as the legacy adapter reads them: no spec, or a
	spec without a verifier section. A step that ran is kept."""
	found = {}
	if "verify" not in steps and spec == "absent":
		found["verify"] = {"state": "skipped", "skip_reason": "no-spec"}
	if "verifier" not in steps and spec == "absent":
		found["verifier"] = {"state": "skipped", "skip_reason": "no-spec"}
	elif "verifier" not in steps and isinstance(spec, dict) and "verifier" not in spec:
		found["verifier"] = {"state": "skipped", "skip_reason": "no-verifier-section"}
	return found


NOTICES = ("heartbeat_stale", "at_limit", "no_limit")


def recordNotice(root, run, name):
	"""Remember under the record's lock that notice name (NOTICES) was told for run: True the first time, False
	when it was told already (so two callers at once tell it once). Writes notices.<name>_at only: no step, no
	exit, no process fact changes. RecordError for a run without a usable record."""
	if name not in NOTICES:
		raise RecordError(f"runstate: {name!r} is not a notice")
	first = {}

	def change(record):
		notices = record.get("notices") if isinstance(record.get("notices"), dict) else {}
		if notices.get(f"{name}_at"):
			return
		record["notices"] = {**notices, f"{name}_at": homeClock.isoNow()}
		first["new"] = True

	update(root, run, change)
	return bool(first)


def unstartedNext(record):
	"""The ended run's next step when it decided a run (NEXT_RUNS) and is done, else None."""
	step = (record.get("steps") or {}).get("next") or {}
	return step if step.get("state") == "done" and step.get("outcome") in NEXT_RUNS else None


def claimNextStart(root, run, attemptsMax):
	"""Count one more start of the next run this run's next step decided (the reconciler's next-unstarted), under
	the record's lock: {"attempt": n} when one may start (steps.next.start_attempts is now n), {"limit": n} when
	attemptsMax starts were made already, {"gone": why} when the next step no longer holds an unstarted decision."""
	decided = {}

	def change(record):
		step = unstartedNext(record)
		if step is None:
			decided["gone"] = "the next step holds no decided run any more"
			return
		used = step.get("start_attempts") if isNumber(step.get("start_attempts")) else 0
		if used >= attemptsMax:
			decided["limit"] = used
			return
		record["steps"]["next"] = {**step, "start_attempts": used + 1}
		decided["attempt"] = used + 1

	update(root, run, change)
	return decided


def recordStepProgram(root, run, name, pid, ticks):
	"""The program a running step started (run-steps.py callProgram): steps.<name>.program_pid and
	program_start_ticks, so a reconciler sees it run even after its runner was killed (reconciler.programAlive).
	Nothing for a step not running."""
	def change(record):
		step = (record.get("steps") or {}).get(name)
		if isinstance(step, dict) and step.get("state") == "running":
			step.update(program_pid=pid, program_start_ticks=ticks)

	return updateIfRecorded(root, run, change)


def reconcileStarts(record):
	"""How often the reconciler started this run's wrap-up (reconcile.wrapup_starts)."""
	starts = ((record or {}).get("reconcile") or {}).get("wrapup_starts")
	return starts if isNumber(starts) else 0


def claimReconcileStart(root, run, attemptsMax):
	"""Count one more wrap-up start by the reconciler; the caller holds the record's lock (the reconciler judges
	again and starts under it): {"attempt": n}, or {"limit": n} when attemptsMax starts were made already (the run
	is then at-limit)."""
	decided = {}

	def change(record):
		used = reconcileStarts(record)
		if used >= attemptsMax:
			decided["limit"] = used
			return
		reconcile = record.get("reconcile") if isinstance(record.get("reconcile"), dict) else {}
		record["reconcile"] = {**reconcile, "wrapup_starts": used + 1}
		decided["attempt"] = used + 1

	updateHeld(root, run, change)
	return decided


def closeNextUnstarted(root, run, reason):
	"""End the next step of a run whose decided next run never started after every allowed start: failed with the
	reason (its outcome dropped, so the decision no longer holds the topic busy), under the record's lock. True when
	it closed it, False when the step no longer held an unstarted decision."""
	closed = {}

	def change(record):
		step = unstartedNext(record)
		if step is None:
			return
		kept = {key: value for key, value in step.items() if key in STEP_BOOKKEEPING}
		record["steps"]["next"] = {**kept, "state": "failed", "reason": reason[:300], "ended_at": homeClock.isoNow()}
		closed["yes"] = True

	update(root, run, change)
	return bool(closed)


def recordWrapperExit(root, run, status, signal=None, reason=None, code=None, mainDir=None):
	"""run-worker.sh's EXIT trap: process.exited_at, exit_code (the shell's status) and exit_signal (the last INT
	or TERM it got) always; the run's exit {reason, code} when the wrapper reached one of its ends (reason
	given) and the record has none yet, with the skipped verify steps, in the same write. Without a reason
	(a signal killed the shell) exit stays absent: the run reads as an orphan, never as completed."""
	written = {}

	def change(record):
		record["process"] = {
			**(record.get("process") or {}),
			"exited_at": homeClock.isoNow(),
			"exit_code": status,
			"exit_signal": signal,
		}
		if reason is not None and endRecord(record, reason, code, mainDir):
			written["exit"] = record["exit"]

	record = updateIfRecorded(root, run, change)
	if written:
		eventlog.emit(root, "run-ended", SOURCE, {"reason": reason, "code": code}, topic=record["topic"], run=run)
	return record


def endRecord(record, reason, code, mainDir):
	"""Give record its exit {reason, code} with the verify steps its spec skips, in place; False (nothing
	changed) when it has an exit already, unless that exit is wrapup-failed and this one isn't: the wrap-up run
	again ends the run for real."""
	end = record.get("exit")
	if end and not (end.get("reason") == WRAPUP_FAILED and reason != WRAPUP_FAILED):
		return False
	steps = record.get("steps") or {}
	record["steps"] = {**steps, **skippedSteps(steps, verifySpec(mainDir, record["topic"]))}
	record["exit"] = {"ended_at": homeClock.isoNow(), "code": code, "reason": reason}
	return True


def recordRunEnded(root, run, reason, code, mainDir=None):
	"""run-steps.py wrapup wrote exit-<run> at the end of the wrap-up: the run's exit, in the same moment, so every
	reader of status() sees the run ended exactly when today's files say so (the queue tick right after it,
	auto-finish); the EXIT trap later adds only the wrapper's own exit (recordWrapperExit)."""
	written = {}

	def change(record):
		if endRecord(record, reason, code, mainDir):
			written["exit"] = record["exit"]

	record = updateIfRecorded(root, run, change)
	if written:
		eventlog.emit(root, "run-ended", SOURCE, {"reason": reason, "code": code}, topic=record["topic"], run=run)
	return record


def recordLaunchFailed(root, run, error):
	"""worker-control could not start the run's wrapper (Popen failed): the run ends launch-failed, with the
	error, so it never reads as launching; today's files have no exit for it (the launch error goes on)."""
	written = {}

	def change(record):
		if record.get("exit"):
			return
		record["exit"] = {"ended_at": homeClock.isoNow(), "code": None, "reason": "launch-failed", "error": error}
		written["exit"] = record["exit"]

	record = updateIfRecorded(root, run, change)
	if written:
		eventlog.emit(
			root, "run-ended", SOURCE, {"reason": "launch-failed", "code": None}, topic=record["topic"], run=run
		)
	return record


def recordWrapupFailed(root, run, code, error):
	"""run-steps.py's wrap-up crashed outside its steps: the run ends wrapup-failed with claude's exit code and the
	error, so it never reads as an orphan; its steps left stay owed (wrapupLeft) and a wrap-up run again replaces
	this exit (endRecord). An exit already written is kept."""
	written = {}

	def change(record):
		if record.get("exit"):
			return
		record["exit"] = {"ended_at": homeClock.isoNow(), "code": code, "reason": WRAPUP_FAILED, "error": error}
		written["exit"] = record["exit"]

	record = updateIfRecorded(root, run, change)
	if written:
		eventlog.emit(root, "run-ended", SOURCE, {"reason": WRAPUP_FAILED, "code": code}, topic=record["topic"], run=run)
	return record


def valueAt(result, path):
	value = result
	for key in path:
		value = value.get(key) if isinstance(value, dict) else None
	return value


def compareStories(mainDir, topic, run, *, clock=None, bootId=None):
	"""The differences between the status the run's record gives and the one today's files give, for one run
	([] = the same story); read-only. Both are computed at the same moment. A run without a record is one
	difference (field "record")."""
	moment = (clock or homeClock.now)()
	fixed = lambda: moment  # noqa: E731
	fromRecord = status(mainDir, topic, clock=fixed, bootId=bootId, run=run)
	fromFiles = status(mainDir, topic, clock=fixed, bootId=bootId, run=run, fromLegacy=True)
	if fromRecord.get("source") != "record":
		return [{"field": "record", "record": None, "legacy": fromFiles.get("source")}]
	differences = []
	for path in COMPARED:
		mine, theirs = valueAt(fromRecord, path), valueAt(fromFiles, path)
		if mine != theirs:
			differences.append({"field": ".".join(path), "record": mine, "legacy": theirs})
	return differences


def fingerprintOf(difference):
	return hashlib.sha256(json.dumps(difference, sort_keys=True, default=str).encode()).hexdigest()[:16]


def reportDivergence(root, topic, run, differences):
	"""Write each difference not reported before for this run as one status-diverged event (run-divergence
	state, under its lock); returns the ones written. The same difference again stays quiet."""
	written = []
	with statefile.locked(root, DIVERGENCE, run):
		seen = statefile.read(root, DIVERGENCE, run) or {"schema": SCHEMA, "fingerprints": []}
		try:
			for difference in differences:
				mark = fingerprintOf(difference)
				if mark in seen["fingerprints"]:
					continue
				eventlog.emit(root, DIVERGED_EVENT, SOURCE, difference, topic=topic, run=run)
				seen["fingerprints"].append(mark)
				written.append(difference)
		finally:
			if written:
				seen["last_at"] = homeClock.isoNow()
				statefile.write(root, DIVERGENCE, seen, run)
	return written


# -- gates (migration step 5) ------------------------------------------------------------------------

STORIES = ("record", "legacy")


def gateView(mainDir, topic, run, *, root, clock=None, bootId=None):
	"""Both stories of run for a gate (module doc): {"record": status from the record, "legacy": status from
	today's files, "spec": the verify spec as verifySpec reads it, "blocks": [{fact, reason, story, outcome?}]},
	every merge block of either story, a block when the run has no usable record (T-1: none, an unknown schema, a
	missing field, a value outside its enum, a time without an offset) and one when the two stories hold different
	main heads. Read-only, both computed at one moment, no pid check (a stale heartbeat reads as an orphan here)."""
	moment = (clock or homeClock.now)()
	fixed = lambda: moment  # noqa: E731
	stories = {
		"record": status(mainDir, topic, root=root, clock=fixed, bootId=bootId, run=run),
		"legacy": status(mainDir, topic, root=root, clock=fixed, bootId=bootId, run=run, fromLegacy=True),
	}
	blocks = []
	if stories["record"]["source"] != "record":
		path = statefile.pathOf(str(root), RECORD, run)
		blocks.append(block("record", f"run {run} has no run record ({path})", story="record"))
	for name in STORIES:
		blocks += [{**found, "story": name} for found in stories[name]["merge_blocks"]]
	heads = [stories[name]["main_head_at_start"] for name in STORIES]
	if heads[0] != heads[1]:
		reason = f"the record and today's files hold different main heads at the start ({heads[0]!r}, {heads[1]!r})"
		blocks.append(block("main_head", reason, story="both"))
	return {**stories, "spec": verifySpec(mainDir, topic), "blocks": blocks}

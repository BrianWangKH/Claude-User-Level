#!/usr/bin/env python3
"""run-worker.sh's steps (hooks/lib/runstate.py): every decision the wrapper makes about its run, and every fact it
hands to the run record. Migration step 2 (dual write): each fact goes into the record next to today's runs/
files. Migration step 6: the claude calls' order (PHASE turns, compactions, the budget left) and the whole wrap-up
(verify, verifier, merge, next run, finish, queue tick) are decided here, each wrap-up step idempotent and safe to
run again; run-worker.sh only starts claude as told, keeps the heartbeat and traps its own exit.

Usage: run-steps.py <command> <worktree> <run> [args]
  started <pid>                       the wrapper started (it wrote start-<run>; its INT/TERM traps are set): process
                                      facts, started_at; prints `stopped` when the record already holds a `wt stop`
                                      (it came before the start was recorded, so worker-control.py sent no signal:
                                      the wrapper ends the run stopped)
  main-head                           it wrote main-<run>: main_head_at_start
  beat                                one heartbeat (runs/<run>-heartbeat.json), the interval from the settings
  next-invocation --topic T --mode new|resume --session S --usd U --agent A --prompt P --max-phases N
                  --after KIND --code C --last C --stopped 0|1
                                      the wrapper's next claude call, printed as shell words for
                                      `eval "spec=($out)"`: `invoke <run code> <kind> <file> <compact 1|''> <base usd>
                                      <claude args...>` or `done <run code>`. --after: the kind of the call just
                                      made ('' before the first), --last its exit code, --code the run's code so far
                                      (the last main or continue call's). A turn whose header starts `PHASE:` is
                                      renamed phase-<run>-<k>.json and followed by a /compact (compact-<run>-<k>.json)
                                      and a continue prompt (the scripted flow's when the run's flow is scripted), at
                                      most --max-phases times; after a `STATUS:` turn one final /compact. Each call
                                      gets what is left of --usd for the run (total_cost_usd is cumulative for the
                                      session); with less than home.json budget worker_call_budget_min_usd left a call counts as made with exit code 99
                                      (a line in the stderr log), never started. No further call after a stop or an
                                      error, but a run stopped right after a PHASE turn keeps that turn as its result.
                                      A call the watcher cut after its final STATUS turn (run_ending.isCut on the
                                      output: its result leads with a STATUS line) ends the claude part with run code 0
                                      and that turn as the result, without the final compact (its total_cost_usd is
                                      unknown); a stopped run reads stopped with claude's exit code, as before
  invocation <kind> <file> <started_at> <code> <base_usd>
                                      one claude invocation ended (kind main, continue, compact, final-compact); when
                                      the watcher cut it (run_ending.cutOf: this file and start), its output file is
                                      first rewritten by run_ending.cutOutput
  watch <pid> <start_ticks> <file> <started_at>
                                      run-worker.sh's watcher of one worker call (main or continue; claude's pid and
                                      start time as the wrapper read them right after starting it, its output file
                                      name, its start). A pid whose start time differs (or start_ticks empty) is
                                      never signalled: the watcher ends at once. While that process lives, every
                                      ending_poll_seconds (budget run_state) it reads the run's newest Stop
                                      (run_ending.finalTurn); once a final STATUS turn of this call has been followed
                                      by ending_idle_max_seconds of waiting, it writes the cut record, sends TERM, and
                                      KILL after ending_kill_grace_seconds. A pause or a PHASE turn never counts, so
                                      every mid-run wait (a background reviewer, work pending at a phase end) is
                                      claude's own
  claude-ended <code> <stopped 0|1> [--topic T]
                                      the run's last claude invocation ended; with --topic also the person's event
                                      (wt_events.py emit run-ended, when WT_MAIN_DIR is set)
  wrapup [--topic T] [--code C --stopped 0|1] [--continue-stopped]
                                      the wrap-up after claude's end, in order: verify, verifier, merge, next, finish,
                                      then exit-<run> and the record's exit (stopped or completed), then queue_tick.
                                      Without --code/--stopped (run again by hand, by `wt wrapup` or the reconciler)
                                      the record decides: claude.exit_code, and a run that was stopped
                                      (runstate.stoppedRun: wt stop, claude's part stopped, an exit stopped, a step
                                      skipped as stopped) skips as stopped. --continue-stopped (only `wt wrapup`
                                      passes it): a run stopped only during its wrap-up (runstate.stopDuringWrapup:
                                      claude's part ended unstopped) is not stopped for this wrap-up, so its steps
                                      skipped as stopped are evaluated again and its failed ones retried; a stop of
                                      claude's part still skips. The merge step asks the topic's flow mode
                                      (homeconfig.flowMode, checked against the record's integration_mode: a
                                      difference, or a mode that can't be read, fails the step). After the queue
                                      tick it starts the reconciler for the repository's other worktrees
                                      (reconciler.startReconcile --exclude this worktree, detached, its output in
                                      this run's stderr log)
  step <name> [--topic T] [--person]  one wrap-up step by the same rules. `step merge --person` is `wt merge`
                                      (worker-control.py): auto-merge.py --person as a new attempt of the merge step,
                                      then the next and finish steps again (unless they already started a run or
                                      auto-finish), then the run's approval record is released
  ended --reason R --code C           the run's exit in the record
  reconcile <worktree> | --all  [--exclude W ...] [--dry-run]   (no run argument)
                                      the reconciler (reconciler.py): each worktree's latest run in one class and
                                      the action that finishes it (a wrap-up, a next run's start, the person told
                                      at the limit), taken only when every process check says gone; --dry-run
                                      prints the actions and takes none. The worktree must be one of the main
                                      checkout's (WT_MAIN_DIR, else the repository it is in); --all: every worktree
                                      of that main checkout but the --exclude ones. A crash is a stderr line and a
                                      hook-failed event in the main checkout (WT_MAIN_DIR), exit 1
  wrapper-exit <status> [--reason R --code C] [--signal S]
                                      the wrapper's EXIT trap: process exit, the run's exit when it reached one of
                                      its ends (reason) and the wrap-up didn't write it already (an early end: not
                                      logged in, a config error), then the record's story compared with today's
                                      files (status-diverged once per difference; needs the main checkout,
                                      WT_MAIN_DIR through homeconfig.env)
Wrap-up steps (runstate.STEP_ORDER), each recorded as steps.<name> {state, attempt, runner_pid, started_at,
outcome | skip_reason, reason}: a step is claimed first (runstate.startStep, under the record's lock), so a step
done never runs twice, a step running under a live runner is left to it (reconciler.runnerAlive: its pid runs
run-steps.py naming this worktree and run, started at the recorded runner_start_ticks), and a step whose runner died or that
failed runs again as the next attempt, up to step_attempts_max (budget run_state). No step is claimed while a step
before it in that order still runs under another live runner or its own program (reconciler.heldSteps: a verify
whose runner was killed outright keeps running in its own session): the runner waits for it, polling every
status_wait_poll_seconds, at most until that step's time limit (its recorded limit_seconds, else its
step_limit_default_seconds) and step_kill_grace_seconds have passed since it started, then goes on (a stderr
line); a stop of this run ends the wait. So a wrap-up continued by `wt wrapup` or the reconciler, and the person's
merge, decide on the outcome of the steps before them, never on a step still running. A skipped step started
nothing, so it is evaluated again when a step before it ran again in the same wrap-up (a merge that failed and now
merged: the finish skipped as not merged follows) and when `step <name>` names it; otherwise it stays. verify.py,
verifier.py, auto-merge.py and worker-control.py write their own outcome; a program that exits while its step is
still running leaves it failed, reason "exited without an outcome (exit N | signal N)", unless it exited 0 having
written its file of today (then the step is read from that file) or found nothing to do (verify: no spec; verifier:
no verifier section, verify not passed). The rules each step runs by (the wrapper's before step 6):
  verify, verifier  claude's run ended `STATUS: done` (the first line of result-<run>.json starts so), not stopped,
                    exit 0, and <main>/plans/verify/<topic>.json exists; verifier.py decides itself whether its
                    section and the first layer let it start. A verifier that could not judge (its ERROR) leaves
                    the step failed, retried by the next wrap-up up to step_attempts_max; PASS and FAIL are final
  merge             local mode, `STATUS: done`, not stopped, exit 0: auto-merge.py; it only decides and writes
                    merge-<run>.json and steps.merge (flags merge_run, fix_run). Skipped (verifier-retry) while the
                    verifier step is failed with attempts left, so the merge decides once the verifier has judged
                    (the retry runs it again, a skip being evaluated again after an earlier step ran)
  next              merge-<run>.json asks for a merge run or a fix run: worker-control.py auto-merge or auto-fix;
                    else a run that ended `STATUS: blocked` (not stopped, exit 0) with dispatch-guard's
                    auto-resume-<run>.json: worker-control.py auto. Never a second time once a decision that starts a
                    run (auto-<run>.json resume, merge or fix) or a run record started by this run (launch.by_run)
                    exists
  finish            merge-<run>.json says merged: auto-finish.py detached (its own session, its output appended to
                    the run's stderr-<run>.log), outcome launched; not when the main checkout's archive already
                    holds this run's finish.json saying the worktree was removed (a failed one is retried, its
                    sub-actions each done at most once: auto-finish.py), nor while an auto-finish.py of this run
                    still runs (reconciler.liveHelpers; it writes its own outcome, a failure back to this step; processes that
                    can't be listed fail the step)
  queue_tick        the main checkout is known: wt_queue.py tick, after the run's exit is written; a failed tick is
                    the person's queue-failed event
A stop (INT or TERM) during the wrap-up is a stop of the run: the steps after it that need a run not stopped skip,
the next and finish steps still act on a merge decision already made, and the exit reads stopped. A step's program
runs in its own session (callProgram), so the stop is forwarded to its process group as it came. Such a run keeps
its wrap-up for the person (runstate.wrapupLeft): `wt wrapup` continues it (--continue-stopped), each step at most
once more as the claims allow (a done step never again, a failed one within step_attempts_max); automatic triggers
never do (the reconciler classes a stopped run nothing), and the exit stays stopped. A continued wrap-up that
leaves steps owed with every attempt used tells the person once per run (step-at-limit, reconciler.tellLimitOnce).
Time limits (worker self-repair 3c): each step started records its limit (step_history.stepLimit: the median of
the step's recent durations in this repository x step_limit_factor, or the step's default with fewer than
step_limit_min_samples_count of them). verify, next and queue_tick: a program still running at its limit is ended
(its process group: TERM, KILL after step_kill_grace_seconds; never run-steps.py itself) and the step is failed
("exceeded <n> min, from <basis>") with the person's step-failed event, retried like any failed step; never a
success. The verifier keeps its own timeout (the limit only reports, B1). The merge is never ended (a merge cut short
can leave the main checkout half merged): at its limit the person's event merge-slow, once, and the wait goes on.
A step this runner closes failed (its program exited without an outcome, or run-steps.py's own action raised) is
also the person's step-failed event (wt_events.py, in the main checkout). A wrap-up that crashes outside its steps
ends the run wrapup-failed (runstate.recordWrapupFailed, with the error: not an orphan) and is the person's
wrapup-failed event (which also says so when that record write failed: the run then reads as an orphan); its steps left stay owed, and the wrap-up run again (`wt wrapup`) continues and writes the
run's real end over it. Any other command that crashes is a stderr line and a hook-failed event (once per distinct
error), for `step` also step-failed.
The follow-up programs are the person's home hooks (~/.claude/hooks), as the wrapper always called them. A run whose
record ended otherwise (orphan-closed, launch-failed, not-logged-in, config-error, superseded) runs no wrap-up step.
A run whose record file exists but can't be read (or the record layer can't load) runs no step either: wrapup and
step refuse with a stderr line and the person's wrapup-failed event, exit 1; only a run without a record file (one
started before the record, or by hand) runs its steps once each without bookkeeping.
Reads: the run's record and today's files in the worktree's runs component, the main checkout's verify spec, the
merge decision files, the archive's finish.json.
Writes: the record (runstate, under its lock), the heartbeat, today's exit-<run> and the phase-/result- files of the
claude calls (a PHASE turn renamed, a stopped run's result, a cut call's output rewritten), the cut record
(run_ending), the run-divergence state and events in the worktree's
stream (run-ended, status-diverged, hook-failed), the person's events in the main checkout (wt_events.py:
step-failed, wrapup-failed, queue-failed); the programs it starts write their own.
Output: next-invocation prints the next call; the others nothing on stdout. A failed record write is one stderr
line, once per distinct error; so is a cut call whose output can't be rewritten (keepCutTurn: what was lost and
where the final message is). Exit 0, also when a record write or a step failed (the wrapper's run never depends
on the record; a failed step is in the record); 1: the command crashed (told as above); 2: usage, or a wrap-up
whose exit code is unknown.
Fails: open — the wrapper's run never waits on the record: a record write that fails is a hook-failed event, told
once, and the steps then run as the wrapper ran them (each once, no bookkeeping); a wrap-up step that fails is
recorded failed and the later steps still run.
"""

import argparse
import contextlib
import datetime
import glob
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import homeconfig  # noqa: E402
import hookio  # noqa: E402
import hookonly  # noqa: E402
import report_header  # noqa: E402
import run_ending  # noqa: E402
import reconciler  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
import step_history  # noqa: E402

SOURCE = "run-steps"
# Today's files of a run live in this component of the worktree's state directory: the wrapper's fixed layout
# (the record's component is the same directory, config/registry/state-registry.json), so the claude calls and the wrap-up
# never depend on the record layer loading.
RUNS = "runs"
HOME_HOOKS = Path.home() / ".claude" / "hooks"  # the follow-up programs, as run-worker.sh always called them
SCRIPTED_RULES = "~/.claude/skills/feature-flow/scripted.md"
COMPACT_PROMPT = (
	"/compact Keep: the topic and plans/{topic}.md, the branch, the phase just finished and the next step, commit "
	"hashes, agent ids and open decisions. Drop subagent reports, tool output and file contents: the plan, git and "
	"the files hold them."
)
CONTINUE_PROMPT = (
	"The conversation was compacted at a phase boundary. Re-read plans/{topic}.md, load the class step files again "
	"(feature-flow, Mode: one cat) and continue with the next phase."
)
SCRIPTED_CONTINUE_PROMPT = (
	"The conversation was compacted at a phase boundary. Load your rules with one Bash call, "
	f"`cat {SCRIPTED_RULES}`, then Read {hookonly.NAME}/flow/next.md and do what it says."
)
BUDGET_EXIT_CODE = runstate.BUDGET_EXIT_CODE
WORKER_DONE = "done"
WORKER_BLOCKED = "blocked"
LAUNCHED = "launched"
WRAPUP_STEPS = ("verify", "verifier", "merge", "next", "finish")
WRAPUP_ENDS = ("completed", "stopped", runstate.WRAPUP_FAILED)
DECISION_FILES = {"verify": "verify", "verifier": "verifier", "merge": "merge", "next": "auto"}
STOP_SIGNALS = {signal.SIGINT: "INT", signal.SIGTERM: "TERM"}
# Steps whose program is ended at its time limit; the merge only tells (merge-slow), the verifier has its own.
ENDED_AT_LIMIT = ("verify", "next", "queue_tick")
CHILD = {"process": None}  # the step program running now (callProgram), so a stop reaches its own session
COMMANDS = (
	"started",
	"main-head",
	"beat",
	"next-invocation",
	"invocation",
	"claude-ended",
	"wrapup",
	"step",
	"ended",
	"wrapper-exit",
	"watch",
)


class StepError(Exception):
	pass


def parser():
	top = argparse.ArgumentParser(description="The worker run's steps and its facts in the run record.")
	commands = top.add_subparsers(dest="command", required=True)
	reconcile = commands.add_parser("reconcile")
	reconcile.add_argument("worktree", nargs="?")
	reconcile.add_argument("--all", action="store_true", dest="every")
	reconcile.add_argument("--dry-run", action="store_true", dest="dry_run")
	reconcile.add_argument("--exclude", action="append", default=[])
	for name in COMMANDS:
		sub = commands.add_parser(name)
		sub.add_argument("worktree")
		sub.add_argument("run", type=int)
		addArguments(name, sub)
	return top


def addArguments(name, sub):
	if name == "started":
		sub.add_argument("pid", type=int)
	elif name == "next-invocation":
		for option in ("--topic", "--mode", "--session", "--agent", "--prompt", "--after"):
			sub.add_argument(option, required=True)
		sub.add_argument("--usd", type=float, required=True)
		sub.add_argument("--max-phases", type=int, required=True)
		for option in ("--code", "--last"):
			sub.add_argument(option, type=int, required=True)
		sub.add_argument("--stopped", choices=("0", "1"), required=True)
	elif name == "invocation":
		sub.add_argument("kind", choices=runstate.INVOCATION_KINDS)
		sub.add_argument("file")
		sub.add_argument("started_at")
		sub.add_argument("code", type=int)
		sub.add_argument("base_usd", type=float)
	elif name == "claude-ended":
		sub.add_argument("code", type=int)
		sub.add_argument("stopped", choices=("0", "1"))
		sub.add_argument("--topic")
	elif name == "wrapup":
		sub.add_argument("--topic")
		sub.add_argument("--code", type=int)
		sub.add_argument("--stopped", choices=("0", "1"))
		sub.add_argument("--continue-stopped", action="store_true")
	elif name == "step":
		sub.add_argument("name", choices=runstate.STEP_ORDER)
		sub.add_argument("--topic")
		sub.add_argument("--person", action="store_true")
	elif name == "ended":
		sub.add_argument("--reason", choices=runstate.EXIT_REASONS, required=True)
		sub.add_argument("--code", type=int, required=True)
	elif name == "wrapper-exit":
		sub.add_argument("status", type=int)
		sub.add_argument("--reason", choices=runstate.EXIT_REASONS)
		sub.add_argument("--code", type=int)
		sub.add_argument("--signal")
	elif name == "watch":
		sub.add_argument("pid", type=int)
		sub.add_argument("start_ticks")
		sub.add_argument("file")
		sub.add_argument("started_at")


# -- the record's facts (migration step 2) -----------------------------------------------------------------


def beat(root, run):
	return runstate.beat(root, run, runstate.settings()["heartbeat_interval_seconds"])


def compare(root, run, mainDir):
	"""The record's story against today's files, once the run has its exit (run-steps wrapper-exit)."""
	record, _problem = runstate.readRecord(root, run)
	topic = (record or {}).get("topic") or os.path.basename(root)
	differences = runstate.compareStories(mainDir, topic, run)
	return runstate.reportDivergence(root, topic, run, differences)


def mainDirOf(root):
	"""The main checkout (WT_MAIN_DIR through homeconfig.env), None when it isn't set or can't be read."""
	mainDir = runstate.dualWrite(root, SOURCE, lambda _root: homeconfig.env("WT_MAIN_DIR"))
	return os.path.realpath(mainDir) if mainDir else None


def wrapperMainDir():
	"""WT_MAIN_DIR as the wrapper's environment holds it (worker-control sets it), None when unset or empty."""
	return os.environ.get("WT_MAIN_DIR") or None


def hookProgram(path):
	"""A follow-up program by its path under ~/.claude/hooks (<role>/<file>)."""
	return str(HOME_HOOKS / path)


class StepTimedOut(Exception):
	"""A step's program ran past its time limit and was ended (callProgram)."""


def endGroup(child, graceSeconds):
	"""TERM the child's process group, KILL it after graceSeconds while it still runs; wait for the child."""
	for stop, wait in ((signal.SIGTERM, graceSeconds), (signal.SIGKILL, None)):
		with contextlib.suppress(ProcessLookupError, PermissionError):
			os.killpg(child.pid, stop)
		try:
			return child.wait(timeout=wait)
		except subprocess.TimeoutExpired:
			continue
	return child.wait()


def callProgram(argv, limitSeconds=None, onSlow=None, onStart=None):
	"""Run a follow-up program in the foreground in its own session, its output going where ours goes (the run's
	stderr log); its exit code (negative: the signal that ended it). limitSeconds (None: no limit): after that long,
	onSlow() once and the wait goes on when given (the merge), else its process group is ended and StepTimedOut
	raised. onStart(pid) right after the start (the step record's program_pid)."""
	sys.stdout.flush()
	sys.stderr.flush()
	child = subprocess.Popen(argv, start_new_session=True)
	CHILD["process"] = child
	if onStart is not None:
		onStart(child.pid)
	try:
		if limitSeconds is None:
			return child.wait()
		try:
			return child.wait(timeout=limitSeconds)
		except subprocess.TimeoutExpired:
			if onSlow is not None:
				onSlow()
				return child.wait()
			code = endGroup(child, runstate.settings()["step_kill_grace_seconds"])
			raise StepTimedOut(f"its program was ended after its limit ({runstate.exitText(code)})") from None
	finally:
		CHILD["process"] = None


# -- today's files of the run ------------------------------------------------------------------------------


def runsOf(root):
	return hookonly.stateDir(root, RUNS)


def runFile(root, run, name, suffix=".json"):
	return os.path.join(runsOf(root), f"{name}-{run}{suffix}")


def readRecord(root, run):
	"""runstate.readRecord, with a record layer that can't load (its settings) as a problem, never a crash: the
	wrapper's run never depends on the record."""
	try:
		return runstate.readRecord(root, run)
	except Exception as error:
		return None, f"{type(error).__name__}: {error}"


def phaseFile(root, run, k):
	return runFile(root, run, "phase", f"-{k}.json")


def compactFile(root, run, k):
	return runFile(root, run, "compact", f"-{k}.json")


def resultText(path):
	"""The worker's text in a claude output file, stripped as the wrapper always read it ('' when unreadable)."""
	data, _error = runstate.readObject(path)
	return str((data or {}).get("result") or "").strip()


def firstLine(path):
	return (resultText(path).splitlines() or [""])[0]


def leadKey(path):
	"""The header key of the turn's first line (report_header.parse), None when that line is no header line: the
	wrapper's `PHASE:*` and `STATUS:*` tests of the first line."""
	return next(iter(report_header.parse(resultText(path))), None)


def leadsWith(path, value):
	"""Whether the turn's first line is `STATUS: <value>...` written exactly (report_header.leading): the wrapper's
	`STATUS: <value>*` test of the first line, the value in either case (R2's upper-case enum, as worker-stop.py and
	run_ending.isStatusText accept it; runstate.workerStatus reads it the same way). A value followed by more text on that line
	counts, as it always did."""
	found = report_header.leading(resultText(path))
	return bool(found) and found[0] == "STATUS" and found[1].lower().startswith(value)


def costOf(path):
	"""total_cost_usd of a claude output file, 0 when it has none or can't be read (cumulative for the session)."""
	data, _error = runstate.readObject(path)
	try:
		return float((data or {}).get("total_cost_usd") or 0)
	except (TypeError, ValueError):
		return 0.0


def nonEmpty(path):
	try:
		return os.path.getsize(path) > 0
	except OSError:
		return False


def runOutputs(root, run):
	"""This run's claude output files in the wrapper's order: result-, then compact-, then phase-<run>-*.json."""
	runs = runsOf(root)
	return (
		[runFile(root, run, "result")]
		+ sorted(glob.glob(os.path.join(runs, f"compact-{run}-*.json")))
		+ sorted(glob.glob(os.path.join(runs, f"phase-{run}-*.json")))
	)


def lastFileOf(root, run):
	"""A run's newest file: the one with the highest total_cost_usd (cumulative). A run without a STATUS line has no
	final compact, so its result file is newer than its highest compact file."""
	best, top = None, -1.0
	for path in runOutputs(root, run):
		if nonEmpty(path) and costOf(path) > top:
			best, top = path, costOf(path)
	return best


def sessionBase(root, run, mode):
	"""The session's cost before this run: the newest file of the latest earlier run that has one (resume only)."""
	if mode != "resume":
		return 0.0
	for earlier in range(run - 1, 0, -1):
		found = lastFileOf(root, earlier)
		if found:
			return costOf(found)
	return 0.0


def newestOutput(root, run):
	"""This run's last non-empty claude output (the newest written; a PHASE turn keeps its time when renamed)."""
	found = [path for path in runOutputs(root, run) if nonEmpty(path)]
	return max(found, key=os.path.getmtime) if found else None


def phaseCount(root, run):
	names = glob.glob(os.path.join(runsOf(root), f"phase-{run}-*.json"))
	numbers = [int(os.path.basename(name)[len(f"phase-{run}-") : -len(".json")]) for name in names]
	return max(numbers, default=0)


# -- next-invocation: the claude calls of one run (migration step 6) -----------------------------------------


def scriptedFlow(root, run):
	"""Whether the run drives the scripted flow: its record's flow (written at the launch, from the marker
	flow-next.py init left), else the marker itself (a run without a record)."""
	record, _problem = readRecord(root, run)
	if record is not None:
		return record.get("flow") == "scripted"
	return os.path.isfile(os.path.join(hookonly.stateDir(root, "flow"), "scripted"))


def remainingUsd(root, run, usd, base):
	"""What is left of the run's USD as the wrapper always printed it (two decimals, never below 0)."""
	last = newestOutput(root, run)
	spent = (costOf(last) if last else base) - base
	return f"{max(usd - spent, 0):.2f}"


def invocation(args, root, kind, file, prompt, code):
	"""The spec of one claude call, or the decision as if it ran with exit 99 when the budget is used up."""
	base = sessionBase(root, args.run, args.mode)
	budget = remainingUsd(root, args.run, args.usd, base)
	if float(budget) < homeconfig.load("budget")["worker_call_budget_min_usd"]:  # the run's budget is used up
		print(f"run-worker: run {args.run} budget used up before: {prompt[:40]}", file=sys.stderr)
		return decide(args, root, kind, BUDGET_EXIT_CODE, code)
	claudeArgs = ["-p", prompt, "--permission-mode", "auto", "--max-budget-usd", budget, "--output-format", "json"]
	claudeArgs += ["--session-id" if kind == "main" and args.mode == "new" else "--resume", args.session]
	if args.agent:
		claudeArgs += ["--agent", args.agent]
	compact = "1" if kind in ("compact", "final-compact") else ""
	return ["invoke", str(code), kind, os.path.basename(file), compact, repr(base), *claudeArgs]


def finalTurn(args, root, code):
	"""After the last main or continue call: a run that stopped right after a PHASE turn keeps that turn as its
	result; a run that ended with a STATUS line (not stopped, exit 0) gets one final compact."""
	result = runFile(root, args.run, "result")
	k = phaseCount(root, args.run)
	if not nonEmpty(result) and k > 0:
		shutil.copyfile(phaseFile(root, args.run, k), result)
	if args.stopped == "0" and code == 0 and leadKey(result) == "STATUS":
		prompt = COMPACT_PROMPT.format(topic=args.topic)
		return invocation(args, root, "final-compact", compactFile(root, args.run, k + 1), prompt, code)
	return ["done", str(code)]


def decide(args, root, after, last, code):
	"""The next call after the call `after` ended with exit code last (module doc, next-invocation)."""
	topic, run = args.topic, args.run
	result = runFile(root, run, "result")
	if after == "":
		if args.stopped != "0":  # stopped before claude's first call: nothing starts
			return ["done", str(code)]
		return invocation(args, root, "main", result, args.prompt, code)
	if after == "final-compact":
		return ["done", str(code)]
	if after in ("main", "continue"):
		if args.stopped == "0" and run_ending.isCut(runstate.readObject(result)[0]):  # a stop wins
			return ["done", "0"]
		code = last
		k = phaseCount(root, run)
		if args.stopped == "0" and code == 0 and k < args.max_phases and leadKey(result) == "PHASE":
			os.replace(result, phaseFile(root, run, k + 1))
			prompt = COMPACT_PROMPT.format(topic=topic)
			return invocation(args, root, "compact", compactFile(root, run, k + 1), prompt, code)
		return finalTurn(args, root, code)
	if after == "compact":
		if last != 0 or args.stopped != "0":
			return finalTurn(args, root, code)
		prompt = SCRIPTED_CONTINUE_PROMPT if scriptedFlow(root, run) else CONTINUE_PROMPT.format(topic=topic)
		return invocation(args, root, "continue", result, prompt, code)
	raise StepError(f"--after {after!r} is not a claude call kind")


# -- watch: the bound on the idle wait after a worker call's final STATUS turn -------------------------------------


def sameProcess(pid, startTicks):
	"""Whether pid is still the process the watcher started on (its start time unchanged)."""
	try:
		os.kill(pid, 0)
	except OSError:
		return False
	return runstate.processFacts(pid)["start_ticks"] == startTicks


def endProcess(pid, startTicks, graceSeconds, pollSeconds):
	"""TERM, then KILL when it still runs after graceSeconds; each signal only to the same process."""
	if not sameProcess(pid, startTicks):
		return
	os.kill(pid, signal.SIGTERM)
	deadline = time.monotonic() + graceSeconds
	while time.monotonic() < deadline:
		if not sameProcess(pid, startTicks):
			return
		time.sleep(min(pollSeconds, graceSeconds))
	if sameProcess(pid, startTicks):
		os.kill(pid, signal.SIGKILL)


def watch(args, root):
	"""The watcher of one worker call (module doc, watch). A pid that isn't the claude the wrapper started (its
	start time differs from the one handed over, or none was) is never signalled: claude's own wait then applies."""
	limits = runstate.settings()
	idleMax, poll = limits["ending_idle_max_seconds"], limits["ending_poll_seconds"]
	startTicks = int(args.start_ticks) if args.start_ticks.isdigit() else None
	if startTicks is None or not sameProcess(args.pid, startTicks):
		print(f"run-steps: run {args.run}: pid {args.pid} is not the claude this call started; no watcher", file=sys.stderr)
		return 0
	while sameProcess(args.pid, startTicks):
		turn = run_ending.finalTurn(root, args.run, args.started_at)
		if turn is not None and run_ending.idleSeconds(turn) >= idleMax:
			run_ending.recordCut(
				root, args.run, file=args.file, startedAt=args.started_at, turn=turn, idleMaxSeconds=idleMax
			)
			print(
				f"run-steps: run {args.run}: claude still waited {idleMax}s after its STATUS turn; ending the call",
				file=sys.stderr,
			)
			endProcess(args.pid, startTicks, limits["ending_kill_grace_seconds"], poll)
			return 0
		time.sleep(poll)
	return 0


def repairCut(root, run, file, startedAt):
	"""A call the watcher cut: its output file rewritten (run_ending.cutOutput) before anyone reads it."""
	cut = run_ending.cutOf(root, run, file, startedAt)
	if cut is None:
		return
	path = os.path.join(runsOf(root), file)
	data, _error = runstate.readObject(path)
	statefile.writeJson(path, run_ending.cutOutput(runstate.readText(path), data, cut))


CUT_NOT_KEPT = (
	"run-steps: run {run}: the call ended after its STATUS turn could not get that turn back as its result "
	"({error}); {file} is what claude left, so the run reads as an error without its STATUS line and the "
	"wrap-up (verify, verifier, merge) skips it. The STATUS turn's message is in {record} "
	"(raw.last_assistant_message): read it there and decide whether to `wt resume` the topic."
)


def keepCutTurn(root, run, file, startedAt):
	"""repairCut; a failure is told once per distinct error (a hook-failed event) with what it cost."""
	try:
		repairCut(root, run, file, startedAt)
	except Exception as error:
		text = f"{type(error).__name__}: {error}"
		try:
			first = hookio.recordFailureAt(root, SOURCE, error)
		except Exception as inner:
			first, text = True, f"{text}; recording that failed too ({type(inner).__name__}: {inner})"
		if first:
			record = statefile.pathOf(root, run_ending.CUT, run)
			print(CUT_NOT_KEPT.format(run=run, error=text, file=file, record=record), file=sys.stderr)


def nextInvocation(args, root):
	for stop in STOP_SIGNALS:  # the wrapper's trap takes a stop; this decision always completes
		signal.signal(stop, signal.SIG_IGN)
	spec = decide(args, root, args.after, args.last, args.code)
	print(" ".join(shlex.quote(word) for word in spec))
	return 0


def announce(root, run, topic, code, stopped):
	"""The run's end as the person's event (wt_events.py): before the wrap-up, so its events follow it."""
	mainDir = wrapperMainDir()
	if not mainDir:
		return
	if stopped:
		ended = "stopped (wt stop)"
	elif code != 0:
		ended = f"exit {code}"
	else:
		ended = firstLine(runFile(root, run, "result"))
	callProgram(
		[
			sys.executable,
			hookProgram("worker/wt_events.py"),
			"emit",
			mainDir,
			"run-ended",
			topic,
			f"run {run}: {ended or 'no STATUS line'}",
		]
	)


# -- the wrap-up (migration step 6) --------------------------------------------------------------------------


class Wrap:
	"""One wrap-up's facts: the run, its claude end, the main checkout and whether the record keeps the steps."""

	def __init__(self, root, run, args):
		self.root, self.run = root, run
		self.record, self.problem = readRecord(root, run)
		record = self.record or {}
		claude = record.get("claude") or {}
		self.topic = getattr(args, "topic", None) or record.get("topic") or os.path.basename(root)
		self.mainDir = wrapperMainDir()
		code = getattr(args, "code", None)
		self.code = code if code is not None else claude.get("exit_code")
		stopped = getattr(args, "stopped", None)
		self.continuesStop = continuesStop = bool(getattr(args, "continue_stopped", False))
		if stopped is not None:  # the wrapper's own wrap-up: its facts decide
			self.stopped = self.stopSeen = stopped == "1"
		else:  # run again (module doc, wrapup): a stopped run skips as stopped unless `wt wrapup` continues its stop
			self.stopSeen = runstate.stoppedRun(record)
			self.stopped = self.stopSeen and not (continuesStop and runstate.stopDuringWrapup(record))
		self.person = bool(getattr(args, "person", False))
		# Steps are kept in the record whenever it exists; only a run without a record file runs each step once
		# without bookkeeping (unreadableRecord refuses one that exists but can't be read).
		self.bookkeeping = self.record is not None

	def file(self, name, suffix=".json"):
		return runFile(self.root, self.run, name, suffix)

	def decision(self, name):
		data, _error = runstate.readObject(self.file(name))
		return data or {}

	def runDone(self):
		return not self.stopped and self.code == 0 and leadsWith(self.file("result"), WORKER_DONE)

	def spec(self):
		"""The topic's verify spec path, None without a main checkout."""
		return os.path.join(self.mainDir, "plans", "verify", f"{self.topic}.json") if self.mainDir else None

	def onStop(self, signum, _frame):
		"""A stop of the run (module doc): the step program running now gets the same signal in its own session."""
		self.stopped = True
		child = CHILD["process"]
		if child is not None and child.poll() is None:
			with contextlib.suppress(ProcessLookupError, PermissionError):
				os.killpg(child.pid, signum)


def attemptsMax():
	return runstate.settings()["step_attempts_max"]


def recordProgram(wrap, name, pid):
	"""The step's own program in the record (runstate.recordStepProgram), so it holds the run while it lives."""
	if wrap.bookkeeping:
		runstate.dualWrite(wrap.root, SOURCE, runstate.recordStepProgram, wrap.run, name, pid,
			reconciler.startTicks(pid))


def stepLimit(wrap, name):
	"""(seconds, basis) of step name's time limit (step_history.stepLimit; the main checkout's history); (None,
	why) when the settings it needs can't be read: the step then runs without a limit, as before 3c (a stderr line),
	since the wrapper's run never depends on its settings loading."""
	try:
		return step_history.stepLimit(wrap.mainDir, name)
	except (homeconfig.ConfigError, KeyError, OSError, TypeError, ValueError) as error:
		text = f"{type(error).__name__}: {error}"[:200]
		print(f"run-steps: step {name} of run {wrap.run} runs without a time limit: {text}", file=sys.stderr)
		if wrap.bookkeeping and runstate.dualWrite(wrap.root, SOURCE, runstate.recordNotice, wrap.run, "no_limit"):
			personEvent(wrap, "limit-unreadable", f"run {wrap.run}: its wrap-up steps run without a time limit and "
				f"without the running-long note, the settings can't give one ({text}); a hung step stays until "
				f"stopped (wt status {wrap.topic})")
		return None, f"no limit ({type(error).__name__})"


def claim(wrap, name, force, again, limitSeconds=None):
	"""startStep for this runner: {"attempt": n}, {"skip": why}, or None (no bookkeeping: run as before)."""
	if not wrap.bookkeeping:
		return None
	return runstate.dualWrite(
		wrap.root,
		SOURCE,
		lambda root, run: runstate.startStep(
			root,
			run,
			name,
			os.getpid(),
			attemptsMax(),
			lambda step: reconciler.runnerAlive(root, run, step),
			force,
			again,
			reconciler.startTicks(os.getpid()),
			limitSeconds,
		),
		wrap.run,
	)


def record(wrap, name, body):
	if wrap.bookkeeping:
		runstate.dualWrite(wrap.root, SOURCE, runstate.recordStep, wrap.run, name, body)


def currentStep(wrap, name):
	found, _problem = readRecord(wrap.root, wrap.run)
	return ((found or {}).get("steps") or {}).get(name) or {}


def realDataGate(wrap):
	"""verify's and verifier's rule (module doc): (run it, else the skip reason)."""
	if not wrap.mainDir or not os.path.isfile(wrap.spec()):
		return False, "no-spec"
	if wrap.stopped:
		return False, "stopped"
	if not wrap.runDone():
		return False, "not-done"
	return True, None


def verifyAction(wrap):
	run, why = realDataGate(wrap)
	if not run:
		return ("skip", why)
	return ("call", [sys.executable, hookProgram("gates/verify.py"), wrap.root, wrap.topic, str(wrap.run)])


def verifierAction(wrap):
	run, why = realDataGate(wrap)
	if not run:
		return ("skip", why)
	return ("call", [sys.executable, hookProgram("gates/verifier.py"), wrap.root, wrap.topic, str(wrap.run)])


def mergeAction(wrap):
	argv = [sys.executable, hookProgram("worker/auto-merge.py"), wrap.root, wrap.topic, str(wrap.run)]
	if wrap.person:
		return ("call", argv + ["--person"])
	if homeconfig.flowMode(wrap.root, recorded=(wrap.record or {}).get("integration_mode")) != "local":
		return ("skip", "origin-mode")
	if wrap.stopped:
		return ("skip", "stopped")
	if not wrap.runDone():
		return ("skip", "not-done")
	if verifierRetryable(wrap):
		return ("skip", "verifier-retry")
	return ("call", argv)


def verifierRetryable(wrap):
	"""Whether the verifier step failed (it could not judge) with attempts left: the merge waits for its retry."""
	step = currentStep(wrap, "verifier")
	attempt = step.get("attempt") if runstate.isNumber(step.get("attempt")) else 0
	return step.get("state") == "failed" and attempt < attemptsMax()


def laterRunOf(wrap):
	"""The record of a run this run's next step started (launch.by_run == run), None without one."""
	records, _legacy = runstate.runNumbersIn(runsOf(wrap.root))
	for later in sorted(k for k in records if k > wrap.run):
		found, _problem = readRecord(wrap.root, later)
		if ((found or {}).get("launch") or {}).get("by_run") == wrap.run:
			return found
	return None


def nextAction(wrap):
	later = laterRunOf(wrap)
	decided = wrap.decision("auto").get("decision")
	if later is not None or decided in runstate.NEXT_RUNS:
		if os.path.exists(wrap.file("auto")):
			return ("from-file", None)
		outcome = {"merge": "merge", "fix": "fix"}.get(later.get("purpose"), "resume")
		return (
			"done",
			{"outcome": outcome, "reason": f"run {later['run']} was already started by this run's next step"},
		)
	merge = wrap.decision("merge")
	control = [sys.executable, hookProgram("worker/worker-control.py")]
	where = [wrap.root, wrap.topic, str(wrap.run)]
	if merge.get("merge_run"):
		return ("call", control + ["auto-merge"] + where)
	if merge.get("fix_run"):
		return ("call", control + ["auto-fix"] + where)
	blocked = not wrap.stopped and wrap.code == 0 and leadsWith(wrap.file("result"), WORKER_BLOCKED)
	if blocked and nonEmpty(wrap.file("auto-resume")):
		return ("call", control + ["auto"] + where)
	return ("skip", "stopped" if wrap.stopped else "no-follow-up")


def finishRecorded(wrap):
	"""The archive's finish.json of this topic's run when that finish succeeded (it removed the worktree), None
	otherwise: a failed finish is retried. One of another schema version (state kind finish-record) counts as
	absent."""
	if not wrap.mainDir:
		return None
	data = statefile.read(wrap.mainDir, "finish-record", wrap.topic) or {}
	if data.get("run") != wrap.run or (data.get("steps") or {}).get("remove") != "removed":
		return None
	return statefile.pathOf(wrap.mainDir, "finish-record", wrap.topic)


def finishAction(wrap):
	if wrap.decision("merge").get("decision") != "merged":
		return ("skip", "not-merged")
	recorded = finishRecorded(wrap)
	if recorded:
		return ("done", {"outcome": LAUNCHED, "reason": f"auto-finish already ran for this run ({recorded})"})
	helpers = reconciler.liveHelpers(wrap.root, wrap.run)
	if helpers is None:  # the step fails (an attempt used): never a second auto-finish.py beside a running one
		raise RuntimeError("the processes can't be listed, so a running auto-finish.py of this run can't be ruled out")
	running = [h for h in helpers if h.endswith("auto-finish.py")]
	if running:
		return ("done", {"outcome": LAUNCHED, "reason": f"auto-finish.py of this run still runs ({running[0]})"})
	return ("launch", [sys.executable, hookProgram("worker/auto-finish.py"), wrap.root, wrap.topic, str(wrap.run)])


def queueAction(wrap):
	if not wrap.mainDir:
		return ("skip", "no-main-checkout")
	return ("tick", [sys.executable, hookProgram("worker/wt_queue.py"), "tick", wrap.mainDir])


ACTIONS = {
	"verify": verifyAction,
	"verifier": verifierAction,
	"merge": mergeAction,
	"next": nextAction,
	"finish": finishAction,
	"queue_tick": queueAction,
}


def nothingToDo(wrap, name):
	"""The skip reason of a program that exited 0 without an outcome because it had nothing to do, else None."""
	spec = wrap.spec()
	if name in ("verify", "verifier") and (not spec or not os.path.isfile(spec)):
		return "no-spec"
	if name == "verifier":
		data, _error = runstate.readObject(spec)
		return "verify-not-pass" if (data or {}).get("verifier") else "no-verifier-section"
	return None


def afterProgram(wrap, name, code):
	"""Close a step its program left running (module doc): failed, or read from its file, or skipped."""
	if not wrap.bookkeeping or currentStep(wrap, name).get("state") in ("done", "skipped"):
		return
	if code != 0:
		closeFailed(wrap, name, f"exited without an outcome ({runstate.exitText(code)})")
	elif name in DECISION_FILES and os.path.exists(wrap.file(DECISION_FILES[name])):
		runstate.dualWrite(wrap.root, SOURCE, runstate.recordStepFromFile, wrap.run, name)
	elif nothingToDo(wrap, name):
		record(wrap, name, {"state": "skipped", "skip_reason": nothingToDo(wrap, name)})
	else:
		closeFailed(wrap, name, "exited without an outcome (exit 0)")


def personEvent(wrap, kind, text):
	"""One event for the person (wt_events.py) in the main checkout; none without one (the stderr log has it)."""
	if wrap.mainDir:
		import wt_events

		wt_events.emit(wrap.mainDir, kind, wrap.topic, text)


def closeFailed(wrap, name, reason):
	"""A step this runner closes failed: the record's step and the person's step-failed event."""
	record(wrap, name, {"state": "failed", "reason": reason[:300]})
	personEvent(wrap, "step-failed", f"run {wrap.run}: {name}: {reason[:200]} (wt status {wrap.topic})")


def launch(argv, log):
	"""Start a program detached in its own session (auto-finish waits for this run's end); its output is appended
	to log (the run's stderr log), never dropped."""
	os.makedirs(os.path.dirname(log), exist_ok=True)
	with open(log, "ab") as out:
		subprocess.Popen(
			argv,
			start_new_session=True,
			env=os.environ.copy(),
			stdin=subprocess.DEVNULL,
			stdout=out,
			stderr=out,
		)


def heldUntil(step, name):
	"""When waiting for a step held elsewhere ends at the latest (module doc): its start plus its time limit (the
	recorded limit_seconds, else the step's step_limit_default_seconds) plus step_kill_grace_seconds; None when its
	start can't be read (nothing to bound the wait by: no wait)."""
	started = runstate.parseStamp(step.get("started_at"))
	if started is None:
		return None
	settings = runstate.settings()
	limit = step.get("limit_seconds")
	if not runstate.isNumber(limit):
		limit = settings["step_limit_default_seconds"][name]
	return started + datetime.timedelta(seconds=limit + settings["step_kill_grace_seconds"])


def waitForEarlierSteps(wrap, name):
	"""Wait while a step before name runs under another live runner or its own program (module doc), so name never
	decides on a step still running; bounded by heldUntil, ended by a stop of this run. A wait that can't be judged
	(the record or the settings can't be read) is a stderr line and the step goes on, as before the wait existed."""
	if not wrap.bookkeeping:
		return
	earlier = runstate.STEP_ORDER[: runstate.STEP_ORDER.index(name)]
	try:
		while not wrap.stopped:
			steps = ((readRecord(wrap.root, wrap.run)[0] or {}).get("steps")) or {}
			held = [n for n in reconciler.heldSteps(wrap.root, wrap.run, steps, earlier)
				if steps[n].get("runner_pid") != os.getpid()]
			if not held:
				return
			until = heldUntil(steps[held[0]], held[0])
			if until is None or clock.now() >= until:
				print(f"run-steps: step {name} of run {wrap.run} goes on while step {held[0]} still runs elsewhere "
					f"(past its limit, or its start can't be read)", file=sys.stderr)
				return
			time.sleep(runstate.settings()["status_wait_poll_seconds"])
	except (homeconfig.ConfigError, KeyError, OSError, TypeError, ValueError) as error:
		print(f"run-steps: step {name} of run {wrap.run} doesn't wait for the steps before it: "
			f"{type(error).__name__}: {error}", file=sys.stderr)


def runStep(wrap, name, force=False, again=False):
	"""One wrap-up step (module doc): wait for a step before it still running elsewhere, claim it, act, record the
	outcome; a failure here is the step's, never the wrap-up's. again: a skipped step is evaluated again (a step
	before it ran again, or `step <name>` asked for it); a done one never is. True when the step ran (its action
	was taken)."""
	waitForEarlierSteps(wrap, name)
	limitSeconds, basis = stepLimit(wrap, name)
	claimed = claim(wrap, name, force, ("skipped",) if again else (), None if limitSeconds is None else round(limitSeconds))
	if claimed is not None and "skip" in claimed:
		return False
	limited = limitSeconds if name in ENDED_AT_LIMIT else None
	try:
		kind, what = ACTIONS[name](wrap)
		if kind == "skip":
			record(wrap, name, {"state": "skipped", "skip_reason": what})
		elif kind == "done":
			record(wrap, name, what)
		elif kind == "from-file":
			runstate.dualWrite(wrap.root, SOURCE, runstate.recordStepFromFile, wrap.run, name)
		elif kind == "launch":
			launch(what, wrap.file("stderr", ".log"))
			record(
				wrap, name, {"outcome": LAUNCHED, "reason": f"{os.path.basename(what[1])} started in its own session"}
			)
		elif kind == "tick":
			outcome = runstate.queueTickOutcome(callProgram(what, limited, onStart=lambda pid: recordProgram(wrap, name, pid)))
			record(wrap, name, outcome)
			if outcome["outcome"] != "ticked":
				personEvent(wrap, "queue-failed", f"queue tick after run {wrap.run}'s wrap-up: {outcome['reason']} "
					f"(stderr-{wrap.run}.log)")
		elif name == "merge":
			afterProgram(wrap, name, callProgram(what, limitSeconds,
				None if limitSeconds is None else lambda: mergeSlow(wrap, limitSeconds, basis),
				lambda pid: recordProgram(wrap, name, pid)))
		else:
			afterProgram(wrap, name, callProgram(what, limited, onStart=lambda pid: recordProgram(wrap, name, pid)))
	except StepTimedOut as error:
		print(f"run-steps: step {name} of run {wrap.run}: {error}", file=sys.stderr)
		closeFailed(wrap, name, f"exceeded {limitSeconds / 60:.0f} min, from {basis}; {error}")
	except Exception as error:  # the step failed, not the wrap-up: record why and go on with the next step
		print(f"run-steps: step {name} of run {wrap.run} failed: {type(error).__name__}: {error}", file=sys.stderr)
		closeFailed(wrap, name, f"run-steps.py: {type(error).__name__}: {error}")
	return True


def mergeSlow(wrap, limitSeconds, basis):
	"""The merge ran past its limit: the person's merge-slow event, once; the merge goes on (module doc)."""
	personEvent(wrap, "merge-slow", f"run {wrap.run}: the merge has run {limitSeconds / 60:.0f} min, longer than "
		f"expected ({basis}); it is not stopped, since a merge cut short can leave the main checkout half merged "
		f"(wt status {wrap.topic})")


def reconcileOthers(wrap):
	"""After the wrap-up: the reconciler for the repository's other worktrees, detached (module doc, wrapup); a
	start that fails is a stderr line only."""
	if not wrap.mainDir:
		return
	try:
		reconciler.startReconcile(wrap.mainDir, exclude=wrap.root, log=wrap.file("stderr", ".log"))
	except OSError as error:
		print(f"run-steps: the reconciler could not be started: {error}", file=sys.stderr)


def endRun(wrap):
	"""exit-<run> ("<code> <time>", kept when it is there already) and the record's exit in the same moment, so the
	queue tick after it sees the run ended in both stories."""
	path = wrap.file("exit", "")
	if not os.path.exists(path):
		with open(path, "w", encoding="utf-8") as f:
			f.write(f"{wrap.code} {clock.isoNow()}\n")
	reason = "stopped" if wrap.stopped or wrap.stopSeen else "completed"
	runstate.dualWrite(wrap.root, SOURCE, runstate.recordRunEnded, wrap.run, reason, wrap.code, mainDirOf(wrap.root))


def endedOtherwise(wrap):
	"""Why the run's wrap-up must not run (its record ended without completing or stopping), None when it may."""
	end = (wrap.record or {}).get("exit") or {}
	if end and end.get("reason") not in WRAPUP_ENDS:
		return f"run {wrap.run} ended {end.get('reason')}: no wrap-up step runs"
	return None


def wrapup(args, root):
	"""The wrap-up (module doc); a crash outside its steps ends the run wrapup-failed (wrapupFailed), exit 1."""
	try:
		return wrapupSteps(args, root)
	except Exception as error:
		return wrapupFailed(args, root, error)


def wrapupFailed(args, root, error):
	"""The wrap-up crashed outside its steps: the stderr log gets the error, the run ends wrapup-failed with claude's
	exit code (runstate.recordWrapupFailed: not an orphan; its steps left stay owed for `wt wrapup`), and the person
	gets wrapup-failed. Exit 1."""
	text = f"{type(error).__name__}: {error}"[:300]
	print(f"run-steps: the wrap-up of run {args.run} failed outside its steps: {text}", file=sys.stderr)
	failure = {}

	def endFailed(root, run, code, why):
		try:
			return runstate.recordWrapupFailed(root, run, code, why)
		except Exception as inner:
			failure["error"] = f"{type(inner).__name__}: {inner}"[:200]
			raise

	runstate.dualWrite(root, SOURCE, endFailed, args.run, args.code, text)
	mainDir = wrapperMainDir()
	if mainDir:
		import wt_events

		topic = args.topic or os.path.basename(root)
		unrecorded = (
			f"; the run record could not be written either ({failure['error']}): the run reads as an orphan"
			if failure
			else ""
		)
		wt_events.emit(mainDir, "wrapup-failed", topic, f"run {args.run}: {text}{unrecorded} (stderr-{args.run}.log)")
	return 1


def unreadableRecord(wrap):
	"""Why no step may run (the run's record file exists but can't be read, or the record layer can't load), None
	when the record is usable or there is no record file (a run before the record runs without bookkeeping)."""
	if wrap.record is not None or wrap.problem is None:
		return None
	return f"run {wrap.run}'s run record can't be read ({wrap.problem}): no wrap-up step runs"


def refuseUnreadable(wrap, why):
	"""A2: refuse, fail closed: a stderr line and the person's wrapup-failed event; exit 1."""
	print(f"run-steps: {why}", file=sys.stderr)
	personEvent(wrap, "wrapup-failed", f"run {wrap.run}: {why}; check the record, then run the wrap-up again "
		f"(stderr-{wrap.run}.log)")
	return 1


def wrapupSteps(args, root):
	wrap = Wrap(root, args.run, args)
	unreadable = unreadableRecord(wrap)
	if unreadable:
		return refuseUnreadable(wrap, unreadable)
	refusal = endedOtherwise(wrap) or (None if wrap.code is not None else f"run {args.run}'s exit code is unknown")
	if refusal:
		print(f"run-steps: {refusal}", file=sys.stderr)
		return 2 if wrap.code is None else 0
	for stop in STOP_SIGNALS:
		signal.signal(stop, wrap.onStop)
	ran = False
	for name in WRAPUP_STEPS:
		# A skip may follow from an earlier step's outcome (finish: not merged): once a step ran again, the skips
		# after it are evaluated again; so is a skip as stopped when this wrap-up is not stopped (wt wrapup).
		ran = runStep(wrap, name, again=ran or staleStopSkip(wrap, name)) or ran
	endRun(wrap)
	runStep(wrap, "queue_tick")
	if wrap.continuesStop:
		tellUsedUp(wrap)
	reconcileOthers(wrap)
	return 0


def tellUsedUp(wrap):
	"""A wrap-up `wt wrapup` started that leaves steps owed with every attempt used: the person's step-at-limit, once
	per run (reconciler.tellLimitOnce; the reconciler never tells it for a stopped run). One that can't be told is
	a stderr line."""
	if not (wrap.bookkeeping and wrap.mainDir):
		return
	found, _problem = readRecord(wrap.root, wrap.run)
	usedUp = runstate.stepsUsedUp(found or {}, attemptsMax())
	if not usedUp:
		return
	why = f"{', '.join(usedUp)} used all {attemptsMax()} attempt(s) (step_attempts_max) and stay owed"
	try:
		reconciler.tellLimitOnce(wrap.mainDir, wrap.root, wrap.run, wrap.topic, why)
	except (runstate.RecordError, OSError, ValueError) as error:
		print(f"run-steps: run {wrap.run}: step-at-limit not told ({type(error).__name__}: {error}): {why}",
			file=sys.stderr)


def staleStopSkip(wrap, name):
	"""Whether step name was skipped as stopped and this wrap-up is not stopped (`wt wrapup` continuing a stop during
	the wrap-up, Wrap): that skip no longer holds, so the step is evaluated again (a done step never is)."""
	return not wrap.stopped and wrap.bookkeeping and runstate.skippedAsStopped(currentStep(wrap, name))


def personForced(wrap, name):
	"""Whether `wt merge` runs step name again (module doc, step): never once it started a run or auto-finish."""
	step = currentStep(wrap, name)
	if name == "next":
		return not (step.get("state") == "done" and step.get("outcome") in runstate.NEXT_RUNS)
	return not (step.get("state") == "done" and step.get("outcome") == LAUNCHED)


def stepCommand(args, root):
	wrap = Wrap(root, args.run, args)
	unreadable = unreadableRecord(wrap)
	if unreadable:
		return refuseUnreadable(wrap, unreadable)
	refusal = endedOtherwise(wrap)
	if refusal:
		print(f"run-steps: {refusal}", file=sys.stderr)
		return 0
	if not (args.person and args.name == "merge"):
		runStep(wrap, args.name, again=True)
		return 0
	import wt_controls

	try:
		runStep(wrap, "merge", force=True)
		for name in ("next", "finish"):
			runStep(wrap, name, force=personForced(wrap, name), again=True)
	finally:
		wt_controls.releaseApproval(root, args.run, os.getpid())
	return 0


def reconcileCommand(args):
	"""`reconcile` (module doc): the reconciler; exit 2 with a wrong target, 1 when it crashed (told)."""
	if bool(args.worktree) == args.every:
		print("run-steps: reconcile needs a worktree or --all (one of them)", file=sys.stderr)
		return 2
	try:
		print(reconciler.reconcile(args.worktree, args.every, os.getcwd(), args.exclude, dryRun=args.dry_run))
	except reconciler.Refused as error:
		print(f"run-steps: reconcile: {error}", file=sys.stderr)
		return 2
	except Exception as error:  # told, never a traceback: a stderr line and a hook-failed event
		print(f"run-steps: reconcile failed: {type(error).__name__}: {error}", file=sys.stderr)
		mainDir = homeconfig.env("WT_MAIN_DIR")
		if mainDir and os.path.isdir(mainDir):
			with contextlib.suppress(Exception):
				hookio.recordFailureAt(mainDir, SOURCE, error)
		return 1
	return 0


def main(argv=None):
	args = parser().parse_args(argv)
	if args.command == "reconcile":
		return reconcileCommand(args)
	root = os.path.realpath(args.worktree)
	if not os.path.isdir(root):
		return 0  # no worktree, no record: nothing to write anywhere
	try:
		return command(args, root)
	except Exception as error:  # wrapup has its own (wrapupFailed); any other command's crash is told, never silent
		return commandFailed(args, root, error)


def commandFailed(args, root, error):
	"""A command crashed: one stderr line, a hook-failed event once per distinct error (hookio.recordFailureAt), and
	for `step` (wt merge, a step by hand) the person's step-failed event. Exit 1."""
	text = f"{type(error).__name__}: {error}"[:300]
	print(f"run-steps: {args.command} of run {args.run} failed: {text}", file=sys.stderr)
	try:
		hookio.recordFailureAt(root, SOURCE, error)
	except Exception as inner:
		print(f"run-steps: recording that failed too ({type(inner).__name__}: {inner})", file=sys.stderr)
	mainDir = wrapperMainDir()
	if args.command == "step" and mainDir:
		import wt_events

		topic = args.topic or os.path.basename(root)
		wt_events.emit(mainDir, "step-failed", topic, f"run {args.run}: {args.name}: {text}")
	return 1


def command(args, root):
	"""One command (module doc), its exit code."""
	write = lambda action, *rest: runstate.dualWrite(root, SOURCE, action, args.run, *rest)  # noqa: E731
	if args.command == "next-invocation":
		return nextInvocation(args, root)
	if args.command == "wrapup":
		return wrapup(args, root)
	if args.command == "step":
		return stepCommand(args, root)
	if args.command == "started":
		record = write(runstate.recordStarted, args.pid)
		if (record or {}).get("stop"):
			print("stopped")
	elif args.command == "main-head":
		write(runstate.recordMainHead)
	elif args.command == "beat":
		write(beat)
	elif args.command == "watch":
		return watch(args, root)
	elif args.command == "invocation":
		keepCutTurn(root, args.run, args.file, args.started_at)
		write(runstate.recordInvocation, args.kind, args.file, args.started_at, args.code, args.base_usd)
	elif args.command == "claude-ended":
		write(runstate.recordClaudeEnded, args.code, args.stopped == "1")
		if args.topic:
			announce(root, args.run, args.topic, args.code, args.stopped == "1")
	elif args.command == "ended":
		write(runstate.recordRunEnded, args.reason, args.code, mainDirOf(root))
	else:
		mainDir = mainDirOf(root)
		write(runstate.recordWrapperExit, args.status, args.signal, args.reason, args.code, mainDir)
		if mainDir and args.reason:
			write(compare, mainDir)
	return 0


if __name__ == "__main__":
	sys.exit(main())

#!/usr/bin/env python3
"""UserPromptSubmit hook: launch, resume and stop headless worktree workers.

Config mode TEST (hooks/lib/config_mode_guard.py): no worker starts outside a rehearsal repo (launch, resume,
a queue start or tick, auto-start; start_run refuses as the last line), and every wt answer ends with the mode's
line while it isn't DEV.

Prompts (typed in the planning session, main checkout):
  wt launch <topic>   first run: new session id, prompt "/feature-flow <topic>"
  wt resume <topic>   resume the recorded session: "re-read the plan, continue"; also lifts the
                      worktree's agent-guard HOLD (the user decided after the ALERT). In merge mode
                      (below) the prompt sends the session to feature-flow merge.md instead. Every
                      decision reads hooks/lib/runstate.py status with the pid check this hook can make
                      (runstate.verifyProcess: the recorded wrapper pid, its command line names
                      run-worker.sh, the topic and the run, its start ticks match). Refused while the run
                      launches, runs or finishes, when its state can't be read, and when the last run
                      ended `STATUS: done` and no new estimate was written since the last run a person or
                      the queue started (status "resumable", the pane's rule, one place). Refused too when
                      the last run ended `STATUS: DONE` and its wrap-up still runs (as for `wt merge`: a live
                      `run-steps.py wrapup` of the run, or a step recorded running whose runner or own
                      program lives) or its merge went through (the finish archives the topic and removes
                      the worktree a new run would work in), whatever the estimate. An orphan (a run
                      with no end whose wrapper died): when the check finds the wrapper gone, wt resume
                      closes the run first (runstate.closeVerifiedOrphan: orphan.confirmed_at and a
                      run-orphaned event, exit orphan-closed, unfinished wrap-up steps abandoned, never a
                      merge), then starts the next run; a run with only today's files has no record to
                      close, and the new run ends it (the legacy adapter reads a run with a later one as
                      ended). An orphan whose worker ended `STATUS: done` with wrap-up steps left
                      (runstate.wrapupLeft) is neither closed nor resumed: the refusal names the
                      `run-steps.py wrapup` command that finishes it. When the check can't confirm it gone
                      (no pid recorded, /proc unreadable), it refuses with the reason and points to
                      `wt status <topic>`
  wt stop <topic>     only a run that launches, runs or finishes, and only after runstate.verifyProcess
                      confirms its recorded wrapper (pid, command line, start ticks; otherwise refused
                      with the reason, nothing signalled): write .hook-only/runs/stopped-<run> and the
                      record's stop, then SIGINT the wrapper's process group, SIGTERM after 10 s while the
                      same check still finds it. Only once the record says the wrapper started
                      (process.started_at, written after its INT/TERM traps are set): a signal before them
                      would kill it without an end. Before that nothing is signalled; the wrapper reads the
                      stop when it records its start (run-steps.py started) and ends the run stopped
                      A run that ended (or a confirmed orphan) whose wrapper is gone but whose wrap-up
                      still runs (`wt wrapup`, the reconciler): the same marker and record, then SIGINT to
                      the processes reconciler.wrapupProcesses confirms (a recorded step runner or program:
                      pid, command line for a runner, start ticks), never anything else; SIGTERM after 10 s
                      to one still confirmed (stopWrapup)
  wt queue [<topic> ...]   append the topics to the queue in that order (wt_queue.py: the planning
                      session wrote plans/queue/<topic>.md and .json; local mode only), then start
                      whatever may start now; shown to the person only, never entering the session; `wt queue` alone starts what may start (a held topic
                      merged by hand ends no run) and shows the queue
  wt queue drop <topic>    take a topic that hasn't started (or whose start failed) out of the queue
  wt status [<topic>]  what each topic's run is doing (every topic wt_topics.topics lists, or the one named):
                      runstate.agent per topic, with the pid check this hook can make outside the sandbox
                      (runstate.verifyProcess: a suspected orphan whose wrapper is gone reads confirmed, a live
                      one with a stale heartbeat keeps its phase). Display only (migration step 3): it decides
                      and writes nothing; the prompt reaches the model with the views as context, so it can
                      answer the person
  wt merge <topic>    the person's approval of a refused automatic merge (local mode; the pane's `merge`
                      button sends it): runstate.status says the last run ended (and its wrapper is gone),
                      its merge step was refused and not as a merge run; `run-steps.py step <worktree> <run>
                      merge --topic <topic> --person` then runs in the background (the tests take longer
                      than a prompt hook may): auto-merge.py --person as a new attempt of the run's merge
                      step, skipping only the checks that ask for a person, merging and recording it in
                      merges.jsonl; then the run's next and finish steps (a merge run when the branch lacks
                      the latest main; auto-finish after a merge); its result is an event (wt_events.py).
                      Refused while the run's wrap-up still runs (a live `run-steps.py wrapup` of the run, or
                      a step recorded running whose runner or own program lives: reconciler.heldSteps),
                      checked first and again under runs/approve.lock: the answer names what runs, and its own
                      merge step decides first; only a merge it refuses is approved, with its reason known.
                      Shown to the person only, never entering the session.
                      One approval per run at a time: before starting run-steps.py it writes
                      runs/approve-<run>.json ({"run", "pid", "started", "started_epoch"}; under
                      runs/approve.lock), and a second `wt merge` while that pid is a live run-steps.py of
                      this topic and run is refused ("already being approved": status "approving", the
                      approval no merge decision is newer than, and its pid checked); a record whose process
                      is gone is stale and replaced. run-steps.py removes the record when its steps end
                      (wt_controls.releaseApproval); the pane reads it (wt_controls.py "approving"). Before,
                      merge-<run>.json kept saying refused for the minutes the tests took, so a second press
                      started a second auto-merge
  wt reconcile [<topic>] [--dry-run]
                      the reconciler (run-steps.py reconcile <worktree>|--all [--dry-run], outside the sandbox:
                      hooks/worker/reconciler.py): per worktree its latest run's class (nothing, hands-off,
                      unreadable, at-limit, wrapup-owed, step-retryable, next-unstarted) and the action it took
                      (a wrap-up or a next run's start detached, the person told at the limit; only when every
                      process check says gone) or, with --dry-run, would take, with a step running long (B1) and a
                      stale heartbeat of a live wrapper (B2) as notes; as context.
  Every `wt` command but `wt status` (display only: it writes and starts nothing) and `wt reconcile` (it runs one
  itself) is also a trigger of the reconciler for the main checkout (reconciler.triggerFrom: detached, at most once
  per reconcile_wt_interval_minutes, before the command is answered; a trigger that fails starts nothing and never
  changes the answer)
  wt wrapup <topic>   finish the kept wrap-up of a run whose worker ended `STATUS: done` but whose wrapper
                      ended before the wrap-up, or whose wrap-up crashed (exit wrapup-failed) or was stopped
                      (exit stopped, claude's part unstopped: runstate.stopDuringWrapup), these two checked
                      gone here with runstate.verifyProcess, no orphan record (runstate status "resumable"
                      code wrapup-left; the pane's `wrapup` button sends it). Refused, one line each: no
                      such run, a wrapper not confirmed gone by the pid check (runstate.verifyProcess), a
                      wrap-up of that run already running (a live `run-steps.py wrapup <worktree> <run>`).
                      Otherwise an orphan's pid check result is recorded (runstate.confirmOrphan, once), and
                      under the run record's
                      lock `run-steps.py wrapup <worktree> <run> --topic <topic> --continue-stopped` starts
                      detached with WT_MAIN_DIR (the record's claude.exit_code decides its steps; a stop of
                      the wrap-up no longer skips them, a stop of claude's part still does). Its merge step
                      goes through auto-merge.py's own rules, which refuse an orphan and a stopped run; the
                      run then ends, and the answer says the next step is `wt merge <topic>` (for a stop
                      during the wrap-up the person's approval passes the stop: runstate.WRAPUP_STOP). When
                      every step left has used all step_attempts_max attempts, nothing starts: the refusal
                      says so and the person's step-at-limit event is written once per run
                      (reconciler.tellLimitOnce). Shown to the person only, never entering the session

A wt prompt whose settings can't be read (homeconfig.ConfigError: the home config, the config mode, the repo's
flow.json, a flow-mode record) is refused with one line on stderr naming the file and the problem, and exit 2
(configRefusal; hook-entry.py reads a program step's exit 2 as its refusal, the stderr as the reason); never a
traceback.

Runs outside the sandbox. Run settings come from plans/worker/<topic>.json in
the main checkout, written by the planning session before each launch/resume:
  {"minutes": 40, "est_usd": 6, "agent": "feature-orchestrator", "session_id": "..."}
  minutes and est_usd are the corrected estimates for this run (minutes with the hook-wait reserve
  cost-history.py estimate branch adds). The limits sit budget_headroom_factor (home.json budget) above
  them: minutes x headroom goes to run-worker.sh (CLAUDE_WORKER_MINUTES for worker_budget.py and
  dispatch_guard.py, and the background wait ceiling), est_usd x headroom is --max-budget-usd. An older
  file with "usd" (the cap itself) still works for the USD. "agent" optional; "session_id" only to
  adopt a session started by hand.
  "flow": "scripted" (optional, logic / wiring / small fix): launch runs `flow-next.py init`, which writes the
  first .hook-only/flow/next.md, and the prompt becomes SCRIPTED_PROMPT (feature-flow scripted.md). An automatic
  resume keeps it (SCRIPTED_AUTO_PROMPT: the planned dispatch is still in next.md); a resume the person starts
  first retries a stop with the current hook code (`flow-next.py retry`) and keeps it while the flow state can
  go on (`flow-next.py runnable`; SCRIPTED_RESUME_PROMPT) and otherwise turns it off; a fix run and merge mode always turn it off
  (`flow-next.py off`) and continue in the written flow.
  "mode": "merge" (cost-history.py estimate branch --merge prints it; skill pr-conflict) makes the
  resume a merge run: the same session, which built the branch and was compacted to a summary when its
  last run ended, merges origin's default branch by feature-flow merge.md. The state file keeps the
  mode, so every later resume of the topic stays in merge.md even when a re-estimate leaves the key
  out (the class flow ended before the PR); launch refuses it (no session to merge with).
The estimate is recorded per run in <worktree>/.hook-only/runs/est-<n>.json, where run-result.py
and cost-history.py import read it.
State goes to <main>/.hook-only/workers/<topic>.json (agents can't write there).
The prompt still reaches the model, with the result as additional context, so
the planning session can start waiting (skill repo-plan: run-result.py --wait, which reads hooks/lib/runstate.py's
status and returns when the run ends, turns orphan or can't be read).

Automatic resume (command line, not a prompt):
  worker-control.py auto <worktree> <topic> <run>
    run-steps.py's next step (the run's wrap-up) calls it, before exit-<run> is written, when dispatch_guard.py left
    .hook-only/runs/auto-resume-<run>.json (the run stopped only because the planned dispatch didn't
    fit its time or USD; the hook computed what a resume needs). It checks, and writes the decision to
    .hook-only/runs/auto-<run>.json for run-result.py: runstate.status of the run says it ended `STATUS:
    blocked`, wasn't stopped, and no agent-guard ALERT or HOLD is pending; the record has minutes and USD within
    worker_auto_resume_max_minutes / _usd, and fewer than worker_auto_resume_max automatic resumes ran
    in a row (the state's auto_resumes; a launch or resume typed by the user resets it). The resume gets
    the larger of the record and what is left of the estimate of the last run a person or the queue started
    (runstate.leftOfEstimate: its estimate minus the minutes and USD of runs k..<run>), capped at
    worker_auto_resume_max_minutes / _usd: a record covers only the one dispatch that didn't fit, so the
    resumed run would stop again at the next one. On "resume" it starts `auto-start` detached and returns.
  worker-control.py auto-merge <worktree> <topic> <run>
    run-steps.py's next step calls it when the only reason auto-merge.py didn't merge a `STATUS: done` run is
    that the branch
    lacks the latest main (merge-<run>.json "merge_run"). Same checks as auto (state; runstate.status: not
    stopped, no ALERT or HOLD), its own count (the state's auto_merges, below worker_auto_merge_max; a launch or
    resume typed by the user resets it, as for auto_resumes) and a fixed budget,
    worker_auto_merge_minutes / _usd: a merge run's size is the conflicts', which nothing knows before
    the merge. Writes auto-<run>.json with "decision": "merge" and starts auto-start, which resumes the
    session in merge mode (MERGE_PROMPT); the mode stays in the state, as for a merge the user started.
  worker-control.py auto-fix <worktree> <topic> <run>
    run-steps.py's next step calls it when the real data shows a `STATUS: done` run wrong (merge-<run>.json
    "fix_run":
    a failed check of verify.py, or the verifier's "fail"). Same checks as auto-merge, its own count (the
    state's auto_fixes, below worker_auto_fix_max; reset by a run the user starts). The budget is the model's:
    `cost-history.py estimate branch <type/scope> <class> worker_auto_fix_usd worker_auto_fix_minutes --fix` in
    the main checkout (the raw guess corrected by the history of earlier fix runs, plus one round's gate wait for
    each gated agent the class dispatches; the worktree's history copy when the main checkout has none yet). Writes auto-<run>.json "decision": "fix" and
    starts auto-start, which resumes the session with FIX_PROMPT: the worker reads the findings from
    runs/verify-<run>.json and runs/verifier-<run>.json and handles them as review findings.
  A refused auto-merge or auto-fix is an event of kind "refused" (wt_events.py): auto-merge.py recorded the
  refusal as "retry", which the person doesn't act on, so this is where it reaches them.
  Since migration step 5 of the run record each of the three also needs the run record to agree with today's file
  it read (auto: the record's dispatch_stop has the same minutes and USD as auto-resume-<run>.json; auto-merge,
  auto-fix: the record's steps.merge carries merge_run / fix_run like merge-<run>.json); a run without a usable
  record, or a record that differs, refuses with the reason (auto-<run>.json "refused").
  worker-control.py auto-start <worktree> <topic> <run>
    waits for run <run>'s wrapper to exit (a "refused" event when it still runs after 180 s), checks that nothing else started meanwhile and that the run record's
    steps.next holds the decision of auto-<run>.json (migration step 5; otherwise a "refused" event and nothing
    starts), and resumes the
    session with the decision's minutes and USD as the run's estimate (limits budget_headroom_factor above, as
    always) and AUTO_PROMPT (MERGE_PROMPT in merge mode, FIX_PROMPT for a fix run). The main checkout comes
    from WT_MAIN_DIR, which this hook puts in the wrapper's environment. Every way it ends without starting the
    run is a "refused" event (another run in the wt state, the wrapper still running, a crash: also a stderr
    line in the run's log and exit 1).

The approved plan: every run the person starts (`wt launch`, `wt resume`, a queue start) first copies the
worktree's plans/<topic>.md to <main>/.hook-only/approved/<topic>.md. Starting the run is the person's approval
of the plan as it stands; automatic runs never refresh it, so a plan the worker edits later never becomes its
own acceptance test. verifier.py gives that copy to the verifier.

The run record (hooks/lib/runstate.py, migration step 2: dual write): every file this hook writes in runs/ is
also written into the run's record, after the file and through runstate.dualWrite, so a record write that
fails is reported once (a hook-failed event in the worktree) and changes nothing here: est-<run>.json -> the
record itself (who started it: person, queue, auto-resume, auto-merge, auto-fix; purpose, prompt kind, flow,
estimate, launch); the state's pid -> process (the wrapper's facts for verifyProcess); stopped-<run> -> stop;
auto-<run>.json -> steps.next (read back from the file, as the legacy adapter reads it); approve-<run>.json ->
approve. A wrapper that can't be started (Popen fails) ends the
record launch-failed; the error still goes on as before. Every program started detached for a run (the wrapper,
auto-start, `wt merge`'s and `wt wrapup`'s run-steps.py) has its output appended to that run's
runs/stderr-<run>.log (runLog), never dropped. Since migration step 4 every decision here reads
runstate.status (the record where one exists, else today's files through the legacy adapter), and a new run's
number is runstate.nextRun (records and today's files, est- included).
"""
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import config_mode_guard  # noqa: E402
import homeconfig  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
import wt_controls  # noqa: E402
import wt_topics  # noqa: E402

WRAPPER = Path.home() / ".claude/hooks/worker/run-worker.sh"
COMMAND = re.compile(r"^\s*wt\s+(launch|resume|stop)\s+([a-z0-9][a-z0-9-]{0,63})\s*$")
MERGE = re.compile(r"^\s*wt\s+merge\s+([a-z0-9][a-z0-9-]{0,63})\s*$")
QUEUE = re.compile(r"^\s*wt\s+queue(?:\s+drop\s+[a-z0-9][a-z0-9-]{0,63}|(?:\s+[a-z0-9][a-z0-9-]{0,63})*)\s*$")
STATUS = re.compile(r"^\s*wt\s+status(?:\s+([a-z0-9][a-z0-9-]{0,63}))?\s*$")
WRAPUP = re.compile(r"^\s*wt\s+wrapup\s+([a-z0-9][a-z0-9-]{0,63})\s*$")
RECONCILE = re.compile(r"^\s*wt\s+reconcile(?:\s+([a-z0-9][a-z0-9-]{0,63}))?(\s+--dry-run)?\s*$")
UUID = wt_topics.UUID
RESUME_PROMPT = (
    "The plan was updated by the planning session. Re-read plans/{topic}.md, "
    "load the class step files again (feature-flow, Mode: one cat; add merge.md if you stopped inside it) "
    "and continue from the step where you stopped."
)
AUTO_PROMPT = (
    "Automatic resume: your last run stopped only because the planned dispatch didn't fit the run's time or "
    "budget (dispatch-guard); nothing was decided in between. Re-read plans/{topic}.md, load the class step "
    "files again (feature-flow, Mode: one cat; add merge.md if you stopped inside it) and make the planned "
    "dispatch."
)
SCRIPTED_PROMPT = (
    "Scripted flow for {topic} (plans/worker/{topic}.json \"flow\": \"scripted\"): load your rules with one Bash "
    "call, `cat ~/.claude/skills/feature-flow/scripted.md`, then Read .hook-only/flow/next.md and do what it says."
)
SCRIPTED_RESUME_PROMPT = (
    "The planning session updated the plan or the flow hook. Load your rules with one Bash call, "
    "`cat ~/.claude/skills/feature-flow/scripted.md`, then Read .hook-only/flow/next.md and do what it says."
)
SCRIPTED_AUTO_PROMPT = (
    "Automatic resume: your last run stopped only because the planned dispatch didn't fit the run's time or "
    "budget (dispatch-guard); nothing was decided in between. Load your rules with one Bash call, "
    "`cat ~/.claude/skills/feature-flow/scripted.md`, then Read .hook-only/flow/next.md and make that call."
)
FLOW_NEXT = Path.home() / ".claude/hooks/worker/flow-next.py"
RUN_RESULT = Path.home() / ".claude/skills/repo-plan/run-result.py"
RUN_STEPS = Path(__file__).resolve().parent / "run-steps.py"  # `wt merge`: the merge step and what follows it
MERGE_PROMPT = (
    "Merge mode: this branch conflicts with the integration branch (origin's default branch, or the local "
    "main in local mode; skill pr-conflict, or auto-merge.py found the branch behind the local main). "
    "Your class flow is finished; don't load its step files. Re-read plans/{topic}.md, "
    "load the merge steps with one Bash call: "
    "`cat ~/.claude/skills/feature-flow/agents.md ~/.claude/skills/feature-flow/merge.md`, "
    "and follow merge.md (its first lines say where to start)."
)


FIX_PROMPT = (
    "Fix run: your last run ended `STATUS: done`, but the hooks' real-data checks after it showed the branch "
    "wrong. The findings are in .hook-only/runs/verify-{run}.json (the mechanical checks: the ones with \"ok\": "
    "false) and .hook-only/runs/verifier-{run}.json (the verifier agent's report: its `fail` lines); read the ones "
    "that exist with one Bash `cat`. Re-read plans/{topic}.md, load the class step files again (feature-flow, "
    "Mode: one cat) and treat each finding as a must-fix review finding (test-first.md, 4. Review, step 5; "
    "small-fix.md likewise): a fixture that reproduces the case goes to qa first, the fix to developer, then a "
    "new code-reviewer round for the change (record its id under ## Reviewer). You can't read the real data: "
    "build the fixtures from the rows and values the findings name. A finding the plan contradicts: change no "
    "code for it and end `STATUS: needs-decision` quoting the plan and the finding. Then wrap up as usual; the "
    "checks run again after your `STATUS: done`."
)
COST_HISTORY = Path.home() / ".claude/skills/repo-plan/cost-history.py"
SOURCE = "worker-control"
STARTED_BY = ("person", "queue")  # who may start a run through the prompt (wt_queue.py passes "queue")


def done(context=None, block=None):
    """The answer to a wt prompt; while the config mode isn't DEV its line ends every answer
    (config_mode_guard.statusLine)."""
    mode = config_mode_guard.statusLine()
    tail = f"\n{mode}" if mode else ""
    if block:
        print(json.dumps({"decision": "block", "reason": f"wt: {block}{tail}"}))
    elif context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": f"wt: {context}{tail}"}}))
    sys.exit(0)


alive = wt_controls.alive


def is_our_wrapper(pid, topic):
    return wt_controls.isOurWrapper(pid, topic, WRAPPER)


def scripted(worktree):
    """The topic runs the scripted flow right now (flow-next.py init wrote the marker; off removes it)."""
    return (worktree / ".hook-only" / "flow" / "scripted").is_file()


def scripted_off(worktree):
    """A run the scripted flow doesn't drive (a resume the person started, a fix run, merge mode): back to
    the written flow for the rest of the topic (flow-next.py off)."""
    if scripted(worktree):
        subprocess.run([sys.executable, str(FLOW_NEXT), "off", str(worktree)], capture_output=True)


def scripted_runnable(worktree):
    """The scripted flow can go on after a resume: a stop is first retried with the current hook code
    (flow-next.py retry: only a local recomputation; a stop that waits for a decision stops again), then
    flow-next.py runnable decides."""
    if not scripted(worktree):
        return False
    subprocess.run([sys.executable, str(FLOW_NEXT), "retry", str(worktree)], capture_output=True)
    return subprocess.run([sys.executable, str(FLOW_NEXT), "runnable", str(worktree)],
                          capture_output=True).returncode == 0


def scripted_init(worktree, topic, klass):
    """True when flow-next.py took the topic (logic, wiring, small fix); otherwise the written flow runs."""
    result = subprocess.run([sys.executable, str(FLOW_NEXT), "init", str(worktree), topic, klass],
                            capture_output=True, text=True)
    return result.returncode == 0 and scripted(worktree)


def record_flow_mode(worktree):
    """At a topic's first launch: keep the mode it runs in for all its later runs (the topic record, which
    homeconfig.flowMode reads before the main checkout's), so `wt local on|off` in between never mixes the
    base a branch started from with where it merges."""
    record = worktree / ".hook-only" / "flow-mode"
    if not record.exists():
        mode = homeconfig.flowMode(str(worktree))  # the main checkout's mode now
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(mode + "\n")


def approve_plan(main_dir, worktree, topic):
    """A run the person starts approves the plan as it stands (module doc, "The approved plan")."""
    plan = worktree / "plans" / f"{topic}.md"
    if plan.is_file():
        approved = main_dir / ".hook-only" / "approved"
        approved.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plan, approved / f"{topic}.md")


def runLog(worktree, run):
    """The run's stderr log (run-worker.sh appends its steps' output there): where the output of a program this hook
    starts detached for the run goes, never DEVNULL."""
    return runstate.legacyPath(str(worktree), "stderr", run, ".log")


@contextlib.contextmanager
def runLogOrNull(worktree, run):
    """runLog opened for appending (its directory made first); DEVNULL with one stderr line when it still can't be
    opened, so a launch never fails for want of its log."""
    path = runLog(worktree, run)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        log = open(path, "ab")
    except OSError as error:
        print(f"worker-control: {path} can't be opened ({error}); the started program's output is dropped",
              file=sys.stderr)
        yield subprocess.DEVNULL
        return
    with log:
        yield log


def startAutoStart(worktree, topic, run):
    """`auto-start` detached for the next run, its output appended to this run's stderr log."""
    with runLogOrNull(worktree, run) as log:
        subprocess.Popen([sys.executable, __file__, "auto-start", str(worktree), topic, str(run)],
                         start_new_session=True, env=os.environ.copy(),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)


def start_run(main_dir, worktree, topic, state_file, mode, session_id, prompt, minutes, est_usd, usd,
              cap_minutes, agent, record, extra_state, launch):
    """Write est-<run>.json, start run-worker.sh in its own process group, write the state. Returns (run, pid).
    launch: {"started_by", "prompt_kind", "by_run"?} for the run record (module doc, "The run record").
    Every caller checks config_mode_guard.refusal first and answers it; here it is the last line, a ConfigError
    before anything is written."""
    refused = config_mode_guard.refusal(main_dir)
    if refused:
        raise homeconfig.ConfigError(refused)
    integration = homeconfig.flowMode(str(worktree))  # a ConfigError before anything is written
    runs = worktree / ".hook-only" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run = runstate.nextRun(str(worktree))
    (runs / f"est-{run}.json").write_text(json.dumps({"minutes": minutes, "usd": est_usd, "cap": usd,
                                                      "cap_minutes": cap_minutes, **record}))
    runstate.dualWrite(str(worktree), SOURCE, runstate.launchRecord, run, {
        "topic": topic, "session_id": session_id, "session_mode": mode, "started_by": launch["started_by"],
        "purpose": record.get("mode") if record.get("mode") in ("merge", "fix") else "build",
        "integration_mode": integration,
        "flow": "scripted" if scripted(worktree) else "written",
        "agent": agent or None, "prompt_kind": launch["prompt_kind"],
        "estimate": {"minutes": minutes, "usd": est_usd, "cap_minutes": cap_minutes, "cap_usd": usd,
                     **{k: record[k] for k in ("raw_minutes", "raw_usd", "hook_wait_minutes") if k in record}},
        "launch": {"launched_at": clock.isoNow(), "by_pid": os.getpid(), "by_run": launch.get("by_run")}})
    try:
        # The wrapper's own output (a bash error before it redirects its steps) goes to the log it writes itself.
        with open(runLog(worktree, run), "ab") as log:
            process = subprocess.Popen(
                [str(WRAPPER), topic, str(worktree), str(run), mode, session_id,
                 str(cap_minutes), str(usd), agent, prompt],
                cwd=worktree, start_new_session=True, env={**os.environ, "WT_MAIN_DIR": str(main_dir)},
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    except OSError as error:
        # The record says the launch failed; the error goes on exactly as before (no exit file today).
        runstate.dualWrite(str(worktree), SOURCE, runstate.recordLaunchFailed, run, f"{type(error).__name__}: {error}")
        raise
    state_file.write_text(json.dumps({
        "pid": process.pid, "session_id": session_id, "run": run, "worktree": str(worktree),
        "plan": str(worktree / "plans" / f"{topic}.md"), "started": time.time(), **extra_state}))
    runstate.dualWrite(str(worktree), SOURCE, runstate.recordSpawned, run, process.pid)
    return run, process.pid


def leftOfEstimate(worktree, topic, run, mainDir):
    """(minutes, usd) left of the estimate of the last run a person or the queue started, after the runs since
    then (runstate.leftOfEstimate); (0, 0) when the run state can't tell."""
    try:
        left = runstate.leftOfEstimate(str(worktree), topic, run, mainDir=str(mainDir))
    except Exception:
        return 0.0, 0.0
    return left if left is not None else (0.0, 0.0)


def auto(worktree, topic, run):
    """The automatic-resume check (module doc). Writes .hook-only/runs/auto-<run>.json either way."""
    runs = worktree / ".hook-only" / "runs"
    decision = runs / f"auto-{run}.json"

    def refuse(reason):
        decision.write_text(json.dumps({"run": run, "decision": "refused", "reason": reason}))
        runstate.dualWrite(str(worktree), SOURCE, runstate.recordStepFromFile, run, "next")
        sys.exit(0)

    main_dir = Path(os.environ.get("WT_MAIN_DIR") or "/nonexistent")
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    try:
        state = json.loads(state_file.read_text())
        record = json.loads((runs / f"auto-resume-{run}.json").read_text())
        defaults = homeconfig.load("budget")
    except (OSError, ValueError, homeconfig.ConfigError) as error:
        refuse(f"can't read the state, the record or the defaults: {error}")
    if state.get("run") != run or not UUID.match(str(state.get("session_id") or "")):
        refuse(f"the state is for run {state.get('run')}, not {run}")
    status = runFacts(main_dir, worktree, topic, run, refuse)
    if status["stop"]:
        refuse("the run was stopped with wt stop")
    if runstate.workerStatus((status["claude"] or {}).get("status")) != "blocked":
        refuse("the run didn't end with STATUS: blocked")
    if status["held"]["state"] != "clear":
        refuse("an agent-guard ALERT or HOLD is pending: the user decides")
    count, limit = int(state.get("auto_resumes", 0)), int(defaults["worker_auto_resume_max"])
    if count >= limit:
        refuse(f"{count} automatic resume(s) in a row already (worker_auto_resume_max {limit})")
    minutes, usd = record.get("minutes"), record.get("usd")
    if minutes is None or usd is None:
        refuse("the dispatch had no USD budget, so the resume can't be bounded")
    if minutes > float(defaults["worker_auto_resume_max_minutes"]) \
            or usd > float(defaults["worker_auto_resume_max_usd"]):
        refuse(f"needs {minutes} min and ${usd}, above worker_auto_resume_max_minutes / _usd")
    recorded = recordedFact(status, run, "dispatch_stop", refuse) or {}
    if (recorded.get("minutes"), recorded.get("usd")) != (minutes, usd):
        refuse(f"the run record's dispatch stop ({recorded.get('minutes')} min, ${recorded.get('usd')}) differs from "
               f"auto-resume-{run}.json ({minutes} min, ${usd})")
    leftMinutes, leftUsd = leftOfEstimate(worktree, topic, run, main_dir)
    minutes = math.ceil(min(max(minutes, leftMinutes), float(defaults["worker_auto_resume_max_minutes"])))
    usd = round(min(max(usd, leftUsd), float(defaults["worker_auto_resume_max_usd"])), 2)
    decision.write_text(json.dumps({"run": run, "decision": "resume", "next_run": run + 1,
                                    "minutes": minutes, "usd": usd}))
    runstate.dualWrite(str(worktree), SOURCE, runstate.recordStepFromFile, run, "next")
    startAutoStart(worktree, topic, run)


def runFacts(mainDir, worktree, topic, run, refuse):
    """runstate.status of run for the automatic follow-ups (auto, auto-merge, auto-fix); refuse(reason) when the
    run state can't be read. No pid check: the run's own wrapper calls these."""
    try:
        return runstate.status(str(mainDir), topic, root=str(worktree), run=run)
    except (homeconfig.ConfigError, OSError, KeyError, ValueError) as error:
        refuse(f"can't read the run state: {error}")


def recordedFact(status, run, key, refuse):
    """A fact of the run record that an automatic run's start needs (migration step 5: the record must agree with
    today's file it was written with); refuse(reason) for a run without a usable record."""
    if status["phase"] == "unknown" or status["source"] != "record":
        refuse(f"run {run} has no usable run record ({status.get('error') or 'none'}): no automatic run")
    return status[key]


def mergeFlag(status, run, flag, refuse):
    """The record's merge step asked for flag (merge_run, fix_run) as merge-<run>.json does, else refuse."""
    merge = (recordedFact(status, run, "steps", refuse) or {}).get("merge") or {}
    if not (merge.get("flags") or {}).get(flag):
        refuse(f"the run record's merge step didn't ask for a {flag.replace('_', ' ')} "
               f"({merge.get('outcome') or 'no merge step'})")


def recordedNextDiffers(mainDir, worktree, topic, run, decision):
    """Why auto-start must not start the decided run (migration step 5), None when the run record's next step holds
    the same decision as auto-<run>.json: a run without a usable record, or another next step."""
    try:
        status = runstate.status(str(mainDir), topic, root=str(worktree), run=run)
    except (homeconfig.ConfigError, OSError, KeyError, ValueError) as error:
        return f"can't read the run state: {error}"
    if status["phase"] == "unknown" or status["source"] != "record":
        return f"run {run} has no usable run record ({status.get('error') or 'none'})"
    recorded = ((status["steps"] or {}).get("next") or {}).get("outcome")
    if recorded != decision:
        return f"the run record's next step is {recorded!r}, auto-{run}.json says {decision!r}"
    return None


def refused_event(topic, what, reason):
    """auto-merge.py recorded a refusal the flow handles ("retry"); when the flow can't, the person must know."""
    main_dir = os.environ.get("WT_MAIN_DIR")
    if main_dir:
        import wt_events
        wt_events.emit(main_dir, "refused", topic, f"no automatic {what} run: {reason}")


def auto_merge(worktree, topic, run):
    """The automatic merge run after auto-merge.py found the branch behind main (module doc)."""
    runs = worktree / ".hook-only" / "runs"
    decision = runs / f"auto-{run}.json"

    def refuse(reason):
        decision.write_text(json.dumps({"run": run, "decision": "refused", "reason": reason}))
        runstate.dualWrite(str(worktree), SOURCE, runstate.recordStepFromFile, run, "next")
        refused_event(topic, "merge", reason)
        sys.exit(0)

    main_dir = Path(os.environ.get("WT_MAIN_DIR") or "/nonexistent")
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    try:
        state = json.loads(state_file.read_text())
        merged = json.loads((runs / f"merge-{run}.json").read_text())
        defaults = homeconfig.load("budget")
    except (OSError, ValueError, homeconfig.ConfigError) as error:
        refuse(f"can't read the state, the merge decision or the defaults: {error}")
    if state.get("run") != run or not UUID.match(str(state.get("session_id") or "")):
        refuse(f"the state is for run {state.get('run')}, not {run}")
    if not merged.get("merge_run"):
        refuse("auto-merge.py didn't ask for a merge run")
    status = runFacts(main_dir, worktree, topic, run, refuse)
    if status["stop"]:
        refuse("the run was stopped with wt stop")
    if status["held"]["state"] != "clear":
        refuse("an agent-guard ALERT or HOLD is pending: the user decides")
    count, limit = int(state.get("auto_merges", 0)), int(defaults["worker_auto_merge_max"])
    if count >= limit:
        refuse(f"{count} automatic merge run(s) in a row already (worker_auto_merge_max {limit}): main keeps moving")
    minutes = math.ceil(float(defaults["worker_auto_merge_minutes"]))
    usd = round(float(defaults["worker_auto_merge_usd"]), 2)
    if minutes <= 0 or usd <= 0:
        refuse(f"worker_auto_merge_minutes / _usd must be above 0 ({homeconfig.where('budget')})")
    mergeFlag(status, run, "merge_run", refuse)
    decision.write_text(json.dumps({"run": run, "decision": "merge", "next_run": run + 1,
                                    "minutes": minutes, "usd": usd}))
    runstate.dualWrite(str(worktree), SOURCE, runstate.recordStepFromFile, run, "next")
    startAutoStart(worktree, topic, run)


def fix_estimate(main_dir, worktree, topic, defaults):
    """The fix run's estimate (module doc, auto-fix): the plans/worker json cost-history.py prints."""
    config = json.loads((main_dir / "plans" / "worker" / f"{topic}.json").read_text())
    klass = config["class"]
    branch = subprocess.run(["git", "-C", str(worktree), "symbolic-ref", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    scope = "/".join(branch.split("/")[:2])
    usd, minutes = float(defaults["worker_auto_fix_usd"]), float(defaults["worker_auto_fix_minutes"])
    where = main_dir if (main_dir / ".claude-cost" / "history.tsv").is_file() else worktree
    out = subprocess.run([sys.executable, str(COST_HISTORY), "estimate", "branch", scope, klass, f"{usd:g}",
                          f"{minutes:g}", "--fix"], cwd=where, capture_output=True, text=True)
    marker = "plans/worker/<topic>.json: "
    line = next((l for l in out.stdout.splitlines() if l.startswith(marker)), None)
    if out.returncode != 0 or line is None:
        raise ValueError(f"cost-history.py estimate failed: {(out.stderr or out.stdout).strip()[-300:]}")
    return json.loads(line[len(marker):])


def auto_fix(worktree, topic, run):
    """The automatic fix run after the real data showed a finished branch wrong (module doc)."""
    runs = worktree / ".hook-only" / "runs"
    decision = runs / f"auto-{run}.json"

    def refuse(reason):
        decision.write_text(json.dumps({"run": run, "decision": "refused", "reason": reason}))
        runstate.dualWrite(str(worktree), SOURCE, runstate.recordStepFromFile, run, "next")
        refused_event(topic, "fix", reason)
        sys.exit(0)

    main_dir = Path(os.environ.get("WT_MAIN_DIR") or "/nonexistent")
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    try:
        state = json.loads(state_file.read_text())
        merged = json.loads((runs / f"merge-{run}.json").read_text())
        defaults = homeconfig.load("budget")
    except (OSError, ValueError, homeconfig.ConfigError) as error:
        refuse(f"can't read the state, the merge decision or the defaults: {error}")
    if state.get("run") != run or not UUID.match(str(state.get("session_id") or "")):
        refuse(f"the state is for run {state.get('run')}, not {run}")
    if not merged.get("fix_run"):
        refuse("auto-merge.py didn't ask for a fix run")
    status = runFacts(main_dir, worktree, topic, run, refuse)
    if status["stop"]:
        refuse("the run was stopped with wt stop")
    if status["held"]["state"] != "clear":
        refuse("an agent-guard ALERT or HOLD is pending: the user decides")
    count, limit = int(state.get("auto_fixes", 0)), int(defaults["worker_auto_fix_max"])
    if count >= limit:
        refuse(f"{count} automatic fix run(s) in a row already (worker_auto_fix_max {limit}): the findings keep "
               "coming back")
    mergeFlag(status, run, "fix_run", refuse)
    try:
        estimate = fix_estimate(main_dir, worktree, topic, defaults)
    except (OSError, ValueError, KeyError) as error:
        refuse(f"no estimate for the fix run: {error}")
    decision.write_text(json.dumps({"run": run, "decision": "fix", "next_run": run + 1,
                                    "minutes": int(estimate["minutes"]), "usd": float(estimate["est_usd"]),
                                    **{k: estimate[k] for k in ("raw_minutes", "raw_usd", "hook_wait_minutes")
                                       if k in estimate}}))
    runstate.dualWrite(str(worktree), SOURCE, runstate.recordStepFromFile, run, "next")
    startAutoStart(worktree, topic, run)


def autoStartTold(worktree, topic, run):
    """auto-start with every failure told: an exception is a stderr line (the run's log) and a "refused" event,
    never a silent end (each give-up path inside auto_start has its own event)."""
    try:
        auto_start(worktree, topic, run)
    except Exception as error:
        text = f"auto-start crashed: {type(error).__name__}: {error}"[:300]
        print(f"worker-control: {topic} run {run}: {text}", file=sys.stderr)
        refused_event(topic, "start", f"{text}; nothing started")
        sys.exit(1)


def auto_start(worktree, topic, run):
    runs = worktree / ".hook-only" / "runs"
    main_dir = Path(os.environ["WT_MAIN_DIR"])
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    state = json.loads(state_file.read_text())
    refused = config_mode_guard.refusal(main_dir)
    if refused:
        refused_event(topic, "start", refused)
        return
    for _ in range(360):
        if not (state.get("pid") and alive(state["pid"]) and is_our_wrapper(state["pid"], topic)):
            break
        time.sleep(0.5)
    else:
        refused_event(topic, "start", f"run {run}'s wrapper (pid {state.get('pid')}) still ran after 180 s; "
                                      "nothing started")
        return
    state = json.loads(state_file.read_text())
    if state.get("run") != run:
        refused_event(topic, "start", f"the wt state names run {state.get('run')}, not {run}: another run started "
                                      "meanwhile; nothing started")
        return
    record = json.loads((runs / f"auto-{run}.json").read_text())
    differs = recordedNextDiffers(main_dir, worktree, topic, run, record.get("decision"))
    if differs:
        refused_event(topic, str(record.get("decision") or "next"), differs)
        return
    defaults = homeconfig.load("budget")
    headroom = float(defaults["budget_headroom_factor"])
    minutes, est_usd = int(record["minutes"]), float(record["usd"])
    config_file = main_dir / "plans" / "worker" / f"{topic}.json"
    try:
        config = json.loads(config_file.read_text())
    except (OSError, ValueError):
        config = {}
    merging = record.get("decision") == "merge"
    fixing = record.get("decision") == "fix"
    counts = {key: int(state.get(key, 0)) for key in ("auto_resumes", "auto_merges", "auto_fixes")}
    resumeScripted = not fixing and not merging and state.get("mode") != "merge" and scripted(worktree)
    if not resumeScripted:
        scripted_off(worktree)
    if fixing:
        # "mode" only in the run's estimate (cost-history.py import: a fix row), never in the state: the
        # session continues its class flow, not a merge.
        prompt, kind, keep = FIX_PROMPT.format(topic=topic, run=run), {"mode": "fix"}, {}
        counts["auto_fixes"] += 1
        launch = {"started_by": "auto-fix", "prompt_kind": "fix"}
    else:
        merge = merging or state.get("mode") == "merge"
        kind = keep = {"mode": "merge"} if merge else {}
        prompt = (MERGE_PROMPT if merge else SCRIPTED_AUTO_PROMPT if resumeScripted else AUTO_PROMPT).format(topic=topic)
        counts["auto_merges" if merging else "auto_resumes"] += 1
        launch = {"started_by": "auto-merge" if merging else "auto-resume",
                  "prompt_kind": "merge" if merge else "scripted-auto" if resumeScripted else "auto"}
    raw = {k: record[k] for k in ("raw_minutes", "raw_usd", "hook_wait_minutes") if k in record}
    start_run(main_dir, worktree, topic, state_file, "resume", state["session_id"], prompt, minutes, est_usd,
              round(est_usd * headroom, 2), math.ceil(minutes * headroom), config.get("agent", ""),
              {"auto": True, **kind, **raw}, {**keep, **counts}, {**launch, "by_run": run})


def queue_command(prompt, main_dir):
    """`wt queue ...` (module doc). None of it enters the session: the answer is shown to the person only
    (a block), so a queue press costs no model turn. Nobody waits on queued runs in the
    planning session; auto-merge, auto-finish, plans/friction.md, the pane and `wt queue` carry the results."""
    import wt_queue
    words = prompt.split()[2:]
    refused = config_mode_guard.refusal(main_dir)
    if refused and words[:1] != ["drop"]:  # showing the queue ticks it, and a tick starts workers
        done(block=f"queue: {refused}")
    try:
        if not words:
            # Showing starts what may start too: after a person merges a held topic by hand no run ends,
            # so nothing else would start its dependents.
            started = [r for r in wt_queue.tick(main_dir) if "started" in r]
            rows = wt_queue.status(main_dir)["items"]
            lines = [f"{r['topic']}: {r.get('state', 'error')}"
                     + (f" ({r['why']})" if r.get("why") else "")
                     + (f" run {r['run']}" if r.get("run") else "")
                     + (f" ERROR {r['error']}" if r.get("error") else "") for r in rows]
            done(block=("started now: " + ", ".join(r["topic"] for r in started) + ". " if started else "")
                 + "queue: " + ("; ".join(lines) if lines else "empty"))
        if words[0] == "drop":
            wt_queue.drop(main_dir, words[1])
            done(block=f"queue: {words[1]} dropped")
        wt_queue.add(main_dir, words)
    except wt_queue.Refused as error:
        done(block=f"queue: {error}")
    try:
        ticked = wt_queue.tick(main_dir)
    except homeconfig.ConfigError as error:  # the topics are in the queue already: the answer says so
        configRefusal(prompt, error, f"queued {', '.join(words)}, but nothing starts while a setting can't be read")
    report = []
    for row in ticked:
        if "started" in row:
            report.append(f"{row['topic']} started: {row['started']}")
        elif "waits" in row:
            report.append(f"{row['topic']} waits: {row['waits']}")
        else:
            report.append(f"{row['topic']} not started: {row['error']}")
    done(block="queued " + ", ".join(words) + ". " + " | ".join(report)
         + ". The rest start by themselves as runs end; `wt queue` or the pane shows the queue.")


def statusCommand(topic, mainDir):
    """`wt status [<topic>]` (module doc): display only, nothing decides on it (new code: camelCase, H2)."""
    topics = [topic] if topic else wt_topics.topics(mainDir)
    if not topics:
        done(context="status: no topic has a worktree, an estimate or a wt state")
    try:
        views = [runstate.agent(runstate.status(str(mainDir), t, pidAlive=runstate.verifyProcess)) for t in topics]
    except (homeconfig.ConfigError, OSError, KeyError) as error:
        done(block=f"status: the run state can't be read: {error}")
    done(context="status (hooks/lib/runstate.py, pid checked outside the sandbox; relay it, nothing was "
                 "changed):\n\n" + "\n\n".join(views))


def reconcileCommand(topic, dryRun, mainDir):
    """`wt reconcile [<topic>] [--dry-run]` (module doc): run-steps.py reconcile, its result as context (new code:
    camelCase, H2)."""
    target = str(wt_topics.worktreeOf(mainDir, topic)) if topic else "--all"
    # From the main checkout: run-steps.py finds it there (the repository holding the worktree or the cwd).
    out = subprocess.run([sys.executable, str(RUN_STEPS), "reconcile", target] + (["--dry-run"] if dryRun else []),
                         capture_output=True, text=True, cwd=str(mainDir))
    if out.returncode != 0:
        done(block=f"reconcile: {(out.stderr or out.stdout).strip()[-400:]}")
    what = "no step was run or changed" if dryRun else "each action as the did: line says"
    done(context=f"reconcile{' --dry-run' if dryRun else ''} (run-steps.py reconcile, process checks outside the "
                 f"sandbox; relay it; {what}):\n\n" + out.stdout.strip())


def is_approving(pid, script, topic, run):
    """Whether pid is a live `run-steps.py step <worktree> <run> merge --topic <topic> --person` (module doc,
    wt merge)."""
    if not isinstance(pid, int) or not alive(pid):
        return False
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return script.encode() in argv and topic.encode() in argv and str(run).encode() in argv


def checkedStatus(mainDir, worktree, topic, prefix="", run=None):
    """runstate.status of the topic's run with the pid check this hook can make outside the sandbox
    (runstate.verifyProcess); a state that can't be read blocks the prompt with the reason (prefix first)."""
    try:
        return runstate.status(str(mainDir), topic, root=str(worktree), pidAlive=runstate.verifyProcess, run=run)
    except (homeconfig.ConfigError, OSError, KeyError, ValueError) as error:
        done(block=f"{prefix}{topic}: the run state can't be read ({error}); nothing was started or stopped")


def startSteps(argv, mainDir, worktree, run):
    """Start a run-steps.py command detached in its own session (wt merge, wt wrapup), with the main checkout as
    WT_MAIN_DIR; its outcome goes to the run record and the events, not to this prompt, its output to the run's
    stderr log."""
    with runLogOrNull(worktree, run) as log:
        return subprocess.Popen(argv, start_new_session=True, env={**os.environ, "WT_MAIN_DIR": str(mainDir)},
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log)


def wrapperGone(topic, status):
    """Whether the run's wrapper is confirmed gone: an orphan's is checked by status (pidAlive); a run that ended has
    an exit, so it is checked here (its wrapper exits right after the wrap-up)."""
    if status["phase"] == "orphan":
        return status["confidence"] == "confirmed"
    return runstate.verifyProcess(status["process"] or {}, topic, status["run"]) is False


def wrapupRunning(worktree, run):
    """Whether a live process runs `run-steps.py wrapup <worktree> <run>` (wt wrapup, module doc)."""
    wanted = [str(RUN_STEPS).encode(), b"wrapup", str(worktree).encode(), str(run).encode()]
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if all(word in argv for word in wanted):
            return True
    return False


def wrapupCommand(topic, mainDir):
    """`wt wrapup <topic>` (module doc)."""
    worktree = wt_topics.worktreeOf(mainDir, topic)
    if not (worktree / ".git").is_file():
        done(block=f"wrapup: {topic} has no worktree")
    status = checkedStatus(mainDir, worktree, topic, "wrapup: ")
    run, kept = status["run"], status["resumable"] or {}
    if kept.get("code") != "wrapup-left":
        done(block=f"wrapup: {topic} has no run with a kept wrap-up: {runstate.human(status, 'en')['line']}")
    process = status["process"] or {}
    if not wrapperGone(topic, status):
        done(block=f"wrapup: {topic} run {run}'s wrapper could not be confirmed gone (no pid recorded, or /proc "
                   f"can't be read); nothing was started (`wt status {topic}` shows the check)")
    evidence = {"wrapper_pid": process.get("wrapper_pid"), "checked": "gone",
                "reasons": status.get("orphan_reasons") or []}
    argv = [sys.executable, str(RUN_STEPS), "wrapup", str(worktree), str(run), "--topic", topic, "--continue-stopped"]
    record = runstate.readRecord(str(worktree), run)[0] or {}
    stopped, continued = runstate.stoppedRun(record), runstate.stopDuringWrapup(record)
    refuseUsedUp(mainDir, worktree, topic, run, record, kept["steps_left"])
    if status["phase"] == "orphan":
        runstate.dualWrite(str(worktree), SOURCE, runstate.confirmOrphan, run, SOURCE, evidence)
    # The record's lock (not held by confirmOrphan any more) makes check-and-start one step for two presses.
    with statefile.locked(str(worktree), runstate.RECORD, run):
        if wrapupRunning(worktree, run):
            done(block=f"wrapup: {topic} run {run}'s wrap-up is already running; its result comes as an event")
        try:
            startSteps(argv, mainDir, worktree, run)
        except OSError as error:
            done(block=f"wrapup: {topic} run {run}'s wrap-up could not be started ({error})")
    if continued:
        expected = ("auto-merge refuses the merge because the run was stopped (shown as an event), and the run ends; "
                    f"then review it and approve the merge with `wt merge {topic}` (or the button): your approval "
                    "passes a stop that came after the work was done, every other check still applies.")
    elif stopped:
        expected = "its steps skip, since claude's part was stopped, and the run ends stopped."
    else:
        expected = ("auto-merge refuses the merge because the run's wrapper had ended (shown as an event), and "
                    f"the run ends; then review it and approve the merge with `wt merge {topic}` (or the button).")
    done(context=f"wrapup: started the wrap-up of {topic} run {run} in the background "
                 f"({', '.join(kept['steps_left'])}). Watch the pane row or `wt status {topic}`. Expected: {expected}")


def refuseUsedUp(mainDir, worktree, topic, run, record, left):
    """`wt wrapup` (module doc): every step left has used all its attempts, so a wrap-up would start nothing: the
    refusal says so, and the person's step-at-limit event is written once per run (reconciler.tellLimitOnce)."""
    attempts = runstate.settings()["step_attempts_max"]
    usedUp = runstate.stepsUsedUp(record, attempts)
    if not usedUp or set(usedUp) != set(left):
        return
    import reconciler

    why = f"{', '.join(usedUp)} used all {attempts} attempt(s) (step_attempts_max)"
    reconciler.tellLimitOnce(str(mainDir), str(worktree), run, topic, f"{why} and stay owed")
    done(block=f"wrapup: {topic} run {run}: nothing left to run: {why}; a wrap-up would start nothing. Read why "
               f"each failed (`wt status {topic}`) and decide")


def checkedFlowMode(worktree, topic, prefix=""):
    """The topic's flow mode (homeconfig.flowMode); one that can't be read blocks the prompt with the reason."""
    try:
        return homeconfig.flowMode(str(worktree))
    except homeconfig.ConfigError as error:
        done(block=f"{prefix}{topic}: the flow mode can't be read ({error}); nothing was started or stopped")


def wrapupStillRunning(worktree, run):
    """What of run's wrap-up still runs (module doc, wt merge): a live `run-steps.py wrapup` of it, or the steps
    recorded running whose runner or own program lives (reconciler.heldSteps); "" when nothing does."""
    import reconciler

    record = runstate.readRecord(str(worktree), run)[0] or {}
    held = reconciler.heldSteps(str(worktree), run, record.get("steps") or {}, runstate.STEP_ORDER)
    if held:
        return f"step {', '.join(held)}"
    return "run-steps.py wrapup" if wrapupRunning(worktree, run) else ""


def refuseWhileWrapupRuns(worktree, topic, run):
    """`wt merge` while run's wrap-up still runs (module doc): refused with what runs; its own merge step decides
    first, after the steps before it."""
    running = wrapupStillRunning(worktree, run)
    if running:
        done(block=f"merge: {topic} run {run}'s wrap-up is still running ({running}); its merge step decides once "
                   f"the steps before it have ended, and only a refused merge can be approved: if it is refused, "
                   f"approve it then with `wt merge {topic}`")


def merge_command(topic, main_dir):
    """`wt merge <topic>` (module doc)."""
    worktree = wt_topics.worktreeOf(main_dir, topic)
    if not (worktree / ".git").is_file():
        done(block=f"merge: {topic} has no worktree")
    if checkedFlowMode(worktree, topic, "merge: ") != "local":
        done(block=f"merge: {topic} runs in PR mode; merge its PR instead")
    status = checkedStatus(main_dir, worktree, topic, "merge: ")
    run = status["run"]
    if run is None:
        done(block=f"merge: {topic} has no run yet")
    refuseWhileWrapupRuns(worktree, topic, run)
    runs = worktree / ".hook-only" / "runs"
    if (status["resumable"] or {}).get("code") == "wrapup-left":
        done(block=f"merge: {topic} run {run}'s wrap-up never ran, nothing to approve yet: type "
                   f"`wt wrapup {topic}` first")
    if status["phase"] == "orphan":
        done(block=f"merge: {topic} run {run} never finished its wrap-up, nothing to approve: "
                   f"{runstate.human(status, 'en')['line']}")
    if status["phase"] != "ended" or runstate.verifyProcess(status["process"] or {}, topic, run) is True:
        done(block=f"merge: {topic} run {run} is still going ({status['phase']})")
    outcome = status["outcome"] or {}
    if not outcome.get("merge"):
        done(block=f"merge: no automatic-merge decision for {topic} run {run} (only a refused one can be approved)")
    if outcome["merge"] != "refused":
        done(block=f"merge: {topic} run {run} was {outcome['merge']}, nothing to approve")
    if (outcome.get("merge_flags") or {}).get("merge_run"):
        if status["stop"]:  # worker-control auto-merge refuses a stopped run: no merge run starts
            done(block=f"merge: {topic} run {run} needs the latest main in its branch first, and a stopped run gets no "
                       f"automatic merge run: merge main into the branch yourself, then `wt merge {topic}` again")
        done(block=f"merge: {topic} needs a merge run first (the branch lacks the latest main); it starts by itself")
    decision = {"reason": str((status["merge"] or {}).get("reason") or "")}
    script = str(RUN_STEPS)
    record_file = wt_controls.approvalPath(worktree, run)
    with open(runs / "approve.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = checkedStatus(main_dir, worktree, topic, "merge: ", run=run)
        if now["approving"] and is_approving((now["approve"] or {}).get("pid"), script, topic, run):
            done(block=f"merge: {topic} run {run} is already being approved (since "
                       f"{now['approving']['since']}); the result comes as an event")
        refuseWhileWrapupRuns(worktree, topic, run)
        proc = startSteps([sys.executable, script, "step", str(worktree), str(run), "merge", "--topic", topic,
                           "--person"], main_dir, worktree, run)
        tmp = record_file.with_name(record_file.name + ".tmp")
        tmp.write_text(json.dumps({"run": run, "pid": proc.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                   "started_epoch": time.time()}))
        os.replace(tmp, record_file)
        runstate.dualWrite(str(worktree), SOURCE, runstate.recordApprove, run, proc.pid)
    done(block=f"merge: approving {topic} run {run} (refused: {decision.get('reason', '')[:120]}); the tests run "
               "first, then the merge and the finish; the result comes as an event (toast, your next prompt)")


def stopRun(worktree, topic, status):
    """`wt stop` (module doc): a signal only to the run's wrapper as runstate.verifyProcess confirms it, or, when the
    wrapper is gone, to the wrap-up a person or the reconciler started (stopWrapup)."""
    run, process = status["run"], status["process"] or {}
    if status["phase"] not in runstate.ACTIVE:
        stopWrapup(worktree, topic, status)
        done(block=f"{topic} has no running worker: {runstate.human(status, 'en')['line']}")
    verified = runstate.verifyProcess(process, topic, run)
    if verified is not True:
        why = ("its recorded wrapper pid is not run-worker.sh of this topic and run (or its start time differs)"
               if verified is False else "no wrapper pid is recorded for it, or /proc can't be read")
        done(block=f"{topic} run {run}: nothing was signalled: {why}; `wt status {topic}` shows the evidence")
    pid = process["wrapper_pid"]  # run-worker.sh starts in its own session: its pid is its process group
    # run-result.py reads this marker: a stopped run says nothing about estimating.
    (worktree / ".hook-only" / "runs" / f"stopped-{run}").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    stopped = runstate.dualWrite(str(worktree), SOURCE, runstate.recordStop, run, "person")
    if notStartedYet(stopped):
        done(context=f"stopped {topic} run {run} before its wrapper had started: no signal was sent, the wrapper "
                     f"ends the run as stopped when it starts; its status: `wt status {topic}`")
    os.killpg(pid, signal.SIGINT)
    for _ in range(20):
        if runstate.verifyProcess(process, topic, run) is not True:
            break
        time.sleep(0.5)
    else:
        os.killpg(pid, signal.SIGTERM)
    done(context=f"stopped {topic} run {run}; its status: `wt status {topic}`, or "
                 f"`python3 {RUN_RESULT} {topic} {run}`")


def notStartedYet(record):
    """Whether the record written with the stop (under the record's lock) says the wrapper hasn't recorded its start:
    its traps may not be set yet, and a signal then would end it without a reason (an orphan); run-steps.py started
    reads the stop instead. No record (one that can't be written, a run before the record): False, the signal goes as
    before."""
    return record is not None and not (record.get("process") or {}).get("started_at")


def stopWrapup(worktree, topic, status):
    """`wt stop` of a wrap-up running after its wrapper (module doc): only when the wrapper is confirmed gone and
    reconciler.wrapupProcesses names a process (a recorded runner or step program confirmed by pid, command line and
    start ticks): the stop recorded unless one is (stopped-<run>, runstate.recordStop: the first stop's time is
    kept), SIGINT to each (a runner itself, which
    forwards it to its step; a program without its runner to its process group), SIGTERM after 10 s to one the same
    check still confirms. Returns when there is nothing to stop."""
    import reconciler

    run = status["run"]
    if run is None or status["phase"] not in ("ended", "orphan"):
        return
    record = runstate.readRecord(str(worktree), run)[0]
    if not wrapperGone(topic, status) or record is None:
        return
    targets = reconciler.wrapupProcesses(str(worktree), run, record)
    if not targets:
        return
    # The first stop's time is kept (runstate.recordStop): it says whether the stop came during claude's part.
    with contextlib.suppress(FileExistsError):
        with open(Path(runstate.runsDir(str(worktree)), f"stopped-{run}"), "x") as marker:
            marker.write(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    runstate.dualWrite(str(worktree), SOURCE, runstate.recordStop, run, "person")
    signalTargets(targets, signal.SIGINT)
    named = ", ".join(f"{kind} {pid}" for kind, pid in targets)
    for _ in range(20):
        left = reconciler.wrapupProcesses(str(worktree), run, runstate.readRecord(str(worktree), run)[0])
        if not left:
            break
        time.sleep(0.5)
    else:
        signalTargets(left, signal.SIGTERM)
    done(context=f"stopped the wrap-up of {topic} run {run} ({named}); its steps left stay kept for "
                 f"`wt wrapup {topic}`; its status: `wt status {topic}`")


def signalTargets(targets, signum):
    """reconciler.wrapupProcesses' processes: a runner by its pid, a step program by its process group."""
    for kind, pid in targets:
        with contextlib.suppress(ProcessLookupError):
            if kind == "runner":
                os.kill(pid, signum)
            else:
                os.killpg(pid, signum)


def refuseUnfinished(topic, status, action):
    """`wt launch` / `wt resume` (module doc): nothing starts over a run that launches, runs, finishes, can't be
    read or may have died unchecked; a confirmed orphan only through wt resume (closeOrphan)."""
    phase, run = status["phase"], status["run"]
    if phase in runstate.ACTIVE:
        done(block=f"{topic} is already running (run {run}, {phase})")
    if phase == "unknown":
        done(block=f"{topic} run {run}: its state can't be read ({status.get('error')}); nothing was started")
    if phase != "orphan":
        return
    line = runstate.human(status, "en")["line"]
    if status["confidence"] != "confirmed":
        done(block=f"{topic}: nothing was closed or started: {line}, and its wrapper process could not be "
                   f"confirmed gone (no pid recorded, or /proc can't be read); `wt status {topic}` shows the "
                   "evidence")
    if action == "launch":
        done(block=f"{topic}: {line}; wt resume closes it first, wt launch never does")


def refuseResumeAfterDoneWrapup(worktree, topic, status):
    """`wt resume` of a run that ended `STATUS: DONE` (module doc): refused while its wrap-up still runs or once its
    merge went through, the same class as `wt merge` refusing while a wrap-up runs (refuseWhileWrapupRuns)."""
    run = status["run"]
    if status["phase"] != "ended" or not runstate.isWorkerDone((status["claude"] or {}).get("status")):
        return
    running = wrapupStillRunning(worktree, run)
    if running:
        done(block=f"resume: {topic} run {run} ended STATUS: DONE and its wrap-up is still running ({running}); "
                   f"nothing to resume: its result comes as an event (`wt status {topic}`)")
    outcome = status["outcome"] or {}
    if outcome.get("merge") == "merged":
        done(block=f"resume: {topic} run {run} ended STATUS: DONE and was merged into main"
                   + (f" ({str(outcome['commit'])[:10]})" if outcome.get("commit") else "")
                   + f"; its finish archives the topic and removes the worktree: nothing to resume (a follow-up "
                   "is a new topic)")


def closeOrphan(worktree, topic, status):
    """wt resume on a confirmed orphan (module doc): close it before the next run starts (its record:
    runstate.closeVerifiedOrphan); a run with only today's files has no record, the new run ends it."""
    if status["phase"] != "orphan" or status["source"] != "record":
        return
    process = status["process"] or {}
    evidence = {"wrapper_pid": process.get("wrapper_pid"), "checked": "gone",
                "reasons": status.get("orphan_reasons") or []}
    try:
        runstate.closeVerifiedOrphan(str(worktree), status["run"], SOURCE, evidence)
    except (runstate.RecordError, OSError, ValueError) as error:
        done(block=f"{topic}: run {status['run']} could not be closed ({error}); nothing was started")


def main():
    commands = {"auto": auto, "auto-merge": auto_merge, "auto-fix": auto_fix, "auto-start": autoStartTold}
    if len(sys.argv) == 5 and sys.argv[1] in commands:
        worktree, topic, run = Path(sys.argv[2]).resolve(), sys.argv[3], int(sys.argv[4])
        commands[sys.argv[1]](worktree, topic, run)
        return
    data = json.load(sys.stdin)
    try:
        answerPrompt(data)
    except homeconfig.ConfigError as error:
        configRefusal(data.get("prompt", ""), error)


def configRefusal(prompt, error, outcome="refused, a setting can't be read"):
    """A wt prompt whose settings can't be read (module doc): one line on stderr (the command, what happened, the
    error), the config mode's line after it while it isn't DEV (as done() adds it), exit 2."""
    command = " ".join(prompt.split()[:2])
    mode = config_mode_guard.statusLine()
    print(f"{command}: {outcome}: {error}" + (f"\n{mode}" if mode else ""), file=sys.stderr)
    sys.exit(2)


WT_PROMPT = re.compile(r"^\s*wt\s")


def answerPrompt(data):
    """A typed `wt ...` prompt (module doc); every answer ends the process (done). Every `wt` prompt but `wt
    status` and `wt reconcile` first triggers the reconciler (module doc)."""
    prompt = data.get("prompt", "")
    if WT_PROMPT.match(prompt) and not (RECONCILE.match(prompt) or STATUS.match(prompt)):
        import reconciler

        reconciler.triggerFrom(Path(data["cwd"]).resolve())
    if QUEUE.match(data.get("prompt", "")):
        queue_command(data["prompt"], Path(data["cwd"]).resolve())
    asked = STATUS.match(data.get("prompt", ""))
    if asked:
        statusCommand(asked.group(1), Path(data["cwd"]).resolve())
    merging = MERGE.match(data.get("prompt", ""))
    if merging:
        merge_command(merging.group(1), Path(data["cwd"]).resolve())
    wrapping = WRAPUP.match(data.get("prompt", ""))
    if wrapping:
        wrapupCommand(wrapping.group(1), Path(data["cwd"]).resolve())
    reconciling = RECONCILE.match(data.get("prompt", ""))
    if reconciling:
        reconcileCommand(reconciling.group(1), bool(reconciling.group(2)), Path(data["cwd"]).resolve())
    match = COMMAND.match(data.get("prompt", ""))
    if not match:
        sys.exit(0)
    action, topic = match.groups()

    main_dir = Path(data["cwd"]).resolve()
    worktree = wt_topics.worktreeOf(main_dir, topic)
    if not (worktree / ".git").is_file():
        done(block=f"{worktree} is not a git worktree")

    state_dir = main_dir / ".hook-only" / "workers"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{topic}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    status = checkedStatus(main_dir, worktree, topic)

    if action == "stop":
        stopRun(worktree, topic, status)
    refused = config_mode_guard.refusal(main_dir)
    if refused:
        done(block=f"{action} {topic}: {refused}")
    refuseUnfinished(topic, status, action)
    if action == "resume":
        refuseResumeAfterDoneWrapup(worktree, topic, status)
    topicMode = checkedFlowMode(worktree, topic)

    config_file = main_dir / "plans" / "worker" / f"{topic}.json"
    headroom = float(homeconfig.load("budget")["budget_headroom_factor"])
    try:
        config = json.loads(config_file.read_text())
        minutes = int(config["minutes"])
        est_usd = float(config["est_usd"]) if "est_usd" in config else None
        cap_minutes = math.ceil(minutes * headroom)
        if "usd" in config:
            usd = float(config["usd"])
        else:
            usd = round(est_usd * headroom, 2)
    except (OSError, ValueError, KeyError, TypeError) as error:
        done(block=f"missing or invalid {config_file} (needs minutes and est_usd): {error}")
    agent = config.get("agent", "")
    if agent and not re.fullmatch(r"[a-z0-9-]{1,64}", agent):
        done(block=f"invalid agent name in {config_file}")
    if config.get("mode") not in (None, "merge"):
        done(block=f"invalid mode in {config_file}: only \"merge\" (or no key)")
    if config.get("flow") not in (None, "scripted"):
        done(block=f"invalid flow in {config_file}: only \"scripted\" (or no key)")
    merge = config.get("mode") == "merge" or state.get("mode") == "merge"

    if action == "launch":
        if state.get("session_id"):
            done(block=f"{topic} already has session {state['session_id']}; use wt resume")
        if merge:
            done(block=f"{topic}: merge mode resumes the session that built the branch, and there is none yet")
        session_id, mode, prompt = str(uuid.uuid4()), "new", f"/feature-flow {topic}"
        prompt_kind = "feature-flow"
        record_flow_mode(worktree)
        if config.get("flow") == "scripted" and scripted_init(worktree, topic, str(config.get("class", ""))):
            prompt, prompt_kind = SCRIPTED_PROMPT.format(topic=topic), "scripted"
    else:
        session_id = state.get("session_id") or config.get("session_id", "")
        if not UUID.match(session_id):
            done(block=f"no session id for {topic} (state or {config_file})")
        resumable = status["resumable"] or {}
        if resumable.get("code") == "done":
            done(block=f"{topic} run {status['run']} ended STATUS: done and {config_file} is the estimate of "
                       "the last run you started: nothing to resume. A merge run needs its estimate first "
                       "(cost-history.py estimate branch --merge); after a merge, /finish-worktree.")
        if resumable.get("ok") is False:
            done(block=f"{topic}: nothing resumed: {resumable.get('why')}")
        closeOrphan(worktree, topic, status)
        mode, prompt = "resume", (MERGE_PROMPT if merge else RESUME_PROMPT).format(topic=topic)
        prompt_kind = "merge" if merge else "resume"
        if not merge and scripted_runnable(worktree):
            prompt, prompt_kind = SCRIPTED_RESUME_PROMPT, "scripted-resume"
        else:
            scripted_off(worktree)
        (worktree / ".hook-only" / "agent-guard" / "HOLD").unlink(missing_ok=True)

    approve_plan(main_dir, worktree, topic)
    raw = {key: config[key] for key in ("raw_minutes", "raw_usd", "hook_wait_minutes") if key in config}
    # "mode" tells cost-history.py import that the run was a merge (its own rows, not the branch's).
    kind = {"mode": "merge"} if merge else {}
    # A run the user starts resets the automatic-resume count (state written without auto_resumes).
    started_by = data.get("started_by") if data.get("started_by") in STARTED_BY else "person"
    run, pid = start_run(main_dir, worktree, topic, state_file, mode, session_id, prompt, minutes, est_usd, usd,
                         cap_minutes, agent, {**raw, **kind}, kind, {"started_by": started_by, "prompt_kind": prompt_kind})
    local = " (local mode)" if topicMode == "local" else ""
    done(context=f"{action} {topic}{' (merge mode)' if merge else ''}{local}: run {run}, pid {pid}, session {session_id}, "
                 f"estimate {minutes} min, limits {cap_minutes} min and ${usd}. Wait in the background: "
                 f"`python3 {RUN_RESULT} {topic} {run} --wait`.")


if __name__ == "__main__":
    main()

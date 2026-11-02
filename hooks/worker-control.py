#!/usr/bin/env python3
"""UserPromptSubmit hook: launch, resume and stop headless worktree workers.

Prompts (typed in the planning session, main checkout):
  wt launch <topic>   first run: new session id, prompt "/feature-flow <topic>"
  wt resume <topic>   resume the recorded session: "re-read the plan, continue"; also lifts the
                      worktree's agent-guard HOLD (the user decided after the ALERT). In merge mode
                      (below) the prompt sends the session to feature-flow merge.md instead. Refused
                      when the last run ended `STATUS: done` and no new estimate was written since the
                      last run the user started (wt_controls.py, "done": the pane's rule, one place)
  wt stop <topic>     write .hook-only/runs/stopped-<run>, SIGINT the worker's process group,
                      SIGTERM after 10 s
  wt queue [<topic> ...]   append the topics to the queue in that order (wt_queue.py: the planning
                      session wrote plans/queue/<topic>.md and .json; local mode only), then start
                      whatever may start now; shown to the person only, never entering the session; `wt queue` alone starts what may start (a held topic
                      merged by hand ends no run) and shows the queue
  wt queue drop <topic>    take a topic that hasn't started (or whose start failed) out of the queue
  wt merge <topic>    the person's approval of a refused automatic merge (local mode; the pane's `merge`
                      button sends it): the last run ended, its merge-<run>.json says refused and not
                      "merge_run"; auto-merge.py --person then runs in the background (the tests take
                      longer than a prompt hook may), skipping only the checks that ask for a person,
                      merging, recording it in merges.jsonl and starting auto-finish; its result is an
                      event (wt_events.py). Shown to the person only, never entering the session.
                      One approval per run at a time: before starting auto-merge.py it writes
                      runs/approve-<run>.json ({"run", "pid", "started", "started_epoch"}; under
                      runs/approve.lock), and a second `wt merge` while that pid is a live auto-merge.py of
                      this topic and run is refused ("already being approved"); a record whose process is
                      gone is stale and replaced. auto-merge.py removes the record when it ends
                      (wt_controls.releaseApproval); the pane reads it (wt_controls.py "approving"). Before,
                      merge-<run>.json kept saying refused for the minutes the tests took, so a second press
                      started a second auto-merge (2026-11-02, twice)

Runs outside the sandbox. Run settings come from plans/worker/<topic>.json in
the main checkout, written by the planning session before each launch/resume:
  {"minutes": 40, "est_usd": 6, "agent": "feature-orchestrator", "session_id": "..."}
  minutes and est_usd are the corrected estimates for this run (minutes with the hook-wait reserve
  cost-history.py estimate branch adds). The limits sit budget_headroom (budget-defaults.json) above
  them: minutes x headroom goes to run-worker.sh (CLAUDE_WORKER_MINUTES for worker-budget.py and
  dispatch-guard.py, and the background wait ceiling), est_usd x headroom is --max-budget-usd. An older
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
the planning session can start waiting on .hook-only/runs/exit-<n>.

Automatic resume (command line, not a prompt; 2026-11-01):
  worker-control.py auto <worktree> <topic> <run>
    run-worker.sh calls it, before writing exit-<run>, when dispatch-guard.py left
    .hook-only/runs/auto-resume-<run>.json (the run stopped only because the planned dispatch didn't
    fit its time or USD; the hook computed what a resume needs). It checks, and writes the decision to
    .hook-only/runs/auto-<run>.json for run-result.py: the run ended `STATUS: blocked` with exit 0, wasn't
    stopped, no agent-guard ALERT or HOLD is pending, the record has minutes and USD within
    worker_auto_resume_max_minutes / _usd, and fewer than worker_auto_resume_max automatic resumes ran
    in a row (the state's auto_resumes; a launch or resume typed by the user resets it). The resume gets
    the larger of the record and what is left of the estimate of the last run the user started (its
    est-<k>.json minus the minutes and USD of runs k..<run>, from run-result.py's helpers), capped at
    worker_auto_resume_max_minutes / _usd: a record covers only the one dispatch that didn't fit, so the
    resumed run would stop again at the next one. On "resume" it starts `auto-start` detached and returns.
  worker-control.py auto-merge <worktree> <topic> <run>
    auto-merge.py calls it when the only reason it didn't merge a `STATUS: done` run is that the branch
    lacks the latest main (merge-<run>.json "merge_run"). Same checks as auto (state, not stopped, no
    ALERT or HOLD), its own count (the state's auto_merges, below worker_auto_merge_max; a launch or
    resume typed by the user resets it, as for auto_resumes) and a fixed budget,
    worker_auto_merge_minutes / _usd: a merge run's size is the conflicts', which nothing knows before
    the merge. Writes auto-<run>.json with "decision": "merge" and starts auto-start, which resumes the
    session in merge mode (MERGE_PROMPT); the mode stays in the state, as for a merge the user started.
  worker-control.py auto-fix <worktree> <topic> <run>
    auto-merge.py calls it when the real data shows a `STATUS: done` run wrong (merge-<run>.json "fix_run":
    a failed check of verify.py, or the verifier's "fail"). Same checks as auto-merge, its own count (the
    state's auto_fixes, below worker_auto_fix_max; reset by a run the user starts). The budget is the model's:
    `cost-history.py estimate branch <type/scope> <class> worker_auto_fix_usd worker_auto_fix_minutes --fix` in
    the main checkout (the raw guess corrected by the history of earlier fix runs, plus one round's gate wait for
    each gated agent the class dispatches; the worktree's history copy when the main checkout has none yet). Writes auto-<run>.json "decision": "fix" and
    starts auto-start, which resumes the session with FIX_PROMPT: the worker reads the findings from
    runs/verify-<run>.json and runs/verifier-<run>.json and handles them as review findings.
  A refused auto-merge or auto-fix is an event of kind "refused" (wt_events.py): auto-merge.py recorded the
  refusal as "retry", which the person doesn't act on, so this is where it reaches them.
  worker-control.py auto-start <worktree> <topic> <run>
    waits for run <run>'s wrapper to exit, checks that nothing else started meanwhile, and resumes the
    session with the decision's minutes and USD as the run's estimate (limits budget_headroom above, as
    always) and AUTO_PROMPT (MERGE_PROMPT in merge mode, FIX_PROMPT for a fix run). The main checkout comes
    from WT_MAIN_DIR, which this hook puts in the wrapper's environment.

The approved plan: every run the person starts (`wt launch`, `wt resume`, a queue start) first copies the
worktree's plans/<topic>.md to <main>/.hook-only/approved/<topic>.md. Starting the run is the person's approval
of the plan as it stands; automatic runs never refresh it, so a plan the worker edits later never becomes its
own acceptance test. verifier.py gives that copy to the verifier.
"""
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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wt_controls  # noqa: E402

WRAPPER = Path.home() / ".claude/hooks/run-worker.sh"
COMMAND = re.compile(r"^\s*wt\s+(launch|resume|stop)\s+([a-z0-9][a-z0-9-]{0,63})\s*$")
MERGE = re.compile(r"^\s*wt\s+merge\s+([a-z0-9][a-z0-9-]{0,63})\s*$")
QUEUE = re.compile(r"^\s*wt\s+queue(?:\s+drop\s+[a-z0-9][a-z0-9-]{0,63}|(?:\s+[a-z0-9][a-z0-9-]{0,63})*)\s*$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
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
FLOW_NEXT = Path.home() / ".claude/hooks/flow-next.py"
DEFAULTS_FILE = Path.home() / ".claude/hooks/budget-defaults.json"
RUN_RESULT = Path.home() / ".claude/skills/repo-plan/run-result.py"
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


def done(context=None, block=None):
    if block:
        print(json.dumps({"decision": "block", "reason": f"wt: {block}"}))
    elif context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": f"wt: {context}"}}))
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


def flow_mode(worktree):
    """The topic's flow mode: its record (written at launch), else this process's FLOW_INTEGRATION."""
    record = worktree / ".hook-only" / "flow-mode"
    try:
        return record.read_text().strip()
    except OSError:
        return "local" if os.environ.get("FLOW_INTEGRATION") == "local" else "origin"


def record_flow_mode(worktree):
    """At a topic's first launch: keep the mode it runs in for all its later runs (run-worker.sh exports it),
    so `wt local on|off` in between never mixes the base a branch started from with where it merges."""
    record = worktree / ".hook-only" / "flow-mode"
    if not record.exists():
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(("local" if os.environ.get("FLOW_INTEGRATION") == "local" else "origin") + "\n")


def approve_plan(main_dir, worktree, topic):
    """A run the person starts approves the plan as it stands (module doc, "The approved plan")."""
    plan = worktree / "plans" / f"{topic}.md"
    if plan.is_file():
        approved = main_dir / ".hook-only" / "approved"
        approved.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plan, approved / f"{topic}.md")


def next_run(runs):
    numbers = [int(m.group(1)) for p in runs.glob("*-*")
               if (m := re.fullmatch(r"(?:result|exit|start)-(\d+)(?:\.json)?", p.name))]
    return max(numbers, default=0) + 1


def start_run(main_dir, worktree, topic, state_file, mode, session_id, prompt, minutes, est_usd, usd,
              cap_minutes, agent, record, extra_state):
    """Write est-<run>.json, start run-worker.sh in its own process group, write the state. Returns (run, pid)."""
    runs = worktree / ".hook-only" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run = next_run(runs)
    (runs / f"est-{run}.json").write_text(json.dumps({"minutes": minutes, "usd": est_usd, "cap": usd,
                                                      "cap_minutes": cap_minutes, **record}))
    process = subprocess.Popen(
        [str(WRAPPER), topic, str(worktree), str(run), mode, session_id,
         str(cap_minutes), str(usd), agent, prompt],
        cwd=worktree, start_new_session=True, env={**os.environ, "WT_MAIN_DIR": str(main_dir)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state_file.write_text(json.dumps({
        "pid": process.pid, "session_id": session_id, "run": run, "worktree": str(worktree),
        "plan": str(worktree / "plans" / f"{topic}.md"), "started": time.time(), **extra_state}))
    return run, process.pid


def first_line(path):
    try:
        text = str(json.loads(path.read_text()).get("result") or "").strip()
    except (OSError, ValueError):
        return ""
    return (text.splitlines() or [""])[0].strip()


def left_of_estimate(runs, run):
    """(minutes, usd) left of the estimate of the last run the user started, after runs since then; (0, 0)
    when run-result.py or the files can't tell."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_result", RUN_RESULT)
        rr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rr)
        first = run
        while first > 1 and (rr.loadJson(str(runs / f"est-{first}.json")) or {}).get("auto"):
            first -= 1
        est = rr.loadJson(str(runs / f"est-{first}.json")) or {}
        minutes = usd = 0.0
        for k in range(first, run + 1):
            start = rr.readText(str(runs / f"start-{k}"))
            end = (rr.readText(str(runs / f"exit-{k}")) or "").partition(" ")[2]
            begun = datetime.fromisoformat(start)
            finished = datetime.fromisoformat(end) if end else datetime.now(begun.tzinfo)
            minutes += (finished - begun).total_seconds() / 60
            path = rr.lastFile(str(runs), k)
            data = (rr.loadJson(path) or {}) if path else {}
            if data:
                usd += float(data.get("total_cost_usd") or 0) - rr.previousCost(str(runs), k, data.get("session_id"))
        return max(float(est.get("minutes") or 0) - minutes, 0.0), max(float(est.get("usd") or 0) - usd, 0.0)
    except Exception:
        return 0.0, 0.0


def auto(worktree, topic, run):
    """The automatic-resume check (module doc). Writes .hook-only/runs/auto-<run>.json either way."""
    runs = worktree / ".hook-only" / "runs"
    decision = runs / f"auto-{run}.json"

    def refuse(reason):
        decision.write_text(json.dumps({"run": run, "decision": "refused", "reason": reason}))
        sys.exit(0)

    main_dir = Path(os.environ.get("WT_MAIN_DIR") or "/nonexistent")
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    try:
        state = json.loads(state_file.read_text())
        record = json.loads((runs / f"auto-resume-{run}.json").read_text())
        defaults = json.loads(DEFAULTS_FILE.read_text())
    except (OSError, ValueError) as error:
        refuse(f"can't read the state, the record or the defaults: {error}")
    if state.get("run") != run or not UUID.match(str(state.get("session_id") or "")):
        refuse(f"the state is for run {state.get('run')}, not {run}")
    if (runs / f"stopped-{run}").exists():
        refuse("the run was stopped with wt stop")
    if not first_line(runs / f"result-{run}.json").startswith("STATUS: blocked"):
        refuse("the run didn't end with STATUS: blocked")
    guard = worktree / ".hook-only" / "agent-guard"
    if (guard / "ALERT").exists() or (guard / "HOLD").exists():
        refuse("an agent-guard ALERT or HOLD is pending: the user decides")
    count, limit = int(state.get("auto_resumes", 0)), int(defaults.get("worker_auto_resume_max", 0))
    if count >= limit:
        refuse(f"{count} automatic resume(s) in a row already (worker_auto_resume_max {limit})")
    minutes, usd = record.get("minutes"), record.get("usd")
    if minutes is None or usd is None:
        refuse("the dispatch had no USD budget, so the resume can't be bounded")
    if minutes > float(defaults.get("worker_auto_resume_max_minutes", 0)) \
            or usd > float(defaults.get("worker_auto_resume_max_usd", 0)):
        refuse(f"needs {minutes} min and ${usd}, above worker_auto_resume_max_minutes / _usd")
    leftMinutes, leftUsd = left_of_estimate(runs, run)
    minutes = math.ceil(min(max(minutes, leftMinutes), float(defaults["worker_auto_resume_max_minutes"])))
    usd = round(min(max(usd, leftUsd), float(defaults["worker_auto_resume_max_usd"])), 2)
    decision.write_text(json.dumps({"run": run, "decision": "resume", "next_run": run + 1,
                                    "minutes": minutes, "usd": usd}))
    subprocess.Popen([sys.executable, __file__, "auto-start", str(worktree), topic, str(run)],
                     start_new_session=True, env=os.environ.copy(),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        refused_event(topic, "merge", reason)
        sys.exit(0)

    main_dir = Path(os.environ.get("WT_MAIN_DIR") or "/nonexistent")
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    try:
        state = json.loads(state_file.read_text())
        merged = json.loads((runs / f"merge-{run}.json").read_text())
        defaults = json.loads(DEFAULTS_FILE.read_text())
    except (OSError, ValueError) as error:
        refuse(f"can't read the state, the merge decision or the defaults: {error}")
    if state.get("run") != run or not UUID.match(str(state.get("session_id") or "")):
        refuse(f"the state is for run {state.get('run')}, not {run}")
    if not merged.get("merge_run"):
        refuse("auto-merge.py didn't ask for a merge run")
    if (runs / f"stopped-{run}").exists():
        refuse("the run was stopped with wt stop")
    guard = worktree / ".hook-only" / "agent-guard"
    if (guard / "ALERT").exists() or (guard / "HOLD").exists():
        refuse("an agent-guard ALERT or HOLD is pending: the user decides")
    count, limit = int(state.get("auto_merges", 0)), int(defaults.get("worker_auto_merge_max", 0))
    if count >= limit:
        refuse(f"{count} automatic merge run(s) in a row already (worker_auto_merge_max {limit}): main keeps moving")
    minutes = math.ceil(float(defaults.get("worker_auto_merge_minutes", 0)))
    usd = round(float(defaults.get("worker_auto_merge_usd", 0)), 2)
    if minutes <= 0 or usd <= 0:
        refuse("no worker_auto_merge_minutes / _usd in budget-defaults.json")
    decision.write_text(json.dumps({"run": run, "decision": "merge", "next_run": run + 1,
                                    "minutes": minutes, "usd": usd}))
    subprocess.Popen([sys.executable, __file__, "auto-start", str(worktree), topic, str(run)],
                     start_new_session=True, env=os.environ.copy(),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        refused_event(topic, "fix", reason)
        sys.exit(0)

    main_dir = Path(os.environ.get("WT_MAIN_DIR") or "/nonexistent")
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    try:
        state = json.loads(state_file.read_text())
        merged = json.loads((runs / f"merge-{run}.json").read_text())
        defaults = json.loads(DEFAULTS_FILE.read_text())
    except (OSError, ValueError) as error:
        refuse(f"can't read the state, the merge decision or the defaults: {error}")
    if state.get("run") != run or not UUID.match(str(state.get("session_id") or "")):
        refuse(f"the state is for run {state.get('run')}, not {run}")
    if not merged.get("fix_run"):
        refuse("auto-merge.py didn't ask for a fix run")
    if (runs / f"stopped-{run}").exists():
        refuse("the run was stopped with wt stop")
    guard = worktree / ".hook-only" / "agent-guard"
    if (guard / "ALERT").exists() or (guard / "HOLD").exists():
        refuse("an agent-guard ALERT or HOLD is pending: the user decides")
    count, limit = int(state.get("auto_fixes", 0)), int(defaults.get("worker_auto_fix_max", 0))
    if count >= limit:
        refuse(f"{count} automatic fix run(s) in a row already (worker_auto_fix_max {limit}): the findings keep "
               "coming back")
    try:
        estimate = fix_estimate(main_dir, worktree, topic, defaults)
    except (OSError, ValueError, KeyError) as error:
        refuse(f"no estimate for the fix run: {error}")
    decision.write_text(json.dumps({"run": run, "decision": "fix", "next_run": run + 1,
                                    "minutes": int(estimate["minutes"]), "usd": float(estimate["est_usd"]),
                                    **{k: estimate[k] for k in ("raw_minutes", "raw_usd", "hook_wait_minutes")
                                       if k in estimate}}))
    subprocess.Popen([sys.executable, __file__, "auto-start", str(worktree), topic, str(run)],
                     start_new_session=True, env=os.environ.copy(),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def auto_start(worktree, topic, run):
    runs = worktree / ".hook-only" / "runs"
    main_dir = Path(os.environ["WT_MAIN_DIR"])
    state_file = main_dir / ".hook-only" / "workers" / f"{topic}.json"
    state = json.loads(state_file.read_text())
    for _ in range(360):
        if not (state.get("pid") and alive(state["pid"]) and is_our_wrapper(state["pid"], topic)):
            break
        time.sleep(0.5)
    else:
        return
    state = json.loads(state_file.read_text())
    if state.get("run") != run:
        return
    record = json.loads((runs / f"auto-{run}.json").read_text())
    defaults = json.loads(DEFAULTS_FILE.read_text())
    headroom = float(defaults["budget_headroom"])
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
    else:
        merge = merging or state.get("mode") == "merge"
        kind = keep = {"mode": "merge"} if merge else {}
        prompt = (MERGE_PROMPT if merge else SCRIPTED_AUTO_PROMPT if resumeScripted else AUTO_PROMPT).format(topic=topic)
        counts["auto_merges" if merging else "auto_resumes"] += 1
    raw = {k: record[k] for k in ("raw_minutes", "raw_usd", "hook_wait_minutes") if k in record}
    start_run(main_dir, worktree, topic, state_file, "resume", state["session_id"], prompt, minutes, est_usd,
              round(est_usd * headroom, 2), math.ceil(minutes * headroom), config.get("agent", ""),
              {"auto": True, **kind, **raw}, {**keep, **counts})


def queue_command(prompt, main_dir):
    """`wt queue ...` (module doc). None of it enters the session: the answer is shown to the person only
    (a block), so a queue press costs no model turn (user 2026-11-01). Nobody waits on queued runs in the
    planning session; auto-merge, auto-finish, plans/friction.md, the pane and `wt queue` carry the results."""
    import wt_queue
    words = prompt.split()[2:]
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
    report = []
    for row in wt_queue.tick(main_dir):
        if "started" in row:
            report.append(f"{row['topic']} started: {row['started']}")
        elif "waits" in row:
            report.append(f"{row['topic']} waits: {row['waits']}")
        else:
            report.append(f"{row['topic']} not started: {row['error']}")
    done(block="queued " + ", ".join(words) + ". " + " | ".join(report)
         + ". The rest start by themselves as runs end; `wt queue` or the pane shows the queue.")


def is_approving(pid, script, topic, run):
    """Whether pid is a live `auto-merge.py <worktree> <topic> <run> --person` (module doc, wt merge)."""
    if not isinstance(pid, int) or not alive(pid):
        return False
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return script.encode() in argv and topic.encode() in argv and str(run).encode() in argv


def merge_command(topic, main_dir):
    """`wt merge <topic>` (module doc)."""
    worktree = main_dir.parent / "worktrees" / topic
    if not (worktree / ".git").is_file():
        done(block=f"merge: {topic} has no worktree")
    if flow_mode(worktree) != "local":
        done(block=f"merge: {topic} runs in PR mode; merge its PR instead")
    try:
        state = json.loads((main_dir / ".hook-only" / "workers" / f"{topic}.json").read_text())
    except (OSError, ValueError):
        done(block=f"merge: no worker state for {topic}")
    run, pid = state.get("run"), state.get("pid")
    runs = worktree / ".hook-only" / "runs"
    if (pid and alive(pid) and is_our_wrapper(pid, topic)) or not (runs / f"exit-{run}").exists():
        done(block=f"merge: {topic} run {run} is still going")
    try:
        decision = json.loads((runs / f"merge-{run}.json").read_text())
    except (OSError, ValueError):
        done(block=f"merge: no automatic-merge decision for {topic} run {run} (only a refused one can be approved)")
    if decision.get("decision") != "refused":
        done(block=f"merge: {topic} run {run} was {decision.get('decision')}, nothing to approve")
    if decision.get("merge_run"):
        done(block=f"merge: {topic} needs a merge run first (the branch lacks the latest main); it starts by itself")
    script = str(Path(__file__).resolve().parent / "auto-merge.py")
    record_file = wt_controls.approvalPath(worktree, run)
    with open(runs / "approve.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = wt_controls.loadJson(record_file)
        if record and is_approving(record.get("pid"), script, topic, run):
            done(block=f"merge: {topic} run {run} is already being approved (since "
                       f"{str(record.get('started', ''))[11:16]}); the result comes as an event")
        proc = subprocess.Popen([sys.executable, script, str(worktree), topic, str(run), "--person"],
                                start_new_session=True,
                                env={**os.environ, "WT_MAIN_DIR": str(main_dir), "FLOW_INTEGRATION": "local"},
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tmp = record_file.with_name(record_file.name + ".tmp")
        tmp.write_text(json.dumps({"run": run, "pid": proc.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                   "started_epoch": time.time()}))
        os.replace(tmp, record_file)
    done(block=f"merge: approving {topic} run {run} (refused: {decision.get('reason', '')[:120]}); the tests run "
               "first, then the merge and the finish; the result comes as an event (toast, your next prompt)")


def main():
    commands = {"auto": auto, "auto-merge": auto_merge, "auto-fix": auto_fix, "auto-start": auto_start}
    if len(sys.argv) == 5 and sys.argv[1] in commands:
        worktree, topic, run = Path(sys.argv[2]).resolve(), sys.argv[3], int(sys.argv[4])
        commands[sys.argv[1]](worktree, topic, run)
        return
    data = json.load(sys.stdin)
    if QUEUE.match(data.get("prompt", "")):
        queue_command(data["prompt"], Path(data["cwd"]).resolve())
    merging = MERGE.match(data.get("prompt", ""))
    if merging:
        merge_command(merging.group(1), Path(data["cwd"]).resolve())
    match = COMMAND.match(data.get("prompt", ""))
    if not match:
        sys.exit(0)
    action, topic = match.groups()

    main_dir = Path(data["cwd"]).resolve()
    worktree = main_dir.parent / "worktrees" / topic
    if not (worktree / ".git").is_file():
        done(block=f"{worktree} is not a git worktree")

    state_dir = main_dir / ".hook-only" / "workers"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{topic}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    pid = state.get("pid")
    running = bool(pid) and alive(pid) and is_our_wrapper(pid, topic)

    if action == "stop":
        if not running:
            done(block=f"{topic} has no running worker")
        # run-result.py reads this marker: a stopped run says nothing about estimating.
        (worktree / ".hook-only" / "runs" / f"stopped-{state.get('run')}").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        os.killpg(pid, signal.SIGINT)
        for _ in range(20):
            if not alive(pid):
                break
            time.sleep(0.5)
        else:
            os.killpg(pid, signal.SIGTERM)
        done(context=f"stopped {topic} run {state.get('run')}; "
                     f"exit file {worktree}/.hook-only/runs/exit-{state.get('run')}")

    if running:
        done(block=f"{topic} is already running (pid {pid}, run {state.get('run')})")

    config_file = main_dir / "plans" / "worker" / f"{topic}.json"
    try:
        config = json.loads(config_file.read_text())
        minutes = int(config["minutes"])
        est_usd = float(config["est_usd"]) if "est_usd" in config else None
        defaults = json.loads(DEFAULTS_FILE.read_text())
        headroom = float(defaults["budget_headroom"])
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
        record_flow_mode(worktree)
        if config.get("flow") == "scripted" and scripted_init(worktree, topic, str(config.get("class", ""))):
            prompt = SCRIPTED_PROMPT.format(topic=topic)
    else:
        session_id = state.get("session_id") or config.get("session_id", "")
        if not UUID.match(session_id):
            done(block=f"no session id for {topic} (state or {config_file})")
        if wt_controls.doneWithoutNewEstimate(worktree, state.get("run"), config_file):
            done(block=f"{topic} run {state.get('run')} ended STATUS: done and {config_file} is the estimate of "
                       "the last run you started: nothing to resume. A merge run needs its estimate first "
                       "(cost-history.py estimate branch --merge); after a merge, /finish-worktree.")
        mode, prompt = "resume", (MERGE_PROMPT if merge else RESUME_PROMPT).format(topic=topic)
        if not merge and scripted_runnable(worktree):
            prompt = SCRIPTED_RESUME_PROMPT
        else:
            scripted_off(worktree)
        (worktree / ".hook-only" / "agent-guard" / "HOLD").unlink(missing_ok=True)

    runs = worktree / ".hook-only" / "runs"
    approve_plan(main_dir, worktree, topic)
    raw = {key: config[key] for key in ("raw_minutes", "raw_usd", "hook_wait_minutes") if key in config}
    # "mode" tells cost-history.py import that the run was a merge (its own rows, not the branch's).
    kind = {"mode": "merge"} if merge else {}
    # A run the user starts resets the automatic-resume count (state written without auto_resumes).
    run, pid = start_run(main_dir, worktree, topic, state_file, mode, session_id, prompt, minutes, est_usd, usd,
                         cap_minutes, agent, {**raw, **kind}, kind)
    local = " (local mode: FLOW_INTEGRATION=local)" if flow_mode(worktree) == "local" else ""
    done(context=f"{action} {topic}{' (merge mode)' if merge else ''}{local}: run {run}, pid {pid}, session {session_id}, "
                 f"estimate {minutes} min, limits {cap_minutes} min and ${usd}. Wait on {runs}/exit-{run}.")


if __name__ == "__main__":
    main()

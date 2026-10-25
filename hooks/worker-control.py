#!/usr/bin/env python3
"""UserPromptSubmit hook: launch, resume and stop headless worktree workers.

Prompts (typed in the planning session, main checkout):
  wt launch <topic>   first run: new session id, prompt "/feature-flow <topic>"
  wt resume <topic>   resume the recorded session: "re-read the plan, continue"; also lifts the
                      worktree's agent-guard HOLD (the user decided after the ALERT)
  wt stop <topic>     write .hook-only/runs/stopped-<run>, SIGINT the worker's process group,
                      SIGTERM after 10 s

Runs outside the sandbox. Run settings come from plans/worker/<topic>.json in
the main checkout, written by the planning session before each launch/resume:
  {"minutes": 40, "est_usd": 6, "agent": "feature-orchestrator", "session_id": "..."}
  minutes and est_usd are the corrected estimates for this run; the hard cap passed as
  --max-budget-usd is est_usd x worker_usd_cap_factor (budget-defaults.json). An older file with
  "usd" (the cap itself) still works. "agent" optional; "session_id" only to adopt a session
  started by hand.
The estimate is recorded per run in <worktree>/.hook-only/runs/est-<n>.json, where run-result.py
and cost-history.py import read it.
State goes to <main>/.hook-only/workers/<topic>.json (agents can't write there).
The prompt still reaches the model, with the result as additional context, so
the planning session can start waiting on .hook-only/runs/exit-<n>.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

WRAPPER = Path.home() / ".claude/hooks/run-worker.sh"
COMMAND = re.compile(r"^\s*wt\s+(launch|resume|stop)\s+([a-z0-9][a-z0-9-]{0,63})\s*$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RESUME_PROMPT = (
    "The plan was updated by the planning session. Re-read plans/{topic}.md, "
    "load the class step files again (feature-flow, Mode: one cat) "
    "and continue from the step where you stopped."
)


def done(context=None, block=None):
    if block:
        print(json.dumps({"decision": "block", "reason": f"wt: {block}"}))
    elif context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": f"wt: {context}"}}))
    sys.exit(0)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_our_wrapper(pid, topic):
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return str(WRAPPER).encode() in argv and topic.encode() in argv


def next_run(runs):
    numbers = [int(m.group(1)) for p in runs.glob("*-*")
               if (m := re.fullmatch(r"(?:result|exit|start)-(\d+)(?:\.json)?", p.name))]
    return max(numbers, default=0) + 1


def main():
    data = json.load(sys.stdin)
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
        if "usd" in config:
            usd = float(config["usd"])
        else:
            defaults = json.loads((Path.home() / ".claude/hooks/budget-defaults.json").read_text())
            usd = round(est_usd * float(defaults.get("worker_usd_cap_factor", 1.2)), 2)
    except (OSError, ValueError, KeyError, TypeError) as error:
        done(block=f"missing or invalid {config_file} (needs minutes and est_usd): {error}")
    agent = config.get("agent", "")
    if agent and not re.fullmatch(r"[a-z0-9-]{1,64}", agent):
        done(block=f"invalid agent name in {config_file}")

    if action == "launch":
        if state.get("session_id"):
            done(block=f"{topic} already has session {state['session_id']}; use wt resume")
        session_id, mode, prompt = str(uuid.uuid4()), "new", f"/feature-flow {topic}"
    else:
        session_id = state.get("session_id") or config.get("session_id", "")
        if not UUID.match(session_id):
            done(block=f"no session id for {topic} (state or {config_file})")
        mode, prompt = "resume", RESUME_PROMPT.format(topic=topic)
        (worktree / ".hook-only" / "agent-guard" / "HOLD").unlink(missing_ok=True)

    runs = worktree / ".hook-only" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run = next_run(runs)
    (runs / f"est-{run}.json").write_text(json.dumps({"minutes": minutes, "usd": est_usd, "cap": usd}))
    process = subprocess.Popen(
        [str(WRAPPER), topic, str(worktree), str(run), mode, session_id,
         str(minutes), str(usd), agent, prompt],
        cwd=worktree, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state_file.write_text(json.dumps({
        "pid": process.pid, "session_id": session_id, "run": run, "worktree": str(worktree),
        "plan": str(worktree / "plans" / f"{topic}.md"), "started": time.time()}))
    done(context=f"{action} {topic}: run {run}, pid {process.pid}, session {session_id}, "
                 f"{minutes} min, ${usd}. Wait on {runs}/exit-{run}.")


if __name__ == "__main__":
    main()

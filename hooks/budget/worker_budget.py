"""PreToolUse(*) [headless worker and its subagents]: time checkpoint for headless worker runs.

Active only when the run was started with CLAUDE_WORKER_MINUTES=<estimated minutes> in its
environment (set in the user's terminal, so the worker can't change it). Once the estimate is used
up, every tool call (the worker's and its subagents') is denied with an instruction to stop and
report, so the run ends with STATUS: blocked instead of being cut off by --max-budget-usd.

The dispatch check (does this agent's budget fit what is left of the run) is dispatch_guard.py.

The run's start is the start of the newest run whose wrapper started (hooks/lib/runstate.py current: its run
record's process.started_at, else its start-<n> file, written by run-worker.sh or by the manual start
command in repo-plan before claude starts). A resumed run is a new run, so it gets a new clock with the
minutes given for that run. Without any started run, the first tool call of the session records the start
in .hook-only/worker-budget/.

Reads: CLAUDE_WORKER_MINUTES, runstate.startedSeconds.
Writes: .hook-only/worker-budget/<session>.json (only without a started run).
Output: check(data) returns the PreToolUse deny, or None.
Fails: closed — a step of hook-entry.py's `pre` entry: an exception (CLAUDE_WORKER_MINUTES not a number
included) denies the call.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio
import hookonly
import runstate

MESSAGE = ("Worker time estimate reached ({used:.0f} of {limit:g} min). Stop now: make no more "
	"tool calls. End your run with a last message starting with `STATUS: blocked`: what is done, "
	"what is left, why it took longer than estimated, and your estimate for the rest. "
	"Uncommitted work stays in the worktree; the user decides whether to resume.")


def sessionStart(stateDir, session, now, limit):
	os.makedirs(stateDir, exist_ok=True)
	statePath = os.path.join(stateDir, f"{session}.json")
	if os.path.isfile(statePath):
		with open(statePath, encoding="utf-8") as f:
			return float(json.load(f)["started"])
	with open(statePath, "w", encoding="utf-8") as f:
		json.dump({"started": now, "minutes": limit}, f)
	return now


def check(data):
	raw = os.environ.get("CLAUDE_WORKER_MINUTES")
	if not raw:
		return None
	limit = float(raw)
	root = hookonly.projectRoot(data)
	now = time.time()
	started = runstate.startedSeconds(root)
	if started is None:
		started = sessionStart(hookonly.stateDir(root, "worker-budget"),
			data.get("session_id") or "unknown", now, limit)
	used = (now - started) / 60
	if limit > 0 and used >= limit:
		return hookio.refuse("PreToolUse", MESSAGE.format(used=used, limit=limit))
	return None

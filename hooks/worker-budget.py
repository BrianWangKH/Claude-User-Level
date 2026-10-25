#!/usr/bin/env python3
"""PreToolUse hook (matcher "*"): time checkpoint for headless worker runs.

Active only when the run was started with CLAUDE_WORKER_MINUTES=<estimated minutes> in its
environment (set in the user's terminal, so the worker can't change it). Once the estimate is used
up, every tool call (the worker's and its subagents') is denied with an instruction to stop and
report, so the run ends with STATUS: blocked instead of being cut off by --max-budget-usd.

The dispatch check (does this agent's budget fit what is left of the run) is dispatch-guard.py.

The run's start is the newest <checkout>/.hook-only/runs/start-<n> (written by run-worker.sh, or by
the manual start command in repo-plan, before claude starts; content `date -Is`). A resumed run
writes the next start-<n>, so it gets a new clock with the minutes given for that run. Without any
start file, the first tool call of the session records the start in .hook-only/worker-budget/.
(The start used to be keyed by the hook's parent pid; every hook call runs under a new shell, so
each call started a fresh clock and the limit never fired.)
Fails open on its own errors (the hard cap --max-budget-usd still applies) but says so.
"""
import json
import os
import re
import sys
import time
from datetime import datetime

import hookonly

MESSAGE = ("Worker time estimate reached ({used:.0f} of {limit:g} min). Stop now: make no more "
	"tool calls. End your run with a last message starting with `STATUS: blocked`: what is done, "
	"what is left, why it took longer than estimated, and your estimate for the rest. "
	"Uncommitted work stays in the worktree; the user decides whether to resume.")
START_FILE = re.compile(r"^start-(\d+)$")
def deny(reason):
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
		"permissionDecision": "deny", "permissionDecisionReason": reason}}))
	sys.exit(0)


def runStart(runsDir):
	"""Start time (epoch seconds) from the highest-numbered start-<n>, or None if there is none."""
	if not os.path.isdir(runsDir):
		return None
	numbered = [(int(m.group(1)), name) for name in os.listdir(runsDir) if (m := START_FILE.match(name))]
	if not numbered:
		return None
	with open(os.path.join(runsDir, max(numbered)[1]), encoding="utf-8") as f:
		return datetime.fromisoformat(f.read().strip()).timestamp()


def sessionStart(stateDir, session, now, limit):
	os.makedirs(stateDir, exist_ok=True)
	statePath = os.path.join(stateDir, f"{session}.json")
	if os.path.isfile(statePath):
		with open(statePath, encoding="utf-8") as f:
			return float(json.load(f)["started"])
	with open(statePath, "w", encoding="utf-8") as f:
		json.dump({"started": now, "minutes": limit}, f)
	return now


def main():
	raw = os.environ.get("CLAUDE_WORKER_MINUTES")
	if not raw:
		sys.exit(0)
	try:
		limit = float(raw)
		data = json.load(sys.stdin)
		root = hookonly.projectRoot(data)
		now = time.time()
		started = runStart(os.path.join(root, hookonly.NAME, "runs"))
		if started is None:
			started = sessionStart(hookonly.stateDir(root, "worker-budget"),
				data.get("session_id") or "unknown", now, limit)
		used = (now - started) / 60
		if limit > 0 and used >= limit:
			deny(MESSAGE.format(used=used, limit=limit))
	except Exception as error:
		print(f"worker-budget hook error (time checkpoint off for this call): {error}", file=sys.stderr)
	sys.exit(0)


main()

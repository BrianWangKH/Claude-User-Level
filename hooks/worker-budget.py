#!/usr/bin/env python3
"""PreToolUse hook (matcher "*"): time checkpoint for headless worker runs.

Active only when the run was started with CLAUDE_WORKER_MINUTES=<estimated minutes> in its
environment (set in the user's terminal, so the worker can't change it). The first tool call
of a run records its start in <cwd>/.hook-only/worker-budget/; once the estimate is used up,
every tool call (the worker's and its subagents') is denied with an instruction to stop and
report, so the run ends with STATUS: blocked instead of being cut off by --max-budget-usd.
A resumed run is a new process and starts a new clock with the minutes given for that run.
Fails open on its own errors (the hard cap --max-budget-usd still applies) but says so.
"""
import json
import os
import sys
import time

MESSAGE = ("Worker time estimate reached ({used:.0f} of {limit:g} min). Stop now: make no more "
	"tool calls. End your run with a last message starting with `STATUS: blocked`: what is done, "
	"what is left, why it took longer than estimated, and your estimate for the rest. "
	"Uncommitted work stays in the worktree; the user decides whether to resume.")


def deny(reason):
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
		"permissionDecision": "deny", "permissionDecisionReason": reason}}))
	sys.exit(0)


def main():
	raw = os.environ.get("CLAUDE_WORKER_MINUTES")
	if not raw:
		sys.exit(0)
	try:
		limit = float(raw)
		data = json.load(sys.stdin)
		cwd = data.get("cwd") or os.getcwd()
		session = data.get("session_id") or "unknown"
		stateDir = os.path.join(cwd, ".hook-only", "worker-budget")
		os.makedirs(stateDir, exist_ok=True)
		statePath = os.path.join(stateDir, f"{session}-{os.getppid()}.json")
		now = time.time()
		if os.path.isfile(statePath):
			with open(statePath, encoding="utf-8") as f:
				started = float(json.load(f)["started"])
		else:
			started = now
			with open(statePath, "w", encoding="utf-8") as f:
				json.dump({"started": now, "minutes": limit}, f)
		used = (now - started) / 60
		if limit > 0 and used >= limit:
			deny(MESSAGE.format(used=used, limit=limit))
	except Exception as error:
		print(f"worker-budget hook error (time checkpoint off for this call): {error}", file=sys.stderr)
	sys.exit(0)


main()

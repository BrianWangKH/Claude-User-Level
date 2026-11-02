#!/usr/bin/env python3
"""Stop hook: a headless worker turn must end on a STATUS or PHASE line, and never while work runs.

Active only in a worker run (CLAUDE_WORKER_MINUTES set by run-worker.sh) and not during the wrapper's
own /compact calls (CLAUDE_WORKER_COMPACT set).

- Background commands or monitors in flight (Stop input `background_tasks` entries without an
  agent_type): a turn whose first line is `STATUS:` or `PHASE:` is blocked, because the wrapper would end
  or compact the run under it; `claude -p` gives one more turn with their outcome. A turn without one is
  a pause and may stop.
- Background subagents (entries with an agent_type) don't hold the ending: `claude -p` waits for them
  before it exits anyway, and their completion gives no further turn (headless docs, "Background tasks
  at exit"), so blocking a STATUS line under one lost it (inferred-report-dates run 1, 2026-10-31). A
  turn without a STATUS line while one runs is a pause: its hand-back starts the next turn. That an
  entry for a subagent carries agent_type is assumed from this hook's earlier use; an entry without it
  is treated as a command, which is the old behaviour.
- Nothing in flight: the first non-empty line of `last_assistant_message` must be
  `STATUS: done|needs-decision|blocked` or `PHASE: tests-done|dev-done`. Otherwise the stop is blocked
  once with the format; on the retry (`stop_hook_active`) it lets the turn end, and the planning
  session treats the missing line as blocked.
Fails open on its own errors.
"""
import json
import os
import re
import sys

ENDING = re.compile(r"^(STATUS: (done|needs-decision|blocked)|PHASE: (tests-done|dev-done))\b")
FORMAT = ("This is a headless worker run: your last message must start with exactly one line "
	"`STATUS: done`, `STATUS: needs-decision` or `STATUS: blocked` (or `PHASE: tests-done` / "
	"`PHASE: dev-done` at a test-first phase boundary), then, in Traditional Chinese: the questions with "
	"a recommendation each, or each blocked operation with its full message; what was done; the next step. "
	"Send that message now.")
IN_FLIGHT = ("{n} background task(s) still running ({what}). Don't end the run or the phase under them: "
	"end your turn without a STATUS or PHASE line and wait for the completion notification, then report.")


def block(reason):
	print(json.dumps({"decision": "block", "reason": f"worker-stop: {reason}"}))
	sys.exit(0)


def main():
	if not os.environ.get("CLAUDE_WORKER_MINUTES") or os.environ.get("CLAUDE_WORKER_COMPACT"):
		return
	data = json.load(sys.stdin)
	lines = [l.strip() for l in (data.get("last_assistant_message") or "").splitlines() if l.strip()]
	first = lines[0] if lines else ""
	ending = bool(ENDING.match(first))
	tasks = data.get("background_tasks") or []
	commands = [t for t in tasks if not t.get("agent_type")]
	if commands:
		if ending:
			what = ", ".join(t.get("description") or t.get("type") or "?" for t in commands)
			block(IN_FLIGHT.format(n=len(commands), what=what[:300]))
		return
	if tasks and not ending:
		return
	if not ending and not data.get("stop_hook_active"):
		block(FORMAT)


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"worker-stop error (not checked): {error}", file=sys.stderr)
	sys.exit(0)

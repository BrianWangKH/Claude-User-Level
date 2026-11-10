#!/usr/bin/env python3
"""Stop hook: a headless worker turn must end on a STATUS or PHASE line, and never while work runs.

Active only in a worker run (CLAUDE_WORKER_MINUTES set by run-worker.sh) and not during the wrapper's
own /compact calls (CLAUDE_WORKER_COMPACT set).

- Background commands or monitors in flight (Stop input `background_tasks` entries without an
  agent_type): a turn whose first line is `STATUS:` or `PHASE:` is blocked, because the wrapper would end
  or compact the run under it; `claude -p` gives one more turn with their outcome. A turn without one is
  a pause and may stop.
- Background subagents (entries with an agent_type) don't hold the ending: blocking a STATUS line under
  one lost it. The wrapper bounds the wait for them after the
  final STATUS turn instead (run_ending.py, run-steps.py watch). A turn without a STATUS line while one runs is
  a pause: its hand-back starts the next turn. That an entry for a subagent carries agent_type is assumed
  from this hook's earlier use; an entry without it is treated as a command, which is the old behaviour.
- Nothing in flight: the first non-empty line of `last_assistant_message` must be
  `STATUS: done|needs-decision|blocked` or `PHASE: tests-done|dev-done`. Otherwise the stop is blocked
  once with the format; on the retry (`stop_hook_active`) it lets the turn end, and the planning
  session treats the missing line as blocked.
Every Stop it sees is recorded for the run (run_ending.recordTurn: final only when the turn ends on a
STATUS line and is let through; a PHASE turn is a mid-run phase end, never final), which the wrapper's
watcher reads.
Fails open on its own errors: no decision, or no record (then claude's own background wait applies).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookonly  # noqa: E402
import run_ending  # noqa: E402
import runstate  # noqa: E402

# The STATUS value in either case (hooks/RULES.md R2: workers write the upper-case enum); the key stays STATUS.
ENDING = re.compile(r"^(STATUS: (?i:done|needs-decision|blocked)|PHASE: (tests-done|dev-done))\b")
FORMAT = ("This is a headless worker run: your last message must start with exactly one line "
	"`STATUS: done`, `STATUS: needs-decision` or `STATUS: blocked` (or `PHASE: tests-done` / "
	"`PHASE: dev-done` at a test-first phase boundary), then, in the user's preferred language: the questions with "
	"a recommendation each, or each blocked operation with its full message; what was done; the next step. "
	"Send that message now.")
IN_FLIGHT = ("{n} background task(s) still running ({what}). Don't end the run or the phase under them: "
	"end your turn without a STATUS or PHASE line and wait for the completion notification, then report.")
BACKGROUND_FIELDS = ("type", "agent_type", "description")


def decide(data):
	"""(the block reason or None, whether the turn ends the run on its STATUS line: the final turn)."""
	lines = [l.strip() for l in (data.get("last_assistant_message") or "").splitlines() if l.strip()]
	first = lines[0] if lines else ""
	ending = bool(ENDING.match(first))
	tasks = data.get("background_tasks") or []
	commands = [t for t in tasks if not t.get("agent_type")]
	if commands:
		if ending:
			what = ", ".join(t.get("description") or t.get("type") or "?" for t in commands)
			return IN_FLIGHT.format(n=len(commands), what=what[:300]), False
		return None, False
	if not ending and not (tasks or data.get("stop_hook_active")):
		return FORMAT, False
	return None, ending and run_ending.isStatusText(first)


def recordTurn(data, final):
	"""The Stop for the newest started run of this checkout (runstate.current); nothing without one."""
	root = hookonly.projectRoot(data)
	found = runstate.current(root)
	if found is None:
		return
	background = [{k: t.get(k) for k in BACKGROUND_FIELDS} for t in data.get("background_tasks") or []]
	run_ending.recordTurn(root, found["run"], final=final, message=data.get("last_assistant_message") or "",
		sessionId=data.get("session_id"), background=background)


def main():
	if not os.environ.get("CLAUDE_WORKER_MINUTES") or os.environ.get("CLAUDE_WORKER_COMPACT"):
		return
	data = json.load(sys.stdin)
	reason, final = decide(data)
	try:
		recordTurn(data, final)
	except Exception as error:
		print(f"worker-stop: turn not recorded (the wait after it stays claude's own): {error}", file=sys.stderr)
	if reason:
		print(json.dumps({"decision": "block", "reason": f"worker-stop: {reason}"}))


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"worker-stop error (not checked): {error}", file=sys.stderr)
	sys.exit(0)

"""The final turn of a worker run's claude call and the bound on the idle wait after it.

After a turn whose first line is `STATUS:` the run is over, but `claude -p` keeps it alive while background work
is pending (subagents, Monitor watches, /loop wakeups; headless docs, "Background tasks at exit"), and
run-worker.sh raises the idle ceiling to the run's minutes so that a mid-run background code-reviewer held by the
mutation scan is not dropped. Two facts bound the wait after the final turn without touching any mid-run wait:
- run-ending `<run>-ending.json` (worker-stop.py, the only writer): the newest Stop of the run's claude call,
  written at every Stop the hook sees: `final` true only when the turn ended on a STATUS line and the hook let it
  end; the message itself in `raw`. A pause (a background reviewer still running) and a PHASE turn (a mid-run
  phase end: work still pending there is unfinished work) write `final` false, so no cut ever follows them.
- run-ending-cut `<run>-ending-cut.json` (run-steps.py watch, the only writer): the wrapper's watcher ended the
  claude call because its final turn was followed by more than ending_idle_max_seconds of waiting; written
  before the signal, with the final turn's message, so run-steps.py invocation always sees it once claude is
  gone.
run-steps.py invocation then rewrites the call's output file (cutOutput): the final turn's text, claude's own
output kept under `raw`, and CUT_KEY, which run-steps.py next-invocation reads as "the call ended on its final
turn" (run code 0, no further claude call).
Reads/Writes: the two kinds above through statefile (config/registry/state-registry.json).
Fails: the callers decide (worker-stop.py and run-steps.py fail open: no record or no cut record leaves claude's
own wait, bounded by CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS).
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import report_header  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402

TURN = "run-ending"
CUT = "run-ending-cut"
SCHEMA = 1
CUT_KEY = "worker_ending_cut"
VALUE_WORD = re.compile(r"[A-Za-z-]+")  # the value's word, read in either case by runstate.workerStatus


class NoStatus(ValueError):
	pass


def isStatusText(text):
	"""Whether a turn's first header line is `STATUS: done|needs-decision|blocked`, the value in either case
	(`STATUS: DONE`, R2's enum; more text may follow the value on that line, as worker-stop.py has always accepted):
	the only turn that ends a run."""
	fields = report_header.parse(text or "")
	if next(iter(fields), None) != "STATUS":
		return False
	word = VALUE_WORD.match(fields["STATUS"][0])
	return bool(word) and runstate.workerStatus(word.group(0)) is not None


def recordTurn(root, run, *, final, message, sessionId, background):
	"""The newest Stop of run's claude call; the message is kept only for a final turn."""
	body = {
		"schema": SCHEMA,
		"turn_ended_at": clock.isoNow(),
		"final": bool(final),
		"session_id": sessionId,
		"background": background,
		"raw": {"last_assistant_message": message if final else None},
	}
	return statefile.write(root, TURN, body, run)


def finalTurn(root, run, startedAt):
	"""The run's newest Stop when it is a final turn of the call started at startedAt (strictly later: a turn of
	an earlier call of the same run never counts; one in the call's first second is missed, leaving claude's own
	wait), else None."""
	turn = statefile.read(root, TURN, run)
	if not turn or turn.get("final") is not True:
		return None
	ended, started = stamp(turn.get("turn_ended_at")), stamp(startedAt)
	if ended is None or started is None or ended <= started:
		return None
	return turn


def idleSeconds(turn):
	"""Seconds since the final turn's Stop."""
	return (clock.now() - stamp(turn["turn_ended_at"])).total_seconds()


def recordCut(root, run, *, file, startedAt, turn, idleMaxSeconds):
	body = {
		"schema": SCHEMA,
		"cut_at": clock.isoNow(),
		"file": file,
		"invocation_started_at": startedAt,
		"turn_ended_at": turn["turn_ended_at"],
		"idle_max_seconds": idleMaxSeconds,
		"raw": {"last_assistant_message": (turn.get("raw") or {}).get("last_assistant_message")},
	}
	return statefile.write(root, CUT, body, run)


def cutOf(root, run, file, startedAt):
	"""The cut record of exactly this call (its output file and start), None when the watcher didn't cut it."""
	cut = statefile.read(root, CUT, run)
	if cut and cut.get("file") == file and cut.get("invocation_started_at") == startedAt:
		return cut
	return None


def cutOutput(claudeText, claudeData, cut):
	"""The call's output after a cut: claude's own result when it already leads with STATUS, else the final
	turn's message the cut record kept; claude's own output under raw; no total_cost_usd (claude didn't report
	it). NoStatus when neither leads with a STATUS line: the call then stays as claude left it (an error)."""
	own = str((claudeData or {}).get("result") or "")
	text = own if isStatusText(own) else (cut.get("raw") or {}).get("last_assistant_message") or ""
	if not isStatusText(text):
		raise NoStatus("neither claude's output nor the cut record starts with a STATUS line")
	return {
		"schema": SCHEMA,
		"type": "result",
		"is_error": False,
		"result": text,
		CUT_KEY: {k: cut[k] for k in ("cut_at", "turn_ended_at", "idle_max_seconds")},
		"raw": {"claude_output": claudeText},
	}


def isCut(data):
	"""A call output rewritten after a cut, its result leading with a STATUS line; anything else is not."""
	data = data or {}
	return isinstance(data.get(CUT_KEY), dict) and isStatusText(str(data.get("result") or ""))


def stamp(value):
	if not isinstance(value, str):
		return None
	try:
		return clock.parse(value)
	except ValueError:
		return None

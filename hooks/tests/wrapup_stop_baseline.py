"""How a run stopped during its wrap-up was decided before `wt wrapup` could continue it (skill refactor, section 2):
test data only, never imported by a program. Pure functions of the run record, transcribed from the old
runstate.wrapupLeft and the old run-steps.py Wrap (stopped, which also gave the exit reason, of a wrap-up run again
without --code/--stopped); test_runstate.py, test_run_steps.py and test_reconciler.py compare them with today's code
over generated records."""

WRAPUP_FAILED = "wrapup-failed"
STEP_ORDER = ("verify", "verifier", "merge", "next", "finish", "queue_tick")
STEP_FINAL = ("done", "skipped", "abandoned")
WORKER_DONE = ("done", "DONE")


def oldWrapupLeft(record):
	"""runstate.wrapupLeft before: only an exit wrapup-failed (or none) owed steps, and a skip was final."""
	claude = record.get("claude") or {}
	code = claude.get("exit_code")
	end = record.get("exit") or {}
	if (end and end.get("reason") != WRAPUP_FAILED) or claude.get("status") not in WORKER_DONE or type(code) is not int:
		return []
	steps = record.get("steps") or {}
	return [name for name in STEP_ORDER if (steps.get(name) or {}).get("state") not in STEP_FINAL]


def oldWrapStopped(record):
	"""run-steps.py Wrap.stopped before, for a wrap-up run again without --stopped (by hand, wt wrapup, the
	reconciler): claude.stopped or the person's stop; the exit it wrote read stopped exactly then."""
	claude = record.get("claude") or {}
	return bool(claude.get("stopped") or record.get("stop"))


def oldStopExitBlocks(record):
	"""runstate.mergeBlocks' stop and exit blocks before, as (fact, reason): every stop and every exit other than
	completed refused the automatic merge and the person's approval alike."""
	found = []
	end = record.get("exit")
	if record.get("stop"):
		found.append(("stop", "the run was stopped"))
	if end and end.get("reason") != "completed":
		found.append(("exit", f"the run ended {end.get('reason')}"))
	return found

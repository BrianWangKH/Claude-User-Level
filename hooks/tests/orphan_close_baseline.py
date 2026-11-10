"""How runstate.closeOrphan closed a confirmed orphan before a done worker's wrap-up was kept (skill refactor,
section 2): test data only, never imported by a program. One pure function of the record, transcribed from the
old closeOrphan's change(); test_runstate.py compares it with the new closeOrphan over generated records."""

import copy

UNFINISHED_STEP = ("pending", "running")


class Refused(Exception):
	"""The old closeOrphan raised RecordError."""


def oldClose(record):
	"""The record the old closeOrphan wrote (exit reason orphan-closed, unfinished steps abandoned), or Refused
	when it raised (not a confirmed orphan, or ended already). ended_at and closed_by are left to the caller."""
	if not (record.get("orphan") or {}).get("confirmed_at"):
		raise Refused("not a confirmed orphan")
	if record.get("exit"):
		raise Refused("ended already")
	closed = copy.deepcopy(record)
	for step in (closed.get("steps") or {}).values():
		if step.get("state") in UNFINISHED_STEP:
			step["state"] = "abandoned"
	closed["exit"] = {"code": None, "reason": "orphan-closed"}
	return closed

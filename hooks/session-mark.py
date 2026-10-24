"""SessionStart / SessionEnd hook: record when each session in a checkout starts and ends.

Writes <checkout>/.hook-only/session/<session_id>.json:
  {"session_id", "started", "source", "permission_mode", "ended"?, "end_reason"?}
so the planning session can tell whether a worker is running, has ended, or never started,
without reading any transcript (skill repo-plan, worker-status.py).

Informational only: it never blocks and never prints. Records older than 30 days are removed at
SessionStart.
"""
import json
import os
import sys
import time

import hookonly

KEEP_SECONDS = 30 * 24 * 3600


def removeOld(directory):
	cutoff = time.time() - KEEP_SECONDS
	for name in os.listdir(directory):
		path = os.path.join(directory, name)
		try:
			if name.endswith(".json") and os.path.getmtime(path) < cutoff:
				os.remove(path)
		except OSError:
			pass


def main():
	data = json.load(sys.stdin)
	event = data.get("hook_event_name")
	sessionId = data.get("session_id")
	if event not in ("SessionStart", "SessionEnd") or not sessionId:
		return
	directory = hookonly.stateDir(hookonly.projectRoot(data), "session")
	os.makedirs(directory, exist_ok=True)
	path = os.path.join(directory, f"{sessionId}.json")
	record = {}
	if os.path.isfile(path):
		with open(path, encoding="utf-8") as f:
			record = json.load(f)
	if event == "SessionStart":
		# A resume starts the same session again: a new start, no end yet.
		record.update({"session_id": sessionId, "started": time.time(), "source": data.get("source"),
			"permission_mode": data.get("permission_mode")})
		record.pop("ended", None)
		record.pop("end_reason", None)
	else:
		record.update({"session_id": sessionId, "ended": time.time(), "end_reason": data.get("reason")})
	with open(path, "w", encoding="utf-8") as f:
		json.dump(record, f)
	if event == "SessionStart":
		removeOld(directory)


if __name__ == "__main__":
	try:
		main()
	except Exception:
		pass
	sys.exit(0)

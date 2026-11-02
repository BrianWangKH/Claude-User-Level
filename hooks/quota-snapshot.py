#!/usr/bin/env python3
"""Status line wrapper: keeps the subscription's 5-hour and weekly usage where the topic queue can read it.

Usage (settings.json "statusLine" command): python3 ~/.claude/hooks/quota-snapshot.py [-- <your status line command ...>]
The status line JSON is the only documented place that carries the usage limits (statusline docs,
"Available data": rate_limits.five_hour / seven_day .used_percentage and .resets_at; claude.ai
subscribers only, and only after the session's first API response; a window is dropped once its
resets_at passes). Headless workers report nothing of it, so an interactive session's status line is
the source. Each call with rate_limits writes <home>/quota/usage.json:
  {"at": <epoch seconds>, "five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}}
(<home> is this file's folder's parent; agents' sandbox can't write there). "at" moves only when the numbers
change: they come from this session's own last API response, so a status line re-run with the same numbers
(an idle session, a refreshInterval) learns nothing new, and the workers' usage since is unseen. Keeping
the old "at" lets the queue treat that as stale instead of fresh. Then the same input goes to
the command after `--`, whose output is the status line, unchanged; without one it prints the two
percentages. Never fails the status line: a write error goes to stderr.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

USAGE = Path(__file__).resolve().parent.parent / "quota" / "usage.json"
WINDOWS = ("five_hour", "seven_day")
LABELS = {"five_hour": "5h", "seven_day": "7d"}


def snapshot(data, now):
	limits = data.get("rate_limits") if isinstance(data, dict) else None
	if not isinstance(limits, dict):
		return None
	out = {"at": now}
	for window in WINDOWS:
		w = limits.get(window)
		if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)) \
				and isinstance(w.get("resets_at"), (int, float)):
			out[window] = {"used_percentage": float(w["used_percentage"]), "resets_at": float(w["resets_at"])}
	return out if len(out) > 1 else None


def main(argv):
	raw = sys.stdin.read()
	try:
		snap = snapshot(json.loads(raw), time.time())
		try:
			previous = json.loads(USAGE.read_text(encoding="utf-8"))
		except (OSError, ValueError):
			previous = None
		if snap and isinstance(previous, dict) and all(previous.get(w) == snap.get(w) for w in WINDOWS):
			snap["at"] = previous.get("at", snap["at"])  # same numbers: nothing new learned (module doc)
		if snap:
			USAGE.parent.mkdir(parents=True, exist_ok=True)
			tmp = USAGE.with_suffix(".tmp")
			tmp.write_text(json.dumps(snap), encoding="utf-8")
			tmp.replace(USAGE)
	except (ValueError, OSError) as error:
		snap = None
		print(f"quota-snapshot: {error}", file=sys.stderr)
	command = argv[argv.index("--") + 1:] if "--" in argv else []
	if command:
		result = subprocess.run(command, input=raw, capture_output=True, text=True)
		sys.stdout.write(result.stdout)
		return
	if snap:
		print(" · ".join(f"{LABELS[w]} {snap[w]['used_percentage']:.0f}%"
			for w in WINDOWS if w in snap))


if __name__ == "__main__":
	main(sys.argv)

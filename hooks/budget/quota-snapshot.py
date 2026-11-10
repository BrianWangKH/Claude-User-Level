#!/usr/bin/env python3
"""Status line wrapper: keeps the subscription's 5-hour and weekly usage where the topic queue can read it.

Usage (settings.json "statusLine" command): python3 ~/.claude/hooks/budget/quota-snapshot.py [-- <your status line command ...>]
The status line JSON is the only documented place that carries the usage limits (statusline docs,
"Available data": rate_limits.five_hour / seven_day .used_percentage and .resets_at; claude.ai
subscribers only, and only after the session's first API response; a window is dropped once its
resets_at passes). Headless workers report nothing of it, so an interactive session's status line is
the source. Each call with rate_limits writes the state kind quota-usage (config/registry/state-registry.json,
location home: <home>/state/quota/usage.json, through hooks/lib/statefile.py; agents' sandbox can't write there):
  {"schema": 1, "at": <epoch seconds>, "five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}}
"at" moves only when the numbers change: they come from this session's own last API response, so a status line
re-run with the same numbers (an idle session, a refreshInterval) learns nothing new, and the workers' usage since
is unseen. Keeping the old "at" lets the queue treat that as stale instead of fresh. Then the same input goes to
the command after `--`, whose output is the status line, unchanged; without one it prints the two percentages.

Reads: the status line JSON on stdin; the previous snapshot (statefile.read, quota-usage).
Writes: statefile.write quota-usage, only for an input with rate_limits.
Output: the `--` command's stdout, unchanged; without one, the two percentages.
Fails: open — the status line never breaks: any error of the snapshot (the home modules' import included) goes
to stderr and the status line still prints.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath

KIND = "quota-usage"
SCHEMA = 1
WINDOWS = ("five_hour", "seven_day")
LABELS = {"five_hour": "5h", "seven_day": "7d"}


def snapshot(data, now):
	limits = data.get("rate_limits") if isinstance(data, dict) else None
	if not isinstance(limits, dict):
		return None
	out = {"schema": SCHEMA, "at": now}
	for window in WINDOWS:
		w = limits.get(window)
		if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)) \
				and isinstance(w.get("resets_at"), (int, float)):
			out[window] = {"used_percentage": float(w["used_percentage"]), "resets_at": float(w["resets_at"])}
	return out if any(w in out for w in WINDOWS) else None


def record(raw):
	"""The snapshot of this status line input, written (module doc); None when the input has no usage."""
	import homepath  # noqa: F401  (here, not at the top: a failed import must not break the status line)
	import statefile

	snap = snapshot(json.loads(raw), time.time())
	if snap is None:
		return None
	previous = statefile.read(None, KIND)
	if previous is not None and all(previous.get(w) == snap.get(w) for w in WINDOWS):
		snap["at"] = previous.get("at", snap["at"])  # same numbers: nothing new learned (module doc)
	statefile.write(None, KIND, snap)
	return snap


def main(argv):
	raw = sys.stdin.read()
	try:
		snap = record(raw)
	except Exception as error:  # Fails: open (module doc)
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

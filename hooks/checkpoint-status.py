#!/usr/bin/env python3
"""UserPromptSubmit hook: a subagent's CHECKPOINT report arrives with `git status --short` beside it.

feature-flow agents.md, "When an agent returns": after a CHECKPOINT the orchestrator records the files
that really exist, not the ones the report lists (a checkpoint report has named files it never wrote).
This adds the checkout's `git status --short` as context, so that step doesn't rest on a text rule.
A background subagent's report reaches the session that started it as a UserPromptSubmit
(hooks docs, UserPromptSubmit: "A background subagent reporting back to the session that started it");
the input has no origin field, so any prompt with a `CHECKPOINT` status line gets it, a typed one too,
which costs a few lines and nothing else. Hook output from inside the subagent (SubagentStop,
PostToolUse) isn't documented to reach the parent, so it isn't used. Fails open (stderr).
"""
import json
import re
import subprocess
import sys

import hookonly

# The report header (report-schema.json) writes `STATUS: CHECKPOINT`; the bare word is the older form.
STATUS_LINE = re.compile(r"^\W*(?:STATUS:\s*)?CHECKPOINT\b", re.MULTILINE)
MAX_LINES = 60


def main():
	data = json.load(sys.stdin)
	if data.get("hook_event_name") != "UserPromptSubmit" or not STATUS_LINE.search(data.get("prompt") or ""):
		return
	root = hookonly.gitRoot(data)
	if not root:
		return
	result = subprocess.run(["git", "-C", root, "status", "--short", "--untracked-files=all"],
		capture_output=True, text=True, timeout=30)
	if result.returncode != 0:
		return
	lines = [l for l in result.stdout.splitlines() if l.strip() and not l[3:].startswith(".hook-only/")]
	shown = lines[:MAX_LINES] + ([f"... and {len(lines) - MAX_LINES} more"] if len(lines) > MAX_LINES else [])
	text = "\n".join(shown) or "(clean: nothing changed or untracked)"
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
		"checkpoint-status: a CHECKPOINT report came in; `git status --short` of " + root
		+ " right now (record these files in the plan, not the ones the report lists):\n" + text}}))


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"checkpoint-status error (not added): {error}", file=sys.stderr)
	sys.exit(0)

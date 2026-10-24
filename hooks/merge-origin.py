#!/usr/bin/env python3
"""PostToolUse(Bash) hook: run `git merge` of origin's default branch outside the sandbox.

Triggers only when the Bash command is exactly the merge-origin check script and it succeeded
with READY. Re-runs the script with --merge (which repeats every check) in the same worktree
and returns its output as additionalContext. Never runs on anything else.
"""
import json
import os
import subprocess
import sys

SCRIPT = os.path.expanduser("~/.claude/skills/feature-flow/merge-origin.sh")
COMMANDS = {
	"bash ~/.claude/skills/feature-flow/merge-origin.sh",
	f"bash {SCRIPT}",
}


def reply(text):
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}));
	sys.exit(0);


def main():
	try:
		data = json.load(sys.stdin);
	except Exception:
		sys.exit(0);
	if data.get("hook_event_name") != "PostToolUse" or data.get("tool_name") != "Bash":
		sys.exit(0);
	command = ((data.get("tool_input") or {}).get("command") or "").strip();
	if command not in COMMANDS:
		sys.exit(0);
	response = data.get("tool_response") or {};
	stdout = response.get("stdout") if isinstance(response, dict) else str(response);
	if "READY:" not in (stdout or ""):
		sys.exit(0);
	cwd = data.get("cwd") or "";
	if not os.path.isdir(cwd):
		reply("merge-origin hook: FAIL: session cwd not found; merge not run.");
	try:
		result = subprocess.run(["bash", SCRIPT, "--merge"], cwd=cwd, capture_output=True, text=True, timeout=120);
	except Exception as error:
		reply(f"merge-origin hook: FAIL: could not run the merge ({error}). Ask the user to run `git merge --no-ff --no-commit origin/<default>` in the worktree.");
	output = (result.stdout + result.stderr).strip();
	reply(f"merge-origin hook (outside the sandbox, exit {result.returncode}):\n{output}");


main();

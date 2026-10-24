"""PostToolUse (Bash): remove a finished worktree outside the sandbox.

Fires only when the Bash command is exactly
	bash ~/.claude/skills/finish-worktree/finish-worktree.sh <target>
It then runs the installed script (fixed path, not taken from the command) with --remove, outside
the sandbox. The script repeats every check (main checkout, fresh fetch, merged, clean) before it
removes anything. The result goes back to the main session as additionalContext.
"""
import json
import os
import re
import subprocess
import sys

SCRIPT = os.path.expanduser("~/.claude/skills/finish-worktree/finish-worktree.sh")
COMMAND = re.compile(r"^bash (?:~|%s)/\.claude/skills/finish-worktree/finish-worktree\.sh ([A-Za-z0-9][A-Za-z0-9/_-]*)$" % re.escape(os.path.expanduser("~")))


def report(text):
	print(json.dumps({
		"hookSpecificOutput": {
			"hookEventName": "PostToolUse",
			"additionalContext": "finish-worktree hook: " + text,
		}
	}))


def main():
	data = json.load(sys.stdin)
	if data.get("tool_name") != "Bash":
		return
	command = ((data.get("tool_input") or {}).get("command") or "").strip()
	match = COMMAND.match(command)
	if not match:
		return
	cwd = data.get("cwd") or os.getcwd()
	try:
		result = subprocess.run(["bash", SCRIPT, match.group(1), "--remove"], cwd=cwd, capture_output=True, text=True, timeout=120)
	except subprocess.TimeoutExpired:
		report("removal timed out after 120s; check `git worktree list`.")
		return
	output = (result.stdout + result.stderr).strip()
	if result.returncode == 0:
		report(output)
	else:
		report("not removed (exit %d): %s" % (result.returncode, output))


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		report(f"error: {error}")
	sys.exit(0)

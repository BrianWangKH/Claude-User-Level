"""PostToolUse(Bash) [every session]: remove a finished worktree outside the sandbox.

Fires only when the Bash command is exactly
	bash ~/.claude/skills/finish-worktree/finish-worktree.sh <target>
It then runs the installed script (fixed path, not taken from the command) with --remove, outside
the sandbox. The script repeats every check (main checkout, fresh fetch, merged, clean) before it
removes anything. The result goes back to the main session as additionalContext.

Reads: the hook input (command, cwd).
Writes: nothing itself (the script removes the worktree).
Output: check(data) returns the PostToolUse context, or None.
Fails: open — the tool already ran; an error comes back as the context "error: ..." (its own catch).
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio

SCRIPT = os.path.expanduser("~/.claude/skills/finish-worktree/finish-worktree.sh")
COMMAND = re.compile(r"^bash (?:~|%s)/\.claude/skills/finish-worktree/finish-worktree\.sh ([A-Za-z0-9][A-Za-z0-9/_-]*)$" % re.escape(os.path.expanduser("~")))


def report(text):
	return hookio.context("PostToolUse", "finish-worktree hook: " + text)


def remove(data):
	if data.get("tool_name") != "Bash":
		return None
	command = ((data.get("tool_input") or {}).get("command") or "").strip()
	match = COMMAND.match(command)
	if not match:
		return None
	cwd = data.get("cwd") or os.getcwd()
	try:
		result = subprocess.run(["bash", SCRIPT, match.group(1), "--remove"], cwd=cwd, capture_output=True, text=True, timeout=120)
	except subprocess.TimeoutExpired:
		return report("removal timed out after 120s; check `git worktree list`.")
	output = (result.stdout + result.stderr).strip()
	if result.returncode == 0:
		return report(output)
	return report("not removed (exit %d): %s" % (result.returncode, output))


def check(data):
	try:
		return remove(data)
	except Exception as error:
		return report(f"error: {error}")

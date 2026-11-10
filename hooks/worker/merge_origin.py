"""PostToolUse(Bash) [every session]: run `git merge` of the integration branch outside the sandbox.

Triggers only when the Bash command is exactly the merge-origin check script and it succeeded
with READY. Re-runs the script with --merge (which repeats every check) in the same worktree
and returns its output as additionalContext. Never runs on anything else.

Reads: the hook input (command, tool_response, cwd).
Writes: nothing itself (the script merges).
Output: check(data) returns the PostToolUse context, or None.
Fails: open — the tool already ran; a merge that can't run comes back as a FAIL context (its own catch).
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio

SCRIPT = os.path.expanduser("~/.claude/skills/feature-flow/merge-origin.sh")
COMMANDS = {
	"bash ~/.claude/skills/feature-flow/merge-origin.sh",
	f"bash {SCRIPT}",
}


def reply(text):
	return hookio.context("PostToolUse", text)


def check(data):
	if data.get("hook_event_name") != "PostToolUse" or data.get("tool_name") != "Bash":
		return None
	command = ((data.get("tool_input") or {}).get("command") or "").strip()
	if command not in COMMANDS:
		return None
	response = data.get("tool_response") or {}
	stdout = response.get("stdout") if isinstance(response, dict) else str(response)
	if "READY:" not in (stdout or ""):
		return None
	cwd = data.get("cwd") or ""
	if not os.path.isdir(cwd):
		return reply("merge-origin hook: FAIL: session cwd not found; merge not run.")
	try:
		result = subprocess.run(["bash", SCRIPT, "--merge"], cwd=cwd, capture_output=True, text=True, timeout=120)
	except Exception as error:
		return reply(f"merge-origin hook: FAIL: could not run the merge ({error}). Ask the user to run `git merge --no-ff --no-commit origin/<default>` in the worktree.")
	output = (result.stdout + result.stderr).strip()
	return reply(f"merge-origin hook (outside the sandbox, exit {result.returncode}):\n{output}")

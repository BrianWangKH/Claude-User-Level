"""Fetch origin before the worktree skills run.

The sandbox blocks network access to the git remote, but hooks run outside it. This hook runs a fixed
`git fetch origin` when new-worktree / finish-worktree is invoked, either by the model (PreToolUse on
Skill) or typed by the user (UserPromptSubmit), and records the result in
<git common dir>/claude-fetch-status as "<epoch> ok" or "<epoch> fail <detail>".
It never blocks and prints nothing; the skill scripts read the status file.
"""
import json
import os
import re
import subprocess
import sys
import time

SKILLS = {"new-worktree", "finish-worktree", "pr-conflict"}
PROMPT_PATTERN = re.compile(r"^\s*/(new-worktree|finish-worktree|pr-conflict)\b")


def wanted(data):
	event = data.get("hook_event_name")
	if event == "PreToolUse":
		toolInput = data.get("tool_input") or {}
		return data.get("tool_name") == "Skill" and toolInput.get("skill") in SKILLS
	if event == "UserPromptSubmit":
		return bool(PROMPT_PATTERN.match(data.get("prompt") or ""))
	return False


def main():
	data = json.load(sys.stdin)
	if not wanted(data):
		return
	cwd = data.get("cwd") or os.getcwd()
	try:
		commonDir = subprocess.run(
			["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
			capture_output=True, text=True, check=True, timeout=5,
		).stdout.strip()
	except (subprocess.SubprocessError, OSError):
		return
	try:
		result = subprocess.run(["git", "-C", cwd, "fetch", "--quiet", "origin"], capture_output=True, text=True, timeout=60)
		ok = result.returncode == 0
		lines = result.stderr.strip().splitlines()
		important = [l for l in lines if l.startswith(("fatal:", "error:"))]
		detail = (important or lines or ["exit %d" % result.returncode])[0]
	except subprocess.TimeoutExpired:
		ok, detail = False, "timeout after 60s"
	status = "%d ok" % time.time() if ok else "%d fail %s" % (time.time(), detail)
	with open(os.path.join(commonDir, "claude-fetch-status"), "w") as f:
		f.write(status + "\n")


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"fetch-origin error: {error}", file=sys.stderr)
	sys.exit(0)

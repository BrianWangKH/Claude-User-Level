"""PreToolUse(Skill), UserPromptSubmit [every session]: fetch origin before the worktree skills run.

The sandbox blocks network access to the git remote, but hooks run outside it. This hook runs a fixed
`git fetch origin` when new-worktree / finish-worktree is invoked, either by the model (PreToolUse on
Skill) or typed by the user (UserPromptSubmit), and records the result in
<git common dir>/claude-fetch-status as "<epoch> ok" or "<epoch> fail <detail>".
It never blocks and prints nothing; the skill scripts read the status file. In local mode
(homeconfig.flowMode of the checkout) the local default branch is the source of truth: no fetch, and the
scripts don't read the status.

Reads: the hook input (its cwd), git, homeconfig.flowMode.
Writes: <git common dir>/claude-fetch-status, <git common dir>/worktrees/.
Output: check(data) returns None.
Fails: closed on PreToolUse — an exception denies the skill call (hook-entry.py); on UserPromptSubmit the
prompt goes ahead and the person is warned once (hook-entry.py).
"""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402

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


def check(data):
	if not wanted(data):
		return None
	cwd = data.get("cwd") or os.getcwd()
	try:
		commonDir = subprocess.run(
			["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
			capture_output=True, text=True, check=True, timeout=5,
		).stdout.strip()
	except (subprocess.SubprocessError, OSError):
		return None
	# git deletes <common dir>/worktrees when its last worktree is removed, and a session's sandbox may
	# mount a missing .git/worktrees as an empty read-only directory, so new-worktree.sh can't create a
	# worktree. This hook runs outside the sandbox, so it makes sure the directory
	# exists; it also covers a new repo, which finish-worktree.sh's recreation does not.
	os.makedirs(os.path.join(commonDir, "worktrees"), exist_ok=True)
	if homeconfig.flowMode(cwd) == "local":
		return None
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
	return None


"""PreToolUse(Write|Edit|NotebookEdit) [every session, every agent]: deny a write outside the allowed directories.

Allowed: the session's checkout (CLAUDE_PROJECT_DIR), ~/.claude/plans, ~/.claude/agent-memory, the linked
worktrees when the checkout is the main one, the session's scratchpad_dir, and Markdown files under
~/.claude/projects/<slug>/memory/ (auto memory). Inside them the hook says nothing and the normal
permission flow decides.

Reads: the hook input, CLAUDE_PROJECT_DIR, `git worktree list`.
Writes: nothing.
Output: check(data) returns the PreToolUse deny, or None.
Fails: closed — a step of hook-entry.py's `pre` entry: an exception denies the call; no CLAUDE_PROJECT_DIR
denies too.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio


def deny(reason):
	raise hookio.Answer(hookio.refuse("PreToolUse", reason))


def isUnder(target, base):
	return target == base or target.startswith(base + os.sep)


def linkedWorktrees(root):
	"""Linked worktrees of root, only when root is the main checkout."""
	def git(*args):
		return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True, timeout=5).stdout.strip()

	try:
		if os.path.realpath(git("rev-parse", "--absolute-git-dir")) != os.path.realpath(git("rev-parse", "--path-format=absolute", "--git-common-dir")):
			return []
		listing = git("worktree", "list", "--porcelain")
	except (subprocess.SubprocessError, OSError):
		return []
	paths = [os.path.realpath(line[len("worktree "):]) for line in listing.splitlines() if line.startswith("worktree ")]
	return [p for p in paths if p != root]


def check(data):
	if not isinstance(data, dict):
		raise TypeError("hook input is not a JSON object")
	toolInput = data.get("tool_input") or {}
	path = toolInput.get("file_path") or toolInput.get("notebook_path")
	if not path:
		return None

	projectDir = os.environ.get("CLAUDE_PROJECT_DIR")
	if not projectDir:
		deny("CLAUDE_PROJECT_DIR not set; refusing write")

	home = os.path.expanduser("~")
	root = os.path.realpath(projectDir)

	allowed = [
		root,
		os.path.realpath(os.path.join(home, ".claude", "plans")),
		os.path.realpath(os.path.join(home, ".claude", "agent-memory")),
	]
	allowed += linkedWorktrees(root)
	scratchpad = data.get("scratchpad_dir")
	if scratchpad:
		allowed.append(os.path.realpath(scratchpad))

	raw = path if os.path.isabs(path) else os.path.join(root, path)
	target = os.path.realpath(raw)

	if any(isUnder(target, a) for a in allowed):
		return None  # no decision: the normal permission flow decides

	# auto memory: only ~/.claude/projects/<slug>/memory/**/*.md
	memBase = os.path.realpath(os.path.join(home, ".claude", "projects"))
	if isUnder(target, memBase):
		parts = os.path.relpath(target, memBase).split(os.sep)
		if len(parts) >= 3 and parts[1] == "memory" and target.endswith(".md"):
			return None

	deny(f"Write outside allowed directories blocked: {target}")

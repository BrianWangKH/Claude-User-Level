"""Where hooks keep their state: <checkout>/.hook-only/, one per checkout (main or worktree).

- .hook-only/                  agents may read, only hooks write
- .hook-only/hook-read-only/   agents may not even read
Enforced by the user settings (sandbox denyWrite ~/**/.hook-only and denyRead
~/**/.hook-only/hook-read-only, matching permissions.deny rules) and kept out of git by the global
gitignore. Hooks run outside the sandbox and create the directories on first use.

The checkout is resolved from CLAUDE_PROJECT_DIR, not the hook's cwd, so an agent that cds into a
scratch copy can't redirect where state is written or which tree is checked.
"""
import os
import subprocess

NAME = ".hook-only"
STRICT = "hook-read-only"


def startDir(data):
	return os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()


def gitRoot(data):
	"""Top of the session's git checkout, or None outside git."""
	try:
		out = subprocess.run(["git", "-C", startDir(data), "rev-parse", "--show-toplevel"],
			capture_output=True, text=True, check=True, timeout=5).stdout.strip()
	except (subprocess.SubprocessError, OSError):
		return None
	return os.path.realpath(out) if out else None


def projectRoot(data):
	"""The git checkout if there is one, else the session's project directory."""
	return gitRoot(data) or os.path.realpath(startDir(data))


def stateDir(root, name, strict=False):
	"""<root>/.hook-only/<name> (or .hook-only/hook-read-only/<name>); not created here."""
	parts = [root, NAME] + ([STRICT] if strict else []) + [name]
	return os.path.join(*parts)


def isHookOnlyPath(relPath):
	return relPath == NAME or relPath.startswith(NAME + "/")

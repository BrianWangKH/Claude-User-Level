#!/usr/bin/env python3
"""PreToolUse(Bash) hook: no merge commit while a conflict is still in the files.

Only while a merge is in progress (MERGE_HEAD; feature-flow merge.md, after merge-origin.sh), for a Bash
command that commits (`git commit`, `git merge --continue`), in any session. Denies when
  - a file is still unmerged (`git diff --diff-filter=U`), or
  - the index or the working tree of a changed file holds a conflict marker line (`<<<<<<<`, `>>>>>>>`
    or `|||||||`, alone or followed by a space) that neither parent has: a line both sides already had,
    such as docs that show git's markers, is content, not a leftover.
`=======` is left out: Markdown uses it as a heading underline, and a leftover conflict always keeps
its `<<<<<<<` or `>>>>>>>` line too (twse-material-info, 2026-10-31: a hand-resolved TODO.md kept
`>>>>>>> origin/main` and the full test suite, which reads no docs, passed; the merge review caught it).
A compound command (`git add f && git commit`) is checked before any of it runs, so the files are seen as
they were before its `git add`: the denial then says to run the earlier steps as their own call and the
commit alone (signal-eval, 2026-11-01: a correct resolution was refused and read as a false block).
Fails open on its own errors (stderr).
"""
import json
import re
import subprocess
import sys

import hookonly

COMMIT = re.compile(r"\bgit\b[^|;&]*\b(commit|merge\s+--continue)\b")
MARKER = re.compile(r"^(<{7}|>{7}|\|{7})( |$)")
MAX_LISTED = 20


def git(root, *args, check=True):
	result = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=30)
	if check and result.returncode != 0:
		raise RuntimeError(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()[:200]}")
	return result


def lines(data):
	if data is None or b"\0" in data:
		return []
	return data.decode("utf-8", errors="replace").splitlines()


def blob(root, spec):
	result = git(root, "show", spec, check=False)
	return result.stdout if result.returncode == 0 else None


def worktreeFile(root, path):
	try:
		with open(f"{root}/{path}", "rb") as f:
			return f.read()
	except OSError:
		return None


def leftovers(root):
	"""["path:line: text"] for marker lines in the index or the working tree that neither parent has."""
	names = git(root, "diff", "--name-only", "-z", "HEAD").stdout.split(b"\0")
	names += git(root, "diff", "--cached", "--name-only", "-z", "HEAD").stdout.split(b"\0")
	found = []
	for path in sorted({n.decode("utf-8", errors="replace") for n in names if n}):
		parents = set(lines(blob(root, f"HEAD:{path}"))) | set(lines(blob(root, f"MERGE_HEAD:{path}")))
		for where, data in (("staged", blob(root, f":0:{path}")), ("working tree", worktreeFile(root, path))):
			for number, text in enumerate(lines(data), 1):
				if MARKER.match(text) and text not in parents:
					found.append(f"{path}:{number} ({where}): {text[:80]}")
	return list(dict.fromkeys(found))


def compoundHint(command):
	"""The note for a commit chained after other steps, else ""."""
	match = COMMIT.search(command)
	before = command[:match.start()].strip() if match else ""
	if not before:
		return ""
	return ("\nThis command runs other steps before the commit (`" + before[:120] + "`), and the check ran before "
		"any of them: run those as their own Bash call first, then the commit alone.")


def deny(reason):
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
		"permissionDecisionReason": reason}}))


def main():
	data = json.load(sys.stdin)
	if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") != "Bash":
		return
	command = (data.get("tool_input") or {}).get("command") or ""
	if not COMMIT.search(command):
		return
	root = hookonly.gitRoot(data)
	if not root:
		return
	gitDir = git(root, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
	if git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode != 0:
		return
	unmerged = sorted(set(git(root, "diff", "--name-only", "--diff-filter=U").stdout.decode(errors="replace").split()))
	if unmerged:
		deny("merge-commit-guard: commit refused, these files are still unmerged: " + ", ".join(unmerged)
			+ ". Resolve each one (Edit), `git add` it with an explicit path, then commit." + compoundHint(command))
		return
	found = leftovers(root)
	if found:
		listed = found[:MAX_LISTED] + ([f"... and {len(found) - MAX_LISTED} more"] if len(found) > MAX_LISTED else [])
		deny("merge-commit-guard: commit refused, conflict markers that neither side of the merge has are "
			f"still in the files ({gitDir} has MERGE_HEAD):\n" + "\n".join(listed)
			+ "\nRemove them (Edit), `git add` those files again, then commit." + compoundHint(command))


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"merge-commit-guard error (not checked): {error}", file=sys.stderr)
	sys.exit(0)

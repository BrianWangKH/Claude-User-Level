"""Check that a repo is ready for the multi-agent flow.

Two callers:
  - SessionStart hook (`--hook`): main checkout only. Fixes what is safe to fix locally, then
    reports anything missing as additionalContext; prints nothing when all is well.
  - skill repo-plan (no flags): prints one line per check and a final STATE line. Report only.

Checks come from repo-check.json next to this script, so adding a check is one entry:
  {"id": ..., "type": "exists" | "ignored" | "grep", "path": ..., "pattern": ..., "kickoff": bool,
   "hint": ...}
  - exists:  path exists in the checkout
  - ignored: git ignores path (any source: global, .gitignore, .git/info/exclude)
  - grep:    file at path has a line matching the regex pattern
  - kickoff: missing means the repo was never set up -> STATE new (repo-plan kickoff mode)
Built in, always run: the worktrees/ directory beside the main checkout, and .hook-only/ being
ignored (fixed with --hook by adding it to .git/info/exclude: local, never committed, shared by
all worktrees).

Output lines: "OK <id>", "FIXED <id>: ...", "MISSING <id>: <hint>", then
"STATE: new | incomplete | ready".
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKS_FILE = os.environ.get("REPO_CHECK_FILE") or os.path.join(HERE, "repo-check.json")
HOOK_ONLY = ".hook-only"


def git(root, *args):
	return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def isIgnored(root, path):
	probe = path.rstrip("/") + "/.probe" if path.endswith("/") else path
	return git(root, "check-ignore", "-q", "--no-index", probe).returncode == 0


def builtIn(root, commonDir, fix):
	results = []
	mainRoot = os.path.dirname(commonDir)
	worktrees = os.path.join(os.path.dirname(mainRoot), "worktrees")
	if os.path.isdir(worktrees):
		results.append(("OK", "worktrees-dir", ""))
	else:
		results.append(("MISSING", "worktrees-dir",
			f"{worktrees} does not exist; clone with wclone, or create it in a terminal (the sandbox can't) and restart the session"))

	if isIgnored(root, HOOK_ONLY + "/"):
		results.append(("OK", "hook-only-ignored", ""))
	elif fix:
		exclude = os.path.join(commonDir, "info", "exclude")
		os.makedirs(os.path.dirname(exclude), exist_ok=True)
		with open(exclude, "a", encoding="utf-8") as f:
			f.write(f"\n{HOOK_ONLY}/\n")
		results.append(("FIXED", "hook-only-ignored", f"added {HOOK_ONLY}/ to .git/info/exclude"))
	else:
		results.append(("MISSING", "hook-only-ignored",
			f"{HOOK_ONLY}/ is not ignored; the SessionStart hook adds it to .git/info/exclude"))
	return results


def configured(root, fixKickoff):
	with open(CHECKS_FILE, encoding="utf-8") as f:
		checks = json.load(f)["checks"]
	results = []
	for check in checks:
		path = os.path.join(root, check["path"])
		kind = check["type"]
		if kind == "exists":
			ok = os.path.exists(path)
		elif kind == "ignored":
			ok = isIgnored(root, check["path"])
		elif kind == "grep":
			ok = False
			if os.path.isfile(path):
				pattern = re.compile(check["pattern"], re.MULTILINE)
				with open(path, encoding="utf-8", errors="replace") as f:
					ok = bool(pattern.search(f.read()))
		else:
			raise ValueError(f"unknown check type {kind!r} in {check['id']}")
		results.append(("OK" if ok else "MISSING", check["id"], "" if ok else check["hint"]))
		fixKickoff[check["id"]] = bool(check.get("kickoff"))
	return results


def run(cwd, fix):
	top = git(cwd, "rev-parse", "--show-toplevel")
	if top.returncode != 0:
		return None, "not a git repo"
	root = top.stdout.strip()
	gitDir = git(root, "rev-parse", "--absolute-git-dir").stdout.strip()
	commonDir = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
	if gitDir != commonDir:
		return None, "worktree"
	kickoff = {}
	results = builtIn(root, commonDir, fix) + configured(root, kickoff)
	missing = [r for r in results if r[0] == "MISSING"]
	if any(kickoff.get(r[1]) for r in missing):
		state = "new"
	elif missing:
		state = "incomplete"
	else:
		state = "ready"
	return results, state


def main():
	hook = "--hook" in sys.argv
	if hook:
		data = json.load(sys.stdin)
		cwd = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
	else:
		cwd = os.getcwd()
	results, state = run(cwd, fix=hook)
	if results is None:
		if not hook:
			print(f"SKIP: {state} (run from the main checkout)")
		return
	if not hook:
		for status, checkId, detail in results:
			print(f"{status} {checkId}" + (f": {detail}" if detail else ""))
		print(f"STATE: {state}")
		return
	notes = [f"{s} {i}: {d}" for s, i, d in results if s != "OK"]
	if not notes:
		return
	advice = {
		"new": "This repo hasn't been set up for the flow yet. Suggest the user run /repo-plan (kickoff).",
		"incomplete": "Suggest the user run /repo-plan to plan the missing items first.",
		"ready": "Nothing else is missing.",
	}[state]
	print(json.dumps({"hookSpecificOutput": {
		"hookEventName": "SessionStart",
		"additionalContext": "repo-check:\n" + "\n".join(notes) + "\n" + advice + " Mention this to the user.",
	}}))


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		# Informational hook: report, never block a session from starting.
		if "--hook" in sys.argv:
			print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
				"additionalContext": f"repo-check failed: {error}. Mention this to the user."}}))
		else:
			print(f"ERROR: {error}")
			sys.exit(1)

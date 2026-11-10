#!/usr/bin/env python3
"""Create a new repo for the flow from the home kit templates/new-repo/ (a tool, not a hook; the person runs it in
their own terminal through templates/new-repo.sh; an external test harness, not included, may run it too).

Usage: new-repo.py <name> --lang go|python [--parent <dir>]
  Makes <parent>/<name>-Workforest/<name>/ (the main checkout) and <parent>/<name>-Workforest/worktrees/ (where
  new-worktree.sh opens the topics); <parent> is the working directory unless --parent names another, never a
  fixed place. Refuses, before writing anything, when <parent> is no directory, is inside any git repository
  (another project, its worktrees or a .git directory: the new repo would nest in it; a .git entry above it or
  git's own answer, and when either can't be checked: insideRepo), or <name>-Workforest
  exists already. Then copies the kit (repo/ for every language, lang/<lang>/ on top; a path segment dot-<x>
  becomes .<x>, {{NAME}} becomes the name, {{ENVIRONMENT}} the language's environment.md) and the language's
  quality-gates template (templates/quality-gates.json for python, quality-gates.go.json for go) as
  .claude/quality-gates.json, runs git init -b main and one commit, records local flow mode in
  .hook-only/flow-mode and runs skills/repo-plan/repo-check.py, which must report every check OK and
  STATE: ready. Every git command runs with core.hooksPath=/dev/null and core.fsmonitor=false (GIT_SAFE), and every
  program it starts gets only the variables of ENV_PASS. Last line: where to open the session (the project directory: the person's home settings
  already give a session there ../worktrees as an extra directory, and a worker in worktrees/<topic> has no
  ../worktrees, so it can't see the other topics).
  Only the mechanical skeleton: the product, the scope table's content and the remote come from /repo-plan's
  kickoff (skills/repo-plan/kickoff-checklist.md).
Exit 0 created, 1 refused or a step failed (the message names it; a failed step leaves what it made), 2 usage.
Fails: closed — any refusal or failed step is exit 1 with the reason, never a partial repo reported as created.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402

KIT = os.path.join(homepath.HOME_ROOT, "templates", "new-repo")
GATES = {"python": "quality-gates.json", "go": "quality-gates.go.json"}  # templates/<file> per language
REPO_CHECK = os.path.join(homepath.HOME_ROOT, "skills", "repo-plan", "repo-check.py")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DOT_PREFIX = "dot-"
ENVIRONMENT_FILE = "environment.md"  # lang/<lang>/: the CLAUDE.md "Environment" text, not copied
FLOW_MODE = "local"  # homeconfig.FLOW_MODES: the queue and auto-merge run only in local mode
# The only variables a program started here gets (a whitelist, as the gates' gate_sandbox env_pass): the person's
# terminal may hold credentials or git's own variables (GIT_DIR, GIT_INDEX_FILE, ...: they would point a command at
# another repository), and neither belongs to git or repo-check.py here. The four GIT_*_NAME/EMAIL are only the
# commit's identity.
ENV_PASS = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME", "TMPDIR",
	"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
# Every git command here runs outside Claude Code's sandbox: no hook (the person's global core.hooksPath or a
# template's) and no fsmonitor program runs with it.
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


class Refused(Exception):
	pass


def childEnv():
	return {k: v for k, v in os.environ.items() if k in ENV_PASS}


def git(cwd, *args):
	return subprocess.run(["git", *GIT_SAFE, "-C", cwd, *args], capture_output=True, text=True, env=childEnv())


NOT_A_REPO = "not a git repository"  # git's own words when no repository holds the path


def insideRepo(path):
	"""What makes path part of a repository, or None. Two signals, either one refuses: a `.git` entry (a directory,
	a file, a link, anything) at path or above it, symlinks resolved; and git itself, unless it answers that no
	repository holds path. An ancestor that can't be checked, or any other git answer (a broken HEAD, a
	gitdir file pointing nowhere, dubious ownership), raises Refused: never nest when unsure."""
	current = os.path.realpath(path)
	while True:
		marker = os.path.join(current, ".git")
		try:
			os.lstat(marker)
			return marker
		except FileNotFoundError:
			pass
		except OSError as error:
			raise Refused(f"can't check {marker} ({error}); create the repo where every parent is readable") from error
		if os.path.dirname(current) == current:
			break
		current = os.path.dirname(current)
	result = git(path, "rev-parse", "--path-format=absolute", "--git-dir")
	if result.returncode == 0:
		return result.stdout.strip()
	if NOT_A_REPO not in result.stderr:
		raise Refused(f"git can't tell whether {path} is inside a repository: {result.stderr.strip()}")
	return None


def targetOf(name, parent):
	"""(workforest, main) for name under parent; Refused when it can't be created there (module doc)."""
	if not NAME.match(name):
		raise Refused(f"name {name!r}: lower-case letters, digits and '-', starting with a letter or digit")
	if not os.path.isdir(parent):
		raise Refused(f"{parent} is not a directory")
	holder = insideRepo(parent)
	if holder:
		raise Refused(f"{parent} is inside the git repository {holder}; create the repo outside any repository")
	workforest = os.path.join(parent, f"{name}-Workforest")
	if os.path.lexists(workforest):
		raise Refused(f"{workforest} exists already")
	return workforest, os.path.join(workforest, name)


def renamed(relative):
	"""A kit path with each dot-<x> segment turned into .<x>."""
	return os.path.join(*("." + part[len(DOT_PREFIX):] if part.startswith(DOT_PREFIX) else part
		for part in relative.split(os.sep)))


def copyKit(source, main, values, skip=()):
	"""Copy the files under source into main (renamed), filling each {{KEY}} of values in their text."""
	for folder, _dirs, files in os.walk(source):
		for file in sorted(files):
			path = os.path.join(folder, file)
			relative = os.path.relpath(path, source)
			if relative in skip:
				continue
			target = os.path.join(main, renamed(relative))
			os.makedirs(os.path.dirname(target), exist_ok=True)
			with open(path, encoding="utf-8") as f:
				text = f.read()
			for key, value in values.items():
				text = text.replace("{{" + key + "}}", value)
			with open(target, "w", encoding="utf-8") as f:
				f.write(text)
			shutil.copymode(path, target)


def run(step, cwd, *argv):
	result = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, env=childEnv())
	if result.returncode != 0:
		raise Refused(f"{step} failed (exit {result.returncode}): {(result.stderr or result.stdout).strip()}")
	return result.stdout


def create(name, lang, parent):
	"""Make the repo (module doc); returns the main checkout."""
	workforest, main = targetOf(name, os.path.abspath(parent))
	langDir = os.path.join(KIT, "lang", lang)
	with open(os.path.join(langDir, ENVIRONMENT_FILE), encoding="utf-8") as f:
		values = {"NAME": name, "ENVIRONMENT": f.read().strip()}
	os.makedirs(main)
	os.makedirs(os.path.join(workforest, "worktrees"))
	copyKit(os.path.join(KIT, "repo"), main, values)
	copyKit(langDir, main, values, skip=(ENVIRONMENT_FILE,))
	shutil.copyfile(os.path.join(homepath.HOME_ROOT, "templates", GATES[lang]), os.path.join(main, ".claude", "quality-gates.json"))
	run("git init", main, "git", *GIT_SAFE, "init", "-q", "-b", "main")
	run("git add", main, "git", *GIT_SAFE, "add", "-A")
	run("git commit", main, "git", *GIT_SAFE, "commit", "-q", "-m", f"Initialize {name} from the home new-repo kit")
	record = os.path.join(main, *homeconfig.FLOW_RECORD)
	os.makedirs(os.path.dirname(record))
	with open(record, "w", encoding="utf-8") as f:
		f.write(FLOW_MODE + "\n")
	report = run("repo-check.py", main, sys.executable, REPO_CHECK)
	lines = report.strip().splitlines()
	if not lines or lines[-1] != "STATE: ready" or any(not line.startswith("OK ") for line in lines[:-1]):
		raise Refused(f"repo-check.py doesn't pass in {main}:\n{report.strip()}")
	return main


def main(argv):
	parser = argparse.ArgumentParser(prog="new-repo.sh", description="Create a new repo for the flow from the home kit.")
	parser.add_argument("name")
	parser.add_argument("--lang", choices=sorted(GATES), required=True)
	parser.add_argument("--parent", default=os.getcwd())
	args = parser.parse_args(argv)
	try:
		created = create(args.name, args.lang, args.parent)
	except (Refused, OSError) as error:
		print(f"FAIL: new-repo: {error}", file=sys.stderr)
		return 1
	print(f"created: {created}")
	print(f"next: open a Claude Code session in {created} (the project directory, not the Workforest), then /repo-plan for the kickoff")
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))

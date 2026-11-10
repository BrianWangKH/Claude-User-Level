"""templates/new-repo.sh and hooks/tools/new-repo.py: a new repo for the flow from the home kit, in the working
directory or --parent, refused inside any git repository or over an existing Workforest, passing repo-check.

Every test runs the tools from a copy of the tree under test (config_fixtures.copyHome, with skills/ and
templates/), so the kit, the quality-gates templates and repo-check.py are the tree's own.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
GO = shutil.which("go") or "/usr/local/go/bin/go"


@pytest.fixture
def home(tmp_path):
	return config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home", tops=("skills", "templates"))


def newRepo(home, cwd, *args, env=None):
	return subprocess.run(["bash", str(home / "templates" / "new-repo.sh"), *args], cwd=cwd, capture_output=True,
		text=True, timeout=120, env={**os.environ, **GIT_IDENTITY, **(env or {})})


def git(cwd, *args):
	return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)


def pathWith(directory):
	"""PATH with directory first (a fake tool in tmp_path shadows the real one)."""
	return f"{directory}{os.pathsep}{os.environ['PATH']}"


def repoCheck(home, main):
	return subprocess.run([sys.executable, str(home / "skills" / "repo-plan" / "repo-check.py")], cwd=main,
		capture_output=True, text=True, timeout=60).stdout.splitlines()


@pytest.mark.parametrize("lang", ["go", "python"])
def test_a_new_repo_has_the_layout_and_passes_repo_check(home, tmp_path, lang):
	parent = tmp_path / "projects"
	parent.mkdir()
	result = newRepo(home, tmp_path, "demo-repo", "--lang", lang, "--parent", str(parent))
	assert result.returncode == 0, result.stderr
	main = parent / "demo-repo-Workforest" / "demo-repo"
	assert sorted(os.listdir(main.parent)) == ["demo-repo", "worktrees"]
	assert result.stdout.splitlines() == [f"created: {main}", f"next: open a Claude Code session in {main} (the project "
		"directory, not the Workforest), then /repo-plan for the kickoff"]
	assert git(main, "log", "--format=%s").stdout == "Initialize demo-repo from the home new-repo kit\n"
	assert git(main, "status", "--porcelain").stdout == ""
	assert (main / ".hook-only" / "flow-mode").read_text() == "local\n"
	assert (main / ".gitignore").read_text().splitlines() == ["plans/", "data/", ".hook-only/"]
	for path in (".claude/flow.json", ".claude/guard-paths.json", ".claude/quality-gates.json", ".claude/skills/project-flow/SKILL.md",
			*(f".claude/skills/{a}-notes/SKILL.md" for a in ("developer", "qa", "code-reviewer", "spec-reviewer"))):
		assert (main / path).is_file(), path
	gates = "quality-gates.go.json" if lang == "go" else "quality-gates.json"
	assert (main / ".claude" / "quality-gates.json").read_text() == (home / "templates" / gates).read_text()
	text = (main / "CLAUDE.md").read_text()
	assert "## Branch scopes" in text and "## Environment" in text and "{{" not in text and "demo-repo" in text
	assert not any(name.startswith("dot-") or name == "environment.md" for _root, dirs, files in os.walk(main)
		for name in dirs + files)
	lines = repoCheck(home, main)
	assert lines[-1] == "STATE: ready" and all(line.startswith("OK ") for line in lines[:-1]), lines


@pytest.mark.skipif(not os.path.exists(GO), reason="no go toolchain on this machine")
def test_the_go_kit_builds_tests_and_passes_its_checks(home, tmp_path):
	assert newRepo(home, tmp_path, "go-demo", "--lang", "go").returncode == 0
	main = tmp_path / "go-demo-Workforest" / "go-demo"
	for argv in ([GO, "test", "-count=1", "./..."], [GO, "vet", "./..."]):
		result = subprocess.run(argv, cwd=main, capture_output=True, text=True, timeout=300)
		assert result.returncode == 0, result.stdout + result.stderr
	gofmt = subprocess.run([os.path.join(os.path.dirname(GO), "gofmt"), "-l", "."], cwd=main, capture_output=True, text=True)
	assert gofmt.returncode == 0 and gofmt.stdout == ""


def test_the_working_directory_is_the_default_parent(home, tmp_path):
	result = newRepo(home, tmp_path, "here-repo", "--lang", "go")
	assert result.returncode == 0, result.stderr
	assert (tmp_path / "here-repo-Workforest" / "here-repo" / ".git").is_dir()


def test_an_existing_workforest_is_refused_and_left_as_it_is(home, tmp_path):
	(tmp_path / "taken-Workforest").mkdir()
	(tmp_path / "taken-Workforest" / "mine.txt").write_text("keep\n")
	result = newRepo(home, tmp_path, "taken", "--lang", "go")
	assert result.returncode == 1 and "exists already" in result.stderr and result.stdout == ""
	assert os.listdir(tmp_path / "taken-Workforest") == ["mine.txt"]


@pytest.mark.parametrize("where", ["top", "subdirectory", "git dir", "worktrees of another project"])
def test_a_parent_inside_any_git_repository_is_refused_before_anything_is_written(home, tmp_path, where):
	project = tmp_path / "other-Workforest" / "other"
	project.mkdir(parents=True)
	assert git(project, "init", "-q", "-b", "main").returncode == 0
	assert git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x").returncode == 0
	topic = tmp_path / "other-Workforest" / "worktrees" / "topic"
	assert git(project, "worktree", "add", "-q", "-b", "feature/x/topic", str(topic)).returncode == 0
	parent = {"top": project, "subdirectory": project / "src", "git dir": project / ".git",
		"worktrees of another project": topic}[where]
	parent.mkdir(exist_ok=True)
	before = sorted(os.listdir(parent))
	result = newRepo(home, tmp_path, "nested", "--lang", "go", "--parent", str(parent))
	assert result.returncode == 1 and "inside the git repository" in result.stderr
	assert sorted(os.listdir(parent)) == before


def test_bad_names_and_parents_are_refused_and_usage_errors_exit_2(home, tmp_path):
	for name in ("Upper", "_under", "a/b", "x" * 65):
		result = newRepo(home, tmp_path, name, "--lang", "go")
		assert result.returncode == 1 and "name" in result.stderr, name
	assert newRepo(home, tmp_path, "ok-name", "--lang", "go", "--parent", str(tmp_path / "missing")).returncode == 1
	assert newRepo(home, tmp_path).returncode == 2
	missing = newRepo(home, tmp_path, "x")
	assert missing.returncode == 2 and "--lang" in missing.stderr and "go" in missing.stderr and "python" in missing.stderr
	assert newRepo(home, tmp_path, "x", "--lang", "rust").returncode == 2
	assert os.listdir(tmp_path) == ["home"]


def test_no_place_is_hard_coded():
	for path in ("tools/new-repo.py",):
		with open(os.path.join(HOOKS, path), encoding="utf-8") as f:
			text = f.read()
		assert "Project" not in text and "expanduser" not in text and "Path.home" not in text


def test_a_repo_check_that_does_not_pass_fails_the_creation_naming_it(home, tmp_path):
	checks = home / "skills" / "repo-plan" / "repo-check.json"
	data = json.loads(checks.read_text())
	data["checks"].append({"id": "kit-gap", "type": "exists", "path": "NOT-IN-THE-KIT.md", "hint": "a check the kit misses"})
	checks.write_text(json.dumps(data))
	result = newRepo(home, tmp_path, "gap-repo", "--lang", "go")
	assert result.returncode == 1 and "repo-check.py doesn't pass" in result.stderr
	assert "MISSING kit-gap: a check the kit misses" in result.stderr and "created:" not in result.stdout


@pytest.mark.parametrize("broken", ["garbage HEAD", "gitdir file pointing nowhere", "a .git symlink to nothing"])
def test_a_parent_below_a_git_entry_git_cant_read_is_refused(home, tmp_path, broken):
	"""git rev-parse fails on these (so it can't say "inside"); the .git entry above still refuses."""
	above = tmp_path / "odd"
	above.mkdir()
	if broken == "garbage HEAD":
		(above / ".git").mkdir()
		(above / ".git" / "HEAD").write_text("garbage\n")
	elif broken == "gitdir file pointing nowhere":
		(above / ".git").write_text("gitdir: /nonexistent/x.git\n")
	else:
		(above / ".git").symlink_to(tmp_path / "nothing")
	parent = above / "sub"
	parent.mkdir()
	assert git(parent, "rev-parse", "--git-dir").returncode != 0  # the premise: git can't read it
	result = newRepo(home, tmp_path, "nested", "--lang", "go", "--parent", str(parent))
	assert result.returncode == 1 and "inside the git repository" in result.stderr and str(above / ".git") in result.stderr
	assert os.listdir(parent) == []


def test_any_other_git_answer_refuses(home, tmp_path):
	"""Dubious ownership (or any error but "not a git repository") is never read as "not inside"."""
	fakeBin = tmp_path / "bin"
	fakeBin.mkdir()
	(fakeBin / "git").write_text("#!/usr/bin/env bash\necho \"fatal: detected dubious ownership in repository at '$PWD'\" >&2\nexit 128\n")
	(fakeBin / "git").chmod(0o755)
	parent = tmp_path / "plain"
	parent.mkdir()
	result = newRepo(home, tmp_path, "owned", "--lang", "go", "--parent", str(parent),
		env={"PATH": pathWith(fakeBin)})
	assert result.returncode == 1 and "git can't tell" in result.stderr and "dubious ownership" in result.stderr
	assert os.listdir(parent) == []



def recordingGit(tmp_path):
	"""A git on PATH that appends its argv and environment (one JSON line per call) to calls.jsonl, then runs the
	real git."""
	real = shutil.which("git")
	fakeBin = tmp_path / "recbin"
	fakeBin.mkdir()
	log = tmp_path / "calls.jsonl"
	(fakeBin / "git").write_text(f"#!{sys.executable}\nimport json, os, sys\n"
		f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ)}}) + '\\n')\n"
		f"os.execv({real!r}, [{real!r}] + sys.argv[1:])\n")
	(fakeBin / "git").chmod(0o755)
	return fakeBin, log


def test_every_git_command_runs_without_hooks_or_fsmonitor_and_with_only_the_whitelisted_variables(home, tmp_path):
	"""The person's global git config may name a hooks directory or an fsmonitor program: neither runs on the
	first commit; a credential-looking variable or git's own GIT_DIR never reaches a program new-repo starts."""
	userHome = tmp_path / "user"
	userHome.mkdir()
	marker = tmp_path / "ran.txt"
	hooks = tmp_path / "global-hooks"
	hooks.mkdir()
	for name in ("pre-commit", "post-commit", "commit-msg"):
		(hooks / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
		(hooks / name).chmod(0o755)
	monitor = tmp_path / "monitor.sh"
	monitor.write_text(f"#!/bin/sh\necho fsmonitor >> {marker}\nexit 1\n")
	monitor.chmod(0o755)
	(userHome / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {monitor}\n")
	fakeBin, log = recordingGit(tmp_path)
	parent = tmp_path / "projects"
	parent.mkdir()
	result = newRepo(home, tmp_path, "safe-repo", "--lang", "go", "--parent", str(parent), env={
		"HOME": str(userHome), "PATH": pathWith(fakeBin),
		"REHEARSAL_SECRET_TOKEN": "leak", "GIT_DIR": str(tmp_path / "elsewhere.git")})
	assert result.returncode == 0, result.stderr
	assert not marker.exists(), marker.read_text()
	calls = [json.loads(line) for line in log.read_text().splitlines()]
	ours = [c for c in calls if {"init", "add", "commit", "--git-dir"} & set(c["argv"])]
	assert [next(a for a in ("--git-dir", "init", "add", "commit") if a in c["argv"]) for c in ours] == [
		"--git-dir", "init", "add", "commit"]
	for call in calls:
		assert "REHEARSAL_SECRET_TOKEN" not in call["env"] and "GIT_DIR" not in call["env"], call["argv"]
	for call in ours:  # new-repo's own git commands (repo-check.py's are its own business)
		argv = call["argv"]
		assert argv[:4] == ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"], argv
	assert set(calls[0]["env"]) <= set(envPass())


def envPass():
	spec = importlib.util.spec_from_file_location("new_repo_tool", os.path.join(HOOKS, "tools", "new-repo.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module.ENV_PASS

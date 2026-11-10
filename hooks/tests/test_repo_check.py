"""skills/repo-plan/repo-check.py, the SessionStart registration of settings.json (hook_entries.REGISTRATIONS):
in the main checkout it adds .hook-only/ to .git/info/exclude, then says what the repo lacks for the flow
(additionalContext) or nothing; a linked worktree is skipped. Without --hook (skill repo-plan) it prints one line
per check and the repo's STATE. The checks come from --checks (a test's own list here; no environment variable
can name them); HOME and
XDG_CONFIG_HOME are scratch directories, so the person's global git ignore (which may hold .hook-only/) plays no
part.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_repo_check.py -q -p no:cacheprovider'
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

HOME_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(HOME_ROOT, "skills", "repo-plan", "repo-check.py")
CHECKS = {"checks": [
	{"id": "project-claude-md", "type": "exists", "path": "CLAUDE.md", "kickoff": True, "hint": "no CLAUDE.md"},
	{"id": "plans-ignored", "type": "ignored", "path": "plans/", "hint": "plans/ not ignored"},
]}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
	"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def isolated(repo, projectDir):
	"""The environment of a run: no global git config or ignore file, the test's checks, the session's checkout."""
	scratch = repo.parent.parent / "home"
	return {**os.environ, **GIT_ENV, "HOME": str(scratch), "XDG_CONFIG_HOME": str(scratch / ".config"),
		"CLAUDE_PROJECT_DIR": str(projectDir)}


def git(repo, *args):
	subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=isolated(repo, repo))


@pytest.fixture
def main(tmp_path):
	"""A main checkout with a worktrees/ directory beside it and the test's checks file."""
	repo = tmp_path / "proj" / "main"
	repo.mkdir(parents=True)
	(tmp_path / "proj" / "worktrees").mkdir()
	git(repo, "init", "-q", "-b", "main")
	git(repo, "commit", "-q", "--allow-empty", "-m", "base")
	(tmp_path / "checks.json").write_text(json.dumps(CHECKS))
	return repo


def run(repo, *args, data=None, env=None):
	"""repo-check.py with the test's checks (--checks)."""
	checks = ["--checks", str(repo.parent.parent / "checks.json")]
	result = subprocess.run([sys.executable, SCRIPT, *args, *checks], cwd=repo, input=json.dumps(data or {}),
		capture_output=True, text=True, timeout=60, env={**isolated(repo, repo), **(env or {})})
	assert result.returncode == 0, result.stderr
	return result.stdout


def test_the_report_names_each_check_and_a_repo_never_set_up_is_new(main):
	lines = run(main).splitlines()
	assert lines == ["OK worktrees-dir", "MISSING hook-only-ignored: .hook-only/ is not ignored; the SessionStart "
		"hook adds it to .git/info/exclude", "MISSING project-claude-md: no CLAUDE.md",
		"MISSING plans-ignored: plans/ not ignored", "STATE: new"]
	assert not (main / ".git" / "info" / "exclude").read_text().count(".hook-only/")  # report only


def test_the_hook_ignores_hook_only_and_tells_what_is_missing(main):
	out = json.loads(run(main, "--hook", data={"cwd": str(main)}))["hookSpecificOutput"]
	assert out["hookEventName"] == "SessionStart"
	assert "FIXED hook-only-ignored" in out["additionalContext"] and "/repo-plan (kickoff)" in out["additionalContext"]
	assert ".hook-only/" in (main / ".git" / "info" / "exclude").read_text()
	(main / "CLAUDE.md").write_text("# x\n")
	(main / ".gitignore").write_text("plans/\n")
	assert run(main, "--hook", data={"cwd": str(main)}) == ""  # all well: nothing to say
	assert run(main).splitlines()[-1] == "STATE: ready"


def test_a_linked_worktree_is_skipped(main):
	worktree = main.parent / "worktrees" / "t"
	git(main, "worktree", "add", "-q", "-b", "t", str(worktree))
	result = subprocess.run([sys.executable, SCRIPT, "--hook"], cwd=worktree, input="{}", capture_output=True,
		text=True, timeout=60, env=isolated(main, worktree))
	assert result.returncode == 0 and result.stdout == ""
	assert ".hook-only/" not in (main / ".git" / "info" / "exclude").read_text()


def test_no_environment_variable_names_the_checks(main, tmp_path):
	"""REPO_CHECK_FILE (once the tests' switch) is never read: a repo's settings env can't swap the checks."""
	(tmp_path / "other.json").write_text(json.dumps({"checks": []}))
	plain = run(main)
	assert run(main, env={"REPO_CHECK_FILE": str(tmp_path / "other.json")}) == plain


@pytest.mark.parametrize("heading, found", [
	("## Branch scopes", True),
	("### Scope table", True),
	("## Branch 範圍", True),  # a project CLAUDE.md written before the English headings
	("## 範圍表", True),
	("## Branches", False),
	("Branch scopes (not a heading)", False),
])
def test_the_scope_table_check_accepts_the_english_and_the_older_chinese_heading(main, tmp_path, heading, found):
	"""The home's own scope-table check (repo-check.json): existing repos keep their Chinese heading, new ones get
	the English one; both satisfy it."""
	with open(os.path.join(HOME_ROOT, "skills", "repo-plan", "repo-check.json"), encoding="utf-8") as f:
		check = next(c for c in json.load(f)["checks"] if c["id"] == "scope-table")
	(tmp_path / "checks.json").write_text(json.dumps({"checks": [check]}))
	(main / "CLAUDE.md").write_text(f"# Project\n\n{heading}\n\n| scope | dir |\n")
	out = run(main)
	assert ("MISSING scope-table" not in out) == found, out


def test_no_git_call_runs_the_repo_s_hooks_or_fsmonitor(main, tmp_path):
	"""repo-check.py runs outside the sandbox when tools/new-repo.py calls it: every git call carries
	core.hooksPath=/dev/null and core.fsmonitor=false, so a repo whose hooks or fsmonitor write a marker never
	gets it written (a recording git on PATH proves the options, the marker proves the effect)."""
	marker = tmp_path / "ran"
	script = tmp_path / "writes-marker"
	script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {marker}\nexit 0\n")
	script.chmod(0o755)
	hooksDir = tmp_path / "repo-hooks"
	hooksDir.mkdir()
	for name in ("post-checkout", "post-index-change", "pre-commit", "reference-transaction"):
		os.symlink(script, hooksDir / name)
	git(main, "config", "core.hooksPath", str(hooksDir))
	git(main, "config", "core.fsmonitor", str(script))
	realGit = shutil.which("git")
	fakeBin = tmp_path / "bin"
	fakeBin.mkdir()
	calls = tmp_path / "calls"
	(fakeBin / "git").write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\nexec {realGit} \"$@\"\n")
	(fakeBin / "git").chmod(0o755)
	path = {"PATH": os.pathsep.join([str(fakeBin), os.path.dirname(realGit)])}
	run(main, env=path)
	run(main, "--hook", data={"cwd": str(main)}, env=path)
	lines = calls.read_text().splitlines()
	assert lines and all(l.startswith("-c core.hooksPath=/dev/null -c core.fsmonitor=false ") for l in lines), lines
	assert not marker.exists(), marker.read_text()

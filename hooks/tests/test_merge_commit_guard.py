"""merge_commit_guard.py on a real merge conflict in a temporary repo.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_merge_commit_guard.py
"""
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
	"GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args, check=True):
	return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check,
		env={**os.environ, **GIT_ENV})


def commitAll(repo, message):
	git(repo, "add", "-A")
	git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def conflicted(tmp_path):
	"""A repo mid-merge: TODO.md conflicts, notes.md (with git's markers as content on both sides) merges."""
	repo = tmp_path / "repo"
	repo.mkdir()
	git(repo, "init", "-q", "-b", "main")
	(repo / "TODO.md").write_text("Title\n=======\n- a\n")
	(repo / "notes.md").write_text("Git shows:\n<<<<<<< HEAD\n>>>>>>> other\n")
	commitAll(repo, "base")
	git(repo, "checkout", "-q", "-b", "feature")
	(repo / "TODO.md").write_text("Title\n=======\n- a\n- feature\n")
	commitAll(repo, "feature")
	git(repo, "checkout", "-q", "main")
	(repo / "TODO.md").write_text("Title\n=======\n- a\n- main\n")
	commitAll(repo, "main")
	git(repo, "checkout", "-q", "feature")
	assert git(repo, "merge", "--no-ff", "--no-commit", "main", check=False).returncode != 0
	return repo


def run(repo, command="git commit --no-edit"):
	data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
		"cwd": str(repo), "session_id": "s"}
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "hook-entry.py"), "check", "merge-commit-guard"], input=json.dumps(data),
		capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert "error" not in result.stderr, result.stderr
	return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if result.stdout.strip() else None


def resolve(repo, text):
	(repo / "TODO.md").write_text(text)
	git(repo, "add", "TODO.md")


def test_unmerged_file_is_refused(conflicted):
	reason = run(conflicted)
	assert reason and "still unmerged: TODO.md" in reason


def test_leftover_marker_is_refused(conflicted):
	resolve(conflicted, "Title\n=======\n- a\n- feature\n- main\n>>>>>>> main\n")
	reason = run(conflicted)
	assert reason and "TODO.md:6 (staged): >>>>>>> main" in reason


def test_clean_resolution_passes_with_markdown_underline_and_markers_both_sides_had(conflicted):
	resolve(conflicted, "Title\n=======\n- a\n- feature\n- main\n")
	assert run(conflicted) is None
	assert run(conflicted, "git merge --continue") is None


def test_marker_left_in_the_index_is_refused_even_when_the_working_tree_is_fixed(conflicted):
	resolve(conflicted, "- feature\n<<<<<<< HEAD\n")
	(conflicted / "TODO.md").write_text("Title\n=======\n- a\n- feature\n- main\n")
	reason = run(conflicted)
	assert reason and "TODO.md:2 (staged): <<<<<<< HEAD" in reason and "working tree" not in reason


def test_marker_only_in_the_working_tree_is_refused(conflicted):
	resolve(conflicted, "Title\n=======\n- a\n- feature\n- main\n")
	(conflicted / "TODO.md").write_text("Title\n=======\n- a\n||||||| base\n")
	reason = run(conflicted, "git commit -a --no-edit")
	assert reason and "TODO.md:4 (working tree): ||||||| base" in reason


def test_no_merge_in_progress_is_not_checked(tmp_path):
	repo = tmp_path / "plain"
	repo.mkdir()
	git(repo, "init", "-q", "-b", "main")
	(repo / "a.md").write_text("x\n")
	commitAll(repo, "base")
	(repo / "a.md").write_text(">>>>>>> leftover\n")
	git(repo, "add", "a.md")
	assert run(repo) is None


def test_other_commands_are_not_checked(conflicted):
	assert run(conflicted, "git status --short") is None
	assert run(conflicted, "git log --oneline -3") is None


def test_a_compound_add_and_commit_is_still_refused_and_told_to_split(conflicted):
	(conflicted / "TODO.md").write_text("Title\n=======\n- a\n- feature\n- main\n")
	reason = run(conflicted, "git add TODO.md && git commit --no-edit")
	assert reason and "still unmerged: TODO.md" in reason
	assert "run those as their own Bash call first" in reason and "`git add TODO.md &&`" in reason


def test_a_plain_commit_gets_no_split_hint(conflicted):
	reason = run(conflicted)
	assert reason and "own Bash call" not in reason

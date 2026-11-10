"""wt_controls.py's two git readers: behindOrigin (how many commits origin's copy of the local main has that the
local main lacks; `wt local on` is refused while it is above 0 or unknown) and changedFiles (the branch's changed
paths against the integration branch, for the pane's merge button).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_wt_controls_git.py -q -p no:cacheprovider'
"""

import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import wt_controls  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
	"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
		env={**os.environ, **GIT_ENV}).stdout.strip()


def commit(repo, name):
	"""A commit adding name; each file holds its own name, so git never reads two files as one renamed."""
	path = repo / name
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(name + "\n")
	git(repo, "add", name)
	git(repo, "commit", "-q", "-m", f"add {name}")


@pytest.fixture(autouse=True)
def recordsDecide(monkeypatch):
	"""The records decide the mode here: no FLOW_INTEGRATION test override (conftest.py)."""
	monkeypatch.delenv("FLOW_INTEGRATION", raising=False)


@pytest.fixture
def origin(tmp_path):
	"""A local bare origin (no network) with one commit on main, and a second clone to push from."""
	bare = tmp_path / "origin.git"
	git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
	other = tmp_path / "other"
	git(tmp_path, "clone", "-q", str(bare), str(other))
	git(other, "checkout", "-q", "-b", "main")
	commit(other, "base.txt")
	git(other, "push", "-q", "origin", "main")
	return bare, other


@pytest.fixture
def main(tmp_path, origin):
	"""The main checkout: a clone of origin in local mode."""
	m = tmp_path / "proj" / "main"
	git(tmp_path, "clone", "-q", str(origin[0]), str(m))
	(m / ".hook-only").mkdir()
	(m / ".hook-only" / "flow-mode").write_text("local\n")
	(m / ".git" / "info" / "exclude").write_text(".hook-only/\n")
	return m


# -- behindOrigin ----------------------------------------------------------------------------------------


def test_behind_origin_counts_the_commits_the_local_main_lacks(main, origin):
	"""Unit: 0 when even; after two commits pushed elsewhere and a fetch, 2; the local main's own commits don't
	count (they are ahead, not behind)."""
	_bare, other = origin
	assert wt_controls.behindOrigin(main) == 0
	commit(other, "one.txt")
	commit(other, "two.txt")
	git(other, "push", "-q", "origin", "main")
	assert wt_controls.behindOrigin(main) == 0  # not fetched: origin/main is the old copy
	git(main, "fetch", "-q", "origin")
	assert wt_controls.behindOrigin(main) == 2
	commit(main, "local.txt")
	assert wt_controls.behindOrigin(main) == 2
	git(main, "merge", "-q", "--no-edit", "origin/main")
	assert wt_controls.behindOrigin(main) == 0


def test_behind_origin_is_unknown_without_an_origin_copy_or_a_local_main(tmp_path):
	"""Unit: None (can't tell) without origin/main, and without a local main or master branch."""
	repo = tmp_path / "solo"
	repo.mkdir()
	git(repo, "init", "-q", "-b", "main")
	commit(repo, "a.txt")
	assert wt_controls.behindOrigin(repo) is None  # no origin/main
	git(repo, "branch", "-q", "-m", "trunk")
	assert wt_controls.behindOrigin(repo) is None  # no main or master


def test_local_on_is_refused_while_the_main_is_behind_or_unknown(main, origin):
	"""Unit: the blocker the pane shows for `local on` follows behindOrigin; `local off` never asks it."""
	_bare, other = origin
	commit(other, "one.txt")
	git(other, "push", "-q", "origin", "main")
	git(main, "fetch", "-q", "origin")
	behind = wt_controls.behindOrigin(main)
	assert wt_controls.localBlocker([], True, behind) == "the local main is 1 commit(s) behind origin/main; git pull first"
	assert wt_controls.localBlocker([], True, None).startswith("can't compare the local main with origin/main")
	assert wt_controls.localBlocker([], False, behind) is None


# -- changedFiles --------------------------------------------------------------------------------------------


@pytest.fixture
def worktree(main):
	wt = main.parent / "worktrees" / "topic-a"
	git(main, "worktree", "add", "-q", "-b", "feature/docs/topic-a", str(wt))
	return wt


def test_changed_files_lists_the_branchs_own_changes_against_the_local_main(main, worktree):
	"""Unit: the paths the branch changed since it left the integration branch; a later commit on main is not
	the branch's."""
	commit(worktree, "docs/a.md")
	commit(worktree, "src/b.py")
	commit(main, "main-only.txt")
	assert wt_controls.changedFiles(worktree) == ["docs/a.md", "src/b.py"]


def test_changed_files_stops_at_twelve(worktree):
	"""Unit: at most 12 paths."""
	for i in range(14):
		commit(worktree, f"f{i:02d}.txt")
	assert wt_controls.changedFiles(worktree) == [f"f{i:02d}.txt" for i in range(12)]


def test_changed_files_is_empty_when_git_cant_tell(main, worktree):
	"""Unit: [] for a flow mode that can't be read (ConfigError), for no integration branch (PR mode without
	origin/main), and when git diff fails; never an error."""
	commit(worktree, "a.txt")
	record = worktree / ".hook-only" / "flow-mode"
	record.parent.mkdir(exist_ok=True)
	record.write_text("bogus\n")
	assert wt_controls.changedFiles(worktree) == []
	record.write_text("origin\n")
	git(main, "remote", "remove", "origin")
	assert wt_controls.changedFiles(worktree) == []
	record.write_text("local\n")
	assert wt_controls.changedFiles(worktree) == ["a.txt"]
	git(worktree, "checkout", "-q", "--orphan", "lonely")  # an unborn HEAD: git diff main...HEAD fails
	assert wt_controls.changedFiles(worktree) == []

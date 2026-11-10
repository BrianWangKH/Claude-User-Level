"""homeconfig.integrationBase: the merge-base the planning session's run-result.py and worker-status.py diff a
topic against (their own copies, topicBase, before).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_integration_base.py -q -p no:cacheprovider'
"""

import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import homeconfig  # noqa: E402

pytestmark = pytest.mark.flow_mode("local")


def git(repo, *args):
	return subprocess.run(
		["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
		check=True,
		capture_output=True,
		text=True,
		timeout=60,
	).stdout.strip()


def test_the_base_is_the_merge_base_with_the_local_main(tmp_path):
	repo = tmp_path / "repo"
	subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, timeout=60)
	git(repo, "commit", "-q", "--allow-empty", "-m", "base")
	base = git(repo, "rev-parse", "HEAD")
	git(repo, "checkout", "-q", "-b", "feature/x/topic")
	git(repo, "commit", "-q", "--allow-empty", "-m", "work")
	assert homeconfig.integrationBase(str(repo)) == (base, "")


def test_no_branch_or_no_common_commit_says_why(tmp_path):
	repo = tmp_path / "repo"
	subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True, timeout=60)
	git(repo, "commit", "-q", "--allow-empty", "-m", "x")
	assert homeconfig.integrationBase(str(repo)) == ("", "no integration branch")
	git(repo, "checkout", "-q", "--orphan", "main")
	git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated")
	git(repo, "checkout", "-q", "trunk")
	assert homeconfig.integrationBase(str(repo)) == ("", "no merge-base with main")


@pytest.mark.flow_mode("origin")
def test_a_flow_mode_error_is_the_why(tmp_path):
	missing = str(tmp_path / "missing")
	assert homeconfig.integrationBase(missing) == ("", f"flow mode of {missing}: no such directory")

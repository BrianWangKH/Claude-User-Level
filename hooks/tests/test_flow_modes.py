"""Flow mode in the scripts that had no test of it: fetch_origin.py, new-worktree.sh,
finish-worktree.sh, run-worker.sh, worker-status.py, run-result.py and pr-conflict.sh. Each runs once per mode (the
flowMode argument, conftest.py) and asserts that mode's outcome. Here flowMode is the main checkout's
record (<main>/.hook-only/flow-mode, what `wt local on|off` writes), not the test override: the override
is removed (mainRecord), so every script reads the mode the way it does outside tests
(homeconfig.flowMode). Where a topic's recorded mode (.hook-only/flow-mode in its worktree, written at
launch) overrides the main checkout's, every record runs against both.

The checkouts: a main repo whose origin/main is its first commit and whose local main is one commit
ahead ("local only", touching .claude/local.json), so the two integration branches give different answers.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_flow_modes.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
RUN_SKILLS = os.path.join(config_fixtures.cleanHome(), "skills")
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
	"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
RECORDS = [None, "origin", "local"]
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import config_fixtures  # noqa: E402
import homeconfig  # noqa: E402


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
		env={**os.environ, **GIT_ENV}).stdout.strip()


def commitFile(repo, path, text, message):
	full = repo / path
	full.parent.mkdir(parents=True, exist_ok=True)
	full.write_text(text)
	git(repo, "add", path)
	git(repo, "commit", "-q", "-m", message)


def effective(record, flowMode):
	"""The mode a topic runs in: its record, else the main checkout's."""
	return record or flowMode


@pytest.fixture
def home(tmp_path):
	"""A home whose ~/.claude/hooks and skills are the files under test."""
	h = tmp_path / "home"
	(h / ".claude").mkdir(parents=True)
	os.symlink(RUN_HOOKS, h / ".claude" / "hooks")
	os.symlink(RUN_SKILLS, h / ".claude" / "skills")
	return h


@pytest.fixture
def main(tmp_path):
	"""The main checkout: origin/main at "base", the local main one commit ahead."""
	m = tmp_path / "proj" / "main"
	m.mkdir(parents=True)
	git(m, "init", "-q", "-b", "main")
	commitFile(m, ".gitignore", ".hook-only/\nplans/\n.claude-cost/\n", "base")
	git(m, "update-ref", "refs/remotes/origin/main", "HEAD")
	commitFile(m, ".claude/local.json", "{}\n", "local only")
	(tmp_path / "proj" / "worktrees").mkdir()
	return m


@pytest.fixture(autouse=True)
def mainRecord(main, flowMode, monkeypatch):
	"""The test's mode as the main checkout's record, with no test override (module doc)."""
	monkeypatch.delenv("FLOW_INTEGRATION", raising=False)
	(main / ".hook-only").mkdir(exist_ok=True)
	(main / ".hook-only" / "flow-mode").write_text(flowMode + "\n")


def addWorktree(main, topic="topic", record=None):
	"""A worktree on feature/x/<topic> at the local main, with the topic's recorded mode if any."""
	wt = main.parent / "worktrees" / topic
	git(main, "worktree", "add", "-q", "-b", f"feature/x/{topic}", str(wt), "main")
	if record:
		(wt / ".hook-only").mkdir()
		(wt / ".hook-only" / "flow-mode").write_text(record + "\n")
	return wt


def fetchedNow(main):
	(main / ".git" / "claude-fetch-status").write_text(f"{int(time.time())} ok\n")


def script(home, cwd, *argv):
	return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=120,
		env={**os.environ, **GIT_ENV, "HOME": str(home)})


# --- fetch_origin.py ------------------------------------------------------------------------------

def test_fetch_origin_fetches_only_in_origin_mode(tmp_path, main, flowMode):
	remote = tmp_path / "origin.git"
	git(tmp_path, "init", "-q", "--bare", str(remote))
	git(main, "remote", "add", "origin", str(remote))
	git(main, "push", "-q", "origin", "main")
	data = {"hook_event_name": "UserPromptSubmit", "prompt": "/new-worktree feature/x/new-topic", "cwd": str(main)}
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "hook-entry.py"), "check", "fetch-origin"], input=json.dumps(data),
		capture_output=True, text=True, timeout=120)
	assert result.returncode == 0 and result.stdout == "", result.stderr
	assert (main / ".git" / "worktrees").is_dir()  # both modes
	status = main / ".git" / "claude-fetch-status"
	if flowMode == "local":
		assert not status.exists()
	else:
		assert status.read_text().split()[1] == "ok", status.read_text()


# --- new-worktree.sh ------------------------------------------------------------------------------

def newWorktree(home, main):
	return script(home, main, "bash", os.path.join(RUN_SKILLS, "new-worktree", "new-worktree.sh"), "feature/x/new-topic")


def test_new_worktree_needs_a_fetch_only_in_origin_mode(home, main, flowMode):
	result = newWorktree(home, main)
	if flowMode == "local":
		assert result.returncode == 0, result.stderr
	else:
		assert result.returncode == 1 and "no fetch status" in result.stderr


def test_new_worktree_starts_from_the_mode_base(home, main, flowMode):
	fetchedNow(main)
	result = newWorktree(home, main)
	assert result.returncode == 0, result.stderr
	base = "main" if flowMode == "local" else "origin/main"
	assert f"from {base} (" in result.stdout
	assert git(main.parent / "worktrees" / "new-topic", "rev-parse", "HEAD") == git(main, "rev-parse", base)


# --- finish-worktree.sh ---------------------------------------------------------------------------

@pytest.mark.parametrize("record", RECORDS)
def test_finish_worktree_checks_the_merge_against_the_topic_mode(home, main, flowMode, record):
	# the branch sits at the local main: merged there, not into origin/main
	addWorktree(main, record=record)
	fetchedNow(main)
	result = script(home, main, "bash", os.path.join(RUN_SKILLS, "finish-worktree", "finish-worktree.sh"), "topic")
	if effective(record, flowMode) == "local":
		assert result.returncode == 0 and "feature/x/topic is merged into main" in result.stdout, result.stderr
	else:
		assert result.returncode == 1 and "feature/x/topic is not merged into origin/main yet" in result.stderr


def test_finish_worktree_needs_a_fetch_only_in_origin_mode(home, main, flowMode):
	addWorktree(main)
	result = script(home, main, "bash", os.path.join(RUN_SKILLS, "finish-worktree", "finish-worktree.sh"), "topic")
	if flowMode == "local":
		assert result.returncode == 0, result.stderr
	else:
		assert result.returncode == 1 and "no fetch status" in result.stderr


# --- run-worker.sh --------------------------------------------------------------------------------

FAKE_CLAUDE = """#!/usr/bin/env bash
if [ "$1" = auth ]; then exit 0; fi
echo '{"result": "STATUS: done\\nall green", "total_cost_usd": 0.1}'
"""
FAKE_HOOK = """import os, sys
sys.path.insert(0, os.environ["FAKE_HOOK_CONFIG"])
import homeconfig
mode = homeconfig.flowMode(os.getcwd())
with open(os.environ["FAKE_HOOK_LOG"], "a") as f:
	f.write(os.path.basename(sys.argv[0]) + " " + os.environ.get("FLOW_INTEGRATION", "-") + " " + mode + "\\n")
"""


@pytest.fixture
def workerHome(tmp_path):
	"""A home with a fake claude (always `STATUS: done`) and fake hooks that log their FLOW_INTEGRATION (none is
	handed down) and the flow mode they read (homeconfig.flowMode of their working directory); tree/ is the copy of
	the home code run-worker.sh runs from, its config phase-free (worker_phases_max 0) with the budget minimum
	run-steps.py reads before each claude call (worker_call_budget_min_usd, the home value)."""
	h = tmp_path / "worker-home"
	(h / ".local" / "bin").mkdir(parents=True)
	(h / ".local" / "bin" / "claude").write_text(FAKE_CLAUDE)
	(h / ".local" / "bin" / "claude").chmod(0o755)
	hooks = h / ".claude" / "hooks"
	for name in ("worker/wt_events.py", "gates/verify.py", "gates/verifier.py", "worker/auto-merge.py",
			"worker/worker-control.py", "worker/wt_queue.py"):
		(hooks / name).parent.mkdir(parents=True, exist_ok=True)
		(hooks / name).write_text(FAKE_HOOK)
	tree = config_fixtures.copyHome(os.path.dirname(HOOKS), h / "tree")
	sections = {name: {} for name in homeconfig.SECTIONS}
	minimum = homeconfig.load("budget")["worker_call_budget_min_usd"]
	(tree / "config" / "home.json").write_text(json.dumps({**sections, "budget": {"worker_phases_max": 0,
		"worker_call_budget_min_usd": minimum}}))
	return h


@pytest.mark.parametrize("record", RECORDS)
def test_run_worker_runs_in_the_topic_mode(tmp_path, workerHome, main, flowMode, record):
	wt = addWorktree(main, record=record)
	(wt / ".hook-only" / "runs").mkdir(parents=True)
	log = tmp_path / "hooks.log"
	result = subprocess.run(["bash", str(workerHome / "tree" / "hooks" / "worker" / "run-worker.sh"), "topic", str(wt), "1", "new",
		"11111111-2222-3333-4444-555555555555", "5", "1", "", "/feature-flow topic"], capture_output=True, text=True,
		timeout=120, env={**os.environ, "HOME": str(workerHome), "WT_MAIN_DIR": str(main), "FAKE_HOOK_LOG": str(log),
			"FAKE_HOOK_CONFIG": os.path.dirname(homeconfig.__file__)})
	assert result.returncode == 0, result.stderr
	runs = wt / ".hook-only" / "runs"
	assert (runs / "exit-1").read_text().startswith("0 ")
	calls = log.read_text().splitlines()
	mode = effective(record, flowMode)
	if mode == "local":
		assert (runs / "main-1").read_text().strip() == git(main, "rev-parse", "HEAD")
		assert calls == ["wt_events.py - local", "auto-merge.py - local", "wt_queue.py - local"]
	else:
		assert not (runs / "main-1").exists()
		assert calls == ["wt_events.py - origin", "wt_queue.py - origin"]


# --- worker-status.py and run-result.py: the base of a topic's commits and .claude changes ---------

def builtTopic(main, record):
	"""A worktree with one commit of its own (.claude/x.json) and run 1 ended `STATUS: done`."""
	wt = addWorktree(main, record=record)
	commitFile(wt, ".claude/x.json", "{}\n", "add x")
	runs = wt / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	(runs / "start-1").write_text("2026-11-03T10:00:00+08:00\n")
	(runs / "exit-1").write_text("0 2026-11-03T10:20:00+08:00\n")
	(runs / "result-1.json").write_text(json.dumps({"result": "STATUS: done\nall green", "total_cost_usd": 1.0,
		"num_turns": 3}))
	return wt


@pytest.mark.parametrize("record", RECORDS)
def test_worker_status_counts_commits_from_the_topic_base(home, main, flowMode, record):
	builtTopic(main, record)
	result = script(home, main, sys.executable, os.path.join(RUN_SKILLS, "repo-plan", "worker-status.py"), "topic")
	assert result.returncode == 0, result.stderr
	# origin/main lacks "local only": the branch's own commit and that one; the local main has it
	expected = "git: 1 commit(s) (" if effective(record, flowMode) == "local" else "git: 2 commit(s) ("
	assert expected in result.stdout, result.stdout


@pytest.mark.parametrize("record", RECORDS)
def test_run_result_shows_claude_changes_from_the_topic_base(home, main, flowMode, record):
	builtTopic(main, record)
	result = script(home, main, sys.executable, os.path.join(RUN_SKILLS, "repo-plan", "run-result.py"), "topic", "1")
	assert result.returncode == 0, result.stderr
	assert result.stdout.count("Changes to .claude / .mcp.json:") == 1, result.stdout
	changes = result.stdout.split("Changes to .claude / .mcp.json:")[1].split("--- result ---")[0]
	assert ".claude/x.json" in changes, result.stdout
	assert (".claude/local.json" in changes) == (effective(record, flowMode) == "origin"), changes


# --- pr-conflict.sh: the topic's integration branch (base-ref.sh, homeconfig.integrationBranch) -------

def prConflict(home, main, topic="topic"):
	fetchedNow(main)
	return script(home, main, "bash", os.path.join(RUN_SKILLS, "pr-conflict", "pr-conflict.sh"), topic)


def remoteBranch(main, name, path, text):
	"""refs/remotes/origin/<name>: origin/main plus one commit writing path, the local main untouched."""
	git(main, "switch", "-q", "-c", f"tmp-{name}", "origin/main")
	commitFile(main, path, text, f"{name} only")
	git(main, "update-ref", f"refs/remotes/origin/{name}", "HEAD")
	git(main, "switch", "-q", "main")
	git(main, "branch", "-q", "-D", f"tmp-{name}")


def conflictingTopic(main, record=None):
	"""A topic with its own commit on f.txt, and origin/master changing f.txt the other way."""
	wt = addWorktree(main, record=record)
	commitFile(wt, "f.txt", "branch\n", "branch f")
	remoteBranch(main, "master", "f.txt", "master\n")
	return wt


@pytest.mark.parametrize("record", RECORDS)
def test_pr_conflict_compares_with_the_topic_mode_base(home, main, flowMode, record):
	"""Integration: the branch sits at the local main, one commit ahead of origin/main: in local mode it is
	contained in main (MERGED), in origin mode origin/main has not moved since it (OK)."""
	addWorktree(main, record=record)
	result = prConflict(home, main)
	assert result.returncode == 0, result.stderr
	if effective(record, flowMode) == "local":
		assert result.stdout.startswith("MERGED: feature/x/topic is contained in main ("), result.stdout
	else:
		assert result.stdout.startswith("OK: origin/main has not moved since feature/x/topic"), result.stdout


def test_pr_conflict_takes_origin_master_before_origin_main(home, main, flowMode):
	"""Integration: with origin/master (origin/HEAD naming it) the origin-mode check compares with origin/master,
	where the topic conflicts, never with origin/main, where it would merge; local mode compares with the local
	main whatever origin has."""
	conflictingTopic(main)
	git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
	result = prConflict(home, main)
	assert result.returncode == 0, result.stderr
	if flowMode == "local":
		assert result.stdout.startswith("OK: main has not moved since feature/x/topic"), result.stdout
	else:
		assert result.stdout.startswith("CONFLICT: feature/x/topic vs origin/master ("), result.stdout
		assert result.stdout.splitlines()[-1] == "  f.txt", result.stdout


def test_pr_conflict_with_only_origin_master_compares_with_it(home, main, flowMode):
	"""Integration: no origin/main, no origin/HEAD: origin mode takes origin/master."""
	conflictingTopic(main)
	git(main, "update-ref", "-d", "refs/remotes/origin/main")
	result = prConflict(home, main)
	assert result.returncode == 0, result.stderr
	if flowMode == "local":
		assert result.stdout.startswith("OK: main has not moved since feature/x/topic"), result.stdout
	else:
		assert result.stdout.startswith("CONFLICT: feature/x/topic vs origin/master ("), result.stdout


@pytest.mark.parametrize("head", ["origin/main", "origin/gone"])
def test_pr_conflict_fails_when_origin_head_names_another_branch(home, main, flowMode, head):
	"""Integration: origin/HEAD naming a branch other than the one picked (origin/master, else origin/main), or a
	missing one, is an error in origin mode (exit 1, nothing compared), never a quiet choice; local mode never
	reads origin."""
	conflictingTopic(main)
	git(main, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/{head}")
	result = prConflict(home, main)
	if flowMode == "local":
		assert result.returncode == 0 and result.stdout.startswith("OK: main has not moved"), result.stderr
	else:
		assert result.returncode == 1, result.stdout
		assert f"origin/HEAD names {head} but the integration branch is origin/master" in result.stderr
		assert "FAIL: cannot find the integration branch (base-ref.sh)" in result.stderr
		assert result.stdout == ""

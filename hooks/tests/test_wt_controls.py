"""wt_controls.py: one place decides what the pane, auto mode and `wt ...` may do (2026-11-01); and
agent-guard's explained ref moves (parallel workers).

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_wt_controls.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOOKS)

import wt_controls  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"
DEFAULTS = json.load(open(os.path.join(HOOKS, "budget-defaults.json"), encoding="utf-8"))


def loadGuard():
	spec = importlib.util.spec_from_file_location("agent_guard", os.path.join(HOOKS, "agent-guard.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.fixture
def main(tmp_path):
	"""A main checkout at tmp/proj/main; worktrees beside it."""
	m = tmp_path / "proj" / "main"
	(m / "plans" / "worker").mkdir(parents=True)
	(m / ".hook-only" / "workers").mkdir(parents=True)
	return m


def worktree(main, topic):
	w = main.parent / "worktrees" / topic
	(w / ".hook-only" / "runs").mkdir(parents=True, exist_ok=True)
	(w / ".git").write_text("gitdir: x\n")
	return w


def estimate(main, topic, **extra):
	path = main / "plans" / "worker" / f"{topic}.json"
	path.write_text(json.dumps({"minutes": 20, "est_usd": 2, **extra}))
	return path


def state(main, topic, **fields):
	(main / ".hook-only" / "workers" / f"{topic}.json").write_text(json.dumps({"pid": 0, "session_id": SESSION,
		"run": 1, **fields}))


def finishedRun(w, run, result, auto=False):
	runs = w / ".hook-only" / "runs"
	(runs / f"est-{run}.json").write_text(json.dumps({"minutes": 20, "usd": 2, **({"auto": True} if auto else {})}))
	(runs / f"start-{run}").write_text("2026-11-01T10:00:00+08:00\n")
	(runs / f"exit-{run}").write_text("0 2026-11-01T10:30:00+08:00\n")
	(runs / f"result-{run}.json").write_text(json.dumps({"result": result}))


def older(path, seconds=60):
	t = time.time() - seconds
	os.utime(path, (t, t))


# --- rows ---------------------------------------------------------------------------------------

def test_a_planned_topic_without_a_session_can_be_launched(main):
	worktree(main, "fresh")
	estimate(main, "fresh")
	row = wt_controls.control(main, "fresh")
	assert row["action"] == "launch" and row["auto"] == {"ok": True} and not row["held"]


def test_no_estimate_says_why_and_auto_refuses(main):
	worktree(main, "bare")
	row = wt_controls.control(main, "bare")
	assert "action" not in row and "no estimate yet" in row["why"]
	assert row["auto"]["ok"] is False and "no estimate yet" in row["auto"]["why"]


def test_a_topic_without_a_worktree_has_no_row(main):
	estimate(main, "gone")
	state(main, "gone")
	assert wt_controls.controls(main) == []


def test_a_run_that_stopped_short_of_done_can_be_resumed(main):
	w = worktree(main, "t")
	state(main, "t")
	finishedRun(w, 1, "STATUS: blocked\nbudget")
	older(estimate(main, "t"))
	assert wt_controls.control(main, "t")["action"] == "resume"


def test_done_without_a_new_estimate_offers_nothing(main):
	w = worktree(main, "t")
	state(main, "t")
	older(estimate(main, "t"))  # written before the run started
	finishedRun(w, 1, "STATUS: done\nall green")
	row = wt_controls.control(main, "t")
	assert "action" not in row and "STATUS: done" in row["why"] and row["auto"]["ok"] is False


def test_a_new_estimate_after_done_brings_resume_back_even_with_equal_numbers(main):
	w = worktree(main, "t")
	state(main, "t")
	finishedRun(w, 1, "STATUS: done\n")
	older(w / ".hook-only" / "runs" / "est-1.json")
	estimate(main, "t", mode="merge")  # written now, after the run's est-1.json
	assert wt_controls.control(main, "t")["action"] == "resume"


def test_automatic_runs_are_skipped_when_finding_the_users_last_run(main):
	w = worktree(main, "t")
	state(main, "t", run=2)
	finishedRun(w, 1, "STATUS: done\n")
	older(estimate(main, "t"), 120)
	older(w / ".hook-only" / "runs" / "est-1.json", 60)
	finishedRun(w, 2, "STATUS: done\n", auto=True)  # its est-2.json is newer than the estimate: ignored
	assert "action" not in wt_controls.control(main, "t")


def test_a_hold_keeps_resume_for_the_person_but_not_for_auto(main):
	w = worktree(main, "t")
	state(main, "t")
	finishedRun(w, 1, "STATUS: blocked\n")
	older(estimate(main, "t"))
	(w / ".hook-only" / "agent-guard").mkdir(parents=True)
	(w / ".hook-only" / "agent-guard" / "HOLD").write_text("x")
	row = wt_controls.control(main, "t")
	assert row["action"] == "resume" and row["held"] and row["auto"] == {"ok": False, "why": wt_controls.HOLD_WHY}


@pytest.mark.parametrize("exitText, running", [(None, True), ("", True), ("0 2026-11-01T10:30:00+08:00\n", False)])
def test_running_is_read_from_the_run_files_not_the_pid(main, exitText, running):
	w = worktree(main, "t")
	estimate(main, "t")
	state(main, "t", pid=999999999)  # no such process: the files decide (a sandboxed caller sees no pid)
	(w / ".hook-only" / "runs" / "start-1").write_text("2026-11-01T10:00:00+08:00\n")
	if exitText is not None:
		(w / ".hook-only" / "runs" / "exit-1").write_text(exitText)
	row = wt_controls.control(main, "t")
	assert (row.get("action") == "stop") is running
	assert row["auto"]["ok"] is (not running)


# --- local switch -------------------------------------------------------------------------------

@pytest.mark.parametrize("rows, wantOn, behind, expected", [
	([{"topic": "a", "action": "stop"}], True, 0, "a worker is running (a)"),
	([{"topic": "b", "action": "launch"}], False, 0, "planned but not launched (b)"),
	([], True, None, "can't compare"),
	([], True, 3, "3 commit(s) behind"),
	([], False, None, None),
	([{"topic": "c", "action": "resume"}], True, 0, None),
])
def test_local_blocker(rows, wantOn, behind, expected):
	why = wt_controls.localBlocker(rows, wantOn, behind)
	assert (why is None) if expected is None else (expected in why)


def test_the_command_prints_schema_1_and_the_local_blocker(main):
	worktree(main, "fresh")
	estimate(main, "fresh")
	out = subprocess.run([sys.executable, os.path.join(HOOKS, "wt_controls.py"), str(main), "--local", "off"],
		capture_output=True, text=True, check=True).stdout
	data = json.loads(out)
	assert data["schema"] == 1 and [r["topic"] for r in data["controls"]] == ["fresh"]
	assert "planned but not launched (fresh)" in data["local_blocker"] and data["plans_visible"] is True


@pytest.mark.parametrize("hide", ["empty", "missing"])
def test_a_checkout_whose_plans_cant_be_seen_says_so(main, hide):
	worktree(main, "fresh")
	(main / "plans" / "worker").rmdir()  # the sandbox's mask: an empty folder
	if hide == "missing":
		(main / "plans").rmdir()
	out = subprocess.run([sys.executable, os.path.join(HOOKS, "wt_controls.py"), str(main)],
		capture_output=True, text=True, check=True).stdout
	assert json.loads(out)["plans_visible"] is False


# --- worker-control.py uses the same rule -------------------------------------------------------

def test_wt_resume_after_done_is_refused_until_a_new_estimate(main, tmp_path):
	home = tmp_path / "home"
	(home / ".claude" / "hooks").mkdir(parents=True)
	(home / ".claude" / "hooks" / "budget-defaults.json").write_text(json.dumps(DEFAULTS))
	wrapper = home / ".claude" / "hooks" / "run-worker.sh"
	wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
	wrapper.chmod(0o755)
	w = worktree(main, "t")
	state(main, "t")
	older(estimate(main, "t"))
	finishedRun(w, 1, "STATUS: done\n")

	def wt():
		env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")} | {"HOME": str(home)}
		result = subprocess.run([sys.executable, os.path.join(HOOKS, "worker-control.py")], env=env,
			input=json.dumps({"prompt": "wt resume t", "cwd": str(main)}), capture_output=True, text=True)
		assert result.returncode == 0, result.stderr
		return json.loads(result.stdout)

	refused = wt()
	assert refused.get("decision") == "block" and "nothing to resume" in refused["reason"]
	estimate(main, "t", mode="merge")
	assert "decision" not in wt()


# --- agent-guard: explained moves of other branches ---------------------------------------------

def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
		env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout.strip()


@pytest.fixture
def repo(tmp_path):
	"""main checkout with two worktrees: here (the round's) and there (another topic's)."""
	m = tmp_path / "r" / "main"
	m.mkdir(parents=True)
	git(m, "init", "-q", "-b", "main")
	(m / "f").write_text("1\n")
	git(m, "add", "f")
	git(m, "commit", "-q", "-m", "base")
	for name in ("here", "there"):
		git(m, "worktree", "add", "-q", "-b", f"feature/x/{name}", str(tmp_path / "r" / "worktrees" / name))
	return m, tmp_path / "r" / "worktrees" / "here", tmp_path / "r" / "worktrees" / "there"


def moveThere(there):
	(there / "g").write_text("2\n")
	git(there, "add", "g")
	git(there, "commit", "-q", "-m", "work")


def notesAfter(guard, here, before, since):
	alerts, notes = guard.refFindings(before, guard.refSnapshot(str(here)), guard.explainedMoves(str(here), since))
	assert alerts == []
	return notes


def test_another_worktrees_branch_moved_by_its_running_worker_sends_nothing(repo):
	guard = loadGuard()
	main_, here, there = repo
	since = time.time()
	before = guard.refSnapshot(str(here))
	(there / ".hook-only" / "runs").mkdir(parents=True)
	(there / ".hook-only" / "runs" / "start-1").write_text("2026-11-01T10:00:00+08:00\n")  # no exit: running
	moveThere(there)
	assert notesAfter(guard, here, before, since) == []


def test_a_run_that_ended_during_the_round_still_explains_its_move(repo):
	guard = loadGuard()
	main_, here, there = repo
	since = time.time() - 5
	before = guard.refSnapshot(str(here))
	runs = there / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	(runs / "start-1").write_text("2026-11-01T10:00:00+08:00\n")
	from datetime import datetime
	(runs / "exit-1").write_text("0 " + datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
	moveThere(there)
	assert notesAfter(guard, here, before, since) == []


def test_a_move_nothing_explains_still_sends_the_reminder(repo):
	guard = loadGuard()
	main_, here, there = repo
	since = time.time()
	before = guard.refSnapshot(str(here))
	moveThere(there)  # no worker run there: maybe this round's agent
	assert notesAfter(guard, here, before, since) == ["other branch refs/heads/feature/x/there moved (maybe another worktree)"]


def test_main_moved_by_a_recorded_automatic_merge_sends_nothing(repo):
	guard = loadGuard()
	main_, here, there = repo
	since = time.time()
	before = guard.refSnapshot(str(here))
	moveThere(there)
	git(main_, "merge", "-q", "--no-ff", "-m", "auto", "feature/x/there")
	(main_ / ".hook-only" / "auto-merge").mkdir(parents=True)
	(main_ / ".hook-only" / "auto-merge" / "merges.jsonl").write_text(json.dumps({"commit": git(main_, "rev-parse", "HEAD")}) + "\n")
	notes = notesAfter(guard, here, before, since)
	assert notes == ["other branch refs/heads/feature/x/there moved (maybe another worktree)"]  # main: explained


# --- a refusal the person can approve: the pane's merge button ---------------------------------

def test_a_refused_merge_comes_with_its_reason_and_the_merge_offer(main):
	w = worktree(main, "t")
	state(main, "t")
	finishedRun(w, 1, "STATUS: done\n")
	(w / ".hook-only" / "runs" / "merge-1.json").write_text(json.dumps({"decision": "refused",
		"reason": "a person must review these paths: .claude/x"}))
	row = wt_controls.control(main, "t")
	assert row["merge"]["reason"].startswith("a person must review") and row["merge"]["files"] == []


@pytest.mark.parametrize("decision", [{"decision": "merged", "commit": "abc"},
	{"decision": "refused", "reason": "behind", "merge_run": True}])
def test_no_merge_offer_for_a_merge_or_a_merge_run(main, decision):
	w = worktree(main, "t")
	state(main, "t")
	finishedRun(w, 1, "STATUS: done\n")
	(w / ".hook-only" / "runs" / "merge-1.json").write_text(json.dumps(decision))
	assert "merge" not in wt_controls.control(main, "t")


def approvingRun(main, decision_age=60):
	"""Topic t: run 1 refused for the person, merge-1.json written before an approval record."""
	w = worktree(main, "t")
	state(main, "t")
	finishedRun(w, 1, "STATUS: done\n")
	decision = w / ".hook-only" / "runs" / "merge-1.json"
	decision.write_text(json.dumps({"decision": "refused", "reason": "a person must review these paths: .claude/x"}))
	older(decision, decision_age)
	return w


def test_an_approval_in_progress_replaces_the_merge_offer(main):
	w = approvingRun(main)
	(w / ".hook-only" / "runs" / "approve-1.json").write_text(json.dumps({"run": 1, "pid": 999999,
		"started": "2026-11-02T07:45:12+0800", "started_epoch": time.time()}))
	row = wt_controls.control(main, "t")
	assert row["approving"] == {"run": 1, "since": "07:45"} and "merge" not in row


def test_an_approval_record_older_than_the_decision_or_of_another_run_offers_merge(main):
	w = approvingRun(main, decision_age=0)
	runs = w / ".hook-only" / "runs"
	(runs / "approve-1.json").write_text(json.dumps({"run": 1, "pid": 1, "started": "2026-11-02T07:45:12+0800",
		"started_epoch": time.time() - 60}))  # auto-merge wrote its decision after it: finished
	assert "approving" not in wt_controls.control(main, "t") and "merge" in wt_controls.control(main, "t")
	(runs / "approve-1.json").rename(runs / "approve-0.json")
	assert "merge" in wt_controls.control(main, "t")


def test_release_approval_removes_only_its_own_or_a_dead_record(tmp_path):
	w = tmp_path / "w"
	(w / ".hook-only" / "runs").mkdir(parents=True)
	path = w / ".hook-only" / "runs" / "approve-1.json"
	path.write_text(json.dumps({"run": 1, "pid": 4242}))
	wt_controls.releaseApproval(w, 1, 1111, isAlive=lambda pid: True)
	assert path.exists()  # another live approval's record stays
	wt_controls.releaseApproval(w, 1, 4242, isAlive=lambda pid: True)
	assert not path.exists()
	path.write_text(json.dumps({"run": 1, "pid": 4242}))
	wt_controls.releaseApproval(w, 1, 1111, isAlive=lambda pid: False)
	assert not path.exists()

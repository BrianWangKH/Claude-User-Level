"""Merge mode (2026-11-01): worker-control.py resumes the branch's session into feature-flow merge.md,
cost-history.py estimates merge runs on their own and imports them apart from the branch row.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_merge_mode.py
"""
import csv
import json
import os
import subprocess
import sys
import time

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
COST_HISTORY = os.path.join(SKILLS, "repo-plan", "cost-history.py")
DEFAULTS = json.load(open(os.path.join(HOOKS, "budget-defaults.json"), encoding="utf-8"))
SESSION = "11111111-2222-3333-4444-555555555555"
COLUMNS = ["date", "topic", "branch", "class", "agent", "round", "estTok", "actTok",
	"estMin", "actMin", "estUsd", "actUsd", "note"]


def env(home):
	out = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	out["HOME"] = str(home)
	return out


# worker-control.py

@pytest.fixture
def project(tmp_path):
	"""(home, main checkout, worktree, args file) with a fake run-worker.sh that records its arguments."""
	home = tmp_path / "home"
	hooks = home / ".claude" / "hooks"
	hooks.mkdir(parents=True)
	(hooks / "budget-defaults.json").write_text(json.dumps(DEFAULTS))
	argsFile = tmp_path / "args.txt"
	(hooks / "run-worker.sh").write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {argsFile}\n")
	(hooks / "run-worker.sh").chmod(0o755)
	main = tmp_path / "proj" / "main"
	(main / "plans" / "worker").mkdir(parents=True)
	worktree = tmp_path / "proj" / "worktrees" / "t"
	worktree.mkdir(parents=True)
	(worktree / ".git").write_text("gitdir: x\n")
	return home, main, worktree, argsFile


def estimate(main, **extra):
	(main / "plans" / "worker" / "t.json").write_text(json.dumps({"minutes": 20, "est_usd": 2,
		"raw_minutes": 20, "raw_usd": 2, "hook_wait_minutes": 0, "class": "logic", **extra}))


def withSession(main, **extra):
	state = main / ".hook-only" / "workers"
	state.mkdir(parents=True, exist_ok=True)
	(state / "t.json").write_text(json.dumps({"pid": 0, "session_id": SESSION, "run": 1, **extra}))


def wt(project, prompt):
	home, main, _, argsFile = project
	if argsFile.exists():
		argsFile.unlink()
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "worker-control.py")],
		input=json.dumps({"prompt": prompt, "cwd": str(main)}), capture_output=True, text=True, env=env(home))
	assert result.returncode == 0, result.stderr
	out = json.loads(result.stdout)
	if out.get("decision") == "block":
		return out["reason"], None
	for _ in range(50):
		if argsFile.exists() and argsFile.read_text():
			break
		time.sleep(0.1)
	return out["hookSpecificOutput"]["additionalContext"], argsFile.read_text().split("\0")


def test_resume_in_merge_mode_sends_the_session_to_merge_md(project):
	_, main, worktree, _ = project
	estimate(main, mode="merge")
	withSession(main)
	context, args = wt(project, "wt resume t")
	assert "(merge mode)" in context
	assert args[3] == "resume" and args[4] == SESSION
	assert "feature-flow/merge.md" in args[8] and "class flow is finished" in args[8]
	run = args[2]
	assert json.loads((worktree / ".hook-only" / "runs" / f"est-{run}.json").read_text())["mode"] == "merge"
	assert json.loads((main / ".hook-only" / "workers" / "t.json").read_text())["mode"] == "merge"


def test_merge_mode_stays_when_a_re_estimate_leaves_it_out(project):
	_, main, worktree, _ = project
	estimate(main)
	withSession(main, mode="merge")
	context, args = wt(project, "wt resume t")
	assert "(merge mode)" in context and "feature-flow/merge.md" in args[8]


def test_plain_resume_keeps_the_class_prompt(project):
	_, main, worktree, _ = project
	estimate(main)
	withSession(main)
	context, args = wt(project, "wt resume t")
	assert "merge mode" not in context
	assert "load the class step files again" in args[8] and "add merge.md if you stopped inside it" in args[8]
	assert "mode" not in json.loads((worktree / ".hook-only" / "runs" / f"est-{args[2]}.json").read_text())


def test_launch_refuses_merge_mode(project):
	_, main, _, _ = project
	estimate(main, mode="merge")
	reason, args = wt(project, "wt launch t")
	assert args is None and "merge mode resumes the session" in reason


def test_unknown_mode_is_refused(project):
	_, main, _, _ = project
	estimate(main, mode="rebase")
	withSession(main)
	reason, args = wt(project, "wt resume t")
	assert args is None and "invalid mode" in reason


# cost-history.py

@pytest.fixture
def home(tmp_path):
	h = tmp_path / "home"
	(h / ".claude").mkdir(parents=True)
	os.symlink(HOOKS, h / ".claude" / "hooks")
	os.symlink(SKILLS, h / ".claude" / "skills")
	return h


def gitRepo(path):
	path.mkdir(parents=True, exist_ok=True)
	subprocess.run(["git", "init", "-q", str(path)], check=True)
	return path


def writeHistory(repo, rows):
	path = repo / ".claude-cost" / "history.tsv"
	path.parent.mkdir(parents=True, exist_ok=True)
	lines = ["\t".join(COLUMNS)] + ["\t".join(str(r.get(c, "")) for c in COLUMNS) for r in rows]
	path.write_text("\n".join(lines) + "\n")


def row(agent, est, act, branch="feature/transform/x", note="est=raw"):
	return {"date": "2026-10-31", "topic": "x", "branch": branch, "class": "logic", "agent": agent, "round": "1",
		"estMin": est, "actMin": act, "estUsd": est, "actUsd": act, "note": note}


def costHistory(repo, home, *args):
	return subprocess.run([sys.executable, COST_HISTORY, *args], cwd=repo, capture_output=True, text=True,
		env=env(home))


def workerJson(output):
	line = [l for l in output.splitlines() if l.startswith("plans/worker/<topic>.json: ")][0]
	return json.loads(line.split(": ", 1)[1])


def test_estimate_merge_uses_only_merge_rows_and_marks_the_mode(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	# the branch row would say 20 for this scope; the one merge row says 5 (its fixed part, slope 0)
	writeHistory(repo, [row("branch", "10", "20", branch="feature/transform/y"), row("merge", "10", "5")])
	result = costHistory(repo, home, "estimate", "branch", "feature/transform", "logic", "2", "20", "--merge",
		"--dev-rounds", "0")
	assert result.returncode == 0, result.stderr
	config = workerJson(result.stdout)
	assert config["mode"] == "merge" and config["class"] == "logic"
	assert config["est_usd"] == 5 and config["minutes"] == 5 and config["raw_minutes"] == 20
	assert "model merge runs" in result.stdout


def test_estimate_merge_without_history_is_the_raw_estimate(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	writeHistory(repo, [row("branch", "10", "20")])
	config = workerJson(costHistory(repo, home, "estimate", "branch", "feature/transform", "logic", "2", "20",
		"--merge", "--dev-rounds", "0").stdout)
	assert config["est_usd"] == 2 and config["minutes"] == 20


def test_estimate_merge_takes_dev_rounds_only_with_quality_gates(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	(repo / ".claude").mkdir()
	(repo / ".claude" / "quality-gates.json").write_text("{}")
	writeHistory(repo, [row("developer", "5", "5", note="hook wait 2 min excluded; est=raw")])
	reviews = costHistory(repo, home, "estimate", "branch", "feature/transform", "logic", "2", "20", "--merge",
		"--reviews", "1", "--dev-rounds", "1")
	assert reviews.returncode != 0 and "a merge run dispatches no code-reviewer" in reviews.stderr
	missing = costHistory(repo, home, "estimate", "branch", "feature/transform", "presentation", "2", "20", "--merge")
	assert missing.returncode != 0 and "--dev-rounds" in missing.stderr
	ok = costHistory(repo, home, "estimate", "branch", "feature/transform", "presentation", "2", "20", "--merge",
		"--dev-rounds", "1")
	assert ok.returncode == 0, ok.stderr
	assert workerJson(ok.stdout)["hook_wait_minutes"] == 2


def test_merge_flag_only_goes_with_estimate_branch(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	writeHistory(repo, [])
	result = costHistory(repo, home, "estimate", "agent", "qa", "1", "10", "--merge")
	assert result.returncode != 0 and "--merge and --fix only go with estimate branch" in result.stderr


def finishedRun(runs, n, usd, minutes, status="STATUS: done", **est):
	start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(1_800_000_000 + n * 10_000))
	end = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(1_800_000_000 + n * 10_000 + minutes * 60))
	(runs / f"start-{n}").write_text(start + "\n")
	(runs / f"exit-{n}").write_text(f"0 {end}\n")
	(runs / f"result-{n}.json").write_text(json.dumps({"result": status, "num_turns": 5, "total_cost_usd": usd,
		"session_id": f"s{n}"}))
	(runs / f"est-{n}.json").write_text(json.dumps({"minutes": 10, "usd": 1, "raw_minutes": 10, "raw_usd": 1, **est}))


def test_import_records_a_merge_run_apart_from_the_branch_row(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	writeHistory(repo, [])
	worktree = tmp_path / "worktrees" / "t"
	runs = worktree / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	finishedRun(runs, 1, 3, 30)
	finishedRun(runs, 2, 0.5, 6, mode="merge")
	result = costHistory(repo, home, "import", str(worktree), "2026-11-01", "t", "feature/transform/t", "logic")
	assert result.returncode == 0, result.stderr
	assert "1 worker runs, 1 merge or fix runs, 1 branch row" in result.stdout
	with open(repo / ".claude-cost" / "history.tsv", encoding="utf-8") as f:
		rows = list(csv.DictReader(f, delimiter="\t"))
	agents = {(r["agent"], r["round"]): r for r in rows}
	assert agents[("worker", "1")]["actUsd"] == "3.00"
	assert agents[("merge", "2")]["actUsd"] == "0.50" and agents[("merge", "2")]["class"] == "logic"
	assert agents[("branch", "-")]["actUsd"] == "3.00" and agents[("branch", "-")]["estUsd"] == "1.00"

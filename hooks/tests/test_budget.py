"""Budget headroom and gate-wait reserve: subagent-budget.py, dispatch-guard.py,
worker-control.py and cost-history.py estimate.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_budget.py
The hooks run as subprocesses on a temporary git repo; HOME points at a temporary home whose
~/.claude/hooks and ~/.claude/skills/repo-plan link to the files under test, because the hooks find
cost-history.py and run-worker.sh under ~ (the home settings through homeconfig, next to the hooks).
"""
import json
import math
import os
import subprocess
import sys
import time

import pytest

import config_fixtures

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import homeconfig  # noqa: E402
import runstate  # noqa: E402

DEFAULTS = homeconfig.load("budget")
HEADROOM = DEFAULTS["budget_headroom_factor"]
# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
SKILLS = os.path.join(config_fixtures.cleanHome(), "skills")
COST_HISTORY = os.path.join(SKILLS, "repo-plan", "cost-history.py")
COLUMNS = ["date", "topic", "branch", "class", "agent", "round", "estTok", "actTok",
	"estMin", "actMin", "estUsd", "actUsd", "note"]


@pytest.fixture
def home(tmp_path):
	"""A home whose ~/.claude/hooks and ~/.claude/skills point at the files under test."""
	h = tmp_path / "home"
	(h / ".claude").mkdir(parents=True)
	os.symlink(RUN_HOOKS, h / ".claude" / "hooks")
	os.symlink(SKILLS, h / ".claude" / "skills")
	return h


def gitRepo(path):
	path.mkdir(parents=True, exist_ok=True)
	subprocess.run(["git", "init", "-q", str(path)], check=True)
	return path


def gitWorktree(main, worktree):
	"""main as a git repository with one commit and worktree as its linked worktree: homeconfig.flowMode finds
	the topic's and the main checkout's records through git."""
	gitRepo(main)
	for args in (["commit", "-q", "--allow-empty", "-m", "x"], ["worktree", "add", "-q", "-b", f"wt-{worktree.name}",
			str(worktree)]):
		subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
			capture_output=True)


def env(home, **extra):
	out = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	out.update({"HOME": str(home), **extra})
	return out


def hook(name, data, home, **extra):
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "hook-entry.py"), "check", name.removesuffix(".py")], input=json.dumps(data),
		capture_output=True, text=True, env=env(home, **extra))
	return result.stdout.strip(), result.stderr


def denied(stdout):
	return bool(stdout) and json.loads(stdout)["hookSpecificOutput"].get("permissionDecision") == "deny"


def writeHistory(repo, rows):
	path = repo / ".claude-cost" / "history.tsv"
	path.parent.mkdir(parents=True, exist_ok=True)
	lines = ["\t".join(COLUMNS)] + ["\t".join(str(r.get(c, "")) for c in COLUMNS) for r in rows]
	path.write_text("\n".join(lines) + "\n")


def reviewerRow(wait, est="1", act="1"):
	return {"date": "2026-10-31", "topic": "t", "branch": "feature/transform/t", "class": "logic",
		"agent": "code-reviewer", "round": "1", "estMin": "10", "actMin": "5", "estUsd": est, "actUsd": act,
		"note": f"hook wait {wait} min excluded; est=raw (budget $1, 10 min)"}


# subagent-budget.py: one stop at budget x headroom

def budgetState(repo, minutesAgo, budgetMinutes=10):
	state = repo / ".hook-only" / "budget"
	state.mkdir(parents=True, exist_ok=True)
	(state / "a1.json").write_text(json.dumps({"minutes": budgetMinutes, "tokens": None, "usd": None,
		"phase": "checkpoint", "source": "test", "agent_type": "qa",
		"started": time.time() - minutesAgo * 60, "tokens_at_start": 0, "usd_at_start": 0}))


def preToolUse(repo):
	return {"hook_event_name": "PreToolUse", "agent_id": "a1", "agent_type": "qa", "tool_name": "Bash",
		"cwd": str(repo), "session_id": "s"}


@pytest.mark.parametrize("used", [0.9, 1.1, HEADROOM - 0.05])
def test_subagent_budget_lets_an_overrun_within_the_headroom_finish(tmp_path, home, used):
	repo = gitRepo(tmp_path / "repo")
	budgetState(repo, used * 10)
	out, err = hook("subagent-budget.py", preToolUse(repo), home)
	assert not denied(out), (out, err)


def test_subagent_budget_stops_at_the_headroom(tmp_path, home):
	repo = gitRepo(tmp_path / "repo")
	budgetState(repo, (HEADROOM + 0.05) * 10)
	out, _ = hook("subagent-budget.py", preToolUse(repo), home)
	assert denied(out)
	# cost-history.py marks the round budget-stopped by this start
	assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"].startswith("Budget used up")


def markGateWait(repo, minutesAgo, maxMinutes):
	path = repo / ".hook-only" / "budget" / "a1.json"
	state = json.loads(path.read_text())
	since = time.time() - minutesAgo * 60
	state["hook_wait"] = {"since": since, "what": "mutation scan", "max_seconds": maxMinutes * 60}
	path.write_text(json.dumps(state))
	return since


def hookWaitEvents(repo):
	path = repo / ".hook-only" / "budget" / "events.jsonl"
	return [e for e in map(json.loads, path.read_text().splitlines()) if e["event"] == "HookWait"] if path.exists() else []


def test_subagent_budget_takes_a_cancelled_gates_hold_off_the_clock(tmp_path, home):
	# 40 min on a 10 min budget, 35 of them held by a gate Claude Code cancelled at its 30 min timeout
	repo = gitRepo(tmp_path / "repo")
	budgetState(repo, 40)
	markGateWait(repo, 35, 30)
	out, err = hook("subagent-budget.py", preToolUse(repo), home)
	assert not denied(out), (out, err)
	assert "hook_wait" not in json.loads((repo / ".hook-only" / "budget" / "a1.json").read_text())
	assert [round(e["seconds"]) for e in hookWaitEvents(repo)] == [1800]


def test_subagent_budget_leaves_a_hold_younger_than_its_timeout(tmp_path, home):
	repo = gitRepo(tmp_path / "repo")
	budgetState(repo, 40)
	markGateWait(repo, 5, 30)
	out, _ = hook("subagent-budget.py", preToolUse(repo), home)
	assert denied(out)  # a parallel call's gate may still hold; nothing excluded yet
	assert "hook_wait" in json.loads((repo / ".hook-only" / "budget" / "a1.json").read_text())


def test_subagent_stop_logs_the_wait_a_cancelled_gate_left(tmp_path, home):
	repo = gitRepo(tmp_path / "repo")
	budgetState(repo, 20)
	since = markGateWait(repo, 5, 30)
	hook("subagent-budget.py", {"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "qa",
		"cwd": str(repo), "session_id": "s"}, home)
	assert not (repo / ".hook-only" / "budget" / "a1.json").exists()
	events = hookWaitEvents(repo)
	assert len(events) == 1 and 290 <= events[0]["seconds"] <= 330
	assert events[0]["t"] == time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since))


# dispatch-guard.py: the gate wait is reserved in the worker run's time

def workerRepo(tmp_path, gates=True, minutesAgo=30):
	repo = gitRepo(tmp_path / "wt")
	runs = repo / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	start = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - minutesAgo * 60))
	(runs / "start-1").write_text(start[:-2] + ":" + start[-2:] + "\n")
	if gates:
		(repo / ".claude").mkdir()
		(repo / ".claude" / "quality-gates.json").write_text("{}")
	writeHistory(repo, [reviewerRow(5.9), reviewerRow(12.4), reviewerRow(7.3)])
	return repo


def dispatch(repo, agent, prompt, tool="Agent"):
	toolInput = {"subagent_type": agent, "prompt": prompt} if tool == "Agent" else {"to": agent, "message": prompt}
	return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": toolInput, "cwd": str(repo),
		"session_id": "s"}


def test_dispatch_guard_reserves_the_mutation_wait_for_a_review(tmp_path, home):
	repo = workerRepo(tmp_path)  # 40 - 30 = 10 min left; 5 + 12.4 + 3 doesn't fit
	out, err = hook("dispatch-guard.py", dispatch(repo, "code-reviewer", "Budget: 5 min, $1"), home,
		CLAUDE_WORKER_MINUTES="40")
	assert denied(out), err
	assert "12.4 min for its quality gate's wait" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("remaining,deny", [(1.2, True), (5, False)])
def test_dispatch_guard_checks_the_usd_against_the_newest_budget_usd_attachment(tmp_path, home, remaining, deny):
	repo = workerRepo(tmp_path, gates=False)  # 10 min left: the minutes fit
	transcript = tmp_path / "session.jsonl"
	attachment = lambda left: json.dumps({"type": "attachment", "attachment": {"type": "budget_usd", "remaining": left}})
	transcript.write_text("\n".join([attachment(0.1), attachment(None), attachment(remaining)]) + "\n")
	data = {**dispatch(repo, "qa", "Budget: 5 min, $1"), "transcript_path": str(transcript)}
	out, err = hook("dispatch-guard.py", data, home, CLAUDE_WORKER_MINUTES="40")
	assert denied(out) == deny, (out, err)  # $1 + the $0.5 margin against what is left
	state = json.loads((repo / ".hook-only" / "hook-read-only" / "usage" / "dispatch-main.json").read_text())
	assert state["latest"]["remaining"] == remaining


def mergeHead(repo):
	"""Give repo a merge commit as HEAD: dispatch-guard keeps a `Merge review:` prompt only then."""
	def run(*args):
		subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
			capture_output=True)
	run("commit", "-q", "--allow-empty", "-m", "base")
	run("checkout", "-q", "-b", "side")
	run("commit", "-q", "--allow-empty", "-m", "side")
	run("checkout", "-q", "-")
	run("commit", "-q", "--allow-empty", "-m", "main")
	run("merge", "-q", "--no-ff", "-m", "merge", "side")


def test_dispatch_guard_reserves_nothing_for_a_merge_review(tmp_path, home):
	repo = workerRepo(tmp_path)  # 5 + 0 + 3 fits in 10
	mergeHead(repo)
	out, err = hook("dispatch-guard.py", dispatch(repo, "code-reviewer", "Merge review: x\nBudget: 5 min, $1"), home,
		CLAUDE_WORKER_MINUTES="40")
	assert not denied(out), (out, err)


def test_dispatch_guard_reserves_nothing_without_quality_gates(tmp_path, home):
	repo = workerRepo(tmp_path, gates=False)
	out, err = hook("dispatch-guard.py", dispatch(repo, "code-reviewer", "Budget: 5 min, $1"), home,
		CLAUDE_WORKER_MINUTES="40")
	assert not denied(out), (out, err)


def test_dispatch_guard_review_prompt_uses_the_rule_line_of_cost_history(tmp_path, home):
	repo = workerRepo(tmp_path, gates=False)
	out, _ = hook("dispatch-guard.py", dispatch(repo, "code-reviewer", "Budget: 5 min, $1"), home)
	prompt = json.loads(out)["hookSpecificOutput"]["updatedInput"]["prompt"]
	estimate = costHistory(repo, home, "estimate", "agent", "qa", "1", "10").stdout
	rule = [l for l in estimate.splitlines() if l.startswith("Follow the budget rules")]
	assert rule and rule[0] in prompt


# cost-history.py estimate

def costHistory(repo, home, *args):
	return subprocess.run([sys.executable, COST_HISTORY, *args], cwd=repo, capture_output=True, text=True,
		env=env(home))


def mainRepo(tmp_path, gates=True):
	repo = gitRepo(tmp_path / "main")
	if gates:
		(repo / ".claude").mkdir()
		(repo / ".claude" / "quality-gates.json").write_text("{}")
	dev = dict(reviewerRow(2.6), agent="developer")
	writeHistory(repo, [reviewerRow(5.9), reviewerRow(12.4), reviewerRow(7.3), dev])
	return repo


def workerJson(output):
	line = [l for l in output.splitlines() if l.startswith("plans/worker/<topic>.json: ")][0]
	return json.loads(line.split(": ", 1)[1])


def test_estimate_branch_adds_the_gate_waits_for_the_rounds(tmp_path, home):
	repo = mainRepo(tmp_path)
	result = costHistory(repo, home, "estimate", "branch", "feature/transform", "logic", "5", "40",
		"--reviews", "2", "--dev-rounds", "3")
	assert result.returncode == 0, result.stderr
	config = workerJson(result.stdout)
	# no branch rows: no bias; 2 x 12.4 (longest reviewer wait) + 3 x 2.6
	assert config["hook_wait_minutes"] == pytest.approx(2 * 12.4 + 3 * 2.6)
	assert config["minutes"] == math.ceil(40 + 2 * 12.4 + 3 * 2.6)
	assert config["raw_minutes"] == 40 and config["est_usd"] == 5
	assert f"{math.ceil(config['minutes'] * HEADROOM)} min" in result.stdout


def test_estimate_branch_needs_the_round_counts_of_its_class(tmp_path, home):
	repo = mainRepo(tmp_path)
	missing = costHistory(repo, home, "estimate", "branch", "feature/transform", "logic", "5", "40", "--dev-rounds", "1")
	assert missing.returncode != 0 and "--reviews" in missing.stderr
	wrong = costHistory(repo, home, "estimate", "branch", "fix/api", "small fix", "1", "10",
		"--reviews", "1", "--dev-rounds", "1")
	assert wrong.returncode != 0 and "no code-reviewer" in wrong.stderr
	docs = costHistory(repo, home, "estimate", "branch", "feature/docs", "docs", "1", "10")
	assert docs.returncode == 0 and workerJson(docs.stdout)["hook_wait_minutes"] == 0


def test_estimate_branch_adds_no_wait_without_quality_gates(tmp_path, home):
	repo = mainRepo(tmp_path, gates=False)
	result = costHistory(repo, home, "estimate", "branch", "feature/transform", "logic", "5", "40",
		"--reviews", "2", "--dev-rounds", "1")
	assert result.returncode == 0, result.stderr
	assert workerJson(result.stdout)["minutes"] == 40


def test_estimate_agent_gives_a_small_job_its_fixed_part(tmp_path, home):
	repo = mainRepo(tmp_path)
	# reviewer rows: every round 5 min and $1 whatever its estimate -> fixed 5 min, $1; slope 0. A single factor
	# (0.5 for minutes) gave this 2-minute job 1 minute, below what any review takes.
	result = costHistory(repo, home, "estimate", "agent", "code-reviewer", "0.2", "2")
	assert result.returncode == 0, result.stderr
	assert "Budget: 5 min, $1" in result.stdout


def devRow(est, act, stopped=False):
	note = "est=raw (budget $1, 10 min)" + ("; budget-stopped" if stopped else "")
	return {"date": "2026-11-01", "topic": "t", "branch": "feature/research/t", "class": "logic",
		"agent": "developer", "round": "1", "estMin": "10", "actMin": str(10 * act / est), "estUsd": str(est),
		"actUsd": str(act), "note": note}


def test_estimate_agent_is_the_fixed_part_plus_the_largest_slope(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	# actuals 9 (oldest), 1, 1, 1 of 10: fixed = median 1; slope = max((9 - 1) / 10, 0) = 0.8
	writeHistory(repo, [devRow(10, 9), devRow(10, 1), devRow(10, 1), devRow(10, 1)])
	result = costHistory(repo, home, "estimate", "agent", "developer", "2", "20")
	assert result.returncode == 0, result.stderr
	assert "Budget: 17 min, $2.6" in result.stdout  # 1 + 0.8 x 20, 1 + 0.8 x 2


def test_estimate_agent_counts_a_stopped_round_only_as_a_lower_bound(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	# the stopped round's 6 is what it used before the stop: it can only raise the slope
	writeHistory(repo, [devRow(10, 2), devRow(10, 6, stopped=True), devRow(10, 2)])
	result = costHistory(repo, home, "estimate", "agent", "developer", "1", "10")
	assert "Budget: 6 min, $2.4" in result.stdout  # fixed 2 + 0.4 x 10, 2 + 0.4 x 1


def test_estimate_agent_has_no_floor(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	assert "agent_bias_floor" not in DEFAULTS
	writeHistory(repo, [devRow(10, 0.5), devRow(10, 0.5)])
	result = costHistory(repo, home, "estimate", "agent", "developer", "4", "30")
	assert "Budget: 1 min, $0.5" in result.stdout  # fixed 0.5 (rounded up to a minute), slope 0


def test_estimate_agent_uses_only_the_last_window_of_rounds(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	window = DEFAULTS["estimate_window_count"]
	# newest first in the file is oldest first: the 9 is one round past the window and drops out
	writeHistory(repo, [devRow(10, 9)] + [devRow(10, 7)] * window)
	result = costHistory(repo, home, "estimate", "agent", "developer", "1", "10")
	assert "Budget: 7 min, $7" in result.stdout


def test_estimate_agent_without_history_is_the_raw_estimate(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	writeHistory(repo, [])
	result = costHistory(repo, home, "estimate", "agent", "developer", "1.5", "12")
	assert "Budget: 12 min, $1.5" in result.stdout


def test_a_resume_after_a_checkpoint_is_never_below_the_raw_estimate(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	writeHistory(repo, [devRow(10, 1)] * 3)  # the model alone: 1 min, $1
	plain = costHistory(repo, home, "estimate", "agent", "developer", "4", "10")
	assert "Budget: 1 min, $1" in plain.stdout
	resumed = costHistory(repo, home, "estimate", "agent", "developer", "4", "10", "--resume")
	assert "Budget: 10 min, $4" in resumed.stdout
	wrong = costHistory(repo, home, "estimate", "branch", "feature/docs", "docs", "1", "10", "--resume")
	assert wrong.returncode != 0 and "--resume only goes with estimate agent" in wrong.stderr


# worker-control.py: the limits sit budget_headroom_factor above the estimate

def test_worker_control_passes_the_limits_and_records_the_estimate(tmp_path):
	home = tmp_path / "home"
	hooks = home / ".claude" / "hooks" / "worker"
	hooks.mkdir(parents=True)
	argsFile = tmp_path / "args.txt"
	(hooks / "run-worker.sh").write_text(
		f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {argsFile}.part\nmv {argsFile}.part {argsFile}\n")
	(hooks / "run-worker.sh").chmod(0o755)
	main = tmp_path / "proj" / "main"
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / "t.json").write_text(json.dumps({"minutes": 40, "est_usd": 4,
		"raw_minutes": 30, "raw_usd": 5, "hook_wait_minutes": 10, "class": "logic"}))
	worktree = tmp_path / "proj" / "worktrees" / "t"
	worktree.mkdir(parents=True)
	(worktree / ".git").write_text("gitdir: x\n")
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": "wt launch t", "cwd": str(main)}), capture_output=True, text=True,
		env=env(home))
	assert result.returncode == 0, result.stderr
	for _ in range(50):
		if argsFile.exists() and argsFile.read_text():
			break
		time.sleep(0.1)
	args = argsFile.read_text().splitlines()
	assert args[5] == str(math.ceil(40 * HEADROOM)) and float(args[6]) == round(4 * HEADROOM, 2)
	est = json.loads((worktree / ".hook-only" / "runs" / "est-1.json").read_text())
	assert est["minutes"] == 40 and est["usd"] == 4 and est["raw_minutes"] == 30 and est["hook_wait_minutes"] == 10


# automatic resume: dispatch-guard records, worker-control.py auto decides and starts

def test_dispatch_guard_records_what_a_resume_needs(tmp_path, home):
	repo = workerRepo(tmp_path, gates=False, minutesAgo=30)
	out, err = hook("dispatch-guard.py", dispatch(repo, "qa", "Budget: 5 min, $1"), home, CLAUDE_WORKER_MINUTES="32")
	assert denied(out), err
	record = json.loads((repo / ".hook-only" / "runs" / "auto-resume-1.json").read_text())
	margin, startup = DEFAULTS["worker_dispatch_margin_minutes"], DEFAULTS["worker_resume_startup_minutes"]
	assert record["minutes"] == math.ceil(5 + margin + startup)
	assert record["usd"] == round(1 + DEFAULTS["worker_dispatch_margin_usd"] + DEFAULTS["worker_resume_startup_usd"], 2)


def test_dispatch_guard_records_nothing_when_the_dispatch_fits(tmp_path, home):
	repo = workerRepo(tmp_path, gates=False, minutesAgo=1)
	out, _ = hook("dispatch-guard.py", dispatch(repo, "qa", "Budget: 5 min, $1"), home, CLAUDE_WORKER_MINUTES="60")
	assert not denied(out)
	assert not (repo / ".hook-only" / "runs" / "auto-resume-1.json").exists()


SESSION = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def stoppedRun(tmp_path):
	"""(home, main, worktree, args file): run 1 ended `STATUS: blocked` after a dispatch-guard denial."""
	home = tmp_path / "home"
	hooks = home / ".claude" / "hooks" / "worker"
	hooks.mkdir(parents=True)
	argsFile = tmp_path / "args.txt"
	(hooks / "run-worker.sh").write_text(
		f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {argsFile}.part\nmv {argsFile}.part {argsFile}\n")
	(hooks / "run-worker.sh").chmod(0o755)
	main = tmp_path / "proj" / "main"
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / "t.json").write_text(json.dumps({"minutes": 40, "est_usd": 4}))
	worktree = tmp_path / "proj" / "worktrees" / "t"
	runs = worktree / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	(worktree / ".git").write_text("gitdir: x\n")
	runstate.launchRecord(str(worktree), 1, {"topic": "t", "started_by": "person", "purpose": "build", "flow": "written",
		"launch": {"launched_at": "2026-11-01T09:59:00+08:00"}})
	(runs / "start-1").write_text("2026-11-01T10:00:00+08:00\n")
	process = {"wrapper_pid": 2**22 + 7, "started_at": "2026-11-01T10:00:00+08:00"}  # the state's pid: gone
	runstate.update(str(worktree), 1, lambda r: r.update(process=process))
	endClaude(worktree, "STATUS: blocked\nno room for qa")
	(runs / "auto-resume-1.json").write_text(json.dumps({"minutes": 11, "usd": 1.9}))
	runstate.recordDispatchStop(str(worktree), 1, 11, 1.9)
	(main / ".hook-only" / "workers").mkdir(parents=True)
	(main / ".hook-only" / "workers" / "t.json").write_text(json.dumps({"pid": 2 ** 22 + 7, "session_id": SESSION,
		"run": 1, "worktree": str(worktree)}))
	return home, main, worktree, argsFile


def endClaude(worktree, text, **extra):
	"""run-worker.sh's end of claude: result-1.json, then the record's claude part read from it (dual write)."""
	(worktree / ".hook-only" / "runs" / "result-1.json").write_text(json.dumps({"result": text, **extra}))
	runstate.recordClaudeEnded(str(worktree), 1, 0, False)


def recordedFile(worktree, name, data, step):
	"""A wrap-up program's result file of run 1 and the record's step read back from it (dual write)."""
	(worktree / ".hook-only" / "runs" / name).write_text(json.dumps(data))
	runstate.recordStepFromFile(str(worktree), 1, step)


def auto(project):
	home, main, worktree, _ = project
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py"), "auto", str(worktree), "t", "1"],
		capture_output=True, text=True, env=env(home, WT_MAIN_DIR=str(main)))
	assert result.returncode == 0, result.stderr
	return json.loads((worktree / ".hook-only" / "runs" / "auto-1.json").read_text())


def waitForArgs(argsFile):
	for _ in range(100):
		if argsFile.exists() and argsFile.read_text():
			return argsFile.read_text().splitlines()
		time.sleep(0.1)
	raise AssertionError("run-worker.sh was not started")


def test_auto_resume_starts_the_next_run_with_the_recorded_need(stoppedRun):
	home, main, worktree, argsFile = stoppedRun
	decision = auto(stoppedRun)
	assert decision["decision"] == "resume" and decision["next_run"] == 2
	args = waitForArgs(argsFile)
	assert args[3] == "resume" and args[4] == SESSION and args[2] == "2"
	assert args[5] == str(math.ceil(11 * HEADROOM)) and float(args[6]) == round(1.9 * HEADROOM, 2)
	assert args[8].startswith("Automatic resume")
	est = json.loads((worktree / ".hook-only" / "runs" / "est-2.json").read_text())
	assert est["auto"] is True and est["minutes"] == 11
	state = json.loads((main / ".hook-only" / "workers" / "t.json").read_text())
	assert state["auto_resumes"] == 1 and state["run"] == 2


@pytest.mark.parametrize("change,reason", [
	("count", "automatic resume(s) in a row"),
	("hold", "ALERT or HOLD"),
	("status", "didn't end with STATUS: blocked"),
	("stopped", "wt stop"),
	("expensive", "above worker_auto_resume_max"),
	("nousd", "no USD budget"),
])
def test_auto_resume_refuses(stoppedRun, change, reason):
	home, main, worktree, argsFile = stoppedRun
	runs = worktree / ".hook-only" / "runs"
	stateFile = main / ".hook-only" / "workers" / "t.json"
	if change == "count":
		stateFile.write_text(json.dumps({**json.loads(stateFile.read_text()), "auto_resumes": DEFAULTS["worker_auto_resume_max"]}))
	elif change == "hold":
		(worktree / ".hook-only" / "agent-guard").mkdir(parents=True)
		(worktree / ".hook-only" / "agent-guard" / "HOLD").write_text("x")
	elif change == "status":
		endClaude(worktree, "STATUS: needs-decision\nwhich rule?")
	elif change == "stopped":
		(runs / "stopped-1").write_text("x")
		runstate.recordStop(str(worktree), 1, "person")
	elif change == "expensive":
		(runs / "auto-resume-1.json").write_text(json.dumps({"minutes": 11, "usd": DEFAULTS["worker_auto_resume_max_usd"] + 1}))
	elif change == "nousd":
		(runs / "auto-resume-1.json").write_text(json.dumps({"minutes": 11, "usd": None}))
	decision = auto(stoppedRun)
	assert decision["decision"] == "refused" and reason in decision["reason"]
	time.sleep(0.3)
	assert not argsFile.exists()


def behindMain(stoppedRun):
	"""Run 1 ended STATUS: done and auto-merge.py asked for a merge run."""
	home, main, worktree, argsFile = stoppedRun
	endClaude(worktree, "STATUS: done\nall green")
	recordedFile(worktree, "merge-1.json", {"run": 1, "decision": "refused", "merge_run": True,
		"reason": "the branch doesn't contain main: a merge run is needed first"}, "merge")
	return stoppedRun


def autoMergeRun(project):
	home, main, worktree, _ = project
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py"), "auto-merge", str(worktree), "t", "1"],
		capture_output=True, text=True, env=env(home, WT_MAIN_DIR=str(main)))
	assert result.returncode == 0, result.stderr
	return json.loads((worktree / ".hook-only" / "runs" / "auto-1.json").read_text())


def test_auto_merge_run_resumes_in_merge_mode(stoppedRun):
	home, main, worktree, argsFile = behindMain(stoppedRun)
	decision = autoMergeRun(stoppedRun)
	assert decision["decision"] == "merge" and decision["next_run"] == 2
	args = waitForArgs(argsFile)
	assert args[3] == "resume" and args[4] == SESSION and args[2] == "2"
	minutes, usd = DEFAULTS["worker_auto_merge_minutes"], DEFAULTS["worker_auto_merge_usd"]
	assert args[5] == str(math.ceil(minutes * HEADROOM)) and float(args[6]) == round(usd * HEADROOM, 2)
	assert args[8].startswith("Merge mode")
	est = json.loads((worktree / ".hook-only" / "runs" / "est-2.json").read_text())
	assert est["auto"] is True and est["mode"] == "merge"
	state = json.loads((main / ".hook-only" / "workers" / "t.json").read_text())
	assert state["mode"] == "merge" and state["auto_merges"] == 1 and state.get("auto_resumes", 0) == 0


@pytest.mark.parametrize("change,reason", [
	("count", "automatic merge run(s) in a row"),
	("hold", "ALERT or HOLD"),
	("asked", "didn't ask for a merge run"),
	("stopped", "wt stop"),
])
def test_auto_merge_run_refuses(stoppedRun, change, reason):
	home, main, worktree, argsFile = behindMain(stoppedRun)
	runs = worktree / ".hook-only" / "runs"
	stateFile = main / ".hook-only" / "workers" / "t.json"
	if change == "count":
		stateFile.write_text(json.dumps({**json.loads(stateFile.read_text()), "auto_merges": DEFAULTS["worker_auto_merge_max"]}))
	elif change == "hold":
		(worktree / ".hook-only" / "agent-guard").mkdir(parents=True)
		(worktree / ".hook-only" / "agent-guard" / "HOLD").write_text("x")
	elif change == "asked":
		recordedFile(worktree, "merge-1.json", {"run": 1, "decision": "refused", "reason": "red suite"}, "merge")
	elif change == "stopped":
		(runs / "stopped-1").write_text("x")
		runstate.recordStop(str(worktree), 1, "person")
	decision = autoMergeRun(stoppedRun)
	assert decision["decision"] == "refused" and reason in decision["reason"]
	time.sleep(0.3)
	assert not argsFile.exists()


def test_a_resume_typed_by_the_user_resets_the_count(stoppedRun):
	home, main, worktree, argsFile = stoppedRun
	stateFile = main / ".hook-only" / "workers" / "t.json"
	stateFile.write_text(json.dumps({**json.loads(stateFile.read_text()), "auto_resumes": 1}))
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": "wt resume t", "cwd": str(main)}), capture_output=True, text=True, env=env(home))
	assert result.returncode == 0, result.stderr
	waitForArgs(argsFile)
	assert "auto_resumes" not in json.loads(stateFile.read_text())


def test_a_dispatch_that_fits_removes_the_record(tmp_path, home):
	repo = workerRepo(tmp_path, gates=False, minutesAgo=1)
	record = repo / ".hook-only" / "runs" / "auto-resume-1.json"
	record.write_text(json.dumps({"minutes": 11, "usd": 1.9}))
	out, _ = hook("dispatch-guard.py", dispatch(repo, "qa", "Budget: 5 min, $1"), home, CLAUDE_WORKER_MINUTES="60")
	assert not denied(out) and not record.exists()


def test_auto_resume_gets_what_is_left_of_the_estimate(stoppedRun):
	home, main, worktree, argsFile = stoppedRun
	os.symlink(SKILLS, home / ".claude" / "skills")
	runs = worktree / ".hook-only" / "runs"
	(runs / "est-1.json").write_text(json.dumps({"minutes": 40, "usd": 4}))
	start = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 10 * 60))
	start = start[:-2] + ":" + start[-2:]
	(runs / "start-1").write_text(start + "\n")
	endClaude(worktree, "STATUS: blocked\nno room", total_cost_usd=1.5, session_id=SESSION)
	call = {"k": 0, "kind": "main", "file": "result-1.json", "started_at": start, "ended_at": start, "exit_code": 0,
		"cost_usd": 1.5}

	def asRun(record):  # the record of the same run: its estimate, its wrapper's start, its spend
		record.update(estimate={"minutes": 40, "usd": 4}, process={"started_at": start})
		record["claude"]["invocations"] = [call]

	runstate.update(str(worktree), 1, asRun)
	decision = auto(stoppedRun)
	assert decision["decision"] == "resume"
	assert decision["minutes"] == 30 and decision["usd"] == 2.5
	args = waitForArgs(argsFile)
	assert args[5] == str(math.ceil(30 * HEADROOM))


# worker-control.py: a topic keeps the flow mode it was launched in (`wt local on|off`)

def launchTopic(tmp_path, mainRecord=None, topicRecord=None, **extra):
	"""`wt launch t` through worker-control.py; the main checkout's and the topic's flow-mode records first, if given."""
	home = tmp_path / "home"
	hooks = home / ".claude" / "hooks" / "worker"
	hooks.mkdir(parents=True)
	argsFile = tmp_path / "args.txt"
	(hooks / "run-worker.sh").write_text(
		f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {argsFile}.part\nmv {argsFile}.part {argsFile}\n")
	(hooks / "run-worker.sh").chmod(0o755)
	main = tmp_path / "proj" / "main"
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / "t.json").write_text(json.dumps({"minutes": 40, "est_usd": 4}))
	worktree = tmp_path / "proj" / "worktrees" / "t"
	gitWorktree(main, worktree)
	for checkout, mode in ((main, mainRecord), (worktree, topicRecord)):
		if mode:
			(checkout / ".hook-only").mkdir(exist_ok=True)
			(checkout / ".hook-only" / "flow-mode").write_text(mode + "\n")
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": "wt launch t", "cwd": str(main)}), capture_output=True, text=True,
		env=env(home, **extra))
	assert result.returncode == 0, result.stderr
	return worktree, result.stdout


def test_launch_records_the_topic_flow_mode(tmp_path, flowMode):
	worktree, out = launchTopic(tmp_path, mainRecord=flowMode)
	assert (worktree / ".hook-only" / "flow-mode").read_text().strip() == flowMode
	assert ("local mode" in out) == (flowMode == "local")


def test_a_recorded_flow_mode_is_never_rewritten_by_launch(tmp_path, monkeypatch, flowMode):
	# the topic's record is the other mode than the main checkout's (flowMode here: the main record): launch keeps it
	# and reports it
	recorded = "origin" if flowMode == "local" else "local"
	worktree, out = launchTopic(tmp_path, mainRecord=flowMode, topicRecord=recorded)
	assert (worktree / ".hook-only" / "flow-mode").read_text().strip() == recorded
	assert ("local mode" in out) == (recorded == "local")


def test_the_replay_runs_the_model_over_a_history(tmp_path, home):
	repo = gitRepo(tmp_path / "main")
	writeHistory(repo, [devRow(10, 2), devRow(10, 2), devRow(10, 9), devRow(10, 2)])
	result = subprocess.run([sys.executable, os.path.join(SKILLS, "repo-plan", "estimate-replay.py"),
		str(repo / ".claude-cost" / "history.tsv"), "--window", "3"], capture_output=True, text=True, env=env(home))
	assert result.returncode == 0, result.stderr
	assert "window 3" in result.stdout
	# the first round has no history (raw 10 vs 2: room); the 9 after two 2s is above 1.25 x (2 + 0 x 10)
	assert "subagent rounds  stopped   1 of   4" in result.stdout


# dispatch-guard.py: the hook turns the raw estimate into the Budget line

def qaRow(est, act):
	return {"date": "2026-10-31", "topic": "t", "branch": "feature/transform/t", "class": "logic", "agent": "qa",
		"round": "1", "estMin": "10", "actMin": "4", "estUsd": est, "actUsd": act,
		"note": f"est=raw (budget ${est}, 10 min)"}


def modelledRepo(tmp_path):
	repo = workerRepo(tmp_path, gates=False)
	writeHistory(repo, [qaRow("2", "0.5"), qaRow("1", "0.4"), qaRow("0.5", "0.3")])
	return repo


def updated(out, field="prompt"):
	return json.loads(out)["hookSpecificOutput"]["updatedInput"][field]


def printedLines(repo, home, *args):
	out = costHistory(repo, home, "estimate", "agent", *args).stdout.splitlines()
	return [l for l in out if l.startswith(("Budget:", "Follow the budget rules"))]


def pending(repo, key):
	return json.loads((repo / ".hook-only" / "budget" / f"pending-{key}.json").read_text())


def test_an_estimate_line_gets_the_budget_estimate_agent_would_print(tmp_path, home):
	repo = modelledRepo(tmp_path)
	out, err = hook("dispatch-guard.py", dispatch(repo, "qa", "Write the tests.\nEstimate: 12 min, $1.5"), home)
	prompt = updated(out)
	budget, rule = printedLines(repo, home, "qa", "1.5", "12")
	assert prompt == f"Write the tests.\n{budget}\n{rule}\nEstimate: 12 min, $1.5", err
	recorded = pending(repo, "qa")
	assert (recorded["raw_minutes"], recorded["raw_usd"]) == (12, 1.5)
	assert f"Budget: {math.ceil(recorded['minutes'])} min" in budget


def test_a_remaining_line_is_replaced_by_a_resume_budget_and_records_no_raw_estimate(tmp_path, home):
	repo = modelledRepo(tmp_path)
	(repo / ".hook-only" / "budget").mkdir(parents=True)
	(repo / ".hook-only" / "budget" / "events.jsonl").write_text(json.dumps(
		{"event": "SubagentStart", "agent_id": "a7", "agent_type": "qa"}) + "\n")
	out, err = hook("dispatch-guard.py", dispatch(repo, "a7", "Go on.\nRemaining: 6 min, $0.8", tool="SendMessage"), home)
	message = updated(out, "message")
	budget, rule = printedLines(repo, home, "qa", "0.8", "6", "--resume")
	assert message == f"Go on.\n{budget}\n{rule}", err
	recorded = pending(repo, "a7")
	assert "raw_usd" not in recorded and recorded["usd"] >= 0.8


def test_a_written_budget_line_is_kept_as_it_is(tmp_path, home):
	repo = modelledRepo(tmp_path)
	out, err = hook("dispatch-guard.py", dispatch(repo, "qa", "x\nBudget: 5 min, $1\nEstimate: 12 min, $1.5"), home)
	assert out == "", (out, err)
	assert pending(repo, "qa")["usd"] == 1


def test_a_dispatch_without_any_budget_line_is_denied_with_the_estimate_format(tmp_path, home):
	repo = modelledRepo(tmp_path)
	out, _ = hook("dispatch-guard.py", dispatch(repo, "qa", "Write the tests."), home)
	assert denied(out)
	assert "Estimate: <minutes> min, $<usd>" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_estimate_without_a_history_copy_is_denied(tmp_path, home):
	repo = gitRepo(tmp_path / "wt")
	out, _ = hook("dispatch-guard.py", dispatch(repo, "qa", "Estimate: 12 min, $1.5"), home)
	assert denied(out)
	assert "no cost history" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_review_with_an_estimate_gets_the_fixed_prompt_with_the_modelled_budget(tmp_path, home):
	repo = workerRepo(tmp_path, gates=False)
	out, err = hook("dispatch-guard.py", dispatch(repo, "code-reviewer", "Estimate: 10 min, $1"), home)
	budget, _ = printedLines(repo, home, "code-reviewer", "1", "10")
	prompt = updated(out)
	assert budget in prompt and "Review the current branch" in prompt and "Estimate:" not in prompt, err
	assert pending(repo, "code-reviewer")["raw_usd"] == 1


def test_the_worker_room_check_uses_the_modelled_budget(tmp_path, home):
	repo = modelledRepo(tmp_path)  # 30 of 32 minutes gone
	out, _ = hook("dispatch-guard.py", dispatch(repo, "qa", "Estimate: 12 min, $1.5"), home, CLAUDE_WORKER_MINUTES="32")
	assert denied(out)
	assert "Not enough worker time" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]

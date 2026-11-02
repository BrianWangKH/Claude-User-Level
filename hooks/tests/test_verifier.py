"""Real-data verification, second layer (2026-11-01): verifier.py runs a forked verifier agent on the approved
plan and a copy of the real data; auto-merge.py condition 9 reads its verdict, and a failure of either layer
goes back to the worker as a fix run (worker-control.py auto-fix). Also: the approved plan copy, the fix-run
estimate and import (cost-history.py --fix), the queue's prune in status.

Run: uv run --offline --no-project --with pytest --with pytest-cov==7.1.0 python -m pytest ~/.claude/hooks/tests/test_verifier.py
A fake `claude` on PATH stands in for the headless session: it records what it was given and prints a report.
"""
import csv
import json
import os
import subprocess
import sys
import time

import pytest

from test_local_mode import (HOOKS, PY, REAL_HOOKS, SANDBOX_ENV, SKILLS, autoMerge, git, loadHook,  # noqa: F401
	project, refused, toolsHome, verifySpec, write)

SESSION = "11111111-2222-3333-4444-555555555555"
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
cwd = os.getcwd()
inputs = os.path.join(cwd, ".hook-only", "verifier-1")
pending = os.path.join(cwd, ".hook-only", "budget", "pending-verifier.json")
seen = {"argv": sys.argv[1:], "cwd": cwd,
	"inputs": sorted(os.path.relpath(os.path.join(d, f), inputs) for d, _, fs in os.walk(inputs) for f in fs),
	"pending": json.load(open(pending)) if os.path.exists(pending) else None}
json.dump(seen, open(os.environ["FAKE_LOG"], "w"))
if os.environ.get("FAKE_TOUCH"):
	open(os.path.join(cwd, "stray.txt"), "w").write("x")
print(json.dumps({"result": os.environ.get("FAKE_REPORT", ""), "total_cost_usd": 0.4, "is_error": False}))
'''
PASS = "DONE\nVERDICT: pass\nV1 ok: rule E1 - 3 rows checked\nUnclear: none\n- **Effort:** 3\n- **Friction:** None"
FAIL = ("DONE\nVERDICT: fail\nV1 ok: rule E1\nV2 fail: rule E2 - row 2330 2024-03-01 uses revenue of 2024-03-10\n"
	"Unclear: none")


@pytest.fixture
def fake(tmp_path, toolsHome):
	"""(env for verifier.py, log path): a fake claude first on PATH, HOME with the hooks and skills under test."""
	bin = tmp_path / "bin"
	bin.mkdir()
	(bin / "claude").write_text(FAKE_CLAUDE)
	(bin / "claude").chmod(0o755)
	log = tmp_path / "claude-log.json"
	env = {**os.environ, "PATH": f"{bin}:{os.environ['PATH']}", "HOME": str(toolsHome), "FAKE_LOG": str(log),
		"FLOW_INTEGRATION": "local", "PYTHONPATH": REAL_HOOKS, **SANDBOX_ENV}
	return env, log


def ready(main, wt, verifier=True, approved=True, layer1=True):
	"""A spec (with a verifier section), the approved plan and a passed first layer for run 1."""
	verifySpec(main, [{"name": "x", "run": [PY, "-c", "pass"]}],
		**({"verifier": {"usd": 1.0, "minutes": 10, "judge": ["values in range"]}} if verifier else {}))
	if approved:
		write(main, ".hook-only/approved/topic.md", "# topic\n\n- E1: ...\n- E2: ...\n")
	(wt / ".hook-only" / "runs" / "verify-1.json").write_text(json.dumps({"passed": layer1, "checks": []}))


def runVerifier(main, wt, env, **extra):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "verifier.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=120, env={**env, "WT_MAIN_DIR": str(main), **extra})
	assert result.returncode == 0, result.stderr
	path = wt / ".hook-only" / "runs" / "verifier-1.json"
	return json.loads(path.read_text()) if path.exists() else None


def events(main):
	path = main / ".hook-only" / "events.jsonl"
	return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


# verifier.py ---------------------------------------------------------------------------------------

def test_the_verifier_gets_the_approved_plan_the_data_and_a_budget_and_passes(project, fake):
	main, wt = project
	env, log = fake
	ready(main, wt)
	result = runVerifier(main, wt, env, FAKE_REPORT=PASS)
	assert result["verdict"] == "passed" and result["usd"] == 0.4
	seen = json.loads(log.read_text())
	assert seen["argv"][:2] == ["-p", "/verify-topic topic 1"] and seen["cwd"] == str(wt)
	assert {"plan.md", "spec.json", "layer1.json", "data/features/x.txt"} <= set(seen["inputs"])
	# no history: the raw estimate as it is, claimed by subagent-budget.py as agent verifier
	assert seen["pending"]["agent_type"] == "verifier"
	assert seen["pending"]["usd"] == 1.0 and seen["pending"]["minutes"] == 10 and seen["pending"]["raw_usd"] == 1.0
	assert not (wt / ".hook-only" / "verifier-1" / "data").exists()  # the data copy is removed
	assert not (wt / ".hook-only" / "budget" / "pending-verifier.json").exists()
	assert (wt / ".hook-only" / "verifier-1" / "plan.md").read_text().startswith("# topic")
	assert events(main)[-1]["kind"] == "verified" and "passed" in events(main)[-1]["text"]


def test_a_fail_verdict_is_failed_and_its_failing_lines_reach_the_event(project, fake):
	main, wt = project
	env, _ = fake
	ready(main, wt)
	assert runVerifier(main, wt, env, FAKE_REPORT=FAIL)["verdict"] == "failed"
	assert "V2 fail: rule E2" in events(main)[-1]["text"]


@pytest.mark.parametrize("report", ["", "DONE\nlooks fine", "CHECKPOINT\nVERDICT: pass",
	"DONE\nVERDICT: pass\nVERDICT: fail"])
def test_no_single_verdict_after_done_is_an_error_never_a_pass(project, fake, report):
	main, wt = project
	env, _ = fake
	ready(main, wt)
	result = runVerifier(main, wt, env, FAKE_REPORT=report)
	assert result["verdict"] == "error" and "VERDICT" in result["why"]


def test_a_verifier_that_changes_the_worktree_voids_its_verdict(project, fake):
	main, wt = project
	env, _ = fake
	ready(main, wt)
	result = runVerifier(main, wt, env, FAKE_REPORT=PASS, FAKE_TOUCH="1")
	assert result["verdict"] == "error" and "changed the worktree" in result["why"]


def test_without_an_approved_plan_the_verifier_does_not_run(project, fake):
	main, wt = project
	env, log = fake
	ready(main, wt, approved=False)
	result = runVerifier(main, wt, env, FAKE_REPORT=PASS)
	assert result["verdict"] == "error" and "no approved plan" in result["why"]
	assert not log.exists()


@pytest.mark.parametrize("kwargs", [{"verifier": False}, {"layer1": False}])
def test_nothing_to_do_without_a_verifier_section_or_a_passed_first_layer(project, fake, kwargs):
	main, wt = project
	env, log = fake
	ready(main, wt, **kwargs)
	assert runVerifier(main, wt, env, FAKE_REPORT=PASS) is None
	assert not log.exists()


def test_the_budget_follows_the_verifier_history(project, fake):
	main, wt = project
	env, log = fake
	ready(main, wt)
	columns = ["date", "topic", "branch", "class", "agent", "round", "estTok", "actTok", "estMin", "actMin",
		"estUsd", "actUsd", "note"]
	rows = [dict(zip(columns, ["2026-10-31", "x", "b", "logic", "verifier", "1", "", "", "10", "14", "1", "1.5",
		"est=raw"]))]
	(wt / ".claude-cost").mkdir()
	with open(wt / ".claude-cost" / "history.tsv", "w", encoding="utf-8", newline="") as f:
		writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t")
		writer.writeheader()
		writer.writerows(rows)
	runVerifier(main, wt, env, FAKE_REPORT=PASS)
	pending = json.loads(log.read_text())["pending"]
	assert pending["usd"] == 1.5 and pending["minutes"] == 14 and pending["raw_minutes"] == 10


# auto-merge.py conditions 8 and 9, fix runs ---------------------------------------------------------

def verdict(wt, **result):
	(wt / ".hook-only" / "runs" / "verifier-1.json").write_text(json.dumps({"run": 1, **result}))


def test_auto_merge_needs_a_passed_verifier_when_the_spec_has_one(project):
	main, wt = project
	ready(main, wt)
	assert "verifier.py left no result" in refused(main, wt)
	verdict(wt, verdict="error", why="no report")
	decision = autoMerge(main, wt)
	assert "could not judge: no report" in decision["reason"] and "fix_run" not in decision
	verdict(wt, verdict="passed", report=PASS)
	assert autoMerge(main, wt)["decision"] == "merged"


def test_a_failed_verdict_asks_for_a_fix_run_as_a_retry_event(project):
	main, wt = project
	ready(main, wt)
	verdict(wt, verdict="failed", report=FAIL)
	decision = autoMerge(main, wt)
	assert decision["fix_run"] is True and "V2 fail: rule E2" in decision["reason"]
	assert events(main)[-2]["kind"] == "retry"
	# no worker state in this fixture: worker-control refuses, records why, and tells the person
	assert json.loads((wt / ".hook-only" / "runs" / "auto-1.json").read_text())["decision"] == "refused"
	assert events(main)[-1]["kind"] == "refused" and "no automatic fix run" in events(main)[-1]["text"]


def test_a_failed_first_layer_check_asks_for_a_fix_run_but_an_error_waits(project):
	main, wt = project
	verifySpec(main, [{"name": "x", "run": [PY, "-c", "pass"]}])
	(wt / ".hook-only" / "runs" / "verify-1.json").write_text(json.dumps({"passed": False,
		"checks": [{"name": "det", "ok": False, "why": "differs between two runs: out.txt"}]}))
	assert autoMerge(main, wt)["fix_run"] is True
	(wt / ".hook-only" / "runs" / "verify-1.json").write_text(json.dumps({"passed": False, "checks": [],
		"error": "data path 'nope' not in data"}))
	decision = autoMerge(main, wt)
	assert "could not run" in decision["reason"] and "fix_run" not in decision


def test_the_persons_merge_never_starts_a_fix_run(project):
	main, wt = project
	ready(main, wt)
	verdict(wt, verdict="failed", report=FAIL)
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "auto-merge.py"), str(wt), "topic", "1", "--person"],
		capture_output=True, text=True, timeout=600, env={**os.environ, "FLOW_INTEGRATION": "local",
		"WT_MAIN_DIR": str(main), "PYTHONPATH": REAL_HOOKS, **SANDBOX_ENV})
	assert result.returncode == 0, result.stderr
	decision = json.loads((wt / ".hook-only" / "runs" / "merge-1.json").read_text())
	assert decision["decision"] == "refused" and "fix_run" not in decision
	assert not (wt / ".hook-only" / "runs" / "auto-1.json").exists()


# worker-control.py: the approved plan, auto-fix --------------------------------------------------------

def test_a_run_the_person_starts_approves_the_plan_as_it_stands(project):
	main, wt = project
	write(wt, "plans/topic.md", "# topic v2\n")
	loadHook("worker-control.py", "worker_control").approve_plan(main, wt, "topic")
	assert (main / ".hook-only" / "approved" / "topic.md").read_text() == "# topic v2\n"


def fixReady(main, wt, auto_fixes=0):
	state = main / ".hook-only" / "workers"
	state.mkdir(parents=True, exist_ok=True)
	(state / "topic.json").write_text(json.dumps({"pid": 0, "session_id": SESSION, "run": 1,
		"auto_fixes": auto_fixes}))
	(wt / ".hook-only" / "runs" / "merge-1.json").write_text(json.dumps({"decision": "refused", "fix_run": True}))
	write(main, "plans/worker/topic.json", json.dumps({"minutes": 20, "est_usd": 2, "class": "logic"}))
	write(main, ".claude-cost/history.tsv", "date\ttopic\tbranch\tclass\tagent\tround\testTok\tactTok\testMin\t"
		"actMin\testUsd\tactUsd\tnote\n")


def autoFix(main, wt, home):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "worker-control.py"), "auto-fix", str(wt), "topic",
		"1"], capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": str(home),
		"WT_MAIN_DIR": str(main), "PYTHONPATH": REAL_HOOKS})
	assert result.returncode == 0, result.stderr
	return json.loads((wt / ".hook-only" / "runs" / "auto-1.json").read_text())


@pytest.fixture
def fakeWrapperHome(tmp_path):
	"""A home whose run-worker.sh only records its arguments (auto-start must not start a real worker)."""
	home = tmp_path / "wrapper-home"
	hooks = home / ".claude" / "hooks"
	hooks.mkdir(parents=True)
	for name in os.listdir(HOOKS):
		if name != "run-worker.sh" and name != "tests":
			os.symlink(os.path.join(HOOKS, name), hooks / name)
	os.symlink(SKILLS, home / ".claude" / "skills")
	args = tmp_path / "wrapper-args.txt"
	(hooks / "run-worker.sh").write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {args}\n")
	(hooks / "run-worker.sh").chmod(0o755)
	return home, args


def test_auto_fix_resumes_the_session_with_the_findings_and_a_modelled_budget(project, fakeWrapperHome):
	main, wt = project
	home, args = fakeWrapperHome
	fixReady(main, wt)
	decision = autoFix(main, wt, home)
	assert decision["decision"] == "fix" and decision["next_run"] == 2
	assert decision["raw_usd"] == 3.0 and decision["usd"] == 3.0  # no fix history yet: the raw guess
	for _ in range(100):
		if args.exists() and args.read_text():
			break
		time.sleep(0.1)
	argv = args.read_text().split("\0")
	assert argv[3] == "resume" and "Fix run:" in argv[8] and "verifier-1.json" in argv[8]
	est = json.loads((wt / ".hook-only" / "runs" / "est-2.json").read_text())
	assert est["mode"] == "fix" and est["auto"] is True and est["raw_usd"] == 3.0
	state = json.loads((main / ".hook-only" / "workers" / "topic.json").read_text())
	assert state["auto_fixes"] == 1 and "mode" not in state


def test_auto_fix_stops_at_its_limit_and_tells_the_person(project, fakeWrapperHome):
	main, wt = project
	home, args = fakeWrapperHome
	limit = json.load(open(os.path.join(HOOKS, "budget-defaults.json")))["worker_auto_fix_max"]
	fixReady(main, wt, auto_fixes=limit)
	decision = autoFix(main, wt, home)
	assert decision["decision"] == "refused" and "worker_auto_fix_max" in decision["reason"]
	assert events(main)[-1]["kind"] == "refused"
	assert not args.exists()


# cost-history.py --fix ----------------------------------------------------------------------------------

def costHistory(repo, home, *args):
	return subprocess.run([sys.executable, os.path.join(SKILLS, "repo-plan", "cost-history.py"), *args], cwd=repo,
		capture_output=True, text=True, env={**os.environ, "HOME": str(home)})


def test_fix_estimates_use_fix_rows_and_one_round_per_gated_agent(project, toolsHome):
	main, _ = project
	write(main, ".claude-cost/history.tsv", "date\ttopic\tbranch\tclass\tagent\tround\testTok\tactTok\testMin\t"
		"actMin\testUsd\tactUsd\tnote\n2026-10-31\tx\tb\tlogic\tfix\t2\t\t\t30\t12\t3\t1\test=raw\n"
		"2026-10-31\tx\tb\tlogic\tdeveloper\t1\t\t\t5\t5\t1\t1\thook wait 2 min excluded; est=raw\n"
		"2026-10-31\tx\tb\tlogic\tcode-reviewer\t1\t\t\t5\t5\t1\t1\thook wait 4 min excluded; est=raw\n")
	result = costHistory(main, toolsHome, "estimate", "branch", "feature/x", "logic", "3", "30", "--fix")
	assert result.returncode == 0, result.stderr
	line = [l for l in result.stdout.splitlines() if l.startswith("plans/worker/<topic>.json: ")][0]
	config = json.loads(line.split(": ", 1)[1])
	assert config["est_usd"] == 1 and config["hook_wait_minutes"] == 6 and config["minutes"] == 12 + 6
	assert "mode" not in config and "model fix runs" in result.stdout
	both = costHistory(main, toolsHome, "estimate", "branch", "feature/x", "logic", "3", "30", "--fix", "--merge")
	assert both.returncode != 0 and "exclude each other" in both.stderr


# wt_queue.py: status prunes ------------------------------------------------------------------------------

def test_queue_status_marks_a_merged_topic_without_a_worktree_done(project):
	main, wt = project
	git(main, "merge", "-q", "--no-ff", "-m", "merge", "feature/x/topic")
	git(main, "worktree", "remove", "--force", str(wt))
	write(main, ".hook-only/flow-mode", "local\n")
	write(main, "plans/queue/topic.json", json.dumps({"branch": "feature/x/topic", "order": 1, "after": [],
		"resources": [], "estimate": {"minutes": 1, "est_usd": 1}}))
	write(main, ".hook-only/queue/queue.json", json.dumps({"items": [{"topic": "topic", "added": "x",
		"started": "y"}]}))
	queue = loadHook("wt_queue.py", "wt_queue_under_test")
	rows = queue.status(main)["items"]
	assert rows == [{"topic": "topic", "state": "merged", "run": rows[0]["run"]}]
	saved = json.loads((main / ".hook-only" / "queue" / "queue.json").read_text())["items"]
	assert saved[0].get("done")
	assert (main / ".hook-only" / "archive" / "topic" / "queue" / "topic.json").exists()


def test_queue_status_still_reads_where_it_cannot_write(project, monkeypatch):
	main, _ = project
	write(main, ".hook-only/queue/queue.json", json.dumps({"items": [{"topic": "topic", "added": "x"}]}))
	queue = loadHook("wt_queue.py", "wt_queue_read_only")

	def readOnly(*args, **kwargs):
		raise OSError(30, "Read-only file system")
	monkeypatch.setattr(queue, "Locked", readOnly)
	assert [r["topic"] for r in queue.status(main)["items"]] == ["topic"]

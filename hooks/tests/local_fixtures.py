"""Shared fixtures of the local-mode tests (hooks/RULES.md T4): a main checkout with quality gates and a worktree
whose run 1 ended `STATUS: done` and was approved, written the way the worker's programs write it (both stories of
the run record), and the helpers that run auto-merge.py, run-steps.py's next step and verify.py's spec on them.
Used by test_local_mode.py and test_verifier.py.
"""
import importlib.util
import json
import os
import subprocess
import sys
import textwrap

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree for gate commands (config_fixtures.gateHome): the person's
# files in the tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.gateHome(), "hooks")
RUN_SKILLS = os.path.join(config_fixtures.gateHome(), "skills")
RUN_LIB = os.path.join(RUN_HOOKS, "lib")
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
LIB = os.path.join(HOOKS, "lib")  # the tree under test's hooks/lib: homepath adds config/ and the role directories
sys.path.insert(0, LIB)
import homepath  # noqa: E402

import clock  # noqa: E402
import qg_common  # noqa: E402
import runstate  # noqa: E402

UV = config_fixtures.testUv()


BWRAP_OK = config_fixtures.bwrapNests()


RUNNER = [UV, "run", "--offline", "--no-project", "--with", "pytest", "--with", "pytest-cov==7.1.0", "python", "-m",
	"pytest", "-q", "-p", "no:cacheprovider"]


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def write(repo, path, text):
	full = os.path.join(str(repo), path)
	os.makedirs(os.path.dirname(full), exist_ok=True)
	with open(full, "w", encoding="utf-8") as f:
		f.write(textwrap.dedent(text))


def loadHook(name, module):
	spec = importlib.util.spec_from_file_location(module, homepath.locate(name))
	loaded = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(loaded)
	return loaded


@pytest.fixture
def project(tmp_path):
	"""(main checkout on main with quality gates, worktree on feature/x/topic)."""
	return makeProject(tmp_path)


def makeProject(tmp_path, checks=None, gates=True):
	"""project's checkouts; checks: the python language's "checks" in the committed quality gates; gates
	False: no quality-gates.json at all (the gates detect python from the tracked files)."""
	python = {"extensions": [".py"], "coverage": RUNNER + ["--cov=pkg", "--cov-report=lcov:{report}"],
		"coverage_format": "lcov", "test": RUNNER + ["-x"]}
	if checks is not None:
		python["checks"] = checks
	main = tmp_path / "main"
	main.mkdir()
	git(main, "init", "-q", "-b", "main")
	git(main, "config", "user.email", "t@t")
	git(main, "config", "user.name", "t")
	write(main, ".gitignore", ".hook-only/\nplans/\n__pycache__/\n")
	write(main, "pkg/__init__.py", "")
	write(main, "pkg/calc.py", "def calc(x):\n    return x\n")
	write(main, "tests/test_calc.py", "from pkg.calc import calc\n\n\ndef test_calc():\n    assert calc(1) == 1\n")
	if gates:
		write(main, ".claude/quality-gates.json", json.dumps({"languages": {"python": python}, "exclude": ["tests/"],
			"auto_merge_hold": ["pkg/secret/"]}))
	git(main, "add", ".")
	git(main, "commit", "-q", "-m", "base")
	wt = tmp_path / "worktrees" / "topic"
	git(main, "worktree", "add", "-q", "-b", "feature/x/topic", str(wt))
	write(wt, "pkg/more.py", "def more(x):\n    return x + 1\n")
	write(wt, "tests/test_more.py", "from pkg.more import more\n\n\ndef test_more():\n    assert more(1) == 2\n")
	git(wt, "add", ".")
	git(wt, "commit", "-q", "-m", "implement more")
	write(main, "plans/worker/topic.json", json.dumps({"minutes": 20, "est_usd": 2, "class": "logic"}))
	recordRun(main, wt, 1, "STATUS: done\nall green")
	approve(wt)
	write(wt, "PR_DESCRIPTION.md", "Add more()\n\nDetails.\n")
	return main, wt


GONE_PID = 999999999  # the wrapper of a test run: no such process (the gate makes no pid check)


def recordRun(main, wt, run, text):
	"""Run <run> as worker-control.py and run-worker.sh leave it when auto-merge.py starts, both stories (dual
	write, the record through runstate's writers): the flow mode, the estimate and the record, the wrapper's
	start, a heartbeat, the main head, the worker's result and claude's end."""
	runs = wt / ".hook-only" / "runs"
	runs.mkdir(parents=True, exist_ok=True)
	(wt / ".hook-only" / "flow-mode").write_text("local\n")
	(runs / f"est-{run}.json").write_text(json.dumps({"minutes": 20, "usd": 2, "cap": 2.6, "cap_minutes": 26}))
	runstate.launchRecord(str(wt), run, {"topic": "topic", "started_by": "person", "purpose": "build",
		"flow": "written", "integration_mode": "local", "prompt_kind": "feature-flow",
		"launch": {"launched_at": clock.isoNow(), "by_pid": 1},
		"estimate": {"minutes": 20, "usd": 2, "cap_minutes": 26, "cap_usd": 2.6}})
	(runs / f"start-{run}").write_text(clock.isoNow() + "\n")
	runstate.recordStarted(str(wt), run, GONE_PID)
	runstate.beat(str(wt), run, runstate.settings()["heartbeat_interval_seconds"])
	setMainHead(main, wt, run)
	(runs / f"result-{run}.json").write_text(json.dumps({"result": text}))
	runstate.recordClaudeEnded(str(wt), run, 0, False)


def setMainHead(main, wt, run=1):
	"""main-<run> and the record's main head: where the main checkout's HEAD is now (run-worker.sh writes both)."""
	(wt / ".hook-only" / "runs" / f"main-{run}").write_text(git(main, "rev-parse", "HEAD") + "\n")
	runstate.recordMainHead(str(wt), run)


def stepResult(wt, name, data, run=1):
	"""verify.py, verifier.py or auto-merge.py's result file of run and the record's step read back from it, as the
	program writes both."""
	(wt / ".hook-only" / "runs" / f"{name}-{run}.json").write_text(json.dumps(data))
	runstate.recordStepFromFile(str(wt), run, {"verify": "verify", "verifier": "verifier", "merge": "merge"}[name])


def endRun(wt, run=1, code=0):
	"""run-worker.sh's end of run: exit-<run> and the record's exit."""
	(wt / ".hook-only" / "runs" / f"exit-{run}").write_text(f"{code} {clock.isoNow()}\n")
	runstate.recordRunEnded(str(wt), run, "completed", code)


def approve(wt, verdict="APPROVE"):
	with open(wt / ".hook-only" / "reviews.jsonl", "a", encoding="utf-8") as f:
		f.write(json.dumps({"agent_id": "r1", "verdict": verdict, "head": git(wt, "rev-parse", "HEAD")}) + "\n")


def autoMerge(main, wt, env=None, home=None):
	"""auto-merge.py of run 1, from the tree under test or from home, a copy of it (config_fixtures.copyHome) with its
	own settings; its merge-1.json."""
	hooks = os.path.join(home, "hooks") if home else RUN_HOOKS
	result = subprocess.run([sys.executable, os.path.join(hooks, "worker", "auto-merge.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=600, env={**os.environ,
		"WT_MAIN_DIR": str(main), "PYTHONPATH": os.path.join(hooks, "lib"), **(env or {})})
	assert result.returncode == 0, result.stderr
	return json.loads((wt / ".hook-only" / "runs" / "merge-1.json").read_text())


def nextStep(main, wt):
	"""run-steps.py's next step of run 1 (migration step 6: what auto-merge.py's decision asks for is started there,
	no longer by auto-merge.py), with a home whose hooks are the tree under test; the auto-<run>.json it left."""
	home = wt.parent.parent / "steps-home"
	if not home.exists():
		(home / ".claude").mkdir(parents=True)
		os.symlink(RUN_HOOKS, home / ".claude" / "hooks")
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "run-steps.py"), "step", str(wt), "1", "next",
		"--topic", "topic"], capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": str(home),
		"WT_MAIN_DIR": str(main)})
	assert result.returncode == 0, result.stderr
	return json.loads((wt / ".hook-only" / "runs" / "auto-1.json").read_text())


def refused(main, wt, env=None, home=None):
	decision = autoMerge(main, wt, env, home)
	assert decision["decision"] == "refused", decision
	return decision["reason"]


PY = "/usr/bin/python3"


def verifySpec(main, checks, **extra):
	write(main, "data/features/x.txt", "real\n")
	spec = {"data_paths": ["features"], "checks": checks, **extra}
	write(main, "plans/verify/topic.json", json.dumps(spec))


@pytest.fixture
def toolsHome(tmp_path):
	"""A home whose ~/.claude/hooks and skills are the files under test (auto-finish calls cost-history)."""
	h = tmp_path / "home"
	(h / ".claude").mkdir(parents=True)
	os.symlink(RUN_HOOKS, h / ".claude" / "hooks")
	os.symlink(RUN_SKILLS, h / ".claude" / "skills")
	return h

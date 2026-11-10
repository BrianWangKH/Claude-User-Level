"""Run record migration step 2, dual write: every program that writes one of today's runs/ files also writes the
matching record fields (worker-control launch, resume, queue, stop, auto, auto-merge, auto-fix, auto-start and
wt merge; dispatch-guard; verify.py; verifier.py; auto-merge.py; wt_controls.releaseApproval), a record write
that fails leaves the old path as it was and is told once, and compareStories / reportDivergence report a
difference between the two stories once per run. Writers and readers share these fixtures (hooks/RULES.md T9).
run-worker.sh's part is in test_run_worker.py.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_dual_write.py -q -p no:cacheprovider'
"""

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import eventlog  # noqa: E402
import runstate  # noqa: E402
import wt_controls  # noqa: E402

TOPIC = "t"
SESSION = "11111111-2222-3333-4444-555555555555"
WAIT_SECONDS = 8


def load(name):
	spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), homepath.locate(name))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def git(*args):
	return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def waitFor(check, seconds=WAIT_SECONDS):
	deadline = time.time() + seconds
	while time.time() < deadline:
		if check():
			return True
		time.sleep(0.05)
	return check()


@pytest.fixture
def project(tmp_path):
	"""(home, main checkout, worktree, args file): a fake run-worker.sh that records its arguments and then
	sleeps (a live wrapper for `wt stop`), an estimate for the topic. The arguments go to a temporary file renamed
	into place, so a reader never sees a half-written list."""
	home = tmp_path / "home"
	hooks = home / ".claude" / "hooks" / "worker"
	hooks.mkdir(parents=True)
	argsFile = tmp_path / "args.txt"
	(hooks / "run-worker.sh").write_text(
		f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > {argsFile}.part\nmv {argsFile}.part {argsFile}\n'
		'sleep "${FAKE_WRAPPER_SECONDS:-0}"\n'
	)
	(hooks / "run-worker.sh").chmod(0o755)
	main = tmp_path / "proj" / "main"
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / f"{TOPIC}.json").write_text(json.dumps({"minutes": 20, "est_usd": 2, "raw_usd": 1.5}))
	worktree = tmp_path / "proj" / "worktrees" / TOPIC
	# a real worktree: homeconfig.flowMode finds the topic's record through git
	git("init", "-q", str(main))
	git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	git("-C", str(main), "worktree", "add", "-q", "-b", f"wt-{TOPIC}", str(worktree))
	return home, main, worktree, argsFile


def env(home, **extra):
	out = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	return {**out, "HOME": str(home), **extra}


def wt(project, prompt, **extra):
	home, main, _, argsFile = project
	if argsFile.exists():
		argsFile.unlink()
	payload = {"prompt": prompt, "cwd": str(main), **extra}
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps(payload),
		capture_output=True,
		text=True,
		timeout=30,
		env=env(home, **{k: v for k, v in os.environ.items() if k == "FAKE_WRAPPER_SECONDS"}),
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout), result.stderr


def args(argsFile):
	assert waitFor(lambda: argsFile.exists() and argsFile.read_text()), "run-worker.sh was not started"
	return argsFile.read_text().splitlines()


def record(worktree, run=1):
	found, problem = runstate.readRecord(str(worktree), run)
	assert found is not None, problem
	return found


def runs(worktree):
	return worktree / ".hook-only" / "runs"


def withSession(main, **extra):
	state = main / ".hook-only" / "workers"
	state.mkdir(parents=True, exist_ok=True)
	(state / f"{TOPIC}.json").write_text(json.dumps({"pid": 2**22 + 7, "session_id": SESSION, "run": 1, **extra}))


# --- worker-control: est-<run>.json and the record --------------------------------------------------


def test_launch_writes_the_estimate_and_the_record(project):
	_, main, worktree, argsFile = project
	wt(project, f"wt launch {TOPIC}")
	assert args(argsFile)[2] == "1"
	est = json.loads((runs(worktree) / "est-1.json").read_text())
	found = record(worktree)
	assert found["topic"] == TOPIC and found["run"] == 1 and found["session_mode"] == "new"
	assert found["started_by"] == "person" and found["purpose"] == "build" and found["prompt_kind"] == "feature-flow"
	assert found["flow"] == "written" and found["integration_mode"] == "origin"
	assert found["estimate"] == {
		"minutes": est["minutes"],
		"usd": est["usd"],
		"cap_minutes": est["cap_minutes"],
		"cap_usd": est["cap"],
		"raw_usd": 1.5,
	}
	assert (
		found["session_id"] == json.loads((main / ".hook-only" / "workers" / f"{TOPIC}.json").read_text())["session_id"]
	)
	assert clock.isStamp(found["launch"]["launched_at"]) and isinstance(found["launch"]["by_pid"], int)


def test_a_queue_start_is_recorded_as_started_by_the_queue(project, monkeypatch):
	home, main, worktree, argsFile = project
	monkeypatch.setenv("HOME", str(home))
	queue = load("wt_queue.py")
	queue.WORKER_CONTROL = Path(RUN_HOOKS) / "worker" / "worker-control.py"  # not the tree's: the person's files
	assert queue.launch(main, TOPIC).startswith(f"wt: launch {TOPIC}")
	args(argsFile)
	assert record(worktree)["started_by"] == "queue"


def test_a_merge_mode_resume_is_recorded_as_a_merge(project):
	_, main, worktree, argsFile = project
	withSession(main, mode="merge")
	wt(project, f"wt resume {TOPIC}")
	assert args(argsFile)[3] == "resume"
	found = record(worktree)
	assert found["purpose"] == "merge" and found["prompt_kind"] == "merge" and found["session_mode"] == "resume"
	assert json.loads((runs(worktree) / "est-1.json").read_text())["mode"] == "merge"


def test_a_failed_record_write_still_launches_and_is_told_once(project):
	"""The record's lock can't be taken (its path is a folder), so every record write of the launch fails; the
	record itself is absent, not unreadable (an unreadable record is an unknown state, which refuses a launch)."""
	_, _, worktree, argsFile = project
	(runs(worktree) / "1.json.lock").mkdir(parents=True)
	_, stderr = wt(project, f"wt launch {TOPIC}")
	assert args(argsFile)[2] == "1" and (runs(worktree) / "est-1.json").exists()
	assert stderr.count("the run record was not updated") == 1
	failed = eventlog.read(str(worktree), event="hook-failed", source="worker-control")
	assert len(failed) == 1 and failed[0]["data"]["outcome"] == "skipped"


def test_a_wrapper_that_cannot_start_ends_the_record_launch_failed_and_fails_as_before(project):
	home, main, worktree, _ = project
	(home / ".claude" / "hooks" / "worker" / "run-worker.sh").chmod(0o644)  # Popen: PermissionError
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": f"wt launch {TOPIC}", "cwd": str(main)}),
		capture_output=True,
		text=True,
		timeout=30,
		env=env(home),
	)
	assert result.returncode != 0 and "PermissionError" in result.stderr  # the old behaviour: the error goes on
	assert (runs(worktree) / "est-1.json").exists() and not (runs(worktree) / "exit-1").exists()
	assert not (main / ".hook-only" / "workers" / f"{TOPIC}.json").exists()
	end = record(worktree)["exit"]
	assert end["reason"] == "launch-failed" and end["code"] is None and "PermissionError" in end["error"]
	assert clock.isStamp(end["ended_at"])
	ended = eventlog.read(str(worktree), event="run-ended")
	assert [e["data"] for e in ended] == [{"reason": "launch-failed", "code": None}]


def test_wt_stop_writes_the_marker_and_the_record_stop(project, monkeypatch):
	_, main, worktree, argsFile = project
	monkeypatch.setenv("FAKE_WRAPPER_SECONDS", "5")
	wt(project, f"wt launch {TOPIC}")
	args(argsFile)
	out, _ = wt(project, f"wt stop {TOPIC}")
	assert "stopped t run 1" in out["hookSpecificOutput"]["additionalContext"]
	assert (runs(worktree) / "stopped-1").exists()
	stop = record(worktree)["stop"]
	assert stop["by"] == "person" and clock.isStamp(stop["requested_at"])


@pytest.fixture
def blocked(project):
	"""Run 1 recorded and ended `STATUS: blocked` after a dispatch-guard denial (auto-resume-1.json)."""
	_, main, worktree, _ = project
	runs(worktree).mkdir(parents=True)
	runstate.launchRecord(
		str(worktree),
		1,
		{
			"topic": TOPIC,
			"started_by": "person",
			"purpose": "build",
			"flow": "written",
			"launch": {"launched_at": clock.isoNow()},
		},
	)
	(runs(worktree) / "est-1.json").write_text(json.dumps({"minutes": 20, "usd": 2}))
	(runs(worktree) / "start-1").write_text("2026-11-01T10:00:00+08:00\n")
	runstate.update(str(worktree), 1, lambda r: r.update(process={"started_at": "2026-11-01T10:00:00+08:00"}))
	(runs(worktree) / "result-1.json").write_text(json.dumps({"result": "STATUS: blocked\nno room for qa"}))
	runstate.recordClaudeEnded(str(worktree), 1, 0, False)  # as run-worker.sh writes both (step 4 reads the record)
	withSession(main)
	return project


def auto(project, command):
	home, main, worktree, _ = project
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py"), command, str(worktree), TOPIC, "1"],
		capture_output=True,
		text=True,
		timeout=30,
		env=env(home, WT_MAIN_DIR=str(main)),
	)
	assert result.returncode == 0, result.stderr
	return json.loads((runs(worktree) / "auto-1.json").read_text())


@pytest.mark.parametrize("command", ["auto", "auto-merge", "auto-fix"])
def test_a_refused_automatic_run_writes_the_decision_and_the_next_step(blocked, command):
	worktree = blocked[2]
	decision = auto(blocked, command)
	assert decision["decision"] == "refused"
	step = record(worktree)["steps"]["next"]
	assert step["state"] == "done" and step["outcome"] == "refused" and step["reason"] == decision["reason"]
	assert step["detail"] == "auto-1.json"


def test_an_automatic_resume_writes_the_next_step_and_the_next_runs_record(blocked):
	_, _, worktree, argsFile = blocked
	(runs(worktree) / "auto-resume-1.json").write_text(json.dumps({"minutes": 11, "usd": 1.9}))
	runstate.recordDispatchStop(str(worktree), 1, 11, 1.9)  # as dispatch-guard writes both
	decision = auto(blocked, "auto")
	assert decision["decision"] == "resume"
	assert record(worktree)["steps"]["next"]["outcome"] == "resume"
	assert args(argsFile)[2] == "2"
	assert waitFor(lambda: runstate.recordExists(str(worktree), 2))
	second = record(worktree, 2)
	assert second["started_by"] == "auto-resume" and second["launch"]["by_run"] == 1
	assert second["prompt_kind"] == "auto" and second["session_mode"] == "resume"
	assert second["estimate"]["usd"] == json.loads((runs(worktree) / "est-2.json").read_text())["usd"]


# --- migration step 5: an automatic run starts only when the record agrees with today's file ----------------


def events(main):
	path = main / ".hook-only" / "events.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("recorded", [None, {"minutes": 11, "usd": 9.9}])
def test_an_automatic_resume_needs_the_record_s_dispatch_stop_to_match_the_file(blocked, recorded):
	_, _, worktree, argsFile = blocked
	(runs(worktree) / "auto-resume-1.json").write_text(json.dumps({"minutes": 11, "usd": 1.9}))
	if recorded:
		runstate.recordDispatchStop(str(worktree), 1, recorded["minutes"], recorded["usd"])
	decision = auto(blocked, "auto")
	assert decision["decision"] == "refused" and "dispatch stop" in decision["reason"], decision
	assert not argsFile.exists()


def test_an_automatic_resume_of_a_run_without_a_record_is_refused(blocked):
	_, _, worktree, argsFile = blocked
	(runs(worktree) / "auto-resume-1.json").write_text(json.dumps({"minutes": 11, "usd": 1.9}))
	os.remove(runstate.statefile.pathOf(str(worktree), runstate.RECORD, 1))
	decision = auto(blocked, "auto")
	assert decision["decision"] == "refused" and "no usable run record" in decision["reason"], decision
	assert not argsFile.exists()


@pytest.mark.parametrize("command, flag", [("auto-merge", "merge_run"), ("auto-fix", "fix_run")])
def test_a_merge_or_fix_run_needs_the_record_s_merge_step_to_ask_for_it(blocked, command, flag):
	_, _, worktree, argsFile = blocked
	(runs(worktree) / "merge-1.json").write_text(json.dumps({"decision": "refused", "reason": "x"}))
	runstate.recordStepFromFile(str(worktree), 1, "merge")  # the record's step without the flag
	(runs(worktree) / "merge-1.json").write_text(json.dumps({"decision": "refused", "reason": "x", flag: True}))
	decision = auto(blocked, command)
	assert decision["decision"] == "refused" and "merge step didn't ask" in decision["reason"], decision
	assert not argsFile.exists()


def test_auto_start_starts_nothing_the_record_s_next_step_does_not_hold(blocked):
	home, main, worktree, argsFile = blocked
	(runs(worktree) / "auto-1.json").write_text(json.dumps({"run": 1, "decision": "refused", "reason": "x"}))
	runstate.recordStepFromFile(str(worktree), 1, "next")
	(runs(worktree) / "auto-1.json").write_text(
		json.dumps({"run": 1, "decision": "resume", "next_run": 2, "minutes": 5, "usd": 1.0})
	)
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py"), "auto-start", str(worktree), TOPIC, "1"],
		capture_output=True,
		text=True,
		timeout=30,
		env=env(home, WT_MAIN_DIR=str(main)),
	)
	assert result.returncode == 0, result.stderr
	assert not argsFile.exists() and not runstate.recordExists(str(worktree), 2)
	told = events(main)[-1]
	assert told["kind"] == "refused" and "next step is 'refused'" in told["text"], told


# --- dispatch-guard, verify.py, verifier.py, auto-merge.py, wt merge ---------------------------------


@pytest.fixture
def recorded(tmp_path):
	"""A worktree whose run 1 has a record and a start file."""
	worktree = tmp_path / "w"
	runs(worktree).mkdir(parents=True)
	(runs(worktree) / "start-1").write_text(clock.isoNow() + "\n")
	runstate.launchRecord(
		str(worktree),
		1,
		{
			"topic": TOPIC,
			"started_by": "person",
			"purpose": "build",
			"flow": "written",
			"launch": {"launched_at": clock.isoNow()},
		},
	)
	return worktree


def test_dispatch_guard_writes_and_clears_the_dispatch_stop(recorded):
	guard = load("dispatch_guard.py")
	config = runstate.homeconfig.load("budget")
	guard.recordAutoResume(str(recorded), {"minutes": 5, "usd": 1.0}, 0, config, "qa")
	written = json.loads((runs(recorded) / "auto-resume-1.json").read_text())
	stop = record(recorded)["dispatch_stop"]
	assert stop["minutes"] == written["minutes"] and stop["usd"] == written["usd"] and clock.isStamp(stop["written_at"])
	guard.clearAutoResume(str(recorded))
	assert not (runs(recorded) / "auto-resume-1.json").exists() and record(recorded)["dispatch_stop"] is None


def test_verify_writes_its_result_and_the_verify_step(recorded):
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "gates", "verify.py"), str(recorded), TOPIC, "1"],
		capture_output=True,
		text=True,
		timeout=30,
		env={k: v for k, v in os.environ.items() if k != "WT_MAIN_DIR"},
	)
	assert result.returncode == 0, result.stderr
	written = json.loads((runs(recorded) / "verify-1.json").read_text())
	assert written["passed"] is False and "WT_MAIN_DIR" in written["error"]
	step = record(recorded)["steps"]["verify"]
	assert step["state"] == "done" and step["outcome"] == "ERROR" and step["detail"] == "verify-1.json"


@pytest.mark.parametrize("verdict, outcome", [("passed", "PASS"), ("failed", "FAIL"), ("error", "ERROR")])
def test_verifier_writes_its_result_and_the_verifier_step(recorded, tmp_path, monkeypatch, verdict, outcome):
	verifier = load("verifier.py")
	given = {"verdict": verdict, "report": "V1 fail x", "why": "w", "session_id": SESSION}
	monkeypatch.setattr(verifier, "check", lambda *a: given)
	monkeypatch.setattr(verifier.wt_events, "emit", lambda *a: None)
	monkeypatch.setenv("WT_MAIN_DIR", str(tmp_path))
	monkeypatch.setattr(sys, "argv", ["verifier.py", str(recorded), TOPIC, "1"])
	verifier.main()
	assert json.loads((runs(recorded) / "verifier-1.json").read_text())["verdict"] == verdict
	step = record(recorded)["steps"]["verifier"]
	assert step["outcome"] == outcome and step["detail"] == "verifier-1.json"


def test_auto_merge_writes_its_decision_and_the_merge_step(recorded):
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-merge.py"), str(recorded), TOPIC, "1"],
		capture_output=True,
		text=True,
		timeout=30,
		env={k: v for k, v in os.environ.items() if k != "WT_MAIN_DIR"},
	)
	assert result.returncode == 0, result.stderr
	decision = json.loads((runs(recorded) / "merge-1.json").read_text())
	step = record(recorded)["steps"]["merge"]
	assert decision["decision"] == "refused" and step["outcome"] == "refused" and step["reason"] == decision["reason"]
	assert step["flags"] == {"merge_run": False, "fix_run": False} and step["detail"] == "merge-1.json"


@pytest.mark.parametrize(
	"decision, outcome",
	[
		({"decision": "merged", "into": "main", "commit": "abc", "review": "already-merged"}, "already-merged"),
		({"decision": "merged", "into": "main", "commit": "abc", "review": "code-reviewer"}, "merged"),
		({"decision": "refused", "reason": "behind", "merge_run": True}, "refused"),
	],
)
def test_auto_merge_records_each_decision_as_the_legacy_adapter_reads_it(recorded, decision, outcome):
	(runs(recorded) / "merge-1.json").write_text(json.dumps({"run": 1, **decision}))
	load("auto-merge.py").recordDecision(str(recorded), 1)
	step = record(recorded)["steps"]["merge"]
	legacy = runstate.legacyMergeStep(str(recorded), 1)
	assert step["outcome"] == legacy["outcome"] == outcome and step["flags"] == legacy["flags"]
	assert step.get("commit") == legacy.get("commit") and step.get("reason") == legacy.get("reason")


@contextlib.contextmanager
def autoMergeLocked(main):
	"""Hold auto-merge.py's lock: the approval it starts waits on it, alive, until the block ends."""
	lockDir = main / ".hook-only" / "auto-merge"
	lockDir.mkdir(parents=True, exist_ok=True)
	with open(lockDir / "lock", "w") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		yield


def test_wt_merge_records_the_approval_and_its_release_clears_it(project):
	home, main, worktree, _ = project
	runs(worktree).mkdir(parents=True)
	runstate.launchRecord(
		str(worktree),
		1,
		{
			"topic": TOPIC,
			"started_by": "person",
			"purpose": "build",
			"flow": "written",
			"launch": {"launched_at": clock.isoNow()},
		},
	)
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	(runs(worktree) / "merge-1.json").write_text(json.dumps({"decision": "refused", "reason": "a person must look"}))
	runstate.recordStepFromFile(str(worktree), 1, "merge")  # as auto-merge.py writes both
	(runs(worktree) / "exit-1").write_text("0 2026-11-01T10:00:00+08:00\n")
	runstate.recordRunEnded(str(worktree), 1, "completed", 0)  # as run-worker.sh writes both (step 4 reads it)
	withSession(main)
	with autoMergeLocked(main):
		out, _ = wt(project, f"wt merge {TOPIC}")
		assert "approving t run 1" in out["reason"]
		approval = json.loads((runs(worktree) / "approve-1.json").read_text())
		approve = record(worktree)["approve"]
		assert approve["pid"] == approval["pid"] and clock.isStamp(approve["started_at"])
		os.kill(approval["pid"], signal.SIGKILL)  # the approval dies without releasing (its pid is gone)
		assert waitFor(lambda: not os.path.exists(f"/proc/{approval['pid']}"))
	wt_controls.releaseApproval(worktree, 1, os.getpid())
	assert not (runs(worktree) / "approve-1.json").exists() and record(worktree)["approve"] is None


def test_a_run_without_a_record_is_skipped_quietly(tmp_path):
	worktree = tmp_path / "w"
	runs(worktree).mkdir(parents=True)
	assert runstate.dualWrite(str(worktree), "verify", runstate.recordStep, 1, "verify", {"outcome": "PASS"}) is None
	assert eventlog.read(str(worktree), event="hook-failed") == [] and not runstate.recordExists(str(worktree), 1)


# --- the two stories: compareStories, reportDivergence, run-status.py --compare --------------------


@pytest.fixture
def story(tmp_path, monkeypatch):
	"""A main checkout and the topic's worktree whose run 1 went launch -> start -> claude `STATUS: done` -> exit,
	each step written the way its writer writes it: today's file, then the record."""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	main = tmp_path / "main"
	git("init", "-q", "-b", "main", str(main))
	git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	(main / "plans").mkdir()
	(main / "plans" / "repo-plan.md").write_text("plan\n")
	worktree = tmp_path / "worktrees" / TOPIC
	git("-C", str(main), "worktree", "add", "-q", "-b", f"feature/x/{TOPIC}", str(worktree))
	runs(worktree).mkdir(parents=True)
	(worktree / ".hook-only" / "flow-mode").write_text("origin\n")
	root = str(worktree)
	control = load("worker-control.py")
	control.WRAPPER = tmp_path / "no-wrapper.sh"  # the launch as worker-control writes it; the wrapper's part follows
	control.WRAPPER.write_text("#!/usr/bin/env bash\nexit 0\n")
	control.WRAPPER.chmod(0o755)
	control.start_run(
		main,
		worktree,
		TOPIC,
		tmp_path / "state.json",
		"new",
		SESSION,
		"/feature-flow t",
		20,
		2,
		3,
		26,
		"",
		{},
		{},
		{"started_by": "person", "prompt_kind": "feature-flow"},
	)
	(runs(worktree) / "start-1").write_text(clock.isoNow() + "\n")
	runstate.recordStarted(root, 1, os.getpid())
	(runs(worktree) / "result-1.json").write_text(json.dumps({"result": "STATUS: done\nok", "total_cost_usd": 1.0}))
	runstate.recordInvocation(root, 1, "main", "result-1.json", clock.isoNow(), 0, 0)
	runstate.recordClaudeEnded(root, 1, 0, False)
	(runs(worktree) / "exit-1").write_text(f"0 {clock.isoNow()}\n")
	runstate.recordWrapperExit(root, 1, 0, None, "completed", 0, str(main))
	return main, worktree


def test_equal_stories_report_nothing(story):
	main, worktree = story
	assert runstate.compareStories(str(main), TOPIC, 1) == []
	assert runstate.reportDivergence(str(worktree), TOPIC, 1, []) == []
	assert eventlog.read(str(worktree), event="status-diverged") == []


def test_a_divergence_is_reported_once_however_often_compare_runs(story):
	main, worktree = story
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")  # today's files now tell another story
	differences = runstate.compareStories(str(main), TOPIC, 1)
	assert differences == [{"field": "integration_mode", "record": "origin", "legacy": "local"}]
	assert runstate.reportDivergence(str(worktree), TOPIC, 1, differences) == differences
	again = runstate.compareStories(str(main), TOPIC, 1)
	assert runstate.reportDivergence(str(worktree), TOPIC, 1, again) == []
	events = eventlog.read(str(worktree), event="status-diverged")
	assert len(events) == 1 and events[0]["run"] == 1 and events[0]["topic"] == TOPIC
	assert events[0]["data"] == {"field": "integration_mode", "record": "origin", "legacy": "local"}


def test_run_status_compare_prints_the_differences_and_writes_nothing(story):
	main, worktree = story
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	cli = load("run-status.py")
	out = io.StringIO()
	assert cli.main(["--topic", TOPIC, "--main", str(main), "--compare"], out=out) == 0
	assert out.getvalue().strip() == "integration_mode: record 'origin', legacy 'local'"
	assert eventlog.read(str(worktree), event="status-diverged") == []
	(worktree / ".hook-only" / "flow-mode").write_text("origin\n")
	out = io.StringIO()
	cli.main(["--topic", TOPIC, "--main", str(main), "--compare", "--json"], out=out)
	assert json.loads(out.getvalue()) == []


def test_a_run_without_a_record_is_one_difference(story):
	main, worktree = story
	os.rename(runs(worktree) / "1.json", runs(worktree) / "gone.json")
	assert runstate.compareStories(str(main), TOPIC, 1) == [{"field": "record", "record": None, "legacy": "legacy"}]


def test_the_wrapper_exit_without_a_reason_leaves_the_run_an_orphan(story):
	main, worktree = story
	runstate.launchRecord(
		str(worktree),
		2,
		{
			"topic": TOPIC,
			"started_by": "person",
			"purpose": "build",
			"flow": "written",
			"launch": {"launched_at": clock.isoNow()},
		},
	)
	runstate.recordStarted(str(worktree), 2, os.getpid())
	runstate.recordWrapperExit(str(worktree), 2, 143, "TERM", None, None, str(main))
	found = record(worktree, 2)
	assert not found.get("exit")
	assert found["process"]["exit_code"] == 143 and found["process"]["exit_signal"] == "TERM"
	assert runstate.status(str(main), TOPIC, run=2)["phase"] == "orphan"

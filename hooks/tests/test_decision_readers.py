"""Run record migration step 4: every decision about a run reads hooks/lib/runstate.py (status, activeSince, current,
leftOfEstimate), the run record where one exists, else today's runs/ files through the legacy adapter. For every
phase but an orphan, a run written as a record and the same run as today's files only give the same decision,
and the one the old file rules gave (frozen in legacy_rules.py and compared here); the new
behaviours are pinned: `wt resume` closes a confirmed orphan and resumes, refuses one it can't check (Q3);
`wt stop` signals only a wrapper verifyProcess confirms (design T-5); an orphan explains no branch move after
its last sign of life (T-6); a suspected orphan keeps the queue busy, a confirmed one doesn't (T-7, in
test_wt_queue.py); ALERT holds like HOLD (Q6). Fixtures: run_fixtures.py (shared with the display readers).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_decision_readers.py -q -p no:cacheprovider'
"""

import io
import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import eventlog  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
import wt_controls  # noqa: E402
import wt_queue  # noqa: E402
import legacy_rules  # noqa: E402
from run_fixtures import (  # noqa: E402
	CASES,
	GONE_PID,
	INTERVAL,
	SESSION,
	START,
	THRESHOLD,
	TOPIC,
	FakeClock,
	addTopic,
	build,
	legacyFile,
	load,
	makeMain,
	workerState,
)

NON_ORPHAN = [(source, phase) for source, phase in CASES if not phase.startswith("orphan")]
SOURCES = ("record", "legacy")
# The row each phase gives (wt_controls.control): run 1 needs-decision when ended, an estimate, a session from
# launching on for a record (worker-control writes the state at the launch) and from running on for today's files.
ROWS = {
	"none": {"action": "launch"},
	"launching": {"why": "run 1 is starting"},
	"running": {"action": "stop"},
	"finishing": {"action": "stop"},
	"ended": {"action": "resume"},
	"unknown": {"why": "run 1's state can't be read"},
}
BUSY = {"none": False, "launching": True, "running": True, "finishing": True, "ended": False, "unknown": True}


@pytest.fixture
def fake():
	source = FakeClock(START)
	clock.useSource(source.now)
	yield source
	clock.useSource(None)


@pytest.fixture
def main(tmp_path, monkeypatch):
	checkout = makeMain(tmp_path, monkeypatch)
	hooks = tmp_path / "home" / ".claude" / "hooks" / "worker"
	hooks.mkdir(parents=True)
	(hooks / "run-worker.sh").write_text("#!/usr/bin/env bash\nexit 0\n")  # worker-control's wrapper: a stub
	(hooks / "run-worker.sh").chmod(0o755)
	return checkout


def oldAction(main, topic=TOPIC):
	"""The action the file rules before migration step 4 gave (legacy_rules.WT_CONTROLS: isRunning,
	doneWithoutNewEstimate)."""
	old = legacy_rules.wtControls(wt_controls)
	state = wt_controls.loadJson(main / ".hook-only" / "workers" / f"{topic}.json") or {}
	estimate = main / "plans" / "worker" / f"{topic}.json"
	worktree = wt_controls.worktreeOf(main, topic)
	run = state.get("run")
	if old["isRunning"](worktree, run):
		return "stop"
	if state.get("session_id") and old["doneWithoutNewEstimate"](worktree, run, estimate):
		return None
	return "resume" if state.get("session_id") else "launch"


def rowDecision(row):
	return {k: row[k] for k in ("action", "why") if k in row}


def verified(answer):
	"""runstate.verifyProcess made deterministic for worker-control's pid check."""
	return lambda process, topic, run: answer


def wt(main, monkeypatch, capsys, prompt, pid=None):
	"""worker-control.py's answer to a typed prompt, its pid check answering pid (True, False or None); the
	reconciler's trigger (test_reconciler.py) off, so the only program started is the command's own."""
	monkeypatch.setattr(runstate, "verifyProcess", verified(pid))
	import reconciler

	monkeypatch.setattr(reconciler, "triggerFrom", lambda cwd: False)
	module = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control")
	monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "cwd": str(main)})))
	with pytest.raises(SystemExit) as ended:
		module.main()
	assert ended.value.code == 0
	out = json.loads(capsys.readouterr().out)
	return out.get("reason") or out["hookSpecificOutput"]["additionalContext"]


def runs(worktree):
	return worktree / ".hook-only" / "runs"


def record(worktree, run=1):
	found, problem = runstate.readRecord(str(worktree), run)
	assert found is not None, problem
	return found


# -- wt_controls: the pane's row -------------------------------------------------------------------------


@pytest.mark.parametrize("source, phase", NON_ORPHAN)
def test_a_record_and_today_s_files_give_the_same_row(main, fake, source, phase):
	build(main, fake, source, phase)
	row = wt_controls.control(main, TOPIC)
	assert rowDecision(row) == ROWS[phase] and row["phase"] == phase


@pytest.mark.parametrize("phase", ["none", "running", "finishing", "ended"])
def test_today_s_files_give_the_row_the_old_file_rules_gave(main, fake, phase):
	"""Regression: for today's files the status-based row is the old rules' row (launching changed: no button
	while the wrapper starts; unknown can't come from today's files)."""
	build(main, fake, "legacy", phase)
	assert wt_controls.control(main, TOPIC).get("action") == oldAction(main)


def test_done_without_a_newer_estimate_offers_nothing_from_either_source(main, fake):
	for source in SOURCES:
		topic = f"{source}-t"
		worktree = addTopic(main, topic)
		estimate = main / "plans" / "worker" / f"{topic}.json"
		estimate.write_text(json.dumps({"minutes": 20, "est_usd": 2}))
		old = (START - timedelta(hours=1)).timestamp()
		os.utime(estimate, (old, old))
		workerState(main, topic)
		doneRun(worktree, fake, source, topic)
		row = wt_controls.control(main, topic)
		assert "action" not in row and "STATUS done" in row["why"] and row["auto"]["ok"] is False, (source, row)
		later = (START + timedelta(minutes=5)).timestamp()
		os.utime(estimate, (later, later))
		assert wt_controls.control(main, topic)["action"] == "resume"


def doneRun(worktree, fake, source, topic, startedBy="person"):
	"""Run 1 ended `STATUS: done`, launched at START (a record, or today's files)."""
	now = fake.now()
	if source == "legacy":
		legacyFile(worktree, "est-1.json", {"minutes": 20, "usd": 2.0, "cap_minutes": 26}, 0, now)
		legacyFile(worktree, "start-1", now.isoformat(timespec="seconds") + "\n", 0, now)
		legacyFile(worktree, "result-1.json", {"result": "STATUS: done\nok"}, 0, now)
		legacyFile(worktree, "exit-1", f"0 {now.isoformat(timespec='seconds')}\n", 0, now)
		return
	root = str(worktree)
	run = launchRecord(root, topic, started_by=startedBy)
	runstate.update(root, run, lambda r: r.update(process={"started_at": clock.isoNow()}))
	claude = {"ended_at": clock.isoNow(), "outcome": "status", "status": "done"}
	runstate.update(root, run, lambda r: r.update(claude=claude))
	runstate.recordRunEnded(root, run, "completed", 0)


def launchRecord(root, topic=TOPIC, **fields):
	body = {
		"topic": topic,
		"started_by": "person",
		"purpose": "build",
		"flow": "written",
		"integration_mode": "local",
		"prompt_kind": "feature-flow",
		"launch": {"launched_at": clock.isoNow(), "by_pid": 1},
		"estimate": {"minutes": 20, "usd": 2.0, "cap_minutes": 26, "cap_usd": 2.6},
		**fields,
	}
	return runstate.create(root, body)


def test_a_run_the_queue_started_counts_as_the_person_s_for_done(main, fake):
	"""A queue start is the person's (they queued it): its done run needs a new estimate like a typed launch."""
	worktree = addTopic(main)
	estimate = main / "plans" / "worker" / f"{TOPIC}.json"
	old = (START - timedelta(hours=1)).timestamp()
	os.utime(estimate, (old, old))
	workerState(main)
	doneRun(worktree, fake, "record", TOPIC, startedBy="queue")
	assert runstate.status(str(main), TOPIC)["resumable"]["code"] == "done"


@pytest.mark.parametrize("guard", ["ALERT", "HOLD"])
def test_an_alert_holds_like_a_hold(main, fake, guard):
	"""Q6: an agent-guard ALERT the person hasn't decided keeps auto mode off the topic, as a HOLD does."""
	worktree = build(main, fake, "record", "ended")
	folder = worktree / ".hook-only" / "agent-guard"
	folder.mkdir(parents=True)
	(folder / guard).write_text("x")
	row = wt_controls.control(main, TOPIC)
	assert row["held"] is True and row["action"] == "resume"
	assert row["auto"] == {"ok": False, "why": wt_controls.HOLD_WHY}


@pytest.mark.parametrize("source", SOURCES)
def test_a_refused_merge_and_its_approval_read_the_same_from_either_source(main, fake, source):
	worktree = build(main, fake, source, "ended")
	legacyFile(worktree, "merge-1.json", {"decision": "refused", "reason": "a person must look"}, 0, fake.now())
	if source == "record":
		runstate.recordStepFromFile(str(worktree), 1, "merge")
	row = wt_controls.control(main, TOPIC)
	assert row["merge"] == {"reason": "a person must look", "files": []} and "approving" not in row
	fake.advance(60)
	started = clock.isoNow()
	legacyFile(worktree, "approve-1.json", {"run": 1, "pid": 4242, "started": started}, 0, fake.now())
	if source == "record":
		runstate.update(str(worktree), 1, lambda r: r.update(approve={"pid": 4242, "started_at": started}))
	row = wt_controls.control(main, TOPIC)
	assert row["approving"] == {"run": 1, "since": started[11:16]} and "merge" not in row


@pytest.mark.parametrize("confidence", ["suspected", "confirmed"])
def test_an_orphan_row_offers_resume_to_the_person_never_to_auto(main, fake, confidence):
	build(main, fake, "record", f"orphan-{confidence}")
	row = wt_controls.control(main, TOPIC)
	assert row["action"] == "resume" and row["phase"] == "orphan"
	assert row["auto"]["ok"] is False and "orphan" in row["auto"]["why"]


# -- worker-control: launch, resume, stop, merge -----------------------------------------------------------


@pytest.mark.parametrize("source, phase", [(s, p) for s, p in NON_ORPHAN if p in ("launching", "running", "finishing")])
def test_nothing_starts_over_a_run_that_is_going(main, fake, monkeypatch, capsys, source, phase):
	build(main, fake, source, phase)
	workerState(main)
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=None)
	assert answer == f"wt: {TOPIC} is already running (run 1, {phase})"
	assert not (runs(main.parent / "worktrees" / TOPIC) / "est-2.json").exists()


def test_an_unreadable_record_starts_nothing(main, fake, monkeypatch, capsys):
	worktree = build(main, fake, "record", "unknown")
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}")
	assert "its state can't be read" in answer and not (runs(worktree) / "est-2.json").exists()


@pytest.mark.parametrize("source", SOURCES)
def test_an_ended_run_resumes_from_either_source(main, fake, monkeypatch, capsys, source):
	worktree = build(main, fake, source, "ended")
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}")
	assert answer.startswith(f"wt: resume {TOPIC}") and "run 2" in answer
	assert record(worktree, 2)["started_by"] == "person"


def test_wt_resume_closes_a_confirmed_orphan_then_starts_the_next_run(main, fake, monkeypatch, capsys):
	"""Q3: the pid check finds the wrapper gone: the run is recorded orphan-confirmed (run-orphaned, once), closed
	orphan-closed with its unfinished wrap-up abandoned (never merged), then run 2 starts."""
	worktree = build(main, fake, "record", "orphan-suspected")
	runstate.update(str(worktree), 1, lambda r: r.update(steps={"merge": {"state": "running"}}))
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=False)
	assert answer.startswith(f"wt: resume {TOPIC}") and "run 2" in answer
	closed = record(worktree)
	assert closed["exit"]["reason"] == "orphan-closed" and closed["exit"]["closed_by"] == "worker-control"
	assert closed["orphan"]["by"] == "worker-control" and closed["orphan"]["evidence"]["checked"] == "gone"
	assert closed["steps"]["merge"]["state"] == "abandoned" and closed["steps"]["merge"].get("outcome") is None
	assert [e["data"]["orphan_of"] for e in eventlog.read(str(worktree), event="run-orphaned")] == ["running"]
	assert [e["data"]["reason"] for e in eventlog.read(str(worktree), event="run-ended")] == ["orphan-closed"]
	assert record(worktree, 2)["session_mode"] == "resume"
	status = runstate.status(str(main), TOPIC, run=1)
	assert status["phase"] == "ended" and status["merge_allowed"] is False


def test_wt_resume_on_today_s_files_only_starts_the_next_run_which_ends_the_orphan(main, fake, monkeypatch, capsys):
	worktree = build(main, fake, "legacy", "orphan-suspected")
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=False)
	assert "run 2" in answer and not runstate.recordExists(str(worktree), 1)
	assert runstate.status(str(main), TOPIC, run=1)["outcome"]["reason"] == "superseded"


@pytest.mark.parametrize("source", SOURCES)
def test_wt_resume_refuses_an_orphan_it_can_t_check(main, fake, monkeypatch, capsys, source):
	worktree = build(main, fake, source, "orphan-suspected")
	before = sorted(os.listdir(runs(worktree)))
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=None)
	assert f"wt: {TOPIC}: nothing was closed or started" in answer and f"`wt status {TOPIC}`" in answer
	assert "may have died" in answer
	assert sorted(os.listdir(runs(worktree))) == before


def test_wt_resume_refuses_while_the_wrapper_lives_though_its_heartbeat_is_stale(main, fake, monkeypatch, capsys):
	build(main, fake, "record", "orphan-suspected")
	assert (
		wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=True)
		== f"wt: {TOPIC} is already running (run 1, running)"
	)


def test_wt_launch_never_closes_an_orphan(main, fake, monkeypatch, capsys):
	worktree = build(main, fake, "record", "orphan-suspected")
	(main / ".hook-only" / "workers" / f"{TOPIC}.json").unlink()  # no session: only the orphan stops a launch
	answer = wt(main, monkeypatch, capsys, f"wt launch {TOPIC}", pid=False)
	assert "wt resume closes it first, wt launch never does" in answer and not record(worktree).get("exit")


def test_a_done_orphan_without_a_new_estimate_is_neither_closed_nor_resumed(main, fake, monkeypatch, capsys):
	worktree = addTopic(main)
	estimate = main / "plans" / "worker" / f"{TOPIC}.json"
	old = (START - timedelta(hours=1)).timestamp()
	os.utime(estimate, (old, old))
	workerState(main)
	run = launchRecord(str(worktree))
	claude = {"ended_at": clock.isoNow(), "outcome": "status", "status": "done"}
	process = {"started_at": clock.isoNow(), "wrapper_pid": GONE_PID}
	runstate.update(str(worktree), run, lambda r: r.update(process=process, claude=claude))
	fake.advance(THRESHOLD + 60)
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=False)
	assert "nothing to resume" in answer and not record(worktree).get("exit") and not record(worktree).get("orphan")


KEPT_MERGE = {"state": "running", "attempt": 1, "runner_pid": GONE_PID}
REAL_POPEN = subprocess.Popen  # popenCalls replaces it; a test that needs a real process takes this one


def keptOrphan(main, fake):
	"""Run 1's worker ended STATUS done (exit code 0), then its wrapper died during the merge step: an orphan
	whose wrap-up is kept (runstate.wrapupLeft), suspected until a pid check."""
	worktree = build(main, fake, "record", "orphan-suspected")
	claude = {"ended_at": clock.isoNow(), "outcome": "status", "status": "done", "exit_code": 0, "stopped": False}
	runstate.update(str(worktree), 1, lambda r: r.update(claude=claude, steps={"merge": dict(KEPT_MERGE)}))
	return worktree


@pytest.fixture
def popenCalls(monkeypatch):
	"""subprocess.Popen of run-steps.py recorded (argv, keyword arguments) and not started; every other program
	(git, through subprocess.run) starts as usual."""
	calls = []

	class Started:
		pid = 4343

	def record(argv, *args, **kwargs):
		if not any(str(word).endswith("run-steps.py") for word in argv):
			return REAL_POPEN(argv, *args, **kwargs)
		calls.append((argv, kwargs))
		return Started()

	monkeypatch.setattr(subprocess, "Popen", record)
	return calls


def test_wt_resume_on_a_done_orphan_with_its_wrapup_left_closes_and_starts_nothing(main, fake, monkeypatch, capsys):
	"""Dangerous direction: the worker ended STATUS done and its wrap-up never finished: wt resume keeps the steps,
	starts no run beside them, never closes the run as merged, and points to wt wrapup in plain words."""
	worktree = keptOrphan(main, fake)
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=False)
	assert answer.startswith(f"wt: {TOPIC}: nothing resumed: run 1 finished its work (STATUS done), but its wrapper")
	assert f"type `wt wrapup {TOPIC}` (or press the button)" in answer and "run-steps.py" not in answer
	kept = record(worktree)
	assert "exit" not in kept and kept["steps"]["merge"] == KEPT_MERGE
	assert not runstate.recordExists(str(worktree), 2) and not (runs(worktree) / "est-2.json").exists()
	assert eventlog.read(str(worktree), event="run-ended") == []
	status = runstate.status(str(main), TOPIC, run=1)
	assert status["merge_allowed"] is False and status["outcome"] is None


def test_the_pane_row_of_a_kept_wrapup_offers_the_wrapup_button_never_to_auto(main, fake):
	keptOrphan(main, fake)
	row = wt_controls.control(main, TOPIC)
	assert (row["action"], row["phase"], row["auto"]["ok"]) == ("wrapup", "orphan", False)
	assert row["human"]["todo"]["command"] == f"wt wrapup {TOPIC}" and row["human"]["todo"]["button"] == "wrapup"


def test_wt_wrapup_starts_the_kept_wrapup_detached_and_says_what_comes_next(main, fake, monkeypatch, capsys,
		popenCalls):
	worktree = keptOrphan(main, fake)
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	started = f"wt: wrapup: started the wrap-up of {TOPIC} run 1 in the background (verify, verifier, merge, next, "
	assert answer.startswith(started)
	assert f"approve the merge with `wt merge {TOPIC}`" in answer and "run-steps.py" not in answer
	[(argv, kwargs)] = popenCalls
	program = os.path.realpath(os.path.join(HOOKS, "worker", "run-steps.py"))
	assert argv[1:] == [program, "wrapup", str(argv[3]), "1", "--topic", TOPIC, "--continue-stopped"]
	assert os.path.realpath(argv[3]) == os.path.realpath(worktree)
	assert kwargs["start_new_session"] is True and kwargs["env"]["WT_MAIN_DIR"] == str(main.resolve())
	assert record(worktree)["orphan"]["by"] == "worker-control" and "exit" not in record(worktree)


def test_wt_wrapup_refuses_a_topic_without_a_kept_wrapup(main, fake, monkeypatch, capsys, popenCalls):
	build(main, fake, "record", "ended")
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	assert answer.startswith(f"wt: wrapup: {TOPIC} has no run with a kept wrap-up: ") and popenCalls == []


def test_wt_wrapup_refuses_while_the_wrapper_may_live(main, fake, monkeypatch, capsys, popenCalls):
	worktree = keptOrphan(main, fake)
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=None)
	assert f"wt: wrapup: {TOPIC} run 1's wrapper could not be confirmed gone" in answer and popenCalls == []
	assert not record(worktree).get("orphan")


def test_wt_wrapup_refuses_while_a_wrapup_of_that_run_runs(main, fake, monkeypatch, capsys, popenCalls):
	keptOrphan(main, fake)
	wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	[(argv, _kwargs)] = popenCalls
	running = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)", *argv[1:]],
		stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	try:
		answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	finally:
		running.kill()
		running.wait()
	assert answer == f"wt: wrapup: {TOPIC} run 1's wrap-up is already running; its result comes as an event"


def test_wt_wrapup_continues_a_run_that_ended_wrapup_failed(main, fake, monkeypatch, capsys, popenCalls):
	"""A run whose wrap-up crashed (exit wrapup-failed) is not an orphan: wt wrapup checks its wrapper gone itself
	(verifyProcess), records no orphan, and starts the kept steps with their output in the run's stderr log."""
	worktree = keptOrphan(main, fake)
	runstate.recordWrapupFailed(str(worktree), 1, 0, "OSError: disk full")
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	assert answer.startswith(f"wt: wrapup: started the wrap-up of {TOPIC} run 1 in the background"), answer
	[(argv, kwargs)] = popenCalls
	assert argv[2:4] == ["wrapup", str(argv[3])] and argv[4] == "1"
	assert kwargs["stderr"].name == str(runs(worktree) / "stderr-1.log") and kwargs["stdout"] is kwargs["stderr"]
	assert not record(worktree).get("orphan") and record(worktree)["exit"]["reason"] == "wrapup-failed"


@pytest.mark.parametrize("pid", [True, None])
def test_wt_wrapup_of_a_wrapup_failed_run_refuses_while_its_wrapper_may_live(main, fake, monkeypatch, capsys,
		popenCalls, pid):
	worktree = keptOrphan(main, fake)
	runstate.recordWrapupFailed(str(worktree), 1, 0, "OSError: disk full")
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=pid)
	assert f"wt: wrapup: {TOPIC} run 1's wrapper could not be confirmed gone" in answer and popenCalls == []


def stoppedInWrapup(main, fake, claudeStopped=False):
	"""Run 1's worker ended STATUS done, then `wt stop` came during its wrap-up: the stop recorded, the steps left
	skipped as stopped, the exit stopped (claude's part stopped too when claudeStopped)."""
	worktree = keptOrphan(main, fake)
	skipped = {"state": "skipped", "attempt": 1, "skip_reason": "stopped"}
	runstate.recordStop(str(worktree), 1, "person")
	runstate.update(str(worktree), 1, lambda r: (r["claude"].update(stopped=claudeStopped), r.update(
		steps={"verify": {"state": "failed", "attempt": 1, "reason": "exited without an outcome (exit 5)"},
			"merge": dict(skipped), "next": dict(skipped)},
		exit={"ended_at": clock.isoNow(), "code": 0, "reason": "stopped"})))
	return worktree


def test_wt_wrapup_continues_a_run_stopped_during_its_wrapup(main, fake, monkeypatch, capsys, popenCalls):
	"""1b: the person continues a wrap-up their stop cut short; wt wrapup checks the wrapper gone itself (the run
	ended), records no orphan, starts the steps with --continue-stopped and names `wt merge` as the approval that
	follows the automatic refusal."""
	worktree = stoppedInWrapup(main, fake)
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	assert answer.startswith(f"wt: wrapup: started the wrap-up of {TOPIC} run 1 in the background (verify, "), answer
	assert "because the run was stopped" in answer and f"approve the merge with `wt merge {TOPIC}`" in answer
	[(argv, _kwargs)] = popenCalls
	assert argv[2] == "wrapup" and argv[4:] == ["1", "--topic", TOPIC, "--continue-stopped"]
	assert not record(worktree).get("orphan") and record(worktree)["exit"]["reason"] == "stopped"


@pytest.mark.parametrize("pid", [True, None])
def test_wt_wrapup_of_a_stopped_run_refuses_while_its_wrapper_may_live(main, fake, monkeypatch, capsys, popenCalls,
		pid):
	stoppedInWrapup(main, fake)
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=pid)
	assert f"wt: wrapup: {TOPIC} run 1's wrapper could not be confirmed gone" in answer and popenCalls == []


def test_a_run_stopped_during_its_wrapup_is_offered_wt_wrapup_never_resumed_or_merged(main, fake, monkeypatch,
		capsys, popenCalls):
	worktree = stoppedInWrapup(main, fake)
	row = wt_controls.control(main, TOPIC)
	assert (row["action"], row["auto"]["ok"]) == ("wrapup", False)
	assert row["human"]["todo"]["command"] == f"wt wrapup {TOPIC}"
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=False)
	assert "but its wrap-up was stopped with steps left" in answer and f"`wt wrapup {TOPIC}`" in answer
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	answer = wt(main, monkeypatch, capsys, f"wt merge {TOPIC}", pid=False)
	assert f"type `wt wrapup {TOPIC}` first" in answer
	assert popenCalls == [] and not runstate.recordExists(str(worktree), 2)


def test_wt_wrapup_leaves_a_run_whose_claude_part_was_stopped_as_before(main, fake, monkeypatch, capsys, popenCalls):
	stoppedInWrapup(main, fake, claudeStopped=True)
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	assert answer.startswith(f"wt: wrapup: {TOPIC} has no run with a kept wrap-up: ") and popenCalls == []


@pytest.mark.parametrize("stopped", [True, False])
def test_wt_merge_names_who_brings_main_into_the_branch(main, fake, monkeypatch, capsys, popenCalls, stopped):
	"""The approval found the branch behind main (merge_run): a stopped run gets no automatic merge run (worker-control
	auto-merge refuses it), so the person is told to merge main in and approve again; otherwise it starts by itself."""
	worktree = stoppedInWrapup(main, fake)
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	final = {name: {"state": "done", "attempt": 2} for name in runstate.STEP_ORDER}
	final["merge"] = {"state": "done", "attempt": 2, "outcome": "refused", "reason": "behind main",
		"flags": {"merge_run": True}}
	runstate.update(str(worktree), 1, lambda r: (r.update(steps=final), None if stopped else r.update(stop=None)))
	answer = wt(main, monkeypatch, capsys, f"wt merge {TOPIC}", pid=False)
	if stopped:
		assert "merge main into the branch yourself, then `wt merge " + TOPIC + "` again" in answer, answer
		assert "starts by itself" not in answer
	else:
		assert answer.endswith("it starts by itself"), answer
	assert popenCalls == []


def test_wt_wrapup_with_every_step_left_at_its_limit_says_so_and_tells_once(main, fake, monkeypatch, capsys,
		popenCalls):
	"""A continued wrap-up left its steps failed at step_attempts_max: wt wrapup starts nothing and says why; the
	person's step-at-limit event is written once per run, however often it is pressed."""
	worktree = stoppedInWrapup(main, fake)
	attempts = runstate.settings()["step_attempts_max"]
	used = {"state": "failed", "attempt": attempts, "reason": "exited without an outcome (exit 1)"}
	runstate.update(str(worktree), 1, lambda r: r.update(steps={name: dict(used) for name in runstate.STEP_ORDER}))
	for _ in range(2):
		answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
		assert answer.startswith(f"wt: wrapup: {TOPIC} run 1: nothing left to run: verify, verifier, merge, next, "
			f"finish, queue_tick used all {attempts} attempt(s) (step_attempts_max)"), answer
	assert popenCalls == []
	told = [e for e in mainEvents(main) if e["kind"] == "step-at-limit"]
	assert len(told) == 1 and "stay owed" in told[0]["text"], told


def test_wt_wrapup_with_a_step_still_runnable_starts_the_wrapup(main, fake, monkeypatch, capsys, popenCalls):
	worktree = stoppedInWrapup(main, fake)
	attempts = runstate.settings()["step_attempts_max"]
	runstate.update(str(worktree), 1, lambda r: r["steps"]["verify"].update(attempt=attempts))
	answer = wt(main, monkeypatch, capsys, f"wt wrapup {TOPIC}", pid=False)
	assert answer.startswith(f"wt: wrapup: started the wrap-up of {TOPIC} run 1"), answer
	assert len(popenCalls) == 1


@pytest.fixture
def lookalike(tmp_path):
	"""Processes that sleep under a given command line, in their own session; each writes <marker> on SIGINT."""
	started = []

	def start(marker, *argv):
		ready = f"{marker}.ready"
		code = ("import signal, sys, time\n"
			f"signal.signal(signal.SIGINT, lambda n, f: (open({str(marker)!r}, 'w').close(), sys.exit(0)))\n"
			f"open({ready!r}, 'w').close()\n"
			"time.sleep(60)\n")
		process = REAL_POPEN([sys.executable, "-c", code, *argv], start_new_session=True)
		started.append(process)
		deadline = time.time() + 10
		while time.time() < deadline and not os.path.exists(ready):  # its handler is in place
			time.sleep(0.02)
		return process

	yield start
	for process in started:
		process.kill()
		process.wait()


def mainEvents(main):
	"""The person's events in the main checkout (wt_events.py)."""
	path = main / ".hook-only" / "events.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ticksOf(pid):
	import reconciler

	return reconciler.startTicks(pid)


def test_wt_stop_stops_a_continued_wrapup_and_nothing_else(main, fake, monkeypatch, capsys, lookalike, tmp_path):
	"""The wrapper is gone, `wt wrapup`'s run-steps.py runs a step: wt stop records the stop and sends SIGINT to that
	runner only (pid, command line naming run-steps.py, the worktree and run, start ticks); a runner pid whose start
	ticks differ, or a process not run-steps.py, gets nothing."""
	worktree = stoppedInWrapup(main, fake)
	runner = lookalike(tmp_path / "runner-int", "run-steps.py", "wrapup", str(worktree), "1", "--topic", TOPIC)
	reused = lookalike(tmp_path / "reused-int", "run-steps.py", "wrapup", str(worktree), "1")
	stranger = lookalike(tmp_path / "stranger-int", "other.py", str(worktree), "1")
	steps = {
		"verify": {"state": "running", "attempt": 2, "runner_pid": runner.pid, "runner_start_ticks": ticksOf(runner.pid)},
		"verifier": {"state": "done", "attempt": 2, "runner_pid": reused.pid,
			"runner_start_ticks": ticksOf(reused.pid) + 1},
		"merge": {"state": "done", "attempt": 2, "runner_pid": stranger.pid,
			"runner_start_ticks": ticksOf(stranger.pid)},
	}
	runstate.update(str(worktree), 1, lambda r: r.update(steps=steps))
	answer = wt(main, monkeypatch, capsys, f"wt stop {TOPIC}", pid=False)
	assert answer.startswith(f"wt: stopped the wrap-up of {TOPIC} run 1"), answer
	assert runner.wait(timeout=10) == 0 and (tmp_path / "runner-int").exists()
	assert reused.poll() is None and stranger.poll() is None
	assert not (tmp_path / "reused-int").exists() and not (tmp_path / "stranger-int").exists()
	assert (runs(worktree) / "stopped-1").exists() and record(worktree)["stop"]["by"] == "person"


def test_wt_stop_signals_a_step_program_whose_runner_is_gone_by_its_group(main, fake, monkeypatch, capsys,
		lookalike, tmp_path):
	worktree = stoppedInWrapup(main, fake)
	program = lookalike(tmp_path / "program-int", "verify.py", str(worktree), TOPIC, "1")
	runstate.update(str(worktree), 1, lambda r: r.update(steps={"verify": {"state": "running", "attempt": 2,
		"runner_pid": GONE_PID, "runner_start_ticks": 1, "program_pid": program.pid,
		"program_start_ticks": ticksOf(program.pid)}}))
	answer = wt(main, monkeypatch, capsys, f"wt stop {TOPIC}", pid=False)
	assert answer.startswith(f"wt: stopped the wrap-up of {TOPIC} run 1 (program {program.pid}"), answer
	assert program.wait(timeout=10) == 0 and (tmp_path / "program-int").exists()


def test_a_program_pid_reused_by_a_foreign_command_is_never_signalled(main, fake, monkeypatch, capsys, lookalike,
		tmp_path):
	"""The step's recorded program pid and start ticks match a live process whose command line is not the step's
	program for this worktree and run (another program, or verify.py of another run): nothing is signalled."""
	worktree = stoppedInWrapup(main, fake)
	foreign = lookalike(tmp_path / "foreign-int", "other.py", str(worktree), TOPIC, "1")
	otherRun = lookalike(tmp_path / "other-run-int", "verify.py", str(worktree), TOPIC, "2")
	runstate.update(str(worktree), 1, lambda r: r.update(steps={
		"verify": {"state": "running", "attempt": 2, "runner_pid": GONE_PID, "runner_start_ticks": 1,
			"program_pid": foreign.pid, "program_start_ticks": ticksOf(foreign.pid)},
		"verifier": {"state": "running", "attempt": 2, "runner_pid": GONE_PID, "runner_start_ticks": 1,
			"program_pid": otherRun.pid, "program_start_ticks": ticksOf(otherRun.pid)}}))
	answer = wt(main, monkeypatch, capsys, f"wt stop {TOPIC}", pid=False)
	assert answer.startswith(f"wt: {TOPIC} has no running worker"), answer
	time.sleep(0.3)
	assert foreign.poll() is None and otherRun.poll() is None
	assert not (tmp_path / "foreign-int").exists() and not (tmp_path / "other-run-int").exists()


def test_a_later_wt_stop_of_a_hand_wrapup_never_turns_a_claude_part_stop_into_a_wrapup_stop(main, fake, monkeypatch,
		capsys, lookalike, tmp_path):
	"""Dangerous direction (merge): a run told without claude.stopped (the record's write of it failed, today's files
	never hold it) was stopped during claude's part; its wrapper died; a hand wrap-up runs; a later `wt stop` of that
	wrap-up keeps the first stop's time in both stories, so the merge gate still holds the stop as a claude-part
	stop: the block stays stop/exit (never wrapup-stop), and the person's approval stays refused."""
	worktree = stoppedInWrapup(main, fake)
	firstStop = clock.isoNow()
	fake.advance(60)
	claudeEnd = clock.isoNow()
	(runs(worktree) / "stopped-1").write_text(firstStop)
	(runs(worktree) / "exit-1").write_text(f"0 {claudeEnd}\n")  # today's files: the run ended (stopped)
	(runs(worktree) / "result-1.json").write_text(json.dumps({"result": "STATUS: done\nall green"}))
	endedAt = fake.now().timestamp()
	os.utime(runs(worktree) / "result-1.json", (endedAt, endedAt))
	runstate.update(str(worktree), 1, lambda r: (r["claude"].pop("stopped"), r["claude"].update(ended_at=claudeEnd,
		outcome="status", status="done", header_file="result-1.json"),
		r.update(stop={"requested_at": firstStop, "by": "person"})))

	def stopFacts():
		view = runstate.gateView(str(main), TOPIC, 1, root=str(worktree))
		return {(b["story"], b["fact"]) for b in view["blocks"] if b["fact"] in ("stop", "exit", runstate.WRAPUP_STOP)}

	before = stopFacts()
	assert ("record", "stop") in before and ("legacy", "stop") in before and not any(
		fact == runstate.WRAPUP_STOP for _story, fact in before), before
	fake.advance(600)
	runner = lookalike(tmp_path / "runner-int", "run-steps.py", "wrapup", str(worktree), "1", "--topic", TOPIC)
	runstate.update(str(worktree), 1, lambda r: r["steps"].update(verify={"state": "running", "attempt": 2,
		"runner_pid": runner.pid, "runner_start_ticks": ticksOf(runner.pid)}))
	answer = wt(main, monkeypatch, capsys, f"wt stop {TOPIC}", pid=False)
	assert answer.startswith(f"wt: stopped the wrap-up of {TOPIC} run 1"), answer
	assert runner.wait(timeout=10) == 0
	assert record(worktree)["stop"]["requested_at"] == firstStop
	assert (runs(worktree) / "stopped-1").read_text() == firstStop
	assert stopFacts() == before


@pytest.mark.parametrize("case", ["wrapper-alive", "unchecked", "no-ticks", "ticks-differ"])
def test_wt_stop_of_a_wrapup_signals_nothing_it_can_t_confirm(main, fake, monkeypatch, capsys, lookalike, tmp_path,
		case):
	worktree = stoppedInWrapup(main, fake)
	runner = lookalike(tmp_path / "runner-int", "run-steps.py", "wrapup", str(worktree), "1")
	ticks = {"no-ticks": None, "ticks-differ": ticksOf(runner.pid) + 1}.get(case, ticksOf(runner.pid))
	runstate.update(str(worktree), 1, lambda r: r.update(steps={"verify": {"state": "running", "attempt": 2,
		"runner_pid": runner.pid, "runner_start_ticks": ticks}}))
	pid = {"wrapper-alive": True, "unchecked": None}.get(case, False)
	answer = wt(main, monkeypatch, capsys, f"wt stop {TOPIC}", pid=pid)
	assert answer.startswith(f"wt: {TOPIC} has no running worker"), answer
	time.sleep(0.3)
	assert runner.poll() is None and not (tmp_path / "runner-int").exists()
	assert not (runs(worktree) / "stopped-1").exists()


def test_a_wrapper_that_fails_before_its_own_logging_leaves_its_error_in_the_run_s_log(tmp_path, monkeypatch):
	"""worker-control starts the wrapper with its output appended to runs/stderr-<run>.log: a bash error before the
	wrapper redirects its steps (here the whole wrapper) is kept, not dropped."""
	main = makeMain(tmp_path, monkeypatch)
	worktree = addTopic(main)
	(worktree / ".hook-only" / "runs").mkdir(parents=True)
	(worktree / ".hook-only" / "flow-mode").write_text("origin\n")
	control = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control_log")
	control.WRAPPER = tmp_path / "early-error.sh"
	control.WRAPPER.write_text("#!/usr/bin/env bash\necho 'early bash error' >&2\nexit 2\n")
	control.WRAPPER.chmod(0o755)
	control.start_run(main, worktree, TOPIC, tmp_path / "state.json", "new", SESSION, "/feature-flow t", 20, 2, 3, 26,
		"", {}, {}, {"started_by": "person", "prompt_kind": "feature-flow"})
	log = runs(worktree) / "stderr-1.log"
	deadline = time.time() + 30
	while time.time() < deadline and not (log.exists() and "early bash error" in log.read_text()):
		time.sleep(0.05)
	assert "early bash error" in log.read_text()


def test_auto_start_s_output_goes_to_the_deciding_run_s_log(tmp_path, monkeypatch):
	control = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control_auto_start")
	worktree = tmp_path / "wt"
	(worktree / ".hook-only" / "runs").mkdir(parents=True)
	calls = []
	monkeypatch.setattr(control.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))
	control.startAutoStart(worktree, TOPIC, 3)
	[(argv, kwargs)] = calls
	assert argv[2:] == ["auto-start", str(worktree), TOPIC, "3"] and kwargs["start_new_session"] is True
	assert kwargs["stderr"].name == str(worktree / ".hook-only" / "runs" / "stderr-3.log")
	assert kwargs["stdout"] is kwargs["stderr"] and kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("runsState", ["missing", "a-file"])
def test_wt_merge_and_wrapup_start_their_steps_even_without_a_usable_runs_dir(tmp_path, monkeypatch, capsys,
		runsState):
	"""startSteps never fails for want of the run's log: a missing runs/ is made, one that can't be (a file in its
	place) gives DEVNULL with a stderr line, and the program starts either way."""
	control = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control_steps")
	worktree = tmp_path / "wt"
	(worktree / ".hook-only").mkdir(parents=True)
	if runsState == "a-file":
		(worktree / ".hook-only" / "runs").write_text("not a directory")
	calls = []
	monkeypatch.setattr(control.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)) or "started")
	assert control.startSteps(["run-steps.py", "wrapup"], tmp_path / "main", worktree, 1) == "started"
	[(_argv, kwargs)] = calls
	if runsState == "missing":
		assert kwargs["stderr"].name == str(worktree / ".hook-only" / "runs" / "stderr-1.log")
		assert (worktree / ".hook-only" / "runs" / "stderr-1.log").exists()
	else:
		assert kwargs["stderr"] == subprocess.DEVNULL and "can't be opened" in capsys.readouterr().err


def test_wt_merge_on_a_kept_wrapup_points_to_wt_wrapup(main, fake, monkeypatch, capsys):
	worktree = keptOrphan(main, fake)
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	answer = wt(main, monkeypatch, capsys, f"wt merge {TOPIC}", pid=False)
	assert answer == (
		f"wt: merge: {TOPIC} run 1's wrap-up never ran, nothing to approve yet: type `wt wrapup {TOPIC}` first"
	)


@pytest.mark.parametrize("source", SOURCES)
def test_wt_merge_reads_the_run_s_end_and_merge_step_from_either_source(main, fake, monkeypatch, capsys, source):
	worktree = build(main, fake, source, "ended")
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	legacyFile(worktree, "merge-1.json", {"decision": "refused", "reason": "behind", "merge_run": True}, 0, fake.now())
	if source == "record":
		runstate.recordStepFromFile(str(worktree), 1, "merge")
	assert "needs a merge run first" in wt(main, monkeypatch, capsys, f"wt merge {TOPIC}")
	legacyFile(worktree, "merge-1.json", {"decision": "merged", "commit": "abc"}, 0, fake.now())
	if source == "record":
		runstate.recordStepFromFile(str(worktree), 1, "merge")
	assert "was merged, nothing to approve" in wt(main, monkeypatch, capsys, f"wt merge {TOPIC}")


@pytest.mark.parametrize("live", [True, False])
def test_wt_merge_waits_for_a_wrap_up_step_still_running_elsewhere(main, fake, monkeypatch, capsys, popenCalls, live):
	"""The run ended with its merge refused while verify.py, whose runner was killed,
	still ran in its own session; `wt merge` then decided on the running verify and stranded the run. While a step's
	runner or own program lives, wt merge starts nothing and says what runs; once it is gone, the approval starts."""
	worktree = build(main, fake, "record", "ended")
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	legacyFile(worktree, "merge-1.json", {"decision": "refused", "reason": "verify step running"}, 0, fake.now())
	runstate.recordStepFromFile(str(worktree), 1, "merge")
	program = REAL_POPEN(["sleep", "30"] if live else ["true"])
	try:
		if not live:
			program.wait()
		held = {"state": "running", "attempt": 1, "runner_pid": GONE_PID, "program_pid": program.pid,
			"started_at": clock.isoNow()}
		runstate.update(str(worktree), 1, lambda r: r["steps"].update(verify=held))
		answer = wt(main, monkeypatch, capsys, f"wt merge {TOPIC}", pid=False)
	finally:
		program.kill()
		program.wait()
	if live:
		assert answer.startswith(f"wt: merge: {TOPIC} run 1's wrap-up is still running (step verify)"), answer
		assert f"approve it then with `wt merge {TOPIC}`" in answer and popenCalls == []
		assert not (runs(worktree) / "approve-1.json").exists()
	else:
		assert "approving" in answer and len(popenCalls) == 1, answer


def doneAndWrapped(main, fake, merge):
	"""Run 1 ended STATUS done with merge as its merge step, and an estimate newer than its launch, so the estimate
	rule alone would let `wt resume` start run 2."""
	worktree = build(main, fake, "record", "ended")
	claude = {"ended_at": clock.isoNow(), "outcome": "status", "status": "done", "exit_code": 0, "stopped": False}
	runstate.update(str(worktree), 1, lambda r: r.update(claude=claude, steps={"merge": merge}))
	estimate = main / "plans" / "worker" / f"{TOPIC}.json"
	later = fake.now().timestamp() + 3600
	os.utime(estimate, (later, later))
	return worktree


@pytest.mark.parametrize("merge, refusedWith", [
	({"state": "done", "outcome": "merged", "commit": "606295fc72aa"}, "was merged into main (606295fc72)"),
	({"state": "done", "outcome": "refused", "reason": "a person must review"}, None),
])
def test_wt_resume_refuses_a_done_run_whose_merge_went_through(main, fake, monkeypatch, capsys, merge, refusedWith):
	"""`wt resume` on a run that ended done and merged started run 2 beside the finish, which
	then archived the topic under it. A merged done run is never resumed; one whose merge was refused still is."""
	worktree = doneAndWrapped(main, fake, merge)
	answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=False)
	if refusedWith:
		assert answer.startswith(f"wt: resume: {TOPIC} run 1 ended STATUS: DONE and {refusedWith}"), answer
		assert "nothing to resume" in answer and not runstate.recordExists(str(worktree), 2)
	else:
		assert answer.startswith(f"wt: resume {TOPIC}") and "run 2" in answer, answer


@pytest.mark.parametrize("live", [True, False])
def test_wt_resume_refuses_a_done_run_while_its_wrap_up_still_runs(main, fake, monkeypatch, capsys, live):
	"""The same class as wt merge's refusal: a step recorded running whose program lives holds the resume."""
	worktree = doneAndWrapped(main, fake, {"state": "done", "outcome": "refused", "reason": "verify step running"})
	program = REAL_POPEN(["sleep", "30"] if live else ["true"])
	try:
		if not live:
			program.wait()
		held = {"state": "running", "attempt": 1, "runner_pid": GONE_PID, "program_pid": program.pid,
			"started_at": clock.isoNow()}
		runstate.update(str(worktree), 1, lambda r: r["steps"].update(verify=held))
		answer = wt(main, monkeypatch, capsys, f"wt resume {TOPIC}", pid=False)
	finally:
		program.kill()
		program.wait()
	if live:
		assert answer.startswith(f"wt: resume: {TOPIC} run 1 ended STATUS: DONE and its wrap-up is still running "
			"(step verify)"), answer
		assert not runstate.recordExists(str(worktree), 2)
	else:
		assert answer.startswith(f"wt: resume {TOPIC}") and "run 2" in answer, answer


@pytest.mark.parametrize(
	"phase, text", [("finishing", "is still going (finishing)"), ("orphan-suspected", "never finished its wrap-up")]
)
def test_wt_merge_refuses_a_run_without_an_end(main, fake, monkeypatch, capsys, phase, text):
	worktree = build(main, fake, "record", phase)
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	assert text in wt(main, monkeypatch, capsys, f"wt merge {TOPIC}")


FAKE_WRAPPER = '#!/usr/bin/env bash\ntrap "exit 130" INT\nsleep 30\n'


@pytest.fixture
def wrapper(tmp_path):
	"""A live process that looks like run 1's wrapper of TOPIC (run-worker.sh <topic> <worktree> <run>) in its own
	session, and a plain `sleep` in its own session; both killed at the end."""
	script = tmp_path / "fake" / "run-worker.sh"
	script.parent.mkdir()
	script.write_text(FAKE_WRAPPER)
	started = []

	def spawn(*argv):
		process = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		started.append(process)
		return process

	yield lambda worktree: spawn("bash", str(script), TOPIC, str(worktree), "1"), lambda: spawn("sleep", "30")
	for process in started:
		if process.poll() is None:
			os.killpg(process.pid, signal.SIGKILL)
		process.wait(timeout=10)


def runningWith(main, fake, process, ticks=None):
	"""A record of run 1 running whose wrapper is process (its real start ticks unless ticks is given)."""
	worktree = build(main, fake, "record", "launching")
	facts = runstate.processFacts(process.pid)
	if ticks is not None:
		facts["start_ticks"] = ticks
	runstate.update(str(worktree), 1, lambda r: r.update(process={**facts, "started_at": clock.isoNow()}))
	runstate.beat(str(worktree), 1, INTERVAL)
	return worktree


def stopOutcome(main, monkeypatch, capsys, process, waitSeconds):
	"""`wt stop` with the real pid check; whether the process ended within waitSeconds."""
	module = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control")
	monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": f"wt stop {TOPIC}", "cwd": str(main)})))
	with pytest.raises(SystemExit):
		module.main()
	out = json.loads(capsys.readouterr().out)
	try:
		process.wait(timeout=waitSeconds)
	except subprocess.TimeoutExpired:
		pass
	return out.get("reason") or out["hookSpecificOutput"]["additionalContext"], process.poll() is not None


def test_wt_stop_signals_the_wrapper_verify_process_confirms(main, fake, monkeypatch, capsys, wrapper):
	fakeWrapper, _sleep = wrapper
	worktree = main.parent / "worktrees" / TOPIC
	process = fakeWrapper(worktree)
	time.sleep(0.2)  # bash has read the script
	runningWith(main, fake, process)
	answer, ended = stopOutcome(main, monkeypatch, capsys, process, 12)
	assert answer.startswith(f"wt: stopped {TOPIC} run 1") and ended
	assert (runs(worktree) / "stopped-1").exists() and record(worktree)["stop"]["by"] == "person"


@pytest.mark.parametrize("case", ["other-command", "other-start-ticks"])
def test_wt_stop_sends_no_signal_unless_verified(main, fake, monkeypatch, capsys, wrapper, case):
	"""Design T-5: a reused pid (another command, or the same command line started at another time) gets no
	signal; the run reads as an orphan whose process is gone, so there is no running worker to stop."""
	fakeWrapper, sleeper = wrapper
	worktree = main.parent / "worktrees" / TOPIC
	process = sleeper() if case == "other-command" else fakeWrapper(worktree)
	time.sleep(0.2)
	runningWith(main, fake, process, ticks=None if case == "other-command" else 1)
	answer, ended = stopOutcome(main, monkeypatch, capsys, process, 1)
	assert answer.startswith(f"wt: {TOPIC} has no running worker") and not ended
	assert not (runs(worktree) / "stopped-1").exists() and record(worktree).get("stop") is None


def test_wt_stop_without_a_recorded_pid_sends_nothing(main, fake, monkeypatch, capsys):
	worktree = build(main, fake, "record", "launching")
	module = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control")
	monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": f"wt stop {TOPIC}", "cwd": str(main)})))
	with pytest.raises(SystemExit):
		module.main()
	answer = json.loads(capsys.readouterr().out)["reason"]
	assert answer == (
		f"wt: {TOPIC} run 1: nothing was signalled: no wrapper pid is recorded for it, or /proc can't "
		f"be read; `wt status {TOPIC}` shows the evidence"
	)
	assert not (runs(worktree) / "stopped-1").exists()


# -- agent-guard: runstate.activeSince ----------------------------------------------------------------


@pytest.mark.parametrize("source, phase", NON_ORPHAN)
def test_a_run_is_active_while_it_goes_and_until_its_end(main, fake, source, phase):
	worktree = build(main, fake, source, phase)
	before = fake.now() - timedelta(minutes=1)
	active = runstate.activeSince(str(worktree), before, clock=fake.now)
	assert active is (phase != "none")
	fake.advance(3600)
	later = fake.now() - timedelta(seconds=1)
	assert runstate.activeSince(str(worktree), later, clock=fake.now) is (
		phase in ("launching", "running", "finishing", "unknown") and not stale(source, phase)
	)


def stale(source, phase):
	"""An hour on, a record's run without heartbeats and today's launching files are orphans (no longer active)."""
	return (source, phase) in (
		("record", "launching"),
		("record", "running"),
		("record", "finishing"),
		("legacy", "launching"),
	)


def test_active_since_ends_at_last_heartbeat_for_orphan(main, fake):
	"""Design T-6: an orphan was active until its last heartbeat; a move after it is not explained by the run."""
	worktree = build(main, fake, "record", "running")
	lastBeat = fake.now()
	fake.advance(THRESHOLD + 600)
	assert runstate.status(str(main), TOPIC)["phase"] == "orphan"
	assert runstate.activeSince(str(worktree), lastBeat - timedelta(seconds=5), clock=fake.now) is True
	assert runstate.activeSince(str(worktree), lastBeat + timedelta(seconds=5), clock=fake.now) is False


def test_agent_guard_explains_a_move_by_a_run_known_only_from_its_record(main, fake):
	"""No start file at all: the record's run explains the other worktree's branch move."""
	guard = load(os.path.join(HOOKS, "guards", "agent_guard.py"), "agent_guard")
	here = addTopic(main, "here-t")
	there = build(main, fake, "record", "running")
	clock.useSource(None)  # agent-guard compares with the real time
	runstate.update(str(there), 1, lambda r: r["process"].update(started_at=clock.isoNow()))
	runstate.beat(str(there), 1, INTERVAL)
	branches, _commits = guard.explainedMoves(str(here), time.time() - 5)
	assert f"refs/heads/feature/x/{TOPIC}" in branches


# -- auto-finish: the run's end ------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_auto_finish_waits_for_the_end_and_a_gone_wrapper(main, fake, monkeypatch, source):
	finish = load(os.path.join(HOOKS, "worker", "auto-finish.py"), "auto_finish")
	worktree = build(main, fake, source, "finishing")
	assert finish.runEnded(str(main), str(worktree), TOPIC, 1) is False
	if source == "legacy":
		legacyFile(worktree, "exit-1", f"0 {fake.now().isoformat(timespec='seconds')}\n", 0, fake.now())
	else:
		runstate.recordRunEnded(str(worktree), 1, "completed", 0)
	monkeypatch.setattr(runstate, "verifyProcess", verified(True))
	assert finish.runEnded(str(main), str(worktree), TOPIC, 1) is False  # its wrapper still finishes
	monkeypatch.setattr(runstate, "verifyProcess", verified(False))
	assert finish.runEnded(str(main), str(worktree), TOPIC, 1) is True


def test_auto_finish_never_takes_a_superseded_run_for_ended(main, fake):
	"""Today's files: a later run makes run 1 read ended (superseded), but it never wrote its end; removing the
	worktree waits for a real one, as it waited for exit-<run> before."""
	finish = load(os.path.join(HOOKS, "worker", "auto-finish.py"), "auto_finish")
	worktree = build(main, fake, "legacy", "finishing")
	legacyFile(worktree, "est-2.json", {"minutes": 5, "usd": 1.0}, 0, fake.now())
	assert runstate.status(str(main), TOPIC, run=1)["outcome"]["reason"] == "superseded"
	assert finish.runEnded(str(main), str(worktree), TOPIC, 1) is False


# -- the current run (worker-budget, dispatch-guard, worker-progress) --------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_the_current_run_is_the_newest_started_one_from_either_source(main, fake, source):
	worktree = build(main, fake, source, "running")
	started = runstate.current(str(worktree))
	assert started["run"] == 1 and runstate.parseStamp(started["started_at"]) is not None
	assert runstate.startedSeconds(str(worktree)) == runstate.parseStamp(started["started_at"]).timestamp()
	legacyFile(worktree, "est-2.json", {"minutes": 5, "usd": 1.0}, 0, fake.now())  # launched, not started yet
	assert runstate.current(str(worktree))["run"] == 1


def test_no_started_run_has_no_clock_and_an_unreadable_start_is_an_error(main, fake):
	worktree = addTopic(main)
	assert runstate.current(str(worktree)) is None and runstate.startedSeconds(str(worktree)) is None
	legacyFile(worktree, "start-1", "garbage\n", 0, fake.now())
	assert runstate.current(str(worktree)) == {"run": 1, "started_at": None}
	with pytest.raises(ValueError):
		runstate.startedSeconds(str(worktree))


def test_dispatch_guard_records_the_dispatch_stop_of_a_run_known_only_from_its_record(main, fake):
	guard = load(os.path.join(HOOKS, "guards", "dispatch_guard.py"), "dispatch_guard")
	worktree = build(main, fake, "record", "running")
	config = runstate.homeconfig.load("budget")
	guard.recordAutoResume(str(worktree), {"minutes": 5, "usd": 1.0}, 0, config, "qa")
	assert (runs(worktree) / "auto-resume-1.json").exists() and record(worktree)["dispatch_stop"]["minutes"] > 5


# -- what is left of the estimate (automatic resume) --------------------------------------------------


def test_left_of_estimate_counts_every_run_since_the_person_s_from_records(main, fake):
	worktree = addTopic(main)
	root = str(worktree)
	for k, startedBy in ((1, "person"), (2, "auto-resume")):
		run = launchRecord(
			root, started_by=startedBy, estimate={"minutes": 40, "usd": 6.0} if k == 1 else {"minutes": 9, "usd": 1.0}
		)
		runstate.update(root, run, lambda r: r.update(process={"started_at": clock.isoNow()}))
		fake.advance(600)
		call = {"k": 0, "kind": "main", "file": f"result-{run}.json", "exit_code": 0, "cost_usd": 1.5}
		runstate.update(root, run, lambda r: r.update(claude={"invocations": [call]}))
		if k == 1:
			runstate.recordRunEnded(root, run, "completed", 0)
	assert runstate.leftOfEstimate(root, TOPIC, 2, clock=fake.now) == (20.0, 3.0)


def test_left_of_estimate_from_today_s_files_takes_each_run_s_own_spend(main, fake):
	worktree = addTopic(main)
	now = fake.now()
	stamp = lambda minutes: (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")  # noqa: E731
	legacyFile(worktree, "est-1.json", {"minutes": 40, "usd": 6.0}, 0, now)
	legacyFile(worktree, "start-1", stamp(0) + "\n", 0, now)
	legacyFile(worktree, "result-1.json", {"session_id": SESSION, "total_cost_usd": 1.0}, 0, now)
	legacyFile(worktree, "exit-1", f"0 {stamp(10)}\n", 0, now)
	legacyFile(worktree, "est-2.json", {"minutes": 9, "usd": 1.0, "auto": True}, 0, now)
	legacyFile(worktree, "start-2", stamp(10) + "\n", 0, now)
	legacyFile(worktree, "result-2.json", {"session_id": SESSION, "total_cost_usd": 2.5}, 0, now)
	fake.advance(20 * 60)
	assert runstate.leftOfEstimate(str(worktree), TOPIC, 2, mainDir=str(main), clock=fake.now) == (20.0, 3.5)


# -- the queue's busy (T-7 is in test_wt_queue.py) ----------------------------------------------------


@pytest.mark.parametrize("source, phase", NON_ORPHAN)
def test_busy_reads_the_same_from_either_source(main, fake, source, phase):
	build(main, fake, source, phase)
	busy, usd = wt_queue.busyUsd(main, TOPIC)
	assert busy is BUSY[phase] and usd == (2.0 if busy and phase != "unknown" else 0.0)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("decision", ["resume", "merge", "fix", "none"])
def test_a_decided_next_run_holds_its_own_usd_until_it_starts(main, fake, source, decision):
	worktree = build(main, fake, source, "ended")
	legacyFile(
		worktree,
		"auto-1.json",
		{"run": 1, "decision": decision, "next_run": 2, "minutes": 9, "usd": 4.5},
		0,
		fake.now(),
	)
	if source == "record":
		runstate.recordStepFromFile(str(worktree), 1, "next")
	expected = (True, 4.5) if decision != "none" else (False, 0.0)
	assert wt_queue.busyUsd(main, TOPIC) == expected
	legacyFile(worktree, "est-2.json", {"minutes": 9, "usd": 4.5, "auto": True}, 0, fake.now())
	assert wt_queue.busyUsd(main, TOPIC) == (True, 4.5)  # run 2 launched: busy as itself


# -- auto-start never ends silently (self-repair stage 3a, A6) ---------------------------------------------------


def autoStartWorld(tmp_path, monkeypatch, state):
	control = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control_a6")
	main = tmp_path / "main"
	(main / ".hook-only" / "workers").mkdir(parents=True)
	(main / ".hook-only" / "workers" / f"{TOPIC}.json").write_text(json.dumps(state))
	worktree = tmp_path / "wt"
	(worktree / ".hook-only" / "runs").mkdir(parents=True)
	monkeypatch.setenv("WT_MAIN_DIR", str(main))
	monkeypatch.setattr(control.config_mode_guard, "refusal", lambda mainDir: None)
	return control, main, worktree


def personEvents(main):
	path = main / ".hook-only" / "events.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_auto_start_tells_when_another_run_took_the_state(tmp_path, monkeypatch):
	control, main, worktree = autoStartWorld(tmp_path, monkeypatch, {"run": 4, "session_id": SESSION})
	control.auto_start(worktree, TOPIC, 3)
	[event] = personEvents(main)
	assert event["kind"] == "refused" and "names run 4, not 3" in event["text"]


def test_auto_start_tells_when_the_wrapper_never_ends(tmp_path, monkeypatch):
	control, main, worktree = autoStartWorld(tmp_path, monkeypatch, {"run": 3, "pid": 4242, "session_id": SESSION})
	monkeypatch.setattr(control, "alive", lambda pid: True)
	monkeypatch.setattr(control, "is_our_wrapper", lambda pid, topic: True)
	monkeypatch.setattr(control.time, "sleep", lambda seconds: None)
	control.auto_start(worktree, TOPIC, 3)
	[event] = personEvents(main)
	assert event["kind"] == "refused" and "still ran after 180 s" in event["text"]


def test_an_auto_start_crash_is_a_stderr_line_an_event_and_exit_1(tmp_path, monkeypatch, capsys):
	"""Its decision file is missing: the exception is told (the run's log and a refused event), never silent."""
	control, main, worktree = autoStartWorld(tmp_path, monkeypatch, {"run": 3, "session_id": SESSION})
	with pytest.raises(SystemExit) as ended:
		control.autoStartTold(worktree, TOPIC, 3)
	assert ended.value.code == 1
	assert "auto-start crashed: FileNotFoundError" in capsys.readouterr().err
	[event] = personEvents(main)
	assert event["kind"] == "refused" and "auto-start crashed" in event["text"]

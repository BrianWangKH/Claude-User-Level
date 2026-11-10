"""run_ending.py (the record of a worker call's newest Stop and the watcher's cut) and worker-stop.py's part in it:
every Stop the hook sees is recorded for the checkout's newest started run, final only for a STATUS turn it lets
end (never a pause or a PHASE turn). The hook's decision itself is compared with the rule before the record (OLD_DECISION) on every
input combination. The watcher and the rewrite of a cut call run through run-worker.sh in test_run_steps.py.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_run_ending.py -q -p no:cacheprovider'
"""

import importlib.util
import itertools
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import run_ending  # noqa: E402
import statefile  # noqa: E402

STOP_HOOK = os.path.join(HOOKS, "worker", "worker-stop.py")
ZONE = timezone(timedelta(hours=8))
START = "2026-11-08T10:00:00+08:00"
SUBAGENT = {"type": "agent", "agent_type": "code-reviewer", "description": "review"}
COMMAND = {"type": "shell", "description": "dev server"}


def oldDecision(data):
	"""worker-stop.py's decision before the record, transcribed (test data, never imported by a program): the
	block reason, or None to let the turn end."""
	ending = re.compile(r"^(STATUS: (done|needs-decision|blocked)|PHASE: (tests-done|dev-done))\b")
	lines = [l.strip() for l in (data.get("last_assistant_message") or "").splitlines() if l.strip()]
	isEnding = bool(ending.match(lines[0] if lines else ""))
	tasks = data.get("background_tasks") or []
	commands = [t for t in tasks if not t.get("agent_type")]
	if commands:
		if isEnding:
			what = ", ".join(t.get("description") or t.get("type") or "?" for t in commands)
			return ("IN_FLIGHT", len(commands), what[:300])
		return None
	if tasks and not isEnding:
		return None
	if not isEnding and not data.get("stop_hook_active"):
		return "FORMAT"
	return None


def loadStopHook():
	spec = importlib.util.spec_from_file_location("worker_stop", STOP_HOOK)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.fixture
def frozen():
	moment = [datetime(2026, 11, 8, 10, 0, 5, tzinfo=ZONE)]
	clock.useSource(lambda: moment[0])
	yield moment
	clock.useSource(None)


def worktree(tmp_path, run=1):
	"""A checkout whose newest started run is run (its start-<run> file, as run-worker.sh writes it)."""
	subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=30)
	runs = tmp_path / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	if run is not None:
		(runs / f"start-{run}").write_text(START + "\n")
	return str(tmp_path.resolve())


def stop(root, message, tasks=(), active=False, **env):
	payload = {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": message,
		"background_tasks": list(tasks), "stop_hook_active": active}
	result = subprocess.run([sys.executable, STOP_HOOK], input=json.dumps(payload), capture_output=True, text=True,
		timeout=60, env={**os.environ, "CLAUDE_PROJECT_DIR": root, "CLAUDE_WORKER_MINUTES": "30", **env})
	assert result.returncode == 0 and not result.stderr, result.stderr
	return json.loads(result.stdout) if result.stdout.strip() else None


def turnOf(root, run=1):
	return statefile.read(root, run_ending.TURN, run)


def test_a_final_status_under_a_pending_subagent_is_recorded_with_its_message(tmp_path):
	root = worktree(tmp_path)
	assert stop(root, "STATUS: done\nall green", [SUBAGENT]) is None
	turn = turnOf(root)
	assert turn["final"] is True and turn["raw"]["last_assistant_message"] == "STATUS: done\nall green"
	assert turn["background"] == [SUBAGENT] and turn["session_id"] == "s1"


def test_a_pause_a_phase_a_held_status_and_a_missing_status_are_recorded_as_not_final(tmp_path):
	root = worktree(tmp_path)
	cases = (("waiting for the reviewer", [SUBAGENT]), ("PHASE: tests-done", [SUBAGENT]), ("PHASE: dev-done", []),
		("STATUS: done", [COMMAND]), ("finished", []))
	for message, tasks in cases:
		stop(root, "STATUS: done", [SUBAGENT])
		stop(root, message, tasks)
		turn = turnOf(root)
		assert turn["final"] is False and turn["raw"]["last_assistant_message"] is None, message


def test_nothing_is_recorded_without_a_started_run_or_in_the_wrappers_compact(tmp_path):
	root = worktree(tmp_path / "a", run=None)
	assert stop(root, "STATUS: done") is None
	assert os.listdir(os.path.join(root, ".hook-only", "runs")) == []
	root = worktree(tmp_path / "b")
	assert stop(root, "STATUS: done", CLAUDE_WORKER_COMPACT="1") is None
	assert turnOf(root) is None


def test_the_record_goes_to_the_newest_started_run(tmp_path):
	root = worktree(tmp_path)
	(tmp_path / ".hook-only" / "runs" / "start-2").write_text(START + "\n")
	stop(root, "STATUS: blocked")
	assert turnOf(root, 1) is None and turnOf(root, 2)["final"] is True


def test_the_decision_is_the_old_rule_on_every_input():
	"""Old vs new over messages x background tasks x stop_hook_active: the block reason is the same in every
	cell, and the turn counts as final exactly when it is let through on a STATUS first line (never PHASE)."""
	hook = loadStopHook()
	messages = ("", "   ", "STATUS: done\nx", "  \nSTATUS: blocked", "STATUS: needs-decision", "PHASE: tests-done",
		"PHASE: dev-done", "STATUS: weird", "STATUS:done", "status: done", "working on it")
	taskSets = ([], None, [SUBAGENT], [COMMAND], [SUBAGENT, COMMAND], [{}], [{"agent_type": ""}])
	counts = {"same": 0}
	for message, tasks, active in itertools.product(messages, taskSets, (False, True)):
		data = {"last_assistant_message": message, "background_tasks": tasks, "stop_hook_active": active}
		reason, final = hook.decide(data)
		old = oldDecision(data)
		assert (old is None) == (reason is None), data
		if old == "FORMAT":
			assert reason == hook.FORMAT, data
		elif old is not None:
			assert reason == hook.IN_FLIGHT.format(n=old[1], what=old[2]), data
		first = message.strip().splitlines()[0] if message.strip() else ""
		assert final == (reason is None and bool(re.match(r"STATUS: (done|needs-decision|blocked)\b", first))), data
		counts["same"] += 1
	assert counts["same"] == len(messages) * len(taskSets) * 2


def test_only_a_final_turn_after_the_call_s_start_counts(tmp_path, frozen):
	root = worktree(tmp_path)
	run_ending.recordTurn(root, 1, final=True, message="STATUS: done", sessionId=None, background=[])
	assert run_ending.finalTurn(root, 1, "2026-11-08T10:00:04+08:00")["final"] is True
	assert run_ending.finalTurn(root, 1, "2026-11-08T10:00:05+08:00") is None  # the same second: missed
	assert run_ending.finalTurn(root, 1, "2026-11-08T10:00:06+08:00") is None  # an earlier call's turn
	assert run_ending.finalTurn(root, 1, "not a time") is None
	frozen[0] += timedelta(seconds=90)
	assert run_ending.idleSeconds(turnOf(root)) == 90
	run_ending.recordTurn(root, 1, final=False, message="paused", sessionId=None, background=[])
	assert run_ending.finalTurn(root, 1, "2026-11-08T10:00:04+08:00") is None


def test_a_cut_record_belongs_only_to_its_own_call(tmp_path, frozen):
	root = worktree(tmp_path)
	run_ending.recordTurn(root, 1, final=True, message="STATUS: done\nx", sessionId=None, background=[])
	turn = run_ending.finalTurn(root, 1, "2026-11-08T10:00:01+08:00")
	run_ending.recordCut(root, 1, file="result-1.json", startedAt="2026-11-08T10:00:01+08:00", turn=turn,
		idleMaxSeconds=120)
	assert run_ending.cutOf(root, 1, "result-1.json", "2026-11-08T10:00:01+08:00")["raw"]["last_assistant_message"] == "STATUS: done\nx"
	assert run_ending.cutOf(root, 1, "result-1.json", "2026-11-08T10:00:02+08:00") is None
	assert run_ending.cutOf(root, 1, "phase-1-1.json", "2026-11-08T10:00:01+08:00") is None
	assert run_ending.cutOf(root, 2, "result-1.json", "2026-11-08T10:00:01+08:00") is None


def test_a_cut_call_s_output_is_its_status_turn_with_claude_s_own_kept():
	cut = {"cut_at": START, "turn_ended_at": START, "idle_max_seconds": 120, "raw": {"last_assistant_message": "STATUS: done\nfrom stop"}}
	out = run_ending.cutOutput("", None, cut)
	assert out["result"] == "STATUS: done\nfrom stop" and out["is_error"] is False and run_ending.isCut(out)
	assert "total_cost_usd" not in out and out["raw"]["claude_output"] == ""
	own = {"result": "STATUS: done\nclaude's own", "total_cost_usd": 1.0}
	assert run_ending.cutOutput(json.dumps(own), own, cut)["result"] == "STATUS: done\nclaude's own"
	phase = {"result": "PHASE: tests-done"}
	assert run_ending.cutOutput(json.dumps(phase), phase, cut)["result"] == "STATUS: done\nfrom stop"
	broken = {"result": "Error: interrupted", "is_error": True}
	out = run_ending.cutOutput(json.dumps(broken), broken, cut)
	assert out["result"] == "STATUS: done\nfrom stop" and out["raw"]["claude_output"] == json.dumps(broken)
	assert not run_ending.isCut(own) and not run_ending.isCut(None)


def test_only_a_status_first_line_with_a_final_value_counts():
	for text in ("STATUS: done", "STATUS: done, with notes\nx", "\nSTATUS: blocked", "STATUS: needs-decision"):
		assert run_ending.isStatusText(text), text
	for text in ("", "PHASE: tests-done", "STATUS: weird", "status: done", "working\nSTATUS: done", "STATUS:"):
		assert not run_ending.isStatusText(text), text


def test_a_cut_output_without_a_status_line_is_refused_and_never_reads_as_cut():
	cut = {"cut_at": START, "turn_ended_at": START, "idle_max_seconds": 90, "raw": {"last_assistant_message": "PHASE: x"}}
	with pytest.raises(run_ending.NoStatus):
		run_ending.cutOutput("", {"result": "Error"}, cut)
	assert not run_ending.isCut({run_ending.CUT_KEY: {}, "result": "PHASE: tests-done"})
	assert not run_ending.isCut({run_ending.CUT_KEY: {}, "result": ""})
	assert run_ending.isCut({run_ending.CUT_KEY: {}, "result": "STATUS: done"})


@pytest.mark.parametrize("value", ["DONE", "NEEDS-DECISION", "BLOCKED", "Done"])
def test_an_upper_case_status_value_ends_the_run_as_the_lower_case_one(tmp_path, value):
	"""R2's enum: workers write `STATUS: DONE`. worker-stop.py lets it through as final, run_ending records it as
	the final turn (so the watcher can cut the idle wait after it); the key itself stays upper case only."""
	hook = loadStopHook()
	message = f"STATUS: {value}\nall green"
	assert hook.decide({"last_assistant_message": message, "background_tasks": [SUBAGENT]}) == (None, True)
	assert run_ending.isStatusText(message)
	assert not run_ending.isStatusText(f"status: {value}") and not run_ending.isStatusText("STATUS: WEIRD")
	root = worktree(tmp_path)
	assert stop(root, message, [SUBAGENT]) is None and turnOf(root)["final"] is True

"""Tests for worker-progress.py (the wt-pane mod's data source).

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_worker_progress.py
Each test feeds hook input JSON to the hook as a subprocess, with CLAUDE_PROJECT_DIR pointing at a
throwaway checkout (not a git repo, so the checkout is that directory) and CLAUDE_PRICES at a price
table where one input token costs $1e-6.
"""
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.environ.get("WP_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(HOOKS, "worker-progress.py")
REAL_HOOKS = os.path.expanduser("~/.claude/hooks")
MODEL = "test-model"


@pytest.fixture
def checkout(tmp_path):
	root = tmp_path / "wt"
	(root / ".hook-only" / "runs").mkdir(parents=True)
	(root / ".hook-only" / "runs" / "start-1").write_text("2026-10-30T10:00:00+08:00\n")
	prices = tmp_path / "prices.json"
	prices.write_text(json.dumps({"models": {MODEL: {
		"input": 1, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}}}))
	transcript = tmp_path / "session.jsonl"
	transcript.write_text("")
	return {"root": root, "prices": prices, "transcript": transcript}


def assistant(requestId, inputTokens, model=MODEL):
	return json.dumps({"type": "assistant", "message": {
		"id": requestId, "model": model, "usage": {"input_tokens": inputTokens, "output_tokens": 0}}}) + "\n"


def run(checkout, agentId=None, tool="Bash", toolInput=None, worker=True, raw=None):
	env = {**os.environ, "CLAUDE_PROJECT_DIR": str(checkout["root"]), "CLAUDE_PRICES": str(checkout["prices"])}
	env["PYTHONPATH"] = os.pathsep.join([HOOKS, REAL_HOOKS])  # hookonly, prices
	env.pop("CLAUDE_WORKER_MINUTES", None)
	if worker:
		env["CLAUDE_WORKER_MINUTES"] = "30"
	data = {"hook_event_name": "PreToolUse", "session_id": "s1", "transcript_path": str(checkout["transcript"]),
		"cwd": str(checkout["root"]), "tool_name": tool, "tool_input": toolInput or {"description": "Run tests"}}
	if agentId:
		data.update(agent_id=agentId, agent_type="qa")
	result = subprocess.run([sys.executable, HOOK], input=raw if raw is not None else json.dumps(data),
		capture_output=True, text=True, env=env, timeout=30)
	return result


def progress(checkout, key):
	path = checkout["root"] / ".hook-only" / "hook-read-only" / "progress" / f"{key}.json"
	return json.loads(path.read_text()) if path.exists() else None


def test_outside_a_worker_run_nothing_is_written(checkout):
	result = run(checkout, worker=False)
	assert result.returncode == 0
	assert not (checkout["root"] / ".hook-only" / "hook-read-only").exists()


def test_orchestrator_call_records_what_and_spend_and_prints_nothing(checkout):
	# One request split over two content blocks (same id) counts once; the last usage wins.
	checkout["transcript"].write_text(assistant("r1", 100) + assistant("r1", 300) + assistant("r2", 200))
	result = run(checkout, toolInput={"description": "Run pytest for transform", "command": "uv run pytest"})
	assert result.returncode == 0
	assert result.stdout == ""
	state = progress(checkout, "main")
	assert state["agent_type"] == "orchestrator"
	assert state["tool"] == "Bash"
	assert state["what"] == "Run pytest for transform"
	assert state["run"] == 1
	assert state["usd"] == pytest.approx(500e-6)
	assert state["usd_run"] == pytest.approx(500e-6)
	assert state["unpriced"] == 0


def test_subagent_call_reads_the_subagent_transcript(checkout):
	checkout["transcript"].write_text(assistant("main-1", 999))
	subagents = checkout["transcript"].with_suffix("") / "subagents"
	subagents.mkdir(parents=True)
	(subagents / "agent-a1.jsonl").write_text(assistant("q1", 40))
	run(checkout, agentId="a1", tool="Edit", toolInput={"file_path": str(checkout["root"] / "transform" / "x.py")})
	state = progress(checkout, "a1")
	assert state["agent_type"] == "qa"
	assert state["what"] == "transform/x.py"
	assert state["usd"] == pytest.approx(40e-6)
	assert progress(checkout, "main") is None


def test_only_new_complete_lines_are_added(checkout):
	checkout["transcript"].write_text(assistant("r1", 100))
	run(checkout)
	# A line still being written (no newline yet) is left for the next call.
	with open(checkout["transcript"], "a") as f:
		f.write(assistant("r2", 50)[:-10])
	run(checkout)
	assert progress(checkout, "main")["usd"] == pytest.approx(100e-6)
	with open(checkout["transcript"], "a") as f:
		f.write(assistant("r2", 50)[-10:])
	run(checkout)
	assert progress(checkout, "main")["usd"] == pytest.approx(150e-6)


def test_a_new_run_counts_from_its_own_start(checkout):
	checkout["transcript"].write_text(assistant("r1", 100))
	run(checkout)
	(checkout["root"] / ".hook-only" / "runs" / "start-2").write_text("2026-10-30T11:00:00+08:00\n")
	with open(checkout["transcript"], "a") as f:
		f.write(assistant("r2", 30))
	run(checkout)
	state = progress(checkout, "main")
	assert state["run"] == 2
	assert state["usd"] == pytest.approx(130e-6)
	assert state["usd_run"] == pytest.approx(30e-6)
	with open(checkout["transcript"], "a") as f:
		f.write(assistant("r3", 5))
	run(checkout)
	assert progress(checkout, "main")["usd_run"] == pytest.approx(35e-6)


def test_unpriced_model_is_counted_not_priced(checkout):
	checkout["transcript"].write_text(assistant("r1", 100) + assistant("r2", 100, model="unknown-model"))
	run(checkout)
	state = progress(checkout, "main")
	assert state["usd"] == pytest.approx(100e-6)
	assert state["unpriced"] == 1


@pytest.mark.parametrize("tool,toolInput,expected", [
	("Agent", {"subagent_type": "developer", "description": "Implement clean"}, "developer: Implement clean"),
	("SendMessage", {"to": "qa", "message": "go on"}, "to qa"),
	("Skill", {"skill": "agent-common"}, "agent-common"),
	("Grep", {"pattern": "available_date"}, "available_date"),
	("Read", {"file_path": "/elsewhere/a.py"}, "/elsewhere/a.py"),
	("Bash", {"command": "echo   a\n  b"}, "echo a b"),
	("TaskList", {}, ""),
])
def test_what_labels_each_tool(checkout, tool, toolInput, expected):
	run(checkout, tool=tool, toolInput=toolInput)
	assert progress(checkout, "main")["what"] == expected


def test_long_labels_are_cut(checkout):
	run(checkout, toolInput={"description": "x" * 300})
	what = progress(checkout, "main")["what"]
	assert len(what) == 100 and what.endswith("…")


def test_bad_input_never_fails_the_call(checkout):
	result = run(checkout, raw="not json")
	assert result.returncode == 0
	assert result.stdout == ""


def test_an_agent_id_that_is_not_a_plain_name_writes_nothing(checkout):
	run(checkout, agentId="../escape")
	assert not (checkout["root"] / ".hook-only" / "hook-read-only" / "progress" / "../escape.json").exists()
	assert not (checkout["root"] / ".hook-only" / "hook-read-only" / "escape.json").exists()


def test_stale_temporary_files_are_removed_fresh_ones_kept(checkout):
	progressDir = checkout["root"] / ".hook-only" / "hook-read-only" / "progress"
	(progressDir / ".offsets").mkdir(parents=True)
	stale = progressDir / "main.json.111.tmp"
	staleOffset = progressDir / ".offsets" / "a1.json.222.tmp"
	fresh = progressDir / "q1.json.333.tmp"
	for path in (stale, staleOffset, fresh):
		path.write_text("{")
	old = os.path.getmtime(fresh) - 3600
	os.utime(stale, (old, old))
	os.utime(staleOffset, (old, old))
	run(checkout)
	assert not stale.exists()
	assert not staleOffset.exists()
	assert fresh.exists()
	assert progress(checkout, "main") is not None

"""report_check.py and report_schema.py: flow agents' hand-backs start with the report header.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_report_check.py
"""
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import report_schema  # noqa: E402


def handback(tmp_path, role, message, agent="a1234567"):
	data = {"hook_event_name": "PreToolUse", "tool_name": "SubagentHandback", "agent_id": agent, "agent_type": role,
		"tool_input": {"message": message}, "cwd": str(tmp_path), "session_id": "s"}
	env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "hook-entry.py"), "check", "report-check"], input=json.dumps(data),
		capture_output=True, text=True, env=env)
	return (json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None), result.stderr


@pytest.mark.parametrize("role,message", [
	("qa-planner", "STATUS: DONE\nQUESTIONS: none\n\nplan written"),
	("qa-planner", "STATUS: CHECKPOINT\n\nhalf done"),
	("spec-reviewer", "STATUS: DONE\nVERDICT: CHANGES REQUESTED\nQUESTIONS: Q1 Q2\n\n1. x"),
	("qa", "\nSTATUS: BLOCKED\n\nflaky test"),
	("developer", "STATUS: DONE\nNEEDS-TEST: a test that executes x.py:3\n\nok"),
	("code-reviewer", "STATUS: DONE\nVERDICT: CHANGES REQUESTED\nFIX-QA: F1, F3\nFIX-DEVELOPER: none\n\nfindings"),
	("code-reviewer", "STATUS: DONE\nVERDICT: APPROVE\n\nall good"),
	("flow-judge", "DECISION: answer Q1 use `failures`\nDECISION: ask-user Q2 show it?\nwhy: Q2 is visible"),
	("verifier", "no header needed"),
])
def test_valid_headers_pass(tmp_path, role, message):
	out, err = handback(tmp_path, role, message)
	assert out is None, (out, err)


@pytest.mark.parametrize("role,message,problem", [
	("qa", "DONE\nall green", "missing `STATUS:` line"),
	("spec-reviewer", "STATUS: DONE\n\nDONE — APPROVE", "missing `VERDICT:` line"),
	("spec-reviewer", "STATUS: DONE\nVERDICT: APPROVE", "missing `QUESTIONS:` line"),
	("code-reviewer", "STATUS: DONE\nVERDICT: CHANGES REQUESTED\nFIX-QA: none\nFIX-DEVELOPER: none", "needs at least one"),
	("developer", "STATUS: FINISHED", "`STATUS: FINISHED` is not allowed"),
	("flow-judge", "I think Q1 is internal.", "missing `DECISION:` line"),
	("qa", "STATUS: DONE\nSTATUS: DONE", "appears 2 times"),
])
def test_invalid_headers_are_denied_with_the_template(tmp_path, role, message, problem):
	out, _ = handback(tmp_path, role, message)
	assert out["permissionDecision"] == "deny"
	reason = out["permissionDecisionReason"]
	assert problem in reason and "STATUS: DONE" in reason or role == "flow-judge" and problem in reason


def test_after_max_denials_the_hand_back_goes_through(tmp_path):
	limit = report_schema.load()["max_denials"]
	for _ in range(limit):
		out, _ = handback(tmp_path, "qa", "no header")
		assert out["permissionDecision"] == "deny"
	out, err = handback(tmp_path, "qa", "no header")
	assert out is None and "let through" in err


def test_a_valid_hand_back_resets_the_count(tmp_path):
	handback(tmp_path, "qa", "no header")
	assert handback(tmp_path, "qa", "STATUS: DONE")[0] is None
	assert not (tmp_path / ".hook-only" / "report-check" / "a1234567.json").exists()


def test_the_main_session_is_ignored(tmp_path):
	data = {"hook_event_name": "PreToolUse", "tool_name": "SubagentHandback", "tool_input": {"message": "x"},
		"cwd": str(tmp_path)}
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "hook-entry.py"), "check", "report-check"], input=json.dumps(data),
		capture_output=True, text=True)
	assert result.stdout.strip() == ""


def test_the_header_ends_at_the_first_line_that_is_not_a_field():
	fields = report_schema.header("STATUS: DONE\nVERDICT: APPROVE\nSome text\nQUESTIONS: Q1")
	assert fields == {"STATUS": ["DONE"], "VERDICT": ["APPROVE"]}

"""bash-guard.py and checkpoint-status.py: two text rules turned into hooks (2026-11-01).

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_rule_hooks.py
"""
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hook(name, data):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(data),
		capture_output=True, text=True, env={**os.environ, "PYTHONPATH": HOOKS})
	assert result.returncode == 0 and "error" not in result.stderr, result.stderr
	return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None


def bash(command):
	return hook("bash-guard.py", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
		"tool_input": {"command": command}, "cwd": "/tmp", "session_id": "s"})


@pytest.mark.parametrize("command", ["cd /repo && uv run pytest", "cd /repo; ls", "  cd sub&&make",
	'cd "/a b" && ls', "cd '/a b';ls"])
def test_a_cd_prefix_is_denied_with_the_alternatives(command):
	out = bash(command)
	assert out["permissionDecision"] == "deny"
	assert "git -C" in out["permissionDecisionReason"] and "env -C" in out["permissionDecisionReason"]


@pytest.mark.parametrize("command", ["cd /repo", "ls && cd /x && ls", "git -C /repo status", "echo cd /x && ls",
	"cdk deploy && ls", "uv run pytest"])
def test_other_commands_pass(command):
	assert bash(command) is None


def test_other_tools_pass():
	assert hook("bash-guard.py", {"hook_event_name": "PreToolUse", "tool_name": "Read",
		"tool_input": {"file_path": "/x"}}) is None


@pytest.fixture
def repo(tmp_path):
	subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
	(tmp_path / "written.py").write_text("x = 1\n")
	(tmp_path / ".hook-only").mkdir()
	(tmp_path / ".hook-only" / "state.json").write_text("{}")
	return tmp_path


def prompt(repo, text):
	return hook("checkpoint-status.py", {"hook_event_name": "UserPromptSubmit", "prompt": text, "cwd": str(repo),
		"session_id": "s"})


def test_a_checkpoint_report_gets_the_real_git_status(repo):
	out = prompt(repo, "<task-notification>\n  CHECKPOINT\n  done: wrote a.py, b.py\n</task-notification>")
	context = out["additionalContext"]
	assert "?? written.py" in context and "a.py" not in context.split("right now")[1]
	assert ".hook-only" not in context.split("right now")[1]


def test_a_checkpoint_in_the_report_header_counts(repo):
	out = prompt(repo, "  STATUS: CHECKPOINT\n\n  done: wrote a.py")
	assert "?? written.py" in out["additionalContext"]


def test_a_clean_checkout_says_so(tmp_path):
	subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
	assert "(clean" in prompt(tmp_path, "CHECKPOINT: half done")["additionalContext"]


@pytest.mark.parametrize("text", ["DONE\nall green", "BLOCKED\nneeds a decision", "the CHECKPOINT rule says"])
def test_other_prompts_get_nothing(repo, text):
	assert prompt(repo, text) is None

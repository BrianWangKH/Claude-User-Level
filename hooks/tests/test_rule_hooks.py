"""bash_guard.py and checkpoint_status.py (steps of hook-entry.py, run alone with `check`): two text rules turned
into hooks.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_rule_hooks.py
"""
import json
import os
import re
import subprocess
import sys

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hook(name, data):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "hook-entry.py"), "check", name.removesuffix(".py")], input=json.dumps(data),
		capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.path.join(HOOKS, "lib")})
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


def test_the_cd_denial_cites_a_section_the_global_claude_md_has():
	reason = bash("cd /repo && ls")["permissionDecisionReason"]
	section = re.search(r"\(global CLAUDE\.md, ([^)]+)\)", reason).group(1)
	with open(os.path.join(os.path.dirname(HOOKS), "CLAUDE.md"), encoding="utf-8") as f:
		assert f"\n## {section}\n" in f.read(), section


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


# --- home-delete-guard (bash_guard.homeDeletes): a home agent's Bash deletes nothing (skill home-draft SAFETY 3) ---

HOME_AGENTS = ("home-drafter", "home-verifier")
DELETES = [
	'rm -f "$f"',  # the verifier's rm on a variable path
	"ls; rm -rf /tmp/draft/overlay-1",
	"/bin/rm a",
	"xargs rm < list",
	"find . -name '*.pyc' -exec rm {} +",
	"find /tmp/draft -name __pycache__ -delete",
	"rmdir empty",
	"unlink f",
	"shred -u f",
	"rsync -a --delete a/ b/",
	"echo $(rm x)",
	'python3 -c "import shutil; shutil.rmtree(p)"',
	"python3 -c 'import os; os.remove(p)'",
	"python3 -c 'import os; os.unlink(p)'",
	"python3 -c 'import os; os.rmdir(p)'",
	"python3 -c 'from pathlib import Path; Path(p).unlink()'",
	"python3 -I -c 'import os,shutil,sys; shutil.rmtree(sys.argv[1])' d",
]
KEEPS = ["ls -la", "mv a /tmp/draft/trash/20261108T2100/", "cp -r a b", "grep -rn rmtree_like f",
	"docker run --rm x", "cat /tmp/rmdata", "python3 x.py", "find . -name '*.py'", "sed -n 1p f"]


def homeCall(command, agentType):
	data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp",
		"session_id": "s"}
	if agentType:
		data.update(agent_type=agentType, agent_id="a1")
	return hook("home-delete-guard", data)


# The step runs for the home agents only while their agent files exist (hook-entry.py warns about an unknown
# agent_type), so these tests need them (conftest.py private_layer).
PRIVATE_AGENTS = pytest.mark.private_layer("agents/home-drafter.md", "agents/home-verifier.md")


@PRIVATE_AGENTS
@pytest.mark.parametrize("command", DELETES)
@pytest.mark.parametrize("agentType", HOME_AGENTS)
def test_a_home_agent_s_deleting_command_is_denied_with_the_alternative(command, agentType):
	out = homeCall(command, agentType)
	assert out and out["permissionDecision"] == "deny", command
	reason = out["permissionDecisionReason"]
	assert "<draft>/trash/<timestamp>/" in reason and "fresh overlay" in reason and "SAFETY 3" in reason


@PRIVATE_AGENTS
@pytest.mark.parametrize("command", KEEPS)
def test_a_home_agent_s_other_commands_pass(command):
	assert homeCall(command, "home-verifier") is None


@pytest.mark.parametrize("agentType", [None, "developer", "investigator", "qa"])
def test_other_agents_and_the_main_session_may_delete(agentType):
	"""The step runs only for the home agents (hook_entries agents filter): nobody else is affected."""
	assert homeCall("rm -rf build", agentType) is None


@PRIVATE_AGENTS
@pytest.mark.parametrize("command", ["python3 /tmp/draft/work/cleanup.py", "\\rm x", "r''m x",
	"c=rm; $c x", 'eval "r""m x"'])
def test_spellings_outside_the_common_forms_are_the_known_limit(command):
	"""Not seen, on purpose (module doc): an escaped or quote-split command word, a variable or eval, a script file.
	The guard is the reminder for the common forms; the skill's rule and the sealed round evidence cover the rest."""
	assert homeCall(command, "home-drafter") is None

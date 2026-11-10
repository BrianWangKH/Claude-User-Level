"""hookrun.py: PreToolUse hooks fail closed (exit 2) on any escaped exception; other events keep the hook's
own error handling (exit 2 there would block the prompt or keep an agent running).

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_hookrun.py
"""
import json
import os
import subprocess
import sys

import pytest

import config_fixtures

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import homeconfig  # noqa: E402

FAKE_HOOK = """
import sys
sys.path.insert(0, {hooks!r})
import hookrun

def main():
	json_in = sys.stdin.read()
	if "exit7" in json_in:
		sys.exit(7)
	raise RuntimeError("boom")

def onError(error):
	print(f"fake error: {{error}}", file=sys.stderr)
	sys.exit(0)

hookrun.run("fake", main, onError)
"""


def runScript(script, data, env=None, cwd=None):
	raw = data if isinstance(data, str) else json.dumps(data)
	return subprocess.run([sys.executable, script], input=raw, capture_output=True, text=True, timeout=60,
		env={**os.environ, **(env or {})}, cwd=cwd)


@pytest.fixture
def fake(tmp_path):
	script = tmp_path / "fake.py"
	script.write_text(FAKE_HOOK.format(hooks=os.path.join(RUN_HOOKS, "lib")))
	return str(script)


def test_pre_tool_use_exception_blocks(fake):
	result = runScript(fake, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
	assert result.returncode == 2
	assert "fake: RuntimeError: boom — blocked (fail closed)" in result.stderr


@pytest.mark.parametrize("data", [{"hook_event_name": "Stop"}, {"hook_event_name": "UserPromptSubmit"},
	{"hook_event_name": "SubagentStop"}, "not json"])
def test_other_events_keep_the_hooks_own_handling(fake, data):
	result = runScript(fake, data)
	assert result.returncode == 0 and "fake error: boom" in result.stderr


def test_a_hooks_own_exit_passes_through(fake):
	assert runScript(fake, {"hook_event_name": "PreToolUse", "x": "exit7"}).returncode == 7


@pytest.fixture
def repo(tmp_path):
	r = tmp_path / "repo"
	r.mkdir()
	subprocess.run(["git", "init", "-q", str(r)], check=True)
	return r


def fetchOrigin(repo, data, env):
	"""fetch_origin.py as a step of hook-entry.py (run alone with `check`)."""
	raw = json.dumps({"cwd": str(repo), **data})
	return subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "hook-entry.py"), "check", "fetch-origin"], input=raw,
		capture_output=True, text=True, timeout=60, env={**os.environ, **env, "CLAUDE_PROJECT_DIR": str(repo)}, cwd=repo)


def denialOf(result):
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


SKILL_CALL = {"hook_event_name": "PreToolUse", "tool_name": "Skill", "tool_input": {"skill": "new-worktree"}}
PROMPT = {"hook_event_name": "UserPromptSubmit", "prompt": "/new-worktree feature/docs/x-y"}


def badRecord(repo):
	"""The checkout's flow-mode record holding no mode: flowMode's ConfigError (no variable is a source)."""
	(repo / ".hook-only").mkdir()
	(repo / ".hook-only" / "flow-mode").write_text("bogus\n")


def test_pre_tool_use_step_with_an_invalid_flow_mode_denies(repo):
	badRecord(repo)
	reason = denialOf(fetchOrigin(repo, SKILL_CALL, {}))
	assert "fetch-origin: ConfigError" in reason and "'bogus'" in reason
	assert "blocked (fail closed)" in reason


def test_pre_tool_use_step_with_a_broken_registry_denies(repo, tmp_path):
	"""A broken settings file the step reads denies (fail closed): the flow mode's record here. The environment
	registry (registry/env.json) is no longer read by the flow mode at all, so breaking it changes nothing."""
	home = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home")
	(home / "config" / homeconfig.REGISTRY_DIR / os.path.basename(homeconfig.path("env"))).write_text("{ not json")

	def run():
		return subprocess.run([sys.executable, str(home / "hooks" / "hook-entry.py"), "check", "fetch-origin"],
			input=json.dumps({"cwd": str(repo), **SKILL_CALL}), capture_output=True, text=True, timeout=60,
			env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}, cwd=repo)

	result = run()
	assert result.returncode == 0 and "decision" not in result.stdout
	badRecord(repo)
	assert "blocked (fail closed)" in denialOf(run())


def test_user_prompt_submit_step_with_an_invalid_flow_mode_does_not_block(repo):
	badRecord(repo)
	result = fetchOrigin(repo, PROMPT, {})
	assert result.returncode == 0
	assert "fetch-origin: ConfigError" in result.stderr and "'bogus'" in result.stderr
	assert "decision" not in json.loads(result.stdout or "{}")

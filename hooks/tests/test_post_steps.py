"""The PostToolUse steps that act outside the sandbox: finish-worktree (runs finish-worktree.sh --remove for the
exact finish command) and merge-origin (runs merge-origin.sh --merge after the exact check command said READY).
Each runs only for its exact command, runs the installed script (a fake one under a scratch HOME here) with its
flag in the session's cwd, and hands the result back as context; an error comes back as context too.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_post_steps.py -q -p no:cacheprovider'
"""

import importlib.util
import json
import os
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)

FINISH_SCRIPT = os.path.join("skills", "finish-worktree", "finish-worktree.sh")  # under <home>/.claude
MERGE_SCRIPT = os.path.join("skills", "feature-flow", "merge-origin.sh")


def fakeScript(home, rel, code=0, say="done"):
	"""A script at <home>/.claude/<rel> that logs its arguments and cwd to calls.jsonl, prints say, exits code."""
	path = home / ".claude" / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	log = home / "calls.jsonl"
	path.write_text("#!/usr/bin/env bash\n"
		f"python3 -c 'import json,os,sys; print(json.dumps({{\"argv\": sys.argv[1:], \"cwd\": os.getcwd()}}))' "
		f"\"$@\" >> {log}\n"
		f"echo {say}\nexit {code}\n")
	return log


def calls(home):
	log = home / "calls.jsonl"
	return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def loadStep(name, home, monkeypatch, script):
	"""The step module loaded fresh with HOME at the scratch home, so its fixed script path is the fake one."""
	monkeypatch.setenv("HOME", str(home))
	path = os.path.join(HOOKS, "worker", f"{name}.py")
	spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	assert module.SCRIPT == str(home / ".claude" / script)  # the fake, never the installed script
	return module


def command(module, home, spelled="~"):
	"""The command that runs the step's script: `bash <script>` with the home as ~ or spelled out (the script's
	path is the step's own SCRIPT, so the test doesn't keep a second copy of it)."""
	return "bash " + ("~" if spelled == "~" else str(home)) + module.SCRIPT[len(str(home)):]


def contextOf(output):
	specific = (output or {}).get("hookSpecificOutput") or {}
	assert specific.get("hookEventName") == "PostToolUse"
	return specific["additionalContext"]


def bash(command, cwd, **extra):
	return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command},
		"cwd": str(cwd), **extra}


@pytest.fixture
def home(tmp_path):
	h = tmp_path / "home"
	h.mkdir()
	return h


@pytest.fixture
def cwd(tmp_path):
	c = tmp_path / "main"
	c.mkdir()
	return c


# -- finish-worktree ------------------------------------------------------------------------------------


@pytest.fixture
def finish(home, monkeypatch):
	fakeScript(home, FINISH_SCRIPT, say="removed worktree")
	return loadStep("finish_worktree", home, monkeypatch, FINISH_SCRIPT)


@pytest.mark.parametrize("prefix", ["~", "HOME"])
def test_finish_worktree_removes_through_the_installed_script_for_the_exact_command(finish, home, cwd, prefix):
	"""Unit: the exact command (with ~ or the home spelled out) runs the installed script with the target and
	--remove in the session's cwd; its output comes back as context."""
	out = finish.check(bash(command(finish, home, prefix) + " feature/docs/some-topic", cwd))
	assert contextOf(out) == "finish-worktree hook: removed worktree"
	assert calls(home) == [{"argv": ["feature/docs/some-topic", "--remove"], "cwd": str(cwd)}]


@pytest.mark.parametrize("other", [
	lambda c: c,
	lambda c: f"{c} some-topic --remove",
	lambda c: f"{c} some-topic; rm -rf x",
	lambda c: f"{c} -rf",
	lambda c: c.replace("bash ~", "bash /elsewhere", 1) + " some-topic",
	lambda c: c.replace("finish-worktree.sh", "other.sh") + " some-topic",
], ids=["no-target", "extra-word", "chained", "flag", "other-place", "other-script"])
def test_finish_worktree_runs_nothing_for_any_other_command(finish, home, cwd, other):
	"""Unit: anything but the exact command (extra words, a chained command, another script or place) is no
	trigger: no output, the script never runs."""
	assert finish.check(bash(other(command(finish, home)), cwd)) is None
	assert finish.check({**bash(command(finish, home) + " some-topic", cwd), "tool_name": "Edit"}) is None
	assert calls(home) == []


def test_finish_worktree_reports_a_failed_removal_and_an_error(home, cwd, tmp_path, monkeypatch):
	"""Unit: a non-zero exit comes back as `not removed (exit n)` with the output; a script that can't run
	(here: the cwd is gone) as `error: ...`; neither raises."""
	fakeScript(home, FINISH_SCRIPT, code=3, say="branch not merged")
	finish = loadStep("finish_worktree", home, monkeypatch, FINISH_SCRIPT)
	out = finish.check(bash(command(finish, home) + " some-topic", cwd))
	assert contextOf(out) == "finish-worktree hook: not removed (exit 3): branch not merged"
	gone = finish.check(bash(command(finish, home) + " some-topic", tmp_path / "missing"))
	assert contextOf(gone).startswith("finish-worktree hook: error: ")


# -- merge-origin -----------------------------------------------------------------------------------------


@pytest.fixture
def merge(home, monkeypatch):
	fakeScript(home, MERGE_SCRIPT, code=1, say="CONFLICT in a.py")
	return loadStep("merge_origin", home, monkeypatch, MERGE_SCRIPT)


@pytest.mark.parametrize("prefix", ["~", "HOME"])
@pytest.mark.parametrize("response", [{"stdout": "checks ok\nREADY: merge origin/main\n"}, "READY: as text"])
def test_merge_origin_merges_after_the_exact_check_said_ready(merge, home, cwd, prefix, response):
	"""Unit: the exact check command whose output says READY: runs the installed script with --merge in the
	session's cwd; its exit code and output come back as context."""
	out = merge.check(bash(command(merge, home, prefix), cwd, tool_response=response))
	assert contextOf(out) == "merge-origin hook (outside the sandbox, exit 1):\nCONFLICT in a.py"
	assert calls(home) == [{"argv": ["--merge"], "cwd": str(cwd)}]


@pytest.mark.parametrize("change", [
	lambda c: {"tool_response": {"stdout": "FAIL: dirty tree"}},
	lambda c: {"tool_response": {}},
	lambda c: {"tool_input": {"command": f"{c} --merge"}},
	lambda c: {"tool_input": {"command": f"{c}; git push"}},
	lambda c: {"tool_name": "Edit"},
	lambda c: {"hook_event_name": "PreToolUse"},
], ids=["fail", "no-output", "merge-flag", "chained", "other-tool", "other-event"])
def test_merge_origin_runs_nothing_without_ready_or_for_another_command(merge, home, cwd, change):
	"""Unit: no READY: in the check's output, another command, another tool or event: no output, no merge."""
	data = {**bash(command(merge, home), cwd, tool_response={"stdout": "READY: x"}), **change(command(merge, home))}
	assert merge.check(data) is None
	assert calls(home) == []


def test_merge_origin_reports_a_missing_cwd_without_merging(merge, home, tmp_path):
	"""Unit: a cwd that doesn't exist is reported as FAIL and nothing runs."""
	out = merge.check(bash(command(merge, home), tmp_path / "missing", tool_response={"stdout": "READY: x"}))
	assert contextOf(out) == "merge-origin hook: FAIL: session cwd not found; merge not run."
	assert calls(home) == []

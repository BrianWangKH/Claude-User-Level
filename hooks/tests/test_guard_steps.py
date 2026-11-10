"""The PreToolUse guard steps of hook-entry.py: every step of the `pre` and `handback` entries fails closed
except the side-effect-only ones (hook_entries.ENTRIES policy), and the rules of write-scope (where Write / Edit
may write) and plan-guard (who may read or write plans/).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_guard_steps.py -q -p no:cacheprovider'
"""

import json
import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import hook_entries  # noqa: E402
import hookio  # noqa: E402
import plan_guard  # noqa: E402
import write_scope  # noqa: E402

ENTRY = os.path.join(HOOKS, "hook-entry.py")
# PreToolUse steps whose only job is a side effect, so their failure must not deny the call (and why).
SIDE_EFFECT_ONLY = {
	"mutation-start": "starts the code-reviewer's automatic mutation scan in the background (a spawn step); it "
	"decides nothing about the call, and the hand-back gate (handback entry, mutation) still checks the scan",
}


def git(repo, *args):
	subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
		capture_output=True)


def outcome(module, data):
	"""A check's output: its return value, or the output of the hookio.Answer it raised."""
	try:
		return module.check(data)
	except hookio.Answer as answer:
		return answer.output


def denial(output):
	"""The deny reason of a PreToolUse output, or None when it doesn't deny."""
	specific = (output or {}).get("hookSpecificOutput") or {}
	return specific.get("permissionDecisionReason") if specific.get("permissionDecision") == "deny" else None


# -- every PreToolUse step fails closed ---------------------------------------------------------------


def preToolUseSteps():
	return [(name, s) for name, entry in hook_entries.ENTRIES.items() if entry["event"] == "PreToolUse"
		for s in entry["steps"]]


def test_every_pre_tool_use_step_fails_closed_except_the_side_effect_ones():
	"""Unit: a guard step switched to policy "record" would let its call through when it crashes (fail open);
	every step of the PreToolUse entries is "guard", except the side-effect-only steps named above."""
	steps = preToolUseSteps()
	assert {name for name, _s in steps} == {"pre", "handback"}
	names = {s["name"] for _name, s in steps}
	assert set(SIDE_EFFECT_ONLY) <= names
	for name, s in steps:
		if s["name"] in SIDE_EFFECT_ONLY:
			assert (s["policy"], s["kind"]) == ("record", "spawn"), f"{name}/{s['name']}"
		else:
			assert s["policy"] == "guard", f"{name}/{s['name']} must fail closed"


def test_a_crashing_write_scope_step_denies_the_write(tmp_path):
	"""Integration: hook-entry.py runs write-scope alone (check write-scope pre) on input that makes it raise; the
	call is denied with the step's error, not let through."""
	repo = tmp_path / "repo"
	repo.mkdir()
	environ = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	environ.update({"HOME": str(tmp_path / "home"), "CLAUDE_PROJECT_DIR": str(repo)})
	data = {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": ["not", "an", "object"],
		"cwd": str(repo), "session_id": "s"}
	result = subprocess.run([sys.executable, ENTRY, "check", "write-scope", "pre"], input=json.dumps(data),
		capture_output=True, text=True, env=environ, cwd=str(repo), timeout=60)
	assert result.returncode == 0, result.stderr
	reason = denial(json.loads(result.stdout))
	assert reason is not None, result.stdout
	assert reason.startswith("write-scope: AttributeError") and reason.endswith("blocked (fail closed)")


# -- write-scope: where Write / Edit / NotebookEdit may write -----------------------------------------


@pytest.fixture
def places(tmp_path, monkeypatch):
	"""A home and a project directory (not a git repository) as the session's."""
	home, project = tmp_path / "home", tmp_path / "proj"
	home.mkdir()
	project.mkdir()
	monkeypatch.setenv("HOME", str(home))
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
	return home, project


def write(path, tool="Write", **extra):
	key = "notebook_path" if tool == "NotebookEdit" else "file_path"
	return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: str(path)}, **extra}


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_write_scope_says_nothing_inside_the_project(places, tool):
	"""Unit: inside the checkout the step has no say (None), so the normal permission flow decides."""
	_home, project = places
	assert outcome(write_scope, write(project / "src" / "a.py", tool)) is None
	assert outcome(write_scope, write("src/relative.py", tool)) is None  # relative: against the checkout


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_write_scope_denies_a_write_outside_the_allowed_directories(places, tmp_path, tool):
	"""Unit: a path outside every allowed directory is denied, naming the resolved target."""
	reason = denial(outcome(write_scope, write(tmp_path / "elsewhere" / "x.py", tool)))
	assert reason == f"Write outside allowed directories blocked: {tmp_path / 'elsewhere' / 'x.py'}"


def test_write_scope_compares_resolved_paths_not_prefixes(places, tmp_path):
	"""Unit: a sibling sharing the checkout's name as a prefix, a `..` escape and a symlink out of the checkout
	are all outside."""
	_home, project = places
	outside = tmp_path / "outside"
	outside.mkdir()
	os.symlink(outside, project / "link")
	for path in (tmp_path / "proj-evil" / "x.py", project / ".." / "outside" / "x.py", project / "link" / "x.py"):
		assert denial(outcome(write_scope, write(path))) is not None, path


def test_write_scope_allows_the_home_plans_agent_memory_and_the_scratchpad(places, tmp_path):
	"""Unit: ~/.claude/plans, ~/.claude/agent-memory and the session's scratchpad_dir are allowed."""
	home, _project = places
	scratch = tmp_path / "scratch"
	assert outcome(write_scope, write(home / ".claude" / "plans" / "p.md")) is None
	assert outcome(write_scope, write(home / ".claude" / "agent-memory" / "a" / "m.md")) is None
	assert outcome(write_scope, write(scratch / "n.txt", scratchpad_dir=str(scratch))) is None
	assert denial(outcome(write_scope, write(scratch / "n.txt"))) is not None  # no scratchpad in this input
	assert denial(outcome(write_scope, write(home / ".claude" / "settings.json"))) is not None


def test_write_scope_allows_only_markdown_in_a_projects_memory_directory(places):
	"""Unit: auto memory is ~/.claude/projects/<slug>/memory/**/*.md, nothing else under projects/."""
	home, _project = places
	projects = home / ".claude" / "projects"
	assert outcome(write_scope, write(projects / "slug" / "memory" / "MEMORY.md")) is None
	assert outcome(write_scope, write(projects / "slug" / "memory" / "deep" / "note.md")) is None
	assert denial(outcome(write_scope, write(projects / "slug" / "memory" / "note.txt"))) is not None
	assert denial(outcome(write_scope, write(projects / "slug" / "transcript.md"))) is not None
	assert denial(outcome(write_scope, write(projects / "memory" / "x.md"))) is not None


def test_write_scope_denies_without_a_project_dir_and_ignores_a_call_without_a_path(places, monkeypatch):
	"""Unit: no CLAUDE_PROJECT_DIR denies (the checkout is never guessed); a call naming no path has no say."""
	_home, project = places
	assert outcome(write_scope, {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {}}) is None
	monkeypatch.delenv("CLAUDE_PROJECT_DIR")
	assert denial(outcome(write_scope, write(project / "a.py"))) == "CLAUDE_PROJECT_DIR not set; refusing write"


def test_write_scope_allows_the_linked_worktrees_only_from_the_main_checkout(places, tmp_path, monkeypatch):
	"""Unit: the main checkout may write into its linked worktrees; a linked worktree may not write into
	another one (nor into the main checkout)."""
	_home, project = places
	git(project, "init", "-q", "-b", "main")
	git(project, "commit", "-q", "--allow-empty", "-m", "base")
	first, second = tmp_path / "worktrees" / "first", tmp_path / "worktrees" / "second"
	git(project, "worktree", "add", "-q", "-b", "first", str(first))
	git(project, "worktree", "add", "-q", "-b", "second", str(second))
	assert outcome(write_scope, write(first / "a.py")) is None
	assert outcome(write_scope, write(second / "a.py")) is None
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(first))
	assert outcome(write_scope, write(first / "a.py")) is None
	assert denial(outcome(write_scope, write(second / "a.py"))) is not None
	assert denial(outcome(write_scope, write(project / "a.py"))) is not None


# -- plan-guard: who may read or write plans/ ---------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
	p = tmp_path / "proj"
	(p / "plans" / "tests").mkdir(parents=True)
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(p))
	return p


def call(tool, agentType, path=None, cwd=None, **toolInput):
	key = {"Grep": "path", "Glob": "path", "NotebookEdit": "notebook_path"}.get(tool, "file_path")
	data = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": dict(toolInput)}
	if path is not None:
		data["tool_input"][key] = str(path)
	if agentType is not None:
		data["agent_type"] = agentType
	if cwd is not None:
		data["cwd"] = str(cwd)
	return data


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit"])
def test_the_code_reviewer_never_touches_plans(project, tool):
	"""Unit: plan-blind review: every read or write under plans/ is denied; elsewhere it has no say."""
	reason = denial(outcome(plan_guard, call(tool, "code-reviewer", project / "plans" / "topic.md")))
	assert reason == "Plan-blind review: code-reviewer may not access plans/."
	assert denial(outcome(plan_guard, call(tool, "code-reviewer", project / "plans"))) is not None
	assert outcome(plan_guard, call(tool, "code-reviewer", project / "src" / "a.py")) is None


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Edit", "Write"])
def test_the_verifier_reads_the_hook_copied_plan_never_the_worktrees(project, tool):
	"""Unit: the verifier may not read plans/ (the worker can edit it); the copy under .hook-only is allowed."""
	reason = denial(outcome(plan_guard, call(tool, "verifier", project / "plans" / "topic.md")))
	assert reason.startswith("verifier: read the approved plan in .hook-only/verifier-<run>/plan.md")
	assert outcome(plan_guard, call(tool, "verifier", project / ".hook-only" / "verifier-1" / "plan.md")) is None


def test_a_relative_path_resolves_against_the_calls_cwd(project):
	"""Unit: a relative path is taken from the call's cwd, so `plans/x.md` from the checkout is under plans/."""
	assert denial(outcome(plan_guard, call("Read", "code-reviewer", "plans/x.md", cwd=project))) is not None
	assert outcome(plan_guard, call("Read", "code-reviewer", "plans/x.md", cwd=project / "src")) is None


@pytest.mark.parametrize("agentType", ["qa-planner", "qa"])
def test_the_test_planners_write_only_under_plans_tests(project, agentType):
	"""Unit: qa-planner and qa may write plans/tests/ and read all of plans/; any other write there is denied."""
	assert outcome(plan_guard, call("Write", agentType, project / "plans" / "tests" / "t.md")) is None
	assert outcome(plan_guard, call("Read", agentType, project / "plans" / "topic.md")) is None
	reason = denial(outcome(plan_guard, call("Edit", agentType, project / "plans" / "topic.md")))
	assert reason == f"{agentType} may only write under plans/tests/."


def test_the_developer_reads_plans_but_never_writes_them(project):
	"""Unit: the developer reads the plan; every write under plans/ (tests included) is denied."""
	assert outcome(plan_guard, call("Read", "developer", project / "plans" / "topic.md")) is None
	for path in (project / "plans" / "topic.md", project / "plans" / "tests" / "t.md"):
		assert denial(outcome(plan_guard, call("Write", "developer", path))) == "developer may not write under plans/."


@pytest.mark.parametrize("agentType", [None, "claude"])
def test_the_main_session_may_not_pass_arguments_to_blind_review(project, agentType):
	"""Unit: the main session (no agent_type, or "claude") may call blind-review only without arguments; it
	may read and write plans/."""
	reason = denial(outcome(plan_guard, call("Skill", agentType, skill="blind-review", args="base abc")))
	assert reason == "blind-review takes no arguments; the reviewer finds the base itself."
	assert outcome(plan_guard, call("Skill", agentType, skill="blind-review", args="  ")) is None
	assert outcome(plan_guard, call("Skill", agentType, skill="other", args="x")) is None
	assert outcome(plan_guard, call("Write", agentType, project / "plans" / "topic.md")) is None


def test_an_internal_agent_with_an_empty_type_is_ignored(project):
	"""Unit: agent_type "" (Claude Code's internal agents) is never checked."""
	assert outcome(plan_guard, call("Read", "", project / "plans" / "topic.md")) is None


def test_plan_guard_denies_when_it_fails(project):
	"""Unit: an error while deciding denies the call with the error (its own catch)."""
	reason = denial(outcome(plan_guard, {"hook_event_name": "PreToolUse", "tool_name": "Read",
		"agent_type": "code-reviewer", "tool_input": ["not", "an", "object"]}))
	assert reason.startswith("plan-guard failed (AttributeError:")

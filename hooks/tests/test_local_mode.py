"""Local mode: the local main is the integration branch (homeconfig.integrationBranch), and
auto-merge.py merges a finished branch into it when every recorded condition holds. The mode comes from the
records only: the topic's (.hook-only/flow-mode, local_fixtures.recordRun) or the main checkout's; in this test
process the stated mode is the default of a main checkout without a record (conftest.py).

Run: uv run --offline --no-project --with pytest --with pytest-cov==7.1.0 python -m pytest ~/.claude/hooks/tests/test_local_mode.py
Inside Claude Code's sandbox bwrap can't nest: the programs then run from a tree copy whose local.json names the
tests' stand-in bwrap (config_fixtures.gateHome).
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

import config_fixtures
from local_fixtures import (  # noqa: F401  (project, toolsHome: fixtures)
	PY,
	approve,
	autoMerge,
	endRun,
	git,
	loadHook,
	makeProject,
	nextStep,
	project,
	recordRun,
	refused,
	setMainHead,
	stepResult,
	toolsHome,
	verifySpec,
	write,
)

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.gateHome(), "hooks")
RUN_SKILLS = os.path.join(config_fixtures.gateHome(), "skills")
RUN_LIB = os.path.join(RUN_HOOKS, "lib")
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
LIB = os.path.join(HOOKS, "lib")  # the tree under test's hooks/lib: homepath adds config/ and the role directories
sys.path.insert(0, LIB)
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)

import homeconfig  # noqa: E402
import qg_common  # noqa: E402


# --- integration branch -------------------------------------------------------------------------

def baseRef(repo, local):
	"""base-ref.sh in repo with the main checkout's record set to the mode (no variable is a source)."""
	(repo / ".hook-only").mkdir(exist_ok=True)
	(repo / ".hook-only" / "flow-mode").write_text("local\n" if local else "origin\n")
	return subprocess.run(["bash", os.path.join(RUN_SKILLS, "feature-flow", "base-ref.sh")], cwd=repo, capture_output=True,
		text=True)


def test_base_ref_follows_the_mode(project):
	main, _ = project
	git(main, "update-ref", "refs/remotes/origin/main", "HEAD")
	assert baseRef(main, local=True).stdout.strip() == "main"
	assert baseRef(main, local=False).stdout.strip() == "origin/main"


def test_integration_ref_needs_only_its_own_branch(project, flowMode):
	# no origin/main here: local mode needs none; origin mode never falls back to the local main
	main, wt = project
	(wt / ".hook-only" / "flow-mode").write_text(flowMode + "\n")  # the topic's mode (origin: no override)
	assert homeconfig.integrationBranch(str(main)) == ("main" if flowMode == "local" else None)
	if flowMode == "local":
		assert qg_common.defaultBase(str(wt)) == git(main, "rev-parse", "main")
		assert qg_common.loadConfig(str(wt))[0] is not None
	else:
		assert qg_common.loadConfig(str(wt)) == (None, None)


def test_integration_ref_follows_the_mode(project, flowMode):
	main, wt = project
	(wt / ".hook-only" / "flow-mode").write_text(flowMode + "\n")  # the topic's mode (origin: no override)
	git(main, "update-ref", "refs/remotes/origin/main", git(wt, "rev-parse", "HEAD"))  # origin/main != main
	ref, base = {"local": ("main", git(main, "rev-parse", "main")),
		"origin": ("origin/main", git(wt, "rev-parse", "HEAD"))}[flowMode]
	assert homeconfig.integrationBranch(str(main)) == ref
	assert qg_common.defaultBase(str(wt)) == base
	assert qg_common.loadConfig(str(wt))[0] is not None


def test_review_prompt_names_the_mode_base(tmp_path, flowMode):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q")
	data = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(repo), "session_id": "s",
		"tool_input": {"subagent_type": "code-reviewer", "prompt": "Budget: 5 min, $1"}}
	(repo / ".hook-only").mkdir()
	(repo / ".hook-only" / "flow-mode").write_text(flowMode + "\n")  # the repo's mode, as the hook reads it
	env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "hook-entry.py"), "check", "dispatch-guard"], input=json.dumps(data),
		capture_output=True, text=True, env=env)
	prompt = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["prompt"]
	if flowMode == "local":
		assert "local default branch" in prompt and "origin" not in prompt.split("Budget:")[0]
	else:
		assert "origin's default branch" in prompt and "`git merge-base origin/master HEAD`" in prompt
		assert "local default branch" not in prompt


# --- verdicts -----------------------------------------------------------------------------------

@pytest.mark.parametrize("message,verdict", [
	("Findings: none.\n\nVerdict: APPROVE\n", "APPROVE"),
	("**Verdict:** CHANGES REQUESTED\n", "CHANGES REQUESTED"),
	("2. A verdict: APPROVE or CHANGES REQUESTED.\nAPPROVE\n", "CHANGES REQUESTED"),
	("no verdict here", "NONE"),
	# VERDICT ignores case: a lower-case verdict line asking for changes refuses, beside an APPROVE line too
	("verdict: changes requested\n", "CHANGES REQUESTED"),
	("Verdict: APPROVE\n\nverdict: changes requested\n", "CHANGES REQUESTED"),
])
def test_agent_guard_records_the_reviewer_verdict(tmp_path, message, verdict):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q")
	guard = loadHook("agent_guard.py", "agent_guard")
	guard.recordVerdict({"agent_type": "code-reviewer", "agent_id": "r9", "last_assistant_message": message}, str(repo))
	record = json.loads((repo / ".hook-only" / "reviews.jsonl").read_text().splitlines()[-1])
	assert record["verdict"] == verdict and record["agent_id"] == "r9"


def test_agent_guard_ignores_other_agents(tmp_path):
	guard = loadHook("agent_guard.py", "agent_guard")
	guard.recordVerdict({"agent_type": "spec-reviewer", "last_assistant_message": "APPROVE"}, str(tmp_path))
	assert not (tmp_path / ".hook-only" / "reviews.jsonl").exists()


def guardEvent(repo, event, **fields):
	data = {"hook_event_name": event, "cwd": str(repo), "session_id": "s", "agent_type": "code-reviewer",
		"agent_id": "rh", **fields}
	return subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "hook-entry.py"), "check", "agent-guard"], input=json.dumps(data),
		capture_output=True, text=True)


def recorded(repo):
	path = repo / ".hook-only" / "reviews.jsonl"
	return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def handbackRepo(tmp_path):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q")
	git(repo, "commit", "-q", "--allow-empty", "-m", "base")
	return repo


def test_agent_guard_records_the_verdict_from_the_hand_back(tmp_path):
	# Claude Code v2.1.271+: the report travels in SubagentHandback's tool_input.message, and
	# last_assistant_message at SubagentStop holds only the closing text (seen: NONE x3)
	repo = handbackRepo(tmp_path)
	out = guardEvent(repo, "PostToolUse", tool_name="SubagentHandback",
		tool_input={"message": "Findings: none.\n\nVerdict: APPROVE"})
	assert out.returncode == 0, out.stderr
	assert [(r["verdict"], r["via"]) for r in recorded(repo)] == [("APPROVE", "handback")]


def test_agent_guard_stop_after_a_hand_back_keeps_its_verdict(tmp_path):
	repo = handbackRepo(tmp_path)
	guardEvent(repo, "PostToolUse", tool_name="SubagentHandback", tool_input={"message": "APPROVE"})
	guardEvent(repo, "SubagentStop", last_assistant_message="Done.", stop_hook_active=False)
	assert [r["verdict"] for r in recorded(repo)] == ["APPROVE"]


def test_agent_guard_stop_records_again_once_head_moved(tmp_path):
	repo = handbackRepo(tmp_path)
	guardEvent(repo, "PostToolUse", tool_name="SubagentHandback", tool_input={"message": "APPROVE"})
	git(repo, "commit", "-q", "--allow-empty", "-m", "fix")
	guardEvent(repo, "SubagentStop", last_assistant_message="CHANGES REQUESTED", stop_hook_active=False)
	assert [r["verdict"] for r in recorded(repo)] == ["APPROVE", "CHANGES REQUESTED"]


def test_agent_guard_stop_without_a_hand_back_still_records(tmp_path):
	repo = handbackRepo(tmp_path)
	guardEvent(repo, "SubagentStop", last_assistant_message="Verdict: APPROVE", stop_hook_active=False)
	assert [(r["verdict"], r["via"]) for r in recorded(repo)] == [("APPROVE", "stop")]


def test_agent_guard_ignores_other_tools_and_agents_after_use(tmp_path):
	repo = handbackRepo(tmp_path)
	guardEvent(repo, "PostToolUse", tool_name="Bash", tool_input={"message": "APPROVE"})
	guardEvent(repo, "PostToolUse", tool_name="SubagentHandback", agent_type="developer",
		tool_input={"message": "APPROVE"})
	assert recorded(repo) == []


# --- auto-merge ---------------------------------------------------------------------------------

def test_auto_merge_merges_a_reviewed_green_branch(project):
	main, wt = project
	decision = autoMerge(main, wt)
	assert decision["decision"] == "merged", decision
	assert git(main, "rev-parse", "HEAD") == decision["commit"]
	assert git(main, "log", "-1", "--format=%P").count(" ") == 1
	assert "Add more()" in git(main, "log", "-1", "--format=%B")


# Without quality-gates.json (stage 3b): the languages detected from the tracked files run on their home
# profiles. The hook runs from a copy of the tree (its config without the person's files), a fake uv as the
# profile's {uv} (the copy's local.json), and no bwrap for these runs: the fake tool lives under the test's tmp,
# which bwrap would hide.

def homeConfig(tmp_path, uv=None, profiles=True):
	"""autoMerge's arguments for a hook run from a tree copy: {uv} set to the given program, no toolchain profiles
	at all when profiles is False, the stand-in bwrap (the fake tools live under tmp, which bwrap hides)."""
	tree = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home")
	d = tree / "config"
	if uv:
		(d / "local.json").write_text(json.dumps({"toolchains": {"tools": {"uv": uv}}}))
	if not profiles:
		home = json.loads((d / "home.json").read_text())
		config_fixtures.writeSection(d, "toolchains", {**home["toolchains"], "languages": {}})
	config_fixtures.useStandIn(d)
	return {"env": {}, "home": tree}


def fakeTool(path, body, shebang=None):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(f"#!{shebang or sys.executable}\nimport sys\n{textwrap.dedent(body)}")
	path.chmod(0o755)
	return str(path)


def test_without_quality_gates_detected_languages_merge_on_their_profile_tests(tmp_path):
	main, wt = makeProject(tmp_path, gates=False)
	log = tmp_path / "uv.log"
	uv = fakeTool(tmp_path / "bin" / "uv", f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
	decision = autoMerge(main, wt, **homeConfig(tmp_path, uv))
	assert decision["decision"] == "merged", decision
	assert "pytest" in log.read_text()  # the python profile's test command ran


def test_without_quality_gates_and_without_a_profiled_language_it_refuses(tmp_path):
	main, wt = makeProject(tmp_path, gates=False)
	reason = refused(main, wt, **homeConfig(tmp_path, profiles=False))
	assert reason.startswith(f"nothing to gate: no {qg_common.CONFIG_FILE}")


def test_a_profile_test_command_that_never_starts_refuses_naming_the_key(tmp_path):
	main, wt = makeProject(tmp_path, gates=False)
	uv = fakeTool(tmp_path / "bin" / "uv", "sys.exit(0)\n", shebang="/nonexistent/python")
	reason = refused(main, wt, **homeConfig(tmp_path, uv))
	assert "the python test command didn't start" in reason
	assert "languages.python.test (value from the home profile" in reason and "enabled automatically" in reason


def test_refuses_without_approval(project):
	main, wt = project
	approve(wt, "CHANGES REQUESTED")
	assert "no APPROVE" in refused(main, wt)


def test_refuses_source_changed_after_approval(project):
	main, wt = project
	write(wt, "pkg/more.py", "def more(x):\n    return x + 2\n")
	write(wt, "tests/test_more.py", "from pkg.more import more\n\n\ndef test_more():\n    assert more(1) == 3\n")
	git(wt, "commit", "-q", "-am", "late change")
	assert "source changed after the approved commit" in refused(main, wt)


def test_refuses_held_paths(project):
	main, wt = project
	write(wt, ".claude/settings.json", "{}")
	write(wt, "pkg/secret/keys.py", "X = 1\n")
	git(wt, "add", ".")
	git(wt, "commit", "-q", "-m", "config")
	approve(wt)
	reason = refused(main, wt)
	assert ".claude/settings.json" in reason and "pkg/secret/keys.py" in reason


def test_refuses_new_urls_in_source(project):
	main, wt = project
	write(wt, "pkg/more.py", "URL = 'https://example.com/upload'\n\n\ndef more(x):\n    return x + 1\n")
	git(wt, "commit", "-q", "-am", "url")
	approve(wt)
	assert "new URLs" in refused(main, wt)


def test_refuses_when_main_moved_during_the_run(project):
	main, wt = project
	write(main, "README.md", "x\n")
	git(main, "add", "README.md")
	git(main, "commit", "-q", "-m", "moved")
	assert "moved during the run" in refused(main, wt)


def test_refuses_a_branch_without_the_latest_main(project):
	main, wt = project
	write(main, "README.md", "x\n")
	git(main, "add", "README.md")
	git(main, "commit", "-q", "-m", "earlier")
	setMainHead(main, wt)
	assert "doesn't contain main" in refused(main, wt)


def test_a_branch_behind_main_asks_worker_control_for_a_merge_run(project):
	main, wt = project
	write(main, "README.md", "x\n")
	git(main, "add", "README.md")
	git(main, "commit", "-q", "-m", "earlier")
	setMainHead(main, wt)
	decision = autoMerge(main, wt)
	assert decision["merge_run"] is True
	# auto-merge.py only decides (migration step 6); the next step asks worker-control
	assert not (wt / ".hook-only" / "runs" / "auto-1.json").exists()
	# no worker state in this fixture: worker-control answers, refusing, so nothing starts
	assert nextStep(main, wt)["decision"] == "refused"


def test_other_refusals_never_ask_for_a_merge_run(project):
	main, wt = project
	(wt / ".hook-only" / "agent-guard").mkdir()
	(wt / ".hook-only" / "agent-guard" / "HOLD").write_text("x")
	assert "merge_run" not in autoMerge(main, wt)
	assert not (wt / ".hook-only" / "runs" / "auto-1.json").exists()


def test_refuses_a_red_suite(project):
	main, wt = project
	write(wt, "tests/test_more.py", "from pkg.more import more\n\n\ndef test_more():\n    assert more(1) == 5\n")
	git(wt, "commit", "-q", "-am", "red")
	assert "suite fails" in refused(main, wt)


# --- checks: format and lint after the suite ----------------------------------------

def test_passing_checks_let_the_merge_through(tmp_path):
	main, wt = makeProject(tmp_path, [["sh", "-c", "test -f {repo}/pkg/more.py"],
		{"argv": ["sh", "-c", "true"], "empty_output": True}])
	assert autoMerge(main, wt)["decision"] == "merged"


def test_a_failing_check_refuses_and_names_it(tmp_path):
	main, wt = makeProject(tmp_path, [["true"], ["sh", "-c", "echo would reformat pkg/more.py; exit 1"]])
	reason = refused(main, wt)
	assert "python check" in reason and "would reformat" in reason and "exit 1" in reason


def test_an_empty_output_check_that_prints_refuses(tmp_path):
	main, wt = makeProject(tmp_path, [{"argv": ["sh", "-c", "echo pkg/more.py"], "empty_output": True}])
	reason = refused(main, wt)
	assert "must print nothing" in reason and "pkg/more.py" in reason


def test_output_without_empty_output_is_fine(tmp_path):
	main, wt = makeProject(tmp_path, [["sh", "-c", "echo all good"]])
	assert autoMerge(main, wt)["decision"] == "merged"


def test_checks_run_only_after_a_green_suite(tmp_path):
	main, wt = makeProject(tmp_path, [["false"]])
	write(wt, "tests/test_more.py", "from pkg.more import more\n\n\ndef test_more():\n    assert more(1) == 5\n")
	git(wt, "commit", "-q", "-am", "red")
	assert "suite fails" in refused(main, wt)


@pytest.mark.parametrize("checks", [{"argv": ["true"]}, [[]], [{"argv": ["true"], "empty_output": "yes"}],
	[{"cmd": ["true"]}]])
def test_a_malformed_check_refuses(tmp_path, checks):
	main, wt = makeProject(tmp_path, checks)
	assert "malformed check" in refused(main, wt)


def test_refuses_with_a_hold(project):
	main, wt = project
	(wt / ".hook-only" / "agent-guard").mkdir()
	(wt / ".hook-only" / "agent-guard" / "HOLD").write_text("x")
	assert "agent-guard HOLD pending" in refused(main, wt)


def test_refuses_outside_local_mode(project):
	main, wt = project
	(wt / ".hook-only" / "flow-mode").write_text("origin\n")  # the topic's record; no test override below
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-merge.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, env={**{k: v for k, v in os.environ.items() if k != "FLOW_INTEGRATION"},
		"WT_MAIN_DIR": str(main), "PYTHONPATH": RUN_LIB})
	assert result.returncode == 0
	assert "not in local mode" in json.loads((wt / ".hook-only" / "runs" / "merge-1.json").read_text())["reason"]


# --- fixes after review --------------------------------------------------------------

def mergeMainIntoBranch(main, wt, message="merge main"):
	return subprocess.run(["git", "-C", str(wt), "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "-m", message,
		"main"], capture_output=True, text=True)


def test_main_moved_only_by_an_automatic_merge_asks_for_a_merge_run(project):
	main, wt = project
	write(main, "README.md", "x\n")
	git(main, "add", "README.md")
	git(main, "commit", "-q", "-m", "other topic")
	(main / ".hook-only" / "auto-merge").mkdir(parents=True)
	(main / ".hook-only" / "auto-merge" / "merges.jsonl").write_text(json.dumps({"commit": git(main, "rev-parse", "HEAD")}) + "\n")
	reason = refused(main, wt)
	assert "merge run is needed" in reason and "reflog" not in reason


def test_an_automatic_merge_of_main_after_approval_still_merges(project):
	main, wt = project
	write(main, "pkg/other.py", "def other(x):\n    return x\n")
	git(main, "add", "pkg/other.py")
	git(main, "commit", "-q", "-m", "other topic")
	assert mergeMainIntoBranch(main, wt).returncode == 0
	setMainHead(main, wt)
	assert autoMerge(main, wt)["decision"] == "merged"


def handResolvedMerge(main, wt):
	write(main, "pkg/calc.py", "def calc(x):\n    return x * 1\n")
	git(main, "commit", "-q", "-am", "main side")
	write(wt, "pkg/calc.py", "def calc(x):\n    return x + 0\n")
	git(wt, "commit", "-q", "-am", "branch side")
	approve(wt)
	assert mergeMainIntoBranch(main, wt).returncode != 0
	write(wt, "pkg/calc.py", "def calc(x):\n    return int(x)\n")
	git(wt, "add", "pkg/calc.py")
	git(wt, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--no-edit")
	setMainHead(main, wt)


def test_a_hand_resolved_merge_needs_a_merge_review(project):
	main, wt = project
	handResolvedMerge(main, wt)
	assert "without a merge-review APPROVE" in refused(main, wt)


def test_a_hand_resolved_merge_with_a_merge_review_merges(project):
	main, wt = project
	handResolvedMerge(main, wt)
	with open(wt / ".hook-only" / "reviews.jsonl", "a", encoding="utf-8") as f:
		f.write(json.dumps({"verdict": "APPROVE", "head": git(wt, "rev-parse", "HEAD"), "kind": "merge"}) + "\n")
	assert autoMerge(main, wt)["decision"] == "merged"


def test_a_merge_review_never_replaces_the_full_one(project):
	main, wt = project
	with open(wt / ".hook-only" / "reviews.jsonl", "w", encoding="utf-8") as f:
		f.write(json.dumps({"verdict": "APPROVE", "head": git(wt, "rev-parse", "HEAD"), "kind": "merge"}) + "\n")
	assert "no APPROVE" in refused(main, wt)


def test_urls_in_scripts_are_held_but_not_in_docs(project):
	main, wt = project
	write(wt, "README.md", "See https://example.com/docs\n")
	git(wt, "add", "README.md")
	git(wt, "commit", "-q", "-m", "docs")
	assert autoMerge(main, wt)["decision"] == "merged"


def docsBranch(main, wt, files):
	"""The worktree branch reset to main with only files committed, and no review recorded."""
	git(wt, "reset", "-q", "--hard", "main")
	for path, text in files.items():
		write(wt, path, text)
		git(wt, "add", path)
	git(wt, "commit", "-q", "-m", "docs")
	(wt / ".hook-only" / "reviews.jsonl").unlink()


def test_a_docs_only_branch_merges_without_a_review(project):
	main, wt = project
	docsBranch(main, wt, {"README.md": "# Usage\n", "docs/notes.txt": "notes\n"})
	decision = autoMerge(main, wt)
	assert decision["decision"] == "merged", decision
	assert decision["review"] == "docs-only"


def test_a_reviewed_merge_records_approved(project):
	main, wt = project
	assert autoMerge(main, wt)["review"] == "approved"


@pytest.mark.parametrize("extra", [{"config/params.toml": "fee = 0.1\n"}, {"tests/data/expected.txt": "1\n"}])
def test_docs_with_a_config_or_test_file_still_need_a_review(project, extra):
	main, wt = project
	docsBranch(main, wt, {"README.md": "# Usage\n", **extra})
	assert "no APPROVE" in refused(main, wt)


def test_a_docs_only_branch_honours_a_recorded_verdict(project):
	main, wt = project
	docsBranch(main, wt, {"README.md": "# Usage\n"})
	approve(wt, "CHANGES REQUESTED")
	assert "no APPROVE" in refused(main, wt)


def test_a_docs_only_branch_still_holds_claude_paths(project):
	main, wt = project
	docsBranch(main, wt, {".claude/skills/x/SKILL.md": "# x\n"})
	assert ".claude/skills/x/SKILL.md" in refused(main, wt)


def test_urls_in_a_shell_script_are_held(project):
	main, wt = project
	write(wt, "scripts/upload.sh", "curl -d @data https://example.com/upload\n")
	git(wt, "add", "scripts/upload.sh")
	git(wt, "commit", "-q", "-m", "script")
	assert "scripts/upload.sh" in refused(main, wt)


def test_agent_guard_records_a_merge_review_kind(tmp_path):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q")
	guard = loadHook("agent_guard.py", "agent_guard")
	directory = str(repo / ".hook-only" / "agent-guard")
	os.makedirs(directory)
	open(os.path.join(directory, "merge-review-pending"), "w").close()
	guard.onStart({"agent_id": "r5", "agent_type": "code-reviewer"}, str(repo), directory)
	assert not os.path.exists(os.path.join(directory, "merge-review-pending"))
	guard.recordVerdict({"agent_type": "code-reviewer", "agent_id": "r5", "last_assistant_message": "Verdict: APPROVE"},
		str(repo), guard.reviewKind(directory, "r5"))
	assert json.loads((repo / ".hook-only" / "reviews.jsonl").read_text().splitlines()[-1])["kind"] == "merge"


def reviewDispatch(repo, prompt):
	data = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(repo), "session_id": "s",
		"tool_input": {"subagent_type": "code-reviewer", "prompt": prompt}}
	env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "hook-entry.py"), "check", "dispatch-guard"], input=json.dumps(data),
		capture_output=True, text=True, env=env)
	return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None


def test_merge_review_prompt_is_kept_only_on_a_merge_commit(project):
	main, wt = project
	out = reviewDispatch(wt, "Merge review: look at this\nBudget: 5 min, $1")
	assert out and "plan-blind" in out["permissionDecisionReason"]
	write(main, "pkg/other.py", "X = 1\n")
	git(main, "add", "pkg/other.py")
	git(main, "commit", "-q", "-m", "other")
	assert mergeMainIntoBranch(main, wt).returncode == 0
	assert reviewDispatch(wt, "Merge review: look at this\nBudget: 5 min, $1") is None
	assert (wt / ".hook-only" / "agent-guard" / "merge-review-pending").exists()


# --- real-data checks (verify.py) and the automatic close (auto-finish.py) ------------


def runVerify(main, wt):
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "gates", "verify.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=600, env={**os.environ,
		"WT_MAIN_DIR": str(main), "PYTHONPATH": RUN_LIB})
	assert result.returncode == 0, result.stderr
	path = wt / ".hook-only" / "runs" / "verify-1.json"
	return json.loads(path.read_text()) if path.exists() else None


DETERMINISTIC = "import os; open(os.environ['DATA_ROOT'] + '/out.txt', 'w').write(open(os.environ['DATA_ROOT'] + '/features/x.txt').read())"
RANDOM = "import os, time; open(os.environ['DATA_ROOT'] + '/out.txt', 'w').write(str(time.time_ns()))"


def test_verify_passes_a_deterministic_command_on_the_real_data_copy(project):
	main, wt = project
	verifySpec(main, [{"name": "det", "run": [PY, "-c", DETERMINISTIC], "repeat_same": ["out.txt"]}])
	result = runVerify(main, wt)
	assert result["passed"] is True and result["checks"][0]["ok"]
	assert not (main / "data" / "out.txt").exists()  # the real data is never written


def test_verify_fails_output_that_differs_between_runs(project):
	main, wt = project
	verifySpec(main, [{"name": "rand", "run": [PY, "-c", RANDOM], "repeat_same": ["out.txt"]}])
	result = runVerify(main, wt)
	assert result["passed"] is False and "differs between two runs" in result["checks"][0]["why"]


def test_verify_checks_an_expected_error_its_text_and_that_nothing_changed(project):
	main, wt = project
	script = "import sys; print('error: bad config'); sys.exit(1)"
	verifySpec(main, [{"name": "err", "run": [PY, "-c", script], "exit": 1,
		"output_contains": ["error: bad config"], "unchanged": ["features/**"]}])
	assert runVerify(main, wt)["passed"] is True
	verifySpec(main, [{"name": "err", "run": [PY, "-c", script], "exit": 0}])
	assert "exit 1, expected 0" in runVerify(main, wt)["checks"][0]["why"]


def test_verify_fails_an_unchanged_glob_that_matches_no_file(project):
	# a mistyped glob checked nothing and passed; repeat_same already failed on no match
	main, wt = project
	verifySpec(main, [{"name": "typo", "run": [PY, "-c", "pass"], "unchanged": ["featurs/**"]}])
	result = runVerify(main, wt)
	assert result["passed"] is False and "no file matches featurs/**" in result["checks"][0]["why"]


def test_verify_writes_files_into_tmp_and_fills_placeholders(project):
	main, wt = project
	verifySpec(main, [{"name": "file", "run": [PY, "{tmp}/check.py"], "output_contains": ["ok"]}],
		files={"check.py": "print('ok')\n"})
	assert runVerify(main, wt)["passed"] is True


def test_verify_without_a_spec_does_nothing(project):
	main, wt = project
	assert runVerify(main, wt) is None


def test_verify_fails_closed_on_a_missing_data_path(project):
	main, wt = project
	verifySpec(main, [{"name": "x", "run": [PY, "-c", "pass"]}], data_paths=["nope"])
	result = runVerify(main, wt)
	assert result["passed"] is False and "nope" in result["error"]


def test_auto_merge_needs_passed_checks_when_a_spec_exists(project):
	main, wt = project
	verifySpec(main, [{"name": "x", "run": [PY, "-c", "pass"]}])
	assert "verify.py left no result" in refused(main, wt)
	stepResult(wt, "verify", {"passed": False,
		"checks": [{"name": "det", "ok": False, "why": "differs between two runs: out.txt"}]})
	assert "real-data checks failed: det: differs" in refused(main, wt)
	stepResult(wt, "verify", {"passed": True, "checks": []})
	assert autoMerge(main, wt)["decision"] == "merged"


def runFinish(main, wt, home):
	return subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-finish.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": str(home), "WT_MAIN_DIR": str(main)})


def test_auto_finish_archives_imports_queues_friction_and_removes_the_worktree(project, toolsHome):
	main, wt = project
	write(wt, "plans/topic.md", "# topic\n\n## Cost\n\n- qa 1: over, big plan\n\n## Friction\n\n- a flaky fixture\n")
	stepResult(wt, "merge", {"decision": "merged", "commit": "abc1234567890"})
	endRun(wt)
	write(main, "plans/worker/topic.json", json.dumps({"class": "logic"}))
	out = runFinish(main, wt, toolsHome)
	assert out.returncode == 0, out.stderr
	steps = json.loads((main / ".hook-only" / "archive" / "topic.finish.json").read_text())["steps"]
	assert steps.get("remove") == "removed", steps
	archive = main / ".hook-only" / "archive" / "topic"
	assert (archive / ".hook-only" / "runs" / "exit-1").exists() and (archive / "plans" / "topic.md").exists()
	assert (archive / ".hook-only" / "imported").exists()
	friction = (main / "plans" / "friction.md").read_text()
	assert "## topic" in friction and "a flaky fixture" in friction and "qa 1: over" in friction
	assert not wt.exists()
	assert "feature/x/topic" in git(main, "branch", "--list", "feature/x/topic")


def test_auto_finish_does_nothing_unless_merged(project, toolsHome):
	main, wt = project
	stepResult(wt, "merge", {"decision": "refused", "reason": "x"})
	endRun(wt)
	runFinish(main, wt, toolsHome)
	assert wt.exists() and not (main / ".hook-only" / "archive" / "topic").exists()


def finishedProject(project):
	"""project with run 1 merged and ended, ready for auto-finish."""
	main, wt = project
	stepResult(wt, "merge", {"decision": "merged", "commit": "abc1234567890"})
	endRun(wt)
	write(main, "plans/worker/topic.json", json.dumps({"class": "logic"}))
	return main, wt


def test_auto_finish_records_the_queue_tick_in_finish_json(project, toolsHome):
	main, wt = finishedProject(project)
	out = runFinish(main, wt, toolsHome)
	assert out.returncode == 0, out.stderr
	steps = json.loads((main / ".hook-only" / "archive" / "topic.finish.json").read_text())["steps"]
	assert steps["queue_tick"] == {"outcome": "ticked"}, steps
	assert [e["kind"] for e in events(main)] == ["finished"]


def test_a_failed_tick_after_auto_finish_is_recorded_and_an_event(project, toolsHome):
	"""The tick runs after the worktree is gone (a finished topic leaves the queue); a main whose flow-mode record
	can't be read makes `wt_queue.py tick` exit non-zero (test_flow_mode_errors.py): finish.json keeps the error
	with the tick's last stderr line and the person gets queue-failed. No FLOW_INTEGRATION: the broken record
	decides the mode."""
	main, wt = finishedProject(project)
	(main / ".hook-only").mkdir(exist_ok=True)
	(main / ".hook-only" / "flow-mode").write_text("bogus\n")
	env = {k: v for k, v in os.environ.items() if k != "FLOW_INTEGRATION"}
	out = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-finish.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=120, env={**env, "HOME": str(toolsHome), "WT_MAIN_DIR": str(main)})
	assert out.returncode == 0, out.stderr
	steps = json.loads((main / ".hook-only" / "archive" / "topic.finish.json").read_text())["steps"]
	assert list(steps)[-2:] == ["remove", "queue_tick"] and steps["remove"] == "removed", steps
	tick = steps["queue_tick"]
	assert tick["outcome"] == "error" and tick["reason"].startswith("exit "), tick
	assert "homeconfig.ConfigError: " in tick["reason"] and "'bogus'" in tick["reason"], tick
	kinds = [(e["kind"], e["topic"]) for e in events(main)]
	assert kinds == [("finished", "topic"), ("queue-failed", "topic")], kinds
	# the event carries the reason's first 200 characters (auto-finish.py report), so whether 'bogus' is among them
	# depends on the tmp path's length: compare the whole text instead
	assert events(main)[-1]["text"] == f"queue tick after auto-finish: {tick['reason'][:200]} (finish.json)"


def finishStep(wt, run=1):
	import runstate

	record, problem = runstate.readRecord(str(wt), run)
	assert record is not None, problem
	return (record.get("steps") or {}).get("finish") or {}


def test_finish_json_is_the_registered_finish_record_kind(project, toolsHome):
	"""finish.json goes through statefile as state kind finish-record (schema 1), so run-steps.py reads it there."""
	import statefile

	main, wt = finishedProject(project)
	out = runFinish(main, wt, toolsHome)
	assert out.returncode == 0, out.stderr
	written = statefile.read(str(main), "finish-record", "topic")
	assert written["schema"] == 1 and written["run"] == 1 and written["steps"]["remove"] == "removed", written
	assert statefile.pathOf(str(main), "finish-record", "topic") == str(main / ".hook-only" / "archive" / "topic.finish.json")


def test_a_crash_inside_auto_finish_is_in_finish_json_an_event_and_the_run_record(project, monkeypatch):
	"""An exception no step catches (here archive raising) is the step "error", the person's finish-failed, and the
	run record's finish step failed (run-steps.py had recorded it launched); the worktree stays."""
	autoFinish = loadHook("auto-finish.py", "auto_finish_crash")
	main, wt = finishedProject(project)
	monkeypatch.setattr(autoFinish, "waitForRun", lambda *args: True)

	def broken(*args):
		raise RuntimeError("archive broke")

	monkeypatch.setattr(autoFinish, "archive", broken)
	autoFinish.finish(str(wt), "topic", 1, str(main))
	steps = json.loads((main / ".hook-only" / "archive" / "topic.finish.json").read_text())["steps"]
	assert steps["error"] == "auto-finish.py crashed: RuntimeError: archive broke", steps
	assert [e["kind"] for e in events(main)][0] == "finish-failed" and "archive broke" in events(main)[0]["text"]
	step = finishStep(wt)
	assert step["state"] == "failed" and "archive broke" in step["reason"] and step["reason"].endswith("(finish.json)")
	assert wt.exists()


def test_a_finish_that_does_nothing_fails_the_run_record_s_finish_step(project, toolsHome):
	main, wt = project
	stepResult(wt, "merge", {"decision": "refused", "reason": "x"})
	endRun(wt)
	runFinish(main, wt, toolsHome)
	step = finishStep(wt)
	assert step["state"] == "failed" and "check: merge-<run>.json doesn't say merged" in step["reason"], step


def test_a_finished_finish_leaves_the_run_record_s_finish_step_alone(project, toolsHome):
	"""Success removes the worktree (and its record with it): nothing is written back, nothing fails."""
	main, wt = finishedProject(project)
	out = runFinish(main, wt, toolsHome)
	assert out.returncode == 0 and out.stderr == "", out.stderr
	assert not wt.exists()


@pytest.mark.parametrize("mainDir", ["", "/no/such/main"])
def test_auto_finish_without_a_main_checkout_says_so_and_fails_the_finish_step(project, toolsHome, mainDir):
	"""No WT_MAIN_DIR (or not a directory): no finish.json can be written, so the run record's finish step is
	failed with the reason, a stderr line (run-steps.py appends it to the run's log) says it, exit 1."""
	main, wt = finishedProject(project)
	out = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-finish.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": str(toolsHome), "WT_MAIN_DIR": mainDir})
	assert out.returncode == 1 and "WT_MAIN_DIR" in out.stderr, out.stderr
	step = finishStep(wt)
	assert step["state"] == "failed" and "WT_MAIN_DIR" in step["reason"], step
	assert not (main / ".hook-only" / "archive" / "topic.finish.json").exists() and wt.exists()


def test_auto_finish_with_a_run_that_is_not_a_number_says_so(project, toolsHome):
	main, wt = finishedProject(project)
	out = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-finish.py"), str(wt), "topic", "one"],
		capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": str(toolsHome), "WT_MAIN_DIR": str(main)})
	assert out.returncode == 1 and "auto-finish: topic run one: ValueError" in out.stderr, out.stderr
	assert "Traceback" not in out.stderr and finishStep(wt).get("state") != "failed"


def test_a_tick_that_cannot_start_is_recorded_as_an_error(tmp_path, monkeypatch):
	autoFinish = loadHook("auto-finish.py", "auto_finish_under_test")

	def cannotStart(*args, **kwargs):
		raise FileNotFoundError("no python")

	monkeypatch.setattr(autoFinish.subprocess, "Popen", cannotStart)
	outcome = autoFinish.queueTick(str(tmp_path), (60, "a test limit"))
	assert outcome == {"outcome": "error", "reason": "not started: FileNotFoundError: no python"}


def test_a_tick_past_the_finish_s_limit_is_ended_and_recorded_as_an_error(tmp_path, monkeypatch):
	autoFinish = loadHook("auto-finish.py", "auto_finish_tick_limit")
	monkeypatch.setattr(autoFinish, "HERE", str(tmp_path))
	(tmp_path / "wt_queue.py").write_text("import time\ntime.sleep(60)\n")
	outcome = autoFinish.queueTick(str(tmp_path), (1, "a test limit"))
	assert outcome["outcome"] == "error" and "exceeded 0 min, from a test limit" in outcome["reason"], outcome


def test_import_refuses_a_checkout_imported_before(project, toolsHome):
	main, wt = project
	(wt / ".hook-only" / "imported").write_text("2026-11-01 topic\n")
	out = subprocess.run([sys.executable, os.path.join(RUN_SKILLS, "repo-plan", "cost-history.py"), "import", str(wt),
		"2026-11-01", "topic", "feature/x/topic", "logic"], cwd=main, capture_output=True, text=True,
		env={**os.environ, "HOME": str(toolsHome)})
	assert out.returncode != 0 and "already imported" in (out.stdout + out.stderr)


# --- the person's approval: auto-merge.py --person (wt merge), events ----------------------------

def personMerge(main, wt):
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-merge.py"), str(wt), "topic", "1", "--person"],
		capture_output=True, text=True, timeout=600, env={**os.environ,
		"WT_MAIN_DIR": str(main), "PYTHONPATH": RUN_LIB})
	assert result.returncode == 0, result.stderr
	return json.loads((wt / ".hook-only" / "runs" / "merge-1.json").read_text())


def events(main):
	path = main / ".hook-only" / "events.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_held_path_is_refused_then_merged_on_the_persons_approval(project):
	main, wt = project
	write(wt, ".claude/skills/x/SKILL.md", "# x\n")
	git(wt, "add", ".")
	git(wt, "commit", "-q", "-m", "skill")
	approve(wt)
	assert "a person must review" in refused(main, wt)
	assert [e["kind"] for e in events(main)] == ["refused"]
	decision = personMerge(main, wt)
	assert decision["decision"] == "merged" and decision["review"] == "person", decision
	assert "approved by the person" in git(main, "log", "-1", "--format=%s")
	record = json.loads((main / ".hook-only" / "auto-merge" / "merges.jsonl").read_text().splitlines()[-1])
	assert record["by"] == "person" and record["commit"] == decision["commit"]
	assert events(main)[-1]["kind"] == "merged" and "approved by the person" in events(main)[-1]["text"]


def test_the_person_needs_no_reviewer_but_tests_still_decide(project):
	main, wt = project
	(wt / ".hook-only" / "reviews.jsonl").unlink()
	write(wt, "tests/test_more.py", "from pkg.more import more\n\n\ndef test_more():\n    assert more(1) == 5\n")
	git(wt, "commit", "-q", "-am", "red")
	decision = personMerge(main, wt)
	assert decision["decision"] == "refused" and "suite fails" in decision["reason"]


def test_the_person_merge_still_needs_the_branch_to_contain_main(project):
	main, wt = project
	write(main, "pkg/other.py", "X = 1\n")
	git(main, "add", ".")
	git(main, "commit", "-q", "-m", "main moved")
	decision = personMerge(main, wt)
	assert decision["decision"] == "refused" and decision.get("merge_run") is True


def test_an_automatic_merge_is_an_event_too(project):
	main, wt = project
	assert autoMerge(main, wt)["decision"] == "merged"
	assert events(main)[-1]["kind"] == "merged" and events(main)[-1]["topic"] == "topic"


def test_auto_finish_reports_its_outcome_as_an_event(project, toolsHome):
	main, wt = project
	stepResult(wt, "merge", {"decision": "merged", "commit": "abc1234567890"})
	endRun(wt)
	write(main, "plans/worker/topic.json", json.dumps({"class": "logic"}))
	assert runFinish(main, wt, toolsHome).returncode == 0
	assert events(main)[-1]["kind"] == "finished"


# --- already merged: a duplicate evaluation or a later run changes nothing -----------

def autoMergeRun(main, wt, run, person=False):
	"""auto-merge.py for run; the run's merge-<run>.json text afterwards, or None."""
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-merge.py"), str(wt), "topic", str(run),
		*(["--person"] if person else [])], capture_output=True, text=True, timeout=600,
		env={**os.environ, "WT_MAIN_DIR": str(main), "PYTHONPATH": RUN_LIB,
		})
	assert result.returncode == 0, result.stderr
	path = wt / ".hook-only" / "runs" / f"merge-{run}.json"
	return path.read_text() if path.exists() else None


def personMerged(project):
	"""A held path refused, then merged on the person's approval (run 1)."""
	main, wt = project
	write(wt, ".claude/skills/x/SKILL.md", "# x\n")
	git(wt, "add", ".")
	git(wt, "commit", "-q", "-m", "skill")
	approve(wt)
	assert "a person must review" in refused(main, wt)
	decision = personMerge(main, wt)
	assert decision["decision"] == "merged", decision
	return decision


def test_a_second_approval_after_the_persons_merge_changes_nothing(project):
	main, wt = project
	first = personMerged(project)
	before = (wt / ".hook-only" / "runs" / "merge-1.json").read_text()
	seen, head = len(events(main)), git(main, "rev-parse", "HEAD")
	assert autoMergeRun(main, wt, 1, person=True) == before
	assert len(events(main)) == seen and git(main, "rev-parse", "HEAD") == head == first["commit"]


def test_an_already_merged_run_never_overwrites_its_decision_or_asks_for_a_merge_run(project):
	# The race seen: the duplicate ran before the first wrote "merged", found main holding
	# the merge commit and said "the branch doesn't contain main" (merge_run), which looped merge runs.
	main, wt = project
	personMerged(project)
	stale = json.dumps({"run": 1, "decision": "refused", "reason": "a person must review these paths: .claude/x"})
	(wt / ".hook-only" / "runs" / "merge-1.json").write_text(stale)
	seen = len(events(main))
	assert autoMergeRun(main, wt, 1, person=True) == stale
	assert len(events(main)) == seen and not (wt / ".hook-only" / "runs" / "auto-1.json").exists()


def test_a_merge_run_on_an_already_merged_branch_starts_no_other_merge_run(project):
	main, wt = project
	first = personMerged(project)
	runs = wt / ".hook-only" / "runs"
	recordRun(main, wt, 2, "STATUS: done\nnothing to merge")  # run 1 never ends here: no auto-finish
	head = git(main, "rev-parse", "HEAD")
	decision = json.loads(autoMergeRun(main, wt, 2))
	assert decision["decision"] == "merged" and decision["review"] == "already-merged", decision
	assert decision["commit"] == first["commit"] and "merge_run" not in decision
	assert git(main, "rev-parse", "HEAD") == head
	assert not (runs / "auto-2.json").exists()
	assert events(main)[-1]["kind"] == "merged" and "nothing new to merge" in events(main)[-1]["text"]


def test_a_branch_with_new_commits_after_its_merge_is_checked_again(project):
	main, wt = project
	personMerged(project)
	write(wt, "pkg/later.py", "X = 2\n")
	git(wt, "add", ".")
	git(wt, "commit", "-q", "-m", "later")
	recordRun(main, wt, 2, "STATUS: done\n")
	decision = json.loads(autoMergeRun(main, wt, 2))
	assert decision["decision"] == "refused" and decision.get("review") != "already-merged", decision
	assert "run's state" not in decision["reason"], decision  # checked again: the branch refuses, not run 2's state


# -- auto-finish run again (self-repair stage 3a, A1): each sub-action at most once ---------------------------


def finishOnce(autoFinish, main, wt):
	autoFinish.finish(str(wt), "topic", 1, str(main))
	return json.loads((main / ".hook-only" / "archive" / "topic.finish.json").read_text())["steps"]


def finishWorld(project, monkeypatch):
	"""finishedProject with an approved plan and auto-finish loaded in-process (the tree's cost-history.py)."""
	main, wt = finishedProject(project)
	write(main, ".hook-only/approved/topic.md", "# the approved plan\n")
	write(wt, "plans/topic.md", "# topic\n\n## Friction\n\n- a flaky fixture\n")
	autoFinish = loadHook("auto-finish.py", "auto_finish_rerun")
	monkeypatch.setattr(autoFinish, "COST_HISTORY", os.path.join(RUN_SKILLS, "repo-plan", "cost-history.py"))
	monkeypatch.setattr(autoFinish, "waitForRun", lambda *args: True)
	return main, wt, autoFinish


def finishOutcome(main):
	"""What a finish leaves, comparable across attempts: the history rows, the friction headings, the archive's
	approved plan and import marker."""
	history = main / ".claude-cost" / "history.tsv"
	friction = (main / "plans" / "friction.md").read_text() if (main / "plans" / "friction.md").exists() else ""
	archive = main / ".hook-only" / "archive" / "topic"
	return {
		"rows": history.read_text().splitlines() if history.exists() else [],
		"headings": [line for line in friction.splitlines() if line.startswith("## topic")],
		"approved": (archive / "approved.md").read_text() if (archive / "approved.md").exists() else None,
		"imported": (archive / ".hook-only" / "imported").exists(),
	}


def test_a_clean_finish_records_every_sub_action_in_its_progress_file(project, monkeypatch):
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	steps = finishOnce(autoFinish, main, wt)
	assert steps["remove"] == "removed" and not wt.exists()
	record = json.loads((main / ".hook-only" / "archive" / "topic.finish.json").read_text())
	assert record["started_at"] <= record["ended_at"]  # the finish's own duration: the history of its limit
	import statefile

	progress = statefile.read(str(main), "finish-progress", "topic-1")
	assert sorted(progress["done"]) == ["archive", "friction", "import", "remove"] and progress["commit"]
	outcome = finishOutcome(main)
	assert outcome["imported"] and outcome["approved"] == "# the approved plan\n" and len(outcome["headings"]) == 1


@pytest.mark.parametrize("crashAt", [1, 2, 3, 4])
@pytest.mark.parametrize("recorded", [False, True], ids=["before-recording", "after-recording"])
def test_a_finish_that_crashed_after_any_sub_action_ends_like_one_clean_run(project, monkeypatch, tmp_path, crashAt,
		recorded):
	"""A crash after the crashAt-th sub-action (before or after its progress is recorded), then the finish run
	again: the same history rows, one friction heading, the approved plan kept, the worktree removed."""
	(tmp_path / "clean").mkdir()
	clean = makeProject(tmp_path / "clean")
	cleanMain, cleanWt, cleanFinish = finishWorld(clean, monkeypatch)
	finishOnce(cleanFinish, cleanMain, cleanWt)
	expected = finishOutcome(cleanMain)

	main, wt, autoFinish = finishWorld(project, monkeypatch)
	original = autoFinish.Progress.mark
	calls = []

	def crashing(self, name):
		calls.append(name)
		if len(calls) == crashAt:
			if recorded:
				original(self, name)
			raise RuntimeError(f"crashed after {name}")
		original(self, name)

	monkeypatch.setattr(autoFinish.Progress, "mark", crashing)
	steps = finishOnce(autoFinish, main, wt)
	assert "crashed after" in steps["error"], steps
	monkeypatch.setattr(autoFinish.Progress, "mark", original)
	steps = finishOnce(autoFinish, main, wt)
	assert steps["remove"] == "removed" and not wt.exists(), steps
	outcome = finishOutcome(main)
	assert len(outcome["rows"]) == len(expected["rows"]) and outcome["headings"] == expected["headings"][:1]
	assert len(outcome["headings"]) == 1 and outcome["approved"] == expected["approved"] and outcome["imported"]
	again = finishOnce(autoFinish, main, wt)  # a third run after success: nothing done again
	assert again["remove"] == "removed" and finishOutcome(main) == outcome


def test_a_topic_name_used_again_starts_its_finish_afresh(project, monkeypatch):
	"""A progress file of an earlier merge commit of the same topic and run is not this finish's: every
	sub-action runs."""
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	import statefile

	statefile.write(str(main), "finish-progress", {"schema": 1, "topic": "topic", "run": 1, "commit": "0ld0ld",
		"done": {"archive": "2026-11-01T00:00:00+08:00", "import": "2026-11-01T00:00:00+08:00"}}, "topic-1")
	steps = finishOnce(autoFinish, main, wt)
	assert steps["remove"] == "removed" and finishOutcome(main)["imported"]
	assert "earlier attempt" not in steps["import"]


def test_an_unreadable_progress_file_does_nothing_and_says_so(project, monkeypatch):
	"""Fail closed: what an earlier attempt did is unknown, so no sub-action runs (no archive, no import, the
	worktree kept); the finish fails with the file named."""
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	write(main, ".hook-only/finish/topic-1.json", "{ torn")
	steps = finishOnce(autoFinish, main, wt)
	assert "topic-1.json exists but can't be read" in steps["error"], steps
	assert wt.exists() and "archive" not in steps and finishOutcome(main)["rows"] == []


def test_a_lost_progress_file_still_imports_once(project, monkeypatch, tmp_path):
	"""The import marker survives the archive being made again: a finish that crashed after its import and lost
	its progress file (moved away here) runs again with the same history rows, one friction heading."""
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	original = autoFinish.Progress.mark

	def crashAtFriction(self, name):
		if name == "friction":
			raise RuntimeError("crashed before friction was recorded")
		original(self, name)

	monkeypatch.setattr(autoFinish.Progress, "mark", crashAtFriction)
	finishOnce(autoFinish, main, wt)
	rows = finishOutcome(main)["rows"]
	assert rows and finishOutcome(main)["imported"]
	os.rename(main / ".hook-only" / "finish" / "topic-1.json", tmp_path / "progress-moved-away.json")
	monkeypatch.setattr(autoFinish.Progress, "mark", original)
	steps = finishOnce(autoFinish, main, wt)
	assert steps["remove"] == "removed", steps
	outcome = finishOutcome(main)
	assert outcome["rows"] == rows and len(outcome["headings"]) == 1 and outcome["imported"]


def test_a_gone_worktree_with_progress_of_another_merge_commit_is_not_a_finished_finish(project, monkeypatch):
	"""The shortcut for a worktree already gone holds only for the merge commit the archive names: a progress file
	of an earlier commit (a topic name used again) never makes a finish read as done."""
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	import statefile

	statefile.write(str(main), "finish-progress", {"schema": 1, "topic": "topic", "run": 1, "commit": "0ld0ld",
		"done": {"archive": "x", "import": "x", "friction": "x", "remove": "x"}}, "topic-1")
	git(main, "worktree", "remove", "--force", str(wt))
	steps = finishOnce(autoFinish, main, wt)
	assert steps.get("remove") != "removed" and autoFinish.failureOf(steps), steps


def importAgain(main):
	"""cost-history.py import of the archive again, as a finish run again would (from the main checkout)."""
	archive = main / ".hook-only" / "archive" / "topic"
	return subprocess.run([PY, os.path.join(RUN_SKILLS, "repo-plan", "cost-history.py"), "import", str(archive),
		"2026-12-01", "topic", "feature/x/topic", "logic"], cwd=main, capture_output=True, text=True)


def test_an_import_whose_marker_was_lost_adds_no_row_twice(project, monkeypatch, tmp_path):
	"""A crash between the rows and the marker (the marker moved away here): the import run again, on another
	day, finds its key in the history, adds nothing and writes the marker."""
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	finishOnce(autoFinish, main, wt)
	rows = finishOutcome(main)["rows"]
	assert len(rows) > 1 and all("import=" in row for row in rows[1:]), rows
	marker = main / ".hook-only" / "archive" / "topic" / ".hook-only" / "imported"
	os.rename(marker, tmp_path / "imported-moved-away")
	out = importAgain(main)
	assert out.returncode == 0 and "imported already" in out.stdout, out.stdout + out.stderr
	assert finishOutcome(main)["rows"] == rows and marker.exists()


def test_an_import_that_crashed_while_writing_leaves_the_history_whole_then_adds_once(project, monkeypatch):
	"""A crash before the history is renamed into place: the old history, none of the rows; the import run again
	adds each row once."""
	import importlib.util

	main, wt, autoFinish = finishWorld(project, monkeypatch)
	history = main / ".claude-cost" / "history.tsv"
	history.parent.mkdir(parents=True)
	history.write_text("date\ttopic\tbranch\tclass\tagent\tround\testTok\tactTok\testMin\tactMin\testUsd\tactUsd\tnote\n"
		"2026-11-01\tother\tfeature/x/other\tlogic\tbranch\t-\t\t\t10\t12\t1.0\t1.5\tkept\n")
	before = history.read_text()
	monkeypatch.setattr(autoFinish, "waitForRun", lambda *args: True)
	autoFinish.finish(str(wt), "topic", 1, str(main))  # archive and import for real, then the crash below
	imported = history.read_text()
	history.write_text(before)  # the history as it was before the import
	marker = main / ".hook-only" / "archive" / "topic" / ".hook-only" / "imported"
	os.rename(marker, marker.parent / "imported-moved")
	spec = importlib.util.spec_from_file_location("cost_history_crash", os.path.join(RUN_SKILLS, "repo-plan",
		"cost-history.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	def crash(*args):
		raise OSError("crashed before the rename")

	real = os.replace
	monkeypatch.setattr(module.os, "replace", crash)
	try:
		with pytest.raises(OSError):
			module.addImport(str(history), [["2026-12-01", "topic", "b", "logic", "worker", "1", "", "", "1", "1", "1",
				"1", "n"]])
	finally:
		monkeypatch.setattr(module.os, "replace", real)
	assert history.read_text() == before
	out = importAgain(main)
	assert out.returncode == 0, out.stdout + out.stderr
	assert withoutDates(history) == withoutDates(imported)
	again = importAgain(main)
	assert again.returncode == 1 and "already imported" in again.stderr and withoutDates(history) == withoutDates(imported)


def withoutDates(history):
	"""The history's rows without their date column (a rerun imports on another day)."""
	text = history if isinstance(history, str) else history.read_text()
	return [line.split("\t", 1)[1] for line in text.splitlines()]


def test_a_finish_program_past_its_limit_is_ended_the_finish_fails_and_a_rerun_completes(project, monkeypatch,
		tmp_path):
	"""3c in the finish: a cost import that hangs is ended at the finish's limit (with its group), the finish is
	failed with the limit's reason and removes nothing; run again with an import that works, it completes with the
	rows of one import."""
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	hanging = tmp_path / "hanging-import.py"
	hanging.write_text("import subprocess, time\nsubprocess.Popen(['sleep', '60'])\ntime.sleep(60)\n")
	working = autoFinish.COST_HISTORY
	monkeypatch.setattr(autoFinish, "COST_HISTORY", str(hanging))
	monkeypatch.setattr(autoFinish, "finishLimit", lambda mainDir: (1, "a test limit"))
	steps = finishOnce(autoFinish, main, wt)
	assert steps["timeout"].startswith("hanging-import.py exceeded 0 min, from a test limit"), steps
	assert "remove" not in steps and wt.exists()
	monkeypatch.setattr(autoFinish, "COST_HISTORY", working)
	assert finishOnce(autoFinish, main, wt)["remove"] == "removed"
	(tmp_path / "clean").mkdir()
	clean = makeProject(tmp_path / "clean")
	cleanMain, cleanWt, cleanFinish = finishWorld(clean, monkeypatch)
	monkeypatch.setattr(cleanFinish, "finishLimit", lambda mainDir: (600, "a test limit"))
	finishOnce(cleanFinish, cleanMain, cleanWt)
	assert len(finishOutcome(main)["rows"]) == len(finishOutcome(cleanMain)["rows"])


def test_a_friction_heading_in_the_older_chinese_form_counts_as_appended(project, monkeypatch, tmp_path):
	"""plans/friction.md written before the heading became English: a rerun finds the topic's merge commit there
	and appends nothing; the heading written now is the English one, found the same way."""
	main, wt, autoFinish = finishWorld(project, monkeypatch)
	commit = "0123456789abcdef"
	path = tmp_path / "friction.md"
	path.write_text(f"## topic（自動 merge {commit[:10]}，2026-11-01；待規劃 session 決定去處）\n")
	assert autoFinish.frictionAppended(str(path), "topic", commit)
	assert not autoFinish.frictionAppended(str(path), "topic", "fedcba9876543210")
	text = autoFinish.friction(str(tmp_path / "no-plan"), "topic", commit, "2026-11-09")
	assert text.startswith("## topic (auto-merge 0123456789, 2026-11-09; ") and "(the plan has no ## Friction)" in text
	path.write_text(text)
	assert autoFinish.frictionAppended(str(path), "topic", commit)

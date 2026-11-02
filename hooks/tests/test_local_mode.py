"""Local mode (2026-11-01): FLOW_INTEGRATION=local makes the local main the integration branch, and
auto-merge.py merges a finished branch into it when every recorded condition holds.

Run: uv run --offline --no-project --with pytest --with pytest-cov==7.1.0 python -m pytest ~/.claude/hooks/tests/test_local_mode.py
Inside Claude Code's sandbox bwrap can't nest: the suite runs pass QG_UNSANDBOXED_FOR_TESTS=1 then.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
REAL_HOOKS = os.path.expanduser("~/.claude/hooks")
sys.path[:0] = [HOOKS, REAL_HOOKS]

import qg_common  # noqa: E402

UV = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
BWRAP_OK = bool(shutil.which("bwrap")) and subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-net", "true"],
	capture_output=True).returncode == 0
SANDBOX_ENV = {} if BWRAP_OK else {qg_common.UNSANDBOXED_ENV: "1"}
RUNNER = [UV, "run", "--offline", "--no-project", "--with", "pytest", "--with", "pytest-cov==7.1.0", "python", "-m",
	"pytest", "-q", "-p", "no:cacheprovider"]


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def write(repo, path, text):
	full = os.path.join(str(repo), path)
	os.makedirs(os.path.dirname(full), exist_ok=True)
	with open(full, "w", encoding="utf-8") as f:
		f.write(textwrap.dedent(text))


def loadHook(name, module):
	spec = importlib.util.spec_from_file_location(module, os.path.join(HOOKS, name))
	loaded = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(loaded)
	return loaded


@pytest.fixture
def project(tmp_path):
	"""(main checkout on main with quality gates, worktree on feature/x/topic)."""
	return makeProject(tmp_path)


def makeProject(tmp_path, checks=None):
	"""project's checkouts; checks: the python language's "checks" in the committed quality gates."""
	python = {"extensions": [".py"], "coverage": RUNNER + ["--cov=pkg", "--cov-report=lcov:{report}"],
		"coverage_format": "lcov", "test": RUNNER + ["-x"]}
	if checks is not None:
		python["checks"] = checks
	main = tmp_path / "main"
	main.mkdir()
	git(main, "init", "-q", "-b", "main")
	git(main, "config", "user.email", "t@t")
	git(main, "config", "user.name", "t")
	write(main, ".gitignore", ".hook-only/\nplans/\n__pycache__/\n")
	write(main, "pkg/__init__.py", "")
	write(main, "pkg/calc.py", "def calc(x):\n    return x\n")
	write(main, "tests/test_calc.py", "from pkg.calc import calc\n\n\ndef test_calc():\n    assert calc(1) == 1\n")
	write(main, ".claude/quality-gates.json", json.dumps({"languages": {"python": python}, "exclude": ["tests/"],
		"auto_merge_hold": ["pkg/secret/"]}))
	git(main, "add", ".")
	git(main, "commit", "-q", "-m", "base")
	wt = tmp_path / "worktrees" / "topic"
	git(main, "worktree", "add", "-q", "-b", "feature/x/topic", str(wt))
	write(wt, "pkg/more.py", "def more(x):\n    return x + 1\n")
	write(wt, "tests/test_more.py", "from pkg.more import more\n\n\ndef test_more():\n    assert more(1) == 2\n")
	git(wt, "add", ".")
	git(wt, "commit", "-q", "-m", "implement more")
	runs = wt / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	(runs / "result-1.json").write_text(json.dumps({"result": "STATUS: done\nall green"}))
	(runs / "main-1").write_text(git(main, "rev-parse", "HEAD") + "\n")
	approve(wt)
	write(wt, "PR_DESCRIPTION.md", "Add more()\n\nDetails.\n")
	return main, wt


def approve(wt, verdict="APPROVE"):
	with open(wt / ".hook-only" / "reviews.jsonl", "a", encoding="utf-8") as f:
		f.write(json.dumps({"agent_id": "r1", "verdict": verdict, "head": git(wt, "rev-parse", "HEAD")}) + "\n")


def autoMerge(main, wt):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "auto-merge.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=600, env={**os.environ, "FLOW_INTEGRATION": "local",
		"WT_MAIN_DIR": str(main), "PYTHONPATH": REAL_HOOKS, **SANDBOX_ENV})
	assert result.returncode == 0, result.stderr
	return json.loads((wt / ".hook-only" / "runs" / "merge-1.json").read_text())


# --- integration branch -------------------------------------------------------------------------

def baseRef(repo, local):
	env = {k: v for k, v in os.environ.items() if k != "FLOW_INTEGRATION"}
	if local:
		env["FLOW_INTEGRATION"] = "local"
	return subprocess.run(["bash", os.path.join(SKILLS, "feature-flow", "base-ref.sh")], cwd=repo, capture_output=True,
		text=True, env=env)


def test_base_ref_follows_the_mode(project):
	main, _ = project
	git(main, "update-ref", "refs/remotes/origin/main", "HEAD")
	assert baseRef(main, local=True).stdout.strip() == "main"
	assert baseRef(main, local=False).stdout.strip() == "origin/main"


def test_integration_ref_in_local_mode_needs_no_origin(project, monkeypatch):
	main, wt = project
	monkeypatch.setenv("FLOW_INTEGRATION", "local")
	assert qg_common.integrationRef(str(main)) == "main"
	assert qg_common.defaultBase(str(wt)) == git(main, "rev-parse", "main")
	assert qg_common.loadConfig(str(wt))[0] is not None
	monkeypatch.delenv("FLOW_INTEGRATION")
	assert qg_common.loadConfig(str(wt)) == (None, None)


def test_review_prompt_names_the_local_base(tmp_path):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q")
	data = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(repo), "session_id": "s",
		"tool_input": {"subagent_type": "code-reviewer", "prompt": "Budget: 5 min, $1"}}
	env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "dispatch-guard.py")], input=json.dumps(data),
		capture_output=True, text=True, env={**env, "FLOW_INTEGRATION": "local"})
	prompt = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["prompt"]
	assert "local default branch" in prompt and "origin" not in prompt.split("Budget:")[0]


# --- verdicts -----------------------------------------------------------------------------------

@pytest.mark.parametrize("message,verdict", [
	("Findings: none.\n\nVerdict: APPROVE\n", "APPROVE"),
	("**Verdict:** CHANGES REQUESTED\n", "CHANGES REQUESTED"),
	("2. A verdict: APPROVE or CHANGES REQUESTED.\nAPPROVE\n", "CHANGES REQUESTED"),
	("no verdict here", "NONE"),
])
def test_agent_guard_records_the_reviewer_verdict(tmp_path, message, verdict):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q")
	guard = loadHook("agent-guard.py", "agent_guard")
	guard.recordVerdict({"agent_type": "code-reviewer", "agent_id": "r9", "last_assistant_message": message}, str(repo))
	record = json.loads((repo / ".hook-only" / "reviews.jsonl").read_text().splitlines()[-1])
	assert record["verdict"] == verdict and record["agent_id"] == "r9"


def test_agent_guard_ignores_other_agents(tmp_path):
	guard = loadHook("agent-guard.py", "agent_guard")
	guard.recordVerdict({"agent_type": "spec-reviewer", "last_assistant_message": "APPROVE"}, str(tmp_path))
	assert not (tmp_path / ".hook-only" / "reviews.jsonl").exists()


def guardEvent(repo, event, **fields):
	data = {"hook_event_name": event, "cwd": str(repo), "session_id": "s", "agent_type": "code-reviewer",
		"agent_id": "rh", **fields}
	return subprocess.run([sys.executable, os.path.join(HOOKS, "agent-guard.py")], input=json.dumps(data),
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
	# last_assistant_message at SubagentStop holds only the closing text (ml-tools, 2026-11-01: NONE x3)
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


def refused(main, wt):
	decision = autoMerge(main, wt)
	assert decision["decision"] == "refused", decision
	return decision["reason"]


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
	(wt / ".hook-only" / "runs" / "main-1").write_text(git(main, "rev-parse", "HEAD") + "\n")
	assert "doesn't contain main" in refused(main, wt)


def test_a_branch_behind_main_asks_worker_control_for_a_merge_run(project):
	main, wt = project
	write(main, "README.md", "x\n")
	git(main, "add", "README.md")
	git(main, "commit", "-q", "-m", "earlier")
	(wt / ".hook-only" / "runs" / "main-1").write_text(git(main, "rev-parse", "HEAD") + "\n")
	decision = autoMerge(main, wt)
	assert decision["merge_run"] is True
	# no worker state in this fixture: worker-control answers, refusing, so nothing starts
	assert json.loads((wt / ".hook-only" / "runs" / "auto-1.json").read_text())["decision"] == "refused"


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


# --- checks: format and lint after the suite (2026-11-02) ----------------------------------------

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
	assert "ALERT or HOLD" in refused(main, wt)


def test_refuses_outside_local_mode(project):
	main, wt = project
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "auto-merge.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, env={**{k: v for k, v in os.environ.items() if k != "FLOW_INTEGRATION"},
		"WT_MAIN_DIR": str(main), "PYTHONPATH": REAL_HOOKS})
	assert result.returncode == 0
	assert "not in local mode" in json.loads((wt / ".hook-only" / "runs" / "merge-1.json").read_text())["reason"]


# --- fixes after review (2026-11-01) --------------------------------------------------------------

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
	(wt / ".hook-only" / "runs" / "main-1").write_text(git(main, "rev-parse", "HEAD") + "\n")
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
	(wt / ".hook-only" / "runs" / "main-1").write_text(git(main, "rev-parse", "HEAD") + "\n")


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
	guard = loadHook("agent-guard.py", "agent_guard")
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
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "dispatch-guard.py")], input=json.dumps(data),
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


# --- real-data checks (verify.py) and the automatic close (auto-finish.py), 2026-11-01 ------------

PY = "/usr/bin/python3"


def verifySpec(main, checks, **extra):
	write(main, "data/features/x.txt", "real\n")
	spec = {"data_paths": ["features"], "checks": checks, **extra}
	write(main, "plans/verify/topic.json", json.dumps(spec))


def runVerify(main, wt):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "verify.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=600, env={**os.environ, "FLOW_INTEGRATION": "local",
		"WT_MAIN_DIR": str(main), "PYTHONPATH": REAL_HOOKS, **SANDBOX_ENV})
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
	(wt / ".hook-only" / "runs" / "verify-1.json").write_text(json.dumps({"passed": False,
		"checks": [{"name": "det", "ok": False, "why": "differs between two runs: out.txt"}]}))
	assert "real-data checks failed: det: differs" in refused(main, wt)
	(wt / ".hook-only" / "runs" / "verify-1.json").write_text(json.dumps({"passed": True, "checks": []}))
	assert autoMerge(main, wt)["decision"] == "merged"


@pytest.fixture
def toolsHome(tmp_path):
	"""A home whose ~/.claude/hooks and skills are the files under test (auto-finish calls cost-history)."""
	h = tmp_path / "home"
	(h / ".claude").mkdir(parents=True)
	os.symlink(HOOKS, h / ".claude" / "hooks")
	os.symlink(SKILLS, h / ".claude" / "skills")
	return h


def runFinish(main, wt, home):
	return subprocess.run([sys.executable, os.path.join(HOOKS, "auto-finish.py"), str(wt), "topic", "1"],
		capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": str(home), "WT_MAIN_DIR": str(main)})


def test_auto_finish_archives_imports_queues_friction_and_removes_the_worktree(project, toolsHome):
	main, wt = project
	write(wt, "plans/topic.md", "# topic\n\n## Cost\n\n- qa 1: over, big plan\n\n## Friction\n\n- a flaky fixture\n")
	runs = wt / ".hook-only" / "runs"
	(runs / "merge-1.json").write_text(json.dumps({"decision": "merged", "commit": "abc1234567890"}))
	(runs / "start-1").write_text("2026-11-01T10:00:00+08:00\n")
	(runs / "exit-1").write_text("0 2026-11-01T10:30:00+08:00\n")
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
	runs = wt / ".hook-only" / "runs"
	(runs / "merge-1.json").write_text(json.dumps({"decision": "refused", "reason": "x"}))
	(runs / "exit-1").write_text("0 2026-11-01T10:30:00+08:00\n")
	runFinish(main, wt, toolsHome)
	assert wt.exists() and not (main / ".hook-only" / "archive" / "topic").exists()


def test_import_refuses_a_checkout_imported_before(project, toolsHome):
	main, wt = project
	(wt / ".hook-only" / "imported").write_text("2026-11-01 topic\n")
	out = subprocess.run([sys.executable, os.path.join(SKILLS, "repo-plan", "cost-history.py"), "import", str(wt),
		"2026-11-01", "topic", "feature/x/topic", "logic"], cwd=main, capture_output=True, text=True,
		env={**os.environ, "HOME": str(toolsHome)})
	assert out.returncode != 0 and "already imported" in (out.stdout + out.stderr)


# --- the person's approval: auto-merge.py --person (wt merge), events ----------------------------

def personMerge(main, wt):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "auto-merge.py"), str(wt), "topic", "1", "--person"],
		capture_output=True, text=True, timeout=600, env={**os.environ, "FLOW_INTEGRATION": "local",
		"WT_MAIN_DIR": str(main), "PYTHONPATH": REAL_HOOKS, **SANDBOX_ENV})
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
	runs = wt / ".hook-only" / "runs"
	(runs / "merge-1.json").write_text(json.dumps({"decision": "merged", "commit": "abc1234567890"}))
	(runs / "start-1").write_text("2026-11-01T10:00:00+08:00\n")
	(runs / "exit-1").write_text("0 2026-11-01T10:30:00+08:00\n")
	write(main, "plans/worker/topic.json", json.dumps({"class": "logic"}))
	assert runFinish(main, wt, toolsHome).returncode == 0
	assert events(main)[-1]["kind"] == "finished"


# --- already merged: a duplicate evaluation or a later run changes nothing (2026-11-02) -----------

def autoMergeRun(main, wt, run, person=False):
	"""auto-merge.py for run; the run's merge-<run>.json text afterwards, or None."""
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "auto-merge.py"), str(wt), "topic", str(run),
		*(["--person"] if person else [])], capture_output=True, text=True, timeout=600,
		env={**os.environ, "FLOW_INTEGRATION": "local", "WT_MAIN_DIR": str(main), "PYTHONPATH": REAL_HOOKS,
		**SANDBOX_ENV})
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
	# The race seen on 2026-11-02: the duplicate ran before the first wrote "merged", found main holding
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
	(runs / "result-2.json").write_text(json.dumps({"result": "STATUS: done\nnothing to merge"}))
	(runs / "main-2").write_text(git(main, "rev-parse", "HEAD") + "\n")
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
	runs = wt / ".hook-only" / "runs"
	(runs / "result-2.json").write_text(json.dumps({"result": "STATUS: done\n"}))
	(runs / "main-2").write_text(git(main, "rev-parse", "HEAD") + "\n")
	decision = json.loads(autoMergeRun(main, wt, 2))
	assert decision["decision"] == "refused" and decision.get("review") != "already-merged", decision

"""Tests for qg_common, qg_complexity, qg_mutate, crap-gate.py and mutation.py.

Run: uv run --offline --no-project --with pytest --with pytest-cov==7.1.0 python -m pytest ~/.claude/hooks/tests
The integration tests build a throwaway git repo (origin/main = its first commit) whose
quality-gates.json runs pytest-cov through `uv run --offline --with`, and feed hook input JSON to the
hooks as subprocesses with CLAUDE_PROJECT_DIR pointing at that repo (test-hooks.md).
Inside Claude Code's sandbox bwrap can't nest: the hook subprocesses then get
the programs run from a tree copy naming the tests' stand-in bwrap (config_fixtures.gateHome) and the tests that
need a real bwrap are skipped. Run the suite once
outside the sandbox (your own terminal) to exercise the sandboxed path end to end.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(HOOKS, "lib")  # the tree under test's hooks/lib: homepath adds config/ and the role directories
sys.path.insert(0, LIB)
import homepath  # noqa: E402

import budget_state  # noqa: E402
import config_fixtures  # noqa: E402
import hook_entries  # noqa: E402
import homeconfig  # noqa: E402
import qg_common  # noqa: E402
import qg_complexity  # noqa: E402
import qg_mutate  # noqa: E402

LANGS = qg_common.loadJson(os.path.join(HOOKS, "lib", "qg_langs.json"))
# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test never change a result.
RUN_HOOKS = os.path.join(config_fixtures.gateHome(), "hooks")
RUN_LIB = os.path.join(RUN_HOOKS, "lib")


def runPath(name):
	"""Where a hook of the tree under test lies in the clean copy."""
	return os.path.join(RUN_HOOKS, os.path.relpath(homepath.locate(name), HOOKS))
GO = shutil.which("go") or "/usr/local/go/bin/go"
UV = config_fixtures.testUv()
BWRAP_OK = config_fixtures.bwrapNests()
needsBwrap = pytest.mark.skipif(not BWRAP_OK, reason="bwrap can't create namespaces here (inside Claude Code's sandbox)")
# The repo fixture's integration branch is origin/main (no local main): the gates run in origin mode
# (conftest.py). Tests taking flowMode run in both; local mode's own gates are in test_local_mode.py.
pytestmark = pytest.mark.flow_mode("origin")


# --- qg_common ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path,pattern,expected", [
	("transform/tests/test_a.py", "transform/tests/", True),
	("transform/testsx/a.py", "transform/tests/", False),
	("ingest/dl_test.go", "ingest/*_test.go", True),
	("ingest/sub/dl_test.go", "ingest/*_test.go", False),
	("ingest/sub/dl_test.go", "ingest/**/*_test.go", True),
	("a/b/tests/c.py", "**/tests/**", True),
	("conftest.py", "**/conftest.py", True),
	("pyproject.toml", "pyproject.toml", True),
	("ingest", "ingest", True),
	("ingest/x.go", "ingest", True),
	("ingestion/x.go", "ingest", False),
])
def test_path_matches(path, pattern, expected):
	assert qg_common.pathMatches(path, pattern) is expected


def test_parse_diff_added_changed_and_deleted_hunks():
	diff = textwrap.dedent("""\
		diff --git a/a.py b/a.py
		--- a/a.py
		+++ b/a.py
		@@ -3,0 +4,2 @@ def f():
		+x
		+y
		@@ -10 +12 @@
		-a
		+b
		@@ -20,3 +21,0 @@
		-gone
		diff --git a/old.py b/old.py
		--- a/old.py
		+++ /dev/null
		""")
	assert qg_common.parseDiff(diff) == {"a.py": {4, 5, 12}}


def test_parse_lcov_relative_and_absolute():
	text = "SF:pkg/m.py\nDA:1,1\nDA:6,0\nend_of_record\nSF:/r/pkg/n.py\nDA:2,3\nend_of_record\n"
	assert qg_common.parseLcov(text, "/r") == {"pkg/m.py": {1: 1, 6: 0}, "pkg/n.py": {2: 3}}


def test_parse_go_cover_maps_module_and_column_one_end():
	text = "mode: set\nmod/x/a.go:10.13,12.2 1 1\nmod/x/a.go:13.2,15.1 2 0\nother/b.go:1.1,2.2 1 1\n"
	cover = qg_common.parseGoCover(text, "mod", "ingest")
	assert cover == {"ingest/x/a.go": {10: 1, 11: 1, 12: 1, 13: 0, 14: 0}}


def test_ranges_and_truncate():
	assert qg_common.ranges([9, 3, 4, 5]) == "3-5, 9"
	out = qg_common.truncate(["a" * 10] * 5, 25)
	assert out.endswith("... and 3 more") and out.count("a" * 10) == 2


# --- qg_complexity -----------------------------------------------------------------------------

PY_SAMPLE = textwrap.dedent('''\
	def f(a, b):
	    if a and b or a:
	        return [x for x in a if x]
	    elif b:
	        pass
	    for i in a:
	        while i:
	            i -= 1
	    try:
	        pass
	    except ValueError:
	        pass
	    match a:
	        case 1:
	            pass
	        case _:
	            pass
	    y = 1 if a else 2
	    s = "if and or for"  # if and or
	    return lambda: a or b

	class K:
	    def m(self):
	        def inner():
	            if self:
	                return 1
	        return inner
	''')


def test_python_counting_rules():
	funcs = {fn["name"]: fn for fn in qg_complexity.pythonFunctions(PY_SAMPLE)}
	# if, and, or, comprehension for + if, elif, for, while, except, 2 cases, ternary, lambda or
	assert funcs["f"]["ccn"] == 1 + 3 + 2 + 1 + 2 + 1 + 2 + 1 + 1
	assert (funcs["f"]["start"], funcs["f"]["end"]) == (1, 20)
	assert funcs["K.m"]["ccn"] == 1 and funcs["K.m.inner"]["ccn"] == 2


def test_generic_engine_matches_python_parser_on_sample():
	generic = {fn["name"]: fn for fn in qg_complexity.genericFunctions(PY_SAMPLE, LANGS["python"])}
	official = {fn["name"]: fn for fn in qg_complexity.pythonFunctions(PY_SAMPLE)}
	assert generic == official


def test_generic_engine_multiline_signature():
	text = "def f(\n    a,\n):\n    if a:\n        return 1\n    return 2\n\nx = 1\n"
	assert qg_complexity.genericFunctions(text, LANGS["python"]) == [{"name": "f", "start": 1, "end": 6, "ccn": 2}]


GO_SAMPLE = textwrap.dedent("""\
	package p

	type T struct{}

	func (t *T) M(a int) int {
		if a > 0 && a < 9 || a == 20 {
			return 1
		}
		switch a {
		case 1, 2:
			return 2
		default:
			return 3
		}
	}

	func F(xs []int) func() int {
		for _, x := range xs {
			_ = x
		}
		s := "if && for" // if || case
		_ = s
		return func() int {
			if len(xs) > 0 {
				return 1
			}
			return 0
		}
	}
	""")


@pytest.mark.skipif(not os.path.exists(GO), reason="go not installed")
def test_go_helper_counting_rules_and_generic_agreement():
	funcs = qg_complexity.goFunctions({"p.go": GO_SAMPLE}, GO)["p.go"]
	byName = {fn["name"]: fn for fn in funcs}
	assert byName["T.M"]["ccn"] == 1 + 1 + 2 + 1 and (byName["T.M"]["start"], byName["T.M"]["end"]) == (5, 15)
	assert byName["F"]["ccn"] == 1 + 1 + 1
	generic = {fn["start"]: fn["ccn"] for fn in qg_complexity.genericFunctions(GO_SAMPLE, LANGS["go"])}
	assert generic == {fn["start"]: fn["ccn"] for fn in funcs}


def fakeGo(tmp_path, body):
	go = tmp_path / "go"
	go.write_text("#!/bin/sh\n" + body + "\n")
	go.chmod(0o755)
	return str(go)


def test_the_go_helper_past_the_gate_deadline_raises_at_once(tmp_path, monkeypatch):
	monkeypatch.setattr(qg_common, "_deadline", (time.monotonic() - 1, "commit_gate_seconds"))
	began = time.monotonic()
	with pytest.raises(qg_common.DeadlineReached):
		qg_complexity.goFunctions({"p.go": GO_SAMPLE}, fakeGo(tmp_path, "exec sleep 30"))
	assert time.monotonic() - began < 15


def test_a_go_helper_run_past_the_gate_deadline_raises_instead_of_waiting(tmp_path, monkeypatch):
	monkeypatch.setattr(qg_common, "_deadline", (time.monotonic() + 1, "commit_gate_seconds"))
	began = time.monotonic()
	with pytest.raises(qg_common.DeadlineReached):
		qg_complexity.goFunctions({"p.go": GO_SAMPLE}, fakeGo(tmp_path, "exec sleep 30"))
	assert time.monotonic() - began < 15


def test_mask_keeps_layout_and_blanks_strings_and_comments():
	text = 'a = "x # y"  # c\nb = \'\'\'\nif\n\'\'\'\n'
	masked = qg_complexity.mask(text, LANGS["python"])
	assert len(masked) == len(text) and masked.count("\n") == text.count("\n")
	assert "#" not in masked and "if" not in masked and "x" not in masked


def test_go_marker_covers_block():
	text = "func f() {\n\tif x { // coverage:ignore only on windows\n\t\ty()\n\t}\n\tz() // coverage:ignore why\n}\n"
	assert qg_complexity.markedLines(text, LANGS["go"]) == {2, 3, 4, 5}
	assert qg_complexity.markedLines("x = 1  # pragma: no cover\n", LANGS["python"]) == set()


# --- qg_mutate ---------------------------------------------------------------------------------

def mutantsOf(line, lang="python"):
	return qg_mutate.fileMutants("a", line + "\n", {1}, LANGS[lang])


def test_comparison_mutants_skip_arrows_shifts_and_strings():
	ops = [m["op"] for m in mutantsOf('def f(x) -> int: return x <= 3 and s == "<"')]
	assert "<= -> <" in ops and "and -> or" in ops and "== -> !=" in ops
	assert not any(op.startswith(">") for op in ops)
	assert [m["op"] for m in mutantsOf("\tv := <-ch << 2", "go") if m["priority"] == 0] == []


def test_go_mutants_negation_and_literals():
	ops = [m["op"] for m in mutantsOf("\tif !ok && done != true {", "go")]
	assert {"drop !", "&& -> ||", "!= -> ==", "true -> false"} <= set(ops)


def test_delete_only_simple_balanced_statements():
	text = "x = f(\n    1,\n)\ny = 2\nif y:\n    pass\n"
	got = qg_mutate.fileMutants("a", text, {1, 2, 3, 4, 5, 6}, LANGS["python"])
	deleted = [m["line"] for m in got if m["op"] == "delete statement"]
	assert deleted == [4]


def test_select_round_robin_by_line():
	ms = [{"file": "a", "line": n, "priority": p, "op": f"{n}-{p}"} for n in (1, 2, 3) for p in (0, 2)]
	assert [m["op"] for m in qg_mutate.select(ms, 4)] == ["1-0", "2-0", "3-0", "1-2"]


def test_apply_manual():
	text = "a = 1\nb = a < 2\n"
	assert qg_mutate.applyManual(text, {"line": 2, "find": "<", "replace": "<="})[0] == "a = 1\nb = a <= 2\n"
	assert qg_mutate.applyManual(text, {"line": 2, "find": "a", "replace": "z"})[0] is not None
	assert qg_mutate.applyManual(text, {"line": 1, "find": "x", "replace": "y"})[0] is None
	assert qg_mutate.applyManual(text, {"line": 9, "find": "a", "replace": "y"})[0] is None


# --- integration ---------------------------------------------------------------------------------

BASE_CALC = textwrap.dedent("""\
	def legacy(a, b, c, d, e, f, g, h):
	    if a:
	        return 1
	    if b:
	        return 2
	    if c:
	        return 3
	    if d:
	        return 4
	    if e:
	        return 5
	    if f:
	        return 6
	    if g:
	        return 60
	    if h:
	        return 61
	    for x in (a, b):
	        if x:
	            return 7
	    return 0
	""")
BASE_TEST = textwrap.dedent("""\
	from pkg.calc import legacy


	def test_legacy():
	    assert legacy(1, 0, 0, 0, 0, 0, 0, 0) == 1
	""")


def git(repo, *args):
	return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def write(repo, path, text):
	full = os.path.join(repo, path)
	os.makedirs(os.path.dirname(full), exist_ok=True)
	with open(full, "w", encoding="utf-8") as f:
		f.write(textwrap.dedent(text))


@pytest.fixture
def repo(tmp_path):
	root = str(tmp_path / "repo")
	os.makedirs(root)
	git(root, "init", "-q", "-b", "feature/x/topic")
	git(root, "config", "user.email", "t@t")
	git(root, "config", "user.name", "t")
	write(root, ".gitignore", ".hook-only/\nplans/\n__pycache__/\n")
	write(root, "pkg/__init__.py", "")
	write(root, "pkg/calc.py", BASE_CALC)
	write(root, "tests/test_calc.py", BASE_TEST)
	write(root, "pyproject.toml", '[project]\nname = "t"\nversion = "0"\nrequires-python = ">=3.12"\n')
	runner = [UV, "run", "--offline", "--no-project", "--with", "pytest", "--with", "pytest-cov==7.1.0", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
	config = {
		"languages": {"python": {
			"extensions": [".py"],
			"coverage": runner + ["--cov=pkg", "--cov-branch", "--cov-report=lcov:{report}"],
			"coverage_format": "lcov",
			"test": runner + ["-x"],
			"invalid_exit_codes": [2, 3, 4, 5]}},
		"exclude": ["tests/"],
		"scratch_exclude": [".hook-only/", ".git/", "__pycache__"],
		"limits": {"mutation_max": 6},
	}
	write(root, ".claude/quality-gates.json", json.dumps(config))
	git(root, "add", ".")
	git(root, "commit", "-q", "-m", "base")
	git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
	return root


def markBase(repo, mode):
	"""Point the mode's integration branch at HEAD: origin/main in origin mode, the local main in local mode."""
	git(repo, "update-ref", "refs/remotes/origin/main" if mode == "origin" else "refs/heads/main", "HEAD")


def hostEnv():
	"""This process's environment at call time (PYTEST_CURRENT_TEST included), without the session's
	FLOW_INTEGRATION: local mode would point the hooks at a local main these repos don't have."""
	return {k: v for k, v in os.environ.items() if k != "FLOW_INTEGRATION"}


def runHook(name, repo, payload, env=None, args=()):
	full = {"cwd": repo, "session_id": "s", "transcript_path": "/dev/null", **payload}
	result = subprocess.run([sys.executable, runPath(name), *args], input=json.dumps(full), capture_output=True, text=True,
		env={**hostEnv(), "CLAUDE_PROJECT_DIR": repo, "PYTHONPATH": RUN_LIB, **(env or {})}, timeout=900)
	assert result.returncode == 0, result.stderr
	assert "error" not in result.stderr, result.stderr
	return json.loads(result.stdout) if result.stdout.strip() else None


def devStop(repo, agent="dev1", message="done"):
	return runHook("crap-gate.py", repo, {"hook_event_name": "SubagentStop", "agent_type": "developer", "agent_id": agent,
		"last_assistant_message": message, "stop_hook_active": False})


def commit(repo):
	return runHook("crap-gate.py", repo, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
		"tool_input": {"command": "git commit -m impl"}})


COMPLEX = """\
	def busy(a, b, c, d, e, f, g, h, i, j, k):
	    if a:
	        return 1
	    if b:
	        return 2
	    if c:
	        return 3
	    if d:
	        return 4
	    if e:
	        return 5
	    if f:
	        return 6
	    if g:
	        return 7
	    if h:
	        return 8
	    if i:
	        return 9
	    if j:
	        return 10
	    if k:
	        return 11
	    return 0
	"""


def test_crap_gate_blocks_three_times_then_alerts(repo):
	write(repo, "pkg/busy.py", COMPLEX)
	for round_ in (1, 2, 3):
		out = devStop(repo)
		assert out["decision"] == "block" and f"block {round_} of 3" in out["reason"]
		assert "busy (pkg/busy.py:1): CCN 12 > 10" in out["reason"]
		assert "pkg/busy.py:1-24: changed, never executed" in out["reason"]
	assert devStop(repo) is None
	with open(os.path.join(repo, ".hook-only/agent-guard/ALERT")) as f:
		assert "crap-gate: developer (dev1) still fails after 3 blocks" in f.read()
	assert devStop(repo) is None
	with open(os.path.join(repo, ".hook-only/agent-guard/ALERT")) as f:
		assert f.read().count("crap-gate") == 1
	# The ALERT is pending: hook-entry.py's pre entry denies the commit (agent-guard), so the gate runs nothing.
	assert commit(repo) is None
	os.remove(os.path.join(repo, ".hook-only/agent-guard/ALERT"))
	denied = commit(repo)
	assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_needs_test_line_lets_developer_stop(repo):
	write(repo, "pkg/busy.py", COMPLEX)
	assert devStop(repo, message="report\nNEEDS-TEST: busy branches b..k") is None


def test_clean_change_passes_and_commit_uses_pass_cache(repo):
	write(repo, "pkg/small.py", """\
		def small(x):
		    if x > 1:
		        return 2
		    return 3
		""")
	write(repo, "tests/test_small.py", """\
		from pkg.small import small


		def test_small():
		    assert small(5) == 2
		    assert small(0) == 3
		""")
	assert devStop(repo) is None
	assert os.path.isfile(os.path.join(repo, ".hook-only/quality/pass"))
	assert commit(repo) is None


def test_legacy_function_may_not_grow(repo):
	with open(os.path.join(repo, "pkg/calc.py")) as f:
		text = f.read()
	write(repo, "pkg/calc.py", text.replace("    return 0\n", "    return 0 if a else 8\n"))
	out = devStop(repo, agent="dev2")
	assert "legacy (pkg/calc.py:1): CCN 12, was 11 on the base" in out["reason"]
	assert "CRAP" not in out["reason"]
	write(repo, "pkg/calc.py", text.replace("return 7", "return 70"))
	out = devStop(repo, agent="dev3")
	assert out is None or "legacy (pkg/calc.py:1): CCN" not in out["reason"]


def test_markers_need_reasons_and_are_capped(repo):
	write(repo, "pkg/marks.py", """\
		def m(x):
		    if x == 1:  # pragma: no cover
		        return 1
		    if x == 2:  # pragma: no cover -- windows only
		        return 2
		    if x == 3:  # pragma: no cover -- a
		        return 3
		    if x == 4:  # pragma: no cover -- b
		        return 4
		    return 0
		""")
	write(repo, "tests/test_marks.py", "from pkg.marks import m\n\n\ndef test_m():\n    assert m(0) == 0\n")
	out = devStop(repo)
	assert "pkg/marks.py:2: exclusion marker without a reason" in out["reason"]
	assert "4 new exclusion markers; at most 3" in out["reason"]
	assert "never executed" not in out["reason"]


def test_presentation_class_checks_ccn_only(repo):
	write(repo, "plans/worker/topic.json", json.dumps({"class": "presentation"}))
	write(repo, "pkg/view.py", "def view(x):\n    return x + 1\n")
	assert devStop(repo) is None
	write(repo, "pkg/busy.py", COMPLEX)
	out = devStop(repo, agent="dev9")
	assert "CCN 12 > 10" in out["reason"] and "never executed" not in out["reason"]


def test_presentation_class_read_from_main_checkout_in_a_worktree(repo, tmp_path):
	wt = str(tmp_path / "wt")
	git(repo, "worktree", "add", "-q", "-b", "feature/x/other", wt)
	write(repo, "plans/worker/other.json", json.dumps({"class": "presentation"}))
	write(wt, "pkg/view.py", "def view(x):\n    return x + 1\n")
	assert devStop(wt) is None


# --- without quality-gates.json (stage 3b): detected languages run on their home profiles -----------------
# The gate runs from a copy of the tree (config_fixtures.copyHome: its config without the person's files), a fake
# program as the profile's {uv} (the copy's local.json), and no bwrap for these runs: the fake tool lives under
# the test's tmp, which bwrap hides.

def homeConfig(tmp_path, uv):
	"""A hook run (gateRun): from a tree copy whose local.json sets {uv} to uv, unsandboxed (section comment)."""
	home = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home")
	(home / "config" / "local.json").write_text(json.dumps({"toolchains": {"tools": {"uv": uv}}}))
	config_fixtures.useStandIn(home / "config")
	return {"home": home, "env": {}}


def fakeTool(path, body, shebang=None):
	"""An executable Python script at path (default interpreter: this one)."""
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(f"#!{shebang or sys.executable}\nimport sys\n{textwrap.dedent(body)}")
	path.chmod(0o755)
	return str(path)


# A coverage run that writes an empty LCOV report: only the CCN rule has something to find.
EMPTY_REPORT = """\
	for arg in sys.argv[1:]:
	    if arg.startswith("--cov-report=lcov:"):
	        open(arg.split(":", 1)[1], "w").close()
	"""
# pytest without pytest-cov: argparse's usage error, exit 4, no report.
NO_PYTEST_COV = """\
	print("ERROR: usage: pytest [options] [file_or_dir] [file_or_dir] [...]")
	print("pytest: error: unrecognized arguments: --cov --cov-branch")
	sys.exit(4)
	"""


def dropConfig(repo):
	git(repo, "rm", "-q", ".claude/quality-gates.json")
	git(repo, "commit", "-q", "-m", "drop config")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def gateRun(name, repo, payload, run):
	"""(parsed stdout or None, stderr) of a hook run; stderr may hold the gate's own notes. run: {"env": extra
	variables, "home": the tree copy to run the gate from (homeConfig), absent: the tree under test}."""
	full = {"cwd": repo, "session_id": "s", "transcript_path": "/dev/null", **payload}
	script, lib = runPath(name), RUN_LIB
	if "home" in run:
		script = os.path.join(run["home"], "hooks", os.path.relpath(homepath.locate(name), HOOKS))
		lib = os.path.join(run["home"], "hooks", "lib")
	result = subprocess.run([sys.executable, script], input=json.dumps(full), capture_output=True,
		text=True, env={**hostEnv(), "CLAUDE_PROJECT_DIR": repo, "PYTHONPATH": lib, **run["env"]}, timeout=300)
	assert result.returncode == 0, result.stderr
	return (json.loads(result.stdout) if result.stdout.strip() else None), result.stderr


def devStopIn(repo, env):
	return gateRun("crap-gate.py", repo, {"hook_event_name": "SubagentStop", "agent_type": "developer",
		"agent_id": "dev1", "last_assistant_message": "done", "stop_hook_active": False}, env)


def commitIn(repo, env):
	return gateRun("crap-gate.py", repo, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
		"tool_input": {"command": "git commit -m impl"}}, env)


def test_without_config_a_tracked_language_runs_on_its_profile(repo, tmp_path):
	dropConfig(repo)
	env = homeConfig(tmp_path, fakeTool(tmp_path / "bin" / "uv", EMPTY_REPORT))
	write(repo, "pkg/busy.py", COMPLEX)
	out, _ = devStopIn(repo, env)
	assert out["decision"] == "block" and "CCN" in out["reason"]
	assert "python was enabled automatically" in out["reason"] and '"languages": {"python": false}' in out["reason"]
	out, _ = commitIn(repo, env)
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "CCN" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_without_config_and_without_a_profiled_language_nothing_is_checked(tmp_path):
	root = str(tmp_path / "docs")
	os.makedirs(root)
	git(root, "init", "-q", "-b", "feature/x/topic")
	write(root, "README.md", "x\n")
	git(root, "add", ".")
	git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
	git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
	write(root, "notes.txt", "more\n")
	assert devStop(root) is None and commit(root) is None


def test_a_coverage_run_without_pytest_cov_blocks_naming_the_key_and_its_source(repo, tmp_path):
	dropConfig(repo)
	env = homeConfig(tmp_path, fakeTool(tmp_path / "bin" / "uv", NO_PYTEST_COV))
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	out, _ = devStopIn(repo, env)
	reason = out["reason"]
	assert out["decision"] == "block" and "coverage for python produced no report (exit 4)" in reason
	assert "unrecognized arguments: --cov" in reason
	profile = f"{os.path.join(env['home'], 'config', os.path.basename(homeconfig.path('toolchains')))} section 'toolchains'"
	assert f"languages.python.coverage (value from the home profile {profile})" in reason
	assert f"override it in {qg_common.CONFIG_FILE}" in reason


def test_a_coverage_command_that_never_starts_spares_the_developer_and_refuses_the_commit(repo, tmp_path):
	dropConfig(repo)
	env = homeConfig(tmp_path, fakeTool(tmp_path / "bin" / "uv", "sys.exit(0)\n", shebang="/nonexistent/python"))
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	out, err = devStopIn(repo, env)
	assert out is None and "not checked, environment problem" in err and "didn't start" in err  # bwrap's execvp
	assert not os.path.exists(os.path.join(repo, ".hook-only", "quality", "dev-dev1.json"))
	out, _ = commitIn(repo, env)
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "never started" in reason
	assert "languages.python.coverage" in reason


def test_a_missing_tool_fails_the_load_and_refuses_the_commit_without_blocking_the_developer(repo, tmp_path):
	dropConfig(repo)
	env = homeConfig(tmp_path, str(tmp_path / "nowhere" / "uv"))
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	out, err = devStopIn(repo, env)
	assert out is None and "config problem" in err
	out, _ = commitIn(repo, env)
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert "config doesn't load" in reason and "tool {uv}" in reason and '"languages": {"python": false}' in reason


@pytest.mark.parametrize("body,expected", [
	('print("sh: 1: uv: not found")\nsys.exit(127)\n', "the test command didn't start"),
	(NO_PYTEST_COV, "the tests didn't run (exit 4, red_invalid_exit_codes)"),
	("sys.exit(5)\n", "the tests didn't run (exit 5, red_invalid_exit_codes)"),
])
def test_a_first_tests_commit_whose_tests_never_ran_is_refused(repo, tmp_path, body, expected):
	config = json.loads(git(repo, "show", f"HEAD:{qg_common.CONFIG_FILE}"))
	config["languages"]["python"]["test"] = [fakeTool(tmp_path / "bin" / "fake-test", body)]
	write(repo, qg_common.CONFIG_FILE, json.dumps(config))
	git(repo, "commit", "-q", "-am", "test command")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
	write(repo, "tests/test_new.py", "from pkg.missing import feature\n\n\ndef test_new():\n    assert feature() == 1\n")
	git(repo, "add", "tests/test_new.py")
	home = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home")
	config_fixtures.useStandIn(home / "config")  # the fake test command lives under tmp, which bwrap hides
	out, _ = commitIn(repo, {"home": home, "env": {}})
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and expected in reason
	assert f"languages.python.test (value from {qg_common.CONFIG_FILE})" in reason


def reviewerStopIn(repo, env, agent="r1"):
	return gateRun("mutation.py", repo, {"hook_event_name": "SubagentStop", "agent_type": "code-reviewer",
		"agent_id": agent, "last_assistant_message": "report", "stop_hook_active": False}, env)


def test_mutation_reports_a_baseline_that_never_started_naming_the_key(repo, tmp_path):
	dropConfig(repo)
	env = homeConfig(tmp_path, fakeTool(tmp_path / "bin" / "uv", "sys.exit(0)\n", shebang="/nonexistent/python"))
	write(repo, "pkg/grade.py", MUTABLE)
	out, _ = reviewerStopIn(repo, env)
	reason = out["reason"]
	assert "baseline didn't run for python: the coverage command didn't start" in reason
	assert "languages.python.coverage (value from the home profile" in reason
	assert "note: python was enabled automatically" in reason
	assert mutationResults(repo)["results"] == []


def test_mutation_holds_the_reviewer_once_when_the_config_does_not_load(repo, tmp_path):
	dropConfig(repo)
	env = homeConfig(tmp_path, str(tmp_path / "nowhere" / "uv"))
	write(repo, "pkg/grade.py", MUTABLE)
	out, _ = reviewerStopIn(repo, env)
	assert out["decision"] == "block" and "config doesn't load" in out["reason"] and "tool {uv}" in out["reason"]
	assert reviewerStopIn(repo, env)[0] is None


def test_run_command_never_hands_back_a_start_failure_as_an_exit_code(tmp_path, monkeypatch):
	config_fixtures.standInHere(monkeypatch, homeconfig)
	with pytest.raises(qg_common.NotStarted, match="is not an executable file"):
		qg_common.runCommand([str(tmp_path / "missing")], str(tmp_path))
	with pytest.raises(qg_common.NotStarted, match="is not on PATH"):
		qg_common.runCommand(["no-such-program-qg"], str(tmp_path))
	bad = fakeTool(tmp_path / "bad", "", shebang="/nonexistent/python")
	with pytest.raises(qg_common.NotStarted, match="didn't start"):  # bwrap's own "execvp" failure
		qg_common.runCommand([bad], str(tmp_path))
	for body in ("sys.exit(127)\n", "sys.exit(126)\n", 'print("bwrap: execvp uv: No such file or directory")\nsys.exit(1)\n'):
		with pytest.raises(qg_common.NotStarted, match="didn't start"):
			qg_common.runCommand([fakeTool(tmp_path / "t", body)], str(tmp_path))
	assert qg_common.runCommand([fakeTool(tmp_path / "ok", "print('bwrap: execvp x')\n")], str(tmp_path))[0] == 0
	assert qg_common.runCommand([fakeTool(tmp_path / "red", "sys.exit(1)\n")], str(tmp_path))[0] == 1


MUTABLE = """\
	def grade(score):
	    if score >= 90:
	        return "A"
	    if score >= 60:
	        return "B"
	    return "C"
	"""
WEAK_TEST = """\
	from pkg.grade import grade


	def test_grade():
	    assert grade(95) == "A"
	    assert grade(10) == "C"
	"""


STRONG_TEST = """\
	from pkg.grade import grade


	def test_grade():
	    assert grade(90) == "A"
	    assert grade(89) == "B"
	    assert grade(60) == "B"
	    assert grade(59) == "C"
	"""


def reviewer(repo, event, agent="r1", args=(), **extra):
	return runHook("mutation.py", repo, {"hook_event_name": event, "agent_type": "code-reviewer", "agent_id": agent, **extra},
		args=args)


def firstToolCall(repo, agent="r1", command="git diff"):
	"""The reviewer's first tool call, as the async frontmatter entry sees it (`mutation.py start`)."""
	return reviewer(repo, "PreToolUse", agent=agent, args=("start",), tool_name="Bash", tool_input={"command": command})


def mutationResults(repo):
	directory = os.path.join(repo, ".hook-only/hook-read-only/mutation")
	files = [f for f in os.listdir(directory) if f.startswith("auto-") and f.endswith(".json")]
	assert len(files) == 1
	return qg_common.loadJson(os.path.join(directory, files[0]))


def test_mutation_scan_reports_survivors_once(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	assert firstToolCall(repo) is None
	result = mutationResults(repo)
	statuses = {(m["line"], m["op"]): m["status"] for m in result["results"]}
	assert statuses[(2, ">= -> >")] == "survived"
	assert statuses[(4, ">= -> >")] == "survived"
	assert statuses[(3, "delete statement")] == "caught"
	assert (6, "delete statement") in statuses and not any(line == 5 for line, _ in statuses)  # line 5 never ran
	assert result["baseline_failed"] == [] and len(result["results"]) == 4
	out = reviewer(repo, "SubagentStop", last_assistant_message="report")
	assert out["decision"] == "block" and "pkg/grade.py:2  >= -> >" in out["reason"]
	assert reviewer(repo, "SubagentStop", last_assistant_message="report", stop_hook_active=True) is None


PYTEST_RUNNER = [UV, "run", "--offline", "--no-project", "--with", "pytest", "--with", "pytest-cov==7.1.0", "python", "-m", "pytest",
	"-q", "-p", "no:cacheprovider"]
CLI = """\
	import sys


	def main():
	    value = int(sys.argv[1])
	    print(value + 1)


	if __name__ == "__main__":
	    main()
	"""
CLI_TEST = """\
	import os
	import subprocess
	import sys

	ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


	def run(*args):
	    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()


	def test_cli():
	    assert run("-m", "pkg.cli", "2") == ["3"]


	def test_grade_in_a_subprocess():
	    code = "from pkg.grade import grade; print(grade(90), grade(60))"
	    assert run("-c", code) == ["A", "B"]
	"""


def selectRepo(repo, guard=True):
	"""Turns on test selection: "select" for python, guard-paths tests, coverage of subprocesses; at the base."""
	config = json.loads(open(os.path.join(repo, ".claude/quality-gates.json")).read())
	config["languages"]["python"]["select"] = {"kind": "pytest-contexts", "pytest": PYTEST_RUNNER,
		"python": PYTEST_RUNNER[:PYTEST_RUNNER.index("-m")], "fallback": "{component}/tests"}
	write(repo, ".claude/quality-gates.json", json.dumps(config))
	if guard:
		write(repo, ".claude/guard-paths.json", json.dumps({"tests": ["tests/"]}))
	write(repo, "pyproject.toml", '[project]\nname = "t"\nversion = "0"\nrequires-python = ">=3.12"\n\n'
		'[tool.coverage.run]\npatch = ["subprocess"]\n')
	git(repo, "add", ".")
	git(repo, "commit", "-q", "-m", "select")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def latestResult(repo):
	directory = os.path.join(repo, ".hook-only/hook-read-only/mutation")
	results = [qg_common.loadJson(os.path.join(directory, f)) for f in os.listdir(directory)
		if f.startswith("auto-") and f.endswith(".json")]
	return max(results, key=lambda r: r["at"])


def test_selected_scan_runs_only_the_tests_that_execute_each_line(repo):
	selectRepo(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	firstToolCall(repo)
	result = latestResult(repo)
	statuses = {(m["line"], m["op"]): m for m in result["results"]}
	assert statuses[(2, ">= -> >")]["status"] == "survived"
	assert statuses[(4, ">= -> >")]["status"] == "survived"
	caught = statuses[(3, "delete statement")]
	assert caught["status"] == "caught" and caught["caught_by"] == "tests/test_grade.py::test_grade"
	# Only the test that runs grade(); the base's test_legacy never does.
	assert {n for m in result["results"] for n in m["tests"]} == {"tests/test_grade.py::test_grade"}
	assert result["reused"] == 0 and result["baseline_failed"] == []


def test_a_line_also_run_in_a_subprocess_confirms_a_survivor_with_the_fallback_tests(repo):
	selectRepo(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "pkg/cli.py", CLI)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	write(repo, "tests/test_cli.py", CLI_TEST)
	firstToolCall(repo)
	byKey = {(m["file"], m["line"], m["op"]): m for m in latestResult(repo)["results"]}
	# grade line 2 ran in test_grade (direct, too weak) and in a subprocess: the survivor is rechecked and caught.
	edge = byKey[("pkg/grade.py", 2, ">= -> >")]
	assert edge["tests"] == ["tests/test_grade.py::test_grade"] and edge["partial"] is True
	assert edge["status"] == "caught" and edge["caught_by"] == "tests/test_cli.py::test_grade_in_a_subprocess"
	# cli.py ran only in a subprocess: no test is known, the fallback (no pkg/tests: every test) catches it.
	cli = [m for (path, _, _), m in byKey.items() if path == "pkg/cli.py"]
	assert cli and all(m["tests"] == [] and m["status"] == "caught" for m in cli)


ORDER_DEPENDENT_TEST = """\
	import pkg.grade as g


	def test_setup():
	    g.READY = True


	def test_needs_setup():
	    assert getattr(g, "READY", False)
	    assert g.grade(95) == "A"
	"""


def test_a_catch_by_a_test_that_fails_alone_is_decided_by_the_whole_suite(repo):
	selectRepo(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_state.py", ORDER_DEPENDENT_TEST)
	firstToolCall(repo)
	statuses = {(m["line"], m["op"]): m for m in latestResult(repo)["results"]}
	# Selected alone, test_needs_setup fails without test_setup even unmutated: not a catch. The whole suite
	# passes: survived, as in a scan without selection.
	edge = statuses[(2, ">= -> >")]
	assert edge["tests"] == ["tests/test_state.py::test_needs_setup"]
	assert edge["status"] == "survived"


def test_a_later_scan_keeps_what_test_changes_cannot_affect_and_reruns_the_rest(repo):
	selectRepo(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	firstToolCall(repo, agent="r1")
	first = latestResult(repo)
	kept = sum(1 for m in first["results"] if m["status"] == "invalid" or (m["status"] == "caught" and m["caught_by"]))
	assert kept and any(m["status"] == "survived" for m in first["results"])

	# QA adds a test file: the survivors are rerun with it and caught; the rest is kept.
	write(repo, "tests/test_grade_edges.py", STRONG_TEST.replace("def test_grade", "def test_edges"))
	firstToolCall(repo, agent="r2")
	second = latestResult(repo)
	assert second["reused"] == kept
	assert all(m["status"] != "survived" for m in second["results"])
	assert len(second["results"]) == len(first["results"])
	out = reviewer(repo, "SubagentStop", agent="r2", last_assistant_message="report")
	assert out is None

	# A conftest can change tests that didn't change: full scan.
	write(repo, "tests/conftest.py", "VALUE = 1\n")
	firstToolCall(repo, agent="r3")
	assert latestResult(repo)["reused"] == 0

	# Production code changed: full scan.
	write(repo, "pkg/grade.py", MUTABLE + "\n\ndef extra():\n    return 1\n")
	firstToolCall(repo, agent="r4")
	assert latestResult(repo)["reused"] == 0


def test_a_later_scan_with_a_red_new_test_reports_the_baseline_instead_of_counting_catches(repo):
	selectRepo(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	firstToolCall(repo, agent="r1")
	write(repo, "tests/test_grade_red.py", "from pkg.grade import grade\n\n\ndef test_red():\n    assert grade(95) == \"Z\"\n")
	firstToolCall(repo, agent="r2")
	result = latestResult(repo)
	assert result["baseline_failed"] and result["results"] == []


def test_without_guard_test_paths_a_later_scan_is_a_full_scan(repo):
	selectRepo(repo, guard=False)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	firstToolCall(repo, agent="r1")
	write(repo, "tests/test_grade_edges.py", STRONG_TEST.replace("def test_grade", "def test_edges"))
	firstToolCall(repo, agent="r2")
	assert latestResult(repo)["reused"] == 0


def test_mutation_stop_runs_scan_when_start_never_ran(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", STRONG_TEST)
	out = reviewer(repo, "SubagentStop", agent="r2", last_assistant_message="report")
	assert out is None
	assert all(m["status"] != "survived" for m in mutationResults(repo)["results"])


def test_mutation_failing_baseline_is_reported(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST.replace('== "C"', '== "Z"'))
	firstToolCall(repo, agent="r3")
	assert mutationResults(repo)["baseline_failed"]
	out = reviewer(repo, "SubagentStop", agent="r3", last_assistant_message="report")
	assert "baseline failed for python" in out["reason"]


def breakSelect(repo):
	"""Points the base's select.pytest at a program that doesn't exist: the selection can't be used."""
	config = json.loads(git(repo, "show", f"HEAD:{qg_common.CONFIG_FILE}"))
	config["languages"]["python"]["select"]["pytest"] = ["/nonexistent/pytest"]
	write(repo, qg_common.CONFIG_FILE, json.dumps(config))
	git(repo, "commit", "-q", "-am", "broken select")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def test_unusable_select_falls_back_to_the_whole_test_command_and_says_so(repo):
	selectRepo(repo)
	breakSelect(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	firstToolCall(repo)
	result = latestResult(repo)
	assert result["baseline_failed"] == []
	assert len(result["select_fallback"]) == 1
	assert result["select_fallback"][0].startswith("python: test selection not used, its command didn't start")
	edge = {(m["line"], m["op"]): m for m in result["results"]}[(2, ">= -> >")]
	assert edge["status"] == "survived" and "tests" not in edge  # the whole test command, no selection
	out = reviewer(repo, "SubagentStop", last_assistant_message="report")
	assert "note: python: test selection not used" in out["reason"]


def test_coverage_data_without_test_contexts_cannot_drive_the_selection():
	mutation = loadHook("mutation.py", "mutation_hook")
	assert not mutation.hasTestContexts({"a.py": {"executed": [1, 2], "contexts": {"1": [""], "2": []}}})
	assert mutation.hasTestContexts({"a.py": {"executed": [1], "contexts": {"1": ["", "t.py::t|run"]}}})
	assert mutation.hasTestContexts({"a.py": {"executed": [], "contexts": {}}})


def test_merge_review_skips_scan(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	directory = os.path.join(repo, ".hook-only/hook-read-only/mutation")
	os.makedirs(directory)
	open(os.path.join(directory, "merge-review-pending"), "w").close()
	firstToolCall(repo, agent="m1")
	assert reviewer(repo, "SubagentStop", agent="m1", last_assistant_message="report") is None
	assert not [f for f in os.listdir(directory) if f.startswith("auto-")]


def test_start_mode_scans_once_and_plain_mode_ignores_other_commands(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	assert reviewer(repo, "PreToolUse", tool_name="Read", tool_input={"file_path": "x"}) is None
	assert not os.path.isdir(os.path.join(repo, ".hook-only/hook-read-only/mutation")) or not [
		f for f in os.listdir(os.path.join(repo, ".hook-only/hook-read-only/mutation")) if f.startswith("auto-")]
	assert firstToolCall(repo, command="ls") is None
	first = mutationResults(repo)
	assert reviewer(repo, "PreToolUse", args=("start",), tool_name="Read", tool_input={"file_path": "x"}) is None
	assert mutationResults(repo)["at"] == first["at"]


def test_subagent_start_registration_still_works(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	assert reviewer(repo, "SubagentStart") is None
	assert mutationResults(repo)["results"]


def test_merge_flag_claimed_at_stop_when_no_tool_call(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	directory = os.path.join(repo, ".hook-only/hook-read-only/mutation")
	os.makedirs(directory)
	open(os.path.join(directory, "merge-review-pending"), "w").close()
	assert reviewer(repo, "SubagentStop", agent="m2", last_assistant_message="report") is None
	assert os.path.isfile(os.path.join(directory, "merge-m2"))
	assert not [f for f in os.listdir(directory) if f.startswith("auto-")]


def test_merge_flag_not_taken_by_a_reviewer_already_running(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	firstToolCall(repo, agent="r5")
	directory = os.path.join(repo, ".hook-only/hook-read-only/mutation")
	open(os.path.join(directory, "merge-review-pending"), "w").close()
	firstToolCall(repo, agent="r5", command="ls")
	assert os.path.isfile(os.path.join(directory, "merge-review-pending"))


def test_mutate_request_runs_manual_mutants(repo, tmp_path):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	spec = tmp_path / "spec.json"
	spec.write_text(json.dumps([
		{"file": "pkg/grade.py", "line": 4, "find": ">= 60", "replace": "> 60"},
		{"file": "pkg/grade.py", "line": 6, "find": '"C"', "replace": '"D"'},
		{"file": "pkg/grade.py", "line": 2, "find": "zz", "replace": "y"},
		{"file": "../etc/passwd", "line": 1, "find": "a", "replace": "b"},
	]))
	out = reviewer(repo, "PreToolUse", tool_name="Bash", tool_input={"command": f"mutate-request {spec}"})
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
	assert "pkg/grade.py:4  `>= 60` -> `> 60`  -> survived" in reason
	assert "pkg/grade.py:6  `\"C\"` -> `\"D\"`  -> caught" in reason
	assert "occurs 0 times" in reason and "rejected (not a source file" in reason
	with open(os.path.join(repo, "pkg/grade.py")) as f:
		assert f.read() == textwrap.dedent(MUTABLE)


def test_mutate_request_refused_for_other_agents(repo):
	out = runHook("mutation.py", repo, {"hook_event_name": "PreToolUse", "agent_type": "developer", "agent_id": "d",
		"tool_name": "Bash", "tool_input": {"command": "mutate-request /tmp/x.json"}})
	assert "only for the code reviewer" in out["hookSpecificOutput"]["permissionDecisionReason"]
	assert runHook("mutation.py", repo, {"hook_event_name": "PreToolUse", "agent_type": "code-reviewer", "agent_id": "r",
		"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


# ---- mutate-request like the scan: the reviewer's call was held 10+ min, unmarked and on its budget


def manualRequest(repo, tmp_path, monkeypatch, specs, agent="r1"):
	"""Loads mutation.py in process for one mutate-request: a budget state for the agent (started 1000.0),
	the host's sandbox switch, and every runMutant call recorded as (argv or None for the test command, the
	budget's hook_wait mark at that moment); each recorded run takes at least 0.6 s, so two of them make a
	wait excludeWait counts (>= 1 s) however warm the test runner is. Returns (module, call, calls); call()
	runs the request (its output goes to stdout)."""
	config_fixtures.usableSandboxHere(monkeypatch, homeconfig)
	monkeypatch.delenv("FLOW_INTEGRATION", raising=False)
	mutation = loadHook("mutation.py", "mutation_hook")
	budgetDir = os.path.join(repo, ".hook-only", "budget")
	os.makedirs(budgetDir, exist_ok=True)
	with open(os.path.join(budgetDir, f"{agent}.json"), "w") as f:
		json.dump({"minutes": 17, "started": 1000.0}, f)
	calls, realRun = [], mutation.runMutant

	def recording(scratch, root, path, mutatedText, langConfig, timeout, protect=(), argv=None):
		calls.append((argv, budgetState(repo, agent).get("hook_wait")))
		time.sleep(0.6)
		return realRun(scratch, root, path, mutatedText, langConfig, timeout, protect, argv)

	monkeypatch.setattr(mutation, "runMutant", recording)
	spec = tmp_path / "spec.json"
	spec.write_text(json.dumps(specs))

	def call():
		config, limits = qg_common.loadConfig(repo)
		data = {"hook_event_name": "PreToolUse", "agent_type": "code-reviewer", "agent_id": agent, "tool_name": "Bash",
			"tool_input": {"command": f"mutate-request {spec}"}}
		mutation.onMutateRequest(data, repo, config, limits, mutation.mutationDir(repo))

	return mutation, call, calls


def budgetState(repo, agent):
	return qg_common.loadJson(os.path.join(repo, ".hook-only", "budget", f"{agent}.json"))


def hookWaits(repo):
	path = os.path.join(repo, ".hook-only", "budget", "events.jsonl")
	if not os.path.exists(path):
		return []
	with open(path) as f:
		return [json.loads(line) for line in f if json.loads(line)["event"] == "HookWait"]


MANUAL_SPECS = [
	{"file": "pkg/grade.py", "line": 4, "find": ">= 60", "replace": "> 60"},
	{"file": "pkg/grade.py", "line": 6, "find": '"C"', "replace": '"D"'},
	{"file": "pkg/grade.py", "line": 2, "find": "zz", "replace": "y"},
]


def test_mutate_request_marks_the_wait_and_without_select_runs_every_mutant_on_the_whole_suite(repo, tmp_path,
		monkeypatch, capsys):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	_, call, calls = manualRequest(repo, tmp_path, monkeypatch, MANUAL_SPECS)
	call()
	reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
	assert reason.splitlines()[1:] == [
		"pkg/grade.py:4  `>= 60` -> `> 60`  -> survived",
		"pkg/grade.py:6  `\"C\"` -> `\"D\"`  -> caught",
		"pkg/grade.py:2  `zz` -> `y`  -> rejected (`zz` occurs 0 times on line 2, needs exactly 1)"]
	# One run per mutant, each the whole test command, all while the wait is marked.
	assert [argv for argv, _ in calls] == [None, None]
	assert all(mark and mark["what"] == "reviewer's mutants" and mark["max_seconds"] == 1800 for _, mark in calls)
	# Afterwards the mark is gone and the held time (two runs, >= 1.2 s) is out of the budget.
	state = budgetState(repo, "r1")
	assert "hook_wait" not in state and state["started"] >= 1001.2
	assert [e["agent_id"] for e in hookWaits(repo)] == ["r1"]


def test_mutate_request_with_select_runs_only_the_covering_tests_and_confirms_survivors_with_the_whole_suite(repo,
		tmp_path, monkeypatch, capsys):
	selectRepo(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	_, call, calls = manualRequest(repo, tmp_path, monkeypatch, MANUAL_SPECS[:2])
	call()
	reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
	assert reason.splitlines()[1:] == ["pkg/grade.py:4  `>= 60` -> `> 60`  -> survived",
		"pkg/grade.py:6  `\"C\"` -> `\"D\"`  -> caught"]
	config, _ = qg_common.loadConfig(repo)
	full = config["languages"]["python"]["test"]
	selected = [argv for argv, _ in calls if argv[:len(PYTEST_RUNNER)] == PYTEST_RUNNER and argv[-2:] == [
		"-x", "tests/test_grade.py::test_grade"]]
	# The survivor: its covering test, then the whole test command; the catch: its covering test only. The
	# base's test_legacy never runs grade() and is never selected.
	assert len(calls) == 3 and len(selected) == 2
	assert calls[0][0] in selected and calls[1][0] == full and calls[2][0] in selected
	assert not any("test_legacy" in " ".join(argv) for argv, _ in calls)
	assert "hook_wait" not in budgetState(repo, "r1")


def test_mutate_request_with_unusable_select_runs_the_whole_suite_and_adds_a_note(repo, tmp_path, monkeypatch,
		capsys):
	selectRepo(repo)
	breakSelect(repo)
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	_, call, calls = manualRequest(repo, tmp_path, monkeypatch, MANUAL_SPECS[:2])
	call()
	lines = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"].splitlines()[1:]
	assert lines[:2] == ["pkg/grade.py:4  `>= 60` -> `> 60`  -> survived", "pkg/grade.py:6  `\"C\"` -> `\"D\"`  -> caught"]
	assert len(lines) == 3 and lines[2].startswith("note: python: test selection not used")
	assert [argv for argv, _ in calls] == [None, None]


def test_mutate_request_clears_the_wait_when_it_fails(repo, tmp_path, monkeypatch, capsys):
	mutation, call, _ = manualRequest(repo, tmp_path, monkeypatch, MANUAL_SPECS)

	def noSandbox(*args):
		assert budgetState(repo, "r1")["hook_wait"]["what"] == "reviewer's mutants"
		raise qg_common.SandboxError("bwrap unavailable: test")

	monkeypatch.setattr(mutation, "runManual", noSandbox)
	call()
	reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
	assert "environment problem" in reason and "hook_wait" not in budgetState(repo, "r1")

	def crash(*args):
		assert "hook_wait" in budgetState(repo, "r1")
		time.sleep(1.1)
		raise RuntimeError("boom")

	monkeypatch.setattr(mutation, "runManual", crash)
	with pytest.raises(RuntimeError):
		call()
	state = budgetState(repo, "r1")
	assert "hook_wait" not in state and state["started"] >= 1001.1
	assert [e["agent_id"] for e in hookWaits(repo)] == ["r1"]


# ---- hand-back gates: a block at Stop after SubagentHandback came too late


def devHandback(repo, agent="dev1", tool_input=None):
	return runHook("crap-gate.py", repo, {"hook_event_name": "PreToolUse", "agent_type": "developer", "agent_id": agent,
		"tool_name": "SubagentHandback", "tool_input": tool_input or {"report": "done"}})


def test_developer_handback_is_denied_with_the_findings(repo):
	write(repo, "pkg/busy.py", COMPLEX)
	out = devHandback(repo)
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert "block 1 of 3" in reason and "busy (pkg/busy.py:1): CCN 12 > 10" in reason
	assert "hand back your full report again" in reason


def test_developer_handback_and_stop_share_the_block_count(repo):
	write(repo, "pkg/busy.py", COMPLEX)
	assert "block 1 of 3" in devHandback(repo)["hookSpecificOutput"]["permissionDecisionReason"]
	assert "block 2 of 3" in devStop(repo)["reason"]
	assert "block 3 of 3" in devHandback(repo)["hookSpecificOutput"]["permissionDecisionReason"]
	assert devHandback(repo) is None
	with open(os.path.join(repo, ".hook-only/agent-guard/ALERT")) as f:
		assert "still fails after 3 blocks" in f.read()


def test_developer_handback_with_needs_test_in_any_field_passes(repo):
	write(repo, "pkg/busy.py", COMPLEX)
	assert devHandback(repo, tool_input={"x": {"y": ["report", "line\nNEEDS-TEST: busy branches"]}}) is None


def test_clean_developer_handback_passes_and_writes_pass(repo):
	write(repo, "pkg/small.py", "def small(x):\n    return x + 1\n")
	write(repo, "tests/test_small.py", "from pkg.small import small\n\n\ndef test_small():\n    assert small(1) == 2\n")
	assert devHandback(repo) is None
	assert os.path.isfile(os.path.join(repo, ".hook-only/quality/pass"))


def reviewerHandback(repo, agent="r1"):
	return reviewer(repo, "PreToolUse", agent=agent, tool_name="SubagentHandback", tool_input={"report": "report"})


def test_reviewer_handback_held_until_scan_then_survivors_then_allowed(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	out = reviewerHandback(repo, agent="rh1")
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert reason.startswith("mutation: hand-back held") and "pkg/grade.py:2  >= -> >" in reason
	assert reviewerHandback(repo, agent="rh1") is None
	assert reviewer(repo, "SubagentStop", agent="rh1", last_assistant_message="report") is None


def mutationSummary(repo, agent):
	"""The readable trace mutation.py leaves in .hook-only/quality for one reviewer."""
	directory = os.path.join(repo, ".hook-only", "quality")
	files = [f for f in os.listdir(directory) if f.startswith(f"mutation-{agent}-")]
	assert len(files) == 1
	return qg_common.loadJson(os.path.join(directory, files[0]))


def test_reviewer_handback_without_survivors_passes(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", STRONG_TEST)
	assert reviewerHandback(repo, agent="rh2") is None
	# nothing to judge, so the reviewer hears nothing; the trace says the scan ran and caught all
	summary = mutationSummary(repo, "rh2")
	assert summary["status"] == "all_caught" and summary["counts"]["survived"] == 0


def test_reviewer_handback_with_survivors_leaves_a_delivered_trace(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	write(repo, "tests/test_grade.py", WEAK_TEST)
	reviewerHandback(repo, agent="rh5")
	summary = mutationSummary(repo, "rh5")
	assert summary["status"] == "delivered" and summary["counts"]["survived"] == 2


def test_reviewer_handback_says_when_the_scan_gave_no_result(repo):
	config = json.loads(open(os.path.join(repo, ".claude/quality-gates.json")).read())
	config["limits"]["mutation_wait_seconds"] = 1
	write(repo, ".claude/quality-gates.json", json.dumps(config))
	git(repo, "commit", "-q", "-am", "short wait")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
	write(repo, "pkg/grade.py", MUTABLE)
	# another process (this test) holds the scan's lock, so no result arrives within the wait
	fp = qg_common.fingerprint(repo, qg_common.defaultBase(repo))
	directory = os.path.join(repo, ".hook-only/hook-read-only/mutation")
	os.makedirs(directory, exist_ok=True)
	write(repo, f".hook-only/hook-read-only/mutation/auto-{fp}.lock", str(os.getpid()))
	out = reviewerHandback(repo, agent="rh6")
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "didn't finish" in reason
	assert mutationSummary(repo, "rh6")["status"] == "no_result"
	assert reviewerHandback(repo, agent="rh6") is None


def test_handback_from_other_agents_is_ignored_by_mutation(repo):
	write(repo, "pkg/grade.py", MUTABLE)
	out = runHook("mutation.py", repo, {"hook_event_name": "PreToolUse", "agent_type": "qa", "agent_id": "q1",
		"tool_name": "SubagentHandback", "tool_input": {"report": "r"}})
	assert out is None


def test_exclude_wait_shifts_budget_start_and_logs_event(tmp_path):
	root = str(tmp_path)
	budgetDir = os.path.join(root, ".hook-only", "budget")
	os.makedirs(budgetDir)
	with open(os.path.join(budgetDir, "a1.json"), "w") as f:
		json.dump({"minutes": 10, "started": 1000.0}, f)
	budget_state.excludeWait(root, "a1", 120.0)
	budget_state.excludeWait(root, "a1", 0.4)
	assert qg_common.loadJson(os.path.join(budgetDir, "a1.json"))["started"] == 1120.0
	with open(os.path.join(budgetDir, "events.jsonl")) as f:
		events = [json.loads(line) for line in f]
	assert [(e["event"], e["agent_id"], e["seconds"]) for e in events] == [("HookWait", "a1", 120.0)]
	budget_state.excludeWait(root, "missing", 5.0)



def test_a_wait_is_marked_while_it_lasts_and_cleared_when_it_ends(tmp_path):
	root = str(tmp_path)
	budgetDir = os.path.join(root, ".hook-only", "budget")
	os.makedirs(budgetDir)
	stateFile = os.path.join(budgetDir, "a1.json")
	with open(stateFile, "w") as f:
		json.dump({"minutes": 10, "started": 1000.0}, f)
	budget_state.markWait(root, "a1", "mutation scan", 3300)
	mark = qg_common.loadJson(stateFile)["hook_wait"]
	assert (mark["what"], mark["max_seconds"]) == ("mutation scan", 3300)
	assert mark["since"] > 1000.0
	budget_state.excludeWait(root, "a1", 0.4)
	state = qg_common.loadJson(stateFile)
	assert "hook_wait" not in state and state["started"] == 1000.0
	assert not os.path.exists(os.path.join(budgetDir, "events.jsonl"))
	budget_state.markWait(root, "a1", "mutation scan", 3300)
	budget_state.excludeWait(root, "a1", 60.0)
	state = qg_common.loadJson(stateFile)
	assert "hook_wait" not in state and state["started"] == 1060.0
	budget_state.markWait(root, "missing", "mutation scan", 3300)
	assert not os.path.exists(os.path.join(budgetDir, "missing.json"))

def workerStop(repo, message, tasks, active=False):
	return runHook("worker-stop.py", str(repo), {"hook_event_name": "Stop", "last_assistant_message": message,
		"background_tasks": tasks, "stop_hook_active": active}, env={"CLAUDE_WORKER_MINUTES": "30"})


def test_worker_stop_lets_status_end_while_a_subagent_runs(tmp_path):
	assert workerStop(tmp_path, "STATUS: done\nall good", [{"agent_type": "code-reviewer"}]) is None


def test_worker_stop_still_holds_status_under_a_background_command(tmp_path):
	out = workerStop(tmp_path, "STATUS: done", [{"type": "shell", "description": "dev server"}])
	assert out["decision"] == "block" and "dev server" in out["reason"]


def test_worker_stop_pause_while_subagent_runs_and_format_when_idle(tmp_path):
	assert workerStop(tmp_path, "waiting for the reviewer", [{"agent_type": "code-reviewer"}]) is None
	assert "STATUS: done" in workerStop(tmp_path, "finished", [])["reason"]
	assert workerStop(tmp_path, "finished", [], active=True) is None


# --- config from the diff base, sandboxed commands ----------------------------------------------

def test_working_tree_config_is_ignored(repo):
	"""An edited coverage command in the working tree never runs: the committed one does."""
	marker = os.path.join(repo, "MARKER")
	config = json.loads(git(repo, "show", "HEAD:.claude/quality-gates.json"))
	config["languages"]["python"]["coverage"] = [sys.executable, "-c", f"open({marker!r}, 'w').close()"]
	write(repo, ".claude/quality-gates.json", json.dumps(config))
	write(repo, "pkg/busy.py", COMPLEX)
	result = devStop(repo)
	assert not os.path.exists(marker)
	assert result and result.get("decision") == "block" and "CCN" in result.get("reason", "")


def test_config_deleted_in_working_tree_still_applies(repo):
	os.remove(os.path.join(repo, ".claude/quality-gates.json"))
	write(repo, "pkg/busy.py", COMPLEX)
	result = devStop(repo)
	assert result and result.get("decision") == "block"


def test_guard_paths_come_from_the_base(repo, flowMode):
	markBase(repo, flowMode)
	write(repo, ".claude/guard-paths.json", json.dumps({"tests": ["pkg/"]}))
	config, _ = qg_common.loadConfig(repo)
	assert "pkg" not in config["exclude"] and "pkg/" not in config["exclude"]
	git(repo, "add", ".claude/guard-paths.json")
	git(repo, "commit", "-q", "-m", "guard")
	markBase(repo, flowMode)
	config, _ = qg_common.loadConfig(repo)
	assert "pkg/" in config["exclude"]


def test_no_origin_means_no_config(tmp_path):
	root = str(tmp_path / "bare")
	os.makedirs(root)
	git(root, "init", "-q")
	write(root, ".claude/quality-gates.json", "{}")
	assert qg_common.loadConfig(root) == (None, None)


SETTINGS = {"bwrap_path": "/usr/bin/bwrap", "home_read": [], "home_write": [], "protect": [".git", ".hook-only", ".claude"],
	"env_pass": ["PATH"], "env_set": {"TMPDIR": "/tmp"}}


def test_sandbox_argv_order_and_env(tmp_path):
	home = str(tmp_path / "home")
	work = os.path.join(home, "repo")
	for d in (".git", ".claude", "src"):
		os.makedirs(os.path.join(work, d))
	out = str(tmp_path / "out")
	os.makedirs(out)
	args = qg_common.sandboxArgv(["pytest", "-q"], work, {"A": "1"}, [work, out], [], SETTINGS, home,
		{"PATH": "/usr/bin", "SECRET_TOKEN": "x"})
	text = " ".join(args)
	assert args[:3] == [SETTINGS["bwrap_path"], "--ro-bind", "/"]
	hidden = str(tmp_path / "mnt")
	os.makedirs(hidden)
	withHide = " ".join(qg_common.sandboxArgv(["true"], work, {}, [work], [], {**SETTINGS, "hide": [hidden, "/no/such"]}, home, {}))
	assert withHide.index(f"--tmpfs {hidden}") < withHide.index(f"--tmpfs {home}") < withHide.index(f"--bind {work}")
	assert "/no/such" not in withHide
	assert text.index(f"--tmpfs {home}") < text.index(f"--bind {work} {work}") < text.index(f"--ro-bind {work}/.git")
	assert f"--ro-bind {work}/.claude" in text and ".hook-only" not in text
	assert "--unshare-net" in args and "--clearenv" in args
	assert "SECRET_TOKEN" not in text
	assert ["--setenv", "HOME", home] == args[args.index("HOME") - 1:args.index("HOME") + 2]
	assert ["--setenv", "A", "1"] == args[args.index("A") - 1:args.index("A") + 2]
	assert args[-5:] == ["--chdir", work, "--", "pytest", "-q"]
	relative = qg_common.sandboxArgv(["true"], ".", {}, [], [], SETTINGS, home, {})
	assert relative[relative.index("--chdir") + 1] == os.path.abspath(".")


def noBwrap(monkeypatch, path="/no/such/bwrap"):
	"""This test process's gate_sandbox bwrap_path set to path (its config copy's local.json)."""
	with open(os.path.join(homeconfig.configDir(), "local.json"), "w", encoding="utf-8") as f:
		json.dump({"gate_sandbox": {"bwrap_path": path}}, f)
	monkeypatch.setattr(homeconfig, "_cache", {})


def test_missing_bwrap_raises_and_runs_nothing(tmp_path, monkeypatch):
	monkeypatch.setattr(qg_common, "_probed", [])
	noBwrap(monkeypatch)
	marker = tmp_path / "ran"
	with pytest.raises(qg_common.SandboxError):
		qg_common.runCommand([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], str(tmp_path))
	assert not marker.exists()


def loadHook(name, module):
	import importlib.util
	spec = importlib.util.spec_from_file_location(module, homepath.locate(name))
	loaded = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(loaded)
	return loaded


def brokenSandbox(*args, **kwargs):
	raise qg_common.SandboxError("bwrap unavailable: test")


def test_sandbox_error_never_blocks_the_developer_but_refuses_the_commit(repo, monkeypatch, capsys):
	crap = loadHook("crap-gate.py", "crap_gate")
	monkeypatch.setattr(crap.qg_common, "measureCoverage", brokenSandbox)
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	config, limits = qg_common.loadConfig(repo)
	work = os.path.join(repo, ".hook-only", "quality")
	os.makedirs(work)
	data = {"agent_id": "dev9", "agent_type": "developer"}
	assert crap.developerGate(data, repo, config, limits, work, "done") is None
	assert not os.path.exists(os.path.join(work, "dev-dev9.json"))
	crap.onCommit({}, repo, config, limits, work)
	out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
	assert out["permissionDecision"] == "deny" and "environment problem" in out["permissionDecisionReason"]


def test_mutation_reports_a_missing_sandbox_apart_from_a_red_baseline(repo, monkeypatch):
	mutation = loadHook("mutation.py", "mutation_hook")
	monkeypatch.setattr(mutation.qg_common, "measureCoverage", brokenSandbox)
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	config, limits = qg_common.loadConfig(repo)
	result = mutation.scan(repo, config, limits, mutation.mutationDir(repo))
	assert result["sandbox_unavailable"].startswith("bwrap unavailable") and result["baseline_failed"] == []


def test_a_bwrap_first_on_path_is_never_the_sandbox(tmp_path, monkeypatch):
	"""bwrap is never looked up on PATH: a fake one first there (as a repo's settings env could put it) never runs;
	the configured absolute path does."""
	fake = tmp_path / "bin" / "bwrap"
	fake.parent.mkdir()
	fake.write_text(f"#!/bin/sh\ntouch {tmp_path / 'fake-ran'}\nexit 0\n")
	fake.chmod(0o755)
	config_fixtures.standInHere(monkeypatch, homeconfig)
	monkeypatch.setattr(qg_common, "_probed", [])
	monkeypatch.setenv("PATH", f"{fake.parent}:/usr/bin:/bin")
	assert qg_common.runCommand([sys.executable, "-c", "print('ran')"], str(tmp_path))[0] == 0
	assert not (tmp_path / "fake-ran").exists()


@pytest.mark.parametrize("path", ["bwrap", "relative/bwrap", "/no/such/bwrap", "/etc/hostname"])
def test_a_bwrap_path_that_is_not_an_absolute_executable_refuses(tmp_path, monkeypatch, path):
	"""Fail closed: a configured bwrap_path that is relative (a PATH lookup), missing or not executable refuses the
	command; nothing runs."""
	noBwrap(monkeypatch, path)
	monkeypatch.setattr(qg_common, "_probed", [True])  # a probe that passed earlier never excuses the path check
	marker = tmp_path / "ran"
	with pytest.raises(qg_common.SandboxError, match="bwrap_path"):
		qg_common.runCommand([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], str(tmp_path))
	assert not marker.exists()


def test_no_environment_variable_runs_a_command_unsandboxed(tmp_path, monkeypatch):
	"""The tests' old switch (QG_UNSANDBOXED_FOR_TESTS, set here, inside pytest) is never read: with no usable bwrap
	the command is refused, never run without it."""
	monkeypatch.setenv("QG_UNSANDBOXED_FOR_TESTS", "1")
	monkeypatch.setattr(qg_common, "_probed", [])
	noBwrap(monkeypatch)
	marker = tmp_path / "ran"
	with pytest.raises(qg_common.SandboxError):
		qg_common.runCommand([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], str(tmp_path))
	assert not marker.exists()


def test_repo_protect_entries_are_added(tmp_path):
	work = str(tmp_path / "w")
	os.makedirs(os.path.join(work, "data"))
	args = " ".join(qg_common.sandboxArgv(["true"], work, {}, [work], [], SETTINGS, str(tmp_path / "h"), {}, protect=["data"]))
	assert args.index(f"--bind {work} {work}") < args.index(f"--ro-bind {work}/data {work}/data")


def test_home_sandbox_settings_cover_the_known_paths():
	settings = homeconfig.load("gate_sandbox")
	assert {"/home", "/mnt", "/run/WSL", "/run/user"} <= set(settings["hide"])
	assert {".hook-only", "plans"} <= set(settings["hide_inside"])
	assert {".git", ".claude"} <= set(settings["protect"])


def test_hide_inside_entries_are_hidden_in_every_root_after_its_binds(tmp_path):
	"""The hooks' state and plans/ inside a writable root and a readable one: an empty read-only tmpfs over a
	directory, /dev/null over a file, mounted after every bind (so nothing re-exposes them); a missing entry
	is skipped and a protect entry stays a read-only bind."""
	home = str(tmp_path / "h")
	work, read = str(tmp_path / "w"), str(tmp_path / "r")
	for root in (work, read):
		os.makedirs(os.path.join(root, ".hook-only", "hook-read-only"))
		os.makedirs(os.path.join(root, ".git"))
	with open(os.path.join(work, "plans"), "w") as f:  # a file of that name is hidden too
		f.write("x")
	settings = {**SETTINGS, "protect": [".git"], "hide_inside": [".hook-only", "plans"]}
	args = qg_common.sandboxArgv(["true"], work, {}, [work], [read], settings, home, {})
	text = " ".join(args)
	for root in (work, read):
		inner = f"{root}/.hook-only"
		assert f"--tmpfs {inner} --remount-ro {inner}" in text
		assert text.index(f"--tmpfs {inner}") > max(text.index(f"--bind {work} {work}"), text.index(f"--ro-bind {read} {read}"))
	assert f"--ro-bind /dev/null {work}/plans" in text and f"{read}/plans" not in text
	assert text.index(f"--ro-bind {work}/.git") < text.index(f"--tmpfs {work}/.hook-only")
	assert f"--ro-bind {work}/.hook-only" not in text


def test_the_gate_no_longer_only_protects_the_hooks_state():
	"""Old vs new for the home defaults: .hook-only and plans moved from protect (read-only, readable) to hide_inside
	(empty, read-only); every other protect entry stays."""
	settings = homeconfig.load("gate_sandbox", defaultsOnly=True)
	old = {".git", ".hook-only", ".claude", "plans"}
	assert set(settings["protect"]) == old - set(settings["hide_inside"])
	assert set(settings["hide_inside"]) == {".hook-only", "plans"}


def test_home_sandbox_paths_have_no_surrounding_spaces():
	# A " ~/godot" entry was never expanded by full() and so never mounted, with no error anywhere.
	settings = homeconfig.load("gate_sandbox")
	for key in ("hide", "hide_inside", "home_read", "home_write", "protect"):
		for path in settings.get(key) or []:
			assert path == path.strip(), f"{key}: {path!r}"


SYSTEM_PYTHON = "/usr/bin/python3"


def sandboxed(code, cwd, **kwargs):
	"""Runs code with the system Python (sys.executable may be a venv under the hidden home). A bwrap error
	of its own (`bwrap: ...`) fails the test: a refusal only counts when the code itself ran."""
	result = qg_common.runCommand([SYSTEM_PYTHON, "-c", code], cwd, **kwargs)
	assert not result[2].lstrip().startswith("bwrap:"), result[2]
	return result


@needsBwrap
def test_sandbox_blocks_network(tmp_path, monkeypatch):
	code, _, out = sandboxed("import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)", str(tmp_path))
	assert code != 0 and "OSError" in out, out


@needsBwrap
def test_sandbox_hides_home_and_protects_entries(tmp_path, monkeypatch):
	work = tmp_path / "w"
	(work / ".hook-only").mkdir(parents=True)
	gitconfig = os.path.expanduser("~/.gitconfig")
	if os.path.exists(gitconfig):
		code, _, out = sandboxed(f"open({gitconfig!r}).read()", str(work))
		assert code != 0 and "FileNotFoundError" in out, out
	code, _, out = sandboxed("open('.hook-only/x', 'w').write('1')", str(work))
	assert code != 0 and "Read-only file system" in out, out
	code, _, out = sandboxed("open('ok', 'w').write('1')", str(work))
	assert code == 0, out
	assert (work / "ok").exists()


@needsBwrap
def test_sandbox_hides_the_hooks_state_and_plans_inside_every_root(tmp_path, monkeypatch):
	"""Real bwrap: the home's hide_inside entries are empty and read-only inside the writable checkout and a readable
	one, hook-read-only/ included; the files of the checkout itself stay readable and writable."""
	work, other = tmp_path / "w", tmp_path / "o"
	for root in (work, other):
		(root / ".hook-only" / "hook-read-only").mkdir(parents=True)
		(root / ".hook-only" / "hook-read-only" / "secret").write_text("s")
		(root / "plans").mkdir()
		(root / "plans" / "topic.md").write_text("p")
	code, _, out = sandboxed(
		"import os\n"
		f"for root in ({str(work)!r}, {str(other)!r}):\n"
		"    print(root[-1], sorted(os.listdir(root + '/.hook-only')), sorted(os.listdir(root + '/plans')))\n"
		"try:\n"
		"    open('.hook-only/x', 'w')\n"
		"except OSError as e:\n"
		"    print('refused', e.errno)\n"
		"open('ok', 'w').write('1')\n",
		str(work), readable=[str(other)])
	assert code == 0, out
	assert "w [] []" in out and "o [] []" in out and "refused 30" in out, out
	assert (work / "ok").exists() and (work / ".hook-only" / "hook-read-only" / "secret").read_text() == "s"


@needsBwrap
def test_sandbox_runs_from_a_relative_cwd(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	code, _, out = sandboxed("import os; print(os.getcwd())", ".")
	assert code == 0 and str(tmp_path) in out, out


@needsBwrap
def test_sandbox_clears_environment(tmp_path, monkeypatch):
	monkeypatch.setenv("QG_TEST_SECRET", "s3cret")
	code, _, out = sandboxed("import os; print(os.environ.get('QG_TEST_SECRET'))", str(tmp_path))
	assert code == 0 and "s3cret" not in out, out


def test_agent_guard_reads_guard_paths_from_the_base(repo, flowMode):
	import importlib.util
	spec = importlib.util.spec_from_file_location("agent_guard", os.path.join(HOOKS, "guards", "agent_guard.py"))
	guard = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(guard)
	markBase(repo, flowMode)
	assert guard.loadGuardPaths(repo) is None
	write(repo, ".claude/guard-paths.json", json.dumps({"tests": ["tests/"]}))
	assert guard.loadGuardPaths(repo) is None
	git(repo, "add", ".claude/guard-paths.json")
	git(repo, "commit", "-q", "-m", "guard")
	markBase(repo, flowMode)
	write(repo, ".claude/guard-paths.json", json.dumps({"tests": []}))
	assert guard.loadGuardPaths(repo)["tests"] == ["tests"]


@pytest.mark.parametrize("status", ["CHECKPOINT", "BLOCKED"])
def test_unfinished_developer_report_is_not_checked(repo, status):
	write(repo, "pkg/busy.py", COMPLEX)
	assert devStop(repo, message=f"{status}\nbudget used up; done: half of busy()") is None
	assert not os.path.exists(os.path.join(repo, ".hook-only", "quality", "dev-dev1.json"))
	assert devStop(repo, message="DONE\nall green")["decision"] == "block"


# --- red/green at commit ------------------------------------------------------------

def commitStaged(repo, *paths):
	git(repo, "add", *paths)
	return commit(repo)


def test_first_tests_commit_that_passes_is_refused(repo):
	write(repo, "tests/test_new.py", "def test_new():\n    assert True\n")
	out = commitStaged(repo, "tests/test_new.py")
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "no implementation (exit 0)" in reason


def test_first_tests_commit_red_on_a_missing_module_is_allowed(repo):
	write(repo, "tests/test_new.py", "from pkg.missing import feature\n\n\ndef test_new():\n    assert feature() == 1\n")
	assert commitStaged(repo, "tests/test_new.py") is None


def test_first_tests_commit_red_for_the_wrong_reason_is_refused(repo):
	config = json.loads(git(repo, "show", "HEAD:.claude/quality-gates.json"))
	config["languages"]["python"]["red_invalid_patterns"] = ["SyntaxError"]
	write(repo, ".claude/quality-gates.json", json.dumps(config))
	git(repo, "commit", "-q", "-am", "red patterns")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
	write(repo, "tests/test_new.py", "def test_new(:\n    pass\n")
	out = commitStaged(repo, "tests/test_new.py")
	assert "wrong reason (SyntaxError)" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_later_tests_commit_is_not_checked(repo):
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	git(repo, "add", "pkg/small.py")
	git(repo, "commit", "-q", "-m", "impl")
	write(repo, "pkg/busy.py", COMPLEX)
	write(repo, "tests/test_small.py", "from pkg.small import small\n\n\ndef test_small():\n    assert small(1) == 1\n")
	assert commitStaged(repo, "tests/test_small.py") is None


def test_tests_fix_before_the_implementation_commit_is_not_checked(repo):
	# Seen: tests committed red, the developer's implementation still uncommitted, and qa
	# fixes a wrong test; the suite now passes because of that implementation, which is not qa's commit
	write(repo, "tests/test_small.py", "from pkg.small import small\n\n\ndef test_small():\n    assert small(1) == 2\n")
	assert commitStaged(repo, "tests/test_small.py") is None
	git(repo, "commit", "-q", "-m", "tests")
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	write(repo, "tests/test_small.py", "from pkg.small import small\n\n\ndef test_small():\n    assert small(1) == 1\n")
	assert commitStaged(repo, "tests/test_small.py") is None


def test_implementation_commit_with_a_red_suite_is_refused(repo):
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	write(repo, "tests/test_small.py", "from pkg.small import small\n\n\ndef test_small():\n    assert small(1) == 2\n")
	out = commitStaged(repo, "pkg/small.py", "tests/test_small.py")
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert "the test suite fails" in reason and "python (exit 1)" in reason


def test_red_suite_never_blocks_the_developer(repo):
	write(repo, "pkg/small.py", "def small(x):\n    return x\n")
	write(repo, "tests/test_small.py", "from pkg.small import small\n\n\ndef test_small():\n    assert small(1) == 2\n")
	assert devStop(repo) is None
	assert not os.path.exists(os.path.join(repo, ".hook-only", "quality", "pass"))


def test_only_the_first_line_of_a_report_counts_as_its_status(repo):
	write(repo, "pkg/busy.py", COMPLEX)
	out = devStop(repo, message="DONE\nBLOCKED earlier on a flaky fixture, fixed now")
	assert out and out["decision"] == "block"


# ---- gate waits: every gate marks its hold and clears it in a finally; a hook Claude Code
# cancels at its timeout runs no finally, so its mark is settled later (budget_state.releaseOrphanWait)

MUTATION_CMD = hook_entries.cmd("gates/mutation.py")
CRAP_CMD = hook_entries.cmd("gates/crap-gate.py")


def settingsTimeouts():
	"""{(event, matcher, if, command): timeout} of the generated settings hooks (hook-entries.py)."""
	hooks = hook_entries.settingsHooks(homeconfig.get(*hook_entries.CEILINGS))
	return {(event, g.get("matcher"), h.get("if"), h["command"]): h.get("timeout") for event, groups in hooks.items()
		for g in groups for h in g["hooks"]}


def test_gate_deadlines_fit_under_the_settings_ceilings():
	timeouts = settingsTimeouts()
	assert timeouts[("PreToolUse", "Bash", "Bash(mutate-request *)", MUTATION_CMD)] >= qg_common.hookTimeout("mutate_request_seconds")
	assert timeouts[("PreToolUse", "Bash", "Bash(git *)", CRAP_CMD)] >= qg_common.hookTimeout("commit_gate_seconds")
	gates = max(qg_common.hookTimeout("review_handback_seconds"), qg_common.hookTimeout("developer_gate_seconds"))
	entry = hook_entries.cmd("hook-entry.py")
	assert timeouts[("PreToolUse", "SubagentHandback", None, entry + " handback")] >= gates
	assert timeouts[("SubagentStop", None, None, entry + " stop")] >= gates
	defaults = homeconfig.load("quality")
	assert defaults["mutation_wait_seconds"] < qg_common.hookTimeout("review_handback_seconds")


def gateBudget(root, agent):
	budgetDir = os.path.join(root, ".hook-only", "budget")
	os.makedirs(budgetDir, exist_ok=True)
	path = os.path.join(budgetDir, f"{agent}.json")
	with open(path, "w") as f:
		json.dump({"minutes": 10, "started": 1000.0}, f)
	return path


def test_developer_handback_marks_the_coverage_wait_and_clears_it_on_a_crash(tmp_path, monkeypatch):
	root = str(tmp_path)
	stateFile = gateBudget(root, "d1")
	gate = loadHook("crap-gate.py", "crap_gate_waits")
	seen = []

	def slow(*args):
		seen.append(qg_common.loadJson(stateFile).get("hook_wait"))
		time.sleep(1.1)

	monkeypatch.setattr(gate, "developerGate", slow)
	gate.onDeveloperHandback({"agent_id": "d1", "tool_input": {"report": "DONE"}}, root, {}, {}, root)
	assert seen[0]["what"] == "coverage run" and seen[0]["max_seconds"] == qg_common.hookTimeout("developer_gate_seconds")
	state = qg_common.loadJson(stateFile)
	assert "hook_wait" not in state and state["started"] >= 1001.1

	def crash(*args):
		assert "hook_wait" in qg_common.loadJson(stateFile)
		raise RuntimeError("boom")

	monkeypatch.setattr(gate, "developerGate", crash)
	with pytest.raises(RuntimeError):
		gate.onDeveloperHandback({"agent_id": "d1", "tool_input": {"report": "DONE"}}, root, {}, {}, root)
	assert "hook_wait" not in qg_common.loadJson(stateFile)


def test_reviewer_handback_clears_the_scan_wait_on_a_crash(tmp_path, monkeypatch):
	root = str(tmp_path)
	stateFile = gateBudget(root, "r1")
	mutation = loadHook("mutation.py", "mutation_waits")

	def crash(*args):
		mark = qg_common.loadJson(stateFile)["hook_wait"]
		assert mark["max_seconds"] == qg_common.hookTimeout("review_handback_seconds")
		time.sleep(1.1)
		raise RuntimeError("boom")

	monkeypatch.setattr(mutation, "survivorReason", crash)
	with pytest.raises(RuntimeError):
		mutation.onHandback({"agent_id": "r1"}, root, {}, {}, root)
	state = qg_common.loadJson(stateFile)
	assert "hook_wait" not in state and state["started"] >= 1001.1


def test_an_orphaned_wait_is_settled_at_stop_or_once_older_than_its_hook_timeout(tmp_path):
	root = str(tmp_path)
	stateFile = gateBudget(root, "a1")

	def mark(since, limit):
		state = qg_common.loadJson(stateFile)
		state["hook_wait"] = {"since": since, "what": "mutation scan", "max_seconds": limit}
		with open(stateFile, "w") as f:
			json.dump(state, f)

	mark(2000.0, 600)
	# younger than its hook's timeout: the hook may still hold a parallel call
	assert budget_state.releaseOrphanWait(root, "a1", now=2300.0) == 0.0
	assert "hook_wait" in qg_common.loadJson(stateFile)
	# older: Claude Code cancelled the hook; the hold lasted at most its timeout
	assert budget_state.releaseOrphanWait(root, "a1", now=2700.0) == 600.0
	state = qg_common.loadJson(stateFile)
	assert "hook_wait" not in state and state["started"] == 1600.0
	mark(3000.0, 600)
	# at SubagentStop any mark left is a cancelled hook's, however young
	assert budget_state.releaseOrphanWait(root, "a1", ended=True, now=3300.0) == 300.0
	with open(os.path.join(root, ".hook-only", "budget", "events.jsonl")) as f:
		events = [json.loads(line) for line in f]
	# dated at the mark, inside the round cost-history.py matches the wait to
	assert [(e["t"], e["seconds"]) for e in events] == [
		(time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(2000.0)), 600.0),
		(time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(3000.0)), 300.0)]
	assert budget_state.releaseOrphanWait(root, "a1", ended=True, now=4000.0) == 0.0  # nothing left
	assert budget_state.releaseOrphanWait(root, "missing", ended=True) == 0.0


# --- coverage targets from the diff (stage 3c): {changed_dirs} --------------------------------------
# The python profile's coverage measures only the changed files' directories (qg_common module doc).
# The acceptance tests run the real pytest-cov on a two-package repo and compare crap-gate's findings with
# derived targets against measuring the whole repo (--cov=., which, unlike bare --cov, also reports files
# no test imports), for the same diff.

def test_changed_dirs_are_the_changed_files_directories_relative_to_cwd():
	root = "/r"
	assert qg_common.changedDirs(root, root, ["pa/a.py", "pa/b.py", "pb/sub/c.py", "top.py"]) == [".", "pa", "pb/sub"]
	assert qg_common.changedDirs(root, "/r/proj", ["proj/pkg/a.py"]) == ["pkg"]
	assert qg_common.changedDirs(root, root, []) == ["."] and qg_common.changedDirs(root, root, None) == ["."]


def test_expand_coverage_repeats_the_changed_dirs_item_and_fills_the_rest():
	argv = ["uv", "--cov={changed_dirs}", "--cov-report=lcov:{report}", "{{changed_dirs}}"]
	out = qg_common.expandCoverage(argv, {"repo": "/r", "root": "/r", "report": "/o/c.lcov"}, ["pa", "pb"])
	assert out == ["uv", "--cov=pa", "--cov=pb", "--cov-report=lcov:/o/c.lcov", "{changed_dirs}"]
	assert qg_common.derivesTargets({"coverage": argv}) and not qg_common.derivesTargets({"coverage": ["--cov=pa"]})
	assert not qg_common.derivesTargets({"coverage": ["{{changed_dirs}}"]}) and not qg_common.derivesTargets({})


def test_parse_lcov_reads_a_relative_sf_against_the_commands_cwd():
	text = "SF:pkg/m.py\nDA:1,1\nend_of_record\nSF:/r/other/n.py\nDA:2,0\nend_of_record\n"
	assert qg_common.parseLcov(text, "/r", "/r/proj") == {"proj/pkg/m.py": {1: 1}, "other/n.py": {2: 0}}


def test_a_repo_coverage_command_without_changed_dirs_runs_exactly_as_written(tmp_path, monkeypatch):
	config_fixtures.standInHere(monkeypatch, homeconfig)  # the fake tool lives under tmp, which bwrap hides
	seen = tmp_path / "argv.json"
	tool = fakeTool(tmp_path / "bin" / "cov", f"""\
		import json
		json.dump(sys.argv[1:], open({str(seen)!r}, "w"))
		for arg in sys.argv[1:]:
		    if arg.startswith("--cov-report=lcov:"):
		        open(arg.split(":", 1)[1], "w").close()
		""")
	own = [tool, "--cov=pa", "--cov=pb", "--cov-report=lcov:{report}"]
	root = str(tmp_path / "r")
	os.makedirs(root)
	cover, code, _, _ = qg_common.measureCoverage(root, "python", {"coverage": own}, str(tmp_path / "w"), 60,
		files={"pc/new.py": {1}})
	argv = json.loads(seen.read_text())
	assert cover == {} and code == 0
	assert argv[:2] == ["--cov=pa", "--cov=pb"] and argv[2].startswith("--cov-report=lcov:") and len(argv) == 3


PA_CORE = """\
	def double(x):
	    return 2 * x
	"""
PB_CORE = """\
	def triple(x):
	    return 3 * x
	"""


@pytest.fixture
def twoPackages(tmp_path):
	"""A repo with packages pa and pb, each with a tested module, quality-gates.json with derived targets."""
	root = str(tmp_path / "two")
	os.makedirs(root)
	git(root, "init", "-q", "-b", "feature/x/topic")
	git(root, "config", "user.email", "t@t")
	git(root, "config", "user.name", "t")
	write(root, ".gitignore", ".hook-only/\nplans/\n__pycache__/\n")
	for package, core in (("pa", PA_CORE), ("pb", PB_CORE)):
		write(root, f"{package}/__init__.py", "")
		write(root, f"{package}/core.py", core)
	write(root, "pb/old.py", "def old():\n    return 1\n")
	write(root, "tests/test_pa.py", "from pa.core import double\n\n\ndef test_double():\n    assert double(2) == 4\n")
	write(root, "tests/test_pb.py", "from pb.core import triple\n\n\ndef test_triple():\n    assert triple(2) == 6\n")
	runner = [UV, "run", "--offline", "--no-project", "--with", "pytest", "--with", "pytest-cov==7.1.0", "python", "-m",
		"pytest", "-q", "-p", "no:cacheprovider"]
	config = {"languages": {"python": {"extensions": [".py"], "coverage_format": "lcov", "test": runner + ["-x"],
		"coverage": runner + ["--cov={changed_dirs}", "--cov-branch", "--cov-report=lcov:{report}"]}},
		"exclude": ["tests/"], "scratch_exclude": [".hook-only/", ".git/", "__pycache__"]}
	write(root, qg_common.CONFIG_FILE, json.dumps(config))
	git(root, "add", ".")
	git(root, "commit", "-q", "-m", "base")
	git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
	return root


def findingsWith(repo, target, monkeypatch):
	"""crap-gate's findings for the working tree, the coverage command's {changed_dirs} item replaced by
	target (None keeps the derived one)."""
	config_fixtures.usableSandboxHere(monkeypatch, homeconfig)
	crap = loadHook("crap-gate.py", "crap_gate_cov")
	config, limits = qg_common.loadConfig(repo)
	python = config["languages"]["python"]
	if target is not None:
		python["coverage"] = [target if "{changed_dirs}" in item else item for item in python["coverage"]]
	findings, failing = crap.evaluate(repo, config, limits, ccnOnly=False)
	assert failing == []
	return sorted(findings)


CHANGED_PA = """\
	def double(x):
	    return 2 * x


	def halve(x):
	    if x is None:
	        return 0
	    return x / 2
	"""


def test_derived_targets_find_what_whole_repo_measurement_finds(twoPackages, monkeypatch):
	write(twoPackages, "pa/core.py", CHANGED_PA)
	derived = findingsWith(twoPackages, None, monkeypatch)
	assert derived == findingsWith(twoPackages, "--cov=.", monkeypatch)
	assert derived == ["pa/core.py:6-8: changed, never executed by a test; delete, or mark with a reason"]


def test_a_new_file_in_a_new_package_no_test_imports_is_reported_unexecuted(twoPackages, monkeypatch):
	write(twoPackages, "pc/__init__.py", "")
	write(twoPackages, "pc/new.py", "def fresh(x):\n    return x + 1\n")
	derived = findingsWith(twoPackages, None, monkeypatch)
	assert derived == findingsWith(twoPackages, "--cov=.", monkeypatch)
	assert "pc/new.py:1-2: changed, never executed by a test; delete, or mark with a reason" in derived
	# Bare --cov (the profile before stage 3c) has no source to search, so the file was missing: no finding.
	assert not any(f.startswith("pc/new.py") for f in findingsWith(twoPackages, "--cov", monkeypatch))


def test_a_deleted_file_is_no_target_and_changes_nothing(twoPackages, monkeypatch):
	git(twoPackages, "rm", "-q", "pb/old.py")
	assert findingsWith(twoPackages, None, monkeypatch) == []
	write(twoPackages, "pa/core.py", CHANGED_PA)
	derived = findingsWith(twoPackages, None, monkeypatch)
	assert derived == findingsWith(twoPackages, "--cov=.", monkeypatch) and len(derived) == 1


def test_a_test_only_change_runs_no_coverage(twoPackages, monkeypatch):
	write(twoPackages, "tests/test_pa.py", "from pa.core import double\n\n\ndef test_double():\n    assert double(3) == 6\n")
	crap = loadHook("crap-gate.py", "crap_gate_cov")
	monkeypatch.setattr(crap.qg_common, "measureCoverage", brokenSandbox)  # raising here would fail the test
	config, limits = qg_common.loadConfig(twoPackages)
	assert crap.evaluate(twoPackages, config, limits, ccnOnly=False) == ([], [])


def test_a_changed_file_derived_coverage_leaves_out_is_a_finding_never_a_pass(twoPackages, monkeypatch):
	write(twoPackages, ".coveragerc", "[run]\nomit = pa/core.py\n")
	write(twoPackages, "pa/core.py", CHANGED_PA)
	derived = findingsWith(twoPackages, None, monkeypatch)
	assert len(derived) == 1 and derived[0].startswith("pa/core.py: changed, but the coverage run reported nothing")
	# A repo's own command keeps its reading: a file its --cov list leaves out isn't checked for coverage.
	write(twoPackages, ".coveragerc", "")
	assert findingsWith(twoPackages, "--cov=pb", monkeypatch) == []


def test_mutation_baseline_measures_the_changed_files(twoPackages, monkeypatch):
	mutation = loadHook("mutation.py", "mutation_cov")
	seen = []

	def spy(*args, **kwargs):
		seen.append(kwargs.get("files"))
		raise qg_common.SandboxError("stop here")

	monkeypatch.setattr(mutation.qg_common, "measureCoverage", spy)
	write(twoPackages, "pa/core.py", CHANGED_PA)
	config, limits = qg_common.loadConfig(twoPackages)
	mutation.scan(twoPackages, config, limits, mutation.mutationDir(twoPackages))
	assert [sorted(files) for files in seen] == [["pa/core.py"]]


def test_a_command_past_the_gate_deadline_raises_instead_of_reading_as_a_timeout(tmp_path, monkeypatch):
	config_fixtures.standInHere(monkeypatch, homeconfig)
	monkeypatch.setattr(qg_common, "_deadline", (time.monotonic() + 1, "commit_gate_seconds"))
	start = time.monotonic()
	with pytest.raises(qg_common.DeadlineReached, match="commit_gate_seconds"):
		qg_common.runCommand(["sleep", "10"], str(tmp_path), timeout=60)
	assert time.monotonic() - start < 5
	with pytest.raises(qg_common.DeadlineReached):  # nothing left: no command starts
		qg_common.runCommand(["true"], str(tmp_path), timeout=60)


def test_a_command_s_own_timeout_still_returns_none_inside_a_deadline(tmp_path, monkeypatch):
	config_fixtures.standInHere(monkeypatch, homeconfig)
	monkeypatch.setattr(qg_common, "_deadline", (time.monotonic() + 60, "commit_gate_seconds"))
	code, _seconds, _tail = qg_common.runCommand(["sleep", "10"], str(tmp_path), timeout=1)
	assert code is None
	monkeypatch.setattr(qg_common, "_deadline", None)
	assert qg_common.remaining() is None


def test_start_deadline_uses_the_gate_key(monkeypatch):
	monkeypatch.setattr(qg_common, "_deadline", None)
	qg_common.startDeadline("mutate_request_seconds")
	assert 0 < qg_common.remaining() <= qg_common.hookTimeout("mutate_request_seconds")
	monkeypatch.setattr(qg_common, "_deadline", None)


def mountedAs(args, path):
	"""How bwrap's mounts in args leave path: "hidden" under a tmpfs, "read" or "write" under a bind, None when only
	the read-only / covers it. Later mounts cover earlier ones, as in bwrap."""
	state, at = None, 0
	takes = {"--tmpfs": 1, "--dev": 1, "--proc": 1, "--remount-ro": 1, "--chdir": 1, "--ro-bind": 2, "--bind": 2,
		"--setenv": 2}
	while at < len(args) and args[at] != "--":
		option = args[at]
		target = args[at + takes.get(option, 0)] if takes.get(option) else None
		if target and option in ("--tmpfs", "--ro-bind", "--bind") and (path == target or path.startswith(target + "/")):
			state = {"--tmpfs": "hidden", "--ro-bind": "read", "--bind": "write"}[option]
		at += 1 + takes.get(option, 0)
	return state


def test_the_gate_home_puts_the_tests_uv_and_its_cache_back_under_a_fake_home(tmp_path, monkeypatch):
	"""public-check.py's clean install runs the suite with a fake HOME, where gate_sandbox's ~ paths name the fake home:
	the gate home and usableSandboxHere (config_fixtures.exposeTools) put the tests' uv and the cache it reports back by
	absolute path, so bwrap's tmpfs over /home and $HOME never hides them ('bwrap: execvp <uv>: No such file or
	directory')."""
	fake = tmp_path / "fake-home"
	fake.mkdir()
	monkeypatch.setenv("HOME", str(fake))
	config_fixtures.usableSandboxHere(monkeypatch, homeconfig)  # exposeTools, as for the gate home
	settings = homeconfig.load("gate_sandbox")
	checkout = tmp_path / "repo"
	checkout.mkdir()
	args = qg_common.sandboxArgv([UV, "run"], str(checkout), {}, [str(checkout)], [], settings, str(fake), {})
	_read, write = config_fixtures.toolPaths()
	assert mountedAs(args, UV) in (None, "read"), args
	assert all(mountedAs(args, path) in (None, "write") for path in write if os.path.exists(path)), args
	assert mountedAs(args, str(fake / "x")) == "hidden"


def test_the_gate_sandbox_passes_the_uv_cache_location_on(tmp_path):
	"""home.json gate_sandbox env_pass carries UV_CACHE_DIR: a run whose uv cache isn't under its HOME (verify.md
	step 6's fake HOME) still finds its packages offline. Only the value is passed; no path is bound for it."""
	import homeconfig

	settings = homeconfig.load("gate_sandbox")
	assert "UV_CACHE_DIR" in settings["env_pass"]
	args = qg_common.sandboxArgv(["true"], str(tmp_path), {}, [str(tmp_path)], [], settings, str(tmp_path / "h"),
		{"UV_CACHE_DIR": "/cache/uv", "SECRET": "x"})
	assert ["--setenv", "UV_CACHE_DIR", "/cache/uv"] == args[args.index("UV_CACHE_DIR") - 1:args.index("UV_CACHE_DIR") + 2]
	assert "SECRET" not in args and "/cache/uv" not in [a for a in args if a.startswith("/cache")][1:]

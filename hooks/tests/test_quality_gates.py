"""Tests for qg_common, qg_complexity, qg_mutate, crap-gate.py and mutation.py.

Run: uv run --offline --no-project --with pytest --with pytest-cov==7.1.0 python -m pytest ~/.claude/hooks/tests
The integration tests build a throwaway git repo (origin/main = its first commit) whose
quality-gates.json runs pytest-cov through `uv run --offline --with`, and feed hook input JSON to the
hooks as subprocesses with CLAUDE_PROJECT_DIR pointing at that repo (test-hooks.md).
Inside Claude Code's sandbox bwrap can't nest: the hook subprocesses then get
QG_UNSANDBOXED_FOR_TESTS=1 and the tests that need a real bwrap are skipped. Run the suite once
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
REAL_HOOKS = os.path.expanduser("~/.claude/hooks")
sys.path[:0] = [HOOKS, REAL_HOOKS]

import qg_common  # noqa: E402
import qg_complexity  # noqa: E402
import qg_mutate  # noqa: E402

LANGS = qg_common.loadJson(os.path.join(HOOKS, "qg_langs.json"))
GO = shutil.which("go") or "/usr/local/go/bin/go"
UV = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
BWRAP_OK = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-net", "true"], capture_output=True).returncode == 0 \
	if shutil.which("bwrap") else False
HOOK_ENV = {} if BWRAP_OK else {qg_common.UNSANDBOXED_ENV: "1"}
needsBwrap = pytest.mark.skipif(not BWRAP_OK, reason="bwrap can't create namespaces here (inside Claude Code's sandbox)")


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


def hostEnv():
	"""This process's environment at call time (PYTEST_CURRENT_TEST included), without the session's
	FLOW_INTEGRATION: local mode would point the hooks at a local main these repos don't have."""
	return {k: v for k, v in os.environ.items() if k != "FLOW_INTEGRATION"}


def runHook(name, repo, payload, env=None, args=()):
	full = {"cwd": repo, "session_id": "s", "transcript_path": "/dev/null", **payload}
	result = subprocess.run([sys.executable, os.path.join(HOOKS, name), *args], input=json.dumps(full), capture_output=True, text=True,
		env={**hostEnv(), "CLAUDE_PROJECT_DIR": repo, "PYTHONPATH": REAL_HOOKS, **HOOK_ENV, **(env or {})}, timeout=900)
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


def test_no_config_does_nothing(repo):
	git(repo, "rm", "-q", ".claude/quality-gates.json")
	git(repo, "commit", "-q", "-m", "drop config")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
	write(repo, "pkg/busy.py", COMPLEX)
	assert devStop(repo) is None and commit(repo) is None


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


# ---- mutate-request like the scan (2026-11-02): the reviewer's call was held 10+ min, unmarked and on its budget


def manualRequest(repo, tmp_path, monkeypatch, specs, agent="r1"):
	"""Loads mutation.py in process for one mutate-request: a budget state for the agent (started 1000.0),
	the host's sandbox switch, and every runMutant call recorded as (argv or None for the test command, the
	budget's hook_wait mark at that moment); each recorded run takes at least 0.6 s, so two of them make a
	wait excludeWait counts (>= 1 s) however warm the test runner is. Returns (module, call, calls); call()
	runs the request (its output goes to stdout)."""
	for key, value in HOOK_ENV.items():
		monkeypatch.setenv(key, value)
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


# ---- hand-back gates (2026-10-31): a block at Stop after SubagentHandback came too late


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
	qg_common.excludeWait(root, "a1", 120.0)
	qg_common.excludeWait(root, "a1", 0.4)
	assert qg_common.loadJson(os.path.join(budgetDir, "a1.json"))["started"] == 1120.0
	with open(os.path.join(budgetDir, "events.jsonl")) as f:
		events = [json.loads(line) for line in f]
	assert [(e["event"], e["agent_id"], e["seconds"]) for e in events] == [("HookWait", "a1", 120.0)]
	qg_common.excludeWait(root, "missing", 5.0)



def test_a_wait_is_marked_while_it_lasts_and_cleared_when_it_ends(tmp_path):
	root = str(tmp_path)
	budgetDir = os.path.join(root, ".hook-only", "budget")
	os.makedirs(budgetDir)
	stateFile = os.path.join(budgetDir, "a1.json")
	with open(stateFile, "w") as f:
		json.dump({"minutes": 10, "started": 1000.0}, f)
	qg_common.markWait(root, "a1", "mutation scan", 3300)
	mark = qg_common.loadJson(stateFile)["hook_wait"]
	assert (mark["what"], mark["max_seconds"]) == ("mutation scan", 3300)
	assert mark["since"] > 1000.0
	qg_common.excludeWait(root, "a1", 0.4)
	state = qg_common.loadJson(stateFile)
	assert "hook_wait" not in state and state["started"] == 1000.0
	assert not os.path.exists(os.path.join(budgetDir, "events.jsonl"))
	qg_common.markWait(root, "a1", "mutation scan", 3300)
	qg_common.excludeWait(root, "a1", 60.0)
	state = qg_common.loadJson(stateFile)
	assert "hook_wait" not in state and state["started"] == 1060.0
	qg_common.markWait(root, "missing", "mutation scan", 3300)
	assert not os.path.exists(os.path.join(budgetDir, "missing.json"))

def workerStop(message, tasks, active=False):
	return runHook("worker-stop.py", "/tmp", {"hook_event_name": "Stop", "last_assistant_message": message,
		"background_tasks": tasks, "stop_hook_active": active}, env={"CLAUDE_WORKER_MINUTES": "30"})


def test_worker_stop_lets_status_end_while_a_subagent_runs():
	assert workerStop("STATUS: done\nall good", [{"agent_type": "code-reviewer"}]) is None


def test_worker_stop_still_holds_status_under_a_background_command():
	out = workerStop("STATUS: done", [{"type": "shell", "description": "dev server"}])
	assert out["decision"] == "block" and "dev server" in out["reason"]


def test_worker_stop_pause_while_subagent_runs_and_format_when_idle():
	assert workerStop("waiting for the reviewer", [{"agent_type": "code-reviewer"}]) is None
	assert "STATUS: done" in workerStop("finished", [])["reason"]
	assert workerStop("finished", [], active=True) is None


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


def test_guard_paths_come_from_the_base(repo):
	write(repo, ".claude/guard-paths.json", json.dumps({"tests": ["pkg/"]}))
	config, _ = qg_common.loadConfig(repo)
	assert "pkg" not in config["exclude"] and "pkg/" not in config["exclude"]
	git(repo, "add", ".claude/guard-paths.json")
	git(repo, "commit", "-q", "-m", "guard")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
	config, _ = qg_common.loadConfig(repo)
	assert "pkg/" in config["exclude"]


def test_no_origin_means_no_config(tmp_path):
	root = str(tmp_path / "bare")
	os.makedirs(root)
	git(root, "init", "-q")
	write(root, ".claude/quality-gates.json", "{}")
	assert qg_common.loadConfig(root) == (None, None)


SETTINGS = {"home_read": [], "home_write": [], "protect": [".git", ".hook-only", ".claude"],
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
	assert args[:3] == ["bwrap", "--ro-bind", "/"]
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


def test_missing_bwrap_raises_and_runs_nothing(tmp_path, monkeypatch):
	monkeypatch.delenv(qg_common.UNSANDBOXED_ENV, raising=False)
	monkeypatch.setattr(qg_common, "_probed", [])
	monkeypatch.setenv("PATH", str(tmp_path))
	marker = tmp_path / "ran"
	with pytest.raises(qg_common.SandboxError):
		qg_common.runCommand([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], str(tmp_path))
	assert not marker.exists()


def loadHook(name, module):
	import importlib.util
	spec = importlib.util.spec_from_file_location(module, os.path.join(HOOKS, name))
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


def test_unsandboxed_switch_only_inside_pytest(monkeypatch, capsys):
	monkeypatch.setenv(qg_common.UNSANDBOXED_ENV, "1")
	assert qg_common.unsandboxedForTests()
	monkeypatch.delenv("PYTEST_CURRENT_TEST")
	assert not qg_common.unsandboxedForTests()
	assert "ignored outside pytest" in capsys.readouterr().err


def test_repo_protect_entries_are_added(tmp_path):
	work = str(tmp_path / "w")
	os.makedirs(os.path.join(work, "data"))
	args = " ".join(qg_common.sandboxArgv(["true"], work, {}, [work], [], SETTINGS, str(tmp_path / "h"), {}, protect=["data"]))
	assert args.index(f"--bind {work} {work}") < args.index(f"--ro-bind {work}/data {work}/data")


def test_home_sandbox_settings_cover_the_known_paths():
	settings = qg_common.loadJson(qg_common.SANDBOX_FILE)
	assert {"/home", "/mnt", "/run/WSL", "/run/user"} <= set(settings["hide"])
	assert {".git", ".hook-only", ".claude", "plans"} <= set(settings["protect"])


SYSTEM_PYTHON = "/usr/bin/python3"


def sandboxed(code, cwd, **kwargs):
	"""Runs code with the system Python (sys.executable may be a venv under the hidden home). A bwrap error
	of its own (`bwrap: ...`) fails the test: a refusal only counts when the code itself ran."""
	result = qg_common.runCommand([SYSTEM_PYTHON, "-c", code], cwd, **kwargs)
	assert not result[2].lstrip().startswith("bwrap:"), result[2]
	return result


@needsBwrap
def test_sandbox_blocks_network(tmp_path, monkeypatch):
	monkeypatch.delenv(qg_common.UNSANDBOXED_ENV, raising=False)
	code, _, out = sandboxed("import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)", str(tmp_path))
	assert code != 0 and "OSError" in out, out


@needsBwrap
def test_sandbox_hides_home_and_protects_entries(tmp_path, monkeypatch):
	monkeypatch.delenv(qg_common.UNSANDBOXED_ENV, raising=False)
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
def test_sandbox_runs_from_a_relative_cwd(tmp_path, monkeypatch):
	monkeypatch.delenv(qg_common.UNSANDBOXED_ENV, raising=False)
	monkeypatch.chdir(tmp_path)
	code, _, out = sandboxed("import os; print(os.getcwd())", ".")
	assert code == 0 and str(tmp_path) in out, out


@needsBwrap
def test_sandbox_clears_environment(tmp_path, monkeypatch):
	monkeypatch.delenv(qg_common.UNSANDBOXED_ENV, raising=False)
	monkeypatch.setenv("QG_TEST_SECRET", "s3cret")
	code, _, out = sandboxed("import os; print(os.environ.get('QG_TEST_SECRET'))", str(tmp_path))
	assert code == 0 and "s3cret" not in out, out


def test_agent_guard_reads_guard_paths_from_the_base(repo):
	import importlib.util
	spec = importlib.util.spec_from_file_location("agent_guard", os.path.join(HOOKS, "agent-guard.py"))
	guard = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(guard)
	assert guard.loadGuardPaths(repo) is None
	write(repo, ".claude/guard-paths.json", json.dumps({"tests": ["tests/"]}))
	assert guard.loadGuardPaths(repo) is None
	git(repo, "add", ".claude/guard-paths.json")
	git(repo, "commit", "-q", "-m", "guard")
	git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
	write(repo, ".claude/guard-paths.json", json.dumps({"tests": []}))
	assert guard.loadGuardPaths(repo)["tests"] == ["tests"]


@pytest.mark.parametrize("status", ["CHECKPOINT", "BLOCKED"])
def test_unfinished_developer_report_is_not_checked(repo, status):
	write(repo, "pkg/busy.py", COMPLEX)
	assert devStop(repo, message=f"{status}\nbudget used up; done: half of busy()") is None
	assert not os.path.exists(os.path.join(repo, ".hook-only", "quality", "dev-dev1.json"))
	assert devStop(repo, message="DONE\nall green")["decision"] == "block"


# --- red/green at commit (2026-11-01) ------------------------------------------------------------

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
	# ml-tools 2026-11-01: tests committed red, the developer's implementation still uncommitted, and qa
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

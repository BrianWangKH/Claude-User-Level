"""qg_common helpers the gates lean on: isSource (is a path source code of a gated language and not excluded:
crap-gate's and auto-merge's "did source change"), lastLines (the tail of a failed run in a message),
agent_guard reading the repo's guard-paths.json at qg_common's path, as committed at the diff base, and matching
its entries by qg_common.pathMatches (old vs new: guard_paths_baseline).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_qg_common_helpers.py -q -p no:cacheprovider'
"""

import itertools
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import agent_guard  # noqa: E402
import guard_paths_baseline  # noqa: E402
import qg_common  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
	"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
CONFIG = {
	"languages": {"python": {"extensions": [".py"]}, "go": {"extensions": [".go"]}},
	"exclude": ["tests/", "vendor/*.py", "gen.py", "docs"],
}


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
		env={**os.environ, **GIT_ENV}).stdout.strip()


# -- isSource --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path, source", [
	("src/a.py", True),
	("cmd/main.go", True),
	("README.md", False),  # no gated language
	("src/a.pyc", False),
	("tests/test_a.py", False),  # "dir/": a prefix
	("vendor/lib.py", False),  # a glob
	("vendor/deep/lib.py", True),  # the glob's * stays within one directory
	("gen.py", False),  # exact
	("src/gen.py", True),  # an exact pattern is not a suffix
	("docs/conf.py", False),  # a bare name is a directory prefix too
	("docsite/conf.py", True),
])
def test_is_source_needs_a_gated_language_and_no_exclude(path, source):
	"""Unit: a path is source when its extension is a configured language's and no exclude pattern matches."""
	assert qg_common.isSource(path, CONFIG) is source


def test_is_source_is_false_without_languages():
	"""Unit: no languages configured, nothing is source."""
	assert qg_common.isSource("src/a.py", {"exclude": []}) is False


# -- lastLines -------------------------------------------------------------------------------------------


def test_last_lines_joins_the_last_lines_of_the_tail():
	"""Unit: the last count lines of the stripped tail, joined with " | "; fewer lines give them all."""
	assert qg_common.lastLines("\nfirst\nsecond\nthird\n\n", 2) == "second | third"
	assert qg_common.lastLines("only\n", 3) == "only"
	assert qg_common.lastLines("", 2) == ""


def test_last_lines_is_cut_at_300_characters():
	"""Unit: the joined text is cut to its first 300 characters."""
	tail = "x" * 250 + "\n" + "y" * 250
	out = qg_common.lastLines(tail, 2)
	assert len(out) == 300 and out == ("x" * 250 + " | " + "y" * 250)[:300]


# -- agent_guard's guard-paths.json ------------------------------------------------------------------------


def test_agent_guard_reads_guard_paths_at_qg_commons_path():
	"""Unit: one path for the repo's guard-paths file, qg_common's (the gates read the same one)."""
	assert agent_guard.GUARD_PATHS_FILE == qg_common.GUARD_PATHS_FILE


@pytest.fixture
def repo(tmp_path, monkeypatch):
	"""A main checkout in local mode (its record decides; no test override)."""
	monkeypatch.delenv("FLOW_INTEGRATION", raising=False)
	r = tmp_path / "main"
	r.mkdir()
	git(r, "init", "-q", "-b", "main")
	(r / ".hook-only").mkdir()
	(r / ".hook-only" / "flow-mode").write_text("local\n")
	(r / ".git" / "info" / "exclude").write_text(".hook-only/\n")
	return r


def test_agent_guard_loads_the_committed_guard_paths_not_the_working_tree(repo):
	"""Unit: loadGuardPaths reads the repo's guard-paths file as committed at the diff base (trailing slashes
	dropped); an uncommitted edit doesn't count, and no committed file is None."""
	(repo / "README.md").write_text("x\n")
	git(repo, "add", "README.md")
	git(repo, "commit", "-q", "-m", "base")
	guard = repo / qg_common.GUARD_PATHS_FILE
	guard.parent.mkdir()
	guard.write_text(json.dumps({"tests": ["tests/"]}))
	assert agent_guard.loadGuardPaths(str(repo)) is None  # not committed
	git(repo, "add", qg_common.GUARD_PATHS_FILE)
	git(repo, "commit", "-q", "-m", "guard paths")
	guard.write_text(json.dumps({"tests": ["everything/"]}))
	assert agent_guard.loadGuardPaths(str(repo)) == {"tests": ["tests"], "qaConfig": [], "shared": []}


# -- guard-paths entries: one matching rule (qg_common.pathMatches) for agent_guard and the gates ------------

KIT_TEST_FILES = {"go": ["a_test.go", "rehearsal-la1_test.go", "pkg/a_test.go", "pkg/deep/er/x_test.go"],
	"python": ["test_a.py", "pkg/test_a.py", "pkg/deep/er/test_x.py"]}
KIT_OTHER_FILES = {"go": ["main.go", "a_test.go.orig", "test.go", "pkg/testing.go", "pkg/a_test.go/x.go",
	"a_test.gox", "README.md", "go.sum"],
	"python": ["main.py", "a_test.py", "pkg/mytest_a.py", "test_a.pyc", "pkg/test_a.txt", "tests.py", "pyproject.toml"]}


def kitGuardPaths(lang):
	"""The new-repo kit's guard-paths file of lang (its dot-claude/ is the repo's .claude/), from the tree under test,
	as agent_guard keeps it."""
	path = os.path.join(os.path.dirname(HOOKS), "templates", "new-repo", "lang", lang, "dot-claude",
		os.path.basename(qg_common.GUARD_PATHS_FILE))
	with open(path, encoding="utf-8") as f:
		given = json.load(f)
	return {key: [agent_guard.entry(p) for p in given.get(key) or []] for key in ("tests", "qaConfig", "shared")}


@pytest.mark.parametrize("lang", sorted(KIT_TEST_FILES))
def test_the_kit_patterns_cover_test_files_at_the_root_and_nested_and_nothing_else(lang):
	"""qa's root test file (`rehearsal-la1_test.go` here) was "outside tests/qaConfig" because
	agent_guard compared the kit's globs as literal paths. Every test file, at the root or nested, is a test path;
	no other file is."""
	tests = kitGuardPaths(lang)["tests"]
	assert [p for p in KIT_TEST_FILES[lang] if not agent_guard.matches(p, tests)] == []
	assert [p for p in KIT_OTHER_FILES[lang] if agent_guard.matches(p, tests)] == []


def snapshotOf(files):
	return {"refs": {"head": "h", "branch": "b", "refs": {}, "stash": []}, "files": files}


@pytest.mark.parametrize("lang", sorted(KIT_TEST_FILES))
def test_a_root_test_file_is_qa_s_and_protected_from_the_developer(lang):
	"""evaluate with the kit's file: qa writing a root test file raises no ALERT; the developer doing it does."""
	path = KIT_TEST_FILES[lang][1] if lang == "go" else KIT_TEST_FILES[lang][0]
	before, after = snapshotOf({}), snapshotOf({path: "x"})
	guardPaths = kitGuardPaths(lang)
	assert agent_guard.evaluate("qa", before, after, guardPaths)[0] == []
	alerts = agent_guard.evaluate("developer", before, after, guardPaths)[0]
	assert alerts == [f"protected test files changed: {path}"]
	source = "main.go" if lang == "go" else "main.py"
	alerts = agent_guard.evaluate("qa", before, snapshotOf({source: "x"}), guardPaths)[0]
	assert alerts == [f"files outside tests/qaConfig changed: {source}"]


@pytest.mark.parametrize("path, pattern, expected", [
	("a_test.go", "*_test.go", True),
	("pkg/a_test.go", "*_test.go", False),  # * stays within one segment
	("a_test.go", "**/*_test.go", True),  # **/ includes the root
	("pkg/deep/a_test.go", "**/*_test.go", True),
	("pkg/testdata/in.txt", "**/testdata/", True),  # a directory glob covers what is under it
	("testdata/in.txt", "**/testdata/", True),
	("testdata", "**/testdata/", False),
	("pkg/testdatax/in.txt", "**/testdata/", False),
	("pkg/x_fixtures/a/b", "pkg/*_fixtures/", True),
	("pkg/sub/x_fixtures/a", "pkg/*_fixtures/", False),
	("test_a.py", "test_?.py", True),
	("test_ab.py", "test_?.py", False),
	("tests/a.py", "tests/", True),
	("tests", "tests/", False),
])
def test_path_matches_globs_keep_to_segments_and_directory_globs_cover_their_contents(path, pattern, expected):
	assert qg_common.pathMatches(path, pattern) is expected


@pytest.mark.parametrize("raw, kept", [("tests/", "tests"), ("tests", "tests"), ("pkg/tests//", "pkg/tests"),
	("**/testdata/", "**/testdata/"), ("*_test.go", "*_test.go")])
def test_agent_guard_strips_a_plain_entry_s_slash_and_keeps_a_glob_as_written(raw, kept):
	assert agent_guard.entry(raw) == kept


def generatedPaths():
	segments = ["tests", "pkg", "a_test.go", "test_a.py", "testdata", "x", "tests.py"]
	return ["/".join(parts) for n in range(1, 4) for parts in itertools.product(segments, repeat=n)]


GENERATED_ENTRIES = ["", "tests", "tests/", "pkg/tests", "pkg/tests/", "a_test.go", "x/", "*_test.go", "**/*_test.go",
	"test_*.py", "**/test_*.py", "**/testdata/", "test_?.py", "**/tests/**", "pkg/*", "pkg/*/"]


def test_old_vs_new_every_change_is_a_named_one():
	"""Old (guard_paths_baseline) vs new over every generated path x entry, for agent_guard.matches (entries as
	loadGuardPaths keeps them) and qg_common.pathMatches: a plain entry answers as before; agent_guard changes only
	for a glob entry (was compared as a literal path), pathMatches only for a glob with a trailing "/" (was a literal
	prefix)."""
	counts = {}
	for path in generatedPaths():
		for raw in GENERATED_ENTRIES:
			old = guard_paths_baseline.oldAgentGuardMatches(path, [raw])
			new = agent_guard.matches(path, [agent_guard.entry(raw)])
			reason = "same" if old == new else ("agent_guard: glob entry was literal" if qg_common.isGlob(raw)
				else "unexplained")
			counts[reason] = counts.get(reason, 0) + 1
			assert reason != "unexplained", ("agent_guard", path, raw, old, new)
			old, new = guard_paths_baseline.oldPathMatches(path, raw), qg_common.pathMatches(path, raw)
			reason = "same" if old == new else ("pathMatches: directory glob was a literal prefix"
				if qg_common.isGlob(raw) and raw.endswith("/") else "unexplained")
			counts[reason] = counts.get(reason, 0) + 1
			assert reason != "unexplained", ("pathMatches", path, raw, old, new)
	print("old vs new:", counts)
	assert counts["agent_guard: glob entry was literal"] > 0 and counts["pathMatches: directory glob was a literal prefix"] > 0

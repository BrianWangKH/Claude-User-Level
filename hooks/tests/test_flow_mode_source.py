"""S4/S5: one reader of the flow mode (homeconfig.flowMode) and one of the integration branch
(homeconfig.integrationBranch; CLI `homeconfig.py flow-mode` / `integration-branch`, base-ref.sh): their
precedence and errors, the old-vs-new matrix over every way a caller used to decide (flow_mode_baseline.py),
and the quality gates failing closed without a diff base in local mode.

"Outside pytest" (the production fixture) removes PYTEST_CURRENT_TEST and sets FLOW_INTEGRATION as a session might
have it; since worker self-repair 3b no variable is a source, inside pytest or not, so the answer is always the
records' (the matrix's reason "variable" counts the cells where the variable used to decide or fail).

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_flow_mode_source.py
"""

import collections
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(os.path.dirname(HOOKS), "config")
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
sys.path[:0] = [os.path.join(HOOKS, "lib"), os.path.dirname(os.path.abspath(__file__))]
import homepath  # noqa: E402  (the tree under test: config/ and the hooks/ role directories)
import flow_mode_baseline as baseline  # noqa: E402
import homeconfig  # noqa: E402
import qg_common  # noqa: E402

GIT_ENV = {
	"GIT_AUTHOR_NAME": "t",
	"GIT_AUTHOR_EMAIL": "t@t",
	"GIT_COMMITTER_NAME": "t",
	"GIT_COMMITTER_EMAIL": "t@t",
	"GIT_CONFIG_GLOBAL": "/dev/null",
	"GIT_CONFIG_NOSYSTEM": "1",
}
ERROR = baseline.ERROR


def git(repo, *args):
	return subprocess.run(
		["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}
	).stdout.strip()


@pytest.fixture
def repo(tmp_path):
	"""(main checkout on main with one commit, the topic's linked worktree on feature/x/topic)."""
	main = tmp_path / "proj" / "main"
	main.mkdir(parents=True)
	git(main, "init", "-q", "-b", "main")
	(main / ".gitignore").write_text(".hook-only/\n")
	git(main, "add", ".gitignore")
	git(main, "commit", "-q", "-m", "base")
	wt = tmp_path / "proj" / "worktrees" / "topic"
	git(main, "worktree", "add", "-q", "-b", "feature/x/topic", str(wt))
	return main, wt


@pytest.fixture
def production(monkeypatch):
	"""Outside pytest as homeconfig sees it (module doc): call it with FLOW_INTEGRATION's value (None: unset) in the
	test's body, since pytest sets PYTEST_CURRENT_TEST again when the call phase starts."""

	def outside(value=None):
		monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
		if value is None:
			monkeypatch.delenv("FLOW_INTEGRATION", raising=False)
		else:
			monkeypatch.setenv("FLOW_INTEGRATION", value)

	return outside


def setRecord(checkout, text):
	"""The checkout's flow-mode record holding text exactly (None: no file)."""
	record = checkout / ".hook-only" / "flow-mode"
	if text is None:
		record.unlink(missing_ok=True)
		return
	record.parent.mkdir(exist_ok=True)
	record.write_text(text)


def modeOr(root, **kwargs):
	"""flowMode's answer, or (ERROR, its message)."""
	try:
		return homeconfig.flowMode(str(root), **kwargs)
	except homeconfig.ConfigError as error:
		return ERROR, str(error)


# --- precedence ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
	"main, topic, expectMain, expectTopic",
	[
		(None, None, "origin", "origin"),  # 4. the default
		("local\n", None, "local", "local"),  # 3. the main checkout's record, inherited by a topic without one
		("origin\n", None, "origin", "origin"),
		("local\n", "origin\n", "local", "origin"),  # 2. the topic's record over the main checkout's
		("origin\n", "local\n", "origin", "local"),
		(None, "local\n", "origin", "local"),
	],
)
def test_the_topic_record_then_the_main_record_then_the_default(repo, production, main, topic, expectMain, expectTopic):
	production()
	mainDir, wt = repo
	setRecord(mainDir, main)
	setRecord(wt, topic)
	assert homeconfig.flowMode(str(mainDir)) == expectMain
	assert homeconfig.flowMode(str(wt)) == expectTopic
	(wt / "sub").mkdir()
	assert homeconfig.flowMode(str(wt / "sub")) == expectTopic  # any path inside the checkout


@pytest.mark.parametrize("value", ["origin", "local", "bogus", ""])
def test_no_environment_variable_changes_the_flow_mode(repo, monkeypatch, value):
	"""The tests' old override (FLOW_INTEGRATION while PYTEST_CURRENT_TEST is set, as it is here) is never read: a
	repo's settings env can't choose the mode; the records decide, as without it."""
	mainDir, wt = repo
	setRecord(mainDir, "origin\n")
	setRecord(wt, "local\n")
	monkeypatch.setenv("FLOW_INTEGRATION", value)
	assert homeconfig.flowMode(str(mainDir)) == "origin" and homeconfig.flowMode(str(wt)) == "local"
	assert cli("flow-mode", str(wt)).stdout == "local\n"


def test_outside_git_it_is_the_default(tmp_path, production):
	production()
	assert homeconfig.flowMode(str(tmp_path)) == "origin"


def test_a_path_that_is_no_directory_is_an_error(tmp_path, production):
	production()
	with pytest.raises(homeconfig.ConfigError, match="no such directory"):
		homeconfig.flowMode(str(tmp_path / "gone"))


@pytest.mark.parametrize("text", ["garbage\n", "", "\n", "Local\n", "local origin\n"])
@pytest.mark.parametrize("where", ["main", "topic"])
def test_a_record_holding_anything_else_is_an_error_never_the_default(repo, production, text, where):
	production()
	mainDir, wt = repo
	setRecord(mainDir if where == "main" else wt, text)
	with pytest.raises(homeconfig.ConfigError, match="flow mode .* is not one of origin, local"):
		homeconfig.flowMode(str(wt))


def test_a_topic_with_its_own_record_never_reads_a_broken_main_record(repo, production):
	production()
	mainDir, wt = repo
	setRecord(mainDir, "garbage\n")
	setRecord(wt, "local\n")
	assert homeconfig.flowMode(str(wt)) == "local"


@pytest.mark.parametrize(
	"main, topic, env, expect",
	[
		("local\n", None, "local", "local"),  # a session started before S4: the variable mirrors the record
		("local\n", "origin\n", "local", "origin"),  # a topic launched in PR mode, run from that session
		(None, None, "origin", "origin"),
		(None, None, "local", ERROR),  # a repo settings env without the record
		("origin\n", None, "local", ERROR),  # switched off after the session started
		("local\n", "local\n", "origin", ERROR),
	],
)
def test_outside_pytest_the_variable_is_no_source_either(repo, production, main, topic, env, expect):
	"""The cells that once were errors (the variable disagreeing with the main checkout) now read the records."""
	production()
	mainDir, wt = repo
	setRecord(mainDir, main)
	setRecord(wt, topic)
	production(env)
	assert modeOr(wt) == ((topic or main or "origin\n").strip())


@pytest.mark.parametrize("recorded, expect", [(None, "local"), ("local", "local"), ("origin", ERROR)])
def test_the_run_records_mode_must_be_the_topics(repo, production, recorded, expect):
	production()
	_, wt = repo
	setRecord(wt, "local\n")
	result = modeOr(wt, recorded=recorded)
	assert (result if expect != ERROR else result[0]) == expect


# --- integration branch ----------------------------------------------------------------------------


@pytest.mark.parametrize(
	"branches, expect", [(["main"], "main"), (["master"], "master"), (["main", "master"], "main"), ([], None)]
)
def test_local_mode_is_the_local_main_else_master(tmp_path, branches, expect):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q", "-b", "trunk")
	git(repo, "commit", "-q", "--allow-empty", "-m", "x")
	for branch in branches:
		git(repo, "branch", branch)
	assert homeconfig.integrationBranch(str(repo), "local") == expect


# (origin branches that exist, origin/HEAD's target or None) -> the branch or ERROR. The old callers agreed on
# every case that isn't an error here (qg_common: origin/master, else origin/main; base-ref.sh and
# pr-conflict.sh: origin/HEAD, else the same; run-result.py and worker-status.py: origin/HEAD, origin/main,
# origin/master) and disagreed on every error case.
ORIGIN_CASES = [
	([], None, None),
	(["main"], None, "origin/main"),
	(["master"], None, "origin/master"),
	(["main"], "main", "origin/main"),
	(["master"], "master", "origin/master"),
	(["main", "master"], "master", "origin/master"),
	(["main", "master"], None, ERROR),  # the gates took origin/master, the display readers origin/main
	(["main", "master"], "main", ERROR),  # the scripts took origin/main, the gates origin/master
	(["master"], "main", ERROR),  # origin/HEAD names a branch that doesn't exist
	(["main", "develop"], "develop", ERROR),
]


@pytest.mark.parametrize("branches, head, expect", ORIGIN_CASES)
def test_origin_mode_is_origin_master_else_main_and_an_ambiguity_is_an_error(tmp_path, branches, head, expect):
	repo = tmp_path / "r"
	repo.mkdir()
	git(repo, "init", "-q", "-b", "main")
	git(repo, "commit", "-q", "--allow-empty", "-m", "x")
	for branch in branches:
		git(repo, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
	if head:
		git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{head}")
	if expect == ERROR:
		with pytest.raises(homeconfig.ConfigError, match="origin/HEAD"):
			homeconfig.integrationBranch(str(repo), "origin")
	else:
		assert homeconfig.integrationBranch(str(repo), "origin") == expect


def test_the_integration_branch_follows_the_checkouts_mode(repo, production):
	production()
	mainDir, wt = repo
	git(mainDir, "update-ref", "refs/remotes/origin/main", "HEAD")
	setRecord(mainDir, "origin\n")
	setRecord(wt, "local\n")
	assert homeconfig.integrationBranch(str(mainDir)) == "origin/main"
	assert homeconfig.integrationBranch(str(wt)) == "main"


def test_an_unknown_mode_is_an_error(repo):
	with pytest.raises(homeconfig.ConfigError, match="is not one of"):
		homeconfig.integrationBranch(str(repo[0]), "pr")


# --- the CLI and base-ref.sh -----------------------------------------------------------------------


def cli(*args, cwd=None):
	return subprocess.run(
		[sys.executable, os.path.join(CONFIG, "homeconfig.py"), *args],
		capture_output=True,
		text=True,
		cwd=cwd,
		timeout=60,
	)


def baseRef(*args, cwd=None):
	return subprocess.run(
		["bash", os.path.join(SKILLS, "feature-flow", "base-ref.sh"), *args],
		capture_output=True,
		text=True,
		cwd=cwd,
		timeout=60,
	)


def test_the_cli_and_base_ref_print_the_mode_and_the_branch(repo, production):
	production()
	mainDir, wt = repo
	setRecord(wt, "local\n")
	assert cli("flow-mode", str(wt)).stdout == "local\n"
	assert cli("flow-mode", cwd=mainDir).stdout == "origin\n"
	assert cli("integration-branch", str(wt)).stdout == "main\n"
	none = cli("integration-branch", str(mainDir))  # origin mode, nothing pushed
	assert none.returncode == 1 and none.stdout == "" and "no origin/master or origin/main" in none.stderr
	shell = baseRef(str(mainDir))
	assert shell.returncode == 1 and "no origin/master or origin/main" in shell.stderr
	git(mainDir, "update-ref", "refs/remotes/origin/main", "HEAD")
	assert baseRef(cwd=mainDir).stdout == "origin/main\n" and baseRef(cwd=wt).stdout == "main\n"
	setRecord(wt, "bogus\n")
	bad = cli("flow-mode", str(wt))
	assert bad.returncode == 1 and bad.stdout == "" and "'bogus'" in bad.stderr
	assert baseRef(cwd=wt).returncode == 1


# --- old vs new ------------------------------------------------------------------------------------

MAIN_TEXTS = [None, "local\n", "origin\n", "garbage\n", ""]
TOPIC_TEXTS = [None, "local\n", "origin\n", "garbage\n"]
RUN_RECORDS = [None, "local", "origin"]
ENVS = [None, "local", "origin", "bogus"]
VALID = ("origin", "local")
SAME = "same"
REASONS = {
	"garbage": "error: a record holding no mode (before: read as origin)",
	"variable": "the records instead of FLOW_INTEGRATION, which is no source (before: the session's value, or an "
	"error when it held no mode)",
	"run": "error: the run record's integration_mode is not the topic's (before: the record was not read)",
	"record": "the record instead of the session's FLOW_INTEGRATION (before: unset, or the main mirror read in a "
	"worktree with its own record)",
}


def isValid(text):
	return text is None or text.strip() in VALID


def newAnswer(mainDir, wt, scope, run):
	mode = modeOr(mainDir if scope == "main" else wt, **({"recorded": run} if scope == "wrapup" else {}))
	return mode[0] if isinstance(mode, tuple) else mode


def expectedError(main, topic, run, env, scope):
	"""Why the new reader must fail in this cell (the first rule that applies), or None. env is never a reason."""
	topicRead = scope != "main" and topic is not None
	if topicRead and not isValid(topic):
		return "garbage"
	if not topicRead and not isValid(main):
		return "garbage"
	mainMode = main.strip() if main else "origin"
	mode = topic.strip() if topicRead else mainMode
	if scope == "wrapup" and run is not None and run != mode:
		return "run"
	return None


def classify(main, topic, run, env, scope, old, source, new):
	"""SAME, or the key of REASONS why the answer changed; AssertionError for a change no rule explains."""
	why = expectedError(main, topic, run, env, scope)
	if why is not None:
		assert new == ERROR, (main, topic, run, env, scope, new)
		return why
	assert new != ERROR, (main, topic, run, env, scope)
	if new == old:
		return SAME
	assert source in ("env", "default"), f"a record's meaning changed: {(main, topic, run, env, scope, old, new)}"
	# The variable as the wt-pane mod set it mirrored the main record: a change there is the record's, as before.
	return "variable" if source == "env" and env is not None and env != baseline.mirror(main) else "record"


def matrixCases(repo, production):
	mainDir, wt = repo
	cases = []
	for main in MAIN_TEXTS:
		setRecord(mainDir, main)
		for topic in TOPIC_TEXTS:
			setRecord(wt, topic)
			for env in ENVS:
				production(env)
				answers = {
					scope: {run: newAnswer(mainDir, wt, scope, run) for run in RUN_RECORDS}
					for scope in ("main", "topic", "wrapup")
				}
				for run in RUN_RECORDS:
					for caller, (scope, rule) in baseline.CALLERS.items():
						old, source = rule(main, topic, env)
						cases.append((main, topic, run, env, caller, scope, old, source, answers[scope][run]))
	production(None)
	return cases


def isValidSetup(main, topic, run, env):
	"""Records valid, the variable as the wt-pane mod set it, the run record absent or the topic's mode."""
	if not (isValid(main) and isValid(topic)) or env != baseline.mirror(main):
		return False
	return run in (None, (topic or main or "origin").strip())


def test_old_vs_new_every_change_is_a_named_one_and_valid_setups_keep_their_answer(repo, production):
	production()
	counts = collections.Counter()
	for main, topic, run, env, caller, scope, old, source, new in matrixCases(repo, production):
		reason = classify(main, topic, run, env, scope, old, source, new)
		counts[(caller.split(" (")[0], reason)] += 1
		if isValidSetup(main, topic, run, env) and reason != SAME:
			# the one change there: an interactive worktree session read the main mirror, now the topic's record
			assert caller.startswith("interactive") and reason == "record" and topic is not None, (
				main,
				topic,
				run,
				env,
				caller,
				old,
				new,
			)
	print("\nold vs new (caller, reason): count")
	for key, count in sorted(counts.items()):
		print(f"  {key}: {count}")
	total = len(MAIN_TEXTS) * len(TOPIC_TEXTS) * len(RUN_RECORDS) * len(ENVS) * len(baseline.CALLERS)
	assert sum(counts.values()) == total


def test_every_scope_reads_one_answer(repo, production):
	"""One source: within a cell every caller of a scope gets the same new answer (they all ask flowMode)."""
	production()
	byCell = collections.defaultdict(set)
	for main, topic, run, env, caller, scope, old, source, new in matrixCases(repo, production):
		byCell[(main, topic, run, env, scope)].add(new)
	assert all(len(answers) == 1 for answers in byCell.values())


# --- the quality gates fail closed without a diff base in local mode -------------------------------


def gateRepo(tmp_path, branch="main"):
	"""A repo on branch with a tracked Python file (a language the home profiles gate), no worktree."""
	repo = tmp_path / "g"
	repo.mkdir()
	git(repo, "init", "-q", "-b", branch)
	(repo / ".gitignore").write_text(".hook-only/\n")
	(repo / "pkg").mkdir()
	(repo / "pkg" / "a.py").write_text("def f():\n\treturn 1\n")
	git(repo, "add", ".")
	git(repo, "commit", "-q", "-m", "base")
	return repo


def test_local_mode_without_a_base_is_an_error_the_gates_see(tmp_path, production):
	production()
	repo = gateRepo(tmp_path, branch="trunk")  # no local main or master
	setRecord(repo, "local\n")
	with pytest.raises(homeconfig.ConfigError, match="local mode, but no diff base"):
		qg_common.loadConfig(str(repo))
	with pytest.raises(homeconfig.ConfigError, match="local mode, but no diff base"):
		qg_common.committedJson(str(repo), qg_common.GUARD_PATHS_FILE)


def test_a_stale_variable_in_a_local_repo_never_skips_the_gates(tmp_path, production):
	"""The old reader took a stale FLOW_INTEGRATION=origin, found no origin branch and skipped the gates; the
	variable is no source now: the record's local mode and its local main are used."""
	production()
	repo = gateRepo(tmp_path)
	setRecord(repo, "local\n")
	production("origin")
	config, _ = qg_common.loadConfig(str(repo))
	assert config is not None


def test_pr_mode_without_a_base_still_has_nothing_to_gate(tmp_path, production):
	production()
	repo = gateRepo(tmp_path)  # origin mode (no record), nothing pushed yet
	assert qg_common.loadConfig(str(repo)) == (None, None)
	assert qg_common.committedJson(str(repo), qg_common.GUARD_PATHS_FILE) is None


def hook(name, repo, payload, env):
	"""A gate hook run as Claude Code runs it, outside pytest (no PYTEST_CURRENT_TEST)."""
	full = {"cwd": str(repo), "session_id": "s", "transcript_path": "/dev/null", **payload}
	base = {k: v for k, v in os.environ.items() if k not in ("PYTEST_CURRENT_TEST", "FLOW_INTEGRATION")}
	result = subprocess.run(
		[sys.executable, homepath.locate(name)],
		input=json.dumps(full),
		capture_output=True,
		text=True,
		timeout=120,
		env={**base, "CLAUDE_PROJECT_DIR": str(repo), **env},
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout) if result.stdout.strip() else None


@pytest.mark.parametrize(
	"branch, env, words",
	[
		("trunk", {}, "local mode, but no diff base"),
		("trunk", {"FLOW_INTEGRATION": "origin"}, "local mode, but no diff base"),  # the variable changes nothing
	],
)
def test_crap_gate_refuses_the_commit_and_mutation_holds_the_reviewer(tmp_path, branch, env, words):
	repo = gateRepo(tmp_path, branch=branch)
	setRecord(repo, "local\n")
	out = hook(
		"crap-gate.py",
		repo,
		{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}},
		env,
	)
	decision = out["hookSpecificOutput"]
	assert decision["permissionDecision"] == "deny" and "config doesn't load" in decision["permissionDecisionReason"]
	assert words in decision["permissionDecisionReason"]
	out = hook(
		"mutation.py",
		repo,
		{
			"hook_event_name": "SubagentStop",
			"agent_type": "code-reviewer",
			"agent_id": "r1",
			"last_assistant_message": "done",
			"stop_hook_active": False,
		},
		env,
	)
	assert out["decision"] == "block" and "mutation: not run" in out["reason"] and words in out["reason"]

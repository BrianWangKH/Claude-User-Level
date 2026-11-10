"""Flow mode for the hook tests: origin (origin/main, fetch, PR) or local (the local main, no fetch, automatic
merge).

The hooks and skill scripts read the mode through homeconfig.flowMode: the checkout's records only (no
environment variable is a source, not even while pytest runs: no home program reads FLOW_INTEGRATION, so
a home draft's suite runs once, without it). The autouse fixture gives each test the mode
it states as homeconfig.FLOW_DEFAULT in this process (the mode of a main checkout without a record); a program a
test starts as a subprocess reads the records, so a test or fixture that needs local mode there writes them
(local_fixtures.markLocal):
- @pytest.mark.flow_mode("origin" | "local") (on the test, a parameter or the module): that mode only;
- a `flowMode` argument: the test runs once per mode (ids origin, local) and asserts each mode's outcome;
  it overrides a module-level mark.

Home settings and state: no variable moves them (homeconfig.configDir is config/ beside the loader,
statefile.homeStateDir is <home>/state of the tree). Every test gets, in this process, its own copy of the tree's
config/ without the person's files (local.json, local.test.json, mode) as homeconfig.configDir and its own
scratch directory as statefile.homeStateDir, both replaced on the modules themselves; a test that needs other
settings writes them into that copy or replaces configDir again (config_fixtures.useConfig). A home program
a test starts as a subprocess reads the config/ and state/ of the tree it runs from: a test that needs other
settings or state for one runs it from a copy of the tree (config_fixtures.copyHome).

Git identity: the person's git configuration is no input of a test (hooks/RULES.md T2). Every process of the
session, and every program a test starts with the inherited environment, commits and merges as GIT_IDENTITY (git's
GIT_AUTHOR_* and GIT_COMMITTER_* variables, which win over any user.name and user.email), so a test passes the
same with the person's HOME, a fake HOME without a git configuration (public-check.py's clean install) or none.

Recursion guard (hooks/RULES.md T7): the first session to load this conftest claims SUITE_MARK in the
environment with its own pid; every process it starts inherits the claim. A pytest session that loads
this conftest while another one holds it (the suite run again from inside a test or a tool a test
calls) stops before collecting anything, so a nested run can't fan out into a recursive one. Nested
pytest runs on fixture repos in tmp_path don't load this conftest and are not affected.
A pytest-xdist worker (-n) loads this conftest too: it joins the claim only when it is a worker (xdist gives a
worker's config a `workerinput` before its configure hooks run; no other process has one, and unlike an
environment variable it isn't inherited by a process a test starts) and its parent process is the session
holding the claim (xdist starts its workers directly from the controller). A run started from inside a test,
serial or under xdist, is no worker, and its parent is a worker, a test process or a shell, never the
controller.

Private layer (TEMPORARY, with the public export's exclude list of hooks/tools/public-check.py): a core test that
needs files of the home's private layer (the flow that changes the home itself, which the public export leaves
out) names them in a private_layer(*paths) mark. It is skipped only when none of them exists in the tree under
test (the public export); when any of them exists it runs, so a full home never skips it and a home that lost
only some of them fails instead of skipping. The proper fix moves those files and their tests into a private
layer the core never names; then the mark goes away.
"""
import os
import sys
import tempfile

import pytest

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
ROOT = os.path.dirname(os.path.dirname(LIB))  # the home tree under test
# A copy of this conftest outside a home tree (test_suite_guard.py's fixture suites) has no settings to isolate.
IN_TREE = os.path.isfile(os.path.join(LIB, "homepath.py"))
# Until a test's own homeDirs runs (module-level reads while the test modules are collected, and the whole session
# in between), the same isolation for the session: SESSION holds a copy of config/ without the person's files and
# a scratch home state directory, removed when the session ends.
SESSION = None
if IN_TREE:
	sys.path.insert(0, LIB)
	import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
	import config_fixtures  # noqa: E402
	import homeconfig  # noqa: E402
	import statefile  # noqa: E402

	SESSION = tempfile.TemporaryDirectory(prefix="home-suite-")
	config_fixtures.SESSION_DIR = SESSION.name
	_sessionConfig = str(config_fixtures.copyConfig(homeconfig.HERE, os.path.join(SESSION.name, "config")))
	_sessionState = os.path.join(SESSION.name, "state")
	homeconfig.configDir = lambda: _sessionConfig
	statefile.homeStateDir = lambda: _sessionState
	# Modules that start a sibling home program start the clean copy's (config_fixtures.cleanHome): a program
	# started from the tree under test would read the person's files there.
	import wt_controls  # noqa: E402
	import wt_queue  # noqa: E402

	_clean = config_fixtures.cleanHome()
	wt_queue.NEW_WORKTREE = _clean / "skills" / "new-worktree" / "new-worktree.sh"
	wt_queue.WORKER_CONTROL = _clean / "hooks" / "worker" / "worker-control.py"
	wt_controls.WRAPPER = _clean / "hooks" / "worker" / "run-worker.sh"

# The fixed commit identity of every test (module doc); the address is in a domain reserved for invalid names.
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "Home Test", "GIT_AUTHOR_EMAIL": "home-test@example.invalid",
	"GIT_COMMITTER_NAME": "Home Test", "GIT_COMMITTER_EMAIL": "home-test@example.invalid"}
os.environ.update(GIT_IDENTITY)

MODES = ("origin", "local")
SUITE_MARK = "CLAUDE_HOME_SUITE"
NESTED_EXIT = 3
PRIVATE_LAYER = "private_layer"


def privateLayerSkip(root, paths):
	"""Why a test marked private_layer(*paths) is skipped in the home tree at root, or None when it runs (module
	doc). ValueError for a mark without paths or with a path that isn't a plain home-relative one."""
	if not paths:
		raise ValueError(f"{PRIVATE_LAYER} needs the paths the test reads")
	for path in paths:
		if not isinstance(path, str) or not path.strip("/") or os.path.isabs(path) \
				or {".", ".."} & set(path.rstrip("/").split("/")):
			raise ValueError(f"{PRIVATE_LAYER}: a plain home-relative path, got {path!r}")
	if any(os.path.lexists(os.path.join(root, path)) for path in paths):
		return None
	return f"the home's private layer is not in this tree (public export): {', '.join(paths)}"


def claimSuite(environ, owner, parent, isWorker):
	"""Claim the suite for owner (a pid); True when owner holds it, or is an xdist worker whose parent holds it;
	False when another session holds it."""
	holder = environ.setdefault(SUITE_MARK, owner)
	return holder == owner or (isWorker and holder == parent)


def pytest_configure(config):
	isWorker = hasattr(config, "workerinput")  # pytest-xdist's documented way to tell a worker
	if not claimSuite(os.environ, str(os.getpid()), str(os.getppid()), isWorker):
		pytest.exit(f"conftest: the hook suite is already running in this process tree ({SUITE_MARK} is set); "
			"a nested run of it is refused (hooks/RULES.md T7)", returncode=NESTED_EXIT)
	config.addinivalue_line("markers", 'flow_mode(mode): run the test in flow mode "origin" or "local" (the '
		'default mode of a main checkout without a record); see conftest.py')
	config.addinivalue_line("markers", f"{PRIVATE_LAYER}(*paths): the test needs these home-relative private-layer "
		"files (a trailing / for a directory); skipped only when none of them exists in the tree under test; see "
		"conftest.py")


def pytest_unconfigure(config):
	if SESSION is not None:
		SESSION.cleanup()


def pytest_generate_tests(metafunc):
	if "flowMode" in metafunc.fixturenames:
		metafunc.parametrize("flowMode", MODES)


def statedMode(request):
	"""The test's flow mode: its flowMode parameter, else its closest flow_mode mark, else origin (unset)."""
	callspec = getattr(request.node, "callspec", None)
	if callspec is not None and "flowMode" in callspec.params:
		return callspec.params["flowMode"]
	mark = request.node.get_closest_marker("flow_mode")
	if mark is None:
		return "origin"
	if len(mark.args) != 1 or mark.args[0] not in MODES:
		pytest.fail(f"flow_mode takes one of {MODES}, got {mark.args}", pytrace=False)
	return mark.args[0]


@pytest.fixture(autouse=True)
def privateLayer(request):
	"""Skip a test marked private_layer when none of its files is in this tree (module doc)."""
	marks = list(request.node.iter_markers(PRIVATE_LAYER))
	if not marks:
		return
	try:
		reason = privateLayerSkip(ROOT, [path for mark in marks for path in mark.args])
	except ValueError as error:
		pytest.fail(str(error), pytrace=False)
	if reason:
		pytest.skip(reason)


@pytest.fixture(autouse=True)
def flowModeDefault(request, monkeypatch):
	"""The stated mode as this process's default for a main checkout without a record (module doc)."""
	if IN_TREE:
		monkeypatch.setattr(homeconfig, "FLOW_DEFAULT", statedMode(request))


@pytest.fixture(autouse=True)
def homeDirs(monkeypatch, tmp_path_factory):
	"""This test's home settings and state (module doc): a copy of the tree's config/ without the person's files,
	a scratch home state directory; nothing outside a home tree (IN_TREE)."""
	if not IN_TREE:
		return
	config = config_fixtures.copyConfig(homeconfig.HERE, tmp_path_factory.mktemp("home-config") / "config")
	config_fixtures.useConfig(monkeypatch, config)
	state = str(tmp_path_factory.mktemp("home-state"))
	monkeypatch.setattr(statefile, "homeStateDir", lambda: state)

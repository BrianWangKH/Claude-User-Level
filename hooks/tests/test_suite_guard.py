"""conftest.py's recursion guard (hooks/RULES.md T7): a run of this suite started from inside a running one
stops before collecting anything, so a test (or a tool a test calls) can't fan out into recursive runs. A
pytest-xdist worker of the holding session joins it; a run started from inside a test, serial or under xdist,
is still refused.

Run (from the home root): bash -c 'ulimit -u $(( $(ps -u $(id -u) -L --no-headers | wc -l) + 400 )); ulimit -v
4000000; exec uv run --offline --no-project --with pytest --with pytest-xdist==3.8.0 python -m pytest
hooks/tests/test_suite_guard.py -q -p no:cacheprovider'
"""

import os
import shutil
import subprocess
import sys

import pytest

import conftest

TESTS = os.path.dirname(os.path.abspath(__file__))
# Runs inside the fixture suite's xdist workers: the worker joined, and a run started from this test (whose
# parent is the worker, not the controller) is refused before collecting.
PROBE = '''import os
import subprocess
import sys


def test_a_worker_joins_and_a_run_from_inside_it_is_refused():
	assert os.environ.get("PYTEST_XDIST_WORKER")
	nested = subprocess.run([sys.executable, "-m", "pytest", __file__, "--collect-only", "-q", "-p",
		"no:cacheprovider"], capture_output=True, text=True, timeout=120, cwd=os.path.dirname(__file__))
	assert nested.returncode == 3, nested.stdout + nested.stderr
	assert "a nested run of it is refused" in nested.stdout + nested.stderr
'''


def test_the_first_session_claims_the_suite_and_a_second_one_is_refused():
	environ = {}
	assert conftest.claimSuite(environ, "100", "1", False)
	assert conftest.claimSuite(environ, "100", "1", False)  # the owner again (a plugin reloading conftest)
	assert not conftest.claimSuite(environ, "200", "1", False)
	assert not conftest.claimSuite(environ, "200", "100", False)  # a child of the holder that is no xdist worker
	assert environ == {conftest.SUITE_MARK: "100"}


def test_an_xdist_worker_joins_only_the_session_that_started_it():
	environ = {conftest.SUITE_MARK: "100"}
	assert conftest.claimSuite(environ, "200", "100", True)
	assert not conftest.claimSuite(environ, "300", "200", True)  # a worker of a session started inside a test
	assert not conftest.claimSuite(environ, "300", "1", True)
	assert environ == {conftest.SUITE_MARK: "100"}


def test_a_nested_run_of_this_suite_stops_before_collecting():
	# This process holds the claim (or is a worker of the session that does), and the child inherits it.
	# --collect-only and one small file: even if the guard were broken, the child would only list tests,
	# never run them (no recursion).
	result = subprocess.run(
		[
			sys.executable,
			"-m",
			"pytest",
			os.path.join(TESTS, "test_hookrun.py"),
			"--collect-only",
			"-q",
			"-p",
			"no:cacheprovider",
		],
		capture_output=True,
		text=True,
		timeout=120,
		cwd=TESTS,
	)
	assert result.returncode == conftest.NESTED_EXIT, result.stdout + result.stderr
	assert "a nested run of it is refused" in result.stdout + result.stderr
	assert "test_" not in result.stdout


def test_a_nested_xdist_run_of_this_suite_stops_before_starting_workers():
	pytest.importorskip("xdist", reason="pytest-xdist is not in this run (the documented commands add it)")
	result = subprocess.run(
		[sys.executable, "-m", "pytest", os.path.join(TESTS, "test_hookrun.py"), "--collect-only", "-q", "-p",
			"no:cacheprovider", "-n", "2", "--max-worker-restart", "0"],
		capture_output=True,
		text=True,
		timeout=120,
		cwd=TESTS,
	)
	assert result.returncode == conftest.NESTED_EXIT, result.stdout + result.stderr
	assert "a nested run of it is refused" in result.stdout + result.stderr


def test_xdist_workers_join_a_fresh_suite_and_refuse_a_run_from_inside_a_test(tmp_path):
	# A fixture suite in tmp_path with a copy of this conftest, started without the claim (as from a terminal).
	pytest.importorskip("xdist", reason="pytest-xdist is not in this run (the documented commands add it)")
	shutil.copy(os.path.join(TESTS, "conftest.py"), tmp_path / "conftest.py")
	(tmp_path / "test_probe.py").write_text(PROBE, encoding="utf-8")
	environ = {k: v for k, v in os.environ.items() if k not in (conftest.SUITE_MARK, "PYTEST_XDIST_WORKER")}
	result = subprocess.run(
		[sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider", "-n", "2",
			"--max-worker-restart", "0"],
		capture_output=True,
		text=True,
		timeout=300,
		cwd=tmp_path,
		env=environ,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "1 passed" in result.stdout

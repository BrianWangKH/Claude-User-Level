"""worker_budget.py, the `pre` entry's time checkpoint of a headless worker run: with CLAUDE_WORKER_MINUTES set,
every tool call past the run's minutes is denied (STATUS: blocked instruction); the clock is the newest started
run's start (runstate.startedSeconds), else the session's first call recorded in .hook-only/worker-budget/. An
unreadable value raises, which the pre entry turns into a deny (fail closed, tests/test_guard_steps.py).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_worker_budget.py -q -p no:cacheprovider'
"""

import json
import os
import subprocess
import sys
import time

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import worker_budget  # noqa: E402

SESSION = "s1"
ENV = "CLAUDE_WORKER_MINUTES"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
	subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
	return tmp_path


def call(checkout):
	return worker_budget.check({"session_id": SESSION, "cwd": str(checkout), "tool_name": "Bash"})


def denied(out):
	return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def startRun(checkout, minutesAgo):
	"""run 1's start file, as run-worker.sh writes it, minutesAgo minutes back."""
	runs = checkout / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	started = clock.now().timestamp() - minutesAgo * 60
	stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started))
	(runs / "start-1").write_text(stamp[:-2] + ":" + stamp[-2:] + "\n")


def test_outside_a_worker_run_it_does_nothing(checkout, monkeypatch):
	monkeypatch.delenv(ENV, raising=False)
	assert call(checkout) is None
	assert not (checkout / ".hook-only").exists()


def test_a_run_past_its_minutes_is_denied_with_the_stop_instruction(checkout, monkeypatch):
	startRun(checkout, 30)
	monkeypatch.setenv(ENV, "10")
	out = call(checkout)
	assert denied(out)
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert reason.startswith("Worker time estimate reached (30 of 10 min)") and "STATUS: blocked" in reason
	monkeypatch.setenv(ENV, "60")
	assert call(checkout) is None


def test_without_a_started_run_the_session_s_first_call_starts_the_clock(checkout, monkeypatch):
	monkeypatch.setenv(ENV, "10")
	assert call(checkout) is None
	state = checkout / ".hook-only" / "worker-budget" / f"{SESSION}.json"
	first = json.loads(state.read_text())
	assert first["minutes"] == 10
	state.write_text(json.dumps({**first, "started": first["started"] - 3600}))  # the first call, an hour ago
	assert denied(call(checkout))


@pytest.mark.parametrize("value", ["ten", "1e"])
def test_an_unreadable_minute_budget_raises(checkout, monkeypatch, value):
	monkeypatch.setenv(ENV, value)
	with pytest.raises(ValueError):
		call(checkout)


def test_an_unreadable_run_start_raises(checkout, monkeypatch):
	runs = checkout / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	(runs / "start-1").write_text("garbage\n")
	monkeypatch.setenv(ENV, "10")
	with pytest.raises(ValueError):
		call(checkout)

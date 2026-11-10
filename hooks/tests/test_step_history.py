"""step_history.py (worker self-repair 3c): wrap-up step time limits from the repository's history.

1. limitOf, the rule: fewer than step_limit_min_samples_count samples give the step's default (never a short limit
   from one or two quick runs); otherwise the median of the newest step_limit_history_count samples times
   step_limit_factor; no floor.
2. The samples: done last attempts in the worktrees' and the archive's run records; a failed or running step, an
   unreadable record, a time running backwards, a symlinked archive entry give none; the finish's come from the
   archive's finish.json files that removed their worktree.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_step_history.py -q -p no:cacheprovider'
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import runstate  # noqa: E402
import statefile  # noqa: E402
import step_history  # noqa: E402

LIMITS = runstate.settings()
TAIPEI = timezone(timedelta(hours=8))
START = datetime(2026, 11, 5, 8, 0, 0, tzinfo=TAIPEI)
MIN = LIMITS["step_limit_min_samples_count"]


def samples(*seconds):
	"""[(ended_at, seconds)], the first the newest."""
	return [(START - timedelta(hours=k), s) for k, s in enumerate(seconds)]


# -- 1. the rule -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", runstate.STEP_ORDER)
def test_too_few_samples_give_the_step_s_lenient_default(name):
	seconds, basis = step_history.limitOf(samples(*[1] * (MIN - 1)), name, LIMITS)
	assert seconds == LIMITS["step_limit_default_seconds"][name] and basis.startswith("the default")


def test_enough_samples_give_the_median_times_the_factor_of_the_newest_only():
	count = LIMITS["step_limit_history_count"]
	newest = [60 * (k + 1) for k in range(count)]
	seconds, basis = step_history.limitOf(samples(*newest, 99999, 99999), "verify", LIMITS)
	median = sorted(newest)[count // 2 - 1: count // 2 + 1]
	assert seconds == sum(median) / 2 * LIMITS["step_limit_factor"] and "median" in basis


def test_no_floor_only_the_sample_count_guards_a_short_limit():
	seconds, _basis = step_history.limitOf(samples(*[1] * MIN), "merge", LIMITS)
	assert seconds == LIMITS["step_limit_factor"]


# -- 2. the samples --------------------------------------------------------------------------------------------


def git(*args):
	return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=60).stdout


@pytest.fixture
def main(tmp_path):
	checkout = tmp_path / "main"
	git("init", "-q", "-b", "main", str(checkout))
	git("-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	return str(checkout.resolve())


def recordWith(root, **steps):
	run = runstate.create(root, {"topic": os.path.basename(root), "started_by": "person", "purpose": "build",
		"flow": "written", "launch": {"launched_at": START.isoformat(), "by_pid": 1}})
	runstate.update(root, run, lambda r: r.update(steps=steps))
	return run


def took(seconds, state="done", ended=None):
	ended = ended or START + timedelta(seconds=seconds)
	return {"state": state, "attempt": 1, "started_at": START.isoformat(), "ended_at": ended.isoformat()}


def test_done_attempts_of_worktrees_and_the_archive_count_and_nothing_else(main, tmp_path):
	worktree = str(tmp_path / "worktrees" / "live")
	git("-C", main, "worktree", "add", "-q", "-b", "feature/x/live", worktree)
	recordWith(worktree, verify=took(10))
	archived = runstate.archiveOf(main, "old")
	recordWith(archived, verify=took(20), merge=took(5, state="failed"))
	recordWith(archived, verify=took(30, ended=START - timedelta(seconds=1)))  # ends before it starts: no sample
	recordWith(archived, verifier=took(7, state="running"))
	broken = recordWith(archived, verify=took(40))
	with open(statefile.pathOf(archived, runstate.RECORD, broken), "w") as f:
		f.write("{garbage")
	elsewhere = runstate.archiveOf(str(tmp_path / "elsewhere"), "forged")
	recordWith(elsewhere, verify=took(1))
	os.symlink(elsewhere, runstate.archiveOf(main, "forged"))
	assert sorted(s for _, s in step_history.stepSamples(main, "verify")) == [10, 20]
	assert step_history.stepSamples(main, "merge") == [] and step_history.stepSamples(main, "verifier") == []


def test_no_main_checkout_gives_no_samples_so_the_default(tmp_path):
	assert step_history.stepSamples(None, "verify") == [] and step_history.stepSamples(str(tmp_path / "x"), "verify") == []
	assert step_history.stepLimit(None, "next")[0] == LIMITS["step_limit_default_seconds"]["next"]


def finishJson(main, topic, steps, **times):
	statefile.write(main, step_history.FINISH_RECORD, {"schema": statefile.kind(step_history.FINISH_RECORD)["schema"],
		"topic": topic, "run": 1, "steps": steps, **times}, topic)


def test_the_finish_s_samples_are_finishes_that_removed_their_worktree(main):
	times = {"started_at": START.isoformat(), "ended_at": (START + timedelta(seconds=90)).isoformat()}
	finishJson(main, "a", {"remove": "removed"}, **times)
	finishJson(main, "b", {"import": "boom"}, **times)
	finishJson(main, "c", {"remove": "removed"})  # from before the times were recorded
	path = statefile.pathOf(main, step_history.FINISH_RECORD, "d")
	with open(path, "w") as f:
		json.dump({"schema": 99, "steps": {"remove": "removed"}, **times}, f)
	assert [s for _, s in step_history.stepSamples(main, "finish")] == [90]

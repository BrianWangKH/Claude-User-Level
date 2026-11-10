"""budget_state.py: the per-agent budget state file's writes in one module (subagent_budget.py's state and removal,
the stale sweep; the gates' wait marks are tested in test_quality_gates.py and test_budget.py).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_budget_state.py -q -p no:cacheprovider'
"""

import json
import os
import sys
import time

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import budget_state  # noqa: E402

# The concurrency tests start their processes with spawn, not fork: under pytest-xdist the worker running them
# has threads, and fork() from a threaded process may deadlock the child (Python 3.12 warns about it).
SPAWN = "spawn"


def test_the_state_is_written_where_subagent_budget_reads_it_and_removed_at_the_end(tmp_path):
	root = str(tmp_path)
	budget_state.write(root, "a1", {"minutes": 10, "started": 1000.0})
	path = os.path.join(root, ".hook-only", "budget", "a1.json")
	assert budget_state.statePath(root, "a1") == path
	with open(path, encoding="utf-8") as f:
		assert json.load(f) == {"minutes": 10, "started": 1000.0}
	budget_state.remove(root, "a1")
	assert not os.path.exists(path)
	budget_state.remove(root, "a1")  # already gone: no error


def test_the_sweep_removes_only_json_files_older_than_a_week(tmp_path):
	budgetDir = tmp_path / "budget"
	budgetDir.mkdir()
	old = time.time() - budget_state.STALE_SECONDS - 60
	for name in ("old.json", "old.jsonl", "new.json"):
		(budgetDir / name).write_text("{}")
	for name in ("old.json", "old.jsonl"):
		os.utime(budgetDir / name, (old, old))
	budget_state.removeStaleStates(str(budgetDir))
	assert sorted(os.listdir(budgetDir)) == ["new.json", "old.jsonl"]
	budget_state.removeStaleStates(str(tmp_path / "missing"))  # no directory: nothing to do


def test_the_sweep_removes_a_week_old_lock_too(tmp_path):
	budgetDir = tmp_path / "budget"
	budgetDir.mkdir()
	old = time.time() - budget_state.STALE_SECONDS - 60
	(budgetDir / "a1.json.lock").write_text("")
	os.utime(budgetDir / "a1.json.lock", (old, old))
	budget_state.removeStaleStates(str(budgetDir))
	assert os.listdir(budgetDir) == []


def excludeMany(root, agentId, times):
	for _ in range(times):
		budget_state.excludeWait(root, agentId, 1.0)


def markMany(root, agentId, times):
	for i in range(times):
		budget_state.markWait(root, agentId, f"gate {i}", 60)


def test_concurrent_writers_never_lose_an_update(tmp_path):
	"""Three writers today (subagent_budget, crap-gate and mutation through markWait / excludeWait): each
	read-modify-write holds the lock, so every excluded second lands (RULES S9)."""
	import multiprocessing

	root = str(tmp_path)
	budget_state.write(root, "a1", {"minutes": 10, "started": 1000.0})
	context = multiprocessing.get_context(SPAWN)
	workers = [context.Process(target=excludeMany, args=(root, "a1", 25)) for _ in range(4)]
	workers.append(context.Process(target=markMany, args=(root, "a1", 25)))
	for w in workers:
		w.start()
	for w in workers:
		w.join(60)
		assert w.exitcode == 0
	with open(budget_state.statePath(root, "a1"), encoding="utf-8") as f:
		state = json.load(f)
	assert state["started"] == 1000.0 + 4 * 25
	with open(os.path.join(root, ".hook-only", "budget", "events.jsonl"), encoding="utf-8") as f:
		assert len(f.read().splitlines()) == 4 * 25


def readMany(path, times, failures):
	for _ in range(times):
		try:
			with open(path, encoding="utf-8") as f:
				json.load(f)
		except ValueError:
			failures.value += 1


def test_a_reader_never_sees_half_a_state(tmp_path):
	import multiprocessing

	root = str(tmp_path)
	big = {"minutes": 10, "started": 1000.0, "note": "x" * 200000}
	budget_state.write(root, "a1", big)
	context = multiprocessing.get_context(SPAWN)
	failures = context.Value("i", 0)
	reader = context.Process(target=readMany, args=(budget_state.statePath(root, "a1"), 400, failures))
	reader.start()
	for i in range(100):
		budget_state.write(root, "a1", {**big, "minutes": i})
	reader.join(60)
	assert reader.exitcode == 0 and failures.value == 0
	assert [n for n in os.listdir(os.path.dirname(budget_state.statePath(root, "a1"))) if n.endswith(".tmp")] == []

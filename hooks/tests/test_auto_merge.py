"""auto-merge.py's gate since run record migration step 5: the run's facts come from runstate.gateView (the record
and today's files, each through runstate.status), never looser than the checks of today's files alone (the old
check, frozen in legacy_rules.py), proven over a matrix of runs (gate_fixtures.cases: both sources and each alone, every phase, every
worker STATUS spelling, every main head, every verify and verifier outcome agreeing and not under every spec, a
pending ALERT or HOLD, every way a record can be unusable; the person's approval and the automatic one); T-1, a
missing or unknown fact refuses with a decision and an event; S7, a crash leaves an error decision and an event;
auto-finish.py removes nothing the run record doesn't hold as merged.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_auto_merge.py -q -p no:cacheprovider'
"""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import timedelta
from collections import Counter

import pytest

import gate_fixtures as gf
import legacy_rules

HOOKS = gf.HOOKS
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.gateHome(), "hooks")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import qg_common  # noqa: E402
import runstate  # noqa: E402

pytestmark = pytest.mark.flow_mode("local")
OUTWARD = ("merge", "merge_run", "fix_run")


def loadAutoMerge():
	spec = importlib.util.spec_from_file_location("auto_merge_under_test", os.path.join(HOOKS, "worker", "auto-merge.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def memo(function):
	"""git answers of a world that never changes while the matrix runs, asked once each."""
	seen = {}

	def cached(*args, **kwargs):
		key = (args, tuple(sorted(kwargs.items())))
		if key not in seen:
			seen[key] = function(*args, **kwargs)
		return seen[key]

	return cached


@pytest.fixture(scope="module")
def matrixWorld(tmp_path_factory):
	"""(world, auto-merge module): one git world for the whole matrix; the module's test suites never run (they
	are the same call for both gates) and its git answers are cached."""
	world = gf.makeWorld(tmp_path_factory.mktemp("gate"))
	module = loadAutoMerge()
	module.checkSuites = lambda worktree, config: None
	module.git = memo(module.git)
	return world, module


def judge(module, gate, world, person):
	"""(what the gate lets happen, its reason): merge, merge_run, fix_run, refused, or crash (an exception)."""
	try:
		gate(str(world["wt"]), gf.TOPIC, gf.RUN, str(world["main"]), person)
		return "merge", ""
	except module.NeedsMergeRun as refusal:
		return "merge_run", str(refusal)
	except module.NeedsFixRun as refusal:
		return "fix_run", str(refusal)
	except module.Refused as refusal:
		return "refused", str(refusal)
	except Exception as error:
		return "crash", f"{type(error).__name__}: {error}"


def runMatrix(world, module):
	"""One row per case and approval: (case, person, old, old reason, new, new reason)."""
	rows = []
	oldCheck = legacy_rules.autoMerge(module)["check"]
	for case in gf.cases():
		gf.tellRun(world, **case)
		for person in (False, True):
			old = judge(module, oldCheck, world, person)
			new = judge(module, module.checkRun, world, person)
			rows.append((case, person, *old, *new))
	return rows


def caseOnlyTwin(row, rows):
	"""row's lower-case twin (gate_fixtures.CASE_ONLY): the matrix row of the same case and approval with the worker's
	STATUS written `done`; None when row's STATUS is no other-case done."""
	case, person = row[0], row[1]
	if case.get("status") not in gf.CASE_ONLY:
		return None
	twin = {**case, "status": gf.CASE_ONLY[case["status"]]}
	found = [r for r in rows if r[1] == person and {**gf.BASE, **r[0]} == {**gf.BASE, **twin}]
	assert found, f"no lower-case twin of {case} in the matrix"
	return found[0]


def test_the_new_gate_never_lets_happen_what_the_old_one_refused(matrixWorld):
	"""Never looser, with one named exception: a done STATUS in another case (R2) gets exactly the new gate's answer
	for the same run with `STATUS: done` (caseOnlyTwin), never more than the old gate gave that run."""
	world, module = matrixWorld
	rows = runMatrix(world, module)
	twins = {id(r): caseOnlyTwin(r, rows) for r in rows}
	looser = [r for r in rows if r[4] in OUTWARD and r[4] != r[2] and not (twins[id(r)] and r[4] == twins[id(r)][2])]
	assert not looser, looser[:5]
	differ = [(r[0], r[1], r[4], twins[id(r)][4]) for r in rows if twins[id(r)] and r[4] != twins[id(r)][4]]
	assert not differ, differ[:5]
	opened = Counter(r[4] for r in rows if twins[id(r)] and r[4] != r[2])
	assert opened["merge"], opened  # the named change happens: an upper- or mixed-case done merges now
	assert not [r for r in rows if r[4] == "crash"], [r for r in rows if r[4] == "crash"][:5]
	outcomes = Counter((r[2], r[4]) for r in rows)
	# the matrix reaches every outcome of both gates, so the comparison above is not vacuous
	assert {o for o, _ in outcomes} >= {"merge", "merge_run", "fix_run", "refused"}
	assert {n for _, n in outcomes} >= {"merge", "merge_run", "fix_run", "refused"}
	assert outcomes[("merge", "merge")] and outcomes[("merge_run", "merge_run")] and outcomes[("fix_run", "fix_run")]


def test_the_matrix_s_base_run_merges_by_both_gates(matrixWorld):
	world, module = matrixWorld
	gf.tellRun(world)
	oldCheck = legacy_rules.autoMerge(module)["check"]
	assert judge(module, oldCheck, world, False) == judge(module, module.checkRun, world, False) == ("merge", "")


# --- T-1: a missing or unknown fact refuses, as a decision and an event -----------------------------------------


@pytest.fixture
def world(tmp_path):
	return gf.makeWorld(tmp_path)


def autoMerge(world, person=False):
	"""auto-merge.py on run 1 as run-worker.sh (or wt merge) calls it: (exit code, decision, events)."""
	env = {**os.environ, "WT_MAIN_DIR": str(world["main"])}
	result = subprocess.run(
		[
			sys.executable,
			os.path.join(RUN_HOOKS, "worker", "auto-merge.py"),
			str(world["wt"]),
			gf.TOPIC,
			"1",
			*(["--person"] if person else []),
		],
		capture_output=True,
		text=True,
		timeout=600,
		env=env,
	)
	path = gf.runsOf(world) / "merge-1.json"
	decision = json.loads(path.read_text()) if path.exists() else None
	return result, decision, events(world)


def events(world):
	path = world["main"] / ".hook-only" / "events.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def refusedAndTold(world, *expected, **bent):
	gf.tellRun(world, **bent)
	head = gf.git(world["main"], "rev-parse", "HEAD")
	result, decision, told = autoMerge(world)
	assert result.returncode == 0, result.stderr
	assert decision["decision"] == "refused" and "fix_run" not in decision and "merge_run" not in decision, decision
	for text in expected:
		assert text in decision["reason"], decision["reason"]
	assert told[-1]["kind"] == "refused" and told[-1]["text"] == decision["reason"]
	assert gf.git(world["main"], "rev-parse", "HEAD") == head
	return decision


def test_the_fixture_run_merges(world):
	gf.tellRun(world)
	result, decision, told = autoMerge(world)
	assert decision["decision"] == "merged", (decision, result.stderr)
	assert told[-1]["kind"] == "merged"


@pytest.mark.parametrize("text", ["STATUS: done\n\nall green", "STATUS: DONE\n\nall green", "STATUS: Done\n\nall green"])
def test_a_worker_done_in_any_case_merges(world, text):
	"""A worker that ended `STATUS: DONE` was refused as not done; every spelling of done
	merges (runstate.workerStatus)."""
	gf.tellRun(world, status=text)
	result, decision, told = autoMerge(world)
	assert decision["decision"] == "merged", (decision, result.stderr)
	assert told[-1]["kind"] == "merged"


def test_a_worker_status_other_than_done_never_merges_in_any_case(world):
	for text in ("STATUS: BLOCKED\n", "STATUS: Needs-Decision\n", "STATUS: DONE-ish\n", "STATUS: DONE and more\n"):
		refusedAndTold(world, "STATUS", status=text)


def test_auto_merge_refuses_without_run_record(world):
	refusedAndTold(world, "has no run record", source="legacy")


def test_auto_merge_refuses_with_unknown_schema(world):
	refusedAndTold(world, "the run state is unknown", corrupt="schema-2")


def test_auto_merge_refuses_without_claude_status(world):
	refusedAndTold(world, "the worker's STATUS is None", status="missing")


def test_auto_merge_refuses_with_status_outside_enum(world):
	refusedAndTold(world, "the run state is unknown", corrupt="status-enum")
	refusedAndTold(world, "the worker's STATUS is None", status="STATUS: done-ish\n")


def test_auto_merge_refuses_without_verify_step_when_spec_exists(world):
	refusedAndTold(world, "verify.py left no result", "verify step missing", spec="checks", verify=("pass", None))


def test_auto_merge_refuses_with_verify_not_pass(world):
	refusedAndTold(world, "could not run", spec="checks", verify=("error", "error"))


def test_auto_merge_refuses_without_main_head_at_start(world):
	refusedAndTold(world, "no main head recorded at the start", head="missing")


@pytest.mark.parametrize("how", ["not-json", "no-started-by", "naive-time", "step-outcome"])
def test_auto_merge_refuses_a_record_with_a_missing_field_an_unknown_value_or_a_naive_time(world, how):
	refusedAndTold(world, "the run state is unknown", corrupt=how)


def test_auto_merge_refuses_when_the_record_and_today_s_files_disagree(world):
	refusedAndTold(world, "different main heads", head="differs")
	decision = refusedAndTold(world, "verify step", spec="checks", verify=("fail", "pass"))
	assert "fix_run" not in decision  # only a failure both stories hold asks for a fix run


def stoppedAfterWork(world, claudeStopped=False, stopSeconds=30):
	"""Run 1 merges by the base facts, then `wt stop` came stopSeconds after claude's part ended (negative: during it)
	and the run ended stopped, told in both stories: the record (claude.stopped, stop, exit) and today's files
	(stopped-1 with the stop's time, result-1.json's time as claude's end)."""
	gf.tellRun(world, phase="ended")
	root, runs = str(world["wt"]), gf.runsOf(world)
	ended = gf.clock.now().replace(microsecond=0) - timedelta(seconds=120)
	os.utime(runs / "result-1.json", (ended.timestamp(), ended.timestamp()))
	stopAt = gf.clock.isoOf(ended + timedelta(seconds=stopSeconds))
	gf.write(runs / "stopped-1", stopAt)
	runstate.update(root, 1, lambda r: (r["claude"].update(stopped=claudeStopped, ended_at=gf.clock.isoOf(ended)),
		r.update(stop={"requested_at": stopAt, "by": "person"}, exit={**r["exit"], "reason": "stopped"})))


def test_a_stop_during_the_wrapup_refuses_the_automatic_merge_and_lets_the_person_approve(world):
	"""1b: a run stopped only during its wrap-up (claude's part completed) is never merged automatically; the
	person's `wt merge` (--person) passes that stop and every other check still runs."""
	stoppedAfterWork(world)
	head = gf.git(world["main"], "rev-parse", "HEAD")
	result, decision, told = autoMerge(world)
	assert decision["decision"] == "refused" and "the run was stopped" in decision["reason"], decision
	assert "the run ended stopped" in decision["reason"] and gf.git(world["main"], "rev-parse", "HEAD") == head
	result, decision, told = autoMerge(world, person=True)
	assert decision["decision"] == "merged" and decision["review"] == "person", (decision, result.stderr)


def test_the_person_s_approval_of_a_stop_during_the_wrapup_still_runs_every_other_check(world, monkeypatch):
	stoppedAfterWork(world)
	module = loadAutoMerge()
	monkeypatch.setattr(module, "checkSuites", lambda worktree, config: (_ for _ in ()).throw(
		module.Refused("the suite fails")))
	with pytest.raises(module.Refused, match="the suite fails"):
		module.checkRun(str(world["wt"]), gf.TOPIC, gf.RUN, str(world["main"]), True)
	gf.write(world["wt"] / ".hook-only" / "agent-guard" / "HOLD", "x")
	with pytest.raises(module.Refused, match="HOLD"):
		module.checkRun(str(world["wt"]), gf.TOPIC, gf.RUN, str(world["main"]), True)


@pytest.mark.parametrize("claudeStopped, stopSeconds", [(True, 30), (False, -30), (False, 0)],
	ids=["claude-part-stopped", "files-say-during-claude", "same-second"])
def test_a_stop_of_claude_s_part_refuses_the_person_s_approval_too(world, claudeStopped, stopSeconds):
	"""A claude-part stop in either story (the record's claude.stopped, or today's files placing the stop at or before
	claude's end) refuses both the automatic merge and the person's."""
	stoppedAfterWork(world, claudeStopped=claudeStopped, stopSeconds=stopSeconds)
	head = gf.git(world["main"], "rev-parse", "HEAD")
	for person in (False, True):
		result, decision, told = autoMerge(world, person=person)
		assert decision["decision"] == "refused" and "the run was stopped" in decision["reason"], (person, decision)
	assert gf.git(world["main"], "rev-parse", "HEAD") == head


def test_auto_merge_refuses_a_stale_run_without_a_pid_check(world):
	refusedAndTold(world, "orphan", phase="orphan-stale")


# --- S7: a crash leaves an error decision and an event ---------------------------------------------------------


def crashRun(world, monkeypatch, *patches):
	"""auto-merge.py's main() in this process with module attributes replaced: (exit code, decision, events)."""
	module = loadAutoMerge()
	for name, value in patches:
		monkeypatch.setattr(module, name, value)
	monkeypatch.setenv("WT_MAIN_DIR", str(world["main"]))
	monkeypatch.setattr(sys, "argv", ["auto-merge.py", str(world["wt"]), gf.TOPIC, "1"])
	with pytest.raises(SystemExit) as stopped:
		module.main()
	path = gf.runsOf(world) / "merge-1.json"
	return stopped.value.code, json.loads(path.read_text()) if path.exists() else None, events(world)


def boom(*args, **kwargs):
	raise RuntimeError("boom\nsecond line")


def test_a_crash_writes_an_error_decision_the_merge_step_and_a_refused_event(world, monkeypatch):
	gf.tellRun(world)
	code, decision, told = crashRun(world, monkeypatch, ("checkRun", boom))
	assert code == 1
	assert (
		decision["decision"] == "error" and decision["reason"] == "auto-merge.py failed: RuntimeError: boom second line"
	)
	step = runstate.readRecord(str(world["wt"]), 1)[0]["steps"]["merge"]
	assert (step["outcome"], step["reason"]) == ("error", decision["reason"])
	assert told[-1]["kind"] == "refused" and told[-1]["text"] == decision["reason"]


def test_a_crash_after_the_decision_leaves_the_decision_and_tells(world, monkeypatch):
	gf.tellRun(world, held="HOLD")
	code, decision, told = crashRun(world, monkeypatch, ("recordDecision", boom))
	assert code == 1 and decision["decision"] == "refused" and "HOLD" in decision["reason"]
	assert told[-1]["kind"] == "refused" and "which stands" in told[-1]["text"] and "boom" in told[-1]["text"]


def test_a_crash_never_overwrites_a_decision_another_evaluation_wrote_meanwhile(world, monkeypatch):
	gf.tellRun(world)
	other = {"run": 1, "decision": "merged", "into": "main", "commit": "abc"}

	def otherThenBoom(*args, **kwargs):
		gf.write(gf.runsOf(world) / "merge-1.json", other)
		raise RuntimeError("boom")

	code, decision, told = crashRun(world, monkeypatch, ("checkRun", otherThenBoom))
	assert code == 1 and decision == other
	assert "which stands" in told[-1]["text"]


# --- auto-finish.py: nothing removed unless the record says merged -----------------------------------------------


def runFinish(world):
	home = world["main"].parent / "home"
	(home / ".claude").mkdir(parents=True, exist_ok=True)
	return subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "auto-finish.py"), str(world["wt"]), gf.TOPIC, "1"],
		capture_output=True,
		text=True,
		timeout=120,
		env={**os.environ, "HOME": str(home), "WT_MAIN_DIR": str(world["main"])},
	)


@pytest.mark.parametrize("recorded", [None, "refused"])
def test_auto_finish_removes_nothing_the_run_record_does_not_hold_as_merged(world, recorded):
	gf.tellRun(world, phase="ended")
	merged = {"run": 1, "decision": "merged", "into": "main", "commit": "abc1234567890"}
	if recorded:
		gf.write(gf.runsOf(world) / "merge-1.json", {"run": 1, "decision": recorded, "reason": "x"})
		runstate.recordStepFromFile(str(world["wt"]), 1, "merge")
	gf.write(gf.runsOf(world) / "merge-1.json", merged)
	assert runFinish(world).returncode == 0
	finished = json.loads((world["main"] / ".hook-only" / "archive" / "topic.finish.json").read_text())
	assert "merge step" in finished["steps"]["check"] and "archive" not in finished["steps"]
	assert world["wt"].exists()

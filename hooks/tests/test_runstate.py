"""runstate.py and run-status.py: the run record, its phases, the legacy adapter for today's runs/ files, the
scripted flow, the merge facts (dangerous direction: anything missing or unknown refuses), the renderers and
the read-only CLI. Records are written through runstate's own writer, so writer and reader share the
fixtures (hooks/RULES.md T9).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_runstate.py -q -p no:cacheprovider'
"""

import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import config_fixtures  # noqa: E402
import eventlog  # noqa: E402
import hookonly  # noqa: E402
import report_header  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
import orphan_close_baseline as baseline  # noqa: E402
import wrapup_stop_baseline  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 8, 0, 0, tzinfo=TAIPEI)
BOOT = "b00t-1"
TOPIC = "cash-account"
LIMITS = runstate.settings()
INTERVAL = LIMITS["heartbeat_interval_seconds"]
THRESHOLD = INTERVAL * LIMITS["orphan_missed_beats_count"]
WRAPUP = LIMITS["legacy_wrapup_stale_minutes"] * 60
LANGS = sorted(LIMITS["status_text"])


def loadCli():
	spec = importlib.util.spec_from_file_location("run_status", os.path.join(HOOKS, "worker", "run-status.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class FakeClock:
	def __init__(self, moment):
		self.moment = moment

	def now(self):
		return self.moment

	def advance(self, seconds):
		self.moment += timedelta(seconds=seconds)


@pytest.fixture
def fake():
	source = FakeClock(START)
	clock.useSource(source.now)
	yield source
	clock.useSource(None)


def gitRun(*args):
	subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=60)


@pytest.fixture
def main(tmp_path, monkeypatch):
	"""A main checkout with one commit and a visible plans/ (a planning checkout outside the sandbox)."""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	checkout = tmp_path / "main"
	gitRun("init", "-q", str(checkout))
	gitRun("-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	(checkout / "plans").mkdir()
	(checkout / "plans" / "repo-plan.md").write_text("plan\n")
	return checkout


def addTopic(main, topic=TOPIC):
	worktree = main.parent / "worktrees" / topic
	gitRun("-C", str(main), "worktree", "add", "-q", "-b", f"feature/x/{topic}", str(worktree))
	return worktree


BASE = {
	"started_by": "person",
	"purpose": "build",
	"flow": "written",
	"integration_mode": "local",
	"prompt_kind": "feature-flow",
}


def launch(worktree, topic=TOPIC, **fields):
	body = {**BASE, "topic": topic, "launch": {"launched_at": clock.isoNow(), "by_pid": 1}, **fields}
	body.setdefault("estimate", {"minutes": 40, "usd": 6.0, "cap_minutes": 52, "cap_usd": 7.8})
	return runstate.create(str(worktree), body)


def change(worktree, run, **fields):
	return runstate.update(str(worktree), run, lambda r: r.update(fields))


def start(worktree, run, boot=BOOT, pid=4242):
	change(
		worktree,
		run,
		process={"wrapper_pid": pid, "start_ticks": 7, "boot_id": boot, "started_at": clock.isoNow()},
		main_head_at_start="65f7802",
	)
	runstate.beat(str(worktree), run, INTERVAL)


def claudeEnded(worktree, run, status="done", outcome="status"):
	"""claude's end as run-worker.sh leaves it: the worker's result-<run>.json, then the record's claude part."""
	path = os.path.join(runstate.runsDir(str(worktree)), f"result-{run}.json")
	with open(path, "w", encoding="utf-8") as f:
		f.write(json.dumps({"result": f"STATUS: {status}\n\nwhat was done"}))
	change(
		worktree,
		run,
		claude={
			"invocations": [
				{
					"k": 0,
					"kind": "main",
					"file": f"result-{run}.json",
					"started_at": clock.isoNow(),
					"ended_at": clock.isoNow(),
					"exit_code": 0,
					"cost_usd": 1.0,
				}
			],
			"ended_at": clock.isoNow(),
			"outcome": outcome,
			"status": status,
			"header_file": f"result-{run}.json",
		},
	)


def steps(worktree, run, **named):
	change(worktree, run, steps=named)


def finish(worktree, run, reason="completed", code=0):
	change(worktree, run, exit={"ended_at": clock.isoNow(), "code": code, "reason": reason})


GOOD_STEPS = {
	"verify": {"state": "done", "attempt": 1, "outcome": "PASS", "detail": "verify-1.json"},
	"verifier": {"state": "skipped", "skip_reason": "no-verifier-section"},
}


def doneRun(worktree):
	"""A local run whose worker said done and whose real-data checks passed: the merge gate may go on."""
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run)
	steps(
		worktree,
		run,
		**GOOD_STEPS,
		merge={"state": "running", "attempt": 1, "runner_pid": 9, "started_at": clock.isoNow()},
	)
	return run


def statusOf(main, fake, topic=TOPIC, **kw):
	kw.setdefault("bootId", BOOT)
	return runstate.status(str(main), topic, clock=fake.now, **kw)


def recordPath(worktree, run):
	return statefile.pathOf(str(worktree), runstate.RECORD, run)


# -- phases ------------------------------------------------------------------------------------------


def test_a_topic_without_any_run_reads_none_and_not_busy(main, fake):
	addTopic(main)
	s = statusOf(main, fake)
	assert (s["phase"], s["run"], s["busy"], s["merge_allowed"]) == ("none", None, False, False)


def test_a_fresh_launch_reads_launching_and_busy(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	s = statusOf(main, fake)
	assert (s["run"], s["phase"], s["busy"], s["busy_usd"]) == (run, "launching", True, 6.0)


def test_a_started_wrapper_with_a_fresh_heartbeat_reads_running(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	fake.advance(THRESHOLD - 1)
	s = statusOf(main, fake)
	assert s["phase"] == "running" and s["warnings"] == []
	alive = [e for e in s["evidence"] if e["claim"] == "alive"]
	assert alive and alive[0]["field"] == "beat_at" and alive[0]["age_seconds"] == THRESHOLD - 1


def test_the_ended_worker_reads_finishing_with_the_running_step(main, fake):
	worktree = addTopic(main)
	doneRun(worktree)
	s = statusOf(main, fake)
	assert s["phase"] == "finishing"
	assert s["step"]["name"] == "merge" and s["step"]["since"] == clock.isoNow()


def test_an_exit_reads_ended_whatever_the_heartbeat_says(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	finish(worktree, run)
	fake.advance(THRESHOLD * 10)
	s = statusOf(main, fake, bootId="another-boot")
	assert (s["phase"], s["busy"], s["outcome"]["reason"]) == ("ended", False, "completed")


# -- orphans (T-3, T-4, T-7) -------------------------------------------------------------------------


def test_stale_heartbeat_reads_as_orphan(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	fake.advance(THRESHOLD + 1)
	s = statusOf(main, fake)
	assert (s["phase"], s["orphan_of"], s["confidence"], s["orphan_reasons"]) == (
		"orphan",
		"running",
		"suspected",
		["stale"],
	)


def test_boot_id_change_reads_as_orphan_immediately(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run, boot="old-boot")
	s = statusOf(main, fake, bootId="new-boot")
	assert (s["phase"], s["orphan_reasons"]) == ("orphan", ["boot"])
	assert statusOf(main, fake, bootId="")["phase"] == "running"  # no boot id here: the rule is skipped


def test_wrapper_exited_without_exit_reads_as_orphan(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	runstate.update(str(worktree), run, lambda r: r["process"].update(exited_at=clock.isoNow(), exit_code=143))
	s = statusOf(main, fake)
	assert (s["phase"], s["orphan_of"], s["orphan_reasons"]) == ("orphan", "finishing", ["wrapper-exited"])


def test_launching_without_start_past_threshold_reads_as_orphan(main, fake):
	worktree = addTopic(main)
	launch(worktree)
	fake.advance(THRESHOLD)
	assert statusOf(main, fake)["phase"] == "launching"
	fake.advance(1)
	s = statusOf(main, fake)
	assert (s["phase"], s["orphan_of"]) == ("orphan", "launching")


def test_status_without_pid_check_is_never_confirmed(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run, boot="old-boot")
	runstate.update(str(worktree), run, lambda r: r["process"].update(exited_at=clock.isoNow()))
	fake.advance(THRESHOLD * 100)
	s = statusOf(main, fake, bootId="new-boot")
	assert (s["confidence"], s["pid_checked"]) == ("suspected", False)


def test_a_pid_check_that_finds_the_process_gone_confirms_the_orphan(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	asked = []

	def gone(process, topic, number):
		asked.append((process["wrapper_pid"], process["start_ticks"], topic, number))
		return False

	s = statusOf(main, fake, pidAlive=gone)
	assert asked == [(4242, 7, TOPIC, run)]
	assert (s["phase"], s["confidence"], s["pid_checked"], s["busy"]) == ("orphan", "confirmed", True, False)


def test_a_live_process_with_a_stale_heartbeat_keeps_its_phase_with_a_warning(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	fake.advance(THRESHOLD * 5)
	s = statusOf(main, fake, pidAlive=lambda process, topic, number: True)
	assert (s["phase"], s["warnings"]) == ("running", ["heartbeat-stale-process-alive"])


def test_a_stale_heartbeat_after_sleep_reads_running_again_once_it_beats(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	fake.advance(THRESHOLD * 30)  # the laptop slept
	assert statusOf(main, fake)["phase"] == "orphan"
	runstate.beat(str(worktree), run, INTERVAL)  # awake again: the loop beats
	s = statusOf(main, fake)
	assert (s["phase"], s["confidence"]) == ("running", None)
	assert statefile.read(str(worktree), runstate.HEARTBEAT, run)["count"] == 2


def test_heartbeat_age_uses_aware_times(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	utc = FakeClock(START.astimezone(timezone.utc) + timedelta(seconds=30))
	s = runstate.status(str(main), TOPIC, clock=utc.now, bootId=BOOT)
	assert [e["age_seconds"] for e in s["evidence"] if e["claim"] == "alive"] == [30]
	with pytest.raises(statefile.StateError):
		statefile.write(
			str(worktree), runstate.HEARTBEAT, {"schema": 1, "beat_at": "2026-11-03T08:00:00", "count": 1}, run
		)


def test_suspected_orphan_counts_busy_and_confirmed_does_not(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	fake.advance(THRESHOLD + 1)
	assert statusOf(main, fake)["busy"] is True
	runstate.confirmOrphan(str(worktree), run, "worker-control", ["pid 4242 gone"])
	s = statusOf(main, fake)
	assert (s["confidence"], s["busy"], s["resumable"]["ok"]) == ("confirmed", False, False)  # no estimate yet


def test_a_recorded_orphan_is_confirmed_and_its_event_is_written_once(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	assert runstate.confirmOrphan(str(worktree), run, "wt_queue", ["pid gone"]) is True
	assert runstate.confirmOrphan(str(worktree), run, "wt_queue", ["pid gone"]) is False
	events = eventlog.read(str(worktree), event="run-orphaned")
	assert [(e["run"], e["data"]) for e in events] == [(run, {"by": "wt_queue", "orphan_of": "finishing"})]
	s = statusOf(main, fake)
	assert (s["phase"], s["confidence"], s["pid_checked"]) == ("orphan", "confirmed", False)


def test_orphan_close_never_marks_merge_done(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	runstate.confirmOrphan(str(worktree), run, "worker-control", ["pid gone"])
	record = runstate.closeOrphan(str(worktree), run, "worker-control")
	assert record["steps"]["merge"]["state"] == "abandoned" and "outcome" not in record["steps"]["merge"]
	s = statusOf(main, fake)
	assert (s["phase"], s["outcome"]["reason"], s["outcome"]["merge"], s["merge_allowed"]) == (
		"ended",
		"orphan-closed",
		None,
		False,
	)
	assert [e["data"] for e in eventlog.read(str(worktree), event="run-ended")] == [
		{"reason": "orphan-closed", "code": None}
	]


def test_orphan_close_never_starts_merge_step(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run)
	runstate.confirmOrphan(str(worktree), run, "worker-control", ["pid gone"])
	record = runstate.closeOrphan(str(worktree), run, "worker-control")
	assert record.get("steps") in (None, {})


def test_only_a_confirmed_orphan_can_be_closed(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	before = open(recordPath(worktree, run)).read()
	with pytest.raises(runstate.RecordError):
		runstate.closeOrphan(str(worktree), run, "worker-control")
	assert open(recordPath(worktree, run)).read() == before
	finish(worktree, run)
	with pytest.raises(runstate.RecordError):
		runstate.confirmOrphan(str(worktree), run, "worker-control", [])


# -- unreadable and unknown (T-1 at library level) ---------------------------------------------------


def corrupt(worktree, run, how):
	path = recordPath(worktree, run)
	record = json.loads(open(path).read())
	if how == "not-json":
		open(path, "w").write("{half")
		return
	if how == "schema-2":
		record["schema"] = 2
	elif how == "no-started-by":
		del record["started_by"]
	elif how == "no-launch":
		del record["launch"]
	elif how == "unknown-exit-reason":
		record["exit"] = {"ended_at": record["launch"]["launched_at"], "code": 0, "reason": "probably-fine"}
	elif how == "status-outside-enum":
		record["claude"]["status"] = "done-ish"
	elif how == "unknown-step-outcome":
		record["steps"]["verify"]["outcome"] = "passed"
	elif how == "naive-time":
		record["claude"]["ended_at"] = "2026-11-03T08:00:00"
	open(path, "w").write(json.dumps(record))


@pytest.mark.parametrize(
	"how",
	[
		"not-json",
		"schema-2",
		"no-started-by",
		"no-launch",
		"unknown-exit-reason",
		"status-outside-enum",
		"unknown-step-outcome",
		"naive-time",
	],
)
def test_an_unusable_record_reads_unknown_busy_and_refuses_the_merge(main, fake, how):
	worktree = addTopic(main)
	run = doneRun(worktree)
	assert statusOf(main, fake)["merge_allowed"] is True  # the control: the same run, intact
	corrupt(worktree, run, how)
	s = statusOf(main, fake)
	assert (s["phase"], s["busy"], s["merge_allowed"], s["resumable"]["ok"]) == ("unknown", True, False, False)
	assert s["error"] and s["evidence"][-1]["file"] == recordPath(worktree, run)


def test_the_writer_refuses_values_outside_their_enum_and_keeps_the_file(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	before = open(recordPath(worktree, run)).read()
	with pytest.raises(runstate.RecordError):
		change(worktree, run, exit={"ended_at": clock.isoNow(), "code": 0, "reason": "fine"})
	with pytest.raises(runstate.RecordError):
		runstate.create(str(worktree), {**BASE, "topic": TOPIC})  # no launch
	assert open(recordPath(worktree, run)).read() == before


def mergeCase(worktree, case):
	run = doneRun(worktree)
	if case == "no-claude-status":
		runstate.update(str(worktree), run, lambda r: r["claude"].update(status=None, outcome="no-status"))
	elif case == "status-blocked":
		runstate.update(str(worktree), run, lambda r: r["claude"].update(status="blocked"))
	elif case == "no-verify-step":
		runstate.update(str(worktree), run, lambda r: r["steps"].pop("verify"))
	elif case == "verify-fail":
		runstate.update(str(worktree), run, lambda r: r["steps"]["verify"].update(outcome="FAIL"))
	elif case == "verify-skipped-not-done":
		runstate.update(
			str(worktree),
			run,
			lambda r: r["steps"]["verify"].update(state="skipped", skip_reason="not-done", outcome=None),
		)
	elif case == "no-verifier-step":
		runstate.update(str(worktree), run, lambda r: r["steps"].pop("verifier"))
	elif case == "no-main-head":
		change(worktree, run, main_head_at_start=None)
	elif case == "origin-mode":
		change(worktree, run, integration_mode="origin")
	elif case == "no-integration-mode":
		change(worktree, run, integration_mode=None)
	elif case == "stopped":
		change(worktree, run, stop={"requested_at": clock.isoNow(), "by": "person"})
	elif case == "alert":
		guard = hookonly.stateDir(str(worktree), "agent-guard")
		os.makedirs(guard, exist_ok=True)
		open(os.path.join(guard, "ALERT"), "w").close()
	elif case == "orphan":
		runstate.confirmOrphan(str(worktree), run, "t", [])
	elif case == "ended-stopped":
		finish(worktree, run, reason="stopped", code=130)
	return run


@pytest.mark.parametrize(
	"case",
	[
		"no-claude-status",
		"status-blocked",
		"no-verify-step",
		"verify-fail",
		"verify-skipped-not-done",
		"no-verifier-step",
		"no-main-head",
		"origin-mode",
		"no-integration-mode",
		"stopped",
		"alert",
		"orphan",
		"ended-stopped",
	],
)
def test_every_missing_or_failed_fact_refuses_the_merge(main, fake, case):
	worktree = addTopic(main)
	if case in ("no-verify-step", "no-verifier-step"):
		verifySpecFile(main, {"checks": [], "verifier": {"usd": 1, "minutes": 5}})
	mergeCase(worktree, case)
	s = statusOf(main, fake)
	assert s["merge_allowed"] is False and s["merge_refusals"], s
	assert [b["reason"] for b in s["merge_blocks"]] == s["merge_refusals"]


def verifySpecFile(main, spec):
	(main / "plans" / "verify").mkdir(parents=True, exist_ok=True)
	(main / "plans" / "verify" / f"{TOPIC}.json").write_text(json.dumps(spec))


@pytest.mark.parametrize(
	"spec, verify, verifier, allowed",
	[
		(None, None, None, True),  # no spec: both steps skip, recorded or not (a run still finishing)
		({"checks": []}, None, None, False),  # a spec: verify must have run
		({"checks": []}, "PASS", None, True),  # no "verifier" key: the verifier skips
		({"checks": [], "verifier": None}, "PASS", None, False),  # the key, even empty: the verifier must PASS
		({"checks": [], "verifier": {"usd": 1}}, "PASS", "PASS", True),
		({"checks": [], "verifier": {"usd": 1}}, "PASS", "FAIL", False),
		(None, "FAIL", None, False),  # a failed check stays a failure though the spec is gone now
		("not-json", "PASS", "PASS", False),  # a spec that can't be read skips nothing and lets no verifier count
	],
)
def test_the_real_data_steps_pass_only_by_a_pass_or_by_the_spec_as_it_reads_now(
	main, fake, spec, verify, verifier, allowed
):
	worktree = addTopic(main)
	run = doneRun(worktree)
	if spec == "not-json":
		verifySpecFile(main, {})
		(main / "plans" / "verify" / f"{TOPIC}.json").write_text("{half")
	elif spec is not None:
		verifySpecFile(main, spec)
	named = {}
	for name, outcome in (("verify", verify), ("verifier", verifier)):
		if outcome:
			named[name] = {"state": "done", "attempt": 1, "outcome": outcome}
	steps(worktree, run, **named)
	assert statusOf(main, fake)["merge_allowed"] is allowed


@pytest.mark.parametrize(
	"text, allowed",
	[
		("STATUS: done\n\nall green", True),
		("\n\nSTATUS: done   \nmore", True),
		("STATUS: DONE\n", True),  # R2's upper case: a done STATUS in any case (runstate.workerStatus)
		("STATUS: Done\n", True),
		("STATUS:done\n", False),
		("STATUS:  done\n", False),
		("  STATUS: done\n", False),
		("PHASE: build\nSTATUS: done\n", False),
		("NOTE: x\nSTATUS: done\n", False),
		("STATUS: done\nSTATUS: done\n", False),
	],
)
def test_the_merge_needs_the_status_line_to_lead_the_header_exactly(main, fake, text, allowed):
	worktree = addTopic(main)
	run = doneRun(worktree)
	written = report_header.value(text, "STATUS")
	status = written if runstate.workerStatus(written) else None
	change(worktree, run, claude={**runstate.readRecord(str(worktree), run)[0]["claude"], "status": status})
	with open(os.path.join(runstate.runsDir(str(worktree)), f"result-{run}.json"), "w") as f:
		f.write(json.dumps({"result": text}))
	s = statusOf(main, fake)
	assert s["merge_allowed"] is allowed, s["merge_refusals"]


@pytest.mark.parametrize("value, word", [
	("done", "done"), ("DONE", "done"), ("Done", "done"), ("dOnE", "done"), ("blocked", "blocked"),
	("BLOCKED", "blocked"), ("Needs-Decision", "needs-decision"), ("NEEDS-DECISION", "needs-decision"),
	("done-ish", None), ("done and more", None), (" done", None), ("", None), (None, None), (1, None)])
def test_a_worker_status_reads_in_any_case_and_nothing_else(value, word):
	"""Every comparison of a worker's STATUS goes through workerStatus (R2: workers write either case)."""
	assert runstate.workerStatus(value) == word
	assert runstate.isWorkerDone(value) is (word == "done")


def test_a_record_with_a_done_status_in_any_case_is_valid_and_another_word_is_not(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	record = runstate.readRecord(str(worktree), run)[0]
	for written, problem in (("Done", False), ("DONE", False), ("done", False), ("done-ish", True)):
		found = runstate.validate({**record, "claude": {**record["claude"], "status": written}})
		assert any("claude.status" in p for p in found) is problem, (written, found)


def test_a_complete_done_run_allows_the_merge(main, fake):
	worktree = addTopic(main)
	doneRun(worktree)
	s = statusOf(main, fake)
	assert (s["merge_allowed"], s["merge_refusals"]) == (True, [])


def test_no_record_refuses_the_merge(main, fake):
	addTopic(main)
	assert statusOf(main, fake)["merge_allowed"] is False


# -- reading is read-only (T-8) ----------------------------------------------------------------------


def snapshot(top):
	out = {}
	for folder, _dirs, names in os.walk(top):
		for name in names:
			path = os.path.join(folder, name)
			out[path] = (os.stat(path).st_mtime_ns, open(path, "rb").read())
	return out


def test_status_is_read_only(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	legacyTree(addTopic(main, "old-topic"), fake, eleventhOfNovember=True)
	before = snapshot(str(main.parent))
	modes = {}
	for folder, dirs, _names in os.walk(str(main.parent)):
		for name in dirs:
			path = os.path.join(folder, name)
			if not os.path.islink(path):
				modes[path] = os.stat(path).st_mode
				os.chmod(path, modes[path] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
	try:
		for topic in (TOPIC, "old-topic", "nobody"):
			runstate.status(str(main), topic, clock=fake.now, bootId=BOOT, pidAlive=lambda p, t, n: False)
			runstate.agent(runstate.status(str(main), topic, clock=fake.now, bootId=BOOT))
	finally:
		for path, mode in modes.items():
			os.chmod(path, mode)
	assert snapshot(str(main.parent)) == before
	assert run == 1


# -- run numbers and writers -------------------------------------------------------------------------


def test_the_next_run_counts_today_s_est_files(main, fake):
	worktree = addTopic(main)
	os.makedirs(runstate.runsDir(str(worktree)))
	open(runstate.legacyPath(str(worktree), "est", 3, ".json"), "w").write("{}")
	assert runstate.nextRun(str(worktree)) == 4
	assert launch(worktree) == 4
	assert launch(worktree) == 5


def test_create_skips_a_number_whose_files_appeared_meanwhile(main, fake, monkeypatch):
	worktree = addTopic(main)
	real = runstate.nextRun
	calls = []

	def racing(root):
		calls.append(1)
		if len(calls) == 1:  # another writer takes run 1 between the count and the lock
			open(runstate.legacyPath(root, "start", 1), "w").write(clock.isoNow())
			return 1
		return real(root)

	os.makedirs(runstate.runsDir(str(worktree)))
	monkeypatch.setattr(runstate, "nextRun", racing)
	assert launch(worktree) == 2
	assert not os.path.exists(recordPath(worktree, 1))


def test_the_record_lock_is_its_file_plus_lock_and_heartbeats_take_none(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	with statefile.locked(str(worktree), runstate.RECORD, run) as path:
		assert path == recordPath(worktree, run) + ".lock"
	with pytest.raises(statefile.StateError):
		statefile.lockPath(str(worktree), runstate.HEARTBEAT, run)


# -- today's files (legacy adapter) ------------------------------------------------------------------


def legacyFile(worktree, name, content, ageSeconds, now):
	path = os.path.join(runstate.runsDir(str(worktree)), name)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		f.write(content if isinstance(content, str) else json.dumps(content))
	stamp = (now - timedelta(seconds=ageSeconds)).timestamp()
	os.utime(path, (stamp, stamp))
	return path


def resultOf(first, cost=3.0):
	return {"type": "result", "is_error": False, "result": f"{first}\n\nwhat was done", "total_cost_usd": cost}


def legacyTree(worktree, fake, eleventhOfNovember=False, run=1, startedAgo=4 * 3600, resultAgo=None, extra=()):
	"""Today's files of one run, in the shapes run-worker.sh and worker-control.py write (from a real
	run). eleventhOfNovember: the case of a result-<run>.json and no
	exit-<run> (the wrapper was gone)."""
	now = fake.now()
	started = now - timedelta(seconds=startedAgo)
	legacyFile(
		worktree,
		f"est-{run}.json",
		{
			"minutes": 219,
			"usd": 9.12,
			"cap": 11.4,
			"cap_minutes": 274,
			"raw_minutes": 120.0,
			"raw_usd": 14.0,
			"hook_wait_minutes": 120.8,
		},
		startedAgo,
		now,
	)
	legacyFile(worktree, f"start-{run}", started.isoformat(timespec="seconds") + "\n", startedAgo, now)
	legacyFile(worktree, f"main-{run}", "380998f6844fba82a902db96339aadda3d548bcb\n", startedAgo, now)
	legacyFile(worktree, f"auth-{run}.log", "Logged in\n", startedAgo, now)
	legacyFile(worktree, f"stderr-{run}.log", "", startedAgo, now)
	legacyFile(worktree, f"phase-{run}-1.json", resultOf("PHASE: tests-done"), startedAgo - 60, now)
	legacyFile(worktree, f"compact-{run}-1.json", resultOf("compacted"), startedAgo - 90, now)
	if eleventhOfNovember:
		legacyFile(worktree, f"result-{run}.json", resultOf("STATUS: done"), WRAPUP + 300, now)
	elif resultAgo is not None:
		legacyFile(worktree, f"result-{run}.json", resultOf("STATUS: done"), resultAgo, now)
	for name, content, age in extra:
		legacyFile(worktree, name, content, age, now)
	return worktree


def workerState(main, topic, run, pid=4242):
	folder = hookonly.stateDir(str(main), "workers")
	os.makedirs(folder, exist_ok=True)
	with open(os.path.join(folder, f"{topic}.json"), "w") as f:
		f.write(json.dumps({"pid": pid, "session_id": "f4467c1a-ff29-420d-8174-31c15b576606", "run": run}))


def test_the_eleventh_of_november_case_reads_as_an_orphan(main, fake):
	legacyTree(addTopic(main), fake, eleventhOfNovember=True)
	workerState(main, TOPIC, 1)
	s = statusOf(main, fake)
	assert (s["source"], s["phase"], s["orphan_of"], s["confidence"]) == ("legacy", "orphan", "finishing", "suspected")
	assert "legacy-files" in s["warnings"] and s["claude"]["status"] == "done"
	assert s["busy"] is True and s["merge_allowed"] is False
	gone = statusOf(main, fake, pidAlive=lambda process, topic, run: process["wrapper_pid"] != 4242)
	assert (gone["phase"], gone["confidence"], gone["pid_checked"], gone["busy"]) == (
		"orphan",
		"confirmed",
		True,
		False,
	)
	alive = statusOf(main, fake, pidAlive=lambda process, topic, run: True)
	assert (alive["phase"], alive["warnings"][-1]) == ("finishing", "heartbeat-stale-process-alive")


def test_a_legacy_run_with_a_fresh_result_reads_finishing(main, fake):
	legacyTree(addTopic(main), fake, resultAgo=WRAPUP - 60)
	assert statusOf(main, fake)["phase"] == "finishing"


def test_a_legacy_phase_turn_without_exit_reads_running(main, fake):
	legacyTree(addTopic(main), fake, extra=[("result-1.json", resultOf("PHASE: dev-done"), 30)])
	assert statusOf(main, fake)["phase"] == "running"


def test_a_legacy_worker_past_its_minute_limit_reads_as_orphan(main, fake):
	worktree = legacyTree(addTopic(main), fake, startedAgo=274 * 60 + WRAPUP - 60)
	assert statusOf(main, fake)["phase"] == "running"
	fake.advance(120)
	s = statusOf(main, fake)
	assert (s["phase"], s["orphan_of"]) == ("orphan", "running")
	assert worktree


def test_a_legacy_launch_without_start_reads_launching_then_orphan(main, fake):
	worktree = addTopic(main)
	legacyFile(worktree, "est-1.json", {"minutes": 5, "usd": 0.5, "cap": 0.6, "cap_minutes": 7}, 0, fake.now())
	assert statusOf(main, fake)["phase"] == "launching"
	fake.advance(THRESHOLD + 1)
	assert (statusOf(main, fake)["phase"], statusOf(main, fake)["orphan_of"]) == ("orphan", "launching")


def test_a_legacy_run_without_exit_but_a_later_run_has_ended(main, fake):
	worktree = legacyTree(addTopic(main), fake, eleventhOfNovember=True)
	legacyTree(worktree, fake, run=2, startedAgo=600, resultAgo=60, extra=[("exit-2", "0 " + clock.isoNow(), 0)])
	first = statusOf(main, fake, run=1)
	assert (first["phase"], first["outcome"]["reason"], first["outcome"]["code"]) == ("ended", "superseded", None)
	latest = statusOf(main, fake)
	assert (latest["run"], latest["phase"], latest["outcome"]["reason"]) == (2, "ended", "completed")


@pytest.mark.parametrize(
	"exitText, extra, reason",
	[
		("90 2026-11-03T07:00:00+08:00", [], "not-logged-in"),
		("130 2026-11-03T07:00:00+08:00", [("stopped-1", "2026-11-03T06:59:00+0800", 60)], "stopped"),
		("0 2026-11-03T07:00:00+08:00", [("result-1.json", resultOf("STATUS: done"), 60)], "completed"),
	],
)
def test_legacy_exit_files_map_to_exit_reasons(main, fake, exitText, extra, reason):
	legacyTree(addTopic(main), fake, extra=extra + [("exit-1", exitText, 0)])
	s = statusOf(main, fake)
	assert (s["phase"], s["outcome"]["reason"]) == ("ended", reason)


def test_a_legacy_config_error_before_the_start_maps_to_config_error(main, fake):
	worktree = addTopic(main)
	legacyFile(worktree, "est-1.json", {"minutes": 5, "usd": 0.5, "cap": 0.6, "cap_minutes": 7}, 10, fake.now())
	legacyFile(worktree, "start-1", clock.isoNow(), 10, fake.now())
	legacyFile(worktree, "exit-1", "1 " + clock.isoNow(), 0, fake.now())
	assert statusOf(main, fake)["outcome"]["reason"] == "config-error"


def test_a_legacy_blocked_run_with_an_automatic_resume_decided_is_busy(main, fake):
	legacyTree(
		addTopic(main),
		fake,
		extra=[
			("result-1.json", resultOf("STATUS: blocked"), 60),
			("auto-resume-1.json", {"minutes": 20, "usd": 2.0}, 60),
			("auto-1.json", {"run": 1, "decision": "resume", "minutes": 20, "usd": 2.0}, 30),
			("exit-1", "0 " + clock.isoNow(), 0),
		],
	)
	s = statusOf(main, fake)
	assert (s["phase"], s["busy"], s["next"]["decision"], s["claude"]["status"]) == ("ended", True, "resume", "blocked")


def test_a_legacy_refused_merge_offers_the_person_s_approval_until_an_approval_runs(main, fake):
	worktree = legacyTree(
		addTopic(main),
		fake,
		extra=[
			("result-1.json", resultOf("STATUS: done"), 120),
			("verify-1.json", {"run": 1, "passed": True, "checks": []}, 100),
			("merge-1.json", {"decision": "refused", "reason": "condition 6: no APPROVE"}, 90),
			("exit-1", "0 " + clock.isoNow(), 0),
		],
	)
	s = statusOf(main, fake)
	assert s["merge"]["reason"] == "condition 6: no APPROVE" and s["approving"] is None
	view = runstate.human(s, "en")
	assert view["todo"]["button"] == "merge" and view["todo"]["command"] == f"wt merge {TOPIC}"
	started = fake.now() - timedelta(seconds=30)
	legacyFile(
		worktree,
		"approve-1.json",
		{"run": 1, "pid": 5, "started": started.strftime("%Y-%m-%dT%H:%M:%S%z"), "started_epoch": started.timestamp()},
		30,
		fake.now(),
	)
	assert statusOf(main, fake)["approving"] == {"run": 1, "since": started.strftime("%H:%M")}


def test_a_legacy_failed_verify_refuses_the_merge(main, fake):
	legacyTree(
		addTopic(main),
		fake,
		extra=[
			("result-1.json", resultOf("STATUS: done"), 120),
			("verify-1.json", {"run": 1, "passed": False, "checks": [{"name": "x", "ok": False}]}, 100),
			("exit-1", "0 " + clock.isoNow(), 0),
		],
	)
	s = statusOf(main, fake)
	assert "verify step done FAIL" in s["merge_refusals"]


def test_a_legacy_run_without_a_verify_file_refuses_when_plans_are_hidden(main, fake):
	legacyTree(
		addTopic(main),
		fake,
		extra=[("result-1.json", resultOf("STATUS: done"), 120), ("exit-1", "0 " + clock.isoNow(), 0)],
	)
	shutil.rmtree(main / "plans")
	s = statusOf(main, fake)
	assert "verify step missing" in s["merge_refusals"] and s["resumable"]["ok"] is None


def test_an_archived_topic_reads_from_its_archive(main, fake):
	archived = runstate.archiveOf(str(main), "gone-topic")
	legacyTree(
		archived,
		fake,
		extra=[
			("result-1.json", resultOf("STATUS: done"), 120),
			("merge-1.json", {"decision": "merged", "into": "main", "commit": "65f78020b977abc"}, 60),
			("exit-1", "0 " + clock.isoNow(), 0),
		],
	)
	s = statusOf(main, fake, topic="gone-topic")
	assert (s["archived"], s["phase"], s["outcome"]["merge"], s["outcome"]["commit"]) == (
		True,
		"ended",
		"merged",
		"65f78020b977abc",
	)


# -- the same story from a record and from today's files (T-9) ---------------------------------------


def comparable(s):
	return {
		"phase": s["phase"],
		"busy": s["busy"],
		"claude": (s["claude"] or {}).get("status"),
		"reason": (s["outcome"] or {}).get("reason"),
		"merge": (s["outcome"] or {}).get("merge"),
		"held_merge": s["merge"] is not None,
		"next": (s["next"] or {}).get("decision"),
		"flow": (s["flow"] or {}).get("kind"),
		"resume": (s["flow"] or {}).get("resume"),
		"merge_allowed": s["merge_allowed"],
	}


def story(main, fake, name):
	"""One run told twice: topic <name>-record through runstate's writer, <name>-legacy as today's files."""
	record, legacy = addTopic(main, f"{name}-record"), addTopic(main, f"{name}-legacy")
	for worktree in (record, legacy):
		os.makedirs(hookonly.stateDir(str(worktree), "flow"), exist_ok=True)
		open(hookonly.stateDir(str(worktree), "flow-mode"), "w").write("local\n")
	now = fake.now()
	run = launch(record, topic=f"{name}-record")
	legacyFile(legacy, "est-1.json", {"minutes": 40, "usd": 6.0, "cap": 7.8, "cap_minutes": 52}, 0, now)
	if name == "launched":
		return record, legacy
	start(record, run)
	legacyFile(legacy, "start-1", clock.isoNow(), 0, now)
	legacyFile(legacy, "main-1", "65f7802\n", 0, now)
	if name == "running":
		return record, legacy
	if name == "scripted":
		change(record, run, flow="scripted")
		for worktree in (record, legacy):
			folder = hookonly.stateDir(str(worktree), "flow")
			open(os.path.join(folder, "scripted"), "w").write("{}\n")
			open(os.path.join(folder, "state.json"), "w").write(
				json.dumps({"schema": 1, "phase": "running", "seq": 3, "waiting": {"a1": {"role": "qa"}}})
			)
		return record, legacy
	if name == "stopped":
		change(record, run, stop={"requested_at": clock.isoNow(), "by": "person"})
		finish(record, run, reason="stopped", code=130)
		legacyFile(legacy, "stopped-1", clock.isoNow(), 0, now)
		legacyFile(legacy, "exit-1", "130 " + clock.isoNow(), 0, now)
		return record, legacy
	status = "blocked" if name == "blocked" else "done"
	claudeEnded(record, run, status=status)
	legacyFile(legacy, "result-1.json", resultOf(f"STATUS: {status}"), 0, now)
	skipped = {
		"verify": {"state": "skipped", "skip_reason": "no-spec"},
		"verifier": {"state": "skipped", "skip_reason": "no-spec"},
	}
	if name == "blocked":
		steps(record, run, **skipped, next={"state": "done", "outcome": "resume"})
		change(record, run, dispatch_stop={"minutes": 20, "usd": 2.0})
		legacyFile(legacy, "auto-resume-1.json", {"minutes": 20, "usd": 2.0}, 0, now)
		legacyFile(legacy, "auto-1.json", {"run": 1, "decision": "resume"}, 0, now)
	elif name == "merged":
		steps(
			record,
			run,
			**skipped,
			merge={"state": "done", "outcome": "merged", "commit": "abc1234", "ended_at": clock.isoNow()},
		)
		legacyFile(legacy, "merge-1.json", {"decision": "merged", "commit": "abc1234"}, 0, now)
	elif name == "finishing":
		steps(record, run, **skipped)
	elif name == "refused":
		steps(
			record,
			run,
			**skipped,
			merge={
				"state": "done",
				"outcome": "refused",
				"reason": "no APPROVE",
				"flags": {"merge_run": False, "fix_run": False},
				"ended_at": clock.isoNow(),
			},
		)
		legacyFile(legacy, "merge-1.json", {"decision": "refused", "reason": "no APPROVE"}, 0, now)
	if name != "finishing":
		finish(record, run)
		legacyFile(legacy, "exit-1", "0 " + clock.isoNow(), 0, now)
	return record, legacy


@pytest.mark.parametrize(
	"name", ["launched", "running", "scripted", "stopped", "finishing", "blocked", "merged", "refused"]
)
def test_legacy_and_record_give_same_status(main, fake, name):
	story(main, fake, name)
	fake.advance(1)
	record = statusOf(main, fake, topic=f"{name}-record")
	legacy = statusOf(main, fake, topic=f"{name}-legacy")
	assert (record["source"], legacy["source"]) == ("record", "legacy")
	assert comparable(record) == comparable(legacy)


# -- the scripted flow (T-16) ------------------------------------------------------------------------


def scriptedFlow(worktree, state, marker=True, log=()):
	folder = hookonly.stateDir(str(worktree), "flow")
	os.makedirs(folder, exist_ok=True)
	if marker:
		open(os.path.join(folder, "scripted"), "w").write("{}\n")
	open(os.path.join(folder, "state.json"), "w").write(json.dumps(state))
	open(os.path.join(folder, "next.md"), "w").write("# Next step\n")
	open(os.path.join(folder, "log.jsonl"), "w").write("".join(json.dumps(line) + "\n" for line in log))


def scriptedRun(main, fake, state, **kw):
	worktree = addTopic(main)
	run = launch(worktree, flow="scripted", prompt_kind="scripted")
	start(worktree, run)
	scriptedFlow(worktree, state, **kw)
	return worktree, run


def test_a_scripted_run_reports_its_step_and_the_agents_it_waits_for(main, fake):
	scriptedRun(
		main,
		fake,
		{
			"topic": TOPIC,
			"class": "test-first",
			"phase": "running",
			"seq": 14,
			"waiting": {"a7c179da3e66d620e": {"role": "qa", "purpose": "tests"}},
		},
		log=[{"t": "2026-11-03T08:00:00", "seq": 14, "event": "dispatched", "do": "wait", "detail": {"role": "qa"}}],
	)
	s = statusOf(main, fake)
	assert s["flow"]["waiting"] == [{"agent_id": "a7c179da3e66d620e", "agent_type": "qa"}]
	assert (s["flow"]["seq"], s["flow"]["next"], s["flow"]["resume"]) == (14, "wait", "scripted")
	assert "flow-state-legacy" in s["warnings"]  # flow-next.py writes no schema yet (Q8)
	assert "14" in runstate.human(s, "en")["line"] and "qa" in runstate.human(s, "en")["line"]


def test_a_flow_state_with_its_schema_reads_without_a_warning(main, fake):
	scriptedRun(main, fake, {"schema": 1, "phase": "wrapup", "seq": 30})
	s = statusOf(main, fake)
	assert (s["flow"]["phase"], s["flow"]["resume"]) == ("wrapup", "scripted") and "flow-state-legacy" not in s[
		"warnings"
	]


@pytest.mark.parametrize("state", [{"schema": 2, "phase": "running"}, {"schema": 1, "phase": "paused"}])
def test_an_unknown_flow_state_is_reported_and_never_resumed_as_scripted(main, fake, state):
	scriptedRun(main, fake, state)
	flow = statusOf(main, fake)["flow"]
	assert (flow["kind"], flow["phase"], flow["resume"]) == ("scripted", None, None) and flow["error"]


def test_scripted_stopped_flow_reports_retry_decides(main, fake):
	worktree, run = scriptedRun(
		main,
		fake,
		{"phase": "stopped", "seq": 22, "waiting": {}},
		log=[{"t": "x", "seq": 22, "event": "report", "do": "status", "detail": {"status": "needs-decision"}}],
	)
	claudeEnded(worktree, run, status="needs-decision")
	finish(worktree, run)
	(main / "plans" / "worker").mkdir()
	(main / "plans" / "worker" / f"{TOPIC}.json").write_text("{}")
	s = statusOf(main, fake)
	assert (s["flow"]["resume"], s["flow"]["stop_reason"], s["resumable"]["prompt_kind"]) == (
		"retry-decides",
		"report",
		None,
	)
	assert runstate.human(s, "en")["todo"]["button"] == "resume"


def test_record_keeps_flow_kind_per_run(main, fake):
	worktree, run = scriptedRun(main, fake, {"phase": "running", "seq": 3})
	finish(worktree, run, reason="stopped", code=130)
	second = launch(worktree, flow="written")
	start(worktree, second)
	assert statusOf(main, fake, run=run)["flow"]["kind"] == "scripted"
	assert statusOf(main, fake)["flow"] == {"kind": "written"}


def test_a_flow_switched_off_resumes_written(main, fake):
	scriptedRun(main, fake, {"phase": "running", "seq": 3}, marker=False)
	flow = statusOf(main, fake)["flow"]
	assert (flow["phase"], flow["resume"]) == ("off", "written")


# -- resumable, held ---------------------------------------------------------------------------------


def test_done_without_a_newer_estimate_is_not_resumable_until_one_is_written(main, fake):
	(main / "plans" / "worker").mkdir()
	estimate = main / "plans" / "worker" / f"{TOPIC}.json"
	estimate.write_text("{}")
	old = (START - timedelta(hours=1)).timestamp()
	os.utime(estimate, (old, old))
	worktree = addTopic(main)
	run = doneRun(worktree)
	finish(worktree, run)
	s = statusOf(main, fake)
	assert s["resumable"]["ok"] is False and "new estimate" in s["resumable"]["why"]
	later = (START + timedelta(minutes=5)).timestamp()
	os.utime(estimate, (later, later))
	assert statusOf(main, fake)["resumable"] == {"ok": True, "why": None, "prompt_kind": "resume", "code": None}


@pytest.mark.parametrize("files, state", [(("ALERT",), "alert"), (("HOLD",), "hold"), (("ALERT", "HOLD"), "hold")])
def test_an_alert_counts_as_held(main, fake, files, state):
	worktree = addTopic(main)
	guard = hookonly.stateDir(str(worktree), "agent-guard")
	os.makedirs(guard)
	for name in files:
		open(os.path.join(guard, name), "w").close()
	s = statusOf(main, fake)
	assert s["held"]["state"] == state
	view = runstate.human(s, "en")
	assert state.upper() in view["line"] and view["todo"]["button"] is None


# -- renderers ---------------------------------------------------------------------------------------


def test_every_language_has_the_same_messages():
	tables = LIMITS["status_text"]
	assert "en" in tables and "zh-TW" in tables
	assert all(set(t) == set(tables["en"]) for t in tables.values())


def everyPhase(main, fake):
	"""One status per rendering branch."""
	out = []
	worktree = addTopic(main, "p-none")
	out.append(statusOf(main, fake, topic="p-none"))
	worktree = addTopic(main, "p-launch")
	launch(worktree, topic="p-launch")
	out.append(statusOf(main, fake, topic="p-launch"))
	worktree = addTopic(main, "p-run")
	run = launch(worktree, topic="p-run")
	start(worktree, run)
	out.append(statusOf(main, fake, topic="p-run"))
	worktree = addTopic(main, "p-finish")
	run = launch(worktree, topic="p-finish")
	start(worktree, run)
	claudeEnded(worktree, run)
	out.append(statusOf(main, fake, topic="p-finish"))
	steps(worktree, run, merge={"state": "running", "started_at": clock.isoNow()})
	out.append(statusOf(main, fake, topic="p-finish"))
	finish(worktree, run)
	out.append(statusOf(main, fake, topic="p-finish"))
	worktree = addTopic(main, "p-blocked")
	run = launch(worktree, topic="p-blocked")
	start(worktree, run)
	claudeEnded(worktree, run, status="needs-decision")
	finish(worktree, run)
	out.append(statusOf(main, fake, topic="p-blocked"))
	worktree = addTopic(main, "p-merged")
	run = launch(worktree, topic="p-merged")
	start(worktree, run)
	claudeEnded(worktree, run)
	steps(worktree, run, merge={"state": "done", "outcome": "merged", "commit": "abc1234def"})
	finish(worktree, run)
	out.append(statusOf(main, fake, topic="p-merged"))
	worktree = addTopic(main, "p-stopped")
	run = launch(worktree, topic="p-stopped")
	start(worktree, run)
	finish(worktree, run, reason="stopped", code=130)
	out.append(statusOf(main, fake, topic="p-stopped"))
	worktree = addTopic(main, "p-boot")
	run = launch(worktree, topic="p-boot")
	start(worktree, run, boot="old")
	out.append(statusOf(main, fake, topic="p-boot"))
	worktree = addTopic(main, "p-exited")
	run = launch(worktree, topic="p-exited")
	start(worktree, run)
	runstate.update(str(worktree), run, lambda r: r["process"].update(exited_at=clock.isoNow()))
	out.append(statusOf(main, fake, topic="p-exited"))
	out.append(statusOf(main, fake, topic="p-run", pidAlive=lambda p, t, n: False))
	worktree = addTopic(main, "p-unknown")
	run = launch(worktree, topic="p-unknown")
	open(recordPath(worktree, run), "w").write("{")
	out.append(statusOf(main, fake, topic="p-unknown"))
	fake.advance(THRESHOLD * 2)
	out.append(statusOf(main, fake, topic="p-run"))
	return out


@pytest.mark.parametrize("lang", LANGS)
def test_human_renders_every_phase_in_every_language(main, fake, lang):
	statuses = everyPhase(main, fake)
	phases = {s["phase"] for s in statuses}
	assert phases == {"none", "launching", "running", "finishing", "ended", "orphan", "unknown"}
	for s in statuses:
		view = runstate.human(s, lang)
		assert view["line"].startswith(s["topic"]) and "{" not in view["line"], view
		assert view["todo"] is None or view["todo"]["button"] in (None, "launch", "resume", "stop", "merge")
	byTopic = {(s["topic"], s["phase"], s["confidence"]): runstate.human(s, lang) for s in statuses}
	assert byTopic[("p-run", "running", None)]["todo"]["button"] == "stop"
	assert byTopic[("p-blocked", "ended", None)]["todo"]["button"] == "resume"
	# No estimate in this checkout: an orphan's to-do waits for the planning session, no button.
	noResume = LIMITS["status_text"][lang]["todo_orphan_no_resume"].format(topic="p-boot")
	assert byTopic[("p-boot", "orphan", "suspected")]["todo"] == {"text": noResume, "command": None, "button": None}
	assert byTopic[("p-unknown", "unknown", None)]["todo"]["button"] is None


@pytest.mark.parametrize("lang", LANGS)
def test_the_orphan_to_do_offers_wt_resume_which_checks_closes_and_resumes(main, fake, lang):
	"""Migration step 4 (Q3): `wt resume` checks a suspected orphan's wrapper outside the sandbox, closes the run
	when it is gone (never a merge) and resumes, and refuses while it runs or can't be checked; a confirmed one it
	closes and resumes. The to-do offers that resume button, and none while no estimate allows a resume."""
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	fake.advance(THRESHOLD + 60)
	words = LIMITS["status_text"][lang]
	resume = {"command": f"wt resume {TOPIC}", "button": "resume"}
	noEstimate = runstate.human(statusOf(main, fake), lang)["todo"]
	assert noEstimate == {"text": words["todo_orphan_no_resume"].format(topic=TOPIC), "command": None, "button": None}
	estimateBefore(main, START)
	suspected = runstate.human(statusOf(main, fake), lang)["todo"]
	assert suspected == {"text": words["todo_orphan_suspected"].format(topic=TOPIC), **resume}
	runstate.confirmOrphan(str(worktree), run, "test", {"wrapper_pid": "gone"})
	confirmed = runstate.human(statusOf(main, fake), lang)["todo"]
	assert confirmed == {"text": words["todo_orphan_confirmed"].format(topic=TOPIC), **resume}
	english = LIMITS["status_text"]["en"]
	assert f"wt status {TOPIC}" in english["todo_orphan_suspected"].format(topic=TOPIC)
	for key in ("todo_orphan_suspected", "todo_orphan_confirmed"):
		assert "wt resume" in english[key] and "closes this run (never merged) and resumes" in english[key]
		assert "does not close" not in english[key] and "planning session" not in english[key]
	assert (
		"refuses" in english["todo_orphan_suspected"] and "checks its process first" in english["todo_orphan_suspected"]
	)
	assert "new estimate" in english["todo_orphan_no_resume"]


def estimateBefore(main, moment):
	"""plans/worker/<topic>.json written before moment (mtime), as the planning session leaves it."""
	path = main / "plans" / "worker" / f"{TOPIC}.json"
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps({"minutes": 20, "est_usd": 2}))
	stamp = (moment - timedelta(seconds=60)).timestamp()
	os.utime(path, (stamp, stamp))


def test_an_orphan_s_resumable_says_what_wt_resume_does_now(main, fake):
	"""Migration step 4 (Q3): a suspected orphan can't be judged without a pid check (ok None): wt resume checks
	its wrapper first, closes and resumes only when it is gone; a confirmed one is resumable, wt resume closes it
	first (orphan-closed, never merged); after STATUS done without a newer estimate it is refused like any run."""
	estimateBefore(main, fake.now())
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	fake.advance(THRESHOLD + 60)
	suspected = statusOf(main, fake)
	assert suspected["resumable"] == {
		"ok": None,
		"why": "run 1 may have died: wt resume checks its wrapper process first, closes run 1 and starts run 2 only "
		f"if it is gone, and refuses while it runs or can't be checked (wt status {TOPIC} shows the check)",
		"prompt_kind": "resume",
		"code": "orphan-suspected",
	}
	text = runstate.agent(suspected)
	assert report_header.value(text, "RESUMABLE") == "unknown"
	runstate.confirmOrphan(str(worktree), run, "test", {"wrapper_pid": "gone"})
	confirmed = statusOf(main, fake)
	assert confirmed["resumable"] == {
		"ok": True,
		"why": "its process is gone: wt resume closes run 1 (orphan-closed: its unfinished wrap-up abandoned, never "
		"merged), then starts run 2",
		"prompt_kind": "resume",
		"code": "orphan-confirmed",
	}
	text = runstate.agent(confirmed)
	assert report_header.value(text, "RESUMABLE") == "yes" and "wt resume: its process is gone" in text


def test_a_done_orphan_without_a_newer_estimate_is_not_resumable(main, fake):
	estimateBefore(main, fake.now())
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run)
	fake.advance(WRAPUP + THRESHOLD)
	runstate.confirmOrphan(str(worktree), run, "test", {"wrapper_pid": "gone"})
	s = statusOf(main, fake)
	assert (s["phase"], s["confidence"]) == ("orphan", "confirmed")
	assert s["resumable"]["ok"] is False and "new estimate" in s["resumable"]["why"]


def test_the_line_differs_by_language_and_defaults_to_the_setting(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	s = statusOf(main, fake)
	assert runstate.human(s)["line"] == runstate.human(s, LIMITS["status_language"])["line"]
	assert runstate.human(s, "en")["line"] != runstate.human(s, "zh-TW")["line"]
	with pytest.raises(runstate.homeconfig.ConfigError):
		runstate.human(s, "xx")


def test_a_local_override_sets_the_language(main, fake, tmp_path, monkeypatch):
	config = config_fixtures.copyConfig(os.path.join(os.path.dirname(HOOKS), "config"), tmp_path / "config")
	(config / "local.json").write_text(json.dumps({"budget": {"run_state": {"status_language": "zh-TW"}}}))
	config_fixtures.useConfig(monkeypatch, config)
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	s = statusOf(main, fake)
	assert runstate.human(s)["line"] == runstate.human(s, "zh-TW")["line"]


def test_the_agent_view_is_a_header_plus_evidence(main, fake):
	legacyTree(addTopic(main), fake, eleventhOfNovember=True)
	text = runstate.agent(statusOf(main, fake))
	header = report_header.parse(text)
	assert {
		k: v[0]
		for k, v in header.items()
		if k in ("PHASE", "ORPHAN-OF", "CONFIDENCE", "PID-CHECKED", "BUSY", "MERGE-ALLOWED", "SOURCE")
	} == {
		"PHASE": "orphan",
		"ORPHAN-OF": "finishing",
		"CONFIDENCE": "suspected",
		"PID-CHECKED": "no",
		"BUSY": "yes",
		"MERGE-ALLOWED": "no",
		"SOURCE": "legacy",
	}
	# No estimate here: nothing to resume until the planning session writes one (no command).
	assert header["TODO"][0] == LIMITS["status_text"]["en"]["todo_orphan_no_resume"].format(topic=TOPIC)
	evidence = text.split("evidence:\n", 1)[1]
	assert "result-1.json mtime" in evidence and "threshold" in evidence


# -- detail: the evidence files, for the display readers (migration step 3) ---------------------------


def test_detail_reads_each_evidence_file_absent_or_unreadable_never_zero(main, fake):
	now = fake.now()
	ended = f"0 {now.isoformat(timespec='seconds')}\n"
	verify = {"passed": False, "checks": [{"name": "rows", "ok": False, "why": "0 rows"}]}
	legacyTree(
		addTopic(main),
		fake,
		resultAgo=30,
		extra=(("exit-1", ended, 10), ("verify-1.json", verify, 5), ("verifier-1.json", "{", 5)),
	)
	s = statusOf(main, fake)
	found = runstate.detail(s)
	assert found["result"]["text"].startswith("STATUS: done") and found["result"]["num_turns"] is None
	assert found["verify"]["failed"] == [{"name": "rows", "why": "0 rows"}]
	assert found["verifier"]["error"].startswith("not JSON") and found["merge"] is None and found["next"] is None
	assert [r["run"] for r in found["results"]] == [1] and "text" not in found["results"][0]
	assert "text" not in runstate.detail(s, text=False)["result"]
	text = runstate.agent(s, found)
	assert "verify-1.json: FAILED: rows: 0 rows" in text and "verifier-1.json: unreadable (not JSON" in text
	assert "- merge: absent" in text and "- run 1: no terminal_reason · ? turns · $3.00" in text
	assert text.rstrip().endswith("what was done")
	assert "detail:" not in runstate.agent(s)


def test_detail_of_a_record_backed_run_reads_the_same_evidence_files(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	legacyFile(worktree, f"result-{run}.json", resultOf("STATUS: done"), 0, fake.now())
	legacyFile(worktree, f"merge-{run}.json", {"decision": "refused", "reason": "review .claude/"}, 0, fake.now())
	found = runstate.detail(statusOf(main, fake))
	assert found["merge"]["decision"] == "refused" and found["result"]["total_cost_usd"] == 3.0
	assert "refused: review .claude/" in runstate.agent(statusOf(main, fake), found)


def test_detail_without_a_run_is_empty(main, fake):
	addTopic(main)
	found = runstate.detail(statusOf(main, fake))
	assert found["run"] is None and found["results"] == [] and found["result"] is None


# -- run-status.py -----------------------------------------------------------------------------------


def cli(argv, fake, sleeps=None):
	out = io.StringIO()

	def sleep(seconds):
		assert seconds == LIMITS["status_wait_poll_seconds"]
		sleeps.append(seconds)
		fake.advance(seconds)

	code = loadCli().main(argv, clock=fake.now, sleep=sleep, out=out)
	return code, out.getvalue()


def test_the_cli_prints_json_human_and_agent_views(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run, boot=runstate.readBootId())  # the CLI reads this machine's boot id
	code, out = cli(["--main", str(main), "--topic", TOPIC, "--json"], fake)
	assert code == 0 and json.loads(out)["phase"] == "running"
	code, out = cli(["--main", str(main), "--topic", TOPIC, "--human", "--lang", "zh-TW"], fake)
	assert code == 0 and "執行中" in out and f"wt stop {TOPIC}" in out
	code, out = cli(["--main", str(main), "--topic", TOPIC], fake)
	assert code == 0 and report_header.value(out, "PHASE") in ("running", "orphan")


def test_the_cli_adds_the_detail_only_when_asked(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	legacyFile(worktree, f"result-{run}.json", resultOf("STATUS: done"), 0, fake.now())
	code, out = cli(["--main", str(main), "--topic", TOPIC, "--json", "--detail"], fake)
	assert code == 0 and json.loads(out)["detail"]["result"]["text"].startswith("STATUS: done")
	assert "detail" not in json.loads(cli(["--main", str(main), "--topic", TOPIC, "--json"], fake)[1])
	assert "\ndetail:\n" in cli(["--main", str(main), "--topic", TOPIC, "--detail"], fake)[1]
	assert "\ndetail:\n" not in cli(["--main", str(main), "--topic", TOPIC], fake)[1]


def test_wait_returns_when_the_run_turns_orphan(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run, boot=runstate.readBootId())  # the CLI reads this machine's boot id
	sleeps = []
	code, out = cli(["--main", str(main), "--topic", TOPIC, "--wait", "--json"], fake, sleeps)
	assert code == 0 and json.loads(out)["phase"] == "orphan"
	assert sum(sleeps) > THRESHOLD and sum(sleeps) <= THRESHOLD + LIMITS["status_wait_poll_seconds"]


def test_wait_returns_at_once_on_an_ended_or_absent_run(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	finish(worktree, run)
	sleeps = []
	assert cli(["--main", str(main), "--topic", TOPIC, "--wait"], fake, sleeps)[0] == 0
	assert cli(["--main", str(main), "--topic", "nobody", "--wait"], fake, sleeps)[0] == 0
	assert sleeps == []


def test_the_cli_without_a_main_checkout_fails_and_bad_usage_exits_2(fake, monkeypatch, tmp_path):
	monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
	monkeypatch.setenv("HOME", str(tmp_path))
	assert cli(["--topic", TOPIC], fake)[0] == 1
	with pytest.raises(SystemExit) as stopped:
		cli(["--topic", TOPIC, "--json", "--agent"], fake)
	assert stopped.value.code == 2


def test_the_cli_runs_as_a_program(main, fake, monkeypatch):
	legacyTree(addTopic(main), fake, eleventhOfNovember=True)
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(main))
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "run-status.py"), "--topic", TOPIC, "--agent"],
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert result.returncode == 0, result.stderr
	assert report_header.value(result.stdout, "SOURCE") == "legacy"


# -- the pid check (T-5, outside the sandbox only) ---------------------------------------------------


@pytest.fixture
def wrapper(tmp_path):
	"""A live process whose command line looks like run-worker.sh <topic> <worktree> <run>."""
	script = tmp_path / runstate.WRAPPER_NAME
	script.write_text("sleep 30\n")
	process = subprocess.Popen(["bash", str(script), TOPIC, str(tmp_path), "3"], start_new_session=True)
	yield process
	try:
		os.killpg(process.pid, signal.SIGKILL)
	except ProcessLookupError:
		pass
	process.wait(timeout=30)


def startTicks(pid):
	with open(f"/proc/{pid}/stat") as f:
		return int(f.read().rsplit(")", 1)[1].split()[19])


def test_verify_process_accepts_only_the_recorded_wrapper(wrapper):
	pid = wrapper.pid
	assert runstate.verifyProcess({"wrapper_pid": pid}, TOPIC, 3) is True
	assert runstate.verifyProcess({"wrapper_pid": pid, "start_ticks": startTicks(pid)}, TOPIC, 3) is True
	assert runstate.verifyProcess({}, TOPIC, 3) is None


def test_verify_process_rejects_other_cmdline(wrapper):
	assert runstate.verifyProcess({"wrapper_pid": wrapper.pid}, "other-topic", 3) is False
	assert runstate.verifyProcess({"wrapper_pid": wrapper.pid}, TOPIC, 4) is False
	assert runstate.verifyProcess({"wrapper_pid": os.getpid()}, TOPIC, 3) is False


def test_verify_process_rejects_other_start_ticks(wrapper):
	assert (
		runstate.verifyProcess({"wrapper_pid": wrapper.pid, "start_ticks": startTicks(wrapper.pid) + 1}, TOPIC, 3)
		is False
	)


def test_verify_process_says_gone_after_the_wrapper_ends(wrapper):
	pid = wrapper.pid
	os.killpg(pid, signal.SIGKILL)
	wrapper.wait(timeout=30)
	assert runstate.verifyProcess({"wrapper_pid": pid}, TOPIC, 3) is False


@pytest.mark.parametrize(
	"code, detail, outcome",
	[
		(0, "", {"outcome": "ticked"}),
		(0, "a warning", {"outcome": "ticked"}),
		(1, "", {"outcome": "error", "reason": "exit 1"}),
		(-9, "", {"outcome": "error", "reason": "signal 9"}),
		(1, "homeconfig.ConfigError: x", {"outcome": "error", "reason": "exit 1: homeconfig.ConfigError: x"}),
		(None, "FileNotFoundError: y", {"outcome": "error", "reason": "not started: FileNotFoundError: y"}),
	],
)
def test_queue_tick_outcome_names_the_exit_and_what_the_tick_said(code, detail, outcome):
	"""One shape for run-steps.py's queue_tick step and auto-finish.py's finish.json, in the step's vocabulary."""
	assert runstate.queueTickOutcome(code, detail) == outcome
	assert outcome["outcome"] in runstate.STEP_OUTCOMES["queue_tick"]


# -- a done worker's unfinished wrap-up is kept, never abandoned ----------------------------------------------


def doneOrphan(worktree, fake, stepsNow=None, code=0, status="done"):
	"""A run whose worker ended STATUS done (claude.exit_code as recordClaudeEnded writes it), then its wrapper
	died with the wrap-up unfinished; recorded confirmed as wt resume's pid check leaves it."""
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run, status=status)
	runstate.update(str(worktree), run, lambda r: r["claude"].update(exit_code=code, stopped=False))
	if stepsNow is not None:
		steps(worktree, run, **stepsNow)
	fake.advance(THRESHOLD + 60)
	runstate.confirmOrphan(str(worktree), run, "worker-control", {"wrapper_pid": "gone"})
	return run


UNFINISHED_MERGE = {**GOOD_STEPS, "merge": {"state": "running", "attempt": 1, "runner_pid": 9}}


def test_a_done_orphan_s_unfinished_wrapup_is_kept_never_closed(main, fake):
	worktree = addTopic(main)
	run = doneOrphan(worktree, fake, UNFINISHED_MERGE)
	before = open(recordPath(worktree, run)).read()
	with pytest.raises(runstate.RecordError, match="kept for run-steps.py wrapup"):
		runstate.closeVerifiedOrphan(str(worktree), run, "worker-control", {"checked": "gone"})
	assert open(recordPath(worktree, run)).read() == before
	assert eventlog.read(str(worktree), event="run-ended") == []


def test_a_kept_orphan_never_reads_as_merged(main, fake):
	"""Dangerous direction: keeping the steps adds no fact a merge gate takes; the run stays an orphan."""
	worktree = addTopic(main)
	doneOrphan(worktree, fake, UNFINISHED_MERGE)
	s = statusOf(main, fake)
	assert (s["phase"], s["confidence"], s["outcome"], s["merge_allowed"]) == ("orphan", "confirmed", None, False)
	assert [b["fact"] for b in s["merge_blocks"]] == ["phase"]
	assert s["steps"]["merge"]["state"] == "running" and "outcome" not in s["steps"]["merge"]


@pytest.mark.parametrize("status, code", [("blocked", 0), ("needs-decision", 0), ("done", None), ("done", "0")])
def test_an_orphan_without_a_done_worker_and_exit_code_is_still_closed_with_its_wrapup_abandoned(
	main, fake, status, code
):
	worktree = addTopic(main)
	run = doneOrphan(worktree, fake, UNFINISHED_MERGE, code=code, status=status)
	record = runstate.closeOrphan(str(worktree), run, "worker-control")
	assert record["exit"]["reason"] == "orphan-closed" and record["steps"]["merge"]["state"] == "abandoned"


def test_a_done_orphan_s_wrapup_is_offered_to_the_person_in_plain_words(main, fake):
	"""The person reads the situation and `wt wrapup <topic>` (pane button wrapup); the command line wt wrapup
	starts shows only in agent()'s evidence view."""
	estimateBefore(main, fake.now())
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run)
	runstate.update(str(worktree), run, lambda r: r["claude"].update(exit_code=0, stopped=False))
	steps(worktree, run, **UNFINISHED_MERGE)
	fake.advance(THRESHOLD + 60)
	program = os.path.join(HOOKS, "worker", "run-steps.py")
	command = f"WT_MAIN_DIR={main} python3 {program} wrapup {worktree} {run} --topic {TOPIC} --continue-stopped"
	left = ["merge", "next", "finish", "queue_tick"]
	suspected = statusOf(main, fake)
	assert {k: suspected["resumable"][k] for k in ("ok", "code", "prompt_kind", "steps_left", "command")} == {
		"ok": None, "code": "wrapup-left", "prompt_kind": None, "steps_left": left, "command": command}
	assert suspected["resumable"]["why"] == (
		"run 1 finished its work (STATUS done), but its wrapper may have ended before the wrap-up "
		f"({', '.join(left)}); "
		f"those steps are kept: type `wt wrapup {TOPIC}` (or press the button) to finish them, after it checks the "
		"wrapper is gone"
	)
	runstate.confirmOrphan(str(worktree), run, "test", {"wrapper_pid": "gone"})
	confirmed = statusOf(main, fake)
	assert (confirmed["resumable"]["ok"], confirmed["resumable"]["code"]) == (False, "wrapup-left")
	assert confirmed["resumable"]["why"] == (
		f"run 1 finished its work (STATUS done), but its wrapper ended before the wrap-up ({', '.join(left)}); "
		f"those steps are kept: type `wt wrapup {TOPIC}` (or press the button) to finish them"
	)
	for status, line, todo in ((suspected, "orphan_suspected", "todo_orphan_wrapup_check"),
			(confirmed, "orphan_wrapup", "todo_orphan_wrapup")):
		for lang in LANGS:
			words = LIMITS["status_text"][lang]
			view = runstate.human(status, lang)
			if line == "orphan_wrapup":
				assert view["line"] == words[line].format(topic=TOPIC, run=run, steps=", ".join(left))
			assert view["todo"] == {"text": words[todo].format(topic=TOPIC, run=run),
				"command": f"wt wrapup {TOPIC}", "button": "wrapup"}
			assert "run-steps.py" not in view["line"] + view["todo"]["text"] + status["resumable"]["why"]
	assert f"wt wrapup starts: {command}" in runstate.agent(confirmed).splitlines()


def test_after_a_wrapup_by_hand_the_refused_merge_is_shown_as_the_expected_next_step(main, fake):
	"""The hand wrap-up's merge step is refused (the run read as an orphan) and the run ends: the row shows the
	reason with the wt merge button and says this is the expected step."""
	worktree = addTopic(main)
	run = doneOrphan(worktree, fake, UNFINISHED_MERGE)
	reason = "the run's state doesn't allow a merge: the run is an orphan: its wrap-up never finished"
	merge = {"state": "done", "attempt": 2, "outcome": "refused", "reason": reason}
	runstate.update(str(worktree), run, lambda r: r["steps"].update(merge=merge))
	finish(worktree, run)
	s = statusOf(main, fake)
	assert (s["phase"], s["outcome"]["after_orphan"], s["merge"]["reason"]) == ("ended", True, reason)
	for lang in LANGS:
		words = LIMITS["status_text"][lang]
		view = runstate.human(s, lang)
		assert view["line"] == words["ended_after_wrapup"].format(topic=TOPIC, run=run, reason=reason)
		assert view["todo"] == {"text": words["todo_merge_after_wrapup"].format(topic=TOPIC, run=run),
			"command": f"wt merge {TOPIC}", "button": "merge"}


def test_a_refused_merge_of_a_run_that_was_never_an_orphan_keeps_its_line(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	merge = {"state": "done", "attempt": 1, "outcome": "refused", "reason": "a person must look"}
	runstate.update(str(worktree), run, lambda r: r["steps"].update(merge=merge))
	finish(worktree, run)
	s = statusOf(main, fake)
	assert s["outcome"]["after_orphan"] is False
	assert runstate.human(s, "en")["todo"]["text"] == LIMITS["status_text"]["en"]["todo_merge"]


def oldVsNewCells():
	"""Every record dimension closeOrphan reads: the worker's STATUS, claude.exit_code, the merge and finish steps
	(each missing or in every state; the other steps done, or every step missing) and the run's exit."""
	states = [None, *runstate.STEP_STATES]
	stepSets = [{}, {name: {"state": "done"} for name in runstate.STEP_ORDER}]
	for merge in states:
		for finish in states:
			found = {name: {"state": "done"} for name in ("verify", "verifier", "next", "queue_tick")}
			for name, state in (("merge", merge), ("finish", finish)):
				if state is not None:
					found[name] = {"state": state}
			stepSets.append(found)
	for status in (None, "done", "DONE", "blocked"):
		for code in ("absent", 0, 3, "0", True):
			for found in stepSets:
				for ended in (False, True):
					yield status, code, found, ended


def test_old_vs_new_orphan_close_every_change_is_a_kept_wrapup(main, fake):
	"""Skill refactor, section 2: the old closeOrphan (orphan_close_baseline.oldClose) and the new one over every
	cell. The only change: a run whose worker ended done with an int exit code and a step left is refused (kept)
	where the old one closed it; every other cell gives the same record or the same refusal."""
	worktree = addTopic(main)
	counts = {}
	for status, code, found, ended in oldVsNewCells():
		run = launch(worktree)
		claude = {} if status is None else {"ended_at": clock.isoNow(), "outcome": "status", "status": status}
		if code != "absent" and claude:
			claude["exit_code"] = code
		fields = {"steps": found, "orphan": {"confirmed_at": clock.isoNow(), "by": "t", "evidence": {}}}
		if claude:
			fields["claude"] = claude
		if ended:
			fields["exit"] = {"ended_at": clock.isoNow(), "code": 0, "reason": "completed"}
		change(worktree, run, **fields)
		before, _problem = runstate.readRecord(str(worktree), run)
		try:
			old = baseline.oldClose(before)
		except baseline.Refused:
			old = "refused"
		try:
			new = runstate.closeOrphan(str(worktree), run, "t")
			new["exit"] = {k: new["exit"][k] for k in ("code", "reason")}
		except runstate.RecordError:
			new = "refused"
		if new == old:
			reason = "same"
		else:
			kept = runstate.isWorkerDone(status) and type(code) is int and not ended
			left = [n for n in runstate.STEP_ORDER if (found.get(n) or {}).get("state") not in runstate.STEP_FINAL]
			assert kept and left and new == "refused", (status, code, found, ended, old, new)
			reason = "kept-wrapup"
		counts[reason] = counts.get(reason, 0) + 1
	print(f"orphan close old vs new: {counts}")
	assert counts["kept-wrapup"] > 0 and counts["same"] > 0


# --- the wrap-up that crashed, failed steps (self-repair stage 2: nothing fails silently) -----------------------


def keptRun(main, fake):
	"""A run whose worker ended STATUS done (exit code 0) with its merge step still running, under an estimate."""
	estimateBefore(main, fake.now())
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run)
	runstate.update(str(worktree), run, lambda r: r["claude"].update(exit_code=0, stopped=False))
	steps(worktree, run, **UNFINISHED_MERGE)
	return worktree, run


def test_a_wrapup_failed_run_has_ended_with_its_steps_kept_for_wt_wrapup(main, fake):
	"""recordWrapupFailed: phase ended (never an orphan), the error in the outcome, the steps left offered as
	`wt wrapup` without a pid check of an orphan (the run ended), and no merge."""
	worktree, run = keptRun(main, fake)
	runstate.recordWrapupFailed(str(worktree), run, 0, "OSError: disk full")
	s = statusOf(main, fake)
	assert (s["phase"], s["outcome"]["reason"], s["outcome"]["error"]) == ("ended", "wrapup-failed", "OSError: disk full")
	left = ["merge", "next", "finish", "queue_tick"]
	assert (s["resumable"]["ok"], s["resumable"]["code"], s["resumable"]["steps_left"]) == (False, "wrapup-left", left)
	assert s["resumable"]["why"].startswith("run 1 finished its work (STATUS done), but its wrap-up failed before")
	assert not s["merge_allowed"] and "exit" in [b["fact"] for b in s["merge_blocks"]]
	for lang in LANGS:
		words = LIMITS["status_text"][lang]
		view = runstate.human(s, lang)
		assert view["line"].startswith(
			words["ended_wrapup_failed"].format(topic=TOPIC, run=run, error="OSError: disk full")
			+ words["steps_kept"].format(steps=", ".join(left))
		)
		assert view["todo"] == {"text": words["todo_orphan_wrapup"].format(topic=TOPIC, run=run),
			"command": f"wt wrapup {TOPIC}", "button": "wrapup"}
	ended = eventlog.read(str(worktree), event="run-ended")
	assert [e["data"] for e in ended] == [{"reason": "wrapup-failed", "code": 0}]


def test_the_wrapup_run_again_writes_the_real_end_over_wrapup_failed_never_the_other_way(main, fake):
	worktree, run = keptRun(main, fake)
	runstate.recordWrapupFailed(str(worktree), run, 0, "OSError: disk full")
	runstate.recordRunEnded(str(worktree), run, "completed", 0)
	record, _problem = runstate.readRecord(str(worktree), run)
	assert record["exit"]["reason"] == "completed" and "error" not in record["exit"]
	runstate.recordWrapupFailed(str(worktree), run, 0, "OSError: again")
	record, _problem = runstate.readRecord(str(worktree), run)
	assert record["exit"]["reason"] == "completed"
	assert runstate.wrapupLeft(record) == []


def test_a_wrapup_failed_run_whose_worker_did_not_end_done_is_resumed_as_before(main, fake):
	estimateBefore(main, fake.now())
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run, status="blocked")
	runstate.update(str(worktree), run, lambda r: r["claude"].update(exit_code=0, stopped=False))
	runstate.recordWrapupFailed(str(worktree), run, 0, "KeyError: 'x'")
	s = statusOf(main, fake)
	assert s["phase"] == "ended" and s["resumable"]["ok"] is True and s["resumable"]["code"] is None
	view = runstate.human(s, "en")
	assert view["line"] == LIMITS["status_text"]["en"]["ended_wrapup_failed"].format(topic=TOPIC, run=run,
		error="KeyError: 'x'")
	assert view["todo"]["command"] == f"wt resume {TOPIC}"


def test_old_vs_new_end_and_kept_steps_differ_only_after_wrapup_failed():
	"""Skill refactor, section 2: endRecord's and wrapupLeft's old rules (an exit already there is kept / means
	nothing is owed) against the new ones over every exit (none, each reason) and every incoming reason. The only
	changes: an exit wrapup-failed is replaced by any other end, and still owes its steps left."""
	counts = {}
	for existing in [None, *runstate.EXIT_REASONS]:
		for incoming in runstate.EXIT_REASONS:
			record = {"topic": TOPIC, "steps": {}, "claude": {"status": "done", "exit_code": 0}}
			if existing:
				record["exit"] = {"ended_at": clock.isoNow(), "code": 0, "reason": existing}
			oldWrites = not record.get("exit")
			oldLeft = [] if record.get("exit") else list(runstate.STEP_ORDER)
			newLeft = runstate.wrapupLeft(record)
			newWrites = runstate.endRecord(record, incoming, 0, None)
			if (oldWrites, oldLeft) == (newWrites, newLeft):
				reason = "same"
			else:
				assert existing == runstate.WRAPUP_FAILED, (existing, incoming, newWrites, newLeft)
				assert newLeft == list(runstate.STEP_ORDER)
				assert newWrites == (incoming != runstate.WRAPUP_FAILED)
				reason = "after-wrapup-failed"
			counts[reason] = counts.get(reason, 0) + 1
	print(f"endRecord/wrapupLeft old vs new: {counts}")
	assert counts == {"same": len(runstate.EXIT_REASONS) * len(runstate.EXIT_REASONS), "after-wrapup-failed":
		len(runstate.EXIT_REASONS)}


def test_failed_wrap_up_steps_show_in_the_person_s_line_in_every_phase(main, fake):
	worktree = addTopic(main)
	run = doneRun(worktree)
	assert "failed" not in runstate.human(statusOf(main, fake), "en")["line"]
	runstate.update(str(worktree), run, lambda r: r["steps"].update(
		finish={"state": "failed", "reason": "auto-finish.py: check: x (finish.json)"},
		next={"state": "failed", "reason": "exited without an outcome (exit 1)"}))
	for ended in (False, True):
		if ended:
			finish(worktree, run)
		s = statusOf(main, fake)
		for lang in LANGS:
			words = LIMITS["status_text"][lang]
			suffix = words["steps_failed"].format(
				steps="next: exited without an outcome (exit 1); finish: auto-finish.py: check: x (finish.json)")
			assert runstate.human(s, lang)["line"].endswith(suffix), (s["phase"], runstate.human(s, lang)["line"])


# -- self-repair stage 3a (A3, A8, B1, B2) -----------------------------------------------------------------------


def keptWrapupRun(worktree, fake):
	"""A run whose worker ended STATUS done and whose wrap-up crashed outside its steps (exit wrapup-failed)."""
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run)
	change(worktree, run, claude={**runstate.readRecord(str(worktree), run)[0]["claude"], "exit_code": 0})
	runstate.recordWrapupFailed(str(worktree), run, 0, "RuntimeError: x")
	return run


@pytest.mark.parametrize("hide", ["plans", "estimate"])
def test_a_kept_wrapup_is_named_before_plans_and_the_estimate_are_looked_at(main, fake, monkeypatch, hide):
	"""A3: without a visible plans/ or without an estimate the kept wrap-up is still the answer (wrapup-left)."""
	worktree = addTopic(main)
	keptWrapupRun(worktree, fake)
	if hide == "plans":
		monkeypatch.setattr(runstate, "plansVisible", lambda mainDir: False)
	status = statusOf(main, fake)
	assert status["resumable"]["code"] == "wrapup-left", status["resumable"]
	assert runstate.human(status, "en")["todo"]["command"] == f"wt wrapup {TOPIC}"


def test_a_verifier_that_could_not_judge_reads_failed_with_its_reason_and_pass_and_fail_stay_done(tmp_path):
	"""A8: the verifier's verdict error is a failed step (outcome ERROR, its why), so a wrap-up retries it; passed
	and failed are done."""
	runs = tmp_path / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	found = {}
	for verdict in ("passed", "failed", "error", "odd"):
		(runs / "verifier-1.json").write_text(json.dumps({"run": 1, "verdict": verdict, "why": "timeout"}))
		found[verdict] = runstate.legacyVerifierStep(str(tmp_path), 1, None)
	assert found["passed"]["state"] == "done" and found["passed"]["outcome"] == "PASS"
	assert found["failed"]["state"] == "done" and found["failed"]["outcome"] == "FAIL"
	for verdict in ("error", "odd"):
		assert found[verdict] == {"state": "failed", "outcome": "ERROR", "reason": "timeout", "detail": "verifier-1.json"}
	block = runstate.verifierBlock(found["error"], {"verifier": {}})
	assert block["outcome"] == "ERROR"  # auto-merge's refusal still reads "could not judge"


def test_a_step_running_past_the_report_limit_is_named_in_the_line_only(main, fake):
	"""B1: a finishing run's step running its report threshold or longer (step_running_report_factor of its default
	limit: the step recorded none) is named in human's line; the status (phase, busy, step) is unchanged and nothing
	is written."""
	worktree = addTopic(main)
	run = doneRun(worktree)
	before = open(recordPath(worktree, run)).read()
	limit = runstate.reportMinutes({"name": "merge"}, LIMITS)
	assert limit == int(LIMITS["step_limit_default_seconds"]["merge"] * LIMITS["step_running_report_factor"] // 60)
	fake.advance(limit * 60 - 60)
	runstate.beat(str(worktree), run, INTERVAL)  # the wrapper still beats: finishing, not an orphan
	early = statusOf(main, fake)
	assert runstate.longStep(early) is None and "has run" not in runstate.human(early, "en")["line"]
	fake.advance(60)
	runstate.beat(str(worktree), run, INTERVAL)
	late = statusOf(main, fake)
	assert runstate.longStep(late) == {"name": "merge", "minutes": limit, "limit_minutes": limit}
	for lang in LANGS:
		assert str(limit) in runstate.human(late, lang)["line"]
	assert (late["phase"], late["busy"], late["step"]["name"]) == (early["phase"], early["busy"], "merge")
	assert open(recordPath(worktree, run)).read() == before


def test_a_notice_is_told_once_per_run_even_by_two_callers_at_once(main, fake):
	"""B2's memory: recordNotice is True only the first time, under the record's lock; it changes no step, exit or
	process fact; an unknown notice and a run without a record raise."""
	worktree = addTopic(main)
	run = doneRun(worktree)
	before = runstate.readRecord(str(worktree), run)[0]
	import concurrent.futures

	with concurrent.futures.ThreadPoolExecutor(8) as pool:
		told = list(pool.map(lambda _: runstate.recordNotice(str(worktree), run, "heartbeat_stale"), range(8)))
	assert told.count(True) == 1
	after = runstate.readRecord(str(worktree), run)[0]
	assert {k: v for k, v in after.items() if k != "notices"} == before
	assert list(after["notices"]) == ["heartbeat_stale_at"]
	with pytest.raises(runstate.RecordError):
		runstate.recordNotice(str(worktree), run, "other")
	with pytest.raises(runstate.RecordError):
		runstate.recordNotice(str(worktree), run + 5, "heartbeat_stale")


def test_a_step_s_recorded_limit_sets_its_report_threshold(main, fake):
	"""B1 from the limit the step started with (limit_seconds, startStep): step_running_report_factor of it."""
	worktree = addTopic(main)
	run = doneRun(worktree)
	merge = runstate.readRecord(str(worktree), run)[0]["steps"]["merge"]
	steps(worktree, run, **GOOD_STEPS, merge={**merge, "limit_seconds": 40 * 60})
	limit = int(40 * LIMITS["step_running_report_factor"])
	fake.advance(limit * 60 - 60)
	runstate.beat(str(worktree), run, INTERVAL)
	assert runstate.longStep(statusOf(main, fake)) is None
	fake.advance(60)
	runstate.beat(str(worktree), run, INTERVAL)
	assert runstate.longStep(statusOf(main, fake)) == {"name": "merge", "minutes": limit, "limit_minutes": limit}


def test_start_step_records_the_limit_and_keeps_it_through_the_outcome(main, fake):
	worktree = addTopic(main)
	run = launch(worktree)
	assert runstate.startStep(str(worktree), run, "verify", 4242, 3, lambda step: False, limitSeconds=600) == {
		"attempt": 1}
	runstate.recordStep(str(worktree), run, "verify", {"outcome": "PASS"})
	step = runstate.readRecord(str(worktree), run)[0]["steps"]["verify"]
	assert (step["limit_seconds"], step["outcome"], step["attempt"]) == (600, "PASS", 1)


def nextDecided(worktree, outcome="resume"):
	run = doneRun(worktree)
	change(worktree, run, exit={"ended_at": clock.isoNow(), "code": 0, "reason": "completed"})
	runstate.recordStep(str(worktree), run, "next", {"outcome": outcome})
	return run


def test_next_starts_are_counted_up_to_the_limit_then_refused(main, fake):
	"""claimNextStart: one start counted per call under the lock, never more than attemptsMax; kept when the step
	is written again from its file (STEP_BOOKKEEPING)."""
	worktree = addTopic(main)
	run = nextDecided(worktree)
	assert [runstate.claimNextStart(str(worktree), run, 2) for _ in range(3)] == [{"attempt": 1}, {"attempt": 2},
		{"limit": 2}]
	runstate.recordStep(str(worktree), run, "next", {"outcome": "resume"})
	assert runstate.readRecord(str(worktree), run)[0]["steps"]["next"]["start_attempts"] == 2


def test_closing_an_unstarted_next_ends_its_hold_on_the_topic(main, fake):
	"""closeNextUnstarted: failed with the reason, outcome dropped, so status is no longer busy; a second call and a
	next step that decided no run change nothing."""
	worktree = addTopic(main)
	run = nextDecided(worktree)
	assert statusOf(main, fake)["busy"] is True
	assert runstate.closeNextUnstarted(str(worktree), run, "never started after 3 starts") is True
	step = runstate.readRecord(str(worktree), run)[0]["steps"]["next"]
	assert step["state"] == "failed" and "outcome" not in step and step["reason"].startswith("never started")
	assert statusOf(main, fake)["busy"] is False
	assert runstate.closeNextUnstarted(str(worktree), run, "again") is False
	assert runstate.claimNextStart(str(worktree), run, 3) == {"gone": "the next step holds no decided run any more"}


def test_the_pid_namespace_is_read_from_proc_and_none_when_it_can_t_be(tmp_path):
	assert runstate.pidNamespace() == os.readlink("/proc/self/ns/pid")
	assert runstate.pidNamespace(999999999) is None
	assert runstate.pidNamespace("self", str(tmp_path)) is None


# -- a run stopped during its wrap-up (its wrap-up kept for wt wrapup) -----------------------------------------

STOPPED_IN_WRAPUP = {
	"verify": {"state": "failed", "attempt": 1, "reason": "exited without an outcome (signal 2)"},
	"verifier": {"state": "skipped", "attempt": 1, "skip_reason": "stopped"},
	"merge": {"state": "skipped", "attempt": 1, "skip_reason": "stopped"},
	"next": {"state": "skipped", "attempt": 1, "skip_reason": "stopped"},
	"finish": {"state": "skipped", "attempt": 1, "skip_reason": "not-merged"},
	"queue_tick": {"state": "done", "attempt": 1, "outcome": "ticked"},
}


def stoppedRunOf(main, fake, claudeStopped=False):
	"""Run 1 as `wt stop` leaves it: the worker ended STATUS done (exit 0, claude's part stopped or not), the stop
	recorded, the wrap-up's steps as a stop during verify leaves them, the exit stopped."""
	estimateBefore(main, fake.now())
	worktree = addTopic(main)
	run = launch(worktree)
	start(worktree, run)
	claudeEnded(worktree, run)
	runstate.update(str(worktree), run, lambda r: r["claude"].update(exit_code=0, stopped=claudeStopped))
	runstate.recordStop(str(worktree), run, "person")
	steps(worktree, run, **STOPPED_IN_WRAPUP)
	finish(worktree, run, reason="stopped")
	return worktree, run


def test_a_run_stopped_during_its_wrapup_keeps_its_steps_for_wt_wrapup(main, fake):
	"""The stop came after claude's part ended unstopped: the steps it skipped or broke stay owed, the run is not
	resumed beside them, and the person reads `wt wrapup` as the next step (the kept-wrap-up words); the evidence
	view's command is what wt wrapup starts (--continue-stopped). No merge fact changes."""
	worktree, run = stoppedRunOf(main, fake)
	s = statusOf(main, fake)
	left = ["verify", "verifier", "merge", "next"]
	assert (s["phase"], s["outcome"]["reason"]) == ("ended", "stopped")
	assert {k: s["resumable"][k] for k in ("ok", "code", "steps_left")} == {"ok": False, "code": "wrapup-left",
		"steps_left": left}
	assert s["resumable"]["why"] == (f"run 1 finished its work (STATUS done), but its wrap-up was stopped with steps "
		f"left ({', '.join(left)}); those steps are kept: type `wt wrapup {TOPIC}` (or press the button) to finish them")
	assert s["resumable"]["command"].endswith(f"wrapup {worktree} {run} --topic {TOPIC} --continue-stopped")
	facts = {b["fact"] for b in s["merge_blocks"]}
	assert not s["merge_allowed"] and runstate.WRAPUP_STOP in facts and not facts & {"stop", "exit"}
	for lang in LANGS:
		words = LIMITS["status_text"][lang]
		view = runstate.human(s, lang)
		assert view["line"].startswith(words["ended_stopped"].format(topic=TOPIC, run=run)
			+ words["steps_kept"].format(steps=", ".join(left)))
		assert view["todo"] == {"text": words["todo_orphan_wrapup"].format(topic=TOPIC, run=run),
			"command": f"wt wrapup {TOPIC}", "button": "wrapup"}


@pytest.mark.parametrize("claudeStopped", [True, None])
def test_a_run_whose_claude_part_was_stopped_keeps_no_wrapup_as_before(main, fake, claudeStopped):
	"""A stop before claude's part ended (claude.stopped True), or one the record can't place (claude.stopped
	missing): nothing is owed, the line is the stopped one without a todo, exactly as before."""
	worktree, run = stoppedRunOf(main, fake, claudeStopped=claudeStopped)
	if claudeStopped is None:
		runstate.update(str(worktree), run, lambda r: r["claude"].pop("stopped"))
	record = runstate.readRecord(str(worktree), run)[0]
	assert runstate.wrapupLeft(record) == [] == wrapup_stop_baseline.oldWrapupLeft(record)
	s = statusOf(main, fake)
	assert s["resumable"]["code"] != "wrapup-left"
	view = runstate.human(s, "en")
	assert view["line"].startswith(LIMITS["status_text"]["en"]["ended_stopped"].format(topic=TOPIC, run=run))
	assert view["todo"] is None
	facts = {b["fact"] for b in s["merge_blocks"]}
	assert {"stop", "exit"} <= facts and runstate.WRAPUP_STOP not in facts  # refused for the person's approval too


def test_a_stop_after_claude_s_part_is_told_apart_in_both_stories():
	"""stopAfterClaude: the record's claude.stopped False; today's files (no flag) a stop strictly after claude's end in
	whole seconds; a STATUS other than done, or no STATUS outcome, never."""
	done = {"outcome": "status", "status": "done", "ended_at": "2026-11-09T10:00:00+08:00"}
	stop = lambda at: {"requested_at": at, "by": "person"}  # noqa: E731
	assert runstate.stopAfterClaude({"claude": {**done, "stopped": False}, "stop": stop(None)}) is True
	assert runstate.stopAfterClaude({"claude": {**done, "stopped": True}, "stop": stop("2026-11-09T11:00:00+08:00")}) \
		is False
	assert runstate.stopAfterClaude({"claude": done, "stop": stop("2026-11-09T10:00:01+08:00")}) is True
	assert runstate.stopAfterClaude({"claude": done, "stop": stop("2026-11-09T10:00:00+08:00")}) is False
	assert runstate.stopAfterClaude({"claude": done, "stop": stop("2026-11-09T09:59:59+08:00")}) is False
	assert runstate.stopAfterClaude({"claude": done, "stop": stop(None)}) is False
	assert runstate.stopAfterClaude({"claude": {**done, "ended_at": None}, "stop": stop("2026-11-09T11:00:00+08:00")}) \
		is False
	for claude in ({**done, "status": "blocked"}, {**done, "outcome": "stopped"}):
		assert runstate.stopAfterClaude({"claude": {**claude, "stopped": False}}) is False


def test_a_stop_after_the_wrapup_left_nothing_owed_keeps_nothing(main, fake):
	"""A stop that came when every step it reached was final and none was skipped as stopped (here during the
	queue tick): nothing is owed, wt wrapup has nothing to continue."""
	worktree, run = stoppedRunOf(main, fake)
	final = {name: {"state": "done", "attempt": 1} for name in runstate.STEP_ORDER}
	runstate.update(str(worktree), run, lambda r: r.update(steps=final))
	assert runstate.wrapupLeft(runstate.readRecord(str(worktree), run)[0]) == []


def test_stopped_run_names_every_kind_of_stop_and_nothing_else():
	base = {"claude": {"status": "done", "exit_code": 0, "stopped": False}, "steps": {}}
	assert runstate.stoppedRun(base) is False and runstate.stopDuringWrapup(base) is False
	for record in (
		{**base, "stop": {"requested_at": None, "by": "person"}},
		{**base, "claude": {**base["claude"], "stopped": True}},
		{**base, "exit": {"reason": "stopped"}},
		{**base, "steps": {"merge": {"state": "skipped", "skip_reason": "stopped"}}},
	):
		assert runstate.stoppedRun(record) is True, record
	assert runstate.stopDuringWrapup({**base, "exit": {"reason": "stopped"}}) is True
	assert runstate.stopDuringWrapup({**base, "claude": {**base["claude"], "stopped": True}}) is False
	assert runstate.stopDuringWrapup({"stop": {"by": "person"}, "claude": {"status": "done"}}) is False
	for reason in runstate.EXIT_REASONS:
		if reason != "stopped":
			assert runstate.stoppedRun({**base, "exit": {"reason": reason}}) is False, reason
	for skip in runstate.SKIP_REASONS:
		if skip != "stopped":
			assert runstate.stoppedRun({**base, "steps": {"merge": {"state": "skipped", "skip_reason": skip}}}) is False


def stopCells():
	"""Every record dimension wrapupLeft reads, with each stop fact: the worker's STATUS, claude.exit_code,
	claude.stopped (missing, None, False, True), the person's stop, the exit (none, each reason) and one step in each
	state (a skip with each reason) beside the others done."""
	stepCases = [None] + [{"state": s} for s in runstate.STEP_STATES if s != "skipped"]
	stepCases += [{"state": "skipped", "skip_reason": r} for r in runstate.SKIP_REASONS]
	for status in ("done", "blocked"):
		for code in (0, None):
			for claudeStopped in ("missing", None, False, True):
				for stop in (False, True):
					for end in [None, *runstate.EXIT_REASONS]:
						for name in ("verify", "merge"):
							for step in stepCases:
								claude = {"status": status, "exit_code": code}
								if claudeStopped != "missing":
									claude["stopped"] = claudeStopped
								found = {n: {"state": "done"} for n in runstate.STEP_ORDER if n != name}
								if step is not None:
									found[name] = dict(step)
								record = {"claude": claude, "steps": found}
								if stop:
									record["stop"] = {"requested_at": None, "by": "person"}
								if end:
									record["exit"] = {"reason": end, "code": 0}
								yield record


def test_old_vs_new_kept_steps_change_only_for_a_stop_during_the_wrapup():
	"""Skill refactor, section 2: the old wrapupLeft (wrapup_stop_baseline) against today's over every cell. The only
	change: a run stopped only during its wrap-up (claude.stopped False, a stop fact) whose worker ended done with an
	int exit code now owes its steps also after an exit stopped, its skips as stopped included; every other cell gives
	the same list."""
	counts = {}
	for record in stopCells():
		old, new = wrapup_stop_baseline.oldWrapupLeft(record), runstate.wrapupLeft(record)
		if old == new:
			reason = "same"
		else:
			claude, end = record["claude"], record.get("exit") or {}
			assert claude.get("stopped") is False and runstate.stoppedRun(record), record
			assert claude["status"] == "done" and claude["exit_code"] == 0, record
			assert end.get("reason") in (None, "stopped", runstate.WRAPUP_FAILED), record
			extra = [n for n in new if n not in old]
			assert set(old) <= set(new) and extra, record
			for name in extra:
				step = record["steps"].get(name) or {}
				assert step.get("skip_reason") == "stopped" or end.get("reason") == "stopped", record
			reason = "stop-during-wrapup"
		counts[reason] = counts.get(reason, 0) + 1
	print(f"wrapupLeft old vs new: {counts}")
	assert counts["same"] > 0 and counts["stop-during-wrapup"] > 0


def test_old_vs_new_stop_and_exit_blocks_change_only_their_fact_after_claude_s_part():
	"""Skill refactor, section 2: mergeBlocks' stop and exit blocks against the old ones over every cell (claude's
	outcome and STATUS, claude.stopped missing/False/True, a stop before, at or after claude's end or none, every
	exit). The reasons never change, so the automatic merge refuses exactly as before; the only change is the fact
	wrapup-stop (which the person's approval passes) for a stop after claude's part completed, and only the stop
	and an exit stopped take it."""
	counts = {}
	ended = "2026-11-09T10:00:00+08:00"
	for outcome, status in (("status", "done"), ("status", "blocked"), ("stopped", None), ("error", None)):
		for claudeStopped in ("missing", False, True):
			for stopAt in (None, "2026-11-09T09:59:00+08:00", ended, "2026-11-09T10:01:00+08:00"):
				for end in [None, *runstate.EXIT_REASONS]:
					claude = {"outcome": outcome, "status": status, "ended_at": ended}
					if claudeStopped != "missing":
						claude["stopped"] = claudeStopped
					record = {"claude": claude, "integration_mode": "local", "main_head_at_start": "x", "steps": {}}
					if stopAt:
						record["stop"] = {"requested_at": stopAt, "by": "person"}
					if end:
						record["exit"] = {"reason": end, "code": 0}
					held = {"state": "clear", "files": []}
					new = [(b["fact"], b["reason"]) for b in runstate.mergeBlocks(record, "ended", held, "absent", status)
						if b["fact"] in ("stop", "exit", runstate.WRAPUP_STOP)]
					old = wrapup_stop_baseline.oldStopExitBlocks(record)
					if new == old:
						reason = "same"
					else:
						assert [r for _f, r in new] == [r for _f, r in old], (record, new, old)
						assert runstate.stopAfterClaude(record), record
						for (newFact, text), (oldFact, _text) in zip(new, old):
							assert newFact == oldFact or (newFact == runstate.WRAPUP_STOP and (oldFact == "stop" or
								text == "the run ended stopped")), (record, new)
						reason = "after-claude"
					counts[reason] = counts.get(reason, 0) + 1
	print(f"stop/exit blocks old vs new: {counts}")
	assert counts["same"] > 0 and counts["after-claude"] > 0


def test_after_a_continued_wrapup_the_refused_merge_offers_wt_merge(main, fake):
	"""The continued wrap-up ended with the automatic merge refused for the stop: the row shows the refusal with the
	wt merge button, which the person's approval can now pass (the stop is wrapup-stop)."""
	worktree, run = stoppedRunOf(main, fake)
	final = {name: {"state": "done", "attempt": 2} for name in runstate.STEP_ORDER}
	reason = "the run's state doesn't allow a merge: the run was stopped; the run ended stopped"
	final["merge"] = {"state": "done", "attempt": 2, "outcome": "refused", "reason": reason}
	runstate.update(str(worktree), run, lambda r: r.update(steps=final))
	s = statusOf(main, fake)
	assert s["resumable"]["code"] != "wrapup-left"
	view = runstate.human(s, "en")
	assert view["todo"]["command"] == f"wt merge {TOPIC}" and view["todo"]["button"] == "merge"
	assert {b["fact"] for b in s["merge_blocks"]} & {"stop", "exit"} == set()

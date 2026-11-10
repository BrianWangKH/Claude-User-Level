"""Run record migration step 3: the display readers show runstate's status (hooks/lib/runstate.py; since step 4
wt_controls.py's row also decides from the same status, test_decision_readers.py): wt_controls.py's display
fields for the wt-pane ("phase", "human"), the planning session's run-result.py and worker-status.py
(runstate.agent with runstate.detail) and worker-control.py's `wt status` (runstate.agent with the pid check). Every reader shows every phase, orphan suspected and confirmed and
unknown included, from a run record and from today's runs/ files only; the pane's JSON keeps every field it
had (schema 1, fields only added); the person's line follows the status_language setting and its local
override. Writers and readers share the fixtures of run_fixtures.py (hooks/RULES.md T9): records through
runstate's writer, today's files in the shapes run-worker.sh and worker-control.py write.

A run of today's files alone never reads unknown: the legacy adapter maps every file it reads into known
values (an unreadable file is a missing fact), so the unknown cases come from a record.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_display_readers.py -q -p no:cacheprovider'
"""

import io
import json
import os
import sys

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import hookonly  # noqa: E402
import runstate  # noqa: E402
import wt_controls  # noqa: E402
from run_fixtures import (  # noqa: E402
	CASES,
	LIMITS,
	START,
	THRESHOLD,
	TOPIC,
	FakeClock,
	addTopic,
	build,
	expected,
	legacyFile,
	legacyRun,
	load,
	makeMain,
	pidCheck,
)

# wt_controls.py's row fields before migration step 3; the pane (mods/wt-pane control.ts) reads these.
OLD_ROW_KEYS = {"topic", "action", "why", "run", "held", "auto", "merge", "approving"}
# Top-level fields added after migration step 3, for display only (the pane's config mode word).
DISPLAY_TOP_KEYS = {"config_mode", "config_mode_error"}
OLD_TOP_KEYS = {
	"schema",
	"controls",
	"plans_visible",
	"flow_mode",
	"flow_mode_error",
	"queue",
	"planned",
	"queue_error",
	"local_blocker",
	"queue_limits",  # added by the config merge: additive, schema 1 stays
}
DISPLAY_KEYS = {"phase", "human"}


@pytest.fixture
def fake():
	source = FakeClock(START)
	clock.useSource(source.now)
	yield source
	clock.useSource(None)


@pytest.fixture
def main(tmp_path, monkeypatch):
	return makeMain(tmp_path, monkeypatch)


# -- the readers -------------------------------------------------------------------------------------


def runResult(main, monkeypatch, capsys, *argv):
	module = load(os.path.join(SKILLS, "repo-plan", "run-result.py"), "run_result")
	monkeypatch.chdir(main)
	monkeypatch.setattr(sys, "argv", ["run-result.py", *argv])
	module.main()
	return module, capsys.readouterr().out


def workerStatus(main, monkeypatch, capsys):
	module = load(os.path.join(SKILLS, "repo-plan", "worker-status.py"), "worker_status")
	monkeypatch.chdir(main)
	monkeypatch.setattr(sys, "argv", ["worker-status.py", TOPIC])
	module.main()
	return capsys.readouterr().out


def wtPrompt(main, monkeypatch, capsys, prompt):
	"""worker-control.py's answer to a typed prompt: the parsed hook output."""
	module = load(os.path.join(HOOKS, "worker", "worker-control.py"), "worker_control")
	monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "cwd": str(main)})))
	with pytest.raises(SystemExit) as ended:
		module.main()
	assert ended.value.code == 0
	return json.loads(capsys.readouterr().out)


def agentShows(text, status):
	"""The agent view of status is in text: its phase, confidence and the line for people (en)."""
	assert f"PHASE: {status['phase']}" in text, text
	if status["phase"] == "orphan":
		assert f"CONFIDENCE: {status['confidence']}" in text, text
	assert runstate.human(status, "en")["line"] in text, text
	assert "evidence:" in text and "detail:" in text, text


@pytest.mark.parametrize("source, phase", CASES)
def test_the_pane_row_shows_the_phase_and_the_person_s_line(main, fake, source, phase):
	build(main, fake, source, phase)
	status = runstate.status(str(main), TOPIC)
	assert (status["phase"], status["confidence"]) == expected(source, phase)
	row = wt_controls.control(main, TOPIC)
	assert row["phase"] == status["phase"]
	assert row["human"] == runstate.human(status)
	assert row["human"]["line"].startswith(TOPIC) and "{" not in row["human"]["line"]


@pytest.mark.parametrize("source, phase", CASES)
def test_run_result_prints_the_status_and_what_to_do_next(main, fake, monkeypatch, capsys, source, phase):
	build(main, fake, source, phase)
	status = runstate.status(str(main), TOPIC)
	_module, out = runResult(main, monkeypatch, capsys, TOPIC)
	agentShows(out, status)
	if phase == "none":
		assert f"Next: {TOPIC} has no run yet" in out
	elif phase == "ended":
		assert "Next: Ask the user the questions" in out
		# the Cost row is judge()'s, from today's files (cost-history.py import); a record alone has none yet
		assert ("Cost row" in out) == (source == "legacy")
		assert out.rstrip().endswith("what was done")
	else:
		assert f"Next: run 1 is {status['phase']}, not ended" in out


@pytest.mark.parametrize("source, phase", CASES)
def test_worker_status_prints_the_status_then_the_progress(main, fake, monkeypatch, capsys, source, phase):
	build(main, fake, source, phase)
	status = runstate.status(str(main), TOPIC)
	out = workerStatus(main, monkeypatch, capsys)
	agentShows(out, status)
	assert "progress:" in out and "git: " in out


@pytest.mark.parametrize("source, phase", CASES)
def test_wt_status_shows_each_phase_with_the_pid_check(main, fake, monkeypatch, capsys, source, phase):
	build(main, fake, source, phase)
	monkeypatch.setattr(runstate, "verifyProcess", pidCheck(phase))
	status = runstate.status(str(main), TOPIC, pidAlive=pidCheck(phase))
	assert (status["phase"], status["confidence"]) == expected(source, phase, pidChecked=True)
	answer = wtPrompt(main, monkeypatch, capsys, f"wt status {TOPIC}")
	context = answer["hookSpecificOutput"]["additionalContext"]
	assert context.startswith("wt: status")
	assert f"PHASE: {status['phase']}" in context and runstate.human(status, "en")["line"] in context
	if phase == "orphan-confirmed":
		assert "CONFIDENCE: confirmed" in context and "PID-CHECKED: yes" in context


# -- readers beyond the phase ------------------------------------------------------------------------


def test_wt_status_with_a_live_wrapper_keeps_a_stale_heartbeat_running(main, fake, monkeypatch, capsys):
	build(main, fake, "record", "orphan-suspected")
	monkeypatch.setattr(runstate, "verifyProcess", lambda process, topic, run: True)
	context = wtPrompt(main, monkeypatch, capsys, f"wt status {TOPIC}")["hookSpecificOutput"]["additionalContext"]
	assert "PHASE: running" in context and "heartbeat-stale-process-alive" in context


def test_wt_status_without_a_topic_lists_every_topic(main, fake, monkeypatch, capsys):
	build(main, fake, "record", "running")
	addTopic(main, "other-topic")
	monkeypatch.setattr(runstate, "verifyProcess", lambda process, topic, run: None)
	context = wtPrompt(main, monkeypatch, capsys, "wt status")["hookSpecificOutput"]["additionalContext"]
	assert f"TOPIC: {TOPIC}" in context and "TOPIC: other-topic" in context


def snapshot(top):
	found = {}
	for folder, _dirs, files in os.walk(top):
		for name in files:
			path = os.path.join(folder, name)
			found[path] = os.stat(path).st_mtime_ns
	return found


def test_wt_status_changes_nothing(main, fake, monkeypatch, capsys, tmp_path):
	worktree = build(main, fake, "record", "orphan-suspected")
	monkeypatch.setattr(runstate, "verifyProcess", lambda process, topic, run: False)
	before = snapshot(worktree / ".hook-only"), snapshot(main / ".hook-only")
	wtPrompt(main, monkeypatch, capsys, f"wt status {TOPIC}")
	assert (snapshot(worktree / ".hook-only"), snapshot(main / ".hook-only")) == before


def test_the_run_result_wait_returns_when_the_run_turns_orphan(main, fake, monkeypatch, capsys):
	build(main, fake, "record", "running")
	module = load(os.path.join(SKILLS, "repo-plan", "run-result.py"), "run_result")
	slept = []

	def sleep(seconds):
		slept.append(seconds)
		fake.advance(seconds)

	monkeypatch.setattr(module.time, "sleep", sleep)
	monkeypatch.chdir(main)
	monkeypatch.setattr(sys, "argv", ["run-result.py", TOPIC, "--wait"])
	module.main()
	out = capsys.readouterr().out
	assert "PHASE: orphan" in out and "CONFIDENCE: suspected" in out
	assert set(slept) == {LIMITS["status_wait_poll_seconds"]} and sum(slept) <= THRESHOLD + slept[0]


def test_run_result_relays_the_evidence_files_and_the_automatic_fix_run(main, fake, monkeypatch, capsys):
	worktree = build(main, fake, "legacy", "ended")
	now = fake.now()
	legacyFile(
		worktree,
		"verify-1.json",
		{"passed": False, "checks": [{"name": "rows", "ok": False, "why": "0 rows"}, {"name": "runs", "ok": True}]},
		5,
		now,
	)
	legacyFile(
		worktree, "verifier-1.json", {"verdict": "failed", "report": "V1 fail: revenue doubled\nV2 pass"}, 5, now
	)
	legacyFile(worktree, "merge-1.json", {"decision": "refused", "reason": "verify failed", "fix_run": True}, 5, now)
	legacyFile(worktree, "auto-1.json", {"decision": "fix", "next_run": 2, "minutes": 30, "usd": 4.0}, 5, now)
	_module, out = runResult(main, monkeypatch, capsys, TOPIC, "1")
	assert "verify-1.json: FAILED: rows: 0 rows" in out
	assert "verifier-1.json: FAILED: V1 fail: revenue doubled" in out
	assert "auto-1.json: automatic fix run 2 started (estimate 30 min, $4.00)" in out
	assert "Automatic fix run: run 2 started by the hook" in out and "run-result.py cash-account 2 --wait" in out
	assert "Automatic merge waits for the fix run: verify failed." in out
	assert "Real-data checks FAILED" in out and "Verifier: FAILED" in out


def test_run_result_on_a_done_run_shows_the_claude_changes_and_the_merge(main, fake, monkeypatch, capsys):
	worktree = build(main, fake, "legacy", "ended")
	legacyFile(worktree, "result-1.json", {"result": "STATUS: done\n\nall green", "num_turns": 9}, 30, fake.now())
	legacyFile(
		worktree, "merge-1.json", {"decision": "merged", "into": "main", "commit": "abc1234def567"}, 5, fake.now()
	)
	_module, out = runResult(main, monkeypatch, capsys, TOPIC)
	assert "WORKER-STATUS: done" in out and "Changes to .claude / .mcp.json:" in out
	assert "Automatic merge: merged into main as abc1234def" in out
	assert out.rstrip().endswith("all green")


def test_worker_status_of_an_archived_topic_prints_the_archive_s_status(main, fake, monkeypatch, capsys):
	archive = os.path.join(hookonly.stateDir(str(main), "archive"), TOPIC)
	legacyRun(main, archive, "ended", fake)
	out = workerStatus(main, monkeypatch, capsys)
	assert "PHASE: ended" in out and "archived or removed" in out


def test_worker_status_without_worktree_or_archive_fails(main, fake, monkeypatch, capsys):
	with pytest.raises(SystemExit) as ended:
		workerStatus(main, monkeypatch, capsys)
	assert ended.value.code == 1


# -- the pane's JSON (schema 1) ------------------------------------------------------------------------


def test_the_pane_json_keeps_every_old_field_and_only_adds_display_ones(main, fake, capsys):
	build(main, fake, "legacy", "running")
	wt_controls.main(["wt_controls.py", str(main), "--local", "off"])
	out = json.loads(capsys.readouterr().out)
	assert out["schema"] == wt_controls.SCHEMA == 1 and set(out) <= OLD_TOP_KEYS | DISPLAY_TOP_KEYS
	(row,) = out["controls"]
	assert set(row) - OLD_ROW_KEYS == DISPLAY_KEYS
	assert {k: row[k] for k in row if k in OLD_ROW_KEYS} == {
		"topic": TOPIC,
		"action": "stop",
		"run": 1,
		"held": False,
		"auto": {"ok": False, "why": "already running (run 1)"},
	}
	assert out["local_blocker"] is None  # a running worker keeps its own mode (migration step 4)
	assert row["phase"] == "running" and row["human"]["todo"]["button"] == "stop"


def test_a_status_that_fails_offers_nothing_and_says_why(main, fake, monkeypatch):
	"""Since migration step 4 the row's decisions read the status: without one there is nothing to send."""
	build(main, fake, "legacy", "running")

	def broken(*args, **kwargs):
		raise OSError("disk gone")

	monkeypatch.setattr(runstate, "status", broken)
	row = wt_controls.control(main, TOPIC)
	assert "action" not in row and "phase" not in row and "human" not in row
	assert row["status_error"] == row["why"] == "run status unreadable: disk gone"
	assert row["auto"] == {"ok": False, "why": "run status unreadable: disk gone"}


def test_the_decisions_read_the_same_status_as_the_line(main, fake):
	"""Migration step 4: a record-only run (no start file) that reads running in the line is stopped by the row."""
	build(main, fake, "record", "running")
	row = wt_controls.control(main, TOPIC)
	assert row["phase"] == "running" and row["action"] == "stop"


# -- language ----------------------------------------------------------------------------------------


def test_the_pane_line_follows_the_language_setting_and_its_local_override(main, fake, tmp_path, monkeypatch):
	build(main, fake, "record", "running")
	assert "running" in wt_controls.control(main, TOPIC)["human"]["line"]  # home default en
	config = config_fixtures.copyConfig(os.path.join(os.path.dirname(HOOKS), "config"), tmp_path / "config")
	(config / "local.json").write_text(json.dumps({"budget": {"run_state": {"status_language": "zh-TW"}}}))
	config_fixtures.useConfig(monkeypatch, config)
	line = wt_controls.control(main, TOPIC)["human"]["line"]
	assert "執行中" in line and line == runstate.human(runstate.status(str(main), TOPIC), "zh-TW")["line"]


def test_the_agent_views_stay_english_whatever_the_language(main, fake, tmp_path, monkeypatch, capsys):
	build(main, fake, "record", "running")
	config = config_fixtures.copyConfig(os.path.join(os.path.dirname(HOOKS), "config"), tmp_path / "config")
	(config / "local.json").write_text(json.dumps({"budget": {"run_state": {"status_language": "zh-TW"}}}))
	config_fixtures.useConfig(monkeypatch, config)
	_module, out = runResult(main, monkeypatch, capsys, TOPIC)
	assert "run 1 running (since" in out and "執行中" not in out

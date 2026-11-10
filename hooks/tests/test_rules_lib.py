"""The shared helpers new home code uses (hooks/RULES.md): clock.py, statefile.py, eventlog.py, hookio.py
and report_header.py.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_rules_lib.py -q -p no:cacheprovider'
"""

import fcntl
import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import config_fixtures  # noqa: E402
import eventlog  # noqa: E402
import hookio  # noqa: E402
import report_header  # noqa: E402
import statefile  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")

TAIPEI = timezone(timedelta(hours=8))
FIXED = datetime(2026, 11, 4, 10, 0, 0, tzinfo=TAIPEI)

FAKE_HOOK = """
import sys
sys.path.insert(0, {hooks!r})
import hookio

def main(hook):
	if hook.data.get("crash"):
		raise RuntimeError(hook.data["crash"])
	if hook.data.get("refuse"):
		return hookio.refuse(hook.event, "fake: no")
	return None

sys.exit(hookio.run("fake-hook", main, {policy!r}))
"""


@pytest.fixture
def fixedClock():
	clock.useSource(lambda: FIXED)
	yield FIXED
	clock.useSource(None)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	return tmp_path


# -- clock ---------------------------------------------------------------------------------------


def test_the_stored_form_is_local_iso_with_an_offset(fixedClock):
	assert clock.isoNow() == "2026-11-04T10:00:00+08:00"
	assert clock.parse("2026-11-04T10:00:00+08:00") == FIXED
	assert clock.isStamp("2026-11-04T10:00:00+08:00")


@pytest.mark.parametrize("value", ["2026-11-04T10:00:00", 1762221600.0, "yesterday", None])
def test_naive_epoch_and_other_values_are_not_stamps(value):
	assert not clock.isStamp(value)


def test_the_system_clock_is_aware():
	assert clock.now().tzinfo is not None


def test_a_naive_source_is_refused():
	clock.useSource(lambda: datetime(2026, 11, 4))
	try:
		with pytest.raises(ValueError):
			clock.now()
	finally:
		clock.useSource(None)


# -- statefile -------------------------------------------------------------------------------------


def test_write_json_is_whole_and_leaves_no_temporary_file(tmp_path, fixedClock):
	path = tmp_path / "d" / "x.json"
	statefile.writeJson(str(path), {"schema": 1, "n": 1, "started_at": clock.isoNow()})
	statefile.writeJson(str(path), {"schema": 1, "n": 2, "started_at": clock.isoNow()})
	assert json.loads(path.read_text())["n"] == 2
	assert os.listdir(tmp_path / "d") == ["x.json"]


@pytest.mark.parametrize(
	"obj",
	[
		{"n": 1},
		{"schema": "1"},
		{"schema": True},
		[1],
		{"schema": 1, "t": 1762221600},
		{"schema": 1, "ended_at": "2026-11-04T10:00:00"},
	],
)
def test_objects_without_schema_or_with_naive_time_are_refused(tmp_path, obj):
	with pytest.raises(statefile.StateError):
		statefile.writeJson(str(tmp_path / "x.json"), obj)
	with pytest.raises(statefile.StateError):
		statefile.appendJsonl(str(tmp_path / "x.jsonl"), obj)
	assert os.listdir(tmp_path) == []


def test_append_writes_one_line_per_object(tmp_path):
	path = tmp_path / "log.jsonl"
	statefile.appendJsonl(str(path), {"schema": 1, "text": "a\nb"})
	statefile.appendJsonl(str(path), {"schema": 1, "text": "c"})
	assert [json.loads(l)["text"] for l in path.read_text().splitlines()] == ["a\nb", "c"]


def test_an_unknown_schema_version_reads_as_absent(tmp_path):
	path = tmp_path / "x.json"
	statefile.writeJson(str(path), {"schema": 2})
	assert statefile.readJson(str(path), 1) is None
	assert statefile.readJson(str(tmp_path / "missing.json"), 1) is None
	(tmp_path / "log.jsonl").write_text('{"schema": 1, "a": 1}\n{"schema": 2}\ntorn{\n')
	assert statefile.readJsonl(str(tmp_path / "log.jsonl"), 1) == [{"schema": 1, "a": 1}]


def test_registered_kinds_get_their_path_and_schema(tmp_path, fixedClock):
	path = statefile.write(str(tmp_path), "hook-error", {"schema": 1, "count": 1}, "some-hook")
	assert path == os.path.join(str(tmp_path), ".hook-only", "hook-errors", "some-hook.json")
	assert statefile.read(str(tmp_path), "hook-error", "some-hook") == {"schema": 1, "count": 1}


@pytest.mark.parametrize(
	"call",
	[
		lambda root: statefile.write(root, "no-such-kind", {"schema": 1}),
		lambda root: statefile.write(root, "hook-error", {"schema": 2}, "x"),
		lambda root: statefile.write(root, "hook-error", {"schema": 1}),
		lambda root: statefile.write(root, "hook-error", {"schema": 1}, "../escape"),
		lambda root: statefile.write(root, "event-log", {"schema": 1}, "x"),
	],
)
def test_unregistered_kinds_wrong_versions_and_bad_ids_are_refused(tmp_path, call):
	with pytest.raises(statefile.StateError):
		call(str(tmp_path))


def test_a_multi_writer_kind_is_locked_and_a_single_writer_kind_refuses_a_lock(tmp_path):
	with statefile.locked(str(tmp_path), "run-record", "1") as path:
		assert path == statefile.pathOf(str(tmp_path), "run-record", "1") + ".lock"
		other = os.open(path, os.O_RDWR)
		try:
			with pytest.raises(BlockingIOError):
				fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
		finally:
			os.close(other)
	other = os.open(path, os.O_RDWR)
	try:
		fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released when the block ends
	finally:
		os.close(other)
	with pytest.raises(statefile.StateError):
		statefile.lockPath(str(tmp_path), "event-log")


def test_a_home_kind_lives_in_the_home_state_and_takes_no_checkout(tmp_path, fixedClock):
	stateDir = statefile.homeStateDir()  # conftest: a scratch directory per test
	path = statefile.write(None, "quota-usage", {"schema": 1, "at": 1.0})
	assert path == os.path.join(stateDir, "quota", "usage.json") == statefile.pathOf(None, "quota-usage")
	assert statefile.read(None, "quota-usage") == {"schema": 1, "at": 1.0}
	with pytest.raises(statefile.StateError):
		statefile.pathOf(str(tmp_path), "quota-usage")  # not a checkout's
	with pytest.raises(statefile.StateError):
		statefile.pathOf(None, "hook-error", "x")  # a checkout kind needs its checkout


def test_without_the_test_redirect_the_home_state_is_this_tree_s():
	"""A fresh load of statefile: conftest replaced homeStateDir on the imported one."""
	spec = importlib.util.spec_from_file_location("statefile_fresh", statefile.__file__)
	fresh = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(fresh)
	assert fresh.homeStateDir() == os.path.join(homepath.HOME_ROOT, "state")
	assert fresh.pathOf(None, "quota-usage") == os.path.join(homepath.HOME_ROOT, "state", "quota", "usage.json")


@pytest.mark.parametrize("entry", [{"multi_writer": True}, {"lock": "{id}.json.lock"}, {"multi_writer": "yes"},
	{"location": "elsewhere"}, {"location": "home", "strict": True}])
def test_a_registry_entry_with_an_inconsistent_lock_is_refused(tmp_path, monkeypatch, entry):
	config = config_fixtures.copyConfig(os.path.dirname(statefile.homeconfig.__file__), tmp_path / "config")
	config_fixtures.useConfig(monkeypatch, config)
	path = statefile.homeconfig.path(statefile.REGISTRY)
	registry = json.loads(open(path).read())
	registry["kinds"]["hook-error"].update(entry)
	open(path, "w").write(json.dumps(registry))
	with pytest.raises(statefile.StateError):
		statefile.kind("hook-error")


# -- eventlog -------------------------------------------------------------------------------------


def test_an_event_line_has_the_fixed_frame(tmp_path, fixedClock):
	eventlog.emit(
		str(tmp_path),
		"hook-failed",
		"some-hook",
		{"hook": "h", "policy": "record", "error": "e"},
		topic="cash-account",
		run=2,
		agent_id=None,
	)
	assert eventlog.read(str(tmp_path)) == [
		{
			"schema": 1,
			"t": "2026-11-04T10:00:00+08:00",
			"event": "hook-failed",
			"source": "some-hook",
			"topic": "cash-account",
			"run": 2,
			"data": {"hook": "h", "policy": "record", "error": "e"},
		}
	]
	assert eventlog.read(str(tmp_path), source="other") == []


@pytest.mark.parametrize(
	"args, ids",
	[
		(("not-registered", "src", {}), {}),
		(("HookFailed", "src", {}), {}),
		(("hook-failed", "src", {"hook": "h"}), {}),
		(("hook-failed", "Bad Source", {"hook": "h", "policy": "p", "error": "e"}), {}),
		(("hook-failed", "src", {"hook": "h", "policy": "p", "error": "e"}), {"n": 1}),
	],
)
def test_unregistered_events_missing_data_and_bad_frames_are_refused(tmp_path, args, ids):
	with pytest.raises(eventlog.EventError):
		eventlog.emit(str(tmp_path), *args, **ids)
	assert not (tmp_path / ".hook-only").exists()


# -- report_header ---------------------------------------------------------------------------------


def test_the_header_is_the_leading_key_lines_only():
	text = "\n\nSTATUS: DONE\nFIX-QA: F1\nFIX-QA: F2\n\nVERDICT: APPROVE in the body\n"
	assert report_header.parse(text) == {"STATUS": ["DONE"], "FIX-QA": ["F1", "F2"]}
	assert report_header.value(text, "STATUS", ("DONE", "BLOCKED")) == "DONE"


@pytest.mark.parametrize(
	"text",
	["Done.\nSTATUS: DONE", "STATUS: done", "STATUS: DONE\nSTATUS: DONE", "", None, "status: DONE", "  STATUS: DONE"],
)
def test_a_missing_repeated_or_unknown_value_is_none(text):
	assert report_header.value(text, "STATUS", ("DONE", "BLOCKED")) is None


@pytest.mark.parametrize(
	"text, expected",
	[
		("\n\nSTATUS: DONE   \nFIX-QA: F1\n", ("STATUS", "DONE")),
		("FIX-QA: F1\nSTATUS: DONE", ("FIX-QA", "F1")),
		("STATUS:DONE", None),
		("STATUS:  DONE", None),
		("STATUS:\tDONE", None),
		("  STATUS: DONE", None),
		("STATUS: ", None),
		("Done.\nSTATUS: DONE", None),
		("", None),
		(None, None),
	],
)
def test_the_leading_line_counts_only_when_written_exactly(text, expected):
	assert report_header.leading(text) == expected


# -- hookio ---------------------------------------------------------------------------------------


def fakeHook(tmp_path, policy, data, raw=None):
	script = tmp_path / f"fake-{policy}.py"
	script.write_text(FAKE_HOOK.format(hooks=os.path.join(RUN_HOOKS, "lib"), policy=policy))
	env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), "HOME": str(tmp_path / "home")}
	return subprocess.run(
		[sys.executable, str(script)],
		input=raw if raw is not None else json.dumps(data),
		capture_output=True,
		text=True,
		timeout=60,
		env=env,
	)


@pytest.mark.parametrize("event, key", [("PreToolUse", "hookSpecificOutput"), ("Stop", "decision")])
def test_a_guard_that_crashes_refuses_the_action(tmp_path, event, key):
	result = fakeHook(tmp_path, "guard", {"hook_event_name": event, "crash": "boom"})
	assert result.returncode == 0, result.stderr
	out = json.loads(result.stdout)
	assert key in out and "fake-hook: RuntimeError: boom — blocked (fail closed)" in json.dumps(out, ensure_ascii=False)
	assert [e["data"]["policy"] for e in eventlog.read(str(tmp_path))] == ["guard"]


@pytest.mark.parametrize(
	"event, owner",
	[("Stop", {"session_id": "s1"}), ("SubagentStop", {"session_id": "s1", "agent_id": "a1", "agent_type": "qa"})],
)
def test_a_crashing_stop_guard_blocks_once_then_lets_the_stop_through(tmp_path, event, owner):
	data = {"hook_event_name": event, "crash": "boom", **owner}
	first = json.loads(fakeHook(tmp_path, "guard", data).stdout)
	assert first["decision"] == "block" and "blocked (fail closed)" in first["reason"]
	second = fakeHook(tmp_path, "guard", data)
	assert second.returncode == 0, second.stderr
	out = json.loads(second.stdout)
	assert "decision" not in out and "let through" in out["systemMessage"] and "boom" in out["systemMessage"]
	assert [e["data"]["outcome"] for e in eventlog.read(str(tmp_path), event="hook-failed")] == [
		"blocked",
		"let-through",
	]


def test_a_stop_block_is_once_per_distinct_error_and_per_agent(tmp_path):
	base = {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_type": "qa"}
	assert "decision" in json.loads(fakeHook(tmp_path, "guard", {**base, "agent_id": "a1", "crash": "boom"}).stdout)
	assert "decision" in json.loads(fakeHook(tmp_path, "guard", {**base, "agent_id": "a1", "crash": "other"}).stdout)
	assert "decision" in json.loads(fakeHook(tmp_path, "guard", {**base, "agent_id": "a2", "crash": "boom"}).stdout)
	assert "decision" not in json.loads(fakeHook(tmp_path, "guard", {**base, "agent_id": "a1", "crash": "boom"}).stdout)


def test_a_stop_guard_that_cannot_record_the_block_lets_the_stop_through(tmp_path):
	script = tmp_path / "fake-guard.py"
	script.write_text(FAKE_HOOK.format(hooks=os.path.join(RUN_HOOKS, "lib"), policy="guard"))
	env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
	result = subprocess.run(
		[sys.executable, str(script)],
		input=json.dumps({"hook_event_name": "Stop", "session_id": "s1", "crash": "boom"}),
		capture_output=True,
		text=True,
		timeout=60,
		env=env,
	)
	assert result.returncode == 0, result.stderr
	assert "let through" in json.loads(result.stdout)["systemMessage"]


def test_a_guard_on_unreadable_input_exits_2(tmp_path):
	result = fakeHook(tmp_path, "guard", None, raw="not json")
	assert result.returncode == 2 and "blocked (fail closed)" in result.stderr and result.stdout == ""


def test_a_guard_refusal_uses_the_event_s_official_shape(tmp_path):
	out = json.loads(fakeHook(tmp_path, "guard", {"hook_event_name": "PreToolUse", "refuse": True}).stdout)
	assert out == {
		"hookSpecificOutput": {
			"hookEventName": "PreToolUse",
			"permissionDecision": "deny",
			"permissionDecisionReason": "fake: no",
		}
	}
	assert fakeHook(tmp_path, "guard", {"hook_event_name": "PreToolUse"}).stdout == ""


def test_a_record_keeping_failure_is_shown_once_per_distinct_error(tmp_path):
	first = fakeHook(tmp_path, "record", {"hook_event_name": "PostToolUse", "crash": "disk full"})
	assert first.returncode == 0, first.stderr
	assert (
		"fake-hook: record-keeping failed and was skipped (RuntimeError: disk full)"
		in json.loads(first.stdout)["systemMessage"]
	)
	again = fakeHook(tmp_path, "record", {"hook_event_name": "PostToolUse", "crash": "disk full"})
	assert again.returncode == 0 and again.stdout == ""
	other = fakeHook(tmp_path, "record", {"hook_event_name": "PostToolUse", "crash": "other"})
	assert "other" in json.loads(other.stdout)["systemMessage"]
	assert len(eventlog.read(str(tmp_path), event="hook-failed")) == 2
	assert statefile.read(str(tmp_path), "hook-error", "fake-hook")["count"] == 1


def test_a_record_keeping_failure_without_a_checkout_still_tells_the_person(tmp_path):
	script = tmp_path / "fake-record.py"
	script.write_text(FAKE_HOOK.format(hooks=os.path.join(RUN_HOOKS, "lib"), policy="record"))
	env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
	result = subprocess.run(
		[sys.executable, str(script)],
		input=json.dumps({"crash": "x"}),
		capture_output=True,
		text=True,
		timeout=60,
		env=env,
	)
	message = json.loads(result.stdout)["systemMessage"]
	assert result.returncode == 0 and "recording that failed too" in message and "CLAUDE_PROJECT_DIR" in message


def test_the_root_is_project_dir_never_cwd(checkout, monkeypatch):
	hook = hookio.read(io.StringIO('{"hook_event_name": "Stop", "cwd": "/elsewhere"}'))
	assert hook.root() == os.path.realpath(str(checkout))
	monkeypatch.delenv("CLAUDE_PROJECT_DIR")
	with pytest.raises(hookio.RootError):
		hook.root()


def test_main_root_is_the_main_checkout_of_a_worktree(tmp_path, monkeypatch):
	main = tmp_path / "main"
	subprocess.run(["git", "init", "-q", str(main)], check=True)
	subprocess.run(
		[
			"git",
			"-C",
			str(main),
			"-c",
			"user.name=t",
			"-c",
			"user.email=t@t",
			"commit",
			"-q",
			"--allow-empty",
			"-m",
			"x",
		],
		check=True,
	)
	subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(tmp_path / "wt")], check=True)
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "wt"))
	hook = hookio.read(io.StringIO("{}"))
	assert hook.root() == os.path.realpath(str(tmp_path / "wt"))
	assert hook.mainRoot() == os.path.realpath(str(main))


def test_roles_come_from_one_signal_each(checkout, monkeypatch):
	monkeypatch.delenv("CLAUDE_WORKER_MINUTES", raising=False)
	assert hookio.Hook({}).role() == "main"
	assert hookio.Hook({"agent_id": "a1", "agent_type": "qa"}).role() == "subagent:qa"
	monkeypatch.setenv("CLAUDE_WORKER_MINUTES", "30")
	assert hookio.Hook({}).role() == "worker"


def test_context_and_warnings_have_the_official_shape():
	assert hookio.context("UserPromptSubmit", "x") == {
		"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x"}
	}
	assert hookio.warnUser("x") == {"systemMessage": "x"}
	with pytest.raises(ValueError):
		hookio.refuse("SessionStart", "x")

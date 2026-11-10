"""run-worker.sh with a stub claude (run record migration step 2, dual write): worker-control.py starts it as in
a real launch, the stub stands in for claude. Every way out writes today's exit file and the record's exit;
claude runs in the background with SIGINT reset, the INT/TERM trap forwards the signal, and the wrapper waits
until claude has really exited and takes claude's exit code; the heartbeat beats while the wrapper lives and its
loop is gone afterwards; a new script copied over the running one doesn't change the run; at the end the
record's story is compared with today's files. No real claude, no network; stubs run well under a second.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_run_worker.py -q -p no:cacheprovider'
"""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
CONFIG = os.path.join(os.path.dirname(HOOKS), "config")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
sys.path.insert(0, CONFIG)
import eventlog  # noqa: E402
import homeconfig  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402

TOPIC = "cash-account"
INTERVAL_SECONDS = 1  # heartbeat interval of these runs (a local.json override of home.json budget.run_state)
WAIT_SECONDS = 8
STUB_CLAUDE = r"""#!/usr/bin/env python3
import json, os, signal, sys, time
stub = os.environ["STUB_DIR"]
def log(text):
    with open(os.path.join(stub, "claude.log"), "a") as f:
        f.write(f"{time.time():.6f} {os.getpid()} {text}\n")
if sys.argv[1:2] == ["auth"]:
    sys.exit(int(os.environ.get("STUB_AUTH_EXIT", "0")))
ignored = int(next(l for l in open("/proc/self/status") if l.startswith("SigIgn")).split()[1], 16)
compact = any(a.startswith("/compact") for a in sys.argv)
got = []
for sig in (signal.SIGINT, signal.SIGTERM):
    if not ignored & (1 << (sig - 1)):  # like a C program: a signal ignored on entry stays ignored
        signal.signal(sig, lambda n, f: got.append(n))
log(f"start compact={compact} int-ignored={bool(ignored & 2)}")
end = time.time() + (0.05 if compact else float(os.environ.get("STUB_SECONDS", "0.3")))
while time.time() < end and not got:
    time.sleep(0.02)
if got:
    time.sleep(0.4)  # claude takes a moment to wind down after a signal
    log(f"exit 7 after {signal.Signals(got[0]).name}")
    sys.exit(7)
print(json.dumps({"result": "STATUS: done\nall green", "total_cost_usd": 0.6 if compact else 0.5}))
log("exit 0")
"""
FAKE_HOOK = """import os, sys
with open(os.environ["FAKE_HOOK_LOG"], "a") as f:
	f.write(os.path.basename(sys.argv[0]) + "\\n")
"""
OVERWRITE = "#!/usr/bin/env bash\n" + 'touch "$OVERWRITE_MARKER"\n' * 4000


def git(*args):
	subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=30)


def homeTree(tmp_path):
	"""A copy of the home hooks (top level and the role directories, tests aside) and config (home.json,
	registry/, the loader): the overwrite test changes its run-worker.sh."""
	root = tmp_path / "homecopy"
	for folder in ["", *homepath.ROLE_DIRS]:
		(root / "hooks" / folder).mkdir(parents=True, exist_ok=True)
		for entry in os.listdir(os.path.join(HOOKS, folder)):
			path = os.path.join(HOOKS, folder, entry)
			if os.path.isfile(path):
				shutil.copy2(path, root / "hooks" / folder / entry)
	config_fixtures.copyConfig(CONFIG, root / "config")
	return root


@pytest.fixture
def world(tmp_path, monkeypatch):
	"""Main checkout with the topic's worktree (local mode) and estimate, a fake home whose run-worker.sh links
	to a copy of the home tree, a stub claude, fake follow-up hooks, and settings with a 1 s heartbeat."""
	main = tmp_path / "proj" / "main"
	git("init", "-q", "-b", "main", str(main))
	git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / f"{TOPIC}.json").write_text(json.dumps({"minutes": 10, "est_usd": 2}))
	worktree = tmp_path / "proj" / "worktrees" / TOPIC
	git("-C", str(main), "worktree", "add", "-q", "-b", f"feature/x/{TOPIC}", str(worktree))
	(worktree / ".hook-only").mkdir()
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	tree = homeTree(tmp_path)
	config = tree / "config"  # the wrapper links into the copy, so it reads the copy's config
	(config / "local.json").write_text(
		json.dumps({"budget": {"run_state": {"heartbeat_interval_seconds": INTERVAL_SECONDS}}})
	)
	home = tmp_path / "home"
	(home / ".local" / "bin").mkdir(parents=True)
	(home / ".local" / "bin" / "claude").write_text(STUB_CLAUDE)
	(home / ".local" / "bin" / "claude").chmod(0o755)
	hooks = home / ".claude" / "hooks"
	(hooks / "worker").mkdir(parents=True)
	(hooks / "gates").mkdir()
	(hooks / "worker" / "run-worker.sh").symlink_to(tree / "hooks" / "worker" / "run-worker.sh")
	for name in ("worker/wt_events.py", "gates/verify.py", "gates/verifier.py", "worker/auto-merge.py",
			"worker/worker-control.py", "worker/wt_queue.py"):
		(hooks / name).write_text(FAKE_HOOK)
	stub = tmp_path / "stub"
	stub.mkdir()
	env = {
		**os.environ,
		"HOME": str(home),
		"STUB_DIR": str(stub),
		"FAKE_HOOK_LOG": str(tmp_path / "hooks.log"),
		"OVERWRITE_MARKER": str(tmp_path / "overwritten"),
	}
	config_fixtures.useConfig(monkeypatch, config)
	return {"main": main, "worktree": worktree, "tree": tree, "stub": stub, "env": env, "tmp": tmp_path}


def wt(world, prompt, **env):
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": prompt, "cwd": str(world["main"])}),
		capture_output=True,
		text=True,
		timeout=30,
		env={**world["env"], **env},
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def workerPid(world):
	state = world["main"] / ".hook-only" / "workers" / f"{TOPIC}.json"
	return json.loads(state.read_text())["pid"]


def runsDir(world):
	return world["worktree"] / ".hook-only" / "runs"


def alive(pid):
	try:
		with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
			return f.read().rsplit(")", 1)[1].split()[0] != "Z"
	except OSError:
		return False


def waitFor(check, seconds=WAIT_SECONDS):
	deadline = time.time() + seconds
	while time.time() < deadline:
		if check():
			return True
		time.sleep(0.05)
	return check()


def claudeLog(world):
	path = world["stub"] / "claude.log"
	return path.read_text().splitlines() if path.exists() else []


def launched(world, **env):
	"""`wt launch` as typed; (wrapper pid, run) once the stub claude runs."""
	wt(world, f"wt launch {TOPIC}", **env)
	pid = workerPid(world)
	assert waitFor(lambda: any(" start compact=False" in line for line in claudeLog(world)))
	return pid, 1


def finished(world, pid, run=1):
	assert waitFor(lambda: not alive(pid)), "the wrapper did not end"
	record, problem = runstate.readRecord(str(world["worktree"]), run)
	assert record is not None, problem
	return record


def group(pgid):
	"""Live processes of the wrapper's process group (zombies aside)."""
	found = []
	for name in os.listdir("/proc"):
		if name.isdigit():
			try:
				with open(f"/proc/{name}/stat", encoding="utf-8") as f:
					fields = f.read().rsplit(")", 1)[1].split()
			except OSError:
				continue
			if int(fields[2]) == pgid and fields[0] != "Z":
				found.append(int(name))
	return found


def stamp(text):
	return runstate.parseStamp(text).timestamp()


def claudeExitTime(world):
	return max(float(line.split()[0]) for line in claudeLog(world) if " exit " in line)


def diverged(world):
	return eventlog.read(str(world["worktree"]), event="status-diverged")


def test_a_normal_end_writes_both_exits_the_heartbeat_and_one_story(world):
	pid, run = launched(world)
	record = finished(world, pid)
	runs = runsDir(world)
	assert (runs / "exit-1").read_text().startswith("0 ")
	assert record["exit"]["reason"] == "completed" and record["exit"]["code"] == 0
	assert record["process"]["wrapper_pid"] == pid and record["process"]["pgid"] == pid
	assert record["process"]["exit_code"] == 0 and record["process"]["exited_at"]
	assert record["started_by"] == "person" and record["prompt_kind"] == "feature-flow"
	assert record["main_head_at_start"] == (runs / "main-1").read_text().strip()
	assert [c["kind"] for c in record["claude"]["invocations"]] == ["main", "final-compact"]
	assert [c["cost_usd"] for c in record["claude"]["invocations"]] == [0.5, 0.6]
	assert record["claude"]["status"] == "done" and record["claude"]["outcome"] == "status"
	assert record["steps"]["verify"] == {**record["steps"]["verify"], "state": "skipped", "skip_reason": "no-spec"}
	beat = statefile.read(str(world["worktree"]), runstate.HEARTBEAT, run)
	assert beat["count"] >= 1 and beat["interval_seconds"] == INTERVAL_SECONDS
	assert waitFor(lambda: group(pid) == [], 0.3), "the heartbeat loop outlived the wrapper"
	time.sleep(INTERVAL_SECONDS * 1.5)
	assert statefile.read(str(world["worktree"]), runstate.HEARTBEAT, run)["count"] == beat["count"]
	assert diverged(world) == []
	assert runstate.compareStories(str(world["main"]), TOPIC, run) == []
	assert (world["tmp"] / "hooks.log").read_text().splitlines() == ["wt_events.py", "auto-merge.py", "wt_queue.py"]


TICK_HOOK = """import json, os, sys
runs = os.path.join(sys.argv[2], "..", "worktrees", "cash-account", ".hook-only", "runs")
record = json.load(open(os.path.join(runs, "1.json")))
with open(os.environ["FAKE_HOOK_LOG"], "a") as f:
	f.write("tick exit=" + str((record.get("exit") or {}).get("reason")) + "\\n")
"""


def test_the_record_s_exit_is_written_before_the_queue_tick(world):
	"""Migration step 4: the queue's tick right after exit-<run> reads runstate.status, so the record's exit is
	written in the same moment as the file (run-steps ended), not only by the EXIT trap after the tick."""
	(world["main"].parent / "worktrees").mkdir(exist_ok=True)
	(world["tmp"] / "home" / ".claude" / "hooks" / "worker" / "wt_queue.py").write_text(TICK_HOOK)
	pid, run = launched(world)
	record = finished(world, pid)
	assert record["exit"]["reason"] == "completed"
	assert (world["tmp"] / "hooks.log").read_text().splitlines()[-1] == "tick exit=completed"
	assert [e["data"]["reason"] for e in eventlog.read(str(world["worktree"]), event="run-ended")] == ["completed"]


def test_wt_stop_int_to_the_group_waits_for_claude_and_keeps_its_code(world):
	pid, run = launched(world, STUB_SECONDS="5")
	out = wt(world, f"wt stop {TOPIC}")
	assert "stopped" in out["hookSpecificOutput"]["additionalContext"]
	record = finished(world, pid)
	log = claudeLog(world)
	assert "int-ignored=False" in log[0] and any("exit 7 after SIGINT" in line for line in log)
	exitFile = runsDir(world) / "exit-1"
	assert exitFile.read_text().startswith("7 ")
	assert os.stat(exitFile).st_mtime > claudeExitTime(world)
	assert record["exit"] == {**record["exit"], "reason": "stopped", "code": 7}
	assert stamp(record["exit"]["ended_at"]) >= int(claudeExitTime(world))
	assert record["process"]["exit_signal"] == "INT" and record["stop"]["by"] == "person"
	assert group(pid) == []
	assert diverged(world) == []


def test_term_to_the_wrapper_pid_only_reaches_claude_through_the_trap(world):
	pid, run = launched(world, STUB_SECONDS="5")
	os.kill(pid, signal.SIGTERM)
	record = finished(world, pid)
	assert any("exit 7 after SIGTERM" in line for line in claudeLog(world))
	assert (runsDir(world) / "exit-1").read_text().startswith("7 ")
	assert os.stat(runsDir(world) / "exit-1").st_mtime > claudeExitTime(world)
	assert record["exit"]["reason"] == "stopped" and record["exit"]["code"] == 7
	assert record["process"]["exit_signal"] == "TERM"
	assert group(pid) == []
	# No stopped-<run> file (not `wt stop`): today's files say completed, the record says stopped.
	assert [e["data"]["field"] for e in diverged(world)] == ["outcome.reason"]


def test_not_logged_in_writes_90_and_the_record_exit(world):
	wt(world, f"wt launch {TOPIC}", STUB_AUTH_EXIT="1")
	record = finished(world, workerPid(world))
	assert (runsDir(world) / "exit-1").read_text().startswith("90 ")
	assert record["exit"]["reason"] == "not-logged-in" and record["exit"]["code"] == 90
	assert claudeLog(world) == []


def loadWorkerControl():
	spec = importlib.util.spec_from_file_location("worker_control", os.path.join(HOOKS, "worker", "worker-control.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def startDirect(world, monkeypatch, **env):
	"""worker-control's start_run itself (est-1.json, the record, the wrapper in its own session) with variables a
	launch through the prompt can't have (a bad flow mode); returns the wrapper's pid, a child of this process."""
	control = loadWorkerControl()
	control.WRAPPER = world["tree"] / "hooks" / "worker" / "run-worker.sh"
	for name in ("HOME", "STUB_DIR", "FAKE_HOOK_LOG", "OVERWRITE_MARKER"):
		monkeypatch.setenv(name, world["env"][name])
	for name, value in env.items():
		monkeypatch.setenv(name, value)
	_run, pid = control.start_run(
		world["main"],
		world["worktree"],
		TOPIC,
		world["tmp"] / "state.json",
		"new",
		"11111111-2222-3333-4444-555555555555",
		f"/feature-flow {TOPIC}",
		10,
		2,
		3,
		13,
		"",
		{},
		{},
		{"started_by": "person", "prompt_kind": "feature-flow"},
	)
	return pid


def reap(pid):
	"""The wrapper's exit status once it ended (it is this process's child)."""
	deadline = time.time() + WAIT_SECONDS
	while time.time() < deadline:
		done, status = os.waitpid(pid, os.WNOHANG)
		if done:
			return os.waitstatus_to_exitcode(status)
		time.sleep(0.05)
	raise AssertionError("the wrapper did not end")


def test_a_bad_flow_mode_starts_nothing(world, monkeypatch):
	# start_run reads the topic's mode (homeconfig.flowMode) before it writes anything; the wrapper's own
	# config-error exit is test_run_steps.py's trap test. A record that holds no mode (no variable is a source).
	(world["worktree"] / ".hook-only" / "flow-mode").write_text("bogus\n")
	with pytest.raises(homeconfig.ConfigError, match="bogus"):
		startDirect(world, monkeypatch)
	assert not runsDir(world).exists() or os.listdir(runsDir(world)) == []


def test_a_new_script_copied_over_the_running_one_changes_nothing(world, monkeypatch):
	pid = startDirect(world, monkeypatch, STUB_SECONDS="1")
	assert waitFor(lambda: any(" start compact=False" in line for line in claudeLog(world)))
	script = world["tree"] / "hooks" / "worker" / "run-worker.sh"
	inode = os.stat(script).st_ino
	with open(script, "w") as f:  # in place, as cp does: the running shell reads this inode
		f.write(OVERWRITE)
	assert os.stat(script).st_ino == inode
	assert reap(pid) == 0
	record, _ = runstate.readRecord(str(world["worktree"]), 1)
	assert (runsDir(world) / "exit-1").read_text().startswith("0 ")
	assert record["exit"]["reason"] == "completed"
	assert not (world["tmp"] / "overwritten").exists()


def test_a_torn_record_is_left_as_it_was_and_no_wrapup_step_runs(world):
	"""Every write refuses a torn record, and the wrap-up runs no step on it (A2, fail closed: what was done can't
	be known): no exit-<n>, no auto-merge, no queue tick; the person's wrapup-failed event names the record."""
	runs = runsDir(world)
	runs.mkdir(parents=True)
	(runs / "est-1.json").write_text(json.dumps({"minutes": 10, "usd": 2, "cap": 3, "cap_minutes": 13}))
	torn = '{"schema": 1, "topic": '
	(runs / "1.json").write_text(torn)  # every write must refuse a torn record, never patch it up
	process = subprocess.Popen(
		[
			"bash",
			str(world["tree"] / "hooks" / "worker" / "run-worker.sh"),
			TOPIC,
			str(world["worktree"]),
			"1",
			"new",
			"11111111-2222-3333-4444-555555555555",
			"13",
			"3",
			"",
			f"/feature-flow {TOPIC}",
		],
		cwd=world["worktree"],
		start_new_session=True,
		env={**world["env"], "WT_MAIN_DIR": str(world["main"])},
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	assert process.wait(timeout=WAIT_SECONDS) == 0
	assert not (runs / "exit-1").exists()
	for name in ("start-1", "main-1", "result-1.json", "compact-1-1.json", "auth-1.log"):
		assert (runs / name).exists(), name
	assert (world["tmp"] / "hooks.log").read_text().splitlines() == ["wt_events.py"]
	events = [json.loads(line) for line in (world["main"] / ".hook-only" / "events.jsonl").read_text().splitlines()]
	assert [e["kind"] for e in events] == ["wrapup-failed"] and "run record can't be read" in events[0]["text"]
	assert "no wrap-up step runs" in (runs / "stderr-1.log").read_text()
	assert (runs / "1.json").read_text() == torn
	assert len(eventlog.read(str(world["worktree"]), event="hook-failed", source="run-steps")) == 1
	assert (runs / "stderr-1.log").read_text().count("the run record was not updated") == 1

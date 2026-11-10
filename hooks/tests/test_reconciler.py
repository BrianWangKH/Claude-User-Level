"""reconciler.py (worker self-repair stages 3a and 3b): the reconciler's judgment, its dry run and its actions.

1. classify over the run states and the process checks: every class for its facts, and the dangerous direction
   locked: a run whose wrapper lives or can't be checked, whose heartbeat is fresh or unreadable, or one a helper
   process still acts on, is never classed for an action (hands-off); a caller in another pid namespace than the
   wrapper's (a sandboxed one), or a record without one, checks nothing.
2. The process checks themselves (namesRun, runnerAlive, liveHelpers) on real processes, a reused pid included.
3. The command: run-steps.py reconcile refuses outside the main checkout's worktrees (`..`, a symlink elsewhere);
   the dry run changes no file but B2's notice, tells B2 once per run, sends no signal; B1 is a note; `wt
   reconcile [--dry-run]` relays it (worker-control.py).
4. The actions: each acting class starts its program (or tells the person) only when every process check says gone,
   once per claim (a held claim, a run that changed under the record's lock: nothing), at-limit told once per run,
   a decided next run started at most step_attempts_max times and then closed; the triggers throttled, starting
   nothing where their state can't be written; a detached start really leaves the caller's session.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_reconciler.py -q -p no:cacheprovider'
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import reconciler  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
import wrapup_stop_baseline  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 8, 0, 0, tzinfo=TAIPEI)
TOPIC = "cash-account"
LIMITS = runstate.settings()
STALE = LIMITS["heartbeat_interval_seconds"] * LIMITS["orphan_missed_beats_count"] + 60
ATTEMPTS = LIMITS["step_attempts_max"]
GONE_PID = 999999999
BASE = {"started_by": "person", "purpose": "build", "flow": "written", "integration_mode": "local",
	"prompt_kind": "feature-flow", "estimate": {"minutes": 40, "usd": 6.0, "cap_minutes": 52, "cap_usd": 7.8}}


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


def git(*args):
	return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=60).stdout


@pytest.fixture
def world(tmp_path):
	main = tmp_path / "proj" / "main"
	git("init", "-q", "-b", "main", str(main))
	git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	worktree = main.parent / "worktrees" / TOPIC
	git("-C", str(main), "worktree", "add", "-q", "-b", f"feature/x/{TOPIC}", str(worktree))
	return {"main": str(main.resolve()), "wt": str(worktree.resolve())}


def launch(wt):
	return runstate.create(wt, {**BASE, "topic": TOPIC, "launch": {"launched_at": clock.isoNow(), "by_pid": 1}})


def change(wt, run, **fields):
	runstate.update(wt, run, lambda r: r.update(fields))


def started(wt, run, pid=GONE_PID, ticks=7, ns=None):
	"""The wrapper started; its pid namespace this test's (the processes it starts share it) unless ns is given."""
	change(wt, run, process={"wrapper_pid": pid, "start_ticks": ticks, "boot_id": None, "started_at": clock.isoNow(),
		"pid_ns": runstate.pidNamespace() if ns is None else ns}, main_head_at_start="65f7802")
	runstate.beat(wt, run, LIMITS["heartbeat_interval_seconds"])


def workerDone(wt, run, status="done"):
	change(wt, run, claude={"ended_at": clock.isoNow(), "outcome": "status", "status": status, "exit_code": 0})


def ended(wt, run, reason="completed", **steps):
	change(wt, run, steps=steps, exit={"ended_at": clock.isoNow(), "code": 0, "reason": reason})


def failedStep(attempt, pid=GONE_PID):
	return {"state": "failed", "attempt": attempt, "runner_pid": pid, "started_at": clock.isoNow(), "reason": "boom"}


def judge(world, wrapper=False, helpers=(), fake=None):
	"""classify with an injected wrapper check (True/False/None) and helper list (None: can't be listed)."""
	facts = reconciler.factsOf(world["main"], world["wt"], now=fake.now() if fake else clock.now(),
		check=lambda process, topic, run: wrapper,
		listHelpers=lambda root, run: None if helpers is None else list(helpers))
	return reconciler.classify(facts, ATTEMPTS, world["main"])


def orphanDone(world, fake):
	"""A run whose worker ended STATUS done and whose wrapper went silent before the wrap-up."""
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	fake.advance(STALE)
	return run


# -- 1. classify ---------------------------------------------------------------------------------------------


def test_no_run_is_nothing(world, fake):
	assert judge(world, fake=fake)["class"] == "nothing"


def test_a_record_that_can_t_be_read_is_unreadable_and_acts_on_nothing(world, fake):
	run = launch(world["wt"])
	with open(statefile.pathOf(world["wt"], runstate.RECORD, run), "w") as f:
		f.write("{ broken")
	judged = judge(world, fake=fake)
	assert judged["class"] == "unreadable" and judged["action"].startswith("nothing")


def test_a_run_of_today_s_files_only_is_not_reconciled(world, fake):
	runs = runstate.runsDir(world["wt"])
	os.makedirs(runs)
	with open(os.path.join(runs, "est-1.json"), "w") as f:
		json.dump({"minutes": 10, "usd": 2, "cap": 3, "cap_minutes": 13}, f)
	assert judge(world, fake=fake)["class"] in ("nothing", "unreadable")
	assert judge(world, fake=fake)["class"] not in reconciler.ACTING


def test_an_orphan_whose_worker_finished_owes_its_wrapup_only_when_every_check_says_gone(world, fake):
	orphanDone(world, fake)
	judged = judge(world, fake=fake)
	assert judged["class"] == "wrapup-owed" and "run-steps.py wrapup" in judged["action"], judged


@pytest.mark.parametrize(
	"wrapper, helpers, heartbeat, why",
	[
		(True, (), "stale", "still runs"),
		(None, (), "stale", "can't be checked"),
		(False, (), "fresh", "heartbeat is fresh"),
		(False, (), "unreadable", "heartbeat can't be read"),
		(False, None, "stale", "can't be listed"),
		(False, ("4242 run-steps.py",), "stale", "4242 run-steps.py"),
		(False, ("4243 auto-finish.py",), "stale", "auto-finish.py"),
	],
)
def test_a_run_that_may_still_run_is_hands_off_never_an_action(world, fake, wrapper, helpers, heartbeat, why):
	"""The dangerous direction: any process check short of "gone" keeps every action class away."""
	run = orphanDone(world, fake)
	path = statefile.pathOf(world["wt"], runstate.HEARTBEAT, run)
	if heartbeat == "fresh":
		runstate.beat(world["wt"], run, LIMITS["heartbeat_interval_seconds"])
	elif heartbeat == "unreadable":
		with open(path, "w") as f:
			f.write("garbage")
	judged = judge(world, wrapper=wrapper, helpers=helpers, fake=fake)
	assert judged["class"] == "hands-off" and why in judged["why"], judged
	assert judged["action"] == "nothing"


@pytest.mark.parametrize("ns", ["pid:[1]", "", 7, "missing"])
def test_a_caller_in_another_pid_namespace_checks_no_process_and_acts_on_nothing(world, fake, ns):
	"""The dangerous direction of the namespace rule: a wrapper recorded in another pid namespace (a sandboxed
	caller sees no process of it), a pid_ns missing (a record from before it) or not a namespace: every check
	unknown, hands-off; SANDBOX_RUNTIME is never read (set here: it changes nothing)."""
	run = launch(world["wt"])
	started(world["wt"], run, ns=ns)
	if ns == "missing":
		process = runstate.readRecord(world["wt"], run)[0]["process"]
		change(world["wt"], run, process={k: v for k, v in process.items() if k != "pid_ns"})
	workerDone(world["wt"], run)
	fake.advance(STALE)
	record = runstate.readRecord(world["wt"], run)[0]
	assert reconciler.processCheck(record) is None
	facts = reconciler.factsOf(world["main"], world["wt"], now=fake.now())
	assert facts["wrapper"] is None and facts["helpers"] is None
	judged = reconciler.classify(facts, ATTEMPTS, world["main"])
	assert judged["class"] == "hands-off" and "can't be checked" in judged["why"], judged


def test_the_wrapper_s_own_namespace_checks_it(world, fake, sleeper, monkeypatch):
	"""The same namespace (the record's pid_ns, written by runstate.processFacts) checks the wrapper for real; the
	sandbox variable is ignored either way."""
	monkeypatch.setenv("SANDBOX_RUNTIME", "1")
	wrapper = sleeper("run-worker.sh", TOPIC, "1")
	run = launch(world["wt"])
	facts = runstate.processFacts(wrapper.pid)
	assert facts["pid_ns"] == runstate.pidNamespace() and facts["pid_ns"].startswith("pid:[")
	started(world["wt"], run, pid=wrapper.pid, ticks=facts["start_ticks"])
	record = runstate.readRecord(world["wt"], run)[0]
	assert reconciler.processCheck(record) is runstate.verifyProcess
	assert reconciler.factsOf(world["main"], world["wt"], now=fake.now())["wrapper"] is True


def test_a_running_run_is_hands_off_and_an_orphan_that_didn_t_finish_is_the_person_s(world, fake):
	run = launch(world["wt"])
	started(world["wt"], run)
	assert judge(world, wrapper=True, fake=fake)["class"] == "hands-off"
	fake.advance(STALE)
	judged = judge(world, wrapper=False, fake=fake)
	assert judged["class"] == "nothing" and "wt resume" in judged["why"]


def test_a_failed_step_with_attempts_left_is_retryable_and_at_the_limit_is_told(world, fake):
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	ended(world["wt"], run, merge=failedStep(1))
	fake.advance(STALE)
	judged = judge(world, fake=fake)
	assert judged["class"] == "step-retryable" and "merge (attempt 1" in judged["why"], judged
	change(world["wt"], run, steps={"merge": failedStep(ATTEMPTS)})
	judged = judge(world, fake=fake)
	assert judged["class"] == "at-limit" and "tell the person once" in judged["action"], judged


def test_a_step_left_running_counts_as_failed_only_when_its_runner_is_gone(world, fake, monkeypatch):
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	running = {"state": "running", "attempt": 1, "runner_pid": GONE_PID, "started_at": clock.isoNow()}
	ended(world["wt"], run, reason=runstate.WRAPUP_FAILED, merge=running)
	fake.advance(STALE)
	assert judge(world, fake=fake)["class"] == "wrapup-owed"
	monkeypatch.setattr(reconciler, "runnerAlive", lambda root, run, step, procDir="/proc": True)
	facts = reconciler.factsOf(world["main"], world["wt"], now=fake.now(), check=lambda *a: False,
		listHelpers=lambda root, run: [])
	retryable, limited = reconciler.stepsOver(facts["record"], ATTEMPTS, facts["runner_alive"])
	assert retryable == [] and limited == []


def test_a_decided_next_run_that_never_started_is_named(world, fake):
	"""A7: the run ended, its next step decided a resume, no later run exists."""
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run, status="blocked")
	ended(world["wt"], run, next={"state": "done", "attempt": 1, "outcome": "resume", "usd": 1.0})
	fake.advance(STALE)
	judged = judge(world, fake=fake)
	assert judged["class"] == "next-unstarted" and "auto-start" in judged["action"], judged


@pytest.mark.parametrize("reason", ["orphan-closed", "launch-failed", "not-logged-in", "config-error"])
def test_a_run_that_ended_without_a_wrapup_is_nothing(world, fake, reason):
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	ended(world["wt"], run, reason=reason, merge=failedStep(1))
	fake.advance(STALE)
	assert judge(world, fake=fake)["class"] == "nothing"


def test_a_finished_run_with_final_steps_is_nothing(world, fake):
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	ended(world["wt"], run, merge={"state": "done", "attempt": 1, "outcome": "merged"})
	fake.advance(STALE)
	assert judge(world, fake=fake)["class"] == "nothing"


# -- 2. process checks ---------------------------------------------------------------------------------------


@pytest.fixture
def sleeper():
	started = []

	def start(*argv):
		process = subprocess.Popen(["bash", "-c", "sleep 30; true", *argv])
		started.append(process)
		return process

	yield start
	for process in started:
		process.kill()
		process.wait()


def test_live_helpers_name_only_processes_of_this_worktree_and_run(world, sleeper):
	finish = sleeper("auto-finish.py", world["wt"], TOPIC, "1")
	steps = sleeper("run-steps.py", "wrapup", world["wt"], "1", "--topic", TOPIC)
	sleeper("run-steps.py", "wrapup", world["wt"], "2")
	sleeper("auto-finish.py", "/elsewhere", TOPIC, "1")
	sleeper("unrelated.py", world["wt"], "1")
	found = reconciler.liveHelpers(world["wt"], 1)
	assert sorted(found) == sorted([f"{finish.pid} auto-finish.py", f"{steps.pid} run-steps.py"])


def test_names_run_compares_real_paths_and_the_position_of_the_run(world, tmp_path):
	link = tmp_path / "link"
	os.symlink(world["wt"], link)
	assert reconciler.namesRun([b"x", str(link).encode(), b"1"], world["wt"], 1)
	assert not reconciler.namesRun([b"x", world["wt"].encode(), b"2", b"--stopped", b"1"], world["wt"], 1)
	assert not reconciler.namesRun([b"x", (world["wt"] + "/..").encode(), b"1"], world["wt"], 1)


def test_a_proc_that_can_t_be_read_counts_as_alive_and_unlisted(tmp_path):
	"""Fail closed: a runner whose /proc entry can't be read is alive (its step never runs twice), and a process
	table that can't be listed is None (hands-off), never "nobody"."""
	proc = tmp_path / "proc"
	(proc / "4242" / "cmdline").mkdir(parents=True)  # a directory: reading it raises an OSError
	assert reconciler.runnerAlive("/w", 1, {"runner_pid": 4242}, procDir=str(proc)) is True
	assert reconciler.runnerAlive("/w", 1, {"runner_pid": 4243}, procDir=str(proc)) is False  # gone
	assert reconciler.liveHelpers("/w", 1, procDir=str(proc)) is None
	assert reconciler.liveHelpers("/w", 1, procDir=str(tmp_path / "missing")) is None


def test_a_runner_with_the_recorded_ticks_unreadable_is_left_alone(world, sleeper, tmp_path):
	"""A5: the command line matches but its start ticks can't be read: alive (never a second runner)."""
	runner = sleeper("run-steps.py", "wrapup", world["wt"], "1")
	proc = tmp_path / "proc" / str(runner.pid)
	proc.mkdir(parents=True)
	with open(f"/proc/{runner.pid}/cmdline", "rb") as f:
		(proc / "cmdline").write_bytes(f.read())
	step = {"runner_pid": runner.pid, "runner_start_ticks": 5}
	assert reconciler.runnerAlive(world["wt"], 1, step, procDir=str(tmp_path / "proc")) is True


# -- 3. the command -------------------------------------------------------------------------------------------


def runSteps(*args, env=None):
	return subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "run-steps.py"), "reconcile", *args],
		capture_output=True, text=True, timeout=60, env={**os.environ, **(env or {})})


def treeHash(*roots):
	digest = hashlib.sha256()
	for root in roots:
		for folder, _dirs, files in sorted(os.walk(root)):
			for name in sorted(files):
				path = os.path.join(folder, name)
				digest.update(path.encode())
				with open(path, "rb") as f:
					digest.update(f.read())
	return digest.hexdigest()


def test_reconcile_with_a_wrong_target_refuses(world):
	env = {"WT_MAIN_DIR": world["main"]}
	assert runSteps(world["wt"], "--all", "--dry-run", env=env).returncode == 2
	assert runSteps(env=env).returncode == 2
	for wrong in (world["main"], world["wt"] + "/..", "/tmp"):
		out = runSteps(wrong, "--dry-run", env=env)
		assert out.returncode == 2 and "is not a worktree of" in out.stderr, out.stderr


def test_a_symlink_to_a_worktree_is_that_worktree(world, tmp_path):
	link = tmp_path / "alias"
	os.symlink(world["wt"], link)
	out = runSteps(str(link), "--dry-run", env={"WT_MAIN_DIR": world["main"]})
	assert out.returncode == 0 and f"{TOPIC} run none: nothing" in out.stdout, out.stderr


def test_the_dry_run_changes_no_file_and_names_each_worktree(world, fake):
	orphanDone(world, fake)
	before = treeHash(os.path.join(world["wt"], ".hook-only"), world["main"] + "/.hook-only")
	out = runSteps("--all", "--dry-run", env={"WT_MAIN_DIR": world["main"]})
	assert out.returncode == 0, out.stderr
	assert "no step was run or changed, nothing started or signalled" in out.stdout and f"{TOPIC} run 1:" in out.stdout
	assert treeHash(os.path.join(world["wt"], ".hook-only"), world["main"] + "/.hook-only") == before


def test_a_live_wrapper_with_a_stale_heartbeat_is_told_once_and_left_alone(world, fake, sleeper):
	"""B2: the pid check finds the wrapper alive, its heartbeat is stale: one heartbeat-stalled event per run (two
	dry runs), the record changes only by its notice, the wrapper gets no signal, the class is hands-off."""
	wrapper = sleeper("run-worker.sh", TOPIC, "1")
	run = launch(world["wt"])
	started(world["wt"], run, pid=wrapper.pid, ticks=reconciler.startTicks(wrapper.pid))
	workerDone(world["wt"], run)
	fake.advance(STALE)
	before = runstate.readRecord(world["wt"], run)[0]
	for _ in range(2):
		text = reconciler.dryRun(world["wt"], False, world["main"])
		assert "hands-off" in text and "heartbeat is" in text, text
	events = [json.loads(line) for line in open(os.path.join(world["main"], ".hook-only", "events.jsonl"))]
	assert [e["kind"] for e in events] == ["heartbeat-stalled"]
	import wt_events

	assert wt_events.needsAttention(events[0])  # the person is told (pane toast, prompt context), once
	after = runstate.readRecord(world["wt"], run)[0]
	assert {k: v for k, v in after.items() if k != "notices"} == before
	assert wrapper.poll() is None  # no signal: it still sleeps


def test_a_step_running_long_is_a_note_only(world, fake):
	"""B1 in the dry run: the note, the class hands-off (the wrapper lives), the step still running."""
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	change(world["wt"], run, steps={"merge": {"state": "running", "attempt": 1, "runner_pid": GONE_PID,
		"started_at": clock.isoNow()}})
	fake.advance(runstate.reportMinutes({"name": "merge"}, LIMITS) * 60)
	runstate.beat(world["wt"], run, LIMITS["heartbeat_interval_seconds"])
	judged = judge(world, wrapper=True, fake=fake)
	assert judged["class"] == "hands-off" and any("merge has run" in n for n in judged["notes"]), judged
	assert runstate.readRecord(world["wt"], run)[0]["steps"]["merge"]["state"] == "running"


def wtPrompt(world, text):
	out = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")], input=json.dumps(
		{"prompt": text, "cwd": world["main"]}), capture_output=True, text=True, timeout=60)
	return json.loads(out.stdout or "{}")


def test_wt_reconcile_relays_the_dry_run_and_the_actions(world):
	answer = wtPrompt(world, "wt reconcile --dry-run")
	assert "no step was run or changed" in answer["hookSpecificOutput"]["additionalContext"], answer
	answer = wtPrompt(world, f"wt reconcile {TOPIC} --dry-run")
	assert f"{TOPIC} run none: nothing" in answer["hookSpecificOutput"]["additionalContext"], answer
	answer = wtPrompt(world, "wt reconcile")
	context = answer["hookSpecificOutput"]["additionalContext"]
	assert f"{TOPIC} run none: nothing" in context and "did: nothing" in context, answer


# -- 4. the actions --------------------------------------------------------------------------------------------


@pytest.fixture
def starts(monkeypatch):
	"""Every detached start the reconciler makes, recorded instead of run: [(argv, mainDir, log)]."""
	made = []
	monkeypatch.setattr(reconciler, "startDetached", lambda argv, mainDir, log=None, cwd=None: made.append(
		(argv, mainDir, log)))
	return made


def events(world):
	path = os.path.join(world["main"], ".hook-only", "events.jsonl")
	return [json.loads(line) for line in open(path)] if os.path.exists(path) else []


def act(world):
	return reconciler.reconcile(world["wt"], False, world["main"])


def test_an_owed_wrapup_is_started_detached_once_every_check_says_gone(world, fake, starts):
	run = orphanDone(world, fake)
	text = act(world)
	assert "did: started the wrap-up" in text, text
	[(argv, mainDir, log)] = starts
	assert argv[1:] == [reconciler.RUN_STEPS, "wrapup", world["wt"], str(run), "--topic", TOPIC]
	assert mainDir == world["main"] and log.endswith(f"stderr-{run}.log")
	claim = statefile.read(world["wt"], reconciler.CLAIM)
	assert (claim["run"], claim["class"]) == (run, "wrapup-owed")


def test_a_live_wrapper_gets_no_action(world, fake, starts, sleeper):
	"""The dangerous direction end to end: the wrapper runs (a real process in this namespace), nothing starts."""
	wrapper = sleeper("run-worker.sh", TOPIC, "1")
	run = launch(world["wt"])
	started(world["wt"], run, pid=wrapper.pid, ticks=reconciler.startTicks(wrapper.pid))
	workerDone(world["wt"], run)
	fake.advance(STALE)
	assert "hands-off" in act(world) and starts == []


def test_a_held_claim_skips_the_worktree(world, fake, starts):
	"""Two reconciles at once: the second finds the claim held and does nothing."""
	orphanDone(world, fake)
	path = statefile.lockPath(world["wt"], reconciler.CLAIM)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	import fcntl

	with open(path, "w") as held:
		fcntl.flock(held, fcntl.LOCK_EX)
		assert "another reconcile holds this worktree" in act(world)
	assert starts == []


def test_a_claim_that_can_t_be_taken_skips_the_worktree(world, fake, starts, monkeypatch):
	orphanDone(world, fake)
	monkeypatch.setattr(statefile, "lockPath", lambda *a: "/proc/no-such-dir/claim.json.lock")
	assert "another reconcile holds this worktree" in act(world) and starts == []


def test_a_run_that_changed_under_the_record_lock_is_left_alone(world, fake, starts, monkeypatch):
	orphanDone(world, fake)
	real = reconciler.judge
	calls = []

	def judgeTwice(mainDir, root):
		facts, judged = real(mainDir, root)
		calls.append(judged["class"])
		if len(calls) > 1:
			judged = {**judged, "class": "hands-off", "why": "its wrapper still runs"}
		return facts, judged

	monkeypatch.setattr(reconciler, "judge", judgeTwice)
	assert "did: nothing: the run is now hands-off" in act(world) and starts == []


def test_at_the_limit_the_person_is_told_once_and_nothing_starts(world, fake, starts):
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	ended(world["wt"], run, merge=failedStep(ATTEMPTS))
	fake.advance(STALE)
	before = runstate.readRecord(world["wt"], run)[0]["steps"]
	for _ in range(2):
		act(world)
	assert [e["kind"] for e in events(world)] == ["step-at-limit"] and starts == []
	import wt_events

	assert wt_events.needsAttention(events(world)[0])
	assert runstate.readRecord(world["wt"], run)[0]["steps"] == before


def test_a_decided_next_run_is_started_at_most_the_limit_then_closed_and_told(world, fake, starts):
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run, status="blocked")
	ended(world["wt"], run, next={"state": "done", "attempt": 1, "outcome": "resume", "usd": 1.0})
	fake.advance(STALE)
	for _ in range(ATTEMPTS + 2):
		act(world)
	assert [argv[1:3] for argv, _m, _l in starts] == [[reconciler.WORKER_CONTROL, "auto-start"]] * ATTEMPTS
	step = runstate.readRecord(world["wt"], run)[0]["steps"]["next"]
	assert step["state"] == "failed" and step["start_attempts"] == ATTEMPTS
	assert [e["kind"] for e in events(world)] == ["step-at-limit"]
	assert judge(world, fake=fake)["class"] == "at-limit"


def test_the_dry_run_and_an_excluded_worktree_start_nothing(world, fake, starts):
	orphanDone(world, fake)
	assert "would: run the wrap-up" in reconciler.dryRun(world["wt"], False, world["main"])
	assert reconciler.reconcile(None, True, world["main"], exclude=[world["wt"] + "/."]).endswith("no worktree")
	assert starts == []


def test_the_trigger_starts_at_most_once_per_interval(world, fake, starts):
	interval = LIMITS["reconcile_wt_interval_minutes"] * 60
	assert reconciler.triggerThrottled(world["main"]) is True
	fake.advance(interval - 1)
	assert reconciler.triggerThrottled(world["main"]) is False
	fake.advance(1)
	assert reconciler.triggerThrottled(world["main"]) is True
	assert [argv[2:] for argv, _m, _l in starts] == [["reconcile", "--all"]] * 2


def test_a_trigger_outside_a_main_checkout_or_without_writable_state_starts_nothing(world, fake, starts, tmp_path):
	assert reconciler.triggerFrom(world["wt"]) is False
	assert reconciler.triggerFrom(tmp_path) is False
	assert not os.path.exists(os.path.join(world["wt"], ".hook-only", "reconcile"))
	state = os.path.join(world["main"], ".hook-only")
	os.makedirs(state, exist_ok=True)
	os.chmod(state, 0o555)  # as inside Claude Code's sandbox: .hook-only is read-only there
	try:
		assert reconciler.triggerFrom(world["main"]) is False
	finally:
		os.chmod(state, 0o755)
	assert starts == []
	assert reconciler.triggerFrom(world["main"]) is True and len(starts) == 1


def test_a_detached_start_leaves_the_caller_s_session_with_the_main_checkout(world, tmp_path):
	out = tmp_path / "seen.json"
	code = ("import json,os,sys; json.dump({'sid': os.getsid(0), 'main': os.environ.get('WT_MAIN_DIR')}, "
		"open(sys.argv[1], 'w'))")
	reconciler.startDetached([sys.executable, "-c", code, str(out)], world["main"], str(tmp_path / "log"))
	for _ in range(200):
		if out.exists() and out.stat().st_size:
			break
		import time

		time.sleep(0.05)
	seen = json.loads(out.read_text())
	assert seen["sid"] != os.getsid(0) and seen["main"] == world["main"]


def test_a_wt_command_triggers_the_reconciler_and_wt_status_does_not(world):
	"""worker-control.py: every wt command but `wt status` (display only) and `wt reconcile` triggers it once per
	interval (its state written in the main checkout); `wt status` writes nothing."""
	trigger = statefile.pathOf(world["main"], reconciler.TRIGGER)
	wtPrompt(world, f"wt status {TOPIC}")
	assert not os.path.exists(trigger)
	wtPrompt(world, "wt queue")
	assert statefile.read(world["main"], reconciler.TRIGGER)["started_at"]


@pytest.mark.parametrize("error, expected", [
	(FileNotFoundError(2, "gone"), []),
	(ProcessLookupError(3, "exited mid-read"), []),
	(OSError(3, "ESRCH"), []),
	(PermissionError(13, "denied"), None),
	(OSError(5, "EIO"), None),
])
def test_a_process_exiting_while_it_is_read_is_gone_and_only_other_errors_are_unknown(monkeypatch, error, expected):
	"""readArgv under process churn: every "process gone" error (ENOENT, ESRCH, ProcessLookupError) means gone, so
	liveHelpers isn't made unknown by a process that exited mid-scan; any other error stays unknown (None)."""
	def failing(*args, **kwargs):
		raise error

	monkeypatch.setitem(reconciler.__dict__, "open", failing)
	assert reconciler.readArgv(4242) == expected


def runningStep(program=None, ticks=None):
	step = {"state": "running", "attempt": 1, "runner_pid": GONE_PID, "started_at": clock.isoNow()}
	if program is not None:
		step.update(program_pid=program, program_start_ticks=ticks)
	return step


def test_a_step_program_that_outlived_its_runner_holds_the_run(world, fake, starts, sleeper):
	"""run-steps.py killed outright: the step's own program (its own session) still runs. Recorded with its start
	ticks, it keeps the run hands-off and its step unclaimable; gone or reused, it doesn't."""
	program = sleeper("verify.py", world["wt"], "1")
	run = launch(world["wt"])
	started(world["wt"], run)
	workerDone(world["wt"], run)
	ticks = reconciler.startTicks(program.pid)
	change(world["wt"], run, steps={"verify": runningStep(program.pid, ticks)})
	fake.advance(STALE)
	text = act(world)
	assert "hands-off" in text and "program still runs (verify" in text, text
	assert starts == []
	assert reconciler.runnerAlive(world["wt"], run, runningStep(program.pid, ticks))
	assert not reconciler.runnerAlive(world["wt"], run, runningStep(program.pid, ticks + 1))  # a reused pid
	assert not reconciler.runnerAlive(world["wt"], run, runningStep(GONE_PID, ticks))
	assert reconciler.runnerAlive(world["wt"], run, runningStep(program.pid, None))  # no ticks: the pid decides


def test_the_reconciler_starts_a_wrapup_at_most_the_limit_then_tells_once(world, fake, starts):
	"""A wrap-up the reconciler starts that dies before it counts an attempt (a crash at import) can't make every
	trigger start another: reconcile.wrapup_starts is counted first, at step_attempts_max the run is at-limit and the
	person is told once."""
	run = orphanDone(world, fake)
	for _ in range(ATTEMPTS + 2):
		act(world)
	assert len(starts) == ATTEMPTS
	assert runstate.reconcileStarts(runstate.readRecord(world["wt"], run)[0]) == ATTEMPTS
	assert [e["kind"] for e in events(world)] == ["step-at-limit"]
	assert judge(world, fake=fake)["class"] == "at-limit"


# -- 5. a stopped run is the person's: no automatic action ----------------------------------------------------


def stoppedLatest(world, kind):
	"""The latest run as a stop leaves it, every process check gone; kind names which stop and what is left."""
	run = launch(world["wt"])
	started(world["wt"], run)
	claudeStopped = kind == "claude-part-stopped"
	change(world["wt"], run, claude={"ended_at": clock.isoNow(), "outcome": "status", "status": "done",
		"exit_code": 0, "stopped": claudeStopped})
	if kind != "signal-only":
		runstate.recordStop(world["wt"], run, "person")
	skipped = {"state": "skipped", "attempt": 1, "skip_reason": "stopped"}
	steps = {"verify": failedStep(ATTEMPTS if kind == "at-limit" else 1), "verifier": dict(skipped),
		"merge": dict(skipped), "next": dict(skipped)}
	if kind == "next-decided":  # every step final, the next one decided a resume that never started
		steps = {name: {"state": "done", "attempt": 1} for name in runstate.STEP_ORDER}
		steps["next"] = {"state": "done", "attempt": 1, "outcome": "resume", "usd": 1.0}
	if kind in ("orphan", "claude-part-stopped"):
		change(world["wt"], run, steps={**steps, "merge": {"state": "running", "attempt": 1, "runner_pid": GONE_PID,
			"started_at": clock.isoNow()}})
	else:
		ended(world["wt"], run, reason="stopped", **steps)
	return run


@pytest.mark.parametrize("kind", ["in-wrapup", "signal-only", "orphan", "claude-part-stopped", "at-limit",
	"next-decided"])
def test_a_stopped_run_is_never_acted_on_by_the_reconciler(world, fake, starts, kind):
	"""The person's stop means hands-off for automation: whatever the stopped run would otherwise be classed for
	(a wrap-up owed, a step to retry, a limit to tell, a next run to start), the reconciler classes it nothing and
	starts or tells nothing; only `wt wrapup` continues it, which the why names when a wrap-up is kept."""
	run = stoppedLatest(world, kind)
	fake.advance(STALE)
	judged = judge(world, fake=fake)
	assert judged["class"] == "nothing" and judged["why"].startswith("stopped: no automatic action"), judged
	record = runstate.readRecord(world["wt"], run)[0]
	keeps = bool(runstate.wrapupLeft(record))
	assert keeps == (kind != "next-decided")
	assert (f"`wt wrapup {TOPIC}` continues its wrap-up" in judged["why"]) == keeps
	before = json.dumps(record, sort_keys=True)
	text = act(world)
	assert "did: nothing" in text and starts == [] and events(world) == [], text
	assert json.dumps(runstate.readRecord(world["wt"], run)[0], sort_keys=True) == before
	assert statefile.read(world["wt"], reconciler.CLAIM) is None


def test_a_stopped_run_whose_wrapper_may_live_stays_hands_off(world, fake):
	"""The stop gate never hides a live process: a stopped run whose wrapper the check finds alive reads hands-off."""
	stoppedLatest(world, "orphan")
	fake.advance(STALE)
	assert judge(world, wrapper=True, fake=fake)["class"] == "hands-off"


def reconcileCells():
	"""Every stop fact and step situation the reconciler's classes read: claude.stopped, the person's stop, the exit
	and one step (failed with attempts left or none, skipped as stopped, left running by a gone runner, done), and a
	decided next run."""
	for claudeStopped in ("missing", False, True):
		for stop in (False, True):
			for end in (None, "completed", "stopped", runstate.WRAPUP_FAILED):
				for step in ("failed", "failed-at-limit", "skipped-stopped", "running-gone", "done", "next-decided"):
					yield claudeStopped, stop, end, step


def cellStep(step):
	if step == "failed":
		return {"merge": failedStep(1)}
	if step == "failed-at-limit":
		return {"merge": failedStep(ATTEMPTS)}
	if step == "skipped-stopped":
		return {"merge": {"state": "skipped", "attempt": 1, "skip_reason": "stopped"}}
	if step == "running-gone":
		return {"merge": {"state": "running", "attempt": 1, "runner_pid": GONE_PID, "started_at": clock.isoNow()}}
	if step == "next-decided":
		return {"next": {"state": "done", "attempt": 1, "outcome": "resume", "usd": 1.0}}
	return {"merge": {"state": "done", "attempt": 1, "outcome": "refused"}}


def test_old_vs_new_only_a_stopped_run_loses_its_action(world, fake, monkeypatch):
	"""Skill refactor, section 2: the classes before (the old wrapupLeft, wrapup_stop_baseline, and no stop gate)
	against today's over every cell, every process check gone. The only change: a stopped run (runstate.stoppedRun)
	that was classed for an action is now nothing; every other cell keeps its class."""
	counts = {}
	for claudeStopped, stop, end, step in reconcileCells():
		run = launch(world["wt"])
		started(world["wt"], run)
		claude = {"ended_at": clock.isoNow(), "outcome": "status", "status": "done", "exit_code": 0}
		if claudeStopped != "missing":
			claude["stopped"] = claudeStopped
		change(world["wt"], run, claude=claude, steps=cellStep(step))
		if stop:
			runstate.recordStop(world["wt"], run, "person")
		if end:
			change(world["wt"], run, exit={"ended_at": clock.isoNow(), "code": 0, "reason": end})
		fake.advance(STALE)
		new = judge(world, fake=fake)
		with monkeypatch.context() as old:
			old.setattr(runstate, "wrapupLeft", wrapup_stop_baseline.oldWrapupLeft)
			old.setattr(runstate, "stoppedRun", lambda record: False)
			before = judge(world, fake=fake)
		record = runstate.readRecord(world["wt"], run)[0]
		if new["class"] == before["class"]:
			reason = "same"
		else:
			assert before["class"] in reconciler.ACTING and new["class"] == "nothing", (record, before, new)
			assert runstate.stoppedRun(record) and new["why"].startswith("stopped:"), (record, new)
			reason = f"stopped-{before['class']}"
		counts[reason] = counts.get(reason, 0) + 1
	print(f"reconcile classes old vs new: {counts}")
	assert counts["same"] > 0 and {"stopped-wrapup-owed", "stopped-step-retryable", "stopped-at-limit",
		"stopped-next-unstarted"} <= set(counts), counts

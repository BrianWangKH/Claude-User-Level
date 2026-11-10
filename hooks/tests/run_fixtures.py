"""Shared fixtures of the run record's reader tests (hooks/RULES.md T4, T9): one run at each phase, written the
way its writers write it, as a run record (runstate's writer, migration step 2) or as today's runs/ files only
(the shapes run-worker.sh and worker-control.py write), plus the clock and checkout helpers. Used by
test_display_readers.py (migration step 3) and test_decision_readers.py (step 4).
"""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import hookonly  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 8, 0, 0, tzinfo=TAIPEI)
TOPIC = "cash-account"
LIMITS = runstate.settings()
INTERVAL = LIMITS["heartbeat_interval_seconds"]
THRESHOLD = INTERVAL * LIMITS["orphan_missed_beats_count"]
WRAPUP = LIMITS["legacy_wrapup_stale_minutes"] * 60
GONE_PID = 999999999  # no such process: /proc/<pid> is missing
SESSION = "f4467c1a-ff29-420d-8174-31c15b576606"

PHASES = ("none", "launching", "running", "finishing", "ended", "orphan-suspected", "orphan-confirmed", "unknown")
CASES = [
	(source, phase) for source in ("record", "legacy") for phase in PHASES if (source, phase) != ("legacy", "unknown")
]


def load(path, name):
	spec = importlib.util.spec_from_file_location(name, path)
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


def gitRun(*args):
	subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=60)


def makeMain(tmp_path, monkeypatch):
	"""A main checkout with one commit, a visible plans/ with the topic's estimate, worktrees beside it."""
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	checkout = tmp_path / "proj" / "main"
	gitRun("init", "-q", "-b", "main", str(checkout))
	gitRun("-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	(checkout / "plans" / "worker").mkdir(parents=True)
	(checkout / "plans" / "worker" / f"{TOPIC}.json").write_text(json.dumps({"minutes": 20, "est_usd": 2}))
	return checkout


def addTopic(main, topic=TOPIC):
	worktree = main.parent / "worktrees" / topic
	gitRun("-C", str(main), "worktree", "add", "-q", "-b", f"feature/x/{topic}", str(worktree))
	return worktree


def workerState(main, topic=TOPIC, run=1, pid=GONE_PID):
	folder = hookonly.stateDir(str(main), "workers")
	os.makedirs(folder, exist_ok=True)
	with open(os.path.join(folder, f"{topic}.json"), "w") as f:
		f.write(json.dumps({"pid": pid, "session_id": SESSION, "run": run}))


# -- the two sources of one run ------------------------------------------------------------------------


def recordRun(main, worktree, phase, fake):
	"""One run at phase, written through runstate's writer (the record of migration step 2)."""
	root = str(worktree)
	if phase == "none":
		return
	run = runstate.create(
		root,
		{
			"topic": TOPIC,
			"started_by": "person",
			"purpose": "build",
			"flow": "written",
			"integration_mode": "local",
			"prompt_kind": "feature-flow",
			"launch": {"launched_at": clock.isoNow(), "by_pid": 1},
			"estimate": {"minutes": 20, "usd": 2.0, "cap_minutes": 26, "cap_usd": 2.6},
		},
	)
	workerState(main, run=run)
	if phase == "launching":
		return
	if phase == "unknown":
		with open(statefile.pathOf(root, runstate.RECORD, run), "w") as f:
			f.write("{")
		return
	process = {
		"wrapper_pid": GONE_PID,
		"start_ticks": 7,
		"boot_id": runstate.readBootId(),
		"started_at": clock.isoNow(),
	}
	runstate.update(root, run, lambda r: r.update(process=process, main_head_at_start="65f7802"))
	runstate.beat(root, run, INTERVAL)
	if phase == "running":
		return
	if phase.startswith("orphan"):
		fake.advance(THRESHOLD + 60)
		if phase == "orphan-confirmed":
			runstate.confirmOrphan(root, run, "test", {"wrapper_pid": "gone"})
		return
	status = "done" if phase == "finishing" else "needs-decision"
	claude = {"ended_at": clock.isoNow(), "outcome": "status", "status": status, "header_file": "result-1.json"}
	runstate.update(root, run, lambda r: r.update(claude=claude))
	legacyFile(
		worktree,
		"result-1.json",
		{"result": f"STATUS: {status}\n\nwhat was done", "num_turns": 9, "total_cost_usd": 1.5},
		0,
		fake.now(),
	)
	if phase == "ended":
		runstate.update(
			root, run, lambda r: r.update(exit={"ended_at": clock.isoNow(), "code": 0, "reason": "completed"})
		)


def legacyFile(worktree, name, content, ageSeconds, now):
	path = os.path.join(runstate.runsDir(str(worktree)), name)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		f.write(content if isinstance(content, str) else json.dumps(content))
	stamp = (now - timedelta(seconds=ageSeconds)).timestamp()
	os.utime(path, (stamp, stamp))
	return path


def legacyRun(main, worktree, phase, fake):
	"""One run at phase as today's files only (no record), the way run-worker.sh and worker-control.py write
	them. orphan-confirmed has the same files as orphan-suspected: only a pid check can confirm it."""
	now = fake.now()
	if phase == "none":
		return
	age = WRAPUP + 900 if phase.startswith("orphan") else 5 if phase == "launching" else 600
	legacyFile(worktree, "est-1.json", {"minutes": 20, "usd": 2.0, "cap": 2.6, "cap_minutes": 26}, age, now)
	if phase == "launching":
		return
	workerState(main)
	started = now - timedelta(seconds=age)
	legacyFile(worktree, "start-1", started.isoformat(timespec="seconds") + "\n", age, now)
	legacyFile(worktree, "main-1", "65f7802\n", age, now)
	if phase == "running":
		return
	if phase.startswith("orphan"):
		legacyFile(
			worktree, "result-1.json", {"result": "STATUS: done\n\nall green", "num_turns": 9}, WRAPUP + 300, now
		)
		return
	status = "done" if phase == "finishing" else "needs-decision"
	legacyFile(
		worktree,
		"result-1.json",
		{"result": f"STATUS: {status}\n\nwhat was done", "num_turns": 9, "total_cost_usd": 1.5},
		30,
		now,
	)
	if phase == "ended":
		legacyFile(worktree, "exit-1", f"0 {now.isoformat(timespec='seconds')}\n", 10, now)


def build(main, fake, source, phase):
	worktree = addTopic(main)
	(recordRun if source == "record" else legacyRun)(main, worktree, phase, fake)
	return worktree


def expected(source, phase, pidChecked=False):
	"""(phase, confidence) every reader must show; without a pid check a legacy orphan is only suspected."""
	if not phase.startswith("orphan"):
		return phase, None
	if phase == "orphan-confirmed" and (source == "record" or pidChecked):
		return "orphan", "confirmed"
	return "orphan", "suspected"


def pidCheck(phase):
	"""The pid check worker-control.py makes, made deterministic: gone for orphan-confirmed, can't tell (None)
	otherwise, so every other phase reads as it does without a check."""
	return lambda process, topic, run: False if phase == "orphan-confirmed" else None

"""wt_queue.py: the person's queued plans start in order within the limits.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_wt_queue.py
"""
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time

import pytest

import config_fixtures

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
RUN_SKILLS = os.path.join(config_fixtures.cleanHome(), "skills")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)

import eventlog  # noqa: E402
import homeconfig  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
import wt_queue  # noqa: E402

DEFAULTS = homeconfig.load("budget")
SESSION = "11111111-2222-3333-4444-555555555555"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
	"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
		env={**os.environ, **GIT_ENV}).stdout.strip()


@pytest.fixture
def main(tmp_path):
	m = tmp_path / "proj" / "main"
	m.mkdir(parents=True)
	git(m, "init", "-q", "-b", "main")
	(m / ".gitignore").write_text(".hook-only/\nplans/\n")
	git(m, "add", ".gitignore")
	git(m, "commit", "-q", "-m", "base")
	(m / ".hook-only").mkdir()
	(m / ".hook-only" / "flow-mode").write_text("local\n")
	(m / "plans" / "queue").mkdir(parents=True)
	(tmp_path / "proj" / "worktrees").mkdir()
	return m


def planned(main, topic, usd=2.0, after=(), resources=(), scope="docs", order=None):
	(main / "plans" / "queue" / f"{topic}.md").write_text(f"# {topic}\n")
	(main / "plans" / "queue" / f"{topic}.json").write_text(json.dumps({"branch": f"feature/{scope}/{topic}",
		"after": list(after), "resources": list(resources), **({"order": order} if order is not None else {}),
		"estimate": {"minutes": 20, "est_usd": usd, "raw_minutes": 20, "raw_usd": usd, "class": "docs"}}))


def realWorktree(main, topic):
	"""A real git worktree of main for topic (on a branch the queue never reads as the topic's): what
	homeconfig.flowMode needs to find the topic's record."""
	git(main, "worktree", "add", "-q", "-b", f"wt-{topic}", str(main.parent / "worktrees" / topic))


def busy(main, topic, usd=2.0, ended=False, auto=None):
	"""A topic with a worktree (a stand-in .git unless a real one is there) and a run: going, or ended (with an
	automatic decision, maybe)."""
	runs = main.parent / "worktrees" / topic / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	if not (main.parent / "worktrees" / topic / ".git").exists():
		(main.parent / "worktrees" / topic / ".git").write_text("gitdir: x\n")
	(main / ".hook-only" / "workers").mkdir(exist_ok=True)
	(main / ".hook-only" / "workers" / f"{topic}.json").write_text(json.dumps({"session_id": SESSION, "run": 1}))
	(runs / "est-1.json").write_text(json.dumps({"minutes": 20, "usd": usd}))
	if ended:
		(runs / "exit-1").write_text("0 2026-11-01T10:00:00+08:00\n")
	if auto:
		(runs / "auto-1.json").write_text(json.dumps({"decision": auto, "usd": usd}))


def started(main, *topics):
	calls = []

	def start(topic, entry):
		calls.append(topic)
		busy(main, topic, usd=entry["estimate"]["est_usd"])  # as worker-control's launch records it
		return f"launch {topic}"
	result = wt_queue.tick(main, start=start, quota=lambda: None)
	return calls, {row["topic"]: row for row in result}


# --- add ----------------------------------------------------------------------------------------

def test_add_needs_local_mode(main):
	planned(main, "alpha-t")
	(main / ".hook-only" / "flow-mode").write_text("origin\n")
	with pytest.raises(wt_queue.Refused, match="local mode only"):
		wt_queue.add(main, ["alpha-t"])


@pytest.mark.parametrize("broken, message", [
	(lambda m: (m / "plans" / "queue" / "alpha-t.md").unlink(), "no plans/queue/alpha-t.md"),
	(lambda m: (m / "plans" / "queue" / "alpha-t.json").write_text(json.dumps({"branch": "feature/docs/other-t",
		"estimate": {"minutes": 1, "est_usd": 1}})), "branch must be"),
	(lambda m: (m / "plans" / "queue" / "alpha-t.json").write_text(json.dumps({"branch": "feature/docs/alpha-t"})),
		"estimate needs"),
	(lambda m: (m / "plans" / "queue" / "alpha-t.json").write_text(json.dumps({"branch": "feature/docs/alpha-t",
		"resources": ["Not A Tag"], "estimate": {"minutes": 1, "est_usd": 1}})), "resources"),
])
def test_add_checks_every_spec_and_adds_nothing_on_a_bad_one(main, broken, message):
	planned(main, "alpha-t")
	planned(main, "beta-t")
	broken(main)
	with pytest.raises(wt_queue.Refused, match=message):
		wt_queue.add(main, ["beta-t", "alpha-t"])
	assert wt_queue.loadState(main)["items"] == []


def test_add_keeps_the_order_and_refuses_twice(main):
	planned(main, "alpha-t")
	planned(main, "beta-t")
	wt_queue.add(main, ["beta-t", "alpha-t"])
	assert [i["topic"] for i in wt_queue.loadState(main)["items"]] == ["beta-t", "alpha-t"]
	with pytest.raises(wt_queue.Refused, match="queued already"):
		wt_queue.add(main, ["alpha-t"])


# --- tick ---------------------------------------------------------------------------------------

def test_tick_starts_in_order_up_to_the_worker_limit(main):
	for topic in ("alpha-t", "beta-t", "gamma-t"):
		planned(main, topic)
	wt_queue.add(main, ["alpha-t", "beta-t", "gamma-t"])
	busy(main, "other-t")  # one busy already
	calls, rows = started(main)
	assert calls == ["alpha-t"]  # the one it started counts at once: no more than the limit in one tick
	assert "queue_workers_max" in rows["beta-t"]["waits"] and "queue_workers_max" in rows["gamma-t"]["waits"]


def test_tick_counts_busy_topics_and_usd(main):
	planned(main, "big-t", usd=DEFAULTS["queue_max_concurrent_usd"])
	planned(main, "small-t", usd=1.0)
	wt_queue.add(main, ["big-t", "small-t"])
	busy(main, "other-t", usd=1.5)
	calls, rows = started(main)
	assert calls == ["small-t"]  # a waiting item doesn't hold back a later one
	assert "exceed queue_max_concurrent_usd" in rows["big-t"]["waits"]


def test_an_estimate_alone_above_the_limit_is_stuck(main):
	planned(main, "huge-t", usd=DEFAULTS["queue_max_concurrent_usd"] + 1)
	wt_queue.add(main, ["huge-t"])
	calls, rows = started(main)
	assert calls == [] and rows["huge-t"]["waits"].startswith("stuck")


def test_a_decided_automatic_run_still_counts_as_busy(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	for topic in ("one-t", "two-t"):
		busy(main, topic, ended=True, auto="merge")
	calls, rows = started(main)
	assert calls == [] and "2 worker(s) busy" in rows["alpha-t"]["waits"]


def test_an_ended_run_no_longer_counts(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	for topic in ("one-t", "two-t"):
		busy(main, topic, ended=True)
	assert started(main)[0] == ["alpha-t"]


GONE_PID = 2 ** 22 + 7  # above any pid_max: no such process
LONG_AGO = "2026-11-01T10:00:00+08:00"


def orphan(main, topic, source, pid=None):
	"""A topic the queue started whose run 1 has no end and no sign of life past its limits: today's files (a
	start long before its minute limit plus the wrap-up allowance) or a run record (a start long ago, no
	heartbeat); pid: its wrapper pid as the wt state (today's files) or the record keeps it."""
	planned(main, topic)
	wt_queue.add(main, [topic])
	state = wt_queue.loadState(main)
	next(i for i in state["items"] if i["topic"] == topic)["started"] = "2026-11-01T09:59:00"
	wt_queue.saveState(main, state)
	busy(main, topic)
	worktree = main.parent / "worktrees" / topic
	runs = worktree / ".hook-only" / "runs"
	(runs / "est-1.json").write_text(json.dumps({"minutes": 20, "usd": 2.0, "cap_minutes": 26}))
	if source == "legacy":
		(runs / "start-1").write_text(LONG_AGO + "\n")
		if pid is not None:
			(main / ".hook-only" / "workers" / f"{topic}.json").write_text(json.dumps({"session_id": SESSION, "run": 1,
				"pid": pid}))
		return worktree
	process = {"started_at": LONG_AGO, **({"wrapper_pid": pid} if pid is not None else {})}
	runstate.launchRecord(str(worktree), 1, {"topic": topic, "started_by": "queue", "purpose": "build",
		"flow": "written", "launch": {"launched_at": LONG_AGO}, "estimate": {"usd": 2.0}, "process": process})
	return worktree


@pytest.mark.parametrize("source", ["legacy", "record"])
def test_suspected_orphan_counts_busy(main, source):
	"""Design T-7: a run that may have died but whose wrapper can't be checked (no pid) may still run: busy, so
	nothing more starts on top of it."""
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	busy(main, "one-t")
	orphan(main, "dead-t", source)
	calls, rows = started(main)
	assert calls == [] and "2 worker(s) busy" in rows["alpha-t"]["waits"]
	assert "dead-t" not in rows  # started already: the queue never starts it again


@pytest.mark.parametrize("source", ["legacy", "record"])
def test_confirmed_orphan_not_busy_and_not_relaunched_by_tick(main, source):
	"""Design T-7: tick (outside the sandbox) finds the orphan's wrapper gone: not busy, so the next item starts;
	the orphan itself is never started again (the person resumes it); a record keeps the confirmation, once."""
	planned(main, "alpha-t")
	planned(main, "beta-t")
	wt_queue.add(main, ["alpha-t", "beta-t"])
	busy(main, "one-t")
	worktree = orphan(main, "dead-t", source, pid=GONE_PID)
	calls, rows = started(main)
	assert calls == ["alpha-t"] and "dead-t" not in rows
	# Without the pid check (the pane, maybe sandboxed): today's files stay suspected, so busy; a record keeps the
	# tick's confirmation, so it reads confirmed (not busy) everywhere.
	assert ("dead-t" in wt_queue.busyTopics(main)) is (source == "legacy")
	if source == "record":
		found, _ = runstate.readRecord(str(worktree), 1)
		assert found["orphan"]["by"] == "wt-queue" and not found.get("exit")
		started(main)
		assert len(eventlog.read(str(worktree), event="run-orphaned")) == 1


def test_a_shared_resource_tag_waits(main):
	planned(main, "other-t", resources=["quota-a"])
	planned(main, "alpha-t", resources=["quota-a"])
	planned(main, "beta-t", resources=["quota-b"])
	busy(main, "other-t")
	wt_queue.add(main, ["alpha-t", "beta-t"])
	calls, rows = started(main)
	assert calls == ["beta-t"] and "shares quota-a" in rows["alpha-t"]["waits"]


def test_after_waits_for_the_merge_into_the_local_main(main):
	planned(main, "first-t")
	planned(main, "second-t", after=["first-t"])
	wt_queue.add(main, ["second-t"])
	calls, rows = started(main)
	assert calls == [] and "waits for first-t" in rows["second-t"]["waits"]
	git(main, "checkout", "-q", "-b", "feature/docs/first-t")
	(main / "f").write_text("x\n")
	git(main, "add", "f")
	git(main, "commit", "-q", "-m", "first-t")
	git(main, "checkout", "-q", "main")
	assert started(main)[0] == []  # a branch, not merged yet
	git(main, "merge", "-q", "--no-ff", "-m", "merge first-t", "feature/docs/first-t")
	assert started(main)[0] == ["second-t"]


def test_a_branch_just_created_from_main_is_not_merged(main):
	# A queued topic's new branch points at main's head until its first commit; being an ancestor
	# of main made it "merged" at once, and its dependent would have started at the topic's first run's end
	planned(main, "first-t")
	planned(main, "second-t", after=["first-t"])
	wt_queue.add(main, ["second-t"])
	git(main, "branch", "feature/docs/first-t")
	assert started(main)[0] == []
	assert not wt_queue.isMerged(main, "first-t", lambda t: None)


def test_a_fast_forward_counts_only_when_recorded(main):
	planned(main, "first-t")
	git(main, "checkout", "-q", "-b", "feature/docs/first-t")
	(main / "f").write_text("x\n")
	git(main, "add", "f")
	git(main, "commit", "-q", "-m", "first-t")
	git(main, "checkout", "-q", "main")
	git(main, "merge", "-q", "--ff-only", "feature/docs/first-t")
	assert not wt_queue.isMerged(main, "first-t", lambda t: None)
	record = main / ".hook-only" / "auto-merge"
	record.mkdir(parents=True, exist_ok=True)
	(record / "merges.jsonl").write_text(json.dumps({"topic": "first-t", "commit": git(main, "rev-parse", "HEAD")}) + "\n")
	assert wt_queue.isMerged(main, "first-t", lambda t: None)


def mergeRecord(main, topic, commit, t):
	record = main / ".hook-only" / "auto-merge"
	record.mkdir(parents=True, exist_ok=True)
	with open(record / "merges.jsonl", "a") as f:
		f.write(json.dumps({"t": t, "topic": topic, "branch": f"feature/docs/{topic}", "commit": commit}) + "\n")


def test_an_old_merge_record_does_not_count_for_a_topic_queued_again(main):
	# a topic name planned again: its merge record from before this start says nothing about the new one
	planned(main, "first-t")
	planned(main, "second-t", after=["first-t"])
	mergeRecord(main, "first-t", git(main, "rev-parse", "HEAD"), "2026-01-01T00:00:00")
	wt_queue.add(main, ["first-t", "second-t"])
	calls, rows = started(main)
	assert calls == ["first-t"]
	assert "waits for first-t" in rows["second-t"]["waits"]


def test_a_dependency_still_waiting_in_the_queue_is_never_merged(main):
	planned(main, "zero-t")
	planned(main, "first-t", after=["zero-t"])
	planned(main, "second-t", after=["first-t"])
	mergeRecord(main, "first-t", git(main, "rev-parse", "HEAD"), "2026-01-01T00:00:00")
	wt_queue.add(main, ["first-t", "second-t"])
	calls, rows = started(main)
	assert calls == [] and "waits for first-t" in rows["second-t"]["waits"]


def test_a_merge_record_counts_only_for_the_branch_it_merged(main):
	planned(main, "first-t")
	mergeRecord(main, "first-t", git(main, "rev-parse", "HEAD"), "2026-01-01T00:00:00")
	git(main, "checkout", "-q", "-b", "feature/docs/first-t")
	(main / "f").write_text("x\n")
	git(main, "add", "f")
	git(main, "commit", "-q", "-m", "first-t again")
	git(main, "checkout", "-q", "main")
	assert not wt_queue.isMerged(main, "first-t", lambda t: None)  # the record's commit lacks this tip
	git(main, "merge", "-q", "--no-ff", "-m", "merge first-t", "feature/docs/first-t")
	mergeRecord(main, "first-t", git(main, "rev-parse", "HEAD"), time.strftime("%Y-%m-%dT%H:%M:%S"))
	assert wt_queue.isMerged(main, "first-t", lambda t: None)


def test_a_failed_start_is_kept_with_its_error_and_not_retried(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])

	def failing(topic, entry):
		raise wt_queue.Refused("new-worktree.sh: FAIL: boom")
	assert wt_queue.tick(main, start=failing, quota=lambda: None) == [{"topic": "alpha-t", "error": "new-worktree.sh: FAIL: boom"}]
	assert started(main)[0] == []
	assert wt_queue.status(main)["items"] == [{"topic": "alpha-t", "error": "new-worktree.sh: FAIL: boom"}]


def test_nothing_runs_outside_local_mode(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	(main / ".hook-only" / "flow-mode").write_text("origin\n")
	assert started(main)[0] == []


def test_a_repo_limit_counts_only_once_its_flow_json_is_committed(main):
	(main / ".claude").mkdir()
	(main / ".claude" / "flow.json").write_text(json.dumps({"queue": {"queue_workers_max": 1}}))
	assert wt_queue.limits(main)["workers"] == DEFAULTS["queue_workers_max"]  # not committed yet
	git(main, "add", ".claude/flow.json")
	git(main, "commit", "-q", "-m", "flow")
	assert wt_queue.limits(main)["workers"] == 1


def commitFlow(main, text):
	(main / ".claude").mkdir(exist_ok=True)
	(main / ".claude" / "flow.json").write_text(text)
	git(main, "add", ".claude/flow.json")
	git(main, "commit", "-q", "-m", "flow")


def personalConfig(tmp_path, monkeypatch, overrides):
	d = config_fixtures.copyConfig(os.path.join(os.path.dirname(HOOKS), "config"), tmp_path / "config")
	(d / "local.json").write_text(json.dumps({"budget": overrides}))
	config_fixtures.useConfig(monkeypatch, d)


def test_each_queue_limit_is_the_smaller_of_the_repo_and_the_person_with_its_source(main):
	assert wt_queue.limits(main) == {"workers": DEFAULTS["queue_workers_max"], "usd": DEFAULTS["queue_max_concurrent_usd"],
		"source": {"workers": "home", "usd": "home"}}
	commitFlow(main, json.dumps({"queue": {"queue_workers_max": 9, "queue_max_concurrent_usd": 1.5}}))
	assert wt_queue.limits(main) == {"workers": DEFAULTS["queue_workers_max"], "usd": 1.5,
		"source": {"workers": "home", "usd": "repo"}}


def test_a_local_override_is_the_person_s_limit_and_says_local(main, tmp_path, monkeypatch):
	personalConfig(tmp_path, monkeypatch, {"queue_workers_max": 1, "queue_max_concurrent_usd": 50.0, "queue_quota_unknown": "start"})
	commitFlow(main, json.dumps({"queue": {"queue_max_concurrent_usd": 30}}))
	assert wt_queue.limits(main) == {"workers": 1, "usd": 30.0, "source": {"workers": "local", "usd": "repo"}}
	planned(main, "alpha-t")
	planned(main, "beta-t")
	wt_queue.add(main, ["alpha-t", "beta-t"])
	assert started(main)[0] == ["alpha-t"]
	why = {row["topic"]: row.get("why") for row in wt_queue.status(main)["items"]}["beta-t"]
	assert "(queue_workers_max 1, from local)" in why


def test_an_invalid_local_limit_uses_home_and_says_so(main, tmp_path, monkeypatch):
	personalConfig(tmp_path, monkeypatch, {"queue_workers_max": "many"})
	assert wt_queue.limits(main)["workers"] == DEFAULTS["queue_workers_max"]
	assert wt_queue.limits(main)["source"]["workers"] == "home (local invalid)"


@pytest.mark.parametrize("text, words", [
	("{half", ["not valid JSON"]),
	(json.dumps({"queue": [1]}), ['"queue" must be an object']),
	(json.dumps({"queue": {"queue_max_workers": 1}}), ["queue.queue_max_workers was renamed to queue.queue_workers_max"]),
	(json.dumps({"queue": {"max_workers": 1}}), ["unknown key queue.max_workers"]),
	(json.dumps({"queue": {"queue_workers_max": 0}}), ["must be a number above 0"]),
	(json.dumps({"queue": {"queue_max_concurrent_usd": "5"}}), ["must be a number above 0"]),
])
def test_a_broken_flow_json_is_an_error_never_ignored(main, text, words):
	commitFlow(main, text)
	for call in (wt_queue.limits, wt_queue.status):
		with pytest.raises(homeconfig.ConfigError) as error:
			call(main)
		assert ".claude/flow.json at main" in str(error.value)
		for word in words:
			assert word in str(error.value)
	with pytest.raises(homeconfig.ConfigError):  # a tick starts nothing
		wt_queue.tick(main, start=lambda topic, entry: "x", quota=lambda: None)


def test_drop_takes_only_a_waiting_item(main):
	planned(main, "alpha-t")
	planned(main, "beta-t", after=["gamma-t"])  # waits: gamma-t never merged
	wt_queue.add(main, ["alpha-t", "beta-t"])
	assert started(main)[0] == ["alpha-t"]
	wt_queue.drop(main, "beta-t")
	assert [i["topic"] for i in wt_queue.loadState(main)["items"]] == ["alpha-t"]
	with pytest.raises(wt_queue.Refused, match="not waiting"):
		wt_queue.drop(main, "alpha-t")


# --- the real start: new-worktree, plan, estimate, worker-control -------------------------------

def test_materialize_and_launch_go_through_new_worktree_and_worker_control(main, tmp_path, monkeypatch):
	planned(main, "alpha-docs")
	fake = tmp_path / "new-worktree.sh"
	fake.write_text("#!/usr/bin/env bash\nset -e\ntopic=${1##*/}\n"
		"git worktree add -q --no-track -b \"$1\" \"$(dirname \"$(pwd)\")/worktrees/$topic\" main\n")
	entry = wt_queue.spec(main, "alpha-docs")
	wt_queue.materialize(main, "alpha-docs", entry, newWorktree=fake)
	worktree = main.parent / "worktrees" / "alpha-docs"
	assert (worktree / "plans" / "alpha-docs.md").read_text() == "# alpha-docs\n"
	assert json.loads((main / "plans" / "worker" / "alpha-docs.json").read_text()) == entry["estimate"]
	assert git(worktree, "symbolic-ref", "--short", "HEAD") == "feature/docs/alpha-docs"

	home = tmp_path / "home"
	(home / ".claude" / "hooks" / "worker").mkdir(parents=True)
	wrapper = home / ".claude" / "hooks" / "worker" / "run-worker.sh"
	wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
	wrapper.chmod(0o755)
	monkeypatch.setenv("HOME", str(home))
	context = wt_queue.launch(main, "alpha-docs")
	assert context.startswith("wt: launch alpha-docs (local mode")
	assert (worktree / ".hook-only" / "flow-mode").read_text().strip() == "local"


def test_worker_control_queue_prompts(main, tmp_path):
	planned(main, "first-t")
	planned(main, "second-t", after=["first-t"])

	def wt(prompt):
		result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
			input=json.dumps({"prompt": prompt, "cwd": str(main)}), capture_output=True, text=True)
		assert result.returncode == 0, result.stderr
		return json.loads(result.stdout)

	added = wt("wt queue second-t")  # shown to the person only: no model turn
	assert added["decision"] == "block" and "hookSpecificOutput" not in added
	assert "queued second-t" in added["reason"] and "second-t waits: " in added["reason"]  # quota or first-t
	shown = wt("wt queue")
	assert shown["decision"] == "block" and "second-t: waiting (" in shown["reason"]
	assert wt("wt queue drop second-t")["reason"] == "wt: queue: second-t dropped"
	assert wt("wt queue")["reason"] == "wt: queue: empty"
	assert "no plans/queue/nope-t.json" in wt("wt queue nope-t")["reason"]


# --- the subscription's usage -------------------------------------------------------------------

NOW = 1_800_000_000.0


def usage(at=NOW, **windows):
	"""The quota snapshot quota-snapshot.py writes (state kind quota-usage, in this test's scratch home state)."""
	return statefile.write(None, wt_queue.QUOTA_KIND, {"schema": 1, "at": at, **windows})


def test_a_window_at_its_limit_stops_new_starts_until_it_resets():
	limit = DEFAULTS["queue_max_five_hour_pct"]
	usage(five_hour={"used_percentage": limit, "resets_at": NOW + 3600})
	why = wt_queue.quotaReason(now=NOW)
	assert why.startswith(f"five_hour usage {limit:.0f}% >=")
	assert wt_queue.quotaReason(now=NOW + 3601) is None  # reset since: a fresh window
	# With a weekly window still live, an old snapshot says nothing current about it: unknown, so it waits.
	usage(five_hour={"used_percentage": limit, "resets_at": NOW + 3600},
		seven_day={"used_percentage": 10, "resets_at": NOW + 86400})
	assert "usage unknown" in wt_queue.quotaReason(now=NOW + 3601 + 3600)


def test_the_weekly_window_counts_too():
	usage(seven_day={"used_percentage": DEFAULTS["queue_max_seven_day_pct"] + 1, "resets_at": NOW + 86400})
	assert wt_queue.quotaReason(now=NOW).startswith("seven_day usage")


def test_below_the_limits_with_a_fresh_snapshot_starts():
	usage(five_hour={"used_percentage": 10, "resets_at": NOW + 3600})
	assert wt_queue.quotaReason(now=NOW + 60) is None


def test_no_snapshot_or_a_stale_one_is_unknown_and_waits_by_default():
	assert "no usage snapshot" in wt_queue.quotaReason(now=NOW)
	usage(five_hour={"used_percentage": 10, "resets_at": NOW + 7 * 3600})
	late = NOW + (DEFAULTS["queue_quota_max_age_minutes"] + 1) * 60
	assert "min old" in wt_queue.quotaReason(now=late)


def test_a_snapshot_without_its_schema_reads_as_none_so_usage_is_unknown():
	"""The file as quota-snapshot.py wrote it before it went through statefile (no "schema"): the same as no
	snapshot, even at 99% of a live window, until the status line writes the next one."""
	path = statefile.pathOf(None, wt_queue.QUOTA_KIND)
	os.makedirs(os.path.dirname(path))
	with open(path, "w", encoding="utf-8") as f:
		json.dump({"at": NOW, "five_hour": {"used_percentage": 99.0, "resets_at": NOW + 3600}}, f)
	assert wt_queue.quotaReason(now=NOW).startswith("usage unknown: no usage snapshot")


def test_a_stale_snapshot_whose_windows_all_reset_is_not_unknown():
	usage(five_hour={"used_percentage": 99, "resets_at": NOW + 60})
	assert wt_queue.quotaReason(now=NOW + 2 * 3600) is None


def test_a_quota_stop_is_the_first_reason_an_item_waits(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	result = wt_queue.tick(main, start=lambda t, e: "x", quota=lambda: "five_hour usage 90% >= 80% until later")
	assert result == [{"topic": "alpha-t", "waits": "five_hour usage 90% >= 80% until later"}]


# --- quota-snapshot.py --------------------------------------------------------------------------

LINE_COMMAND = ("--", sys.executable, "-c", "import json,sys; print('line ' + json.load(sys.stdin)['model']['display_name'])")


@pytest.fixture
def snap(tmp_path, monkeypatch):
	"""quota-snapshot.py run from a copy of the tree under test (config_fixtures.copyHome), so its home state is the
	copy's state/, which this test process reads too: snap(data, *command) gives (stdout, stderr, the registry path
	of the snapshot); snap.home is the copy."""
	home = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home")
	state = str(home / "state")
	monkeypatch.setattr(statefile, "homeStateDir", lambda: state)

	def run(data, *command):
		result = subprocess.run([sys.executable, str(home / "hooks" / "budget" / "quota-snapshot.py"), *command],
			input=json.dumps(data), capture_output=True, text=True, check=True)
		return result.stdout, result.stderr, statefile.pathOf(None, wt_queue.QUOTA_KIND)

	run.home = home
	return run


def stored(path):
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def test_the_status_line_wrapper_keeps_the_windows_with_their_schema_and_passes_the_input_on(snap):
	data = {"model": {"display_name": "Opus"}, "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
		"seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}
	out, err, path = snap(data, *LINE_COMMAND)
	assert out == "line Opus\n" and err == ""  # the person's own status line, unchanged
	assert path == os.path.join(snap.home, "state", "quota", "usage.json")  # <home>/state of the tree it ran from
	saved = stored(path)
	assert saved["schema"] == 1 and saved == statefile.read(None, wt_queue.QUOTA_KIND)
	assert saved["five_hour"] == {"used_percentage": 23.5, "resets_at": 1738425600.0}
	assert saved["seven_day"]["used_percentage"] == 41.2 and saved["at"] > 0


def test_without_rate_limits_nothing_is_written_and_without_a_command_it_prints_the_percentages(snap):
	out, _, path = snap({"model": {}})
	assert out == "" and not os.path.exists(path)
	out, _, path = snap({"rate_limits": {"seven_day": {"used_percentage": "5", "resets_at": 1}}})  # no usable window
	assert out == "" and not os.path.exists(path)
	out, _, _ = snap({"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 1}}})
	assert out == "5h 5%\n"


def test_a_rerun_with_the_same_numbers_keeps_the_old_time_so_it_can_go_stale(snap):
	data = {"rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600}}}
	_, _, path = snap(data)
	statefile.write(None, wt_queue.QUOTA_KIND, {**stored(path), "at": 1000.0})  # as if written long ago
	snap(data)  # an idle session's refresh: same numbers
	assert stored(path)["at"] == 1000.0
	data["rate_limits"]["five_hour"]["used_percentage"] = 24.0  # a new API response moved them
	snap(data)
	assert stored(path)["at"] > 1000.0


def test_a_snapshot_that_cant_be_written_goes_to_stderr_and_the_status_line_is_unchanged(snap):
	blocked = snap.home / "state"
	blocked.write_text("")  # the state directory can't be created: a file holds its name
	data = {"model": {"display_name": "Opus"}, "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1}}}
	out, err, _ = snap(data, *LINE_COMMAND)
	assert out == "line Opus\n"
	assert err.startswith("quota-snapshot: ") and blocked.is_file() and not (snap.home / "quota").exists()
	out, err, _ = snap(data)
	assert out == "" and err.startswith("quota-snapshot: ")  # without a command: nothing to show, no crash


# --- planned topics: the pane's buttons ---------------------------------------------------------

def test_planned_lists_specs_not_queued_and_without_a_worktree_by_order_then_name(main):
	planned(main, "zeta-t", order=1)
	planned(main, "alpha-t")
	planned(main, "beta-t", order=2, after=["zeta-t"])
	planned(main, "gone-t")
	(main.parent / "worktrees" / "gone-t").mkdir()
	planned(main, "queued-t")
	wt_queue.add(main, ["queued-t"])
	(main / "plans" / "queue" / "bad-t.json").write_text("{}")
	rows = wt_queue.status(main)["planned"]
	assert [r["topic"] for r in rows] == ["zeta-t", "beta-t", "alpha-t", "bad-t"]
	assert rows[1] == {"topic": "beta-t", "order": 2, "after": ["zeta-t"], "est_usd": 2.0}
	assert "no plans/queue/bad-t.md" in rows[3]["error"]


def test_a_dropped_topic_is_planned_again(main):
	planned(main, "alpha-t", after=["never-t"])
	wt_queue.add(main, ["alpha-t"])
	assert wt_queue.status(main)["planned"] == []
	wt_queue.drop(main, "alpha-t")
	assert [r["topic"] for r in wt_queue.status(main)["planned"]] == ["alpha-t"]


def test_showing_the_queue_runs_tick_first_so_a_manual_merge_frees_dependents(main, monkeypatch, capsys):
	import importlib.util
	spec_ = importlib.util.spec_from_file_location("worker_control", os.path.join(HOOKS, "worker", "worker-control.py"))
	control = importlib.util.module_from_spec(spec_)
	spec_.loader.exec_module(control)
	calls = []
	monkeypatch.setattr(wt_queue, "tick", lambda mainDir: calls.append(mainDir) or [{"topic": "second-t", "started": "x"}])
	monkeypatch.setattr(wt_queue, "status", lambda mainDir: {"items": []})
	with pytest.raises(SystemExit):
		control.queue_command("wt queue", main)
	assert calls == [main]
	assert "started now: second-t" in json.loads(capsys.readouterr().out)["reason"]


# --- done items leave the queue; starts are events ----------------------------------------------

def mergedBranch(main, topic):
	git(main, "checkout", "-q", "-b", f"feature/docs/{topic}")
	(main / f"{topic}.txt").write_text("x\n")
	git(main, "add", f"{topic}.txt")
	git(main, "commit", "-q", "-m", topic)
	git(main, "checkout", "-q", "main")
	git(main, "merge", "-q", "--no-ff", "-m", f"merge {topic}", f"feature/docs/{topic}")


def test_a_merged_finished_topic_is_marked_done_its_files_archived_then_removed(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	assert started(main)[0] == ["alpha-t"]
	(main.parent / "worktrees" / "alpha-t" / ".git").unlink()  # the fake run's worktree, then removed
	import shutil
	shutil.rmtree(main.parent / "worktrees" / "alpha-t")
	mergedBranch(main, "alpha-t")
	state = wt_queue.loadState(main)
	assert wt_queue.prune(main, state, now=1000.0) is True
	assert state["items"][0]["done"] == 1000.0
	assert not (main / "plans" / "queue" / "alpha-t.md").exists()
	assert (main / ".hook-only" / "archive" / "alpha-t" / "queue" / "alpha-t.json").exists()
	assert wt_queue.prune(main, state, now=1000.0 + 60) is False  # within the linger: kept, nothing new
	linger = DEFAULTS["queue_done_linger_minutes"] * 60
	assert wt_queue.prune(main, state, now=1000.0 + linger + 1) is True and state["items"] == []


def test_status_shows_a_done_item_as_merged_while_it_lingers(main, monkeypatch):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	state = wt_queue.loadState(main)
	import time as _time
	state["items"][0].update({"started": "x", "done": _time.time()})
	wt_queue.saveState(main, state)
	assert wt_queue.status(main)["items"] == [{"topic": "alpha-t", "state": "merged", "run": None}]
	state["items"][0]["done"] = _time.time() - DEFAULTS["queue_done_linger_minutes"] * 60 - 5
	wt_queue.saveState(main, state)
	assert wt_queue.status(main)["items"] == []


def test_a_failed_start_can_be_dropped_and_is_an_event(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])

	def failing(topic, entry):
		raise wt_queue.Refused("new-worktree.sh: FAIL: boom")
	wt_queue.tick(main, start=failing, quota=lambda: None)
	events = [json.loads(l) for l in (main / ".hook-only" / "events.jsonl").read_text().splitlines()]
	assert events[-1]["kind"] == "queue-failed" and "boom" in events[-1]["text"]
	wt_queue.drop(main, "alpha-t")
	assert wt_queue.loadState(main)["items"] == []


def test_a_start_is_an_event(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	started(main)
	events = [json.loads(l) for l in (main / ".hook-only" / "events.jsonl").read_text().splitlines()]
	assert events[-1]["kind"] == "queue-started" and events[-1]["topic"] == "alpha-t"


# --- wt merge (worker-control) ------------------------------------------------------------------

def treeHome(main):
	"""A home whose ~/.claude/hooks and skills are the tree under test: `wt merge` starts run-steps.py, which runs
	the follow-up programs from ~/.claude/hooks (never the real home's)."""
	home = main.parent.parent / "home"
	if not home.exists():
		(home / ".claude").mkdir(parents=True)
		os.symlink(RUN_HOOKS, home / ".claude" / "hooks")
		os.symlink(RUN_SKILLS, home / ".claude" / "skills")
	return home


def wtMerge(main, topic):
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": f"wt merge {topic}", "cwd": str(main)}), capture_output=True, text=True,
		env={**os.environ, "HOME": str(treeHome(main))})
	assert result.returncode == 0, result.stderr
	out = json.loads(result.stdout)
	assert out["decision"] == "block"  # shown to the person only
	return out["reason"]


def heldTopic(main, topic, decision):
	realWorktree(main, topic)
	busy(main, topic, ended=True)
	worktree = main.parent / "worktrees" / topic
	(worktree / ".hook-only" / "flow-mode").write_text("local\n")
	(worktree / ".hook-only" / "runs" / "merge-1.json").write_text(json.dumps(decision))


@pytest.mark.parametrize("decision, message", [
	({"decision": "merged", "commit": "abc"}, "was merged, nothing to approve"),
	({"decision": "refused", "reason": "behind", "merge_run": True}, "needs a merge run first"),
])
def test_wt_merge_refuses_what_is_not_a_held_refusal(main, decision, message):
	heldTopic(main, "alpha-t", decision)
	assert message in wtMerge(main, "alpha-t")


@pytest.mark.parametrize("record", [None, "origin", "local"])
def test_wt_merge_follows_the_topic_flow_mode(main, monkeypatch, flowMode, record):
	# the topic's record decides, else the main checkout's (flowMode here: the main record, no override); PR mode
	# has nothing to approve here
	monkeypatch.delenv("FLOW_INTEGRATION", raising=False)
	(main / ".hook-only" / "flow-mode").write_text(flowMode + "\n")
	realWorktree(main, "alpha-t")
	busy(main, "alpha-t", ended=True)
	worktree = main.parent / "worktrees" / "alpha-t"
	if record:
		(worktree / ".hook-only" / "flow-mode").write_text(record + "\n")
	(worktree / ".hook-only" / "runs" / "merge-1.json").write_text(json.dumps({"decision": "merged", "commit": "abc"}))
	reason = wtMerge(main, "alpha-t")
	if (record or flowMode) == "local":
		assert "was merged, nothing to approve" in reason
	else:
		assert "runs in PR mode" in reason


def test_wt_merge_refuses_a_running_topic_and_one_without_a_worktree(main):
	realWorktree(main, "alpha-t")
	busy(main, "alpha-t")  # no exit file: running
	(main.parent / "worktrees" / "alpha-t" / ".hook-only" / "flow-mode").write_text("local\n")
	assert "still going" in wtMerge(main, "alpha-t")
	assert "has no worktree" in wtMerge(main, "nope-t")


def test_wt_merge_starts_the_persons_merge_in_the_background(main):
	heldTopic(main, "alpha-t", {"decision": "refused", "reason": "a person must review these paths: .claude/x"})
	assert "approving alpha-t run 1" in wtMerge(main, "alpha-t")


# --- one approval per run at a time (a second press must not start a second auto-merge) -----

@contextlib.contextmanager
def autoMergeLocked(main):
	"""Hold auto-merge.py's lock: a started approval waits on it, alive, until the block ends."""
	lockDir = main / ".hook-only" / "auto-merge"
	lockDir.mkdir(parents=True, exist_ok=True)
	with open(lockDir / "lock", "w") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		yield


def approval(main, topic, run=1):
	path = main.parent / "worktrees" / topic / ".hook-only" / "runs" / f"approve-{run}.json"
	return json.loads(path.read_text()) if path.exists() else None


def waitGone(main, topic, run=1, seconds=60):
	deadline = time.time() + seconds
	while approval(main, topic, run) is not None and time.time() < deadline:
		time.sleep(0.1)
	return approval(main, topic, run) is None


def cmdline(pid):
	try:
		return open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
	except OSError:
		return []


def test_two_wt_merge_in_a_row_start_one_approval(main):
	heldTopic(main, "alpha-t", {"decision": "refused", "reason": "a person must review these paths: .claude/x"})
	with autoMergeLocked(main):
		assert "approving alpha-t run 1" in wtMerge(main, "alpha-t")
		record = approval(main, "alpha-t")
		# the approving process is run-steps.py step merge --person (migration step 6), which runs auto-merge.py
		assert any(arg.endswith(b"run-steps.py") for arg in cmdline(record["pid"]))
		second = wtMerge(main, "alpha-t")
		assert "merge: alpha-t run 1 is already being approved (since " in second
		assert approval(main, "alpha-t") == record  # no second auto-merge, the record untouched
	# run-steps.py removes the record when its steps end, whatever auto-merge.py decided
	assert waitGone(main, "alpha-t")
	assert json.loads((main.parent / "worktrees" / "alpha-t" / ".hook-only" / "runs" / "merge-1.json")
		.read_text())["decision"] == "refused"


@pytest.mark.parametrize("owner", ["dead", "not auto-merge"])
def test_a_stale_approval_record_allows_a_new_approval(main, owner):
	heldTopic(main, "alpha-t", {"decision": "refused", "reason": "a person must review these paths: .claude/x"})
	if owner == "dead":
		gone = subprocess.Popen(["true"])
		gone.wait()
		pid = gone.pid
	else:
		pid = os.getpid()
	runs = main.parent / "worktrees" / "alpha-t" / ".hook-only" / "runs"
	(runs / "approve-1.json").write_text(json.dumps({"run": 1, "pid": pid, "started": "2026-11-02T07:45:00+0800",
		"started_epoch": 0}))
	with autoMergeLocked(main):
		assert "approving alpha-t run 1" in wtMerge(main, "alpha-t")
		assert approval(main, "alpha-t")["pid"] != pid
	assert waitGone(main, "alpha-t")


def test_an_approval_record_of_an_earlier_run_never_blocks(main):
	heldTopic(main, "alpha-t", {"decision": "refused", "reason": "x"})
	runs = main.parent / "worktrees" / "alpha-t" / ".hook-only" / "runs"
	with autoMergeLocked(main):
		assert "approving alpha-t run 1" in wtMerge(main, "alpha-t")
		(main / ".hook-only" / "workers" / "alpha-t.json").write_text(json.dumps({"session_id": SESSION, "run": 2}))
		(runs / "exit-2").write_text("0 2026-11-01T11:00:00+08:00\n")
		(runs / "merge-2.json").write_text(json.dumps({"decision": "refused", "reason": "x"}))
		assert "approving alpha-t run 2" in wtMerge(main, "alpha-t")  # run 1's live approval doesn't block run 2
	assert waitGone(main, "alpha-t", 1) and waitGone(main, "alpha-t", 2)


def test_a_tick_in_test_mode_outside_a_rehearsal_repo_creates_no_worktree(main, monkeypatch):
	"""Rehearsal-1 residual: the config-mode refusal comes before materialize, so a TEST mode tick (from a wrap-up or
	auto-finish, not through worker-control) makes no worktree and marks nothing started: the item waits."""
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	monkeypatch.setattr(wt_queue.config_mode_guard, "refusal", lambda mainDir: "config mode is TEST (x)")
	made = []
	monkeypatch.setattr(wt_queue, "materialize", lambda *args, **kwargs: made.append(args))
	rows = {row["topic"]: row for row in wt_queue.tick(main, quota=lambda: None)}
	assert made == [] and not (main.parent / "worktrees" / "alpha-t").exists()
	assert rows["alpha-t"] == {"topic": "alpha-t", "waits": "config mode is TEST (x)"}
	assert not wt_queue.loadState(main)["items"][0].get("started")

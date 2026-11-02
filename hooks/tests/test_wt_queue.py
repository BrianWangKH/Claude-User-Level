"""wt_queue.py: the person's queued plans start in order within the limits (2026-11-01).

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

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOOKS)

import wt_queue  # noqa: E402

DEFAULTS = json.load(open(os.path.join(HOOKS, "budget-defaults.json"), encoding="utf-8"))
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


def busy(main, topic, usd=2.0, ended=False, auto=None):
	"""A topic with a worktree and a run: going, or ended (with an automatic decision, maybe)."""
	runs = main.parent / "worktrees" / topic / ".hook-only" / "runs"
	runs.mkdir(parents=True)
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
	assert "queue_max_workers" in rows["beta-t"]["waits"] and "queue_max_workers" in rows["gamma-t"]["waits"]


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
	# 2026-11-01: a queued topic's new branch points at main's head until its first commit; being an ancestor
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
	(record / "merges.jsonl").write_text(json.dumps({"topic": "first-t", "commit": "x"}) + "\n")
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


def test_a_repo_overrides_the_limits_from_its_committed_flow_json(main):
	(main / ".claude").mkdir()
	(main / ".claude" / "flow.json").write_text(json.dumps({"queue": {"queue_max_workers": 1}}))
	assert wt_queue.limits(main)["workers"] == DEFAULTS["queue_max_workers"]  # not committed yet
	git(main, "add", ".claude/flow.json")
	git(main, "commit", "-q", "-m", "flow")
	assert wt_queue.limits(main)["workers"] == 1


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
	(home / ".claude" / "hooks").mkdir(parents=True)
	(home / ".claude" / "hooks" / "budget-defaults.json").write_text(json.dumps(DEFAULTS))
	wrapper = home / ".claude" / "hooks" / "run-worker.sh"
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
		result = subprocess.run([sys.executable, os.path.join(HOOKS, "worker-control.py")],
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


def usage(tmp_path, at=NOW, **windows):
	path = tmp_path / "usage.json"
	path.write_text(json.dumps({"at": at, **windows}))
	return path


def test_a_window_at_its_limit_stops_new_starts_until_it_resets(tmp_path):
	limit = DEFAULTS["queue_max_five_hour_pct"]
	path = usage(tmp_path, five_hour={"used_percentage": limit, "resets_at": NOW + 3600})
	why = wt_queue.quotaReason(now=NOW, usageFile=path)
	assert why.startswith(f"five_hour usage {limit:.0f}% >=")
	assert wt_queue.quotaReason(now=NOW + 3601, usageFile=path) is None  # reset since: a fresh window
	# With a weekly window still live, an old snapshot says nothing current about it: unknown, so it waits.
	both = usage(tmp_path, five_hour={"used_percentage": limit, "resets_at": NOW + 3600},
		seven_day={"used_percentage": 10, "resets_at": NOW + 86400})
	assert "usage unknown" in wt_queue.quotaReason(now=NOW + 3601 + 3600, usageFile=both)


def test_the_weekly_window_counts_too(tmp_path):
	path = usage(tmp_path, seven_day={"used_percentage": DEFAULTS["queue_max_seven_day_pct"] + 1, "resets_at": NOW + 86400})
	assert wt_queue.quotaReason(now=NOW, usageFile=path).startswith("seven_day usage")


def test_below_the_limits_with_a_fresh_snapshot_starts(tmp_path):
	path = usage(tmp_path, five_hour={"used_percentage": 10, "resets_at": NOW + 3600})
	assert wt_queue.quotaReason(now=NOW + 60, usageFile=path) is None


def test_no_snapshot_or_a_stale_one_is_unknown_and_waits_by_default(tmp_path):
	assert "no usage snapshot" in wt_queue.quotaReason(now=NOW, usageFile=tmp_path / "none.json")
	old = usage(tmp_path, five_hour={"used_percentage": 10, "resets_at": NOW + 7 * 3600})
	late = NOW + (DEFAULTS["queue_quota_max_age_minutes"] + 1) * 60
	assert "min old" in wt_queue.quotaReason(now=late, usageFile=old)


def test_a_stale_snapshot_whose_windows_all_reset_is_not_unknown(tmp_path):
	path = usage(tmp_path, five_hour={"used_percentage": 99, "resets_at": NOW + 60})
	assert wt_queue.quotaReason(now=NOW + 2 * 3600, usageFile=path) is None


def test_a_quota_stop_is_the_first_reason_an_item_waits(main):
	planned(main, "alpha-t")
	wt_queue.add(main, ["alpha-t"])
	result = wt_queue.tick(main, start=lambda t, e: "x", quota=lambda: "five_hour usage 90% >= 80% until later")
	assert result == [{"topic": "alpha-t", "waits": "five_hour usage 90% >= 80% until later"}]


# --- quota-snapshot.py --------------------------------------------------------------------------

def snap(tmp_path, data, *command):
	home = tmp_path / "home"
	(home / "hooks").mkdir(parents=True, exist_ok=True)
	script = home / "hooks" / "quota-snapshot.py"
	script.write_text(open(os.path.join(HOOKS, "quota-snapshot.py"), encoding="utf-8").read())
	result = subprocess.run([sys.executable, str(script), *command], input=json.dumps(data), capture_output=True,
		text=True, check=True)
	return result.stdout, home / "quota" / "usage.json"


def test_the_status_line_wrapper_keeps_the_windows_and_passes_the_input_on(tmp_path):
	data = {"model": {"display_name": "Opus"}, "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
		"seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}
	out, path = snap(tmp_path, data, "--", sys.executable, "-c",
		"import json,sys; print('line ' + json.load(sys.stdin)['model']['display_name'])")
	assert out == "line Opus\n"  # the person's own status line, unchanged
	saved = json.loads(path.read_text())
	assert saved["five_hour"] == {"used_percentage": 23.5, "resets_at": 1738425600.0}
	assert saved["seven_day"]["used_percentage"] == 41.2 and saved["at"] > 0


def test_without_rate_limits_nothing_is_written_and_without_a_command_it_prints_the_percentages(tmp_path):
	out, path = snap(tmp_path, {"model": {}})
	assert out == "" and not path.exists()
	out, _ = snap(tmp_path / "b", {"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 1}}})
	assert out == "5h 5%\n"


def test_a_rerun_with_the_same_numbers_keeps_the_old_time_so_it_can_go_stale(tmp_path):
	data = {"rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600}}}
	_, path = snap(tmp_path, data)
	first = json.loads(path.read_text())
	path.write_text(json.dumps({**first, "at": 1000.0}))  # as if written long ago
	snap(tmp_path, data)  # an idle session's refresh: same numbers
	assert json.loads(path.read_text())["at"] == 1000.0
	data["rate_limits"]["five_hour"]["used_percentage"] = 24.0  # a new API response moved them
	snap(tmp_path, data)
	assert json.loads(path.read_text())["at"] > 1000.0


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
	spec_ = importlib.util.spec_from_file_location("worker_control", os.path.join(HOOKS, "worker-control.py"))
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

def wtMerge(main, topic):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "worker-control.py")],
		input=json.dumps({"prompt": f"wt merge {topic}", "cwd": str(main)}), capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	out = json.loads(result.stdout)
	assert out["decision"] == "block"  # shown to the person only
	return out["reason"]


def heldTopic(main, topic, decision):
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


def test_wt_merge_refuses_a_running_topic_and_one_without_a_worktree(main):
	busy(main, "alpha-t")  # no exit file: running
	(main.parent / "worktrees" / "alpha-t" / ".hook-only" / "flow-mode").write_text("local\n")
	assert "still going" in wtMerge(main, "alpha-t")
	assert "has no worktree" in wtMerge(main, "nope-t")


def test_wt_merge_starts_the_persons_merge_in_the_background(main):
	heldTopic(main, "alpha-t", {"decision": "refused", "reason": "a person must review these paths: .claude/x"})
	assert "approving alpha-t run 1" in wtMerge(main, "alpha-t")


# --- one approval per run at a time (2026-11-02: a second press started a second auto-merge) -----

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
		assert any(arg.endswith(b"auto-merge.py") for arg in cmdline(record["pid"]))
		second = wtMerge(main, "alpha-t")
		assert "merge: alpha-t run 1 is already being approved (since " in second
		assert approval(main, "alpha-t") == record  # no second auto-merge, the record untouched
	# auto-merge.py removes the record when it ends, whatever it decided
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

"""A flow-mode record that can't be read (homeconfig.flowMode raises ConfigError) is an error, never PR mode
read quietly: `wt launch` / `wt resume` / `wt merge` refuse with the reason and start nothing
(worker-control.py checkedFlowMode), and the queue's tick fails loudly instead of skipping, with the person told:
the pane's flow_mode_error and queue_error (wt_controls.py), a refusal of a typed `wt queue` (worker-control.py
configRefusal: one line naming the record, exit 2, never a traceback), the tick's exit code (run-steps.py records
it as the queue_tick step's error).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_flow_mode_errors.py -q -p no:cacheprovider'
"""

import json
import os
import subprocess
import sys

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import homeconfig  # noqa: E402
import wt_queue  # noqa: E402

TOPIC = "alpha-t"
BROKEN = "bogus"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
	"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
		env={**os.environ, **GIT_ENV}).stdout.strip()


@pytest.fixture(autouse=True)
def recordsDecide(monkeypatch):
	"""The records decide the mode here: no FLOW_INTEGRATION test override (conftest.py)."""
	monkeypatch.delenv("FLOW_INTEGRATION", raising=False)


@pytest.fixture
def main(tmp_path):
	"""A main checkout in local mode with no queue usage snapshot (the home state is a scratch one, conftest)."""
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


def worktreeOf(main):
	return main.parent / "worktrees" / TOPIC


def plannedWorktree(main, record=BROKEN):
	"""The topic's worktree with its flow-mode record, a run settings file and a fake wrapper home."""
	worktree = worktreeOf(main)
	git(main, "worktree", "add", "-q", "-b", f"feature/docs/{TOPIC}", str(worktree))
	(worktree / ".hook-only").mkdir()
	(worktree / ".hook-only" / "flow-mode").write_text(record + "\n")
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / f"{TOPIC}.json").write_text(json.dumps({"minutes": 20, "est_usd": 2}))
	return worktree


def fakeHome(main):
	"""A home whose run-worker.sh only exits: a launch that got through would start nothing real."""
	home = main.parent.parent / "home"
	wrapper = home / ".claude" / "hooks" / "worker" / "run-worker.sh"
	wrapper.parent.mkdir(parents=True)
	wrapper.write_text("#!/usr/bin/env bash\nexit 0\n")
	wrapper.chmod(0o755)
	return home


def workerControl(main, prompt, home):
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": prompt, "cwd": str(main)}), capture_output=True, text=True, timeout=120,
		env={**os.environ, "HOME": str(home)})
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def nothingStarted(main, worktree):
	runs = worktree / ".hook-only" / "runs"
	assert not runs.exists() or not os.listdir(runs)
	assert not (main / ".hook-only" / "workers" / f"{TOPIC}.json").exists()
	assert (worktree / ".hook-only" / "flow-mode").read_text() == BROKEN + "\n"  # record_flow_mode never ran


# -- wt launch / resume / merge (worker-control.py checkedFlowMode) -----------------------------------------


@pytest.mark.parametrize("action", ["launch", "resume"])
def test_wt_launch_and_resume_refuse_an_unreadable_topic_flow_mode(main, action):
	"""Integration: a topic record that isn't a mode blocks the prompt with the reason, and no run starts."""
	worktree = plannedWorktree(main)
	out = workerControl(main, f"wt {action} {TOPIC}", fakeHome(main))
	assert out["decision"] == "block"
	assert out["reason"].startswith(f"wt: {TOPIC}: the flow mode can't be read (")
	assert f"flow mode '{BROKEN}' is not one of origin, local" in out["reason"]
	assert out["reason"].endswith("nothing was started or stopped")
	nothingStarted(main, worktree)


def test_wt_merge_refuses_an_unreadable_topic_flow_mode(main):
	"""Integration: `wt merge` on a topic whose record isn't a mode blocks with the reason; no approval starts."""
	worktree = plannedWorktree(main)
	out = workerControl(main, f"wt merge {TOPIC}", fakeHome(main))
	assert out["decision"] == "block"
	assert out["reason"].startswith(f"wt: merge: {TOPIC}: the flow mode can't be read (")
	assert out["reason"].endswith("nothing was started or stopped")
	nothingStarted(main, worktree)


# -- the queue's tick on a broken main record ---------------------------------------------------------------


def queued(main):
	(main / "plans" / "queue" / f"{TOPIC}.md").write_text(f"# {TOPIC}\n")
	(main / "plans" / "queue" / f"{TOPIC}.json").write_text(json.dumps({"branch": f"feature/docs/{TOPIC}",
		"estimate": {"minutes": 20, "est_usd": 2.0, "raw_minutes": 20, "raw_usd": 2.0, "class": "docs"}}))
	wt_queue.add(main, [TOPIC])
	(main / ".hook-only" / "flow-mode").write_text(BROKEN + "\n")


def test_a_tick_on_a_broken_main_record_raises_and_starts_nothing(main):
	"""Unit: the tick raises the ConfigError naming the record (it used to read PR mode and skip quietly)."""
	queued(main)
	before = (main / ".hook-only" / "queue" / "queue.json").read_text()
	starts = []
	with pytest.raises(homeconfig.ConfigError, match=f"flow-mode: flow mode '{BROKEN}' is not one of origin, local"):
		wt_queue.tick(main, start=lambda topic, entry: starts.append(topic), quota=lambda: None)
	assert starts == []
	assert (main / ".hook-only" / "queue" / "queue.json").read_text() == before


def test_the_tick_program_exits_non_zero_with_the_reason(main):
	"""Integration: `wt_queue.py tick` (what run-worker.sh's wrap-up and auto-finish run) exits non-zero with
	the ConfigError last on stderr; run-steps.py records that exit as the queue_tick step's error."""
	queued(main)
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "wt_queue.py"), "tick", str(main)],
		capture_output=True, text=True, timeout=120)
	assert result.returncode != 0 and result.stdout == ""
	assert result.stderr.strip().splitlines()[-1].startswith("homeconfig.ConfigError: ")
	assert f"'{BROKEN}' is not one of origin, local" in result.stderr


def test_the_pane_shows_the_broken_record_and_the_queue_error(main):
	"""Integration: wt_controls.py (what the pane draws) gives no mode but flow_mode_error with the reason, and
	queue_error for the queue it can't read; the pane shows both (mods/wt-pane control.ts). The queue is empty, so
	nothing reads a usage snapshot."""
	(main / ".hook-only" / "flow-mode").write_text(BROKEN + "\n")
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "wt_controls.py"), str(main)],
		capture_output=True, text=True, timeout=120)
	assert result.returncode == 0, result.stderr
	out = json.loads(result.stdout)
	assert out["flow_mode"] is None
	assert out["flow_mode_error"].startswith("flow mode unreadable: ")
	assert f"'{BROKEN}' is not one of origin, local" in out["flow_mode_error"]
	assert out["queue_error"].startswith("wt_queue.py status failed: ")
	assert f"'{BROKEN}'" in out["queue_error"]


@pytest.mark.parametrize("prompt", ["wt queue", f"wt queue {TOPIC}"])
def test_a_typed_wt_queue_on_a_broken_record_is_one_line_refusal_never_a_traceback(main, prompt):
	"""Integration: worker-control.py alone: the tick (wt queue) or the add (wt queue <topic>) raises the
	ConfigError; the program answers exit 2 with one stderr line naming the record and the value, nothing on
	stdout."""
	queued(main)
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": prompt, "cwd": str(main)}), capture_output=True, text=True, timeout=120)
	assert result.returncode == 2 and result.stdout == ""
	lines = result.stderr.strip().splitlines()
	assert len(lines) == 1 and "Traceback" not in result.stderr, result.stderr
	assert lines[0].startswith(f"{' '.join(prompt.split()[:2])}: refused, a setting can't be read: ")
	assert f"{os.path.join(main, '.hook-only', 'flow-mode')}: flow mode '{BROKEN}' is not one of origin, local" in lines[0]


def test_a_topic_queued_before_the_tick_fails_is_named_as_queued(main):
	"""Integration: a valid flow-mode record but a .claude/flow.json at main that isn't JSON: the add succeeds and
	the tick raises (wt_queue.limits); the one line says the topic is queued and nothing started, never that the
	prompt was refused."""
	(main / ".claude").mkdir()
	(main / ".claude" / "flow.json").write_text("{oops")
	git(main, "add", ".claude/flow.json")
	git(main, "commit", "-q", "-m", "broken flow.json")
	(main / "plans" / "queue" / f"{TOPIC}.md").write_text(f"# {TOPIC}\n")
	(main / "plans" / "queue" / f"{TOPIC}.json").write_text(json.dumps({"branch": f"feature/docs/{TOPIC}",
		"estimate": {"minutes": 20, "est_usd": 2.0, "raw_minutes": 20, "raw_usd": 2.0, "class": "docs"}}))
	result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "worker", "worker-control.py")],
		input=json.dumps({"prompt": f"wt queue {TOPIC}", "cwd": str(main)}), capture_output=True, text=True, timeout=120)
	assert result.returncode == 2 and "Traceback" not in result.stderr, result.stderr
	line, = result.stderr.strip().splitlines()
	assert line.startswith(f"wt queue: queued {TOPIC}, but nothing starts while a setting can't be read: ")
	assert "flow.json" in line and "not valid JSON" in line
	queue = json.loads((main / ".hook-only" / "queue" / "queue.json").read_text())
	assert [(i["topic"], i.get("started")) for i in queue["items"]] == [(TOPIC, None)]


def test_a_typed_wt_queue_is_refused_with_the_reason_every_time(main):
	"""Integration: `wt queue` through hook-entry.py's prompt entry: worker-control.py's exit 2 is the entry's
	block with the one line as its reason (the prompt never reaches the session), each time it is typed; no
	hook-failed warning."""
	queued(main)
	home = main.parent.parent / "home"
	(home / ".claude").mkdir(parents=True)
	os.symlink(RUN_HOOKS, home / ".claude" / "hooks")
	os.symlink(os.path.join(config_fixtures.cleanHome(), "skills"), home / ".claude" / "skills")
	environ = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	environ.update({"HOME": str(home), "CLAUDE_PROJECT_DIR": str(main)})
	data = {"hook_event_name": "UserPromptSubmit", "prompt": "wt queue", "cwd": str(main), "session_id": "s"}
	for _ in range(2):
		result = subprocess.run([sys.executable, os.path.join(RUN_HOOKS, "hook-entry.py"), "prompt"],
			input=json.dumps(data), capture_output=True, text=True, env=environ, timeout=120, cwd=str(main))
		assert result.returncode == 0, result.stderr
		out = json.loads(result.stdout)
		assert out["decision"] == "block" and "Traceback" not in out["reason"]
		assert out["reason"].startswith("wt queue: refused, a setting can't be read: ")
		assert f"flow-mode: flow mode '{BROKEN}'" in out["reason"]
		assert "worker-control:" not in (out.get("systemMessage") or "")

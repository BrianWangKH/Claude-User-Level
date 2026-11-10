"""hooks/lib/config_mode_guard.py and its use in worker-control.py: while config/mode is TEST no worker starts
outside a rehearsal repo (launch, resume, queue, auto-start, start_run itself), every wt answer carries the mode's
line, and DEV is unchanged.

worker-control.py runs from a copy of the tree under test (config_fixtures.copyHome) whose config/ holds the mode
under test; HOME is a scratch home whose run-worker.sh only records its arguments.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import config_mode_guard  # noqa: E402
import homeconfig  # noqa: E402
import statefile  # noqa: E402

TOPIC = "guard-topic"
RESTORE = "printf 'DEV\\n' >"


def git(*args):
	return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
	"""A tree copy, a scratch home with a recording run-worker.sh, a main checkout with the topic's worktree and
	estimate; setMode(text) writes the copy's config/mode (and a valid local.test.json)."""
	tree = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "tree", tops=("skills", "agents"))
	(tree / "config" / "local.test.json").write_text("{}")
	config_fixtures.useConfig(monkeypatch, tree / "config")
	home = tmp_path / "home"
	wrapper = home / ".claude" / "hooks" / "worker" / "run-worker.sh"
	wrapper.parent.mkdir(parents=True)
	args = tmp_path / "args.txt"
	wrapper.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {args}\n")
	wrapper.chmod(0o755)
	main = tmp_path / "proj" / "main"
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / f"{TOPIC}.json").write_text(json.dumps({"minutes": 10, "est_usd": 1}))
	git("init", "-q", "-b", "main", str(main))
	git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	(main / ".hook-only").mkdir()
	(main / ".hook-only" / "flow-mode").write_text("local\n")
	git("-C", str(main), "worktree", "add", "-q", "-b", f"feature/x/{TOPIC}", str(tmp_path / "proj" / "worktrees" / TOPIC))

	def setMode(text):
		(tree / "config" / "mode").write_text(text)

	return {"tree": tree, "home": home, "main": main, "args": args, "setMode": setMode, "tmp": tmp_path}


def wt(world, prompt):
	env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_") and k != "FLOW_INTEGRATION"}
	result = subprocess.run([sys.executable, str(world["tree"] / "hooks" / "worker" / "worker-control.py")],
		input=json.dumps({"prompt": prompt, "cwd": str(world["main"])}), capture_output=True, text=True, timeout=60,
		env={**env, "HOME": str(world["home"])})
	assert result.returncode == 0, result.stderr
	data = json.loads(result.stdout)
	return data.get("decision", ""), data.get("reason") or data["hookSpecificOutput"]["additionalContext"]


def runs(world):
	folder = world["main"].parent / "worktrees" / TOPIC / ".hook-only" / "runs"
	return sorted(os.listdir(folder)) if folder.is_dir() else []


# --- the module ---------------------------------------------------------------------------------------------

def test_dev_refuses_nothing_and_shows_no_line(world):
	assert config_mode_guard.refusal(world["main"]) is None and config_mode_guard.statusLine() is None
	world["setMode"]("DEV\n")
	assert config_mode_guard.refusal(world["main"]) is None and config_mode_guard.statusLine() is None


def test_test_refuses_an_unmarked_repo_and_allows_a_marked_one(world):
	world["setMode"]("TEST\n")
	why = config_mode_guard.refusal(world["main"])
	assert "config mode is TEST" in why and "no rehearsal repo" in why and RESTORE in why
	config_mode_guard.mark(world["main"], "a")
	assert config_mode_guard.refusal(world["main"]) is None
	assert config_mode_guard.statusLine().startswith("config mode: TEST (only rehearsal repos start workers;")


@pytest.mark.parametrize("problem", ["unknown mode", "broken marker", "marker of another schema"])
def test_what_cant_be_read_refuses(world, problem):
	world["setMode"]("TEST\n")
	if problem == "unknown mode":
		config_mode_guard.mark(world["main"], "a")
		world["setMode"]("PROD\n")
	else:
		path = statefile.pathOf(str(world["main"]), config_mode_guard.KIND)
		os.makedirs(os.path.dirname(path))
		with open(path, "w") as f:
			f.write("{oops" if problem == "broken marker" else json.dumps({"schema": 99}))
	why = config_mode_guard.refusal(world["main"])
	assert why is not None and "no worker starts here" in why and RESTORE in why


# --- worker-control ----------------------------------------------------------------------------------------

def test_dev_launch_is_unchanged_and_carries_no_mode_line(world):
	decision, text = wt(world, f"wt launch {TOPIC}")
	assert decision == "" and text.startswith(f"wt: launch {TOPIC}") and "config mode" not in text
	assert world["args"].exists() and runs(world)


def test_test_launch_in_a_repo_that_isnt_a_rehearsal_is_refused_before_anything_is_written(world):
	world["setMode"]("TEST\n")
	decision, text = wt(world, f"wt launch {TOPIC}")
	assert decision == "block" and "no rehearsal repo" in text and RESTORE in text
	assert text.splitlines()[-1].startswith("config mode: TEST")
	assert not world["args"].exists() and runs(world) == []


def test_test_launch_in_a_rehearsal_repo_starts(world):
	world["setMode"]("TEST\n")
	config_mode_guard.mark(world["main"], "a")
	decision, text = wt(world, f"wt launch {TOPIC}")
	assert decision == "" and text.startswith(f"wt: launch {TOPIC}") and world["args"].exists()
	assert text.splitlines()[-1].startswith("config mode: TEST")


def test_test_queue_is_refused_outside_a_rehearsal_and_a_drop_still_works(world):
	world["setMode"]("TEST\n")
	for prompt in ("wt queue", f"wt queue {TOPIC}"):
		decision, text = wt(world, prompt)
		assert decision == "block" and "queue: config mode is TEST" in text, prompt
	decision, text = wt(world, f"wt queue drop {TOPIC}")
	assert "config mode is TEST" not in text.splitlines()[0]


def test_wt_status_carries_the_mode_line_only_while_test(world):
	_, text = wt(world, f"wt status {TOPIC}")
	assert "config mode" not in text
	world["setMode"]("TEST\n")
	_, text = wt(world, f"wt status {TOPIC}")
	assert text.splitlines()[-1].startswith("config mode: TEST") and text.count("config mode: TEST") == 1


def loadWorkerControl(world):
	spec = importlib.util.spec_from_file_location("worker_control_guard", homepath.locate("worker-control.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_start_run_itself_refuses_in_test_outside_a_rehearsal(world):
	world["setMode"]("TEST\n")
	control = loadWorkerControl(world)
	worktree = world["main"].parent / "worktrees" / TOPIC
	with pytest.raises(homeconfig.ConfigError, match="no rehearsal repo"):
		control.start_run(world["main"], worktree, TOPIC, world["tmp"] / "state.json", "new", "s", "p", 10, 1.0, 1.25,
			13, "", {}, {}, {"started_by": "person", "prompt_kind": "feature-flow"})
	assert runs(world) == [] and not (world["tmp"] / "state.json").exists()


def test_auto_start_in_test_outside_a_rehearsal_starts_nothing_and_says_why(world, monkeypatch):
	world["setMode"]("TEST\n")
	control = loadWorkerControl(world)
	state = world["main"] / ".hook-only" / "workers" / f"{TOPIC}.json"
	state.parent.mkdir(parents=True)
	state.write_text(json.dumps({"run": 1, "session_id": "s"}))
	monkeypatch.setenv("WT_MAIN_DIR", str(world["main"]))
	control.auto_start(world["main"].parent / "worktrees" / TOPIC, TOPIC, 1)
	events = (world["main"] / ".hook-only" / "events.jsonl").read_text().splitlines()
	assert len(events) == 1 and "no automatic start run: config mode is TEST" in json.loads(events[0])["text"]
	assert not world["args"].exists()

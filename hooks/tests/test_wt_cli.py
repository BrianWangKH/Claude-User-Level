"""The command lines other programs run as a subprocess keep their bytes: `wt_controls.py <main> [--local on|off]`
(the wt-pane mod draws its JSON) and `wt_queue.py status|tick <main>` (run-steps.py, auto-finish.py,
worker-control.py). wt_cli.golden.json holds what the tree before the shared-module stage (wt_topics.py)
printed for the same checkout in each flow mode: stdout, stderr and the exit code of each command,
the scratch directory written as <tmp>. Only states whose output doesn't depend on the wall clock are built
(ended runs, no run, held, unstarted, the queue's started, broken and failed items, planned topics), since
the subprocess reads the real time; no queue item waits, so the subscription usage snapshot (wt_queue.QUOTA_KIND)
is never read. The commands run from a copy of the tree (config_fixtures.copyHome), so their settings and home
state are the copy's, never the person's.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_wt_cli.py -q -p no:cacheprovider'
"""

import json
import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(HOOKS, "tests", "wt_cli.golden.json")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import config_fixtures  # noqa: E402
import homeconfig  # noqa: E402
import hookonly  # noqa: E402
from run_fixtures import START, TOPIC, FakeClock, addTopic, build, legacyFile  # noqa: E402
from run_fixtures import makeMain as makeCheckout  # noqa: E402

COMMANDS = {
	"controls": ["wt_controls.py", "{main}"],
	"controls_local_on": ["wt_controls.py", "{main}", "--local", "on"],
	"controls_local_off": ["wt_controls.py", "{main}", "--local", "off"],
	"controls_usage": ["wt_controls.py"],
	"queue_status": ["wt_queue.py", "status", "{main}"],
	"queue_tick": ["wt_queue.py", "tick", "{main}"],
	"queue_usage": ["wt_queue.py", "status"],
}


def writeJson(path, value):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		f.write(json.dumps(value))


def estimate(main, topic):
	writeJson(os.path.join(main, "plans", "worker", f"{topic}.json"), {"minutes": 20, "est_usd": 2})


def queueSpec(main, topic, order):
	folder = os.path.join(main, "plans", "queue")
	writeJson(
		os.path.join(folder, f"{topic}.json"),
		{
			"branch": f"feature/x/{topic}",
			"after": [],
			"resources": [],
			"order": order,
			"estimate": {"minutes": 20, "est_usd": 2.0},
		},
	)
	with open(os.path.join(folder, f"{topic}.md"), "w", encoding="utf-8") as f:
		f.write(f"# {topic}\n")


def scenario(tmp_path, monkeypatch):
	"""A main checkout with one topic per clock-free state and a queue (module doc)."""
	main = makeCheckout(tmp_path, monkeypatch)
	# The test's stated mode as the main checkout's record: the wt programs run as subprocesses read the records
	# (no variable is a source; conftest's FLOW_DEFAULT reaches this process only).
	os.makedirs(hookonly.stateDir(str(main), ""), exist_ok=True)
	with open(os.path.join(hookonly.stateDir(str(main), ""), "flow-mode"), "w", encoding="utf-8") as f:
		f.write(homeconfig.FLOW_DEFAULT + "\n")
	fake = FakeClock(START)
	clock.useSource(fake.now)
	try:
		build(main, fake, "record", "ended")
		merged = addTopic(main, "merge-wait")
		estimate(main, "merge-wait")
		legacyFile(merged, "est-1.json", {"minutes": 20, "usd": 2.0, "cap": 2.6, "cap_minutes": 26}, 900, fake.now())
		legacyFile(merged, "start-1", START.isoformat(timespec="seconds") + "\n", 900, fake.now())
		legacyFile(
			merged,
			"result-1.json",
			{"result": "STATUS: done\n\nall green", "num_turns": 3, "total_cost_usd": 1.0},
			60,
			fake.now(),
		)
		legacyFile(merged, "exit-1", f"0 {START.isoformat(timespec='seconds')}\n", 30, fake.now())
		legacyFile(merged, "merge-1.json", {"decision": "refused", "reason": "a .claude/ file changed"}, 20, fake.now())
		held = addTopic(main, "held-topic")
		estimate(main, "held-topic")
		guard = hookonly.stateDir(str(held), "agent-guard")
		os.makedirs(guard)
		with open(os.path.join(guard, "HOLD"), "w", encoding="utf-8") as f:
			f.write("held by the test\n")
		addTopic(main, "fresh-topic")
		estimate(main, "fresh-topic")
		estimate(main, "no-worktree")
		queueSpec(main, "planned-one", 2)
		queueSpec(main, "planned-first", 1)
		queueSpec(main, "started-one", 3)
		writeJson(
			os.path.join(hookonly.stateDir(str(main), "queue"), "queue.json"),
			{
				"items": [
					{"topic": "started-one", "added": "2026-11-03T08:00:00", "started": "2026-11-03T08:00:30"},
					{"topic": "broken-one", "added": "2026-11-03T08:01:00"},
					{
						"topic": "failed-one",
						"added": "2026-11-03T08:02:00",
						"started": "2026-11-03T08:03:00",
						"error": "new-worktree.sh: no such branch",
					},
				]
			},
		)
	finally:
		clock.useSource(None)
	assert TOPIC != "merge-wait"
	return main


def outputs(tmp_path, monkeypatch):
	"""{command: {"code", "stdout", "stderr"}} of COMMANDS run on the scenario, <tmp> for the scratch path."""
	main = scenario(tmp_path, monkeypatch)
	home = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home")  # its config and state, not the person's
	out = {}
	for name, argv in COMMANDS.items():
		program = os.path.join(home, "hooks", os.path.relpath(homepath.locate(argv[0]), HOOKS))
		args = [sys.executable, program, *(a.format(main=main) for a in argv[1:])]
		result = subprocess.run(args, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
		out[name] = {
			"code": result.returncode,
			"stdout": result.stdout.replace(str(tmp_path), "<tmp>"),
			"stderr": result.stderr.replace(str(tmp_path), "<tmp>"),
		}
	return out


# Added after the golden, outside its bytes: wt_controls.py "queue_limits" and the "source" of wt_queue.py status
# "limits" (each effective queue limit's origin: home, local or repo; the config merge), and
# wt_controls.py "config_mode" (the home's config mode, DEV in a copy without config/mode).
LIMITS = {"workers": 2, "usd": 20.0, "source": {"workers": "home", "usd": "home"}}


def withoutConfigMergeFields(out, flowMode):
	"""out with the config merge's additions taken out of the JSON stdouts after checking their values; every
	other byte stays as printed."""
	for name, result in out.items():
		if result["code"] != 0 or not result["stdout"].startswith("{"):
			continue
		data = json.loads(result["stdout"])
		if name.startswith("controls"):
			assert data.pop("queue_limits") == LIMITS, flowMode
			assert data.pop("config_mode") == "DEV", flowMode
		if name == "queue_status":
			assert data["limits"].pop("source") == LIMITS["source"], flowMode
		result["stdout"] = json.dumps(data) + "\n"
	return out


def test_the_wt_command_lines_print_what_they_printed_before_the_shared_module(tmp_path, monkeypatch, flowMode):
	with open(GOLDEN, encoding="utf-8") as f:
		golden = json.load(f)["outputs"][flowMode]
	assert withoutConfigMergeFields(outputs(tmp_path, monkeypatch), flowMode) == golden


def test_the_golden_outputs_cover_rows_queue_and_errors():
	"""The scenario reaches the parts of the output the moved helpers feed (module doc)."""
	with open(GOLDEN, encoding="utf-8") as f:
		golden = json.load(f)["outputs"]
	for mode in ("origin", "local"):
		controls = json.loads(golden[mode]["controls"]["stdout"])
		topics = {row["topic"] for row in controls["controls"]}
		assert topics == {TOPIC, "merge-wait", "held-topic", "fresh-topic"}
		assert [row["topic"] for row in controls["planned"]] == ["planned-first", "planned-one"]
		assert {item["topic"] for item in controls["queue"]} == {"started-one", "broken-one", "failed-one"}
		assert "usage" not in golden[mode]["queue_status"]["stdout"] + golden[mode]["queue_tick"]["stdout"]
		assert golden[mode]["controls_usage"]["code"] == 1 and golden[mode]["queue_usage"]["code"] == 1


@pytest.fixture(autouse=True)
def noRealClock():
	yield
	clock.useSource(None)

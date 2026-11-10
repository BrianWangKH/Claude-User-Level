"""Migration step 6 of the run record: run-worker.sh's decisions (the claude calls) and its wrap-up moved into
run-steps.py, each wrap-up step idempotent and safe to run again.

1. Equivalence: the wrapper before step 6 (run_worker_baseline.SCRIPT) and today's run-worker.sh run the same
   scenario in two fresh worlds, with a stub claude and stub follow-up programs that log their arguments; each
   scenario must give the same program calls in the same order, the same claude calls (prompts, budgets, session
   flags, the compact marker), the same files of today's layout with the same content (times aside), the same
   record facts the programs and the wrapper write, and the same status-diverged events. INTENDED_DIFFERENCES lists
   every difference the move makes on purpose; the harness checks the measurable ones.
2. T-10 idempotence, crashes (a step program killed or raising, a runner that died, the attempt limit), T-11, the
   person's merge (`step merge --person`), and the dangerous directions T-12 and T-13 against today's wrapper.
3. What run-worker.sh's header promises and nothing else checked: every claude call gets the run's minutes as
   CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS, an unreadable worker_phases_max ends the run as a config error before
   claude, a stop before the first claude call starts no claude, a wrap-up that crashes outside its steps ends the
   run wrapup-failed (not an orphan; the person's wrapup-failed event) and `run-steps.py wrapup` run again
   continues it; and the merge step's check of the run record's integration_mode against the topic's mode.
4. Nothing fails silently: a step closed failed is the person's step-failed event, a failed queue tick their
   queue-failed, a detached program's output lands in the run's stderr log, any other command's crash is exit 1
   with a hook-failed event, and finish.json is read as the registered finish-record kind.
No real claude, no network; the stubs answer at once unless a scenario makes claude wait for a signal.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_run_steps.py -q -p no:cacheprovider'
"""

import argparse
import contextlib
import datetime
import importlib.util
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(os.path.dirname(HOOKS), "config")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import eventlog  # noqa: E402
import homeconfig  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
from run_worker_baseline import SCRIPT as BASELINE  # noqa: E402
import wrapup_stop_baseline  # noqa: E402

TOPIC = "topic"
SESSION = "11111111-2222-3333-4444-555555555555"
WAIT_SECONDS = 30
GONE_PID = 999999999  # no such process

INTENDED_DIFFERENCES = (
	"who starts the follow-ups: before step 6 auto-merge.py itself started auto-finish.py (after a merge) and "
	"worker-control.py auto-merge / auto-fix (a refusal with merge_run / fix_run); now run-steps.py's finish and "
	"next steps start them, with the same arguments and in the same order (the baseline world's stub auto-merge.py "
	"starts them as the old auto-merge.py did, today's world's stub only decides, like today's auto-merge.py)",
	"the run record keeps every wrap-up step (steps.verify ... steps.queue_tick: done or skipped with a reason, "
	"attempt, runner_pid, started_at); before step 6 only the steps a program wrote were there",
	"claude.exit_code and claude.stopped in the record (run-steps.py claude-ended), so a wrap-up run again by hand "
	"decides from the wrapper's facts",
	"`wt merge` starts run-steps.py step merge --person (approval pid and command line are run-steps.py's), which "
	"runs auto-merge.py --person, then the next and finish steps, then releases the approval",
	"a stop that arrives before the first claude call ends the run without starting claude (the wrapper used to "
	"start it anyway); a stop during verify.py also skips verifier.py (it used to share verify.py's condition)",
	"a wrap-up that fails (run-steps.py itself, not a step program) leaves exit-<run> unwritten and ends the "
	"record's run wrapup-failed with the error (never an orphan); run-steps.py wrapup run again continues it and "
	"writes the real end over it (the bash wrap-up always wrote exit-<run>, also after a broken step)",
)

STUB_CLAUDE = r"""#!/usr/bin/env python3
import json, os, signal, sys, time
stub = os.environ["STUB_DIR"]
scenario = json.loads(os.environ["STUB_SCENARIO"])
def log(text):
    with open(os.path.join(stub, "claude.log"), "a") as f:
        f.write(text + "\n")
if sys.argv[1:2] == ["auth"]:
    if scenario.get("auth_sleep"):  # a window before the first claude call (a stop sent while auth runs)
        open(os.path.join(stub, "auth-started"), "w").close()
        time.sleep(scenario["auth_sleep"])
    sys.exit(int(scenario.get("auth", 0)))
ignored = int(next(l for l in open("/proc/self/status") if l.startswith("SigIgn")).split()[1], 16)
got = []
for sig in (signal.SIGINT, signal.SIGTERM):
    if not ignored & (1 << (sig - 1)):
        signal.signal(sig, lambda n, f: got.append(n))
prompt = sys.argv[sys.argv.index("-p") + 1]
compact = prompt.startswith("/compact")
log("call " + json.dumps(sys.argv[1:]) + " compact=" + os.environ.get("CLAUDE_WORKER_COMPACT", "")
    + " minutes=" + os.environ.get("CLAUDE_WORKER_MINUTES", "")
    + " bg-wait=" + os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "") + " int-ignored=" + str(bool(ignored & 2)))
calls = os.path.join(stub, "calls")
n = int(open(calls).read()) if os.path.exists(calls) else 0
if compact:
    turn = {"result": "Compacted.", "cost": scenario.get("compact_cost", 0.01)}
else:
    turn = scenario["turns"][n]
    turn = {"result": turn} if isinstance(turn, str) else turn
    open(calls, "w").write(str(n + 1))
open(os.path.join(stub, "started"), "a").write("x")
end = time.time() + turn.get("sleep", 0)
while time.time() < end and not got:
    time.sleep(0.02)
for stop in turn.get("stops", []):  # the turn's Stops through the tree's worker-stop.py, each then a wait
    if got:
        break
    hook = os.path.join(os.path.dirname(os.environ["STUB_HOOKS"]), "worker", "worker-stop.py")
    payload = {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": stop.get("message", turn["result"]),
        "background_tasks": stop.get("tasks", []), "stop_hook_active": False}
    import subprocess
    subprocess.run([sys.executable, hook], input=json.dumps(payload), text=True, capture_output=True, timeout=60,
        env={**os.environ, "CLAUDE_PROJECT_DIR": os.getcwd()})
    log("stop " + payload["last_assistant_message"].splitlines()[0])
    end = time.time() + stop.get("wait", 0)
    while time.time() < end and not got:
        time.sleep(0.02)
if got:
    time.sleep(0.2)
    log("exit 7 after " + signal.Signals(got[0]).name)
    sys.exit(7)
totalFile = os.path.join(stub, "total")
total = float(open(totalFile).read()) if os.path.exists(totalFile) else scenario.get("start_cost", 0)
total = round(total + turn.get("cost", 0.1), 4)
open(totalFile, "w").write(str(total))
if turn.get("auto_resume"):
    with open(os.path.join(".hook-only", "runs", "auto-resume-" + os.environ["STUB_RUN"] + ".json"), "w") as f:
        json.dump({"minutes": 5, "usd": 1.0}, f)
print(json.dumps({"result": turn["result"], "total_cost_usd": total}))
sys.exit(turn.get("exit", 0))
"""

STUB_PROGRAM = r"""import json, os, signal, subprocess, sys, time
name = os.path.basename(sys.argv[0])
env = os.environ
def norm(a):
    return a.replace(env["STUB_WT"], "W").replace(env["STUB_MAIN"], "M")
with open(env["STUB_LOG"], "a") as f:
    f.write(" ".join([name] + [norm(a) for a in sys.argv[1:]]) + "\n")
sys.path.insert(0, env["STUB_HOOKS"])
import runstate
scenario = json.loads(env["STUB_SCENARIO"])
home = os.path.dirname(os.path.abspath(sys.argv[0]))
def runsOf(wt):
    return os.path.join(wt, ".hook-only", "runs")
def write(wt, kind, run, data):
    with open(os.path.join(runsOf(wt), kind + "-" + str(run) + ".json"), "w") as f:
        json.dump({"run": run, **data}, f)
def detached(argv):
    subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
if name == "verify.py":
    wt, topic, run = sys.argv[1], sys.argv[2], int(sys.argv[3])
    passed = scenario.get("verify", "pass") == "pass"
    write(wt, "verify", run, {"passed": passed, "checks": [{"name": "c1", "ok": passed, "why": "differs"}]})
    runstate.dualWrite(wt, "verify", runstate.recordStepFromFile, run, "verify")
elif name == "verifier.py":
    wt, topic, run = sys.argv[1], sys.argv[2], int(sys.argv[3])
    spec = json.load(open(os.path.join(env["WT_MAIN_DIR"], "plans", "verify", topic + ".json")))
    layer1 = json.load(open(os.path.join(runsOf(wt), "verify-" + str(run) + ".json")))
    if spec.get("verifier") and layer1.get("passed"):
        write(wt, "verifier", run, {"verdict": scenario.get("verifier", "passed"), "report": "V2 fail: rule E2"})
        runstate.dualWrite(wt, "verifier", runstate.recordStepFromFile, run, "verifier")
elif name == "auto-merge.py":
    wt, topic, run = sys.argv[1], sys.argv[2], int(sys.argv[3])
    person = sys.argv[4:] == ["--person"]
    kind = scenario.get("merge_person" if person else "merge", "merged")
    if os.path.exists(os.path.join(env["STUB_DIR"], "merge-kind")):
        kind = open(os.path.join(env["STUB_DIR"], "merge-kind")).read().strip()
    if kind == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    if kind == "raise":
        raise RuntimeError("stub auto-merge.py broke")
    if kind == "silent":
        sys.exit(0)
    decision = {
        "merged": {"decision": "merged", "into": "main", "commit": "c0ffee", "review": "approved"},
        "refused": {"decision": "refused", "reason": "no APPROVE"},
        "merge_run": {"decision": "refused", "reason": "behind main", "merge_run": True},
        "fix_run": {"decision": "refused", "reason": "checks failed", **({} if person else {"fix_run": True})},
    }[kind]
    write(wt, "merge", run, decision)
    runstate.dualWrite(wt, "auto-merge", runstate.recordStepFromFile, run, "merge")
    if env.get("STUB_OLD"):  # auto-merge.py before migration step 6 started the follow-ups itself
        if decision["decision"] == "merged":
            detached([sys.executable, os.path.join(home, "auto-finish.py"), wt, topic, str(run)])
        if decision.get("merge_run") or decision.get("fix_run"):
            subprocess.run([sys.executable, os.path.join(home, "worker-control.py"),
                "auto-merge" if decision.get("merge_run") else "auto-fix", wt, topic, str(run)])
elif name == "worker-control.py":
    wt, topic, run = sys.argv[2], sys.argv[3], int(sys.argv[4])
    decision = scenario.get("next", "refused")
    extra = {"reason": "stub refused"} if decision == "refused" else {"next_run": run + 1, "minutes": 5, "usd": 1.0}
    write(wt, "auto", run, {"decision": decision, **extra})
    runstate.dualWrite(wt, "worker-control", runstate.recordStepFromFile, run, "next")
"""

PROGRAMS = (  # paths under ~/.claude/hooks
	"worker/wt_events.py",
	"gates/verify.py",
	"gates/verifier.py",
	"worker/auto-merge.py",
	"worker/worker-control.py",
	"worker/auto-finish.py",
	"worker/wt_queue.py",
)
SPEC = {"checks": [{"name": "c1", "run": ["true"]}], "data_paths": []}
VERIFIER_SPEC = {**SPEC, "verifier": {"usd": 1, "minutes": 5, "judge": ["x"]}}
DONE = "STATUS: done\nall green"

# name -> scenario: turns (claude's non-compact turns: text or {result, cost, exit, sleep, auto_resume, stops: the
# turn's Stops [{message (default the result), tasks, wait}] run through the tree's worker-stop.py, each followed
# by a wait), mode
# (local/origin), merge (auto-merge.py's decision), next (worker-control.py's), spec, verify, verifier, signal, usd,
# phases (worker_phases_max), scripted, agent, run/session (resume with earlier spend), auth, auth_sleep (seconds the
# auth check takes, the stub's "auth-started" file marking its start).
SCENARIOS = {
	"done-merged": {"turns": [DONE], "merge": "merged"},
	"done-refused": {"turns": [DONE], "merge": "refused"},
	"refused-merge-run": {"turns": [DONE], "merge": "merge_run", "next": "merge"},
	"refused-merge-run-refused-next": {"turns": [DONE], "merge": "merge_run", "next": "refused"},
	"refused-fix-run": {
		"turns": [DONE],
		"spec": VERIFIER_SPEC,
		"verifier": "failed",
		"merge": "fix_run",
		"next": "fix",
	},
	"blocked-auto-resume": {"turns": [{"result": "STATUS: blocked\nno room", "auto_resume": True}], "next": "resume"},
	"blocked-without-dispatch-stop": {"turns": ["STATUS: blocked\nwaiting"]},
	"needs-decision": {"turns": ["STATUS: needs-decision\nwhich one?"]},
	"stop-wt-stop": {"turns": [{"result": DONE, "sleep": 10}], "signal": "wt-stop"},
	"stop-int-group": {"turns": [{"result": DONE, "sleep": 10}], "signal": "int-group"},
	"stop-term-wrapper": {"turns": [{"result": DONE, "sleep": 10}], "signal": "term-wrapper"},
	"claude-nonzero": {"turns": [{"result": "boom", "exit": 3}]},
	"phase-continuation": {"turns": ["PHASE: tests-done\nnext dev", DONE], "merge": "merged", "agent": "orch-x"},
	"phase-limit": {"turns": ["PHASE: a", "PHASE: b"], "phases": 1},
	"scripted-continue": {"turns": ["PHASE: tests-done", DONE], "scripted": True, "merge": "refused"},
	"budget-before-compact": {"turns": [{"result": "PHASE: a", "cost": 0.58}], "usd": 0.6},
	"budget-before-continue": {"turns": [{"result": "PHASE: a", "cost": 0.3}], "compact_cost": 0.29, "usd": 0.6},
	"budget-before-final-compact": {"turns": [{"result": DONE, "cost": 0.58}], "usd": 0.6, "merge": "refused"},
	"origin-done": {"turns": [DONE], "mode": "origin", "spec": SPEC},
	"origin-blocked": {
		"turns": [{"result": "STATUS: blocked\nno room", "auto_resume": True}],
		"mode": "origin",
		"next": "resume",
	},
	"spec-verify-pass-no-verifier": {"turns": [DONE], "spec": SPEC, "merge": "merged"},
	"spec-verify-fail": {"turns": [DONE], "spec": VERIFIER_SPEC, "verify": "fail", "merge": "fix_run", "next": "fix"},
	"spec-verifier-pass": {"turns": [DONE], "spec": VERIFIER_SPEC, "verifier": "passed", "merge": "merged"},
	"not-logged-in": {"turns": [DONE], "auth": 1},
	"resume-with-earlier-spend": {"turns": [DONE], "run": 2, "start_cost": 1.0, "usd": 1.5, "merge": "merged"},
	"done-with-trailing-text": {"turns": ["STATUS: done, with notes\nx"], "merge": "refused"},
	"status-spelled-loosely": {"turns": ["STATUS:  done\nx"]},
}


def git(*args):
	return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def todayLayout(script):
	"""The baseline wrapper with its paths moved to today's hooks/ layout (stage 7a: the role directories,
	hooks/lib/homepath.py) and its mode read the way today's loader allows (worker self-repair 3b: no variable is a
	source, so `homeconfig.py env FLOW_INTEGRATION`, which read the value the baseline exported from the topic's
	record, becomes `homeconfig.py flow-mode` of the same record); its logic is unchanged: the same programs,
	arguments and order."""
	old = 'hooks_dir=$(dirname "$(readlink -f "$0")")\nsteps="$hooks_dir/run-steps.py"'
	assert script.count(old) == 1
	script = script.replace(old, 'hooks_dir=$(dirname "$(dirname "$(readlink -f "$0")")")\nsteps="$hooks_dir/worker/run-steps.py"')
	mode = 'homeconfig.py" env FLOW_INTEGRATION'
	assert script.count(mode) == 1
	script = script.replace(mode, 'homeconfig.py" flow-mode "$worktree"')
	for program in PROGRAMS:
		name = os.path.basename(program)
		if name != "auto-finish.py":  # the baseline's auto-merge.py started it, never the wrapper
			assert f'/.claude/hooks/{name}"' in script, name
		script = script.replace(f'/.claude/hooks/{name}"', f'/.claude/hooks/{program}"')
	return script


def copyTree(root, scriptText):
	"""A copy of the home hooks (top level and the role directories, tests aside) and config (home.json, registry/,
	the loader), its run-worker.sh the given script."""
	for folder in ["", *homepath.ROLE_DIRS]:
		(root / "hooks" / folder).mkdir(parents=True, exist_ok=True)
		for entry in os.listdir(os.path.join(HOOKS, folder)):
			path = os.path.join(HOOKS, folder, entry)
			if os.path.isfile(path):
				shutil.copy2(path, root / "hooks" / folder / entry)
	config_fixtures.copyConfig(CONFIG, root / "config")
	if scriptText is not None:
		(root / "hooks" / "worker" / "run-worker.sh").write_text(scriptText)


def makeWorld(base, scenario, old):
	"""A main checkout, the topic's worktree with its run record launched as worker-control.py writes it, a tree
	with the wrapper (the baseline when old), a home with the stub claude and stub programs."""
	base.mkdir(parents=True)
	main = base / "proj" / "main"
	git("init", "-q", "-b", "main", str(main))
	git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
	wt = base / "proj" / "worktrees" / TOPIC
	git("-C", str(main), "worktree", "add", "-q", "-b", f"feature/x/{TOPIC}", str(wt))
	main, wt = main.resolve(), wt.resolve()
	runs = wt / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	(wt / ".hook-only" / "flow-mode").write_text(scenario.get("mode", "local") + "\n")
	if scenario.get("scripted"):
		(wt / ".hook-only" / "flow").mkdir()
		(wt / ".hook-only" / "flow" / "scripted").write_text("")
	if "spec" in scenario:
		(main / "plans" / "verify").mkdir(parents=True)
		(main / "plans" / "verify" / f"{TOPIC}.json").write_text(json.dumps(scenario["spec"]))
	tree = base / "tree"
	copyTree(tree, todayLayout(BASELINE) if old else None)
	(tree / "config" / "local.json").write_text(
		json.dumps(
			{
				"budget": {
					"worker_phases_max": scenario.get("phases", 3),
					"run_state": {"heartbeat_interval_seconds": 30},
				}
			}
		)
	)
	home = base / "home"
	(home / ".local" / "bin").mkdir(parents=True)
	(home / ".local" / "bin" / "claude").write_text(STUB_CLAUDE)
	(home / ".local" / "bin" / "claude").chmod(0o755)
	for program in PROGRAMS:
		(home / ".claude" / "hooks" / program).parent.mkdir(parents=True, exist_ok=True)
		(home / ".claude" / "hooks" / program).write_text(STUB_PROGRAM)
	stub = base / "stub"
	stub.mkdir()
	run = scenario.get("run", 1)
	if run > 1:
		(runs / f"start-{run - 1}").write_text("2026-11-05T08:00:00+08:00\n")
		(runs / f"result-{run - 1}.json").write_text(json.dumps({"result": "STATUS: blocked", "total_cost_usd": 1.0}))
		(runs / f"exit-{run - 1}").write_text("0 2026-11-05T08:10:00+08:00\n")
	(runs / f"est-{run}.json").write_text(json.dumps({"minutes": 10, "usd": 2, "cap": 3, "cap_minutes": 13}))
	runstate.launchRecord(
		str(wt),
		run,
		{
			"topic": TOPIC,
			"session_id": SESSION,
			"session_mode": "resume" if run > 1 else "new",
			"started_by": "person",
			"purpose": "build",
			"integration_mode": scenario.get("mode", "local"),
			"flow": "scripted" if scenario.get("scripted") else "written",
			"agent": scenario.get("agent"),
			"prompt_kind": "feature-flow",
			"estimate": {"minutes": 10, "usd": 2, "cap_minutes": 13, "cap_usd": 3},
			"launch": {"launched_at": "2026-11-05T09:00:00+08:00", "by_pid": os.getpid(), "by_run": None},
		},
	)
	env = {k: v for k, v in os.environ.items() if k != "FLOW_INTEGRATION"}
	env.update(
		{
			"HOME": str(home),
			"WT_MAIN_DIR": str(main),
			"STUB_DIR": str(stub),
			"STUB_LOG": str(stub / "programs.log"),
			"STUB_WT": str(wt),
			"STUB_MAIN": str(main),
			"STUB_HOOKS": str(tree / "hooks" / "lib"),
			"STUB_SCENARIO": json.dumps(scenario),
			"STUB_RUN": str(run),
		}
	)
	if old:
		env["STUB_OLD"] = "1"
	return {"main": main, "wt": wt, "runs": runs, "tree": tree, "home": home, "stub": stub, "env": env, "run": run}


def waitFor(check, seconds=WAIT_SECONDS):
	deadline = time.time() + seconds
	while time.time() < deadline:
		if check():
			return True
		time.sleep(0.05)
	return check()


def programLog(world):
	path = world["stub"] / "programs.log"
	return path.read_text().splitlines() if path.exists() else []


def runScenario(world, scenario):
	run = world["run"]
	argv = [
		"bash",
		str(world["tree"] / "hooks" / "worker" / "run-worker.sh"),
		TOPIC,
		str(world["wt"]),
		str(run),
		"resume" if run > 1 else "new",
		SESSION,
		"13",
		str(scenario.get("usd", 3)),
		scenario.get("agent") or "",
		f"/feature-flow {TOPIC}",
	]
	process = subprocess.Popen(
		argv,
		cwd=world["wt"],
		start_new_session=True,
		env=world["env"],
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	how = scenario.get("signal")
	if how:
		assert waitFor(lambda: (world["stub"] / "started").exists()), "claude never started"
		time.sleep(0.2)
		if how == "wt-stop":  # as worker-control.py stopRun: the marker, the record's stop, INT to the group
			(world["runs"] / f"stopped-{run}").write_text("2026-11-05T09:01:00+0800")
			runstate.recordStop(str(world["wt"]), run, "person")
			os.killpg(process.pid, signal.SIGINT)
		elif how == "int-group":
			os.killpg(process.pid, signal.SIGINT)
		else:
			os.kill(process.pid, signal.SIGTERM)
	assert process.wait(timeout=WAIT_SECONDS) == 0
	if scenario.get("merge") == "merged" and scenario.get("mode", "local") == "local" and not how:
		assert waitFor(lambda: any(line.startswith("auto-finish.py") for line in programLog(world))), "no auto-finish"
	return process.pid


def todaysFiles(world):
	"""The run's files of today's layout, by name: content (times aside; logs only by name)."""
	found = {}
	for name in sorted(os.listdir(world["runs"])):
		if not runstate.LEGACY_FILE.match(name):
			continue
		text = (world["runs"] / name).read_text()
		if name.startswith(("stderr-", "auth-", "start-")):
			text = "<present>"
		elif name.startswith("exit-"):
			text = text.split()[0]
		elif name.startswith("stopped-"):
			text = "<present>"
		elif name.startswith("main-"):
			text = "<main head>" if text.strip() == git("-C", str(world["main"]), "rev-parse", "HEAD") else text
		found[name] = text
	return found


def recordFacts(world):
	record, problem = runstate.readRecord(str(world["wt"]), world["run"])
	assert record is not None, problem
	claude = record.get("claude") or {}
	steps = record.get("steps") or {}
	return {
		"claude": {k: claude.get(k) for k in ("outcome", "status", "header_file")},
		"invocations": [(c["kind"], c["file"], c["exit_code"], c["cost_usd"]) for c in claude.get("invocations") or []],
		"exit": {k: (record.get("exit") or {}).get(k) for k in ("reason", "code")},
		"process": {k: (record.get("process") or {}).get(k) for k in ("exit_code", "exit_signal")},
		"main_head": record.get("main_head_at_start") == git("-C", str(world["main"]), "rev-parse", "HEAD"),
		"stop": bool(record.get("stop")),
		"programs' steps": {
			name: (steps.get(name) or {}).get("outcome")
			for name in ("verify", "verifier", "merge", "next")
			if (steps.get(name) or {}).get("state") == "done"
		},
	}


def observed(world):
	log = programLog(world)
	claude = world["stub"] / "claude.log"
	return {
		"programs": [line for line in log if not line.startswith("auto-finish.py")],
		"detached": sorted(line for line in log if line.startswith("auto-finish.py")),
		"claude": claude.read_text().splitlines() if claude.exists() else [],
		"files": todaysFiles(world),
		"record": recordFacts(world),
		"diverged": sorted(e["data"]["field"] for e in eventlog.read(str(world["wt"]), event="status-diverged")),
	}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_old_and_new_wrapper_make_the_same_calls_and_files(tmp_path, name):
	scenario = SCENARIOS[name]
	seen = {}
	for label, old in (("old", True), ("new", False)):
		world = makeWorld(tmp_path / label, scenario, old)
		runScenario(world, scenario)
		seen[label] = observed(world)
		seen[label + "-world"] = world
	for part in ("programs", "detached", "claude", "files", "record", "diverged"):
		assert seen["new"][part] == seen["old"][part], f"{name}: {part} differs"
	if scenario.get("scripted"):  # the scenario reaches the scripted flow's own continue prompt
		assert any("feature-flow/scripted.md" in line for line in seen["new"]["claude"])
	if name.startswith("budget-"):  # and the budget scenarios a call the budget no longer covers
		log = (seen["new-world"]["runs"] / "stderr-1.log").read_text()
		assert "budget used up before" in log
	# The intended difference 2: today's record keeps every wrap-up step in a final state.
	record, _ = runstate.readRecord(str(seen["new-world"]["wt"]), seen["new-world"]["run"])
	if scenario.get("auth"):
		assert "steps" not in record or not record["steps"].get("merge")
	else:
		states = {k: (record["steps"].get(k) or {}).get("state") for k in runstate.STEP_ORDER}
		assert set(states.values()) <= {"done", "skipped"}, states
		assert record["claude"]["exit_code"] == int(seen["new"]["files"][f"exit-{seen['new-world']['run']}"])


def test_the_matrix_covers_every_scenario_the_step_asks_for():
	"""The scenarios of migration step 6's proof (done->merge, refused, merge run, fix run, auto-resume,
	needs-decision, stops, claude's exit code, PHASE continuation, scripted, budget, both modes, spec and verifier)."""
	asked = {
		"done-merged",
		"done-refused",
		"refused-merge-run",
		"refused-fix-run",
		"blocked-auto-resume",
		"needs-decision",
		"stop-wt-stop",
		"stop-int-group",
		"stop-term-wrapper",
		"claude-nonzero",
		"phase-continuation",
		"scripted-continue",
		"budget-before-continue",
		"origin-done",
		"spec-verify-pass-no-verifier",
		"spec-verifier-pass",
	}
	assert asked <= set(SCENARIOS)
	assert len(INTENDED_DIFFERENCES) == 6


# --- the wrap-up alone: idempotence, crashes, the person's merge ------------------------------------------------


def endedClaude(world, result=DONE, code=0):
	"""run 1's claude part ended as the wrapper leaves it before the wrap-up (result file, record's claude end)."""
	(world["runs"] / "result-1.json").write_text(json.dumps({"result": result, "total_cost_usd": 0.5}))
	(world["runs"] / "start-1").write_text("2026-11-05T09:00:00+08:00\n")
	runstate.recordClaudeEnded(str(world["wt"]), 1, code, False)


def steps(world, *args, scenario=None, check=True):
	env = dict(world["env"])
	if scenario is not None:
		env["STUB_SCENARIO"] = json.dumps(scenario)
	result = subprocess.run(
		[sys.executable, str(world["tree"] / "hooks" / "worker" / "run-steps.py"), *args],
		capture_output=True,
		text=True,
		timeout=WAIT_SECONDS,
		env=env,
	)
	if check:
		assert result.returncode == 0, result.stderr
	return result


def wrapup(world, scenario=None):
	return steps(
		world,
		"wrapup",
		str(world["wt"]),
		"1",
		"--topic",
		TOPIC,
		"--code",
		"0",
		"--stopped",
		"0",
		scenario=scenario,
	)


def recordOf(world, run=1):
	record, problem = runstate.readRecord(str(world["wt"]), run)
	assert record is not None, problem
	return record


def counts(world):
	found = {}
	for line in programLog(world):
		found[line.split()[0] + (" " + line.split()[1] if line.startswith("worker-control") else "")] = (
			found.get(line.split()[0] + (" " + line.split()[1] if line.startswith("worker-control") else ""), 0) + 1
		)
	return found


@pytest.fixture
def stepsWorld(tmp_path):
	def make(**scenario):
		world = makeWorld(tmp_path / "w", scenario, old=False)
		endedClaude(world)
		return world, scenario

	return make


@pytest.mark.parametrize(
	"scenario",
	[
		{"spec": VERIFIER_SPEC, "verifier": "passed", "merge": "merged"},
		{"merge": "merge_run", "next": "merge"},
		{"spec": VERIFIER_SPEC, "verifier": "failed", "merge": "fix_run", "next": "fix"},
	],
	ids=["merged", "merge-run", "fix-run"],
)
def test_each_step_twice_is_idempotent(stepsWorld, scenario):
	world, scenario = stepsWorld(**scenario)
	wrapup(world)
	assert waitFor(lambda: scenario["merge"] != "merged" or "auto-finish.py" in counts(world))
	first = counts(world)
	exitText = (world["runs"] / "exit-1").read_text()
	wrapup(world)
	for name in runstate.STEP_ORDER:
		steps(world, "step", str(world["wt"]), "1", name, "--topic", TOPIC)
	time.sleep(0.3)  # a second auto-finish would be detached: give it time to show
	assert counts(world) == first
	assert first.get("auto-merge.py") == 1 and first.get("wt_queue.py") == 1
	assert first.get("auto-finish.py", 0) == (1 if scenario["merge"] == "merged" else 0)
	assert sum(v for k, v in first.items() if k.startswith("worker-control")) == (
		0 if scenario["merge"] == "merged" else 1
	)
	assert (world["runs"] / "exit-1").read_text() == exitText
	record = recordOf(world)
	done = [k for k in runstate.STEP_ORDER if record["steps"][k]["state"] == "done"]
	assert {k: record["steps"][k]["attempt"] for k in done} == dict.fromkeys(done, 1)  # never done twice
	assert {record["steps"][k]["state"] for k in runstate.STEP_ORDER} <= {"done", "skipped"}  # a skip re-read only
	assert record["exit"]["reason"] == "completed"


def test_next_step_skips_when_next_record_exists(stepsWorld):
	world, scenario = stepsWorld(merge="merge_run", next="merge")
	(world["runs"] / "merge-1.json").write_text(json.dumps({"run": 1, "decision": "refused", "merge_run": True}))
	runstate.recordStepFromFile(str(world["wt"]), 1, "merge")
	runstate.update(
		str(world["wt"]),
		1,
		lambda r: r["steps"].update(
			next={"state": "running", "attempt": 1, "runner_pid": GONE_PID, "started_at": "2026-11-05T09:05:00+08:00"}
		),
	)
	runstate.launchRecord(
		str(world["wt"]),
		2,
		{
			"topic": TOPIC,
			"session_id": SESSION,
			"session_mode": "resume",
			"started_by": "auto-merge",
			"purpose": "merge",
			"integration_mode": "local",
			"flow": "written",
			"prompt_kind": "merge",
			"launch": {"launched_at": "2026-11-05T09:06:00+08:00", "by_pid": os.getpid(), "by_run": 1},
		},
	)
	steps(world, "step", str(world["wt"]), "1", "next", "--topic", TOPIC)
	assert not any(line.startswith("worker-control.py") for line in programLog(world))
	nextStep = recordOf(world)["steps"]["next"]
	assert nextStep["state"] == "done" and nextStep["outcome"] == "merge" and "run 2" in nextStep["reason"]
	assert nextStep["attempt"] == 2


@pytest.mark.parametrize(
	"kind, reason", [("kill", "exited without an outcome (signal 9)"), ("raise", "exited without an outcome (exit 1)")]
)
def test_a_crashed_step_is_failed_and_the_wrapup_goes_on_then_continues(stepsWorld, kind, reason):
	world, scenario = stepsWorld(merge="merged")
	(world["stub"] / "merge-kind").write_text(kind)
	wrapup(world)
	record = recordOf(world)
	assert record["steps"]["merge"]["state"] == "failed" and record["steps"]["merge"]["reason"] == reason
	assert record["steps"]["finish"] == {**record["steps"]["finish"], "state": "skipped", "skip_reason": "not-merged"}
	assert record["exit"]["reason"] == "completed" and (world["runs"] / "exit-1").exists()
	assert record["steps"]["queue_tick"]["outcome"] == "ticked"
	# The wrap-up run again (by hand, later the reconciler): the failed step runs as attempt 2, the rest stays.
	(world["stub"] / "merge-kind").unlink()
	wrapup(world)
	assert waitFor(lambda: "auto-finish.py" in counts(world))
	record = recordOf(world)
	assert record["steps"]["merge"]["state"] == "done" and record["steps"]["merge"]["attempt"] == 2
	# The finish skipped as not merged follows the merge that now merged; what was done stays done.
	assert record["steps"]["finish"]["outcome"] == "launched" and record["steps"]["finish"]["attempt"] == 2
	assert counts(world)["auto-merge.py"] == 2 and counts(world)["wt_queue.py"] == 1
	assert record["steps"]["queue_tick"]["attempt"] == 1


def test_step_by_name_evaluates_a_skip_again_but_never_a_done_step(stepsWorld):
	"""`step finish` after the merge step was run again by hand: the finish skipped as not merged follows; a
	second `step finish` starts nothing (done)."""
	world, scenario = stepsWorld(merge="merged")
	(world["stub"] / "merge-kind").write_text("kill")
	wrapup(world)
	(world["stub"] / "merge-kind").unlink()
	steps(world, "step", str(world["wt"]), "1", "merge", "--topic", TOPIC)
	assert recordOf(world)["steps"]["merge"]["outcome"] == "merged"
	assert recordOf(world)["steps"]["finish"]["state"] == "skipped"
	steps(world, "step", str(world["wt"]), "1", "finish", "--topic", TOPIC)
	steps(world, "step", str(world["wt"]), "1", "finish", "--topic", TOPIC)
	assert waitFor(lambda: "auto-finish.py" in counts(world))
	time.sleep(0.3)
	assert counts(world)["auto-finish.py"] == 1 and recordOf(world)["steps"]["finish"]["outcome"] == "launched"


def test_a_step_whose_runner_died_runs_again(stepsWorld):
	world, scenario = stepsWorld(merge="refused")
	runstate.update(
		str(world["wt"]),
		1,
		lambda r: r.update(
			steps={
				"merge": {
					"state": "running",
					"attempt": 1,
					"runner_pid": GONE_PID,
					"started_at": "2026-11-05T09:05:00+08:00",
				}
			}
		),
	)
	wrapup(world)
	merge = recordOf(world)["steps"]["merge"]
	assert merge["state"] == "done" and merge["outcome"] == "refused" and merge["attempt"] == 2
	assert counts(world)["auto-merge.py"] == 1


def test_a_step_with_no_attempt_left_is_failed_and_not_run(stepsWorld):
	world, scenario = stepsWorld(merge="refused")
	limit = runstate.settings()["step_attempts_max"]
	runstate.update(
		str(world["wt"]),
		1,
		lambda r: r.update(
			steps={
				"merge": {
					"state": "running",
					"attempt": limit,
					"runner_pid": GONE_PID,
					"started_at": "2026-11-05T09:05:00+08:00",
				}
			}
		),
	)
	wrapup(world)
	merge = recordOf(world)["steps"]["merge"]
	assert merge["state"] == "failed" and "no attempt is left" in merge["reason"] and merge["attempt"] == limit
	assert "auto-merge.py" not in counts(world)


def test_a_step_under_a_live_runner_is_left_to_it(stepsWorld):
	world, scenario = stepsWorld(merge="refused")
	# Its command line names run-steps.py, this worktree and this run, as a real runner's does.
	runner = subprocess.Popen(["bash", "-c", "sleep 30; true", "run-steps.py", "wrapup", str(world["wt"]), "1"])
	try:
		runstate.update(
			str(world["wt"]),
			1,
			lambda r: r.update(
				steps={
					"merge": {
						"state": "running",
						"attempt": 1,
						"runner_pid": runner.pid,
						"started_at": "2026-11-05T09:05:00+08:00",
					}
				}
			),
		)
		wrapup(world)
		assert recordOf(world)["steps"]["merge"]["state"] == "running"
		assert "auto-merge.py" not in counts(world)
	finally:
		runner.kill()
		runner.wait()


# A verify step whose runner was killed outright while verify.py goes on in its own session:
# this process names run-steps.py, the worktree and the run as that runner did, sleeps HOLD seconds,
# then records verify PASS as verify.py does.
HELD_VERIFY = r"""import json, os, sys, time
sys.path.insert(0, os.environ["STUB_HOOKS"])
import runstate
time.sleep(float(os.environ["HOLD"]))
wt = os.environ["STUB_WT"]
with open(os.path.join(wt, ".hook-only", "runs", "verify-1.json"), "w") as f:
    json.dump({"run": 1, "passed": True, "checks": [{"name": "c1", "ok": True, "why": ""}]}, f)
runstate.recordStepFromFile(wt, 1, "verify")
"""
HOLD_SECONDS = 3


def holdVerify(world, startedAt, hold=HOLD_SECONDS, limit=600):
	"""Run 1's verify recorded running under a live runner that ends it PASS after hold seconds (HELD_VERIFY); the
	tree polls once a second. The runner process."""
	local = json.loads((world["tree"] / "config" / "local.json").read_text())
	local["budget"]["run_state"]["status_wait_poll_seconds"] = 1
	(world["tree"] / "config" / "local.json").write_text(json.dumps(local))
	runner = subprocess.Popen([sys.executable, "-c", HELD_VERIFY, "run-steps.py", "wrapup", str(world["wt"]), "1"],
		env={**world["env"], "HOLD": str(hold)})
	held = {"state": "running", "attempt": 1, "runner_pid": runner.pid, "started_at": startedAt, "limit_seconds": limit}
	runstate.update(str(world["wt"]), 1, lambda r: r.update(steps={"verify": held}))
	return runner


def test_a_continued_wrapup_waits_for_a_verify_still_running_elsewhere_then_decides_on_it(stepsWorld):
	"""d2's root cause: a wrap-up continued while verify.py of a killed runner still ran went on to the verifier and
	the merge at once, so both decided on a verify still running (the merge refused for it, nothing merged later).
	Now no step is claimed while a step before it runs elsewhere: the verifier and the merge wait for that verify,
	then decide on its PASS; the verify runs no second time."""
	world, scenario = stepsWorld(spec=VERIFIER_SPEC, verifier="passed", merge="merged")
	runner = holdVerify(world, runstate.homeClock.isoNow())
	try:
		wrapup(world)
	finally:
		runner.wait(timeout=WAIT_SECONDS)
	record = recordOf(world)
	assert record["steps"]["verify"]["outcome"] == "PASS" and record["steps"]["verify"]["attempt"] == 1
	assert record["steps"]["verifier"]["outcome"] == "PASS" and record["steps"]["merge"]["outcome"] == "merged"
	assert "verify.py" not in counts(world) and counts(world)["auto-merge.py"] == 1
	verifyEnd = runstate.parseStamp(record["steps"]["verify"]["ended_at"])
	assert runstate.parseStamp(record["steps"]["verifier"]["started_at"]) >= verifyEnd


def test_a_step_held_elsewhere_past_its_limit_is_waited_for_no_longer(stepsWorld):
	"""The wait is bounded: a verify held by a live runner that started more than its limit and the kill grace ago
	doesn't hold the wrap-up; the later steps go on at once (a stderr line says so) and the run ends."""
	world, scenario = stepsWorld(spec=SPEC, merge="refused")
	grace = runstate.settings()["step_kill_grace_seconds"]
	longAgo = (runstate.homeClock.now() - datetime.timedelta(seconds=60 + grace + 5)).isoformat(timespec="seconds")
	runner = holdVerify(world, longAgo, hold=WAIT_SECONDS, limit=60)
	try:
		started = time.time()
		result = wrapup(world)
		assert time.time() - started < HOLD_SECONDS + 10
	finally:
		runner.kill()
		runner.wait()
	assert "goes on while step verify still runs elsewhere" in result.stderr
	record = recordOf(world)
	assert record["steps"]["verify"]["state"] == "running" and record["exit"]["reason"] == "completed"


def test_step_left_running_by_dead_child_is_failed(stepsWorld):
	"""T-11: auto-merge.py exits 0 without a decision: the step can't stay running."""
	world, scenario = stepsWorld(merge="silent")
	wrapup(world)
	merge = recordOf(world)["steps"]["merge"]
	assert merge["state"] == "failed" and merge["reason"] == "exited without an outcome (exit 0)"


def test_a_verifier_with_nothing_to_judge_is_skipped_not_failed(stepsWorld):
	world, scenario = stepsWorld(spec=SPEC, merge="refused")
	wrapup(world)
	record = recordOf(world)
	assert record["steps"]["verify"]["outcome"] == "PASS"
	assert record["steps"]["verifier"] == {
		**record["steps"]["verifier"],
		"state": "skipped",
		"skip_reason": "no-verifier-section",
	}


def test_a_failure_inside_a_step_is_the_step_s_and_the_wrapup_goes_on(stepsWorld, monkeypatch):
	world, scenario = stepsWorld(merge="refused")
	for key in (
		"HOME",
		"WT_MAIN_DIR",
		"STUB_DIR",
		"STUB_LOG",
		"STUB_WT",
		"STUB_MAIN",
		"STUB_HOOKS",
		"STUB_SCENARIO",
	):
		monkeypatch.setenv(key, world["env"][key])
	config_fixtures.useConfig(monkeypatch, world["tree"] / "config")  # the settings the copy's programs read
	spec = importlib.util.spec_from_file_location("run_steps", world["tree"] / "hooks" / "worker" / "run-steps.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	def broken(_wrap):
		raise RuntimeError("decision broke")

	monkeypatch.setitem(module.ACTIONS, "merge", broken)
	assert (
		module.main(
			["wrapup", str(world["wt"]), "1", "--topic", TOPIC, "--code", "0", "--stopped", "0"]
		)
		== 0
	)
	record = recordOf(world)
	assert record["steps"]["merge"]["state"] == "failed"
	assert record["steps"]["merge"]["reason"] == "run-steps.py: RuntimeError: decision broke"
	assert record["exit"]["reason"] == "completed" and record["steps"]["queue_tick"]["outcome"] == "ticked"


def test_a_closed_orphan_runs_no_wrapup_step(stepsWorld):
	"""T-2's direction: a run closed as an orphan never gets a merge (or any other step) from a wrap-up run again.
	Only a run whose worker did not end done is closed (a done one's wrap-up is kept: the test after this)."""
	world, scenario = stepsWorld(merge="merged")
	runstate.update(str(world["wt"]), 1, lambda r: r["claude"].update(status="blocked"))
	runstate.closeVerifiedOrphan(str(world["wt"]), 1, "test", {"checked": "gone"})
	result = wrapup(world)
	assert "ended orphan-closed" in result.stderr
	assert programLog(world) == []
	assert "steps" not in recordOf(world) or not recordOf(world)["steps"]


def test_a_kept_done_orphan_is_wrapped_up_by_hand(stepsWorld):
	"""A done worker's orphan is not closed (runstate.closeOrphan refuses), so `run-steps.py wrapup` run by hand
	takes its steps up: the merge step calls auto-merge.py, whose own unchanged rules decide (here a stub)."""
	world, scenario = stepsWorld(merge="merged")
	runstate.confirmOrphan(str(world["wt"]), 1, "test", {"checked": "gone"})
	with pytest.raises(runstate.RecordError):
		runstate.closeVerifiedOrphan(str(world["wt"]), 1, "test", {"checked": "gone"})
	wrapup(world)
	assert counts(world).get("auto-merge.py") == 1
	record = recordOf(world)
	assert record["steps"]["merge"]["state"] == "done" and record["exit"]["reason"] == "completed"


def test_a_wrapup_without_the_exit_code_refuses(stepsWorld):
	world, scenario = stepsWorld()
	runstate.update(str(world["wt"]), 1, lambda r: r["claude"].pop("exit_code"))
	result = steps(world, "wrapup", str(world["wt"]), "1", check=False)
	assert result.returncode == 2 and "exit code is unknown" in result.stderr
	assert programLog(world) == []


def test_a_wrapup_run_again_by_hand_decides_from_the_record(stepsWorld):
	world, scenario = stepsWorld(merge="refused")
	steps(world, "wrapup", str(world["wt"]), "1")
	assert counts(world)["auto-merge.py"] == 1
	assert recordOf(world)["exit"] == {**recordOf(world)["exit"], "reason": "completed", "code": 0}


def test_the_persons_merge_is_a_new_attempt_then_next_and_finish(stepsWorld):
	world, scenario = stepsWorld(merge="refused", merge_person="merged")
	wrapup(world)
	assert recordOf(world)["steps"]["merge"]["outcome"] == "refused"
	(world["runs"] / "approve-1.json").write_text(json.dumps({"run": 1, "pid": GONE_PID}))
	steps(world, "step", str(world["wt"]), "1", "merge", "--topic", TOPIC, "--person")
	assert waitFor(lambda: "auto-finish.py" in counts(world))
	assert [line for line in programLog(world) if line.startswith("auto-merge.py")] == [
		f"auto-merge.py W {TOPIC} 1",
		f"auto-merge.py W {TOPIC} 1 --person",
	]
	record = recordOf(world)
	assert record["steps"]["merge"]["outcome"] == "merged" and record["steps"]["merge"]["attempt"] == 2
	assert record["steps"]["finish"]["outcome"] == "launched" and record["steps"]["finish"]["attempt"] == 2
	assert not (world["runs"] / "approve-1.json").exists()


def test_the_persons_merge_run_request_goes_to_the_next_step(stepsWorld):
	world, scenario = stepsWorld(merge="refused", merge_person="merge_run", next="merge")
	wrapup(world)
	steps(world, "step", str(world["wt"]), "1", "merge", "--topic", TOPIC, "--person")
	assert counts(world).get("worker-control.py auto-merge") == 1
	assert recordOf(world)["steps"]["next"]["outcome"] == "merge"


def test_a_running_merge_step_keeps_the_refusal_being_approved(stepsWorld):
	"""startStep keeps the earlier attempt's decision until the new one replaces it: the pane's "approving" and
	worker-control's second `wt merge` check read it while the person's approval runs."""
	world, scenario = stepsWorld(merge="refused")
	wrapup(world)
	claimed = runstate.startStep(str(world["wt"]), 1, "merge", os.getpid(), 3, lambda pid: False, force=True)
	assert claimed == {"attempt": 2}
	merge = recordOf(world)["steps"]["merge"]
	assert merge["state"] == "running" and merge["outcome"] == "refused"


def test_auto_merge_takes_only_a_done_merge_step_as_a_decision(monkeypatch):
	spec = importlib.util.spec_from_file_location("auto_merge", os.path.join(HOOKS, "worker", "auto-merge.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	for state, expected in (("running", None), ("failed", None), ("done", "refused")):
		step = {"state": state, "outcome": "refused"}
		monkeypatch.setattr(module.runstate, "status", lambda *a, **k: {"source": "record", "steps": {"merge": step}})
		assert (module.recordedMerge("w", TOPIC, 1, "m") or {}).get("outcome") == expected, state


def test_worker_control_approves_through_run_steps():
	spec = importlib.util.spec_from_file_location("worker_control", os.path.join(HOOKS, "worker", "worker-control.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	assert module.RUN_STEPS.name == "run-steps.py" and module.RUN_STEPS.parent == module.Path(HOOKS, "worker").resolve()
	with open(os.path.join(HOOKS, "worker", "worker-control.py"), encoding="utf-8") as f:
		text = f.read()
	assert '"step", str(worktree), str(run), "merge", "--topic", topic' in text


# --- T-12, T-13 against today's wrapper -------------------------------------------------------------------------


def liveWorld(tmp_path, scenario, interval=1):
	world = makeWorld(tmp_path / "live", scenario, old=False)
	(world["tree"] / "config" / "local.json").write_text(
		json.dumps({"budget": {"run_state": {"heartbeat_interval_seconds": interval}}})
	)
	return world


def startWrapper(world, **env):
	argv = [
		"bash",
		str(world["tree"] / "hooks" / "worker" / "run-worker.sh"),
		TOPIC,
		str(world["wt"]),
		"1",
		"new",
		SESSION,
		"13",
		"3",
		"",
		f"/feature-flow {TOPIC}",
	]
	return subprocess.Popen(
		argv,
		cwd=world["wt"],
		start_new_session=True,
		env={**world["env"], **env},
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)


def beats(world):
	found = statefile.read(str(world["wt"]), runstate.HEARTBEAT, 1)
	return found["count"] if found else 0


def test_heartbeat_loop_stops_when_wrapper_killed(tmp_path):
	world = liveWorld(tmp_path, {"turns": [{"result": DONE, "sleep": 20}]})
	process = startWrapper(world)
	try:
		assert waitFor(lambda: (world["stub"] / "started").exists() and beats(world) >= 1)
		process.kill()
		process.wait(timeout=WAIT_SECONDS)
		time.sleep(2.5)
		after = beats(world)
		time.sleep(2.5)
		assert beats(world) == after
	finally:
		try:
			os.killpg(process.pid, signal.SIGKILL)
		except ProcessLookupError:
			pass
	assert "exited_at" not in (recordOf(world).get("process") or {})  # SIGKILL: no trap; the heartbeat tells


@pytest.mark.parametrize("how", ["int", "term", "error"])
def test_trap_records_exit_on_int_term_and_error(tmp_path, how):
	world = liveWorld(tmp_path, {"turns": [{"result": DONE, "sleep": 20}]})
	if how == "error":  # a flow mode that can't be read: an invalid topic record (no variable is a source)
		(world["wt"] / ".hook-only" / "flow-mode").write_text("bogus\n")
		process = startWrapper(world)
	else:
		process = startWrapper(world)
		assert waitFor(lambda: (world["stub"] / "started").exists())
		if how == "int":
			os.killpg(process.pid, signal.SIGINT)
		else:
			os.kill(process.pid, signal.SIGTERM)
	assert process.wait(timeout=WAIT_SECONDS) == 0
	record = recordOf(world)
	assert record["process"]["exited_at"] and record["process"]["exit_code"] == 0
	if how == "error":
		assert record["exit"]["reason"] == "config-error" and record["process"]["exit_signal"] is None
	else:
		assert record["process"]["exit_signal"] == how.upper()
		assert record["exit"]["reason"] == "stopped" and record["exit"]["code"] == 7
		skipped = {k: record["steps"][k].get("skip_reason") for k in ("verify", "merge", "next")}
		assert skipped == {"verify": "no-spec", "merge": "stopped", "next": "stopped"}


BROKEN_NEXT = """import os, sys
if sys.argv[1] == "next-invocation":
    sys.exit("run-steps: a bug")
os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-steps-real.py")]
    + sys.argv[1:])
"""


def test_a_failing_next_invocation_ends_the_claude_part_as_an_error(tmp_path):
	world = liveWorld(tmp_path, {"turns": [DONE], "merge": "merged"}, interval=30)
	hooks = world["tree"] / "hooks" / "worker"
	shutil.copy2(hooks / "run-steps.py", hooks / "run-steps-real.py")
	(hooks / "run-steps.py").write_text(BROKEN_NEXT)
	process = startWrapper(world)
	assert process.wait(timeout=WAIT_SECONDS) == 0
	assert not (world["stub"] / "claude.log").exists()
	assert (world["runs"] / "exit-1").read_text().startswith("1 ")
	record = recordOf(world)
	assert record["exit"]["code"] == 1 and record["steps"]["merge"]["skip_reason"] == "not-done"
	assert "next-invocation failed" in (world["runs"] / "stderr-1.log").read_text()


# --- the wrapper's header, and the merge step's mode check --------------------------------------------------------


def claudeCalls(world):
	path = world["stub"] / "claude.log"
	return [line for line in path.read_text().splitlines() if line.startswith("call ")] if path.exists() else []


def killGroup(process):
	"""Leave nothing running when an assertion failed before the wrapper ended."""
	try:
		os.killpg(process.pid, signal.SIGKILL)
	except ProcessLookupError:
		pass


def test_every_claude_call_gets_the_run_s_minutes_as_the_background_wait_ceiling(tmp_path):
	"""Integration: run-worker.sh exports CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS = the run's minutes x 60000 for each
	claude call (main, compact, continue, final compact); without it `claude -p` stops background work after 10
	idle minutes and a background code-reviewer's report is lost."""
	scenario = SCENARIOS["phase-continuation"]
	world = makeWorld(tmp_path / "w", scenario, old=False)
	runScenario(world, scenario)  # the run's minutes: 13
	calls = claudeCalls(world)
	assert len(calls) == 4, calls
	assert all(" bg-wait=780000 " in line for line in calls), calls


# --- the bound on the wait after a call's ending turn (run-steps.py watch, run_ending.py) ---------------------


TURN_SECONDS = 1.1  # a turn's Stop counts only in a later second than its call's start (run_ending.finalTurn)
SUBAGENT = {"type": "agent", "agent_type": "code-reviewer", "description": "review"}


def endingWorld(tmp_path, scenario, idleMaxSeconds):
	"""Today's wrapper with the watcher's settings small enough for a test (poll 1 s, KILL 2 s after TERM)."""
	world = makeWorld(tmp_path / "w", scenario, old=False)
	local = json.loads((world["tree"] / "config" / "local.json").read_text())
	local["budget"]["run_state"].update(
		ending_idle_max_seconds=idleMaxSeconds, ending_poll_seconds=1, ending_kill_grace_seconds=2
	)
	(world["tree"] / "config" / "local.json").write_text(json.dumps(local))
	return world


def runTimed(world, scenario):
	started = time.monotonic()
	runScenario(world, scenario)
	return time.monotonic() - started


def resultOf(world, run=1):
	return json.loads((world["runs"] / f"result-{run}.json").read_text())


def cutRecord(world, run=1):
	return statefile.read(str(world["wt"]), "run-ending-cut", run)


def test_status_without_background_work_ends_at_once_and_is_never_cut(tmp_path):
	"""The final turn's Stop is recorded as ending and claude exits by itself: no cut, the final compact as
	before, the run ends long before the bound."""
	scenario = {"turns": [{"result": DONE, "sleep": TURN_SECONDS, "stops": [{"wait": 0}]}], "merge": "refused"}
	world = endingWorld(tmp_path, scenario, idleMaxSeconds=60)
	assert runTimed(world, scenario) < 20
	assert cutRecord(world) is None
	assert "worker_ending_cut" not in resultOf(world)
	assert len(claudeCalls(world)) == 2  # main and the final compact
	assert recordFacts(world)["claude"]["status"] == "done"


def test_a_call_still_waiting_after_its_status_is_ended_and_keeps_its_status(tmp_path):
	"""A background subagent still pending at STATUS: claude would wait up to the run's minutes. The watcher ends
	it ending_idle_max_seconds after the ending turn; the STATUS stays the run's result, the run code is 0 and
	the wrap-up runs as for any `STATUS: done` (the merge step), without a final compact."""
	scenario = {
		"turns": [{"result": DONE, "sleep": TURN_SECONDS, "stops": [{"tasks": [SUBAGENT], "wait": 120}]}],
		"merge": "refused",
	}
	world = endingWorld(tmp_path, scenario, idleMaxSeconds=1)
	assert runTimed(world, scenario) < 25
	result = resultOf(world)
	assert result["result"] == DONE and result["worker_ending_cut"]["idle_max_seconds"] == 1
	assert result["raw"]["claude_output"] == ""  # the stub printed nothing after its TERM
	assert cutRecord(world)["file"] == "result-1.json"
	assert len(claudeCalls(world)) == 1
	assert "exit 7 after SIGTERM" in (world["stub"] / "claude.log").read_text()
	assert (world["runs"] / "exit-1").read_text().split()[0] == "0"
	facts = recordFacts(world)
	assert facts["claude"]["status"] == "done" and facts["claude"]["outcome"] == "status"
	assert facts["invocations"] == [("main", "result-1.json", 7, None)]
	assert any(line.startswith("auto-merge.py") for line in programLog(world))


def test_a_phase_turn_with_pending_background_work_is_never_cut(tmp_path):
	"""A PHASE turn is a mid-run phase end: a background subagent still running then is unfinished work. The turn
	waits far longer than the bound and is never cut; claude's own wait (the run's minutes) applies and the run
	goes on with compact and continue as before."""
	scenario = {
		"turns": [
			{"result": "PHASE: tests-done\nnext dev", "sleep": TURN_SECONDS, "stops": [{"tasks": [SUBAGENT], "wait": 4}]},
			DONE,
		],
		"merge": "refused",
	}
	world = endingWorld(tmp_path, scenario, idleMaxSeconds=1)
	runTimed(world, scenario)
	assert cutRecord(world) is None
	assert "SIGTERM" not in (world["stub"] / "claude.log").read_text()
	calls = claudeCalls(world)
	assert len(calls) == 4 and " bg-wait=780000 " in calls[0], calls  # main, compact, continue, final compact
	assert (world["runs"] / "phase-1-1.json").exists()
	assert recordFacts(world)["claude"]["status"] == "done"


def test_a_pause_waiting_for_a_background_reviewer_is_never_cut(tmp_path):
	"""The mid-run wait: the turn pauses (no STATUS line) while a background reviewer runs, far longer than the
	bound; the hand-back's turn then ends with STATUS. Nothing is cut and the run goes on as before."""
	scenario = {
		"turns": [
			{
				"result": DONE,
				"sleep": TURN_SECONDS,
				"stops": [{"message": "waiting for the reviewer", "tasks": [SUBAGENT], "wait": 4}, {"wait": 0}],
			}
		],
		"merge": "refused",
	}
	world = endingWorld(tmp_path, scenario, idleMaxSeconds=1)
	runTimed(world, scenario)
	assert cutRecord(world) is None
	assert "SIGTERM" not in (world["stub"] / "claude.log").read_text()
	assert len(claudeCalls(world)) == 2
	assert recordFacts(world)["claude"]["status"] == "done"


def test_a_cut_call_whose_output_can_t_be_rewritten_says_what_was_lost_and_where_the_message_is(tmp_path):
	"""The rewrite fails (the output path is a directory): one stderr text naming the lost STATUS and the cut
	record that keeps the message, a hook-failed event, and the call recorded as claude left it."""
	world = makeWorld(tmp_path / "w", {"turns": [DONE]}, old=False)
	root, started = str(world["wt"]), "2026-11-08T10:00:00+08:00"
	(world["runs"] / "result-1.json").mkdir()
	runEnding = loadTreeModule(world, "run_ending")
	runEnding.recordCut(root, 1, file="result-1.json", startedAt=started, idleMaxSeconds=90,
		turn={"turn_ended_at": "2026-11-08T10:00:01+08:00", "raw": {"last_assistant_message": DONE}})
	err = steps(world, "invocation", root, "1", "main", "result-1.json", started, "143", "0").stderr
	assert "could not get that turn back as its result" in err and "without its STATUS line" in err
	assert str(world["runs"] / "1-ending-cut.json") in err and "today's files are unaffected" not in err
	assert [e["source"] for e in eventlog.read(root, event="hook-failed")] == ["run-steps"]
	assert recordFacts(world)["invocations"] == [("main", "result-1.json", 143, None)]


def nextAfterMain(world, last, stopped):
	"""run-steps.py next-invocation right after a main call that exited with last."""
	args = ["next-invocation", str(world["wt"]), "1", "--topic", TOPIC, "--mode", "new", "--session", SESSION, "--usd", "3",
		"--agent", "", "--prompt", "p", "--max-phases", "3", "--after", "main", "--code", "0", "--last", str(last),
		"--stopped", stopped]
	return steps(world, *args).stdout.split()


def cutCall(world, message, output):
	"""A main call the watcher cut: its cut record (keeping message), claude's output, then run-steps.py
	invocation as the wrapper calls it; its stderr."""
	root, started = str(world["wt"]), "2026-11-08T10:00:00+08:00"
	(world["runs"] / "result-1.json").write_text(output)
	loadTreeModule(world, "run_ending").recordCut(root, 1, file="result-1.json", startedAt=started, idleMaxSeconds=90,
		turn={"turn_ended_at": "2026-11-08T10:00:01+08:00", "raw": {"last_assistant_message": message}})
	return steps(world, "invocation", root, "1", "main", "result-1.json", started, "143", "0").stderr


def test_a_cut_record_without_a_status_line_never_reads_as_done(tmp_path):
	"""Neither claude's output nor the recorded message starts with a STATUS line: the output stays as claude left
	it, the person is told, and the call reads as an error (claude's exit code), never done with code 0."""
	world = makeWorld(tmp_path / "w", {"turns": [DONE]}, old=False)
	err = cutCall(world, "PHASE: tests-done\nstill working", '{"result": "Error: interrupted", "is_error": true}')
	assert "could not get that turn back as its result" in err and "STATUS line" in err
	assert resultOf(world) == {"result": "Error: interrupted", "is_error": True}
	assert nextAfterMain(world, 143, "0") == ["done", "143"]


def test_a_stop_during_the_cut_window_reads_stopped_not_done(tmp_path):
	"""A cut call whose STATUS was kept, but `wt stop` came: the stop wins, as before the cut existed."""
	world = makeWorld(tmp_path / "w", {"turns": [DONE]}, old=False)
	assert cutCall(world, DONE, "") == ""
	assert resultOf(world)["result"] == DONE
	assert nextAfterMain(world, 143, "1") == ["done", "143"]
	assert nextAfterMain(world, 143, "0") == ["done", "0"]


def test_the_watcher_never_signals_a_pid_that_is_not_the_claude_it_was_given(tmp_path):
	"""The watcher gets claude's start time from the wrapper: a pid whose start time differs (reused) or an empty
	one ends the watcher at once without a signal or a cut record, however old the final turn; with the right
	start time the same situation is cut."""
	world = endingWorld(tmp_path, {"turns": [DONE]}, idleMaxSeconds=1)
	root = str(world["wt"])
	loadTreeModule(world, "run_ending").recordTurn(root, 1, final=True, message=DONE, sessionId=None, background=[])
	sleeper = subprocess.Popen(["sleep", "60"])
	try:
		ticks = runstate.processFacts(sleeper.pid)["start_ticks"]
		for given in (str(ticks + 1), ""):
			result = steps(world, "watch", root, "1", str(sleeper.pid), given, "result-1.json", "2026-01-01T00:00:00+08:00")
			assert "is not the claude this call started" in result.stderr
			assert sleeper.poll() is None and cutRecord(world) is None
		steps(world, "watch", root, "1", str(sleeper.pid), str(ticks), "result-1.json", "2026-01-01T00:00:00+08:00")
		assert sleeper.wait(timeout=WAIT_SECONDS) == -signal.SIGTERM
		assert cutRecord(world)["file"] == "result-1.json"
	finally:
		sleeper.kill()
		sleeper.wait()


def loadTreeModule(world, name):
	spec = importlib.util.spec_from_file_location(name, world["tree"] / "hooks" / "worker" / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_an_unreadable_worker_phases_max_ends_the_run_as_a_config_error_before_claude(tmp_path):
	"""Integration: the home defaults without budget.worker_phases_max (homeconfig.py get exits 1): exit-<run> 1, the
	record's exit config-error, no claude call and no wrap-up program; the value never reaches next-invocation."""
	world = liveWorld(tmp_path, {"turns": [DONE], "merge": "merged"}, interval=30)
	budget = homeconfig.load("budget", defaultsOnly=True)  # the copied tree's defaults (config_fixtures.copyConfig)
	del budget["worker_phases_max"]
	config_fixtures.writeSection(world["tree"] / "config", "budget", budget)
	process = startWrapper(world)
	try:
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	assert (world["runs"] / "auth-1.log").exists()  # it got past the login check
	assert (world["runs"] / "exit-1").read_text().startswith("1 ")
	record = recordOf(world)
	assert record["exit"] == {**record["exit"], "reason": "config-error", "code": 1}
	assert claudeCalls(world) == [] and not (world["stub"] / "started").exists()
	assert programLog(world) == []
	assert not (record.get("claude") or {}).get("invocations")
	assert "worker_phases_max" in (world["runs"] / "stderr-1.log").read_text()


def test_a_stop_before_the_first_claude_call_starts_no_claude(tmp_path):
	"""Integration: TERM reaches the wrapper while it checks the login (before the first claude call): no claude
	call starts, the run ends stopped and its wrap-up skips the merge (the wrapper before migration step 6 started
	claude anyway)."""
	world = liveWorld(tmp_path, {"turns": [DONE], "merge": "merged", "auth_sleep": 1.5}, interval=30)
	process = startWrapper(world)
	try:
		assert waitFor(lambda: (world["stub"] / "auth-started").exists()), "the login check never started"
		os.kill(process.pid, signal.SIGTERM)
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	assert claudeCalls(world) == [] and not (world["stub"] / "started").exists()
	record = recordOf(world)
	assert not (record.get("claude") or {}).get("invocations")
	assert record["process"]["exit_signal"] == "TERM"
	assert record["exit"]["reason"] == "stopped" and (world["runs"] / "exit-1").exists()
	assert record["steps"]["merge"]["skip_reason"] == "stopped"
	assert not any(line.startswith("auto-merge.py") for line in programLog(world))


BROKEN_WRAPUP = """import importlib.util, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("run_steps_real", os.path.join(here, "run-steps-real.py"))
real = importlib.util.module_from_spec(spec)
spec.loader.exec_module(real)
if sys.argv[1] == "wrapup":
    def broken(wrap):
        raise RuntimeError("the wrap-up itself broke")
    real.endRun = broken  # after the steps, before exit-<run>: run-steps.py fails, not a step program
sys.exit(real.main(sys.argv[1:]))
"""


def mainEvents(world):
	path = world["main"] / ".hook-only" / "events.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_failed_wrapup_ends_the_run_wrapup_failed_and_runs_again_by_hand(tmp_path):
	"""Integration: run-steps.py wrapup itself fails (exit 1) after its steps: exit-<run> is not written, the record's
	run ends wrapup-failed with the error and claude's exit code, so the run reads as ended (never as an orphan),
	its steps left are kept for `wt wrapup` (resumable wrapup-left) and the person gets wrapup-failed;
	`run-steps.py wrapup <worktree> <run>` run again by hand continues it: the done steps stay done, the exit and
	the queue tick follow, and the real end replaces wrapup-failed."""
	world = liveWorld(tmp_path, {"turns": [DONE], "merge": "refused"}, interval=30)
	worker = world["tree"] / "hooks" / "worker"
	shutil.copy2(worker / "run-steps.py", worker / "run-steps-real.py")
	(worker / "run-steps.py").write_text(BROKEN_WRAPUP)
	process = startWrapper(world)
	try:
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	assert "the wrap-up itself broke" in (world["runs"] / "stderr-1.log").read_text()
	assert not (world["runs"] / "exit-1").exists()
	record = recordOf(world)
	assert record["exit"]["reason"] == "wrapup-failed" and record["exit"]["code"] == 0
	assert record["exit"]["error"] == "RuntimeError: the wrap-up itself broke"
	assert record["process"]["exited_at"] and record["steps"]["merge"]["outcome"] == "refused"
	(world["main"] / "plans" / "worker").mkdir(parents=True)  # the topic's estimate, as every launched topic has
	(world["main"] / "plans" / "worker" / f"{TOPIC}.json").write_text(json.dumps({"minutes": 10, "est_usd": 2}))
	status = runstate.status(str(world["main"]), TOPIC, root=str(world["wt"]))
	assert status["phase"] == "ended" and status["outcome"]["error"] == "RuntimeError: the wrap-up itself broke"
	assert status["resumable"]["code"] == "wrapup-left" and status["resumable"]["steps_left"] == ["queue_tick"]
	assert runstate.human(status, "en")["todo"]["command"] == f"wt wrapup {TOPIC}"
	failed = [e for e in mainEvents(world) if e["kind"] == "wrapup-failed"]
	assert len(failed) == 1 and "the wrap-up itself broke" in failed[0]["text"], mainEvents(world)
	assert "wt_queue.py" not in counts(world)

	shutil.copy2(worker / "run-steps-real.py", worker / "run-steps.py")
	steps(world, "wrapup", str(world["wt"]), "1")
	assert (world["runs"] / "exit-1").read_text().startswith("0 ")
	record = recordOf(world)
	assert record["exit"] == {**record["exit"], "reason": "completed", "code": 0} and "error" not in record["exit"]
	assert record["steps"]["merge"]["attempt"] == 1 and counts(world)["auto-merge.py"] == 1
	assert record["steps"]["queue_tick"]["outcome"] == "ticked" and counts(world)["wt_queue.py"] == 1
	status = runstate.status(str(world["main"]), TOPIC, root=str(world["wt"]))
	assert status["phase"] == "ended" and status["resumable"]["code"] != "wrapup-left"


def test_a_step_closed_failed_is_the_persons_step_failed_event(stepsWorld):
	world, scenario = stepsWorld(merge="merged")
	(world["stub"] / "merge-kind").write_text("raise")
	wrapup(world)
	failed = [e for e in mainEvents(world) if e["kind"] == "step-failed"]
	assert len(failed) == 1 and failed[0]["topic"] == TOPIC, mainEvents(world)
	assert failed[0]["text"].startswith("run 1: merge: exited without an outcome (exit 1)")
	line = runstate.human(runstate.status(str(world["main"]), TOPIC, root=str(world["wt"])), "en")["line"]
	assert "failed wrap-up step: merge: exited without an outcome (exit 1)" in line


def test_steps_that_end_well_send_no_failure_event(stepsWorld):
	world, scenario = stepsWorld(merge="merged")
	wrapup(world)
	assert not [e for e in mainEvents(world) if e["kind"] in ("step-failed", "queue-failed", "wrapup-failed")]


def test_a_failed_queue_tick_is_the_persons_queue_failed_event(stepsWorld):
	world, scenario = stepsWorld(merge="refused")
	tick = pathlib.Path(world["env"]["HOME"]) / ".claude" / "hooks" / "worker" / "wt_queue.py"
	tick.write_text("import sys\nsys.exit(3)\n")
	wrapup(world)
	assert recordOf(world)["steps"]["queue_tick"] == {**recordOf(world)["steps"]["queue_tick"], "outcome": "error",
		"reason": "exit 3"}
	failed = [e for e in mainEvents(world) if e["kind"] == "queue-failed"]
	assert len(failed) == 1 and "exit 3" in failed[0]["text"] and "stderr-1.log" in failed[0]["text"], mainEvents(world)


def test_the_finish_program_s_output_goes_to_the_run_s_stderr_log(stepsWorld):
	world, scenario = stepsWorld(merge="merged")
	finish = pathlib.Path(world["env"]["HOME"]) / ".claude" / "hooks" / "worker" / "auto-finish.py"
	finish.write_text("import sys\nprint('finish out')\nprint('finish err', file=sys.stderr)\n")
	wrapup(world)
	log = world["runs"] / "stderr-1.log"
	assert waitFor(lambda: log.exists() and "finish err" in log.read_text() and "finish out" in log.read_text())


@pytest.mark.parametrize(
	"schema, steps, launched",
	[(1, {"remove": "removed"}, False), (None, {"remove": "removed"}, True), (1, {"check": "x"}, True)],
	ids=["registered", "no-schema", "failed-finish"],
)
def test_the_finish_step_reads_finish_json_as_the_registered_kind(stepsWorld, schema, steps, launched):
	"""A finish.json of this run with schema 1 that removed the worktree means auto-finish ran already; one without
	a schema is another version (hooks/RULES.md S5), read as absent, and one that failed (A1) is retried: in both
	auto-finish starts."""
	world, scenario = stepsWorld(merge="merged")
	archive = world["main"] / ".hook-only" / "archive"
	archive.mkdir(parents=True)
	body = {"topic": TOPIC, "run": 1, "steps": steps, **({"schema": schema} if schema else {})}
	(archive / f"{TOPIC}.finish.json").write_text(json.dumps(body))
	wrapup(world)
	finish = recordOf(world)["steps"]["finish"]
	assert finish["outcome"] == "launched"
	if launched:
		assert waitFor(lambda: "auto-finish.py" in counts(world))
	else:
		assert "auto-finish already ran" in finish["reason"]
		time.sleep(0.3)
		assert "auto-finish.py" not in counts(world)


def loadSteps(world, monkeypatch):
	for key in ("HOME", "WT_MAIN_DIR", "STUB_DIR", "STUB_LOG", "STUB_WT", "STUB_MAIN", "STUB_HOOKS", "STUB_SCENARIO"):
		monkeypatch.setenv(key, world["env"][key])
	config_fixtures.useConfig(monkeypatch, world["tree"] / "config")  # the settings the copy's programs read
	spec = importlib.util.spec_from_file_location("run_steps_crash", world["tree"] / "hooks" / "worker" / "run-steps.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_a_crashed_command_exits_1_and_is_told(stepsWorld, monkeypatch, capsys):
	"""Any command but wrapup that crashes: a stderr line, a hook-failed event (once per distinct error) in the
	worktree's stream, exit 1; for `step` also the person's step-failed."""
	world, scenario = stepsWorld()
	module = loadSteps(world, monkeypatch)

	def broken(*args):
		raise RuntimeError("command broke")

	monkeypatch.setattr(module, "command", broken)
	assert module.main(["started", str(world["wt"]), "1", "123"]) == 1
	assert "started of run 1 failed: RuntimeError: command broke" in capsys.readouterr().err
	assert eventlog.read(str(world["wt"]), event="hook-failed")
	assert not [e for e in mainEvents(world) if e["kind"] == "step-failed"]
	assert module.main(["step", str(world["wt"]), "1", "merge", "--topic", TOPIC]) == 1
	failed = [e for e in mainEvents(world) if e["kind"] == "step-failed"]
	assert len(failed) == 1 and failed[0]["text"] == "run 1: merge: RuntimeError: command broke"


@pytest.mark.parametrize("checkout, recorded", [("local", "origin"), ("origin", "local")])
def test_the_merge_step_fails_when_the_run_record_s_mode_differs_from_the_topic_s(stepsWorld, checkout, recorded):
	"""Integration: the merge step asks homeconfig.flowMode with the record's integration_mode (recorded=): a run
	launched in one mode never merges, nor skips as origin mode, under another; the step fails with the reason."""
	world, scenario = stepsWorld(mode=checkout, merge="merged")
	runstate.update(str(world["wt"]), 1, lambda r: r.update(integration_mode=recorded))
	wrapup(world)
	merge = recordOf(world)["steps"]["merge"]
	assert merge["state"] == "failed", merge
	assert f"the run record says {recorded!r}" in merge["reason"] and f"in {checkout} mode" in merge["reason"]
	assert "auto-merge.py" not in counts(world) and "auto-finish.py" not in counts(world)
	assert recordOf(world)["steps"]["finish"]["skip_reason"] == "not-merged"


# --- self-repair stage 3a: the defects the reconciler depends on (A1, A2, A4, A5, A8) ---------------------------


def test_a_verifier_that_could_not_judge_is_retried_and_the_merge_waits_for_it(stepsWorld):
	"""A8: the verifier's ERROR is a failed step (outcome ERROR), the merge skips verifier-retry meanwhile; the wrap-up
	run again retries the verifier (attempt 2), and once it judged, the skipped merge and finish follow."""
	world, scenario = stepsWorld(spec=VERIFIER_SPEC, verifier="error", merge="merged")
	wrapup(world)
	record = recordOf(world)
	verifier = record["steps"]["verifier"]
	assert (verifier["state"], verifier["outcome"], verifier["attempt"]) == ("failed", "ERROR", 1), verifier
	assert record["steps"]["merge"] == {**record["steps"]["merge"], "state": "skipped", "skip_reason": "verifier-retry"}
	assert "auto-merge.py" not in counts(world)
	wrapup(world, scenario={**scenario, "verifier": "passed"})
	record = recordOf(world)
	assert (record["steps"]["verifier"]["state"], record["steps"]["verifier"]["outcome"]) == ("done", "PASS")
	assert record["steps"]["verifier"]["attempt"] == 2 and record["steps"]["merge"]["outcome"] == "merged"
	assert waitFor(lambda: "auto-finish.py" in counts(world))


def test_a_verifier_error_at_the_limit_lets_the_merge_decide_and_a_fail_is_never_retried(stepsWorld):
	"""A8: with step_attempts_max used the merge runs (and refuses, as before A8); a FAIL is final: a wrap-up run
	again never starts the verifier again."""
	world, scenario = stepsWorld(spec=VERIFIER_SPEC, verifier="error", merge="refused")
	limit = runstate.settings()["step_attempts_max"]
	for _ in range(limit):
		wrapup(world)
	record = recordOf(world)
	assert record["steps"]["verifier"]["attempt"] == limit and record["steps"]["verifier"]["state"] == "failed"
	assert counts(world)["auto-merge.py"] == 1 and record["steps"]["merge"]["outcome"] == "refused"
	wrapup(world)
	assert counts(world)["verifier.py"] == limit and counts(world)["auto-merge.py"] == 1

	failed = makeWorld(world["wt"].parent.parent.parent / "fail", {"spec": VERIFIER_SPEC, "verifier": "failed",
		"merge": "fix_run", "next": "fix"}, old=False)
	endedClaude(failed)
	wrapup(failed)
	wrapup(failed)
	assert counts(failed)["verifier.py"] == 1 and recordOf(failed)["steps"]["verifier"]["outcome"] == "FAIL"


def test_a_failed_finish_is_retried_but_not_while_its_auto_finish_still_runs(stepsWorld):
	"""A1: auto-finish wrote its failure back (finish failed, finish.json without the removal): the next wrap-up
	launches it again; while an auto-finish.py of this run still runs, the step records that instead of a second."""
	world, scenario = stepsWorld(merge="merged")
	wrapup(world)
	assert waitFor(lambda: counts(world).get("auto-finish.py") == 1)
	archive = world["main"] / ".hook-only" / "archive"
	archive.mkdir(parents=True)
	(archive / f"{TOPIC}.finish.json").write_text(json.dumps({"schema": 1, "topic": TOPIC, "run": 1,
		"steps": {"import": "x"}}))
	runstate.recordStep(str(world["wt"]), 1, "finish", {"state": "failed", "reason": "auto-finish.py: import: x"})
	running = subprocess.Popen(["bash", "-c", "sleep 30; true", "auto-finish.py", str(world["wt"]), TOPIC, "1"])
	try:
		steps(world, "step", str(world["wt"]), "1", "finish", "--topic", TOPIC)
		finish = recordOf(world)["steps"]["finish"]
		assert finish["state"] == "done" and "still runs" in finish["reason"] and str(running.pid) in finish["reason"]
	finally:
		running.kill()
		running.wait()
	runstate.recordStep(str(world["wt"]), 1, "finish", {"state": "failed", "reason": "auto-finish.py: import: x"})
	wrapup(world)
	assert waitFor(lambda: counts(world).get("auto-finish.py") == 2)
	assert recordOf(world)["steps"]["finish"]["outcome"] == "launched"


def test_a_record_that_exists_but_can_t_be_read_refuses_every_step(stepsWorld):
	"""A2: fail closed: no step program runs, exit 1, a stderr line and the person's wrapup-failed event."""
	world, scenario = stepsWorld(merge="merged")
	(world["runs"] / "1.json").write_text("{ not json")
	for args in (["wrapup", str(world["wt"]), "1", "--topic", TOPIC, "--code", "0", "--stopped", "0"],
			["step", str(world["wt"]), "1", "merge", "--topic", TOPIC]):
		result = steps(world, *args, check=False)
		assert result.returncode == 1 and "run record can't be read" in result.stderr, result.stderr
	assert programLog(world) == []
	failed = [e for e in mainEvents(world) if e["kind"] == "wrapup-failed"]
	assert len(failed) == 2 and "can't be read" in failed[0]["text"]


def test_a_run_without_a_record_file_still_runs_its_steps_once(stepsWorld):
	"""A2's other side: a missing record (a run before the record) is unchanged: the steps run without bookkeeping."""
	world, scenario = stepsWorld(merge="refused")
	os.rename(world["runs"] / "1.json", world["runs"] / "moved-1.json.bak")
	wrapup(world)
	assert counts(world)["auto-merge.py"] == 1 and counts(world)["wt_queue.py"] == 1
	assert not [e for e in mainEvents(world) if e["kind"] == "wrapup-failed"]


def test_a_wrapup_failure_whose_record_write_fails_says_so_in_the_event(stepsWorld, monkeypatch):
	"""A4: the wrap-up crashed and recordWrapupFailed failed too: the person's wrapup-failed event still comes and
	names the record write, so the orphan that follows has a clear reason."""
	world, scenario = stepsWorld()
	module = loadSteps(world, monkeypatch)

	def unwritable(*args):
		raise OSError("disk full")

	monkeypatch.setattr(module.runstate, "recordWrapupFailed", unwritable)
	args = module.parser().parse_args(["wrapup", str(world["wt"]), "1", "--topic", TOPIC, "--code", "0"])
	assert module.wrapupFailed(args, str(world["wt"]), RuntimeError("broke")) == 1
	failed = [e for e in mainEvents(world) if e["kind"] == "wrapup-failed"]
	assert len(failed) == 1 and "broke" in failed[0]["text"], failed
	assert "the run record could not be written either (OSError: disk full)" in failed[0]["text"]


def test_a_runner_pid_reused_by_another_process_is_not_a_live_runner(stepsWorld):
	"""A5: a running step whose runner_pid now belongs to another run-steps.py (another worktree or run), or whose
	start ticks differ, runs again; one naming this worktree and run with the recorded ticks is left to it."""
	world, scenario = stepsWorld(merge="refused")
	import reconciler

	other = subprocess.Popen(["bash", "-c", "sleep 30; true", "run-steps.py", "wrapup", "/elsewhere", "1"])
	same = subprocess.Popen(["bash", "-c", "sleep 30; true", "run-steps.py", "wrapup", str(world["wt"]), "1"])
	try:
		root = str(world["wt"])
		assert not reconciler.runnerAlive(root, 1, {"runner_pid": other.pid})
		assert not reconciler.runnerAlive(root, 2, {"runner_pid": same.pid})  # the same worktree, another run
		ticks = reconciler.startTicks(same.pid)
		assert reconciler.runnerAlive(root, 1, {"runner_pid": same.pid, "runner_start_ticks": ticks})
		assert not reconciler.runnerAlive(root, 1, {"runner_pid": same.pid, "runner_start_ticks": ticks + 1})
		assert not reconciler.runnerAlive(root, 1, {"runner_pid": GONE_PID})
		runstate.update(root, 1, lambda r: r.update(steps={"merge": {"state": "running", "attempt": 1,
			"runner_pid": other.pid, "started_at": "2026-11-05T09:05:00+08:00"}}))
		wrapup(world)
		merge = recordOf(world)["steps"]["merge"]
		assert merge["state"] == "done" and merge["attempt"] == 2 and isinstance(merge["runner_start_ticks"], int)
	finally:
		for process in (other, same):
			process.kill()
			process.wait()


def test_a_finish_whose_processes_can_t_be_listed_fails_and_starts_no_second_auto_finish(stepsWorld, monkeypatch):
	"""A1 with fail closed: a retried finish starts auto-finish.py only when the process list rules out a running
	one; a list that can't be read fails the step (an attempt used), never a second auto-finish.py."""
	world, scenario = stepsWorld(merge="merged")
	wrapup(world)
	assert waitFor(lambda: counts(world).get("auto-finish.py") == 1)
	runstate.recordStep(str(world["wt"]), 1, "finish", {"state": "failed", "reason": "auto-finish.py: import: x"})
	module = loadSteps(world, monkeypatch)
	monkeypatch.setattr(module.reconciler, "liveHelpers", lambda *args, **kwargs: None)
	module.main(["step", str(world["wt"]), "1", "finish", "--topic", TOPIC])
	finish = recordOf(world)["steps"]["finish"]
	assert finish["state"] == "failed" and "can't be listed" in finish["reason"], finish
	assert finish["attempt"] == 2
	assert counts(world).get("auto-finish.py") == 1


# -- 5. time limits from history (worker self-repair 3c) and the reconcile after a wrap-up -----------------------

HISTORY_SECONDS = 2


def fastLimits(world, *names):
	"""Short step limits from history, as a repository with earlier runs has them: an archived topic whose run
	records hold step_limit_min_samples_count done attempts of each named step, HISTORY_SECONDS each; the tree's
	step_limit_factor 1 (local.json), grace one second. The limit is then HISTORY_SECONDS."""
	local = json.loads((world["tree"] / "config" / "local.json").read_text())
	local["budget"]["run_state"].update(step_limit_factor=1, step_kill_grace_seconds=1)
	(world["tree"] / "config" / "local.json").write_text(json.dumps(local))
	archived = runstate.archiveOf(str(world["main"]), "earlier")
	for _ in range(runstate.settings()["step_limit_min_samples_count"]):
		run = runstate.create(archived, {"topic": "earlier", "started_by": "person", "purpose": "build",
			"flow": "written", "launch": {"launched_at": "2026-11-05T08:00:00+08:00", "by_pid": 1}})
		done = {"state": "done", "attempt": 1, "started_at": "2026-11-05T08:00:00+08:00",
			"ended_at": f"2026-11-05T08:00:0{HISTORY_SECONDS}+08:00"}
		runstate.update(archived, run, lambda r: r.update(steps={name: dict(done) for name in names}))


HANGING = r"""import os, subprocess, sys, time
open(os.path.join(os.environ["STUB_DIR"], "hang-" + os.path.basename(sys.argv[0])), "w").write(
    str(subprocess.Popen(["sleep", "120"]).pid))
time.sleep(120)
"""


def gone(pid):
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return True
	return open(f"/proc/{pid}/stat").read().split(") ")[1].startswith("Z")


def test_a_step_past_its_limit_is_ended_with_its_group_failed_and_never_done(stepsWorld):
	"""3c's dangerous direction: verify.py hangs (and a child of it); at the limit both are ended, the step is
	failed with the limit's reason and the person's step-failed event, the wrap-up goes on; the run's next wrap-up
	retries it (attempt 2), never reading it as passed."""
	world, scenario = stepsWorld(spec=SPEC, merge="merged")
	fastLimits(world, "verify")
	program = world["home"] / ".claude" / "hooks" / "gates" / "verify.py"
	program.write_text(HANGING)
	started = time.time()
	wrapup(world)
	assert time.time() - started < 20
	child = int((world["stub"] / "hang-verify.py").read_text())
	assert waitFor(lambda: gone(child), 5), "the step program's own child still runs"
	step = recordOf(world)["steps"]["verify"]
	assert step["state"] == "failed" and step["reason"].startswith("exceeded 0 min, from 1 x the median"), step
	assert isinstance(step["program_pid"], int) and step["program_start_ticks"]  # the program, for the reconciler
	assert step["limit_seconds"] == HISTORY_SECONDS
	assert [e for e in mainEvents(world) if e["kind"] == "step-failed" and "verify: exceeded" in e["text"]]
	assert recordOf(world)["steps"]["merge"]["state"] == "skipped"  # not-done gate: verify never passed
	program.write_text(STUB_PROGRAM)
	steps(world, "step", str(world["wt"]), "1", "verify", "--topic", TOPIC)
	assert recordOf(world)["steps"]["verify"]["attempt"] == 2 and recordOf(world)["steps"]["verify"]["outcome"] == "PASS"


def test_a_merge_past_its_limit_is_told_once_and_never_ended(stepsWorld):
	world, scenario = stepsWorld(merge="merged")
	fastLimits(world, "merge")
	program = world["home"] / ".claude" / "hooks" / "worker" / "auto-merge.py"
	program.write_text("import time\ntime.sleep(5)\n" + STUB_PROGRAM)
	wrapup(world)
	slow = [e for e in mainEvents(world) if e["kind"] == "merge-slow"]
	assert len(slow) == 1 and "it is not stopped" in slow[0]["text"], mainEvents(world)
	assert recordOf(world)["steps"]["merge"]["outcome"] == "merged"


def test_the_verifier_keeps_its_own_timeout_and_gets_no_outer_limit(stepsWorld):
	world, scenario = stepsWorld(spec=VERIFIER_SPEC, verifier="passed", merge="merged")
	fastLimits(world, "verifier")
	program = world["home"] / ".claude" / "hooks" / "gates" / "verifier.py"
	program.write_text("import time\ntime.sleep(5)\n" + STUB_PROGRAM)
	wrapup(world)
	assert recordOf(world)["steps"]["verifier"]["outcome"] == "PASS"
	assert not [e for e in mainEvents(world) if e["kind"] in ("step-failed", "merge-slow")]


def test_a_stop_reaches_the_step_program_in_its_own_session(stepsWorld):
	"""callProgram starts each step in its own session; a stop of the run (INT to run-steps.py, as the wrapper
	forwards it) is forwarded to the step's group: verify.py ends, the run reads stopped."""
	world, scenario = stepsWorld(spec=SPEC)
	program = world["home"] / ".claude" / "hooks" / "gates" / "verify.py"
	program.write_text("import os, signal, sys, time\n"
		"signal.signal(signal.SIGINT, lambda n, f: (open(os.path.join(os.environ['STUB_DIR'], 'verify-int'), 'w').close(),"
		" sys.exit(5)))\nopen(os.path.join(os.environ['STUB_DIR'], 'verify-started'), 'w').close()\ntime.sleep(60)\n")
	process = subprocess.Popen([sys.executable, str(world["tree"] / "hooks" / "worker" / "run-steps.py"), "wrapup",
		str(world["wt"]), "1", "--topic", TOPIC, "--code", "0", "--stopped", "0"], env=world["env"],
		stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	assert waitFor(lambda: (world["stub"] / "verify-started").exists())
	process.send_signal(signal.SIGINT)
	assert process.wait(timeout=WAIT_SECONDS) == 0
	assert (world["stub"] / "verify-int").exists()
	assert recordOf(world)["exit"]["reason"] == "stopped"


def test_the_wrapup_ends_by_reconciling_the_other_worktrees(stepsWorld):
	world, scenario = stepsWorld(merge="refused")
	wrapup(world)
	log = world["runs"] / "stderr-1.log"
	assert waitFor(lambda: log.exists() and "reconcile (" in log.read_text()), log.read_text() if log.exists() else ""
	assert f"{TOPIC} run" not in log.read_text().split("reconcile (", 1)[1]  # its own worktree is excluded


def test_the_heartbeat_starts_the_reconciler_for_the_other_worktrees(tmp_path):
	"""run-worker.sh's heartbeat every reconcile_heartbeat_beats_count beats: run-steps.py reconcile --all --exclude
	<this worktree> (setsid: its own session), its output in the run's log; the
	beats go on. Here every beat (count 1); the repository's only worktree is this run's, so it reconciles none."""
	world = liveWorld(tmp_path, {"turns": [{"result": DONE, "sleep": 6}]})
	local = json.loads((world["tree"] / "config" / "local.json").read_text())
	local["budget"]["run_state"]["reconcile_heartbeat_beats_count"] = 1
	(world["tree"] / "config" / "local.json").write_text(json.dumps(local))
	log = world["runs"] / "stderr-1.log"
	process = startWrapper(world)
	try:
		assert waitFor(lambda: log.exists() and log.read_text().count("reconcile (") >= 2), log.read_text()
		assert "no worktree" in log.read_text() and beats(world) >= 2
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		with contextlib.suppress(ProcessLookupError):
			os.killpg(process.pid, signal.SIGKILL)


def test_a_beat_count_of_zero_turns_only_the_heartbeat_s_reconcile_off(tmp_path):
	world = liveWorld(tmp_path, {"turns": [{"result": DONE, "sleep": 3}]})
	local = json.loads((world["tree"] / "config" / "local.json").read_text())
	local["budget"]["run_state"]["reconcile_heartbeat_beats_count"] = 0
	(world["tree"] / "config" / "local.json").write_text(json.dumps(local))
	process = startWrapper(world)
	assert process.wait(timeout=WAIT_SECONDS) == 0
	log = (world["runs"] / "stderr-1.log").read_text()
	assert beats(world) >= 2 and log.count("reconcile (") == 1, log  # only the wrap-up's own, at its end


def test_settings_that_can_t_give_a_limit_run_the_step_without_one(stepsWorld):
	"""A home.json without the run_state limits (or one that can't be read): the steps still run, unbounded and
	without a limit_seconds, each with a stderr line; never a crashed wrap-up."""
	world, scenario = stepsWorld(spec=SPEC, merge="refused")
	home = json.loads((world["tree"] / "config" / "home.json").read_text())
	home["budget"]["run_state"].pop("step_limit_history_count")
	(world["tree"] / "config" / "home.json").write_text(json.dumps(home))
	result = wrapup(world)
	recorded = recordOf(world)["steps"]
	assert recorded["verify"]["outcome"] == "PASS" and recorded["verify"].get("limit_seconds") is None
	assert recordOf(world)["exit"]["reason"] == "completed"
	assert "runs without a time limit" in result.stderr
	steps(world, "step", str(world["wt"]), "1", "verify", "--topic", TOPIC)  # a later run of a step: told once
	told = [e for e in mainEvents(world) if e["kind"] == "limit-unreadable"]
	assert len(told) == 1 and "without a time limit" in told[0]["text"], mainEvents(world)
	import wt_events

	assert wt_events.needsAttention(told[0])


# --- a stop during the wrap-up, then the person's `wt wrapup` (--continue-stopped) ------------------------------

STOPPABLE_VERIFY = (
	"import os, signal, sys, time\n"
	"signal.signal(signal.SIGINT, lambda n, f: sys.exit(5))\n"
	"open(os.path.join(os.environ['STUB_DIR'], 'verify-started'), 'w').close()\n"
	"time.sleep(60)\n"
)


def stoppedDuringVerify(stepsWorld, **scenario):
	"""Run 1's worker ended STATUS done, then `wt stop` came while its verify step ran: the stop recorded, then INT
	to the wrap-up as the wrapper forwards it. verify.py is the plain stub again afterwards."""
	world, scenario = stepsWorld(spec=SPEC, merge="refused", **scenario)
	program = world["home"] / ".claude" / "hooks" / "gates" / "verify.py"
	program.write_text(STOPPABLE_VERIFY)
	process = subprocess.Popen([sys.executable, str(world["tree"] / "hooks" / "worker" / "run-steps.py"), "wrapup",
		str(world["wt"]), "1", "--topic", TOPIC, "--code", "0", "--stopped", "0"], env=world["env"],
		stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	assert waitFor(lambda: (world["stub"] / "verify-started").exists())
	runstate.recordStop(str(world["wt"]), 1, "person")
	process.send_signal(signal.SIGINT)
	assert process.wait(timeout=WAIT_SECONDS) == 0
	program.write_text(STUB_PROGRAM)
	return world


def continueStopped(world):
	return steps(world, "wrapup", str(world["wt"]), "1", "--topic", TOPIC, "--continue-stopped")


def test_wt_wrapup_continues_a_wrapup_stopped_during_verify_each_step_once(stepsWorld):
	"""The person's continuation: verify (broken by the stop) is retried as its next attempt, the steps skipped as
	stopped are evaluated again and run once, the queue tick done before is not repeated, the exit stays stopped; a
	second continuation starts nothing."""
	world = stoppedDuringVerify(stepsWorld)
	record = recordOf(world)
	assert record["exit"]["reason"] == "stopped" and record["claude"]["stopped"] is False
	assert record["steps"]["verify"]["state"] == "failed" and record["steps"]["verify"]["attempt"] == 1
	assert {n: record["steps"][n]["skip_reason"] for n in ("verifier", "merge", "next")} == dict.fromkeys(
		("verifier", "merge", "next"), "stopped")
	assert runstate.wrapupLeft(record) == ["verify", "verifier", "merge", "next"]
	assert counts(world) == {"wt_queue.py": 1}
	continueStopped(world)
	after = counts(world)
	assert after == {"wt_queue.py": 1, "verify.py": 1, "verifier.py": 1, "auto-merge.py": 1}, after
	record = recordOf(world)
	found = {n: (record["steps"][n]["state"], record["steps"][n].get("outcome") or record["steps"][n].get(
		"skip_reason"), record["steps"][n]["attempt"]) for n in runstate.STEP_ORDER}
	assert found == {
		"verify": ("done", "PASS", 2),
		"verifier": ("skipped", "no-verifier-section", 2),
		"merge": ("done", "refused", 2),
		"next": ("skipped", "no-follow-up", 2),
		"finish": ("skipped", "not-merged", 2),
		"queue_tick": ("done", "ticked", 1),
	}, found
	assert record["exit"]["reason"] == "stopped" and runstate.wrapupLeft(record) == []
	continueStopped(world)
	time.sleep(0.3)
	assert counts(world) == after and recordOf(world)["steps"] == record["steps"]


def test_a_stop_between_steps_is_continued_from_the_first_step_it_skipped(stepsWorld):
	"""The stop came after verify was done: nothing before the skipped steps runs again, so each skip as stopped is
	evaluated again on its own (not only after an earlier step ran); verify stays done at its first attempt."""
	world, scenario = stepsWorld(spec=SPEC, merge="refused")
	(world["runs"] / "verify-1.json").write_text(json.dumps({"run": 1, "passed": True, "checks": []}))
	skipped = {"state": "skipped", "attempt": 1, "skip_reason": "stopped"}
	runstate.update(str(world["wt"]), 1, lambda r: r.update(steps={
		"verify": {"state": "done", "attempt": 1, "outcome": "PASS", "detail": "verify-1.json"},
		"verifier": dict(skipped), "merge": dict(skipped), "next": dict(skipped),
		"finish": {"state": "skipped", "attempt": 1, "skip_reason": "not-merged"},
		"queue_tick": {"state": "done", "attempt": 1, "outcome": "ticked"}}))
	runstate.recordStop(str(world["wt"]), 1, "person")
	runstate.recordRunEnded(str(world["wt"]), 1, "stopped", 0)
	continueStopped(world)
	assert counts(world) == {"verifier.py": 1, "auto-merge.py": 1}, counts(world)
	record = recordOf(world)
	assert record["steps"]["verify"]["attempt"] == 1 and record["steps"]["verifier"]["attempt"] == 2
	assert record["steps"]["merge"]["outcome"] == "refused" and runstate.wrapupLeft(record) == []


def test_a_continued_wrapup_whose_wrapper_died_after_the_stop_ends_stopped(stepsWorld):
	"""The wrapper died after the stop, before the exit (an orphan): the person's continuation runs the steps and
	writes the run's exit as stopped, the fact the stop left, never completed."""
	world, scenario = stepsWorld(spec=SPEC, merge="refused")
	skipped = {"state": "skipped", "attempt": 1, "skip_reason": "stopped"}
	runstate.update(str(world["wt"]), 1, lambda r: r.update(steps={"verifier": dict(skipped), "merge": dict(skipped)}))
	runstate.recordStop(str(world["wt"]), 1, "person")
	continueStopped(world)
	record = recordOf(world)
	assert counts(world)["auto-merge.py"] == 1 and counts(world)["verify.py"] == 1
	assert record["exit"]["reason"] == "stopped" and (world["runs"] / "exit-1").exists()


def test_a_wrapup_run_again_without_continue_stopped_never_continues_a_stop(stepsWorld):
	"""What automation would start (the reconciler's wrap-up, a hand run): a stopped run skips as stopped, no step
	program runs; the steps stay owed for the person."""
	world = stoppedDuringVerify(stepsWorld)
	steps(world, "wrapup", str(world["wt"]), "1", "--topic", TOPIC)
	assert counts(world) == {"wt_queue.py": 1}
	record = recordOf(world)
	assert record["steps"]["verify"]["skip_reason"] == "stopped" and record["exit"]["reason"] == "stopped"
	assert runstate.wrapupLeft(record) == ["verify", "verifier", "merge", "next"]


def test_a_continued_step_whose_attempts_are_used_up_is_not_run_again(stepsWorld):
	"""A step broken by the stop at its last attempt: the continuation leaves it (step_attempts_max) and tells the
	person once per run (step-at-limit); the steps skipped as stopped after it are evaluated again."""
	world = stoppedDuringVerify(stepsWorld)
	attempts = runstate.settings()["step_attempts_max"]
	runstate.update(str(world["wt"]), 1, lambda r: r["steps"]["verify"].update(attempt=attempts))
	continueStopped(world)
	record = recordOf(world)
	assert "verify.py" not in counts(world) and record["steps"]["verify"]["attempt"] == attempts
	assert record["steps"]["verify"]["state"] == "failed"
	assert record["steps"]["verifier"]["attempt"] == 2 and record["steps"]["verifier"].get("skip_reason") != "stopped"
	continueStopped(world)  # the person's step-at-limit, once per run
	told = [e for e in mainEvents(world) if e["kind"] == "step-at-limit"]
	assert len(told) == 1 and told[0]["text"].startswith("run 1: verify") and "stay owed" in told[0]["text"], told


def test_continue_stopped_still_skips_a_run_whose_claude_part_was_stopped(stepsWorld):
	"""Unchanged: a stop of claude's part (claude.stopped) skips every step that needs a run not stopped, also for
	`wt wrapup`; the exit reads stopped."""
	world, scenario = stepsWorld(spec=SPEC, merge="merged")
	runstate.update(str(world["wt"]), 1, lambda r: r["claude"].update(stopped=True))
	runstate.recordStop(str(world["wt"]), 1, "person")
	continueStopped(world)
	assert counts(world) == {"wt_queue.py": 1}
	record = recordOf(world)
	assert {n: record["steps"][n].get("skip_reason") for n in ("verify", "verifier", "merge", "next")} == dict.fromkeys(
		("verify", "verifier", "merge", "next"), "stopped")
	assert record["exit"]["reason"] == "stopped"


def wrapCells():
	"""Every record fact Wrap reads about a stop: claude.stopped (missing, False, True), the person's stop, the exit
	(none, stopped, completed, wrapup-failed) and the merge step (missing, skipped as stopped, skipped otherwise)."""
	for claudeStopped in ("missing", False, True):
		for stop in (False, True):
			for end in (None, "stopped", "completed", runstate.WRAPUP_FAILED):
				for merge in (None, "stopped", "not-done"):
					claude = {"status": "done", "exit_code": 0}
					if claudeStopped != "missing":
						claude["stopped"] = claudeStopped
					record = {"topic": TOPIC, "claude": claude, "steps": {}}
					if stop:
						record["stop"] = {"requested_at": None, "by": "person"}
					if end:
						record["exit"] = {"reason": end, "code": 0}
					if merge:
						record["steps"]["merge"] = {"state": "skipped", "skip_reason": merge}
					yield record


def test_old_vs_new_a_wrapup_run_again_is_stopped_only_as_named(stepsWorld, monkeypatch):
	"""Skill refactor, section 2: Wrap.stopped and the exit reason of a wrap-up run again (no --stopped) against the
	old rule (wrapup_stop_baseline.oldWrapStopped) over every cell, with and without --continue-stopped. The only
	changes: without the flag a stop seen only in the exit or a skip also reads stopped (safe direction); with it a
	stop during the wrap-up (runstate.stopDuringWrapup) no longer does, while its exit still reads stopped. The
	wrapper's own wrap-up (--stopped given) is unchanged in every cell."""
	world, scenario = stepsWorld()
	module = loadSteps(world, monkeypatch)
	counts = {}
	for record in wrapCells():
		monkeypatch.setattr(module, "readRecord", lambda root, run, record=record: (record, None))
		old = wrapup_stop_baseline.oldWrapStopped(record)
		for flag in (False, True):
			wrap = module.Wrap(str(world["wt"]), 1, module.parser().parse_args(["wrapup", str(world["wt"]), "1"]
				+ (["--continue-stopped"] if flag else [])))
			ended = wrap.stopped or wrap.stopSeen
			if (wrap.stopped, ended) == (old, old):
				reason = "same"
			elif wrap.stopped and ended and not old:
				assert runstate.stoppedRun(record), record
				reason = "stop-seen-in-exit-or-skip"
			else:
				assert flag and not wrap.stopped and ended and runstate.stopDuringWrapup(record), (record, flag)
				reason = "continued-stop"
			counts[reason] = counts.get(reason, 0) + 1
			for given in ("0", "1"):
				own = module.Wrap(str(world["wt"]), 1, module.parser().parse_args(["wrapup", str(world["wt"]), "1",
					"--code", "0", "--stopped", given] + (["--continue-stopped"] if flag else [])))
				assert own.stopped == own.stopSeen == (given == "1")
	print(f"Wrap.stopped old vs new: {counts}")
	assert set(counts) == {"same", "stop-seen-in-exit-or-skip", "continued-stop"}


# --- a stop at any moment of the start-up -------------------------------------------------

# Sourced by the wrapper's bash (BASH_ENV) before the script: once the wrapper has set its EXIT trap (the last of its
# traps), every command of the wrapper's own shell is counted (functrace: inside its functions too; never in a
# subshell or command substitution, whose BASHPID differs), and the STOP_AT_COMMAND-th sends SIGINT to the whole
# process group, as `wt stop` does, just before that command runs. STOP_AT_COMMAND 0 never stops: each counted
# command is appended to STOP_TRACE instead.
STOP_AT_COMMAND = r"""set -o functrace
stop_armed=0 stop_count=0
trap 'if [ "$BASHPID" = "$$" ]; then
  if [ "$stop_armed" = 1 ]; then
    stop_count=$((stop_count + 1))
    if [ "$STOP_AT_COMMAND" = 0 ]; then printf "%s\n" "$BASH_COMMAND" >> "$STOP_TRACE"
    elif [ "$stop_count" = "$STOP_AT_COMMAND" ]; then printf "%s\n" "$BASH_COMMAND" > "$STOP_TRACE"; kill -INT 0; fi
  elif [ "$BASH_COMMAND" = "trap on_exit EXIT" ]; then stop_armed=1; fi
fi' DEBUG
"""
# Commands of the wrapper's shell from its EXIT trap up to its first `next-invocation` (included), on the path of a
# run that goes on to claude: test_the_start_up_stops_cover_every_command_up_to_the_first_claude_decision keeps it
# exact, so a change of the start-up shows here.
START_UP_COMMANDS = 34


def startUpWorld(tmp_path, stopAt):
	world = liveWorld(tmp_path, {"turns": [DONE], "merge": "merged"}, interval=30)
	(world["stub"] / "stop-at.sh").write_text(STOP_AT_COMMAND)
	env = {"BASH_ENV": str(world["stub"] / "stop-at.sh"), "STOP_AT_COMMAND": str(stopAt),
		"STOP_TRACE": str(world["stub"] / "stop-trace")}
	return world, env


def test_the_start_up_stops_cover_every_command_up_to_the_first_claude_decision(tmp_path):
	world, env = startUpWorld(tmp_path, 0)
	process = startWrapper(world, **env)
	try:
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	trace = (world["stub"] / "stop-trace").read_text().splitlines()
	first = next(i for i, command in enumerate(trace) if "next-invocation" in command)
	assert first + 1 == START_UP_COMMANDS, trace[: first + 1]
	assert recordOf(world)["exit"]["reason"] == "completed"


@pytest.mark.parametrize("stopAt", range(1, START_UP_COMMANDS + 1))
def test_a_stop_at_any_command_of_the_start_up_ends_the_run_stopped_with_its_reason(tmp_path, stopAt):
	"""Integration (`wt stop` in the same second as `wt resume` left run 2 with
	process.exited_at, exit code 0, no exit signal and no exit: an orphan without a reason). INT to the group before
	each command of the start-up, from the wrapper's EXIT trap to its first next-invocation: the run ends stopped, its
	exit written in the record and exit-<run>, the INT recorded, no claude call, never not-logged-in or a
	config error from a start-up step the INT ended."""
	world, env = startUpWorld(tmp_path, stopAt)
	process = startWrapper(world, **env)
	try:
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	where = (world["stub"] / "stop-trace").read_text().strip()
	record = recordOf(world)
	assert record.get("exit") and record["exit"]["reason"] == "stopped", (where, record.get("exit"), record["process"])
	assert record["process"]["exit_signal"] == "INT", where
	assert (world["runs"] / "exit-1").exists(), where
	assert claudeCalls(world) == [], where
	assert not any(line.startswith("auto-merge.py") for line in programLog(world)), where


HOLD_BEFORE_TRAPS = r"""until [ -e "$RELEASE_WRAPPER" ]; do sleep 0.05; done
"""


def test_wt_stop_before_the_wrapper_recorded_its_start_signals_nothing_and_the_run_ends_stopped(tmp_path):
	"""Integration: the wrapper held before its first command (BASH_ENV: no trap set yet, no start recorded), as
	right after worker-control.py started it. `wt stop` then (as stopRun: marker, the record's stop under its lock)
	sends no signal, since a signal there would kill bash without an exit; the wrapper, released, reads the stop when
	it records its start (run-steps.py started) and ends the run stopped without starting claude."""
	world = liveWorld(tmp_path, {"turns": [DONE], "merge": "merged"}, interval=30)
	(world["stub"] / "hold.sh").write_text(HOLD_BEFORE_TRAPS)
	release = world["stub"] / "release"
	process = startWrapper(world, BASH_ENV=str(world["stub"] / "hold.sh"), RELEASE_WRAPPER=str(release))
	try:
		runstate.recordSpawned(str(world["wt"]), 1, process.pid)
		control = loadTreeModule(world, "worker-control")
		status = {"run": 1, "phase": "launching", "process": recordOf(world)["process"]}
		with pytest.raises(SystemExit):
			control.stopRun(world["wt"], TOPIC, status)
		time.sleep(0.3)
		assert process.poll() is None, "the wrapper got a signal before its traps"
		release.write_text("go")
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	record = recordOf(world)
	assert record["exit"]["reason"] == "stopped" and record["stop"]["by"] == "person"
	assert record["process"]["started_at"] and record["process"]["exit_signal"] is None
	assert claudeCalls(world) == [] and (world["runs"] / "exit-1").exists()


def test_wt_stop_after_the_wrapper_recorded_its_start_signals_it(tmp_path):
	"""The other side of the lock: a record that holds started_at when the stop is written gets the signal."""
	world = liveWorld(tmp_path, {"turns": [{"result": DONE, "sleep": 20}], "merge": "merged"}, interval=30)
	process = startWrapper(world)
	try:
		assert waitFor(lambda: (world["stub"] / "started").exists()), "claude never started"
		control = loadTreeModule(world, "worker-control")
		status = {"run": 1, "phase": "running", "process": recordOf(world)["process"]}
		with pytest.raises(SystemExit):
			control.stopRun(world["wt"], TOPIC, status)
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	record = recordOf(world)
	assert record["exit"]["reason"] == "stopped" and record["process"]["exit_signal"] == "INT"


# A start-up step the stop's INT ends itself: python3, git and claude (whose stub runs through `env python3`) are
# found first in $HOME/.local/bin (the wrapper puts it first on PATH); the stand-in sends INT to the whole group, as
# `wt stop` does, the first time a command line holds STOP_DURING (it dies of it with the rest of the group's
# foreground), and otherwise runs the real program.
STOP_DURING = r"""#!/bin/sh
case "$*" in
*"$STOP_DURING"*) if [ ! -e "$STOP_FIRED" ]; then : > "$STOP_FIRED"; kill -INT 0; sleep 5; exit 130; fi ;;
esac
exec "$REAL_PROGRAM_DIR/$(basename "$0")" "$@"
"""
START_UP_STEPS = (
	"run-steps.py started",
	"reconcile_heartbeat_beats_count",
	"heartbeat_interval_seconds",
	"flow-mode",
	"rev-parse HEAD",
	"main-head",
	"auth status",
	"worker_phases_max",
)


@pytest.mark.parametrize("step", START_UP_STEPS)
def test_a_start_up_step_the_stop_ends_is_never_read_as_its_own_failure(tmp_path, step):
	"""Integration: INT to the group while a start-up step runs ends that step too (exit 130). The run ends stopped
	with the INT recorded, never not-logged-in (the login check killed) or config-error (a setting's read killed),
	and no claude call starts."""
	world = liveWorld(tmp_path, {"turns": [DONE], "merge": "merged"}, interval=30)
	real = tmp_path / "real-bin"
	real.mkdir()
	local = world["home"] / ".local" / "bin"
	for name in ("python3", "git"):
		os.symlink(shutil.which(name), real / name)
		(local / name).write_text(STOP_DURING)
		(local / name).chmod(0o755)
	fired = world["stub"] / "stop-fired"
	process = startWrapper(world, STOP_DURING=step, STOP_FIRED=str(fired), REAL_PROGRAM_DIR=str(real))
	try:
		assert process.wait(timeout=WAIT_SECONDS) == 0
	finally:
		killGroup(process)
	assert fired.exists(), f"no start-up command held {step!r}"
	record = recordOf(world)
	assert record.get("exit") and record["exit"]["reason"] == "stopped", (record.get("exit"), record["process"])
	assert record["process"]["exit_signal"] == "INT"
	assert (world["runs"] / "exit-1").read_text().split()[0] == "0"
	assert claudeCalls(world) == []


@pytest.mark.parametrize("minimum, left, starts", [(None, "0.05", True), (None, "0.04", False), (0.5, "0.30", False),
	(0.5, "0.50", True), (0.0, "0.00", True)])
def test_a_call_starts_only_with_at_least_the_configured_minimum_left(tmp_path, monkeypatch, minimum, left, starts):
	"""The minimum budget for one more claude call is home.json budget worker_call_budget_min_usd (0.05 by
	default), not a number in run-steps.py: below it the call counts as made with exit code 99."""
	config = config_fixtures.copyConfig(os.path.join(os.path.dirname(HOOKS), "config"), tmp_path / "config")
	if minimum is not None:
		config_fixtures.writeSection(config, "budget", lambda budget: {**budget, "worker_call_budget_min_usd": minimum})
	config_fixtures.useConfig(monkeypatch, config)
	assert homeconfig.load("budget")["worker_call_budget_min_usd"] == (0.05 if minimum is None else minimum)
	spec = importlib.util.spec_from_file_location("run_steps_minimum", os.path.join(HOOKS, "worker", "run-steps.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setattr(module, "sessionBase", lambda root, run, mode: 0.0)
	monkeypatch.setattr(module, "remainingUsd", lambda root, run, usd, base: left)
	monkeypatch.setattr(module, "decide", lambda args, root, kind, last, code: ["decided", str(last)])
	args = argparse.Namespace(run="1", mode="new", usd=1.0, session="s", agent="")
	answer = module.invocation(args, str(tmp_path), "main", str(tmp_path / "result.json"), "prompt", 0)
	assert (answer[0] == "invoke") is starts, answer
	assert starts or answer == ["decided", str(module.BUDGET_EXIT_CODE)]

#!/usr/bin/env python3
"""Real-data verification, second layer: a verifier agent judges a finished branch against its approved plan.

Usage: verifier.py <worktree> <topic> <run>   (run-worker.sh, after verify.py passed a run that ended `STATUS: done`)
Runs only when <main>/plans/verify/<topic>.json has a "verifier" section ({"usd", "minutes", "judge": [...]},
written by the planning session at hand-off; templates/verify.json) and verify-<run>.json passed.

verify.py judges what a hook can see (exit codes, output text, identical files); the verifier judges what needs
a reader of the plan: each rule of the plan and the spec's "judge" items, on a copy of the real data
(agents/verifier.md). It is blind to the orchestrator (a forked skill in a new headless session that this hook
starts, so the worker can neither skip it nor write its prompt) but not to the plan: it reads the plan as the
person approved it (<main>/.hook-only/approved/<topic>.md, copied by worker-control.py at every run the person
starts), never the worktree's copy, which the worker can edit.

Steps, outside Claude Code's sandbox:
  1. inputs in <worktree>/.hook-only/verifier-<run>/ (agents can read .hook-only/, not write it): plan.md,
     spec.json, layer1.json and data/ (the spec's data_paths of <main>/<data>, copied as verify.py does)
  2. the budget: the spec's raw estimate through the history model (cost-history.py, agent "verifier", the
     worktree's history copy), written to .hook-only/budget/pending-verifier.json; subagent-budget.py claims it
     when the fork starts, as for a dispatch with a `Budget:` line, subagent-cost.py records the round, and
     cost-history.py import brings it into the history with the other agents
  3. `claude -p "/verify-topic <topic> <run>"` in the worktree (skill verify-topic forks agent verifier), with
     --max-budget-usd = budget x budget_headroom + verifier_session_usd (the outer session's own turn); stopped
     after the budget's minutes x budget_headroom + verifier_grace_minutes (a command still running when the
     budget hook stops the agent, then its report)
  4. the worktree must be as before: HEAD, the branch and `git status --porcelain`
  5. the report's `VERDICT: pass|fail` line (agents/verifier.md) decides
Result: <worktree>/.hook-only/runs/verifier-<run>.json {"run", "verdict": "passed" | "failed" | "error", "report",
"why" (error only), "session_id", "usd", "budget"}; the data copy is removed afterwards (auto-finish archives
.hook-only/). auto-merge.py condition 9 merges only on "passed"; on "failed" it asks worker-control.py for a fix
run, which hands the report to the worker; "error" (no verdict, a changed worktree, a timeout, no approved plan)
waits for the person: the verifier failed, not the branch. Fails closed: never "passed" without a verdict line.
Each outcome is an event (wt_events.py, kind "verified").
"""
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hookonly  # noqa: E402
import qg_common  # noqa: E402
import verify  # noqa: E402
import wt_events  # noqa: E402

DEFAULTS_FILE = os.path.join(HERE, "budget-defaults.json")
COST_HISTORY = os.path.expanduser("~/.claude/skills/repo-plan/cost-history.py")
AGENT = "verifier"
VERDICT = re.compile(r"^\s*VERDICT:\s*(pass|fail)\s*$", re.MULTILINE | re.IGNORECASE)
RELAY = ("This session only runs the skill named in the prompt. When the forked skill returns, reply with its "
	"report exactly as it came back: nothing added, nothing left out.")


class Failed(Exception):
	"""The verifier couldn't judge (an "error" result)."""


def approvedPlan(mainDir, topic):
	return os.path.join(mainDir, ".hook-only", "approved", f"{topic}.md")


def loadCostHistory():
	spec = importlib.util.spec_from_file_location("cost_history", COST_HISTORY)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def budget(worktree, raw):
	"""The raw estimate through the verifier's history model; no history: the raw estimate as it is."""
	ch = loadCostHistory()
	rows = [r for r in ch.newestFirst(ch.load(os.path.join(worktree, ch.HISTORY))) if ch.isValid(r) and r["agent"] == AGENT]
	usd, minutes = ch.applyModel(float(raw["usd"]), float(raw["minutes"]), ch.model(rows))
	return {"minutes": minutes, "usd": usd, "raw_minutes": float(raw["minutes"]), "raw_usd": float(raw["usd"])}


def state(worktree):
	"""What the verifier must leave as it found it."""
	def git(*args):
		return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True).stdout
	return {"head": git("rev-parse", "HEAD").strip(), "branch": git("symbolic-ref", "-q", "HEAD").strip(),
		"status": git("status", "--porcelain")}


def prepare(worktree, topic, run, mainDir, spec, layer1):
	approved = approvedPlan(mainDir, topic)
	if not os.path.isfile(approved):
		raise Failed(f"no approved plan ({approved}): worker-control.py copies it when the person starts a run")
	inputs = os.path.join(worktree, ".hook-only", f"verifier-{run}")
	if os.path.exists(inputs):
		shutil.rmtree(inputs)
	os.makedirs(inputs)
	shutil.copy2(approved, os.path.join(inputs, "plan.md"))
	for name, content in (("spec.json", spec), ("layer1.json", layer1)):
		with open(os.path.join(inputs, name), "w", encoding="utf-8") as f:
			json.dump(content, f, ensure_ascii=False, indent=1)
	verify.copyData(os.path.join(mainDir, spec.get("data", "data")), os.path.join(inputs, "data"),
		spec.get("data_paths") or ["."])
	return inputs


def launch(worktree, topic, run, given, defaults, sessionId):
	"""The headless session; returns its parsed JSON output."""
	headroom = float(defaults["budget_headroom"])
	cap = round(given["usd"] * headroom + float(defaults["verifier_session_usd"]), 2)
	seconds = math.ceil(given["minutes"] * headroom + float(defaults["verifier_grace_minutes"])) * 60
	argv = ["claude", "-p", f"/verify-topic {topic} {run}", "--permission-mode", "auto", "--output-format", "json",
		"--max-budget-usd", f"{cap:.2f}", "--session-id", sessionId, "--append-system-prompt", RELAY]
	# Not a worker run: the worker's own limits (worker-budget.py, worker-stop.py) don't apply to it.
	env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_WORKER_")}
	process = subprocess.Popen(argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
		stderr=subprocess.PIPE, text=True, start_new_session=True)
	try:
		out, err = process.communicate(timeout=seconds)
	except subprocess.TimeoutExpired:
		os.killpg(process.pid, signal.SIGTERM)
		process.communicate()
		raise Failed(f"no report within {seconds // 60} min (budget x budget_headroom + verifier_grace_minutes)")
	try:
		data = json.loads(out or "")
	except ValueError:
		raise Failed(f"claude exited {process.returncode} without JSON output: {(err or out).strip()[-300:]}")
	if data.get("is_error"):
		raise Failed(f"the session ended in an error: {str(data.get('result'))[:300]}")
	return data


def judge(text):
	"""passed | failed from the report; Failed when it carries no single verdict after a DONE status."""
	lines = text.strip().splitlines()
	status = lines[0].strip() if lines else ""
	verdicts = {v.lower() for v in VERDICT.findall(text)}
	if not status.startswith("DONE") or len(verdicts) != 1:
		raise Failed("the report has no `DONE` + single `VERDICT:` line: " + (text.strip()[:300] or "empty"))
	return "passed" if verdicts == {"pass"} else "failed"


def check(worktree, topic, run, mainDir):
	"""The result dict, or None when this run has nothing to verify."""
	path = verify.specPath(mainDir, topic)
	if not os.path.isfile(path):
		return None
	spec = qg_common.loadJson(path)
	section = spec.get("verifier")
	if not section:
		return None
	try:
		layer1 = qg_common.loadJson(os.path.join(worktree, ".hook-only", "runs", f"verify-{run}.json"))
	except (OSError, ValueError):
		layer1 = {}
	if not layer1.get("passed"):
		return None  # the first layer failed: auto-merge.py handles that, nothing for a verifier to add
	defaults = qg_common.loadJson(DEFAULTS_FILE)
	sessionId = str(uuid.uuid4())
	result = {"session_id": sessionId}
	inputs = None
	pending = os.path.join(hookonly.stateDir(worktree, "budget"), f"pending-{AGENT}.json")
	try:
		try:
			raw = {"usd": float(section["usd"]), "minutes": float(section["minutes"])}
		except (KeyError, TypeError, ValueError):
			raise Failed("the spec's verifier section needs numbers usd and minutes (the raw estimate)")
		inputs = prepare(worktree, topic, run, mainDir, spec, layer1)
		given = budget(worktree, raw)
		result["budget"] = given
		os.makedirs(os.path.dirname(pending), exist_ok=True)
		with open(pending, "w", encoding="utf-8") as f:
			json.dump({**given, "agent_type": AGENT, "written": time.time()}, f)
		before = state(worktree)
		data = launch(worktree, topic, run, given, defaults, sessionId)
		result["usd"] = data.get("total_cost_usd")
		report = str(data.get("result") or "")
		result["report"] = report
		after = state(worktree)
		if after != before:
			changed = [k for k in before if before[k] != after[k]]
			raise Failed("the verifier changed the worktree (" + ", ".join(changed) + "): its verdict doesn't count")
		result["verdict"] = judge(report)
	except Failed as error:
		result.update(verdict="error", why=str(error))
	except (OSError, ValueError, KeyError, TypeError) as error:
		result.update(verdict="error", why=f"{type(error).__name__}: {error}")
	finally:
		if inputs:
			shutil.rmtree(os.path.join(inputs, "data"), ignore_errors=True)
		if os.path.exists(pending):
			os.remove(pending)
	return result


def main():
	worktree, topic, run = os.path.abspath(sys.argv[1]), sys.argv[2], int(sys.argv[3])
	mainDir = os.environ.get("WT_MAIN_DIR") or ""
	if not os.path.isdir(mainDir):
		print("verifier.py: no main checkout (WT_MAIN_DIR)", file=sys.stderr)
		return
	result = check(worktree, topic, run, mainDir)
	if result is None:
		return
	with open(os.path.join(worktree, ".hook-only", "runs", f"verifier-{run}.json"), "w", encoding="utf-8") as f:
		json.dump({"run": run, **result}, f, ensure_ascii=False)
	if result["verdict"] == "error":
		text = "could not judge: " + result["why"]
	else:
		text = result["verdict"] + (": " + " | ".join(l.strip() for l in result["report"].splitlines()
			if re.match(r"^\s*V\d+\s+fail", l))[:250] if result["verdict"] == "failed" else "")
	wt_events.emit(mainDir, "verified", topic, f"run {run}: {text}")


if __name__ == "__main__":
	if len(sys.argv) != 4:
		sys.exit("usage: verifier.py <worktree> <topic> <run>")
	main()

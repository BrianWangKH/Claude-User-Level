"""Summarise a worker's progress, for the planning session in the main checkout.

Usage: python3 worker-status.py <topic>   (run from the main checkout)

Output (migration step 3 of the run record: a display reader; nothing decides on it): first runstate.agent with
runstate.detail (hooks/lib/runstate.py): the run's phase (none, launching, running, finishing, ended, orphan with
suspected or confirmed, unknown), BUSY, RESUMABLE, TODO, the line for people, the evidence, and every run's
result line; then "progress:" with the subagents now, the finished rounds and git. A topic without a worktree
prints the status of its archive, or fails when there is none.

Reads only what already exists; the worker writes nothing extra:
  - the run state through hooks/lib/runstate.py (status, detail)
  - <worktree>/.hook-only/budget/*.json            a file exists while that subagent runs
  - <worktree>/.hook-only/agent-guard/guard.log    one line per finished guarded round
  - <worktree>/.hook-only/cost/*.jsonl             tokens per finished round
  - git in the worktree                            commits since the base, uncommitted files
A finished run's outcome: run-result.py.
"""
import glob
import json
import os
import subprocess
import sys
import time

HOME_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.join(HOME_ROOT, "hooks", "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import runstate  # noqa: E402
import wt_topics  # noqa: E402


def git(cwd, *args):
	result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
	return result.stdout.strip() if result.returncode == 0 else ""


def minutes(seconds):
	return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


def loadJson(path):
	try:
		with open(path, encoding="utf-8") as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


def current(hookOnly):
	lines = []
	now = time.time()
	for path in glob.glob(os.path.join(hookOnly, "budget", "*.json")):
		state = loadJson(path) or {}
		if "error" in state:
			lines.append(f"now: {os.path.basename(path)[:-5]} · budget state error: {state['error']}")
			continue
		# A gate holding the agent (budget_state.markWait) is hook time: the agent's own clock stops at
		# the wait's start, as the budget hook counts it once the wait ends.
		wait = state.get("hook_wait") or {}
		since = float(wait.get("since") or now)
		elapsed = since - float(state.get("started") or since)
		line = f"now: {state.get('agent_type', '?')} · {minutes(elapsed)} of {state.get('minutes', '?'):g} min"
		if wait:
			line += f" · waiting for {wait.get('what', 'a hook')} {minutes(now - since)}"
			if wait.get("max_seconds"):
				line += f" (limit {minutes(float(wait['max_seconds']))})"
		lines.append(line)
	return lines


def finished(hookOnly):
	counts, alerts = {}, 0
	logPath = os.path.join(hookOnly, "agent-guard", "guard.log")
	if os.path.isfile(logPath):
		with open(logPath, encoding="utf-8") as f:
			for line in f:
				parts = line.split()
				if len(parts) >= 4:
					counts[parts[1]] = counts.get(parts[1], 0) + 1
					alerts += parts[3] == "ALERT"
	tokens = 0
	for path in glob.glob(os.path.join(hookOnly, "cost", "*.jsonl")):
		with open(path, encoding="utf-8") as f:
			for line in f:
				try:
					tokens += int(json.loads(line)["this_round"]["all_tokens"])
				except (ValueError, KeyError, TypeError):
					pass
	if not counts and not tokens:
		return []
	rounds = ", ".join(f"{name} ×{n}" for name, n in sorted(counts.items())) or "no guarded rounds"
	return [f"done: {rounds}" + (f" · {alerts} ALERT" if alerts else " · no ALERT")
		+ (f" · {tokens // 1000}k tokens" if tokens else "")]


def gitSummary(worktree):
	base, why = homeconfig.integrationBase(worktree)
	commits = git(worktree, "log", "--format=%h %s", f"{base}..HEAD").splitlines() if base else []
	status = git(worktree, "status", "--porcelain").splitlines()
	modified = [l for l in status if not l.startswith("??")]
	untracked = [l for l in status if l.startswith("??")]
	text = f"git: {len(commits)} commit(s)" if base else f"git: commits unknown ({why})"
	if commits:
		text += " (" + "; ".join(commits[:5]) + (" …" if len(commits) > 5 else "") + ")"
	return [text + f" · {len(modified)} modified, {len(untracked)} untracked"]


def main():
	if len(sys.argv) != 2:
		print("usage: python3 worker-status.py <topic>", file=sys.stderr)
		sys.exit(2)
	topic = sys.argv[1]
	commonDir = git(os.getcwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
	if not commonDir:
		print("FAIL: not inside a git repo")
		sys.exit(1)
	mainRoot = os.path.dirname(commonDir)
	worktree = str(wt_topics.worktreeOf(mainRoot, topic))
	status = runstate.status(mainRoot, topic)
	if not os.path.isdir(worktree) and status["phase"] == "none":
		print(f"FAIL: no worktree at {worktree} and no archived runs")
		sys.exit(1)
	print(runstate.agent(status, runstate.detail(status, text=False)))
	print()
	print("progress:")
	if not os.path.isdir(worktree):
		print(f"no worktree at {worktree}: archived or removed; the status above is the archive's")
		return
	hookOnly = os.path.join(worktree, ".hook-only")
	for line in current(hookOnly) + finished(hookOnly) + gitSummary(worktree):
		print(line)


if __name__ == "__main__":
	main()

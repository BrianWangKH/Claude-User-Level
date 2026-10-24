"""Summarise a worker's progress in a few lines, for the planning session in the main checkout.

Usage: python3 worker-status.py <topic>   (run from the main checkout)

Reads only what already exists; the worker writes nothing extra:
  - <worktree>/.hook-only/session/*.json           start / end of each session (session-mark hook)
  - <worktree>/.hook-only/runs/result-*.json       one per finished `claude -p` run (--output-format json)
  - <worktree>/.hook-only/budget/*.json            a file exists while that subagent runs
  - <worktree>/.hook-only/agent-guard/guard.log    one line per finished guarded round
  - <worktree>/.hook-only/cost/*.jsonl             tokens per finished round
  - git in the worktree                            commits since the base, uncommitted files
The run's outcome is the `STATUS:` first line of the result file's `result` (skill repo-plan, "When it ends").
"""
import glob
import json
import os
import subprocess
import sys
import time

STALE_SECONDS = 2 * 3600


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


def sessionState(hookOnly):
	"""From the session-mark hook's records: the latest session's start and end."""
	records = [loadJson(p) or {} for p in glob.glob(os.path.join(hookOnly, "session", "*.json"))]
	records = [r for r in records if r.get("started")]
	if not records:
		return "not started"
	latest = max(records, key=lambda r: r["started"])
	now = time.time()
	if latest.get("ended"):
		return (f"ended {minutes(now - latest['ended'])} ago ({latest.get('end_reason') or 'unknown'}):"
			" read the STATUS line of the latest result file")
	running = now - latest["started"]
	if running > STALE_SECONDS:
		return f"started {minutes(running)} ago, no end recorded: probably killed (terminal closed?)"
	return f"running for {minutes(running)} ({latest.get('permission_mode') or '?'} mode)"


def current(hookOnly):
	lines = []
	now = time.time()
	for path in glob.glob(os.path.join(hookOnly, "budget", "*.json")):
		state = loadJson(path) or {}
		if "error" in state:
			lines.append(f"now: {os.path.basename(path)[:-5]} · budget state error: {state['error']}")
			continue
		elapsed = now - float(state.get("started") or now)
		lines.append(f"now: {state.get('agent_type', '?')} · {minutes(elapsed)} of {state.get('minutes', '?'):g} min"
			+ (f" · phase {state['phase']}" if state.get("phase") else ""))
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
	base = git(worktree, "merge-base", "HEAD", "origin/HEAD") or git(worktree, "merge-base", "HEAD", "origin/master")
	commits = git(worktree, "log", "--format=%h %s", f"{base}..HEAD").splitlines() if base else []
	status = git(worktree, "status", "--porcelain").splitlines()
	modified = [l for l in status if not l.startswith("??")]
	untracked = [l for l in status if l.startswith("??")]
	text = f"git: {len(commits)} commit(s)"
	if commits:
		text += " (" + "; ".join(commits[:5]) + (" …" if len(commits) > 5 else "") + ")"
	return [text + f" · {len(modified)} modified, {len(untracked)} untracked"]


def runs(runsDir):
	lines = []
	for path in sorted(glob.glob(os.path.join(runsDir, "result-*.json")), key=os.path.getmtime):
		data = loadJson(path) or {}
		name = os.path.basename(path)[len("result-"):-len(".json")]
		denials = len(data.get("permission_denials") or [])
		lines.append(f"run {name}: {data.get('terminal_reason') or 'unreadable'}"
			f" · {data.get('num_turns', '?')} turns · ${data.get('total_cost_usd', 0):.2f}"
			+ (f" · {denials} denials" if denials else "")
			+ (" · ERROR" if data.get("is_error") else ""))
	return lines


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
	worktree = os.path.join(os.path.dirname(mainRoot), "worktrees", topic)
	if not os.path.isdir(worktree):
		print(f"FAIL: no worktree at {worktree}")
		sys.exit(1)
	hookOnly = os.path.join(worktree, ".hook-only")
	print(f"{topic}: {sessionState(hookOnly)}")
	for line in current(hookOnly) + finished(hookOnly) + gitSummary(worktree) + runs(os.path.join(hookOnly, "runs")):
		print(line)


if __name__ == "__main__":
	main()

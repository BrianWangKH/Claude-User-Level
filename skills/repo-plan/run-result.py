#!/usr/bin/env python3
"""Judge one headless worker run from its files, for the planning session (main checkout).

Usage: python3 run-result.py <topic> [<run>] [--wait]
  <run>   default: the highest-numbered start-<n> in <worktree>/.hook-only/runs/
  --wait  sleep until exit-<run> exists (run it with run_in_background; never check the worker's PID)

Reads <main>/../worktrees/<topic>/.hook-only/runs/: start-<n>, exit-<n> ("<code> <time>"),
est-<n>.json (the estimate worker-control.py recorded at launch/resume), stopped-<n> (written by
`wt stop`), result-<n>.json and compact-<n>-<k>.json (claude -p json output).
- USD: total_cost_usd of the run's last file (highest compact-<n>-<k>, else result-<n>), minus the
  previous run's last file when it belongs to the same session (a resume is cumulative).
- Minutes: exit time minus start-<n> (duration_ms leaves out background subagents).
- Verdict, first match: no result (exit code, 90 = not logged in) / stopped (stopped-<n>) / failed
  (num_turns 0 or is_error) / the result's STATUS line / no STATUS line (treat as blocked).
  The first three are "invalid" for estimating.
Prints the verdict, the numbers, the Cost row, what to do next, on `done` the worktree's diff of
.claude and .mcp.json against origin's default branch, and the result text to relay.
cost-history.py import uses judge() for the worker and branch rows.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

NEXT = {
	"NO RESULT": "Tell the user the exit code (90: run `claude auth login` in an outside terminal), then `wt resume <topic>`.",
	"STOPPED": "Tell the user where it stopped (git log, .hook-only/budget/events.jsonl); `wt resume <topic>` continues it.",
	"FAILED": "Show the user the result and terminal_reason; fix the cause together, then `wt resume <topic>`.",
	"STATUS: done": "Point out any .claude / .mcp.json change below (the user reviews those), relay the summary and the push command; after the merge, `/finish-worktree <topic>`.",
	"STATUS: needs-decision": "Ask the user the questions, write the answers into the worktree plan, then `wt resume <topic>` (re-estimate the rest first: plans/worker/<topic>.json).",
	"STATUS: blocked": "Fix the cause with the user (or explain it), re-estimate the rest (plans/worker/<topic>.json), then `wt resume <topic>`.",
	"NO STATUS LINE": "Treat as blocked: relay the full result; log the missing line under the plan's ## Friction.",
}


def git(cwd, *args):
	result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
	return result.stdout.strip() if result.returncode == 0 else ""


def loadJson(path):
	try:
		with open(path, encoding="utf-8") as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


def readText(path):
	try:
		with open(path, encoding="utf-8") as f:
			return f.read().strip()
	except OSError:
		return None


def worktreeOf(topic):
	commonDir = git(os.getcwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
	if not commonDir:
		sys.exit("FAIL: not inside a git repo")
	return os.path.join(os.path.dirname(os.path.dirname(commonDir)), "worktrees", topic)


def runNumbers(runsDir):
	if not os.path.isdir(runsDir):
		return []
	return sorted(int(m.group(1)) for n in os.listdir(runsDir) if (m := re.fullmatch(r"start-(\d+)", n)))


def lastFile(runsDir, run):
	compacts = []
	for path in glob.glob(os.path.join(runsDir, f"compact-{run}-*.json")):
		match = re.search(r"-(\d+)\.json$", path)
		if match and os.path.getsize(path):
			compacts.append((int(match.group(1)), path))
	if compacts:
		return max(compacts)[1]
	path = os.path.join(runsDir, f"result-{run}.json")
	return path if os.path.isfile(path) and os.path.getsize(path) else None


def previousCost(runsDir, run, sessionId):
	for earlier in range(run - 1, 0, -1):
		path = lastFile(runsDir, earlier)
		if path:
			data = loadJson(path) or {}
			if data.get("session_id") == sessionId:
				return float(data.get("total_cost_usd") or 0)
			return 0.0
	return 0.0


def judge(runsDir, run):
	"""Everything known about one run, or None while it is still running."""
	start = readText(os.path.join(runsDir, f"start-{run}"))
	exitText = readText(os.path.join(runsDir, f"exit-{run}"))
	if not exitText:
		return None
	code, _, end = exitText.partition(" ")
	minutes = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60
	path = lastFile(runsDir, run)
	data = (loadJson(path) or {}) if path else {}
	result = loadJson(os.path.join(runsDir, f"result-{run}.json")) or {}
	text = str(result.get("result") or "").strip()
	usd = None
	if data:
		usd = float(data.get("total_cost_usd") or 0) - previousCost(runsDir, run, data.get("session_id"))
	turns = result.get("num_turns", 0)
	est = loadJson(os.path.join(runsDir, f"est-{run}.json")) or {}

	statusLines = [l.strip() for l in text.splitlines() if l.strip().startswith("STATUS:")]
	invalid, note = True, ""
	if not result:
		kind, verdict = "NO RESULT", f"NO RESULT: exit code {code}"
	elif os.path.isfile(os.path.join(runsDir, f"stopped-{run}")):
		kind, verdict = "STOPPED", "STOPPED by wt stop"
	elif not turns or result.get("is_error"):
		kind = "FAILED"
		verdict = f"FAILED: is_error={result.get('is_error')}, num_turns={turns}, terminal_reason={result.get('terminal_reason')}"
	elif statusLines:
		verdict, invalid = statusLines[0], False
		kind = next((k for k in NEXT if verdict.startswith(k)), "NO STATUS LINE")
		if not text.startswith("STATUS:"):
			note = "STATUS line not first"
	else:
		kind, verdict, invalid = "NO STATUS LINE", "NO STATUS LINE", False
	return {"run": run, "start": start, "end": end, "code": code, "minutes": minutes, "usd": usd,
		"turns": turns, "estMin": est.get("minutes"), "estUsd": est.get("usd"), "kind": kind,
		"verdict": verdict, "invalid": invalid, "note": note, "text": text}


def costCells(r):
	def fmt(value, digits):
		return "" if value is None else f"{value:.{digits}f}"
	note = "invalid" if r["invalid"] else f"num_turns {r['turns']}"
	if r["note"]:
		note += f"; {r['note']}"
	if r["estMin"] is None:
		note += "; est: none"
	return {"estUsd": fmt(r["estUsd"], 2), "actUsd": fmt(r["usd"], 2), "estMin": fmt(r["estMin"], 0),
		"actMin": fmt(r["minutes"], 1), "note": note}


def main():
	args = [a for a in sys.argv[1:] if a != "--wait"]
	if not args or len(args) > 2:
		sys.exit("usage: python3 run-result.py <topic> [<run>] [--wait]")
	topic = args[0]
	worktree = worktreeOf(topic)
	runsDir = os.path.join(worktree, ".hook-only", "runs")
	runs = runNumbers(runsDir)
	if not runs:
		sys.exit(f"FAIL: no start-<n> in {runsDir}")
	run = int(args[1]) if len(args) == 2 else runs[-1]
	exitPath = os.path.join(runsDir, f"exit-{run}")
	while "--wait" in sys.argv and not (os.path.isfile(exitPath) and os.path.getsize(exitPath)):
		time.sleep(30)

	r = judge(runsDir, run)
	if r is None:
		print(f"{topic} run {run}: still running (no exit-{run}); started {readText(os.path.join(runsDir, f'start-{run}'))}")
		return
	c = costCells(r)
	print(f"{topic} run {run}: {r['verdict']}" + (f"   ({r['note']}: log it under ## Friction)" if r["note"] else ""))
	print(f"start {r['start']} · end {r['end']} · exit {r['code']} · {r['minutes']:.1f} min"
		+ (f" · ${r['usd']:.2f} this run" if r["usd"] is not None else "") + f" · {r['turns']} turns")
	print(f"Cost row (recorded by the hooks; cost-history.py import writes it): | worker (run {run}) | {run} | {c['estUsd']} | {c['actUsd']} | {c['estMin']} | {c['actMin']} | {c['note']} |")
	print("Next: " + NEXT[r["kind"]].replace("<topic>", topic))
	if r["kind"] == "STATUS: done":
		base = ""
		for ref in ("origin/HEAD", "origin/main", "origin/master"):
			base = git(worktree, "merge-base", "HEAD", ref)
			if base:
				break
		diff = git(worktree, "diff", "--stat", f"{base}..HEAD", "--", ".claude", ".mcp.json") if base else "(no origin base)"
		print("Changes to .claude / .mcp.json: " + (diff or "none"))
	if r["text"]:
		print("--- result ---")
		print(r["text"])


if __name__ == "__main__":
	main()

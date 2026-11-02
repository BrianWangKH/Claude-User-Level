#!/usr/bin/env python3
"""Judge one headless worker run from its files, for the planning session (main checkout).

Usage: python3 run-result.py <topic> [<run>] [--wait]
  <run>   default: the highest-numbered start-<n> in <worktree>/.hook-only/runs/
  --wait  sleep until exit-<run> exists (run it with run_in_background; never check the worker's PID)

Reads <main>/../worktrees/<topic>/.hook-only/runs/: start-<n>, exit-<n> ("<code> <time>"),
est-<n>.json (the estimate worker-control.py recorded at launch/resume; "mode": "merge" for a merge
run, skill pr-conflict), stopped-<n> (written by
`wt stop`), result-<n>.json and compact-<n>-<k>.json (claude -p json output).
- USD: total_cost_usd of the run's last file (the result, compact or phase file with the highest
  total_cost_usd, which is cumulative), minus the previous run's last file when it belongs to the same
  session (a resume is cumulative).
- Minutes: exit time minus start-<n> (duration_ms leaves out background subagents).
- Verdict, first match: no result (exit code, 90 = not logged in) / stopped (stopped-<n>) / failed
  (num_turns 0 or is_error) / the result's STATUS line / no STATUS line (treat as blocked).
  The first three are "invalid" for estimating.
auto-<n>.json: worker-control.py auto's decision on an automatic resume (written before exit-<n>):
"resume" -> the next run already started, wait on its exit file instead of acting; "refused" -> why.
In local mode also worker-control.py auto-merge's: "merge" -> a merge run started because the branch
lacked the latest main, wait on it the same way.
verify-<n>.json: verify.py's real-data checks (only when <main>/plans/verify/<topic>.json exists).
verifier-<n>.json: verifier.py's verdict, the second layer (only when that spec has a "verifier" section).
In local mode also worker-control.py auto-fix's "fix" (auto-<n>.json): a fix run started because the real data
showed the branch wrong; wait on it the same way.
A topic auto-finish.py closed after an automatic merge has no worktree any more: the run files are read
from <main>/.hook-only/archive/<topic>/.hook-only/runs/ instead (also while --wait is polling).
merge-<n>.json (local mode): auto-merge.py's decision after `STATUS: done`: "merged" (into the local
main, commit given) or "refused" (why; a person reviews and merges by hand, or a merge run is needed).
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



def flowLocal(worktree):
	"""The topic's recorded flow mode (worker-control.py writes .hook-only/flow-mode at launch), else the env."""
	try:
		with open(os.path.join(worktree, ".hook-only", "flow-mode"), encoding="utf-8") as f:
			return f.read().strip() == "local"
	except OSError:
		return os.environ.get("FLOW_INTEGRATION") == "local"

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


def mainDirOf():
	commonDir = git(os.getcwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
	if not commonDir:
		sys.exit("FAIL: not inside a git repo")
	return os.path.dirname(commonDir)


def worktreeOf(topic):
	return os.path.join(os.path.dirname(mainDirOf()), "worktrees", topic)


def runsDirOf(topic):
	"""(runs dir, archived): the worktree's, or the archive's once auto-finish.py removed the worktree."""
	live = os.path.join(worktreeOf(topic), ".hook-only", "runs")
	archived = os.path.join(mainDirOf(), ".hook-only", "archive", topic, ".hook-only", "runs")
	if not os.path.isdir(live) and os.path.isdir(archived):
		return archived, True
	return live, False


def runNumbers(runsDir):
	if not os.path.isdir(runsDir):
		return []
	return sorted(int(m.group(1)) for n in os.listdir(runsDir) if (m := re.fullmatch(r"start-(\d+)", n)))


def lastFile(runsDir, run):
	"""The run's newest claude -p output: the file with the highest total_cost_usd (cumulative for the
	session). Not "the highest compact file": a run without a STATUS line gets no final compact, so its
	result file is newer (seen in a real run: compact-1-2 $4.34, result-1 $5.36)."""
	paths = [os.path.join(runsDir, f"result-{run}.json")]
	paths += glob.glob(os.path.join(runsDir, f"compact-{run}-*.json"))
	paths += glob.glob(os.path.join(runsDir, f"phase-{run}-*.json"))
	best, top = None, None
	for path in paths:
		if not (os.path.isfile(path) and os.path.getsize(path)):
			continue
		cost = (loadJson(path) or {}).get("total_cost_usd")
		if cost is not None and (top is None or float(cost) > top):
			best, top = path, float(cost)
	return best


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
		"turns": turns, "estMin": est.get("minutes"), "estUsd": est.get("usd"),
		"rawMin": est.get("raw_minutes"), "rawUsd": est.get("raw_usd"), "mode": est.get("mode"), "kind": kind,
		"verdict": verdict, "invalid": invalid, "note": note, "text": text}


def costCells(r):
	def fmt(value, digits):
		return "" if value is None else f"{value:.{digits}f}"
	note = "invalid" if r["invalid"] else f"num_turns {r['turns']}"
	if r["note"]:
		note += f"; {r['note']}"
	if r["estMin"] is None:
		note += "; est: none"
	estUsd, estMin = r["estUsd"], r["estMin"]
	# History rows hold the raw estimate when the run has one (cost-history.py bias uses only those).
	if r.get("rawUsd") is not None and r.get("rawMin") is not None:
		note += f"; est=raw (budget ${fmt(estUsd, 2) or '-'}, {fmt(estMin, 0) or '-'} min)"
		estUsd, estMin = r["rawUsd"], r["rawMin"]
	return {"estUsd": fmt(estUsd, 2), "actUsd": fmt(r["usd"], 2), "estMin": fmt(estMin, 0),
		"actMin": fmt(r["minutes"], 1), "note": note}


def main():
	args = [a for a in sys.argv[1:] if a != "--wait"]
	if not args or len(args) > 2:
		sys.exit("usage: python3 run-result.py <topic> [<run>] [--wait]")
	topic = args[0]
	worktree = worktreeOf(topic)
	runsDir, archived = runsDirOf(topic)
	runs = runNumbers(runsDir)
	if not runs:
		sys.exit(f"FAIL: no start-<n> in {runsDir}")
	run = int(args[1]) if len(args) == 2 else runs[-1]

	def finished():
		path = os.path.join(runsDirOf(topic)[0], f"exit-{run}")
		return os.path.isfile(path) and os.path.getsize(path)

	while "--wait" in sys.argv and not finished():
		time.sleep(30)
	runsDir, archived = runsDirOf(topic)

	r = judge(runsDir, run)
	if r is None:
		print(f"{topic} run {run}: still running (no exit-{run}); started {readText(os.path.join(runsDir, f'start-{run}'))}")
		return
	c = costCells(r)
	print(f"{topic} run {run}{' (' + r['mode'] + ')' if r['mode'] in ('merge', 'fix') else ''}: {r['verdict']}"
		+ (f"   ({r['note']}: log it under ## Friction)" if r["note"] else ""))
	print(f"start {r['start']} · end {r['end']} · exit {r['code']} · {r['minutes']:.1f} min"
		+ (f" · ${r['usd']:.2f} this run" if r["usd"] is not None else "") + f" · {r['turns']} turns")
	print(f"Cost row (recorded by the hooks; cost-history.py import writes it): | worker (run {run}) | {run} | {c['estUsd']} | {c['actUsd']} | {c['estMin']} | {c['actMin']} | {c['note']} |")
	checked = loadJson(os.path.join(runsDir, f"verify-{run}.json"))
	if checked:
		failed = [c for c in checked.get("checks") or [] if not c.get("ok")]
		if checked.get("passed"):
			print(f"Real-data checks: passed ({len(checked.get('checks') or [])} checks, verify-{run}.json).")
		else:
			print("Real-data checks FAILED (verify.py): " + (checked.get("error") or "; ".join(
				f"{c['name']}: {c['why']}" for c in failed)) + ". In PR mode tell the user and resume the worker with "
				"the failures (they go into the plan); in local mode a fix run starts by itself (below).")
	judged = loadJson(os.path.join(runsDir, f"verifier-{run}.json"))
	if judged:
		verdict = judged.get("verdict")
		if verdict == "passed":
			print(f"Verifier: passed (verifier-{run}.json).")
		elif verdict == "failed":
			print(f"Verifier: FAILED (verifier-{run}.json): " + "; ".join(l.strip() for l in str(judged.get("report") or "")
				.splitlines() if re.match(r"^\s*V\d+\s+fail", l))[:500] + ". In local mode a fix run starts by itself (below).")
		else:
			print(f"Verifier: could not judge ({judged.get('why')}). Tell the user: the verifier failed, not the branch.")
	auto = loadJson(os.path.join(runsDir, f"auto-{run}.json"))
	if auto and auto.get("decision") == "fix":
		nextRun = auto.get("next_run")
		print(f"Automatic fix run: run {nextRun} started by the hook (estimate {auto.get('minutes')} min, "
			f"${auto.get('usd')}): the real data showed the branch wrong, so the worker handles the findings as review "
			f"findings; its STATUS: done goes through the checks again. Tell the user in one line and wait: "
			f"`python3 ~/.claude/skills/repo-plan/run-result.py {topic} {nextRun} --wait`.")
	elif auto and auto.get("decision") == "merge":
		nextRun = auto.get("next_run")
		print(f"Automatic merge run: run {nextRun} started by the hook (estimate {auto.get('minutes')} min, "
			f"${auto.get('usd')}): the branch lacked the latest main, so the worker merges main into it and "
			f"resolves any conflict (merge.md); its STATUS: done goes to auto-merge again. Tell the user in one "
			f"line and wait: `python3 ~/.claude/skills/repo-plan/run-result.py {topic} {nextRun} --wait`.")
	elif auto and auto.get("decision") == "resume":
		nextRun = auto.get("next_run")
		print(f"Automatic resume: run {nextRun} started by the hook (estimate {auto.get('minutes')} min, "
			f"${auto.get('usd')}); the run stopped only because the planned dispatch didn't fit. Tell the user "
			f"in one line and wait: `python3 ~/.claude/skills/repo-plan/run-result.py {topic} {nextRun} --wait`.")
	else:
		if auto:
			print(f"No automatic resume: {auto.get('reason')}")
		print("Next: " + NEXT[r["kind"]].replace("<topic>", topic))
	merged = loadJson(os.path.join(runsDir, f"merge-{run}.json"))
	if merged and merged.get("decision") == "merged":
		print(f"Automatic merge: merged into {merged.get('into')} as {str(merged.get('commit'))[:10]} (local mode; "
			f"undo with `git revert -m 1 {str(merged.get('commit'))[:10]}`"
			+ ("; docs only, merged without a code review" if merged.get("review") == "docs-only" else "")
			+ f"). Tell the user in one line. auto-finish.py "
			f"then archives the records, imports the cost, appends the friction to plans/friction.md and removes the "
			f"worktree (<main>/.hook-only/archive/{topic}.finish.json says how it went); the friction waits for the "
			f"next planning session, no /finish-worktree needed.")
	elif merged and auto and auto.get("decision") in ("merge", "fix"):
		print(f"Automatic merge waits for the {auto.get('decision')} run: {merged.get('reason')}.")
	elif merged:
		print(f"Automatic merge refused: {merged.get('reason')}. Tell the user why; they review and merge by hand"
			+ (", or start the merge run (estimate --merge, wt resume): the automatic one didn't start." if merged.get("merge_run") else "."))
	if r["kind"] == "STATUS: done" and archived:
		print("Changes to .claude / .mcp.json: none merged (auto-merge holds .claude/ for a person); the worktree was "
			f"archived and removed after the automatic merge (<main>/.hook-only/archive/{topic}/).")
	elif r["kind"] == "STATUS: done":
		base = ""
		refs = ("main", "master") if flowLocal(worktree) else ("origin/HEAD", "origin/main", "origin/master")
		for ref in refs:
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

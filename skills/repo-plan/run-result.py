#!/usr/bin/env python3
"""Judge one headless worker run, for the planning session (main checkout).

Usage: python3 run-result.py <topic> [<run>] [--wait]
  <run>   default: the topic's current run (hooks/lib/runstate.py status: the highest record or file number)
  --wait  read the status again every status_wait_poll_seconds (budget run_state) until the run is
          ended, an orphan, unknown or absent (runstate.WAIT_ENDS), then print (run it with
          run_in_background; never check the worker's PID). A run that dies without its exit file turns
          orphan, so the wait always ends

Output (migration step 3 of the run record: a display reader; nothing decides on it): first runstate.agent
with runstate.detail, the status's KEY lines (PHASE, BUSY, RESUMABLE, TODO, ...), the line for people, the
evidence and what the run's evidence files say (verify, verifier, merge, the next run, every run's result
line); then this script's own lines: the run's numbers and Cost row (judge() below, which cost-history.py
import also uses), what to do next (Next:, from the status's claude part and exit reason), on `done` the
worktree's diff of .claude and .mcp.json, and the worker's result text to relay. The status is read-only and
replaces reading exit-<n>, auto-, merge-, verify- and verifier- files here; judge() and its helpers below
still read today's files for the cost (cost-history.py imports them).

judge() reads <main>/../worktrees/<topic>/.hook-only/runs/: start-<n>, exit-<n> ("<code> <time>"),
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
cost-history.py import uses judge() for the worker and branch rows.
What main() prints from runstate.detail: the next-run decision (auto-<n>.json: "resume", "merge" or "fix" ->
that run already started, wait on it instead of acting; "refused" -> why), the real-data checks
(verify-<n>.json, verifier-<n>.json) and the automatic merge (merge-<n>.json: "merged" with its commit, or
"refused" with why). A topic auto-finish.py closed after an automatic merge has no worktree any more:
runstate reads <main>/.hook-only/archive/<topic>/ instead (also while --wait is polling). runNumbers() is used
by cost-history.py.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HOME_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.join(HOME_ROOT, "hooks", "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import runstate  # noqa: E402
import wt_topics  # noqa: E402

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


def mainDirOf():
	commonDir = git(os.getcwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
	if not commonDir:
		sys.exit("FAIL: not inside a git repo")
	return os.path.dirname(commonDir)


def worktreeOf(topic):
	return str(wt_topics.worktreeOf(mainDirOf(), topic))


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


WAIT_AGAIN = "`python3 ~/.claude/skills/repo-plan/run-result.py {topic} {run} --wait`"
AUTOMATIC = {
	"fix": "Automatic fix run: run {run} started by the hook (estimate {minutes} min, ${usd}): the real data showed "
	"the branch wrong, so the worker handles the findings as review findings; its STATUS: done goes through the "
	"checks again. Tell the user in one line and wait: {wait}.",
	"merge": "Automatic merge run: run {run} started by the hook (estimate {minutes} min, ${usd}): the branch lacked "
	"the latest main, so the worker merges main into it and resolves any conflict (merge.md); its STATUS: done goes "
	"to auto-merge again. Tell the user in one line and wait: {wait}.",
	"resume": "Automatic resume: run {run} started by the hook (estimate {minutes} min, ${usd}); the run stopped only "
	"because the planned dispatch didn't fit. Tell the user in one line and wait: {wait}.",
}
NOT_ENDED = (
	"Next: run {run} is {phase}, not ended: tell the user the status line above and its TODO; nothing here "
	"decides on it. A wait that returned on an orphan or unknown phase: the run died or its record can't be read "
	"(evidence above); `wt status {topic}` checks the process outside the sandbox, and `wt resume {topic}` checks it "
	"too, closes a run whose process is gone (never merged) and resumes, and refuses while it runs or can't be checked."
)


def kindOf(status):
	"""The verdict NEXT is keyed on, from the status's claude part and exit reason (judge()'s order)."""
	claude = status.get("claude") or {}
	if not claude:
		return "NO RESULT"
	if (status.get("outcome") or {}).get("reason") == "stopped" or claude.get("outcome") == "stopped":
		return "STOPPED"
	if claude.get("outcome") == "error":
		return "FAILED"
	if claude.get("status"):
		return f"STATUS: {claude['status'].lower()}"
	return "NO STATUS LINE"


def costLines(runsDir, run):
	"""The run's numbers and its Cost row (judge(): today's files, as cost-history.py import reads them)."""
	r = judge(runsDir, run) if runsDir else None
	if r is None:
		return []
	c = costCells(r)
	return [
		f"start {r['start']} · end {r['end']} · exit {r['code']} · {r['minutes']:.1f} min"
		+ (f" · ${r['usd']:.2f} this run" if r["usd"] is not None else "") + f" · {r['turns']} turns"
		+ (f"   ({r['note']}: log it under ## Friction)" if r["note"] else ""),
		f"Cost row (recorded by the hooks; cost-history.py import writes it): | worker (run {run}) | {run} | "
		f"{c['estUsd']} | {c['actUsd']} | {c['estMin']} | {c['actMin']} | {c['note']} |",
	]


def nextLines(status, found, topic):
	"""What the planning session does next: the automatic run that started, or NEXT for the verdict; then the
	automatic merge's outcome."""
	lines = []
	auto, merged = found.get("next") or {}, found.get("merge") or {}
	checked, judged = found.get("verify") or {}, found.get("verifier") or {}
	if checked and checked.get("passed") is not True:
		lines.append("Real-data checks FAILED (verify.py, detail above): in PR mode tell the user and resume the worker "
			"with the failures (they go into the plan); in local mode a fix run starts by itself (below).")
	if judged.get("verdict") == "failed":
		lines.append("Verifier: FAILED (detail above). In local mode a fix run starts by itself (below).")
	elif judged and judged.get("verdict") != "passed":
		lines.append(f"Verifier: could not judge ({judged.get('why') or judged.get('error')}). Tell the user: the "
			"verifier failed, not the branch.")
	if auto.get("decision") in AUTOMATIC:
		wait = WAIT_AGAIN.format(topic=topic, run=auto.get("run"))
		lines.append(AUTOMATIC[auto["decision"]].format(wait=wait, **{k: auto.get(k) for k in ("run", "minutes", "usd")}))
	else:
		if auto:
			lines.append(f"No automatic resume: {auto.get('reason')}")
		lines.append("Next: " + NEXT.get(kindOf(status), NEXT["NO STATUS LINE"]).replace("<topic>", topic))
	if merged.get("decision") == "merged":
		commit = str(merged.get("commit"))[:10]
		lines.append(
			f"Automatic merge: merged into {merged.get('into')} as {commit} (local mode; undo with `git revert -m 1 "
			f"{commit}`" + ("; docs only, merged without a code review" if merged.get("review") == "docs-only" else "")
			+ "). Tell the user in one line. auto-finish.py then archives the records, imports the cost, appends the "
			f"friction to plans/friction.md and removes the worktree (<main>/.hook-only/archive/{topic}.finish.json "
			"says how it went); the friction waits for the next planning session, no /finish-worktree needed."
		)
	elif merged.get("decision") and auto.get("decision") in ("merge", "fix"):
		lines.append(f"Automatic merge waits for the {auto.get('decision')} run: {merged.get('reason')}.")
	elif merged.get("decision"):
		lines.append(
			f"Automatic merge refused: {merged.get('reason')}. Tell the user why; they review and merge by hand"
			+ (", or start the merge run (estimate --merge, wt resume): the automatic one didn't start."
				if merged.get("merge_run") else ".")
		)
	return lines


def readStatus(mainDir, topic, run, wait):
	"""The status of run (None: the current one), waiting as --wait says (module doc)."""
	status = runstate.status(mainDir, topic, run=run)
	run = status["run"]  # the wait stays on this run; an automatic next run is a wait of its own
	while wait and status["phase"] not in runstate.WAIT_ENDS:
		time.sleep(runstate.settings()["status_wait_poll_seconds"])
		status = runstate.status(mainDir, topic, run=run)
	return status


def main():
	args = [a for a in sys.argv[1:] if a != "--wait"]
	if not args or len(args) > 2:
		sys.exit("usage: python3 run-result.py <topic> [<run>] [--wait]")
	topic = args[0]
	worktree = worktreeOf(topic)
	status = readStatus(mainDirOf(), topic, int(args[1]) if len(args) == 2 else None, "--wait" in sys.argv)
	found = runstate.detail(status, text=False)
	print(runstate.agent(status, found))
	print()
	if status["phase"] == "none":
		print(f"Next: {topic} has no run yet (no run record and no run files, in its worktree or its archive).")
		return
	if status["phase"] != "ended":
		print(NOT_ENDED.format(topic=topic, run=status["run"], phase=status["phase"]))
		return
	for line in costLines(status["runs_dir"], status["run"]) + nextLines(status, found, topic):
		print(line)
	kind = kindOf(status)
	if kind == "STATUS: done" and status["archived"]:
		print("Changes to .claude / .mcp.json: none merged (auto-merge holds .claude/ for a person); the worktree was "
			f"archived and removed after the automatic merge (<main>/.hook-only/archive/{topic}/).")
	elif kind == "STATUS: done":
		base, why = homeconfig.integrationBase(worktree)
		diff = git(worktree, "diff", "--stat", f"{base}..HEAD", "--", ".claude", ".mcp.json") if base else f"(unknown: {why})"
		print("Changes to .claude / .mcp.json: " + (diff or "none"))
	text = (runstate.detail(status).get("result") or {}).get("text")
	if text:
		print("--- result ---")
		print(text)


if __name__ == "__main__":
	main()

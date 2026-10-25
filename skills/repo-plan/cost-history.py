#!/usr/bin/env python3
"""Estimate-vs-actual history for budgeting. Raw numbers only; bias is computed on demand.

Stored in <main checkout>/.claude-cost/history.tsv (globally gitignored; Bash can read and
write it, unlike plans/). add and import run from the main checkout. A worktree session can't read
the main checkout, so new-worktree.sh copies the file into <worktree>/.claude-cost/ (snapshot) and
every command run in a worktree reads that copy.

  add <date> <topic> <branch> <class> <agent> <round> <estTok> <actTok> <estMin> <actMin> <estUsd> <actUsd> <note>
      Append one row. Empty numbers as "". agent "branch" = the whole branch (USD and minutes).
      Put "invalid" in note for runs that say nothing about estimating (never started, login
      expired, killed by the environment); they are kept but never used for bias.
  bias agent <agent>
      Bias for one agent: last 5 valid rows of that agent, any branch.
  bias branch <type/scope> <class>
      Bias for a whole branch: last 5 valid "branch" rows of the same type/scope; if fewer
      than 5, filled with the same class, then with any branch.
  estimate branch <type/scope> <class> <usd> <minutes>
      A worker run: the `bias branch` lines, the corrected USD and minutes, the --max-budget-usd cap
      (USD x worker_usd_cap_factor) and the plans/worker/<topic>.json content.
  estimate agent <agent> <usd> <minutes>
      A subagent round, in a worktree: the agent's bias from the history copy times this branch's own
      actual/estimate for that agent so far (the `rows` ratios), and the `Budget:` line.
  snapshot <worktree>
      Copy this checkout's history into <worktree>/.claude-cost/history.tsv (header only when there
      is no history yet, so a missing copy always means the copy failed).
  rows [<checkout>]
      The subagent rounds of a checkout (default: this one) as Cost table rows, from the hooks'
      records: estimate = the `Budget:` line subagent-budget.py claimed (.hook-only/budget/events.jsonl),
      actual = subagent-cost.py's this_round.usd and started_at..stopped_at (.hook-only/cost/).
      Then this branch's actual/estimate per agent so far.
  import <checkout> <date> <topic> <branch> <class>
      Append every subagent round of that checkout (the `rows` output), every finished worker run
      (run-result.py judge(): estimate from est-<n>.json, actual USD and minutes) and one `branch`
      row (the worker runs summed; skipped when the checkout has no worker runs).

Bias = recency-weighted mean of actual/estimate (weights 5,4,3,2,1, newest first), per metric.
Below 1 means past estimates were too high; above 1, too low. estimate multiplies by every bias that
has at least one pair, in full; a metric without pairs stays at x1 and says so. An agent's USD
without pairs falls back to its tokens bias.

Token unit: rows whose note contains "tok=weighted" count tokens price-weighted (cache reads at a
tenth; hooks/tokenweights.py), the unit of budgets and of the Cost table's `est wtok`/`actual wtok`
columns. Older rows summed plain tokens (cache reads in full, about 5-8x larger), so the tokens
bias uses only "tok=weighted" rows; minutes and USD use every valid row.
"""
import csv
import glob
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime

COLUMNS = ["date", "topic", "branch", "class", "agent", "round", "estTok", "actTok",
	"estMin", "actMin", "estUsd", "actUsd", "note"]
WEIGHTS = [5, 4, 3, 2, 1]
WEIGHTED_MARK = "tok=weighted"
METRICS = [("tokens", "estTok", "actTok"), ("min", "estMin", "actMin"), ("usd", "estUsd", "actUsd")]


HISTORY = os.path.join(".claude-cost", "history.tsv")
BUDGET_DEFAULTS = os.path.expanduser("~/.claude/hooks/budget-defaults.json")


def gitPath(*args):
	result = subprocess.run(["git", "rev-parse", "--path-format=absolute", *args], capture_output=True, text=True)
	if result.returncode != 0:
		sys.exit("FAIL: not inside a git repo")
	return result.stdout.strip()


def checkouts():
	"""(this checkout's top, the main checkout's top); equal in the main checkout."""
	return os.path.realpath(gitPath("--show-toplevel")), os.path.realpath(os.path.dirname(gitPath("--git-common-dir")))


def historyPath():
	"""This checkout's history: the real one in the main checkout, the snapshot in a worktree."""
	return os.path.join(checkouts()[0], HISTORY)


def load(path):
	if not os.path.isfile(path):
		return []
	with open(path, encoding="utf-8", newline="") as f:
		return list(csv.DictReader(f, delimiter="\t"))


def number(text):
	try:
		value = float(text)
	except (TypeError, ValueError):
		return None
	return value if value > 0 else None


def isValid(row):
	return "invalid" not in (row.get("note") or "").lower()


def bias(rows):
	"""rows newest first; returns {metric: (bias, count)} over rows having both numbers."""
	out = {}
	for name, estKey, actKey in METRICS:
		usable = [r for r in rows if name != "tokens" or WEIGHTED_MARK in (r.get("note") or "")]
		pairs = [(number(r[estKey]), number(r[actKey])) for r in usable]
		pairs = [(e, a) for e, a in pairs if e and a][:len(WEIGHTS)]
		if pairs:
			weights = WEIGHTS[:len(pairs)]
			value = sum(w * a / e for w, (e, a) in zip(weights, pairs)) / sum(weights)
			out[name] = (value, len(pairs))
	return out


def describe(label, rows):
	result = bias(rows)
	if not result:
		return f"{label}: no estimate/actual pairs yet"
	parts = [f"{name} x{value:.2f} (n={count})" for name, (value, count) in result.items()]
	return f"{label}: " + ", ".join(parts)


def newestFirst(rows):
	return list(reversed(rows))


def cmdAdd(path, values):
	if len(values) != len(COLUMNS):
		sys.exit(f"FAIL: add needs {len(COLUMNS)} values ({' '.join(COLUMNS)}), got {len(values)}")
	os.makedirs(os.path.dirname(path), exist_ok=True)
	isNew = not os.path.isfile(path)
	with open(path, "a", encoding="utf-8", newline="") as f:
		writer = csv.writer(f, delimiter="\t", lineterminator="\n")
		if isNew:
			writer.writerow(COLUMNS)
		writer.writerow([v.replace("\t", " ").replace("\n", " ") for v in values])
	print(f"OK: added {values[4]} {values[5]} for {values[1]}")


def cmdBias(path, args):
	rows = [r for r in newestFirst(load(path)) if isValid(r)]
	if len(args) == 2 and args[0] == "agent":
		print(describe(f"agent {args[1]}", [r for r in rows if r["agent"] == args[1]]))
	elif len(args) == 3 and args[0] == "branch":
		picked = branchRows(rows, args[1], args[2])
		print(describe(f"branch {args[1]} ({args[2]})", picked))
		for r in picked:
			print(f"  {r['date']} {r['topic']} {r['class']}: est ${r['estUsd'] or '-'} / {r['estMin'] or '-'} min,"
				f" actual ${r['actUsd'] or '-'} / {r['actMin'] or '-'} min")
	else:
		sys.exit("usage: bias agent <agent> | bias branch <type/scope> <class>")


def branchRows(rows, scope, klass):
	"""Last 5 valid "branch" rows of the same type/scope, filled with the same class, then any."""
	branchRows = [r for r in rows if r["agent"] == "branch"]
	prefix = scope.rstrip("/") + "/"
	picked = [r for r in branchRows if r["branch"].startswith(prefix)]
	for extra in ([r for r in branchRows if r["class"] == klass], branchRows):
		for r in extra:
			if len(picked) >= len(WEIGHTS):
				break
			if r not in picked:
				picked.append(r)
	return picked[:len(WEIGHTS)]


def factors(*biases):
	"""{"usd": x, "min": x}: the product of the given bias() results, per metric."""
	out = {}
	for name in ("usd", "min"):
		out[name] = math.prod(b[name][0] for b in biases if name in b)
	return out


def corrected(usd, minutes, factor):
	return round(usd * factor["usd"], 2), math.ceil(minutes * factor["min"])


def cmdEstimate(path, args):
	if len(args) < 3 or args[0] not in ("branch", "agent"):
		sys.exit("usage: estimate branch <type/scope> <class> <usd> <minutes> | estimate agent <agent> <usd> <minutes>")
	if not os.path.isfile(path):
		sys.exit(f"FAIL: no history at {path}. In a worktree it is the copy new-worktree.sh makes;"
			" ask the planning session to run `cost-history.py snapshot <worktree>` (a headless worker ends"
			" with `STATUS: blocked`).")
	try:
		usd, minutes = float(args[-2]), float(args[-1])
	except ValueError:
		sys.exit("FAIL: <usd> and <minutes> must be numbers")
	rows = [r for r in newestFirst(load(path)) if isValid(r)]
	print(f"raw: ${usd:g}, {minutes:g} min")
	if args[0] == "branch":
		if len(args) != 5:
			sys.exit("usage: estimate branch <type/scope> <class> <usd> <minutes>")
		picked = branchRows(rows, args[1], args[2])
		history = bias(picked)
		print(describe(f"bias branch {args[1]} ({args[2]})", picked))
		for r in picked:
			print(f"  {r['date']} {r['topic']} {r['class']}: est ${r['estUsd'] or '-'} / {r['estMin'] or '-'} min,"
				f" actual ${r['actUsd'] or '-'} / {r['actMin'] or '-'} min")
		newUsd, newMin = corrected(usd, minutes, factors(history))
		try:
			with open(BUDGET_DEFAULTS, encoding="utf-8") as f:
				capFactor = float(json.load(f).get("worker_usd_cap_factor", 1.2))
		except (OSError, ValueError):
			capFactor = 1.2
		print(f"corrected: ${newUsd:g}, {newMin} min; cap ${newUsd * capFactor:.2f} (x{capFactor:g})")
		print("plans/worker/<topic>.json: " + json.dumps({"minutes": newMin, "est_usd": newUsd}))
		return
	if len(args) != 4:
		sys.exit("usage: estimate agent <agent> <usd> <minutes>")
	agent = args[1]
	mine = [r for r in rows if r["agent"] == agent]
	history = bias(mine)
	if "usd" not in history and "tokens" in history:
		history["usd"] = history["tokens"]
		print("history has no USD pairs for this agent: tokens bias used for USD")
	print(describe(f"history, {agent}", mine))
	here = [{"estUsd": r["estUsd"], "actUsd": r["actUsd"], "estMin": r["estMin"], "actMin": r["actMin"],
		"estTok": "", "actTok": "", "note": ""} for r in subagentRounds(checkouts()[0]) if r["agent"] == agent]
	branch = bias(list(reversed(here)))
	print(describe(f"this branch, {agent}", list(reversed(here))))
	newUsd, newMin = corrected(usd, minutes, factors(history, branch))
	print(f"corrected: ${newUsd:g}, {newMin} min")
	print(f"Budget: {newMin} min, ${newUsd:g}")


def cmdSnapshot(path, args):
	if len(args) != 1:
		sys.exit("usage: snapshot <worktree>")
	target = os.path.join(os.path.abspath(args[0]), HISTORY)
	os.makedirs(os.path.dirname(target), exist_ok=True)
	if os.path.isfile(path):
		shutil.copyfile(path, target)
	else:
		with open(target, "w", encoding="utf-8") as f:
			f.write("\t".join(COLUMNS) + "\n")
	print(f"OK: history copied to {target} ({len(load(target))} rows)")


def loadLines(path):
	out = []
	try:
		with open(path, encoding="utf-8") as f:
			for line in f:
				try:
					out.append(json.loads(line))
				except ValueError:
					pass
	except OSError:
		pass
	return out


def subagentRounds(checkout):
	"""One dict per subagent round in <checkout>/.hook-only, oldest first."""
	hookOnly = os.path.join(checkout, ".hook-only")
	budgets = {}
	for event in loadLines(os.path.join(hookOnly, "budget", "events.jsonl")):
		if event.get("event") == "SubagentStart" and "budget_minutes" in event:
			budgets.setdefault(event.get("agent_id"), []).append(event)
	rounds = []
	for path in glob.glob(os.path.join(hookOnly, "cost", "*.jsonl")):
		agentId = os.path.basename(path)[:-len(".jsonl")]
		last = {}
		for record in loadLines(path):
			last[record.get("round")] = record
		for roundNo, record in last.items():
			given = budgets.get(agentId, [])
			budget = given[roundNo - 1] if isinstance(roundNo, int) and 0 < roundNo <= len(given) else {}
			actMin = ""
			if record.get("started_at") and record.get("stopped_at"):
				seconds = (datetime.fromisoformat(record["stopped_at"]) - datetime.fromisoformat(record["started_at"])).total_seconds()
				actMin = f"{seconds / 60:.1f}"
			notes = []
			source = budget.get("budget_source") or "none"
			if source.startswith("default") or source == "none":
				notes.append("est: none")
			this = record.get("this_round") or {}
			if this.get("unpriced_requests"):
				notes.append(f"{this['unpriced_requests']} unpriced requests")
			if record.get("complete") is False:
				notes.append("record incomplete")
			rounds.append({"agent": record.get("agent_type") or "?", "id": agentId,
				"start": record.get("started_at") or record.get("stopped_at") or "",
				"estUsd": "" if source.startswith("default") or budget.get("budget_usd") is None else f"{budget['budget_usd']:g}",
				"actUsd": f"{this.get('usd', 0):.2f}",
				"estMin": "" if source.startswith("default") or "budget_minutes" not in budget else f"{budget['budget_minutes']:g}",
				"actMin": actMin, "note": "; ".join(notes)})
	rounds.sort(key=lambda r: r["start"])
	seen = {}
	for r in rounds:
		seen[r["agent"]] = seen.get(r["agent"], 0) + 1
		r["round"] = str(seen[r["agent"]])
	return rounds


def cmdRows(args):
	checkout = os.path.abspath(args[0]) if args else os.getcwd()
	rounds = subagentRounds(checkout)
	if not rounds:
		print(f"no subagent records in {checkout}/.hook-only")
		return
	print("| agent | round | est usd | actual usd | est min | actual min | gap reason |")
	print("|---|---|---|---|---|---|---|")
	for r in rounds:
		print(f"| {r['agent']} | {r['round']} | {r['estUsd']} | {r['actUsd']} | {r['estMin']} | {r['actMin']} | {r['note']} |")
	for agent in sorted({r["agent"] for r in rounds}):
		mine = [{"estUsd": r["estUsd"], "actUsd": r["actUsd"], "estMin": r["estMin"], "actMin": r["actMin"],
			"estTok": "", "actTok": "", "note": ""} for r in rounds if r["agent"] == agent]
		print(describe(f"this branch, {agent}", list(reversed(mine))))


def workerRuns(checkout):
	"""Finished worker runs of a checkout, as judged by run-result.py."""
	runResult = loadRunResult()
	runsDir = os.path.join(checkout, ".hook-only", "runs")
	judged = [runResult.judge(runsDir, n) for n in runResult.runNumbers(runsDir)]
	return [(r, runResult.costCells(r)) for r in judged if r]


def loadRunResult():
	"""run-result.py, loaded from its file (the hyphen keeps it from a plain import)."""
	import importlib.util
	spec = importlib.util.spec_from_file_location("run_result",
		os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-result.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def cmdImport(path, args):
	if len(args) != 5:
		sys.exit("usage: import <checkout> <date> <topic> <branch> <class>")
	checkout, date, topic, branch, klass = args
	checkout = os.path.abspath(checkout)
	rounds = subagentRounds(checkout)
	for r in rounds:
		cmdAdd(path, [date, topic, branch, klass, r["agent"], r["round"], "", "", r["estMin"], r["actMin"],
			r["estUsd"], r["actUsd"], r["note"]])
	runs = workerRuns(checkout)
	for r, c in runs:
		cmdAdd(path, [date, topic, branch, klass, "worker", str(r["run"]), "", "", c["estMin"], c["actMin"],
			c["estUsd"], c["actUsd"], c["note"]])
	valid = [(r, c) for r, c in runs if not r["invalid"]]
	if runs:
		def total(key):
			values = [c[key] for _, c in valid]
			return f"{sum(float(v) for v in values):.2f}" if values and all(values) else ""
		note = f"{len(valid)} of {len(runs)} worker runs valid"
		if not valid:
			note += "; invalid"
		cmdAdd(path, [date, topic, branch, klass, "branch", "-", "", "", total("estMin"), total("actMin"),
			total("estUsd"), total("actUsd"), note])
	print(f"OK: imported {len(rounds)} subagent rounds, {len(runs)} worker runs" + (", 1 branch row" if runs else ""))


def main():
	if len(sys.argv) < 2:
		sys.exit(__doc__)
	path = historyPath()
	command, args = sys.argv[1], sys.argv[2:]
	if command in ("add", "import", "snapshot") and len(set(checkouts())) != 1:
		sys.exit(f"FAIL: {command} runs from the main checkout (a worktree holds only a read copy)")
	if command == "add":
		cmdAdd(path, args)
	elif command == "bias":
		cmdBias(path, args)
	elif command == "estimate":
		cmdEstimate(path, args)
	elif command == "snapshot":
		cmdSnapshot(path, args)
	elif command == "rows":
		cmdRows(args)
	elif command == "import":
		cmdImport(path, args)
	else:
		sys.exit(__doc__)


if __name__ == "__main__":
	main()

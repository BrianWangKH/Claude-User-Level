#!/usr/bin/env python3
"""Estimate-vs-actual history for budgeting. Raw numbers only; bias is computed on demand.

Stored in <main checkout>/.claude-cost/history.tsv (globally gitignored; Bash can read and
write it, unlike plans/). Run from the main checkout.

  add <date> <topic> <branch> <class> <agent> <round> <estTok> <actTok> <estMin> <actMin> <estUsd> <actUsd> <note>
      Append one row. Empty numbers as "". agent "branch" = the whole branch (USD and minutes).
      Put "invalid" in note for runs that say nothing about estimating (never started, login
      expired, killed by the environment); they are kept but never used for bias.
  bias agent <agent>
      Bias for one agent: last 5 valid rows of that agent, any branch.
  bias branch <type/scope> <class>
      Bias for a whole branch: last 5 valid "branch" rows of the same type/scope; if fewer
      than 5, filled with the same class, then with any branch.
  summary
      One line per agent (agent bias), for the worktree's plans/past-estimates.md.

Bias = recency-weighted mean of actual/estimate (weights 5,4,3,2,1, newest first), per metric.
Below 1 means past estimates were too high; above 1, too low. Multiply a new estimate by it.
"""
import csv
import os
import subprocess
import sys

COLUMNS = ["date", "topic", "branch", "class", "agent", "round", "estTok", "actTok",
	"estMin", "actMin", "estUsd", "actUsd", "note"]
WEIGHTS = [5, 4, 3, 2, 1]
METRICS = [("tokens", "estTok", "actTok"), ("min", "estMin", "actMin"), ("usd", "estUsd", "actUsd")]


def historyPath():
	result = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
		capture_output=True, text=True)
	if result.returncode != 0:
		sys.exit("FAIL: not inside a git repo")
	return os.path.join(os.path.dirname(result.stdout.strip()), ".claude-cost", "history.tsv")


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
		pairs = [(number(r[estKey]), number(r[actKey])) for r in rows]
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
		branchRows = [r for r in rows if r["agent"] == "branch"]
		prefix = args[1].rstrip("/") + "/"
		picked = [r for r in branchRows if r["branch"].startswith(prefix)]
		for extra in ([r for r in branchRows if r["class"] == args[2]], branchRows):
			for r in extra:
				if len(picked) >= len(WEIGHTS):
					break
				if r not in picked:
					picked.append(r)
		print(describe(f"branch {args[1]} ({args[2]})", picked))
		for r in picked[:len(WEIGHTS)]:
			print(f"  {r['date']} {r['topic']} {r['class']}: est ${r['estUsd'] or '-'} / {r['estMin'] or '-'} min,"
				f" actual ${r['actUsd'] or '-'} / {r['actMin'] or '-'} min")
	else:
		sys.exit("usage: bias agent <agent> | bias branch <type/scope> <class>")


def cmdSummary(path):
	rows = [r for r in newestFirst(load(path)) if isValid(r) and r["agent"] != "branch"]
	agents = sorted({r["agent"] for r in rows})
	if not agents:
		print("no history yet")
	for agent in agents:
		print(describe(agent, [r for r in rows if r["agent"] == agent]))


def main():
	if len(sys.argv) < 2:
		sys.exit(__doc__)
	path = historyPath()
	command, args = sys.argv[1], sys.argv[2:]
	if command == "add":
		cmdAdd(path, args)
	elif command == "bias":
		cmdBias(path, args)
	elif command == "summary":
		cmdSummary(path)
	else:
		sys.exit(__doc__)


if __name__ == "__main__":
	main()

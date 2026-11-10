#!/usr/bin/env python3
"""Replay the estimate model over the cost history: how many rounds would it have stopped?

Usage: estimate-replay.py [<history.tsv>] [--window <n>] [--quantile <q>]
Defaults: this checkout's history (cost-history.py historyPath) and estimate_window_count /
estimate_fixed_quantile from home.json budget. For each subagent type, each worker class and the
branch rows, in date order, every row with a raw estimate gets the budget cost-history.py's model()
would have given from the rows before it; it counts as stopped when its actual USD or minutes exceed
budget_headroom_factor x that budget (where the hooks stop it). Also prints the median budget / actual, the
room the model leaves. Run it before changing estimate_window_count or estimate_fixed_quantile, and now and
then as the history grows. A budget-stopped row's actual is a lower bound of its need, the same for
every setting compared.
"""
import importlib.util
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cost_history", os.path.join(HERE, "cost-history.py"))
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)


def option(args, name, cast, default):
	if name in args:
		i = args.index(name)
		value = cast(args[i + 1])
		del args[i:i + 2]
		return value
	return default


def replay(groups, window, q, headroom):
	stops = n = 0
	room = []
	for seq in groups.values():  # oldest first
		for i, row in enumerate(seq):
			pairs = {m: (ch.number(row[e]), ch.number(row[a])) for m, e, a in ch.METRICS}
			if not all(e and a for e, a in pairs.values()):
				continue
			fitted = ch.model(list(reversed(seq[:i])), window, q)
			usd, minutes = ch.applyModel(pairs["usd"][0], pairs["min"][0], fitted)
			budgets = {"usd": usd, "min": minutes}
			n += 1
			stops += any(pairs[m][1] > headroom * budgets[m] for m in budgets)
			room.append(statistics.mean(budgets[m] / pairs[m][1] for m in budgets))
	return stops, n, (statistics.median(room) if room else 0.0)


def main(argv):
	args = argv[1:]
	window, q = ch.modelSettings()
	window = option(args, "--window", int, window)
	q = option(args, "--quantile", float, q)
	path = args[0] if args else ch.historyPath()
	headroom = float(ch.loadDefaults()["budget_headroom_factor"])
	rows = [r for r in ch.load(path) if ch.isValid(r) and ch.RAW_MARK in (r.get("note") or "")]
	agents, workers, branches = {}, {}, {}
	for r in rows:
		if r["agent"].startswith("worker"):
			workers.setdefault(r["class"], []).append(r)
		elif r["agent"] == "branch":
			branches.setdefault(r["class"], []).append(r)
		elif r["agent"] != ch.MERGE:
			agents.setdefault(r["agent"], []).append(r)
	print(f"window {window}, fixed quantile {q}, stopped above x{headroom:g} the budget ({path})")
	for label, groups in (("subagent rounds", agents), ("worker runs", workers), ("branch rows", branches)):
		stops, n, room = replay(groups, window, q, headroom)
		print(f"  {label:16s} stopped {stops:3d} of {n:3d}, median budget/actual {room:.2f}")


if __name__ == "__main__":
	main(sys.argv)

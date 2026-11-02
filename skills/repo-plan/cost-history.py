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
      The estimate model (below) for one agent: its last estimate_window valid rows, any branch.
  bias branch <type/scope> <class>
      The model for a whole branch: the last estimate_window valid "branch" rows of the same type/scope;
      if fewer, filled with the same class, then with any branch.
  estimate branch <type/scope> <class> <usd> <minutes> [--reviews <n>] [--dev-rounds <n>] [--merge]
      A worker run: <usd> and <minutes> are the work itself, without the quality gates' waits. Prints
      the `bias branch` lines, the corrected USD and minutes, plus the hook-wait reserve: <n> code-reviewer
      rounds x the mutation scan wait and <n> developer rounds x the crap-gate wait, each the longest of
      the last 5 recorded waits of that agent (the `hook wait` notes); none in a repo without
      .claude/quality-gates.json, where neither gate runs. The class decides which counts apply:
      logic and wiring (test-first) need both (a `Merge review:` round runs no scan: leave it out of
      --reviews), small fix only --dev-rounds, other classes neither. Then the limits worker-control.py
      derives (x budget_headroom, budget-defaults.json) and the plans/worker/<topic>.json content
      (with the class, which crap-gate.py reads). No floors: the headroom and the reserve cover what
      the floors did.
      --merge: a merge run (skill pr-conflict, feature-flow merge.md), estimated on its own: the model
      comes from the last estimate_window valid "merge" rows of any branch (none yet: the raw estimate), only --dev-rounds applies
      (a `Merge review:` runs no scan; developer resolves implementation conflicts in any class), and
      the json gets "mode": "merge", which worker-control.py turns into the merge prompt.
      --fix: a fix run (worker-control.py auto-fix, after the real data showed a finished branch wrong): the
      model comes from the last estimate_window valid "fix" rows of any branch (none yet: the raw estimate);
      the class's gate waits apply as for the branch, one round of each gated agent the class dispatches unless
      --reviews / --dev-rounds say otherwise (a fix: a qa fixture, one developer round, one review). No "mode" in
      the json: worker-control.py starts the run itself.
  estimate agent <agent> <usd> <minutes> [--resume]
      A subagent round, in a worktree: the agent's model from the history copy and the lines for the
      dispatch: `Budget:` (the corrected estimate; the hook stops the agent at budget_headroom x it)
      and `Estimate:` (the raw numbers, which the hooks record for the bias). A gate's wait is not
      part of an agent's budget (qg_common.excludeWait takes it out). This branch's own
      actual/estimate (the `rows` ratios) is printed for reference only: its rounds were budgeted with
      the history bias already, so multiplying both shrinks twice.
      The orchestrator doesn't run it to dispatch: dispatch-guard.py applies the same model (applyModel)
      to a prompt's `Estimate:` line (`Remaining:` = --resume) and adds the Budget line itself; this
      command only shows the model.
  snapshot <worktree>
      Copy this checkout's history into <worktree>/.claude-cost/history.tsv (header only when there
      is no history yet, so a missing copy always means the copy failed).
  rows [<checkout>]
      The subagent rounds of a checkout (default: this one) as Cost table rows, from the hooks'
      records: estimate = the dispatch's raw `Estimate:` line (note `est=raw`, the budget in the note),
      or for older rounds without one the `Budget:` line (not used for bias), as subagent-budget.py
      claimed them (.hook-only/budget/events.jsonl),
      actual = subagent-cost.py's this_round.usd and started_at..stopped_at (.hook-only/cost/).
      A round the budget hook stopped (a "Budget used up" or "80% of the budget used" deny in
      events.jsonl) gets the note
      "budget-stopped": its work was cut short, so its actual is only a lower bound of its need.
      Then this branch's actual/estimate per agent so far.
  import <checkout> <date> <topic> <branch> <class>
      Append every subagent round of that checkout (the `rows` output), every finished worker run
      (run-result.py judge(): estimate from est-<n>.json, actual USD and minutes without the hook waits
      inside the run, which the raw estimate leaves out) and one `branch` row (the worker runs summed;
      skipped when the checkout has no worker runs). A merge run (its est-<n>.json has "mode": "merge")
      is recorded with agent "merge" instead of "worker" and stays out of the branch row: the branch
      bias is about building a topic, and merges get their own bias (estimate branch --merge). A fix run
      ("mode": "fix", worker-control.py auto-fix) likewise becomes an agent "fix" row (estimate branch --fix).
      It writes <checkout>/.hook-only/imported and refuses a checkout that has one: auto-finish.py imports
      after an automatic merge, and a later /finish-worktree must not add the rows again.

Estimate model (2026-11-01, replacing a single bias factor and its floor): per metric (USD, minutes), over
the last estimate_window valid rows with a raw estimate (budget-defaults.json), newest first:
  fixed = the estimate_fixed_quantile of their actuals (default the median): what a round or run needs
          whatever its size (an agent's set-up, a reviewer's scratch copy and mutants, a worker's start,
          compactions and wrap-up)
  slope = the largest (actual - fixed) / raw estimate over the same rows, at least 0
  need  = fixed + slope x raw
A single factor scaled small jobs below their fixed part and stopped them (code-reviewer at 8.7 of 5
minutes; a docs worker run at 4.0 of a 4-minute limit), and a floor only hid that. Replayed over the
history (skills/repo-plan/estimate-replay.py; a round counts as stopped when its actual exceeds
budget_headroom x the budget the model would have given from the rows before it): subagent rounds 3 of 96
stopped (the floored maximum 4; median budget 2.04 x actual, minutes rounded up), worker runs 4 of 32 (the
weighted mean 15), branch rows 1 of 15 (3). A budget-stopped row is a lower bound of its need; the
maximum slope takes it as it is. No rows: the raw estimate as it is. Re-run the replay when the window or
quantile change.
`estimate agent ... --resume` (the round after a CHECKPOINT): that round already proved the estimate low,
so the budget is never below the raw estimate of the remaining work (the model may still raise it).
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
import re
import shutil
import subprocess
import sys
from datetime import datetime

COLUMNS = ["date", "topic", "branch", "class", "agent", "round", "estTok", "actTok",
	"estMin", "actMin", "estUsd", "actUsd", "note"]
WEIGHTS = [5, 4, 3, 2, 1]
WEIGHTED_MARK = "tok=weighted"
STOPPED_MARK = "budget-stopped"
RAW_MARK = "est=raw"
WAIT_NOTE = re.compile(r"hook wait (\d+(?:\.\d+)?) min excluded")
# Which gate holds which agent: the mutation scan the code-reviewer, crap-gate's coverage run the developer.
GATED_AGENTS = ("code-reviewer", "developer")
# Classes whose flow dispatches each gated agent (feature-flow SKILL.md: test-first and small-fix.md);
# "merge" stands for a merge run (merge.md: developer for implementation conflicts, merge review unscanned).
MERGE = "merge"
# A fix run (worker-control.py auto-fix): its own rows, like a merge run's, out of the branch row.
FIX = "fix"
GATED_CLASSES = {"code-reviewer": ("logic", "wiring"), "developer": ("logic", "wiring", "small fix", MERGE)}
COUNT_OPTION = {"code-reviewer": "--reviews", "developer": "--dev-rounds"}
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
	"""rows newest first; returns {metric: (bias, count)} over rows having both numbers.

	USD and minutes use only rows whose estimate is the raw one (RAW_MARK). Older rows hold the budget
	the dispatch was given, already multiplied by the bias and raised to a floor, so actual / budget
	compounds the bias; and the budget hook caps the actual at that budget, so the ratio stayed below 1
	and every correction shrank the next budget (2026-10-31: 1 of 48 counted rounds above its budget,
	26 of 56 budgets at the floor). A stopped round stays in as a lower bound: its real need was at
	least what it used, and dropping it removed exactly the rounds that showed an underestimate."""
	out = {}
	for name, estKey, actKey in METRICS:
		if name == "tokens":
			usable = [r for r in rows if WEIGHTED_MARK in (r.get("note") or "")
				and STOPPED_MARK not in (r.get("note") or "")]
		else:
			usable = [r for r in rows if RAW_MARK in (r.get("note") or "")]
		pairs = [(number(r[estKey]), number(r[actKey])) for r in usable]
		pairs = [(e, a) for e, a in pairs if e and a][:len(WEIGHTS)]
		if pairs:
			weights = WEIGHTS[:len(pairs)]
			value = sum(w * a / e for w, (e, a) in zip(weights, pairs)) / sum(weights)
			out[name] = (value, len(pairs))
	return out


def quantile(values, q):
	s = sorted(values)
	k = (len(s) - 1) * q
	low = int(k)
	high = min(low + 1, len(s) - 1)
	return s[low] + (s[high] - s[low]) * (k - low)


def modelSettings():
	defaults = loadDefaults()
	return int(defaults.get("estimate_window", 10)), float(defaults.get("estimate_fixed_quantile", 0.5))


def model(rows, window=None, q=None):
	"""{metric: (fixed, slope, n)} from rows newest first (module doc, Estimate model): raw-estimate rows
	only (budget-stopped ones included, as lower bounds); a metric without pairs is left out."""
	if window is None or q is None:
		window, q = modelSettings()
	out = {}
	for name, estKey, actKey in METRICS:
		if name == "tokens":
			continue
		pairs = [(number(r[estKey]), number(r[actKey])) for r in rows if RAW_MARK in (r.get("note") or "")]
		pairs = [(e, a) for e, a in pairs if e and a][:window]
		if pairs:
			fixed = quantile([a for _, a in pairs], q)
			slope = max(max(a - fixed, 0) / e for e, a in pairs)
			out[name] = (fixed, slope, len(pairs))
	return out


def applyModel(usd, minutes, fitted, resume=False):
	"""(usd, minutes) the model gives for a raw estimate; with resume, never below the raw estimate."""
	def one(name, raw):
		if name not in fitted:
			return raw
		fixed, slope, _ = fitted[name]
		need = fixed + slope * raw
		return max(need, raw) if resume else need
	return round(one("usd", usd), 2), math.ceil(one("min", minutes))


def describeModel(label, fitted):
	if not fitted:
		return f"{label}: no estimate/actual pairs yet (the raw estimate as it is)"
	parts = [f"{name} {fixed:.2f} + {slope:.2f} x raw (n={n})" for name, (fixed, slope, n) in fitted.items()]
	return f"{label}: " + ", ".join(parts)


def describe(label, rows, result=None):
	result = bias(rows) if result is None else result
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
		mine = [r for r in rows if r["agent"] == args[1]]
		print(describeModel(f"agent {args[1]}", model(mine)))
	elif len(args) == 3 and args[0] == "branch":
		picked = branchRows(rows, args[1], args[2])
		print(describeModel(f"branch {args[1]} ({args[2]})", model(picked)))
		for r in picked:
			print(f"  {r['date']} {r['topic']} {r['class']}: est ${r['estUsd'] or '-'} / {r['estMin'] or '-'} min,"
				f" actual ${r['actUsd'] or '-'} / {r['actMin'] or '-'} min")
	else:
		sys.exit("usage: bias agent <agent> | bias branch <type/scope> <class>")


def branchRows(rows, scope, klass):
	"""Last estimate_window valid "branch" rows of the same type/scope, filled with the same class, then any."""
	window = modelSettings()[0]
	branchRows = [r for r in rows if r["agent"] == "branch"]
	prefix = scope.rstrip("/") + "/"
	picked = [r for r in branchRows if r["branch"].startswith(prefix)]
	for extra in ([r for r in branchRows if r["class"] == klass], branchRows):
		for r in extra:
			if len(picked) >= window:
				break
			if r not in picked:
				picked.append(r)
	return picked[:window]


def loadDefaults():
	try:
		with open(BUDGET_DEFAULTS, encoding="utf-8") as f:
			return json.load(f)
	except (OSError, ValueError):
		return {}


def gatesOn(checkout):
	"""True when the repo has quality gates: without .claude/quality-gates.json neither gate runs."""
	return os.path.isfile(os.path.join(checkout, ".claude", "quality-gates.json"))


def recordedWaits(rows, agent):
	"""The hook waits (minutes) of an agent's rounds, newest first, from the rows' `hook wait` notes."""
	out = []
	for r in rows:
		if r.get("agent") == agent and isValid(r):
			match = WAIT_NOTE.search(r.get("note") or "")
			if match:
				out.append(float(match.group(1)))
	return out


def expectedWait(rows, agent):
	"""(minutes, count): the longest of the agent's last 5 recorded waits; (0, 0) without any.

	The longest, not the mean: the reserve keeps a run from ending while a gate still holds the agent,
	so it has to cover a slow scan, and a reserve that turns out too large only leaves time unused."""
	waits = recordedWaits(rows, agent)[:len(WEIGHTS)]
	return (max(waits), len(waits)) if waits else (0.0, 0)


def checkoutWait(checkout, agent):
	"""expectedWait over <checkout>'s history (the worktree copy) plus its own rounds so far.

	dispatch-guard.py calls this before a code-reviewer or developer dispatch in a headless worker run;
	0 in a repo without quality gates."""
	if not gatesOn(checkout):
		return 0.0
	rows = load(os.path.join(checkout, HISTORY)) + subagentRounds(checkout)
	return expectedWait(newestFirst(rows), agent)[0]


USAGE_BRANCH = ("estimate branch <type/scope> <class> <usd> <minutes> [--reviews <n>] [--dev-rounds <n>]"
	" [--merge | --fix]")


def takeCounts(args):
	"""(args without the count options, {agent: n} for the options given)."""
	rest, counts = [], {}
	options = {option: agent for agent, option in COUNT_OPTION.items()}
	i = 0
	while i < len(args):
		if args[i] in options:
			if i + 1 >= len(args) or not args[i + 1].isdigit():
				sys.exit(f"FAIL: {args[i]} needs a whole number")
			counts[options[args[i]]] = int(args[i + 1])
			i += 2
		else:
			rest.append(args[i])
			i += 1
	return rest, counts


def waitReserve(rows, checkout, klass, counts):
	"""(minutes, lines): the hook-wait reserve of a worker run, or a FAIL exit for wrong counts."""
	lines, total = [], 0.0
	gates = gatesOn(checkout)
	for agent in GATED_AGENTS:
		option = COUNT_OPTION[agent]
		applies = klass in GATED_CLASSES[agent]
		kind = "a merge run" if klass == MERGE else f"class {klass}"
		if not applies:
			if counts.get(agent):
				sys.exit(f"FAIL: {kind} dispatches no {agent} that waits on a gate; drop {option}")
			continue
		if agent not in counts:
			sys.exit(f"FAIL: {kind} needs {option} <n>: how many {agent} rounds this run will have,"
				" judged from the plan (0 if none)")
		n = counts[agent]
		if not gates:
			lines.append(f"hook wait, {agent}: no .claude/quality-gates.json, no gate wait")
			continue
		wait, seen = expectedWait(rows, agent)
		if not seen:
			lines.append(f"hook wait, {agent}: no recorded wait yet; {n} rounds x 0 min (the estimate must include it)")
			continue
		total += n * wait
		lines.append(f"hook wait, {agent}: {n} rounds x {wait:g} min (longest of the last {seen} recorded)")
	return total, lines


def cmdEstimate(path, args):
	if "--merge-review" in args:
		# Older feature-flow text passed it to skip the code-reviewer floor; there are no floors now.
		print("note: --merge-review is no longer needed; ignored")
		args = [a for a in args if a != "--merge-review"]
	merge = "--" + MERGE in args
	fix = "--" + FIX in args
	resume = "--resume" in args
	args = [a for a in args if a not in ("--" + MERGE, "--" + FIX, "--resume")]
	if merge and fix:
		sys.exit("usage: --merge and --fix exclude each other")
	if resume and args[:1] != ["agent"]:
		sys.exit("usage: --resume only goes with estimate agent (the round after a CHECKPOINT)")
	args, counts = takeCounts(args)
	if (counts or merge or fix) and args[:1] != ["branch"]:
		sys.exit("usage: --reviews, --dev-rounds, --merge and --fix only go with estimate branch")
	if len(args) < 3 or args[0] not in ("branch", "agent"):
		sys.exit(f"usage: {USAGE_BRANCH} | estimate agent <agent> <usd> <minutes>")
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
			sys.exit(f"usage: {USAGE_BRANCH}")
		klass = args[2]
		if fix:
			for agent in GATED_AGENTS:
				if klass in GATED_CLASSES[agent]:
					counts.setdefault(agent, 1)
		# Waits come from every agent row (subagent rounds of any branch), not only the picked branch rows.
		reserve, waitLines = waitReserve(rows, checkouts()[0], MERGE if merge else klass, counts)
		if merge or fix:
			kind = MERGE if merge else FIX
			picked = [r for r in rows if r["agent"] == kind][:modelSettings()[0]]
			label = f"model {kind} runs (any branch)"
		else:
			picked = branchRows(rows, args[1], klass)
			label = f"model branch {args[1]} ({klass})"
		fitted = model(picked)
		print(describeModel(label, fitted))
		for r in picked:
			print(f"  {r['date']} {r['topic']} {r['class']}: est ${r['estUsd'] or '-'} / {r['estMin'] or '-'} min,"
				f" actual ${r['actUsd'] or '-'} / {r['actMin'] or '-'} min")
		newUsd, newMin = applyModel(usd, minutes, fitted)
		print(f"modelled work: ${newUsd:g}, {newMin} min")
		for line in waitLines:
			print(line)
		# The waits are measured, not judged, so the bias doesn't touch them; they cost no USD (the gates
		# run locally while the agent waits).
		newMin = math.ceil(newMin + reserve)
		print(f"estimate: ${newUsd:g}, {newMin} min (work + {reserve:g} min hook wait)")
		headroom = loadDefaults().get("budget_headroom")
		if headroom:
			print(f"limits (worker-control.py, x{headroom:g}): {math.ceil(newMin * headroom)} min,"
				f" ${newUsd * headroom:.2f} --max-budget-usd")
		else:
			print("limits: worker-control.py applies budget_headroom (budget-defaults.json unreadable here)")
		# raw_minutes / raw_usd: worker-control.py records them per run, so the branch bias compares
		# actual (without hook waits) with the raw estimate (without them too).
		# "class" stays the topic's class in a merge run too: crap-gate.py reads it (presentation).
		print("plans/worker/<topic>.json: " + json.dumps({"minutes": newMin, "est_usd": newUsd,
			"raw_minutes": minutes, "raw_usd": usd, "hook_wait_minutes": reserve, "class": klass,
			**({"mode": MERGE} if merge else {})}))
		return
	if len(args) != 4:
		sys.exit("usage: estimate agent <agent> <usd> <minutes>")
	agent = args[1]
	mine = [r for r in rows if r["agent"] == agent]
	fitted = model(mine)
	# No tokens-for-USD fallback: the tokens pairs are old rows measured against corrected budgets, the
	# same compounding the RAW_MARK rule removes; without raw pairs the raw estimate is used as it is.
	print(describeModel(f"history, {agent}", fitted) + (" (resume: not below the raw estimate)" if resume else ""))
	here = [{"estUsd": r["estUsd"], "actUsd": r["actUsd"], "estMin": r["estMin"], "actMin": r["actMin"],
		"estTok": "", "actTok": "", "note": r["note"]} for r in subagentRounds(checkouts()[0]) if r["agent"] == agent]
	print(describe(f"this branch, {agent} (reference only, not applied)", list(reversed(here))))
	newUsd, newMin = applyModel(usd, minutes, fitted, resume)
	print(f"modelled: ${newUsd:g}, {newMin} min")
	# The lines go into the dispatch verbatim: dispatch-guard.py matches the Budget line only when it
	# stands alone (a sentence after it on the same line was denied twice on 2026-10-31). The Estimate
	# line carries the raw numbers to the history, so the bias compares actual with them.
	print("Paste these three lines into the dispatch as they are:")
	print(f"Budget: {newMin} min, ${newUsd:g}")
	print(RULE_LINE)
	print(f"Estimate: {minutes:g} min, ${usd:g}")


# The second dispatch line; dispatch-guard.py's fixed review prompt uses the same text.
RULE_LINE = ("Follow the budget rules in `agent-common`: aim to finish within the budget, `CHECKPOINT`"
	" when the rest clearly won't fit, `BLOCKED` early.")


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
	denials = {}
	waits = {}
	for event in loadLines(os.path.join(hookOnly, "budget", "events.jsonl")):
		if event.get("event") == "HookWait" and event.get("t"):
			waits.setdefault(event.get("agent_id"), []).append(
				(datetime.fromisoformat(event["t"]).astimezone(), float(event.get("seconds") or 0)))
		if event.get("event") == "SubagentStart" and "budget_minutes" in event:
			budgets.setdefault(event.get("agent_id"), []).append(event)
		if event.get("event") == "PreToolUse" and (event.get("note") or "").startswith(("deny: Budget used up", "deny: 80% of the budget used")) and event.get("t"):
			# events.jsonl times are local and naive; astimezone() reads them as local
			denials.setdefault(event.get("agent_id"), []).append(datetime.fromisoformat(event["t"]).astimezone())
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
			waitNote = ""
			if record.get("started_at") and record.get("stopped_at"):
				start, stop = datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["stopped_at"])
				# A hand-back gate's wait (mutation scan, coverage run) is hook time, not the agent's work.
				waited = sum(s for t, s in waits.get(agentId, []) if start <= t <= stop)
				seconds = (stop - start).total_seconds() - waited
				actMin = f"{seconds / 60:.1f}"
				if waited >= 60:
					waitNote = f"hook wait {waited / 60:.1f} min excluded"
			notes = [waitNote] if waitNote else []
			source = budget.get("budget_source") or "none"
			raw = not source.startswith("default") and budget.get("raw_usd") is not None \
				and budget.get("raw_minutes") is not None
			if raw:
				notes.append(f"{RAW_MARK} (budget ${budget.get('budget_usd') or 0:g}, {budget['budget_minutes']:g} min)")
			if source.startswith("default") or source == "none":
				notes.append("est: none")
			this = record.get("this_round") or {}
			if this.get("unpriced_requests"):
				notes.append(f"{this['unpriced_requests']} unpriced requests")
			if record.get("complete") is False:
				notes.append("record incomplete")
			if record.get("started_at") and record.get("stopped_at"):
				start, stop = datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["stopped_at"])
				if any(start <= t <= stop for t in denials.get(agentId, [])):
					notes.append(STOPPED_MARK)
			rounds.append({"agent": record.get("agent_type") or "?", "id": agentId,
				"start": record.get("started_at") or record.get("stopped_at") or "",
				"estUsd": f"{budget['raw_usd']:g}" if raw else "" if source.startswith("default") or budget.get("budget_usd") is None else f"{budget['budget_usd']:g}",
				"actUsd": f"{this.get('usd', 0):.2f}",
				"estMin": f"{budget['raw_minutes']:g}" if raw else "" if source.startswith("default") or "budget_minutes" not in budget else f"{budget['budget_minutes']:g}",
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
			"estTok": "", "actTok": "", "note": r["note"]} for r in rounds if r["agent"] == agent]
		print(describe(f"this branch, {agent}", list(reversed(mine))))


def workerRuns(checkout):
	"""Finished worker runs of a checkout, as judged by run-result.py, with the hook waits inside each
	run taken out of its actual minutes (the raw estimate leaves them out; estimate branch adds them as
	a measured reserve)."""
	runResult = loadRunResult()
	runsDir = os.path.join(checkout, ".hook-only", "runs")
	judged = [runResult.judge(runsDir, n) for n in runResult.runNumbers(runsDir)]
	waits = [(datetime.fromisoformat(e["t"]).astimezone(), float(e.get("seconds") or 0))
		for e in loadLines(os.path.join(checkout, ".hook-only", "budget", "events.jsonl"))
		if e.get("event") == "HookWait" and e.get("t")]
	out = []
	for r in judged:
		if not r:
			continue
		cells = runResult.costCells(r)
		try:
			start = datetime.fromisoformat(r["start"]).astimezone()
			end = datetime.fromisoformat(r["end"]).astimezone()
		except (TypeError, ValueError, KeyError):
			start = end = None
		waited = sum(s for t, s in waits if start and start <= t <= end)
		if waited >= 60 and cells.get("actMin"):
			cells["actMin"] = f"{max(float(cells['actMin']) - waited / 60, 0):.1f}"
			cells["note"] = f"hook wait {waited / 60:.1f} min excluded; " + cells["note"]
		out.append((r, cells))
	return out


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
	marker = os.path.join(checkout, ".hook-only", "imported")
	if os.path.isfile(marker):
		sys.exit(f"FAIL: {checkout} was already imported ({marker}); importing again would double its rows")
	rounds = subagentRounds(checkout)
	for r in rounds:
		cmdAdd(path, [date, topic, branch, klass, r["agent"], r["round"], "", "", r["estMin"], r["actMin"],
			r["estUsd"], r["actUsd"], r["note"]])
	allRuns = workerRuns(checkout)
	for r, c in allRuns:
		agent = r.get("mode") if r.get("mode") in (MERGE, FIX) else "worker"
		cmdAdd(path, [date, topic, branch, klass, agent, str(r["run"]), "", "", c["estMin"], c["actMin"],
			c["estUsd"], c["actUsd"], c["note"]])
	runs = [(r, c) for r, c in allRuns if r.get("mode") not in (MERGE, FIX)]
	valid = [(r, c) for r, c in runs if not r["invalid"]]
	if runs:
		def total(key):
			values = [c[key] for _, c in valid]
			return f"{sum(float(v) for v in values):.2f}" if values and all(values) else ""
		note = f"{len(valid)} of {len(runs)} worker runs valid"
		if valid and all(RAW_MARK in c["note"] for _, c in valid):
			note += f"; {RAW_MARK}"
		if not valid:
			note += "; invalid"
		cmdAdd(path, [date, topic, branch, klass, "branch", "-", "", "", total("estMin"), total("actMin"),
			total("estUsd"), total("actUsd"), note])
	merges = len(allRuns) - len(runs)
	try:
		with open(marker, "w", encoding="utf-8") as f:
			f.write(f"{date} {topic}\n")
	except OSError as error:
		print(f"note: import marker not written ({error})", file=sys.stderr)
	print(f"OK: imported {len(rounds)} subagent rounds, {len(runs)} worker runs"
		+ (f", {merges} merge or fix runs" if merges else "") + (", 1 branch row" if runs else ""))


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

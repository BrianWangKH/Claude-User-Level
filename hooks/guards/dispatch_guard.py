"""PreToolUse(Agent|Task|SendMessage) [main session]: budgets travel in the dispatch prompt.

Main session only (a call with agent_id comes from a subagent and is ignored).

Every dispatch or resume of a budgeted agent type (home.json budget "fallback_minutes") must carry one line
  Budget: <minutes> min, $<usd>
(usd optional; the estimate of the work: subagent_budget.py stops the agent at budget_headroom_factor x it. A
trailing `, full` or `, checkpoint` from the old two-stage rule is still accepted and means nothing).
The orchestrator normally writes only its raw estimate and the hook adds the Budget line (so
the orchestrator spends no request on `cost-history.py estimate agent` and keeps its output out of its
context):
  Estimate: <minutes> min, $<usd>     new work: the agent's history model (cost-history.py, the checkout's
                                      .claude-cost/history.tsv) turns it into the Budget line; the Estimate
                                      line stays, so the history records the raw estimate
  Remaining: <minutes> min, $<usd>    a resume after CHECKPOINT: the same model, never below the raw
                                      estimate (`estimate agent --resume`); replaced, nothing recorded
The hook inserts the Budget line and the budget-rules line (cost-history.py RULE_LINE) before it through
updatedInput. A prompt that already has a Budget line is kept as written. Without any of the three lines
the call is denied with the format; a missing history copy is denied too (a headless worker ends with
STATUS: blocked), and so is any error while modelling, so a dispatch never goes out without a budget.
The budget goes to <checkout>/.hook-only/budget/pending-<key>.json (key =
the SendMessage target, else the agent type), which subagent_budget.py claims at SubagentStart. The
SubagentStart input carries no prompt, hence the hand-over file.
SendMessage targets are resolved to an agent type through .hook-only/budget/events.jsonl (the
SubagentStart lines subagent_budget.py logs); an unknown target is not checked.

Headless worker (CLAUDE_WORKER_MINUTES set): the dispatch must also fit what is left of the run:
minutes + the gate wait + worker_dispatch_margin_minutes within the minutes left (from the start of the newest
started run, hooks/lib/runstate.py current: its record's process.started_at, else its start-<n> file), usd + worker_dispatch_margin_usd within the newest budget_usd attachment's
`remaining` (session transcript, read through usage.trackedAttachment, reader "dispatch": each dispatch
parses only the lines added since the previous one).
Otherwise the call is denied with the instruction to end the run with STATUS: blocked, and
the hook writes .hook-only/runs/auto-resume-<run>.json: the minutes and USD a resume needs for this
dispatch (its budget, the gate wait, the wrap-up margins and worker_resume_startup_minutes/_usd). The
run stopped only for room, so the run's wrap-up (run-steps.py, its next step) offers that record to worker-control.py
auto, which starts
one bounded automatic resume (home.json budget worker_auto_resume_*). The record is hook-written:
agents can't write .hook-only, so a model can't ask for an automatic resume on its own. A later dispatch
that fits removes the record: the run went on, so a later stop has another cause.
The gate wait: a code-reviewer round (not a `Merge review:` spawn) waits for the mutation scan and a
developer round for crap-gate's coverage run before its report goes back, outside its own budget
(budget_state.excludeWait) but inside the run's minutes; cost-history.py checkoutWait gives the longest of
the last 5 recorded waits of that agent (0 without quality gates). The budget itself is the estimate,
not the limit: the headroom above it stays for overruns.

code-reviewer spawn (Agent): the prompt is replaced with the fixed plan-blind review prompt plus the
Budget line, so nothing from the plan reaches the reviewer. A prompt with a line starting
"Merge review:" (feature-flow merge.md step 7, after the merge commit) is kept as written only while HEAD
is a merge commit (otherwise it gets the fixed prompt like any review: a merge review the orchestrator
words itself must not stand in for the plan-blind one), leaves
.hook-only/agent-guard/merge-review-pending so agent-guard records that verdict as kind "merge", and
.hook-only/hook-read-only/mutation/merge-review-pending so mutation.py skips its automatic scan for
that reviewer (the SubagentStart input carries no prompt).

The run record (hooks/lib/runstate.py, migration step 2: dual write): writing auto-resume-<run>.json also writes the
record's dispatch_stop, removing it clears that, through runstate.dualWrite (a record write that fails is
reported once and changes nothing here).

It is the last step of hook-entry.py's `pre` entry: it runs, and writes pending-<key>.json, auto-resume-<run>.json
and merge-review-pending, only when no earlier check denied the call.

Reads: the hook input, budget, cost history (cost-history.py), runstate, the session transcript.
Writes: budget/pending-<key>.json, runs/auto-resume-<run>.json (+ run record), agent-guard/ and mutation/
merge-review-pending.
Output: check(data) returns the PreToolUse deny, an allow with updatedInput, or None.
Fails: closed — an error while modelling the estimate denies with it (its own catch); any other exception
denies the call too (hook-entry.py).
"""
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import hookio  # noqa: E402
import hookonly  # noqa: E402
import runstate  # noqa: E402
import usage  # noqa: E402

BUDGET_LINE = re.compile(
	r"^\s*Budget:\s*(\d+(?:\.\d+)?)\s*min(?:\s*,\s*\$(\d+(?:\.\d+)?))?(?:\s*,\s*(full|checkpoint))?\s*$",
	re.MULTILINE | re.IGNORECASE)
# The raw estimate (before the estimate model), recorded for cost-history.py's model; optional.
ESTIMATE_LINE = re.compile(r"^\s*Estimate:\s*(\d+(?:\.\d+)?)\s*min\s*,\s*\$(\d+(?:\.\d+)?)\s*$",
	re.MULTILINE | re.IGNORECASE)
REMAINING_LINE = re.compile(r"^\s*Remaining:\s*(\d+(?:\.\d+)?)\s*min\s*,\s*\$(\d+(?:\.\d+)?)\s*$",
	re.MULTILINE | re.IGNORECASE)
FORMAT = "Estimate: <minutes> min, $<usd>"
COST_HISTORY = os.path.expanduser("~/.claude/skills/repo-plan/cost-history.py")
GATED = ("code-reviewer", "developer")
BASE_ORIGIN = ("the merge-base of `HEAD` with origin's default branch (`git merge-base origin/master HEAD`, or "
	"`origin/main` if there is no `origin/master`; never the local branch, which the main checkout doesn't pull)")
BASE_LOCAL = ("the merge-base of `HEAD` with the local default branch (`git merge-base main HEAD`, or `master` "
	"if there is no `main`): this repo works in local mode, where the local branch is the source of truth")
REVIEW_PROMPT = (
	"Review the current branch. Find the base yourself: {base}. Review everything from that base to the "
	"working tree, following your agent instructions. End with your full report.\n\n"
	"{budget}\nFollow the budget rules in `agent-common`: aim to finish within the budget, `CHECKPOINT`"
	" when the rest clearly won't fit, `BLOCKED` early.")  # same text as cost-history.py RULE_LINE
TIME_MESSAGE = ("Not enough worker time for this dispatch: {need:g} min for the agent, {wait:g} min for its "
	"quality gate's wait, {margin:g} min to wrap up, {left:.0f} of {limit:g} min left. Don't dispatch. Write the planned dispatch and its estimate "
	"into plans/<topic>.md, then end your run with a last message starting with `STATUS: blocked`: what is "
	"done, the next dispatch and its estimate. The hook recorded this dispatch for one automatic resume; "
	"write the plan entry anyway.")
USD_MESSAGE = ("Not enough worker budget for this dispatch: ${need:.2f} for the agent plus ${margin:.2f} "
	"to wrap up, ${left:.2f} left in this run. Don't dispatch. Write the planned dispatch and its estimate into "
	"plans/<topic>.md, then end your run with a last message starting with `STATUS: blocked`: what is done, "
	"the next dispatch and its estimate. The hook recorded this dispatch for one automatic resume; write "
	"the plan entry anyway.")


def deny(reason):
	raise hookio.Answer(hookio.refuse("PreToolUse", f"dispatch-guard: {reason}"))


def parseBudget(text):
	"""({minutes, usd, phase}, the line as written), or (None, None) without a Budget line."""
	match = BUDGET_LINE.search(text or "")
	if not match:
		return None, None
	minutes, usd, phase = match.groups()
	budget = {"minutes": float(minutes), "usd": float(usd) if usd else None,
		"phase": (phase or "checkpoint").lower()}
	estimate = ESTIMATE_LINE.search(text or "")
	if estimate:
		budget["raw_minutes"], budget["raw_usd"] = float(estimate.group(1)), float(estimate.group(2))
	return budget, match.group(0).strip()


def loadCostHistory():
	import importlib.util
	spec = importlib.util.spec_from_file_location("cost_history", COST_HISTORY)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def modelBudget(root, agentType, text):
	"""The prompt with the Budget and budget-rules lines added from an Estimate or Remaining line (module
	doc); None when it already has a Budget line or has neither. Denies when the history copy is missing
	or the model fails."""
	if not text or BUDGET_LINE.search(text):
		return None
	match, resume = ESTIMATE_LINE.search(text), False
	if not match:
		match, resume = REMAINING_LINE.search(text), True
	if not match:
		return None
	try:
		ch = loadCostHistory()
		path = os.path.join(root, ch.HISTORY)
		if not os.path.isfile(path):
			deny(f"no cost history at {path}, so the estimate can't be turned into a budget. In a worktree it "
				"is the copy new-worktree.sh makes: a headless worker ends with `STATUS: blocked`, otherwise ask "
				"the planning session to run `cost-history.py snapshot <worktree>`.")
		rows = [r for r in ch.newestFirst(ch.load(path)) if ch.isValid(r) and r["agent"] == agentType]
		minutes, usd = float(match.group(1)), float(match.group(2))
		newUsd, newMin = ch.applyModel(usd, minutes, ch.model(rows), resume)
		lines = f"Budget: {newMin} min, ${newUsd:g}\n{ch.RULE_LINE}"
	except hookio.Answer:
		raise
	except Exception as error:
		deny(f"the estimate couldn't be modelled ({error}); write the lines `cost-history.py estimate agent "
			f"{agentType} <usd> <minutes>` prints instead.")
	start, end = match.span()
	if resume:
		return text[:start] + lines + text[end:]
	return text[:start] + lines + "\n" + text[start:]


def agentTypeOf(budgetDir, target):
	"""Agent type of a SendMessage target, from the SubagentStart lines in events.jsonl."""
	agentType = None
	try:
		with open(os.path.join(budgetDir, "events.jsonl"), encoding="utf-8") as f:
			for line in f:
				try:
					event = json.loads(line)
				except ValueError:
					continue
				if event.get("event") == "SubagentStart" and event.get("agent_id") == target:
					agentType = event.get("agent_type")
	except OSError:
		pass
	return agentType


def usdRemaining(root, transcript):
	"""`remaining` of the newest budget_usd attachment in the session transcript, or None."""
	if not transcript or not os.path.isfile(transcript):
		return None
	attachment = usage.trackedAttachment(root, "dispatch", "main", transcript, "budget_usd", "remaining", True)
	return float(attachment["remaining"]) if attachment is not None else None


def gateWait(root, agentType, mergeReview):
	"""Minutes a gate will hold this dispatch's agent at hand-back (see the module doc); 0 when none."""
	if agentType not in GATED or (agentType == "code-reviewer" and mergeReview):
		return 0.0
	try:
		return float(loadCostHistory().checkoutWait(root, agentType))
	except Exception as error:
		print(f"dispatch-guard: gate wait unknown, not reserved ({error})", file=sys.stderr)
		return 0.0


def recordAutoResume(root, budget, wait, config, agentType):
	"""Write .hook-only/runs/auto-resume-<run>.json for the newest started run (runstate.current): what a resume
	needs (module doc)."""
	runsDir = os.path.join(root, hookonly.NAME, "runs")
	found = runstate.current(root)
	if found is None:
		return
	run = found["run"]
	minutes = budget["minutes"] + wait + float(config["worker_dispatch_margin_minutes"]) \
		+ float(config["worker_resume_startup_minutes"])
	usd = None
	if budget["usd"]:
		usd = round(budget["usd"] + float(config["worker_dispatch_margin_usd"])
			+ float(config["worker_resume_startup_usd"]), 2)
	with open(os.path.join(runsDir, f"auto-resume-{run}.json"), "w", encoding="utf-8") as f:
		json.dump({"minutes": math.ceil(minutes), "usd": usd, "agent_type": agentType, "at": time.time()}, f)
	runstate.dualWrite(root, "dispatch-guard", runstate.recordDispatchStop, run, math.ceil(minutes), usd)


def clearAutoResume(root):
	"""A dispatch that fits: drop the newest started run's auto-resume record (module doc)."""
	runsDir = os.path.join(root, hookonly.NAME, "runs")
	found = runstate.current(root)
	if found is not None:
		try:
			os.remove(os.path.join(runsDir, f"auto-resume-{found['run']}.json"))
		except FileNotFoundError:
			pass
		else:
			runstate.dualWrite(root, "dispatch-guard", runstate.clearDispatchStop, found["run"])


def headIsMerge(root):
	import subprocess
	result = subprocess.run(["git", "-C", root, "rev-list", "--parents", "-n", "1", "HEAD"], capture_output=True,
		text=True)
	return result.returncode == 0 and len(result.stdout.split()) > 2


def checkWorkerRoom(data, root, budget, config, agentType, mergeReview):
	raw = os.environ.get("CLAUDE_WORKER_MINUTES")
	if not raw or float(raw) <= 0:
		return
	limit = float(raw)
	started = runstate.startedSeconds(root)
	wait = gateWait(root, agentType, mergeReview)
	if started is not None:
		left = limit - (time.time() - started) / 60
		margin = float(config["worker_dispatch_margin_minutes"])
		if budget["minutes"] + wait + margin > left:
			recordAutoResume(root, budget, wait, config, agentType)
			deny(TIME_MESSAGE.format(need=budget["minutes"], wait=wait, margin=margin, left=left, limit=limit))
	if budget["usd"]:
		left = usdRemaining(root, data.get("transcript_path"))
		margin = float(config["worker_dispatch_margin_usd"])
		if left is not None and budget["usd"] + margin > left:
			recordAutoResume(root, budget, wait, config, agentType)
			deny(USD_MESSAGE.format(need=budget["usd"], margin=margin, left=left))
	clearAutoResume(root)


def check(data):
	if data.get("agent_id"):
		return None
	tool = data.get("tool_name")
	toolInput = data.get("tool_input") or {}
	root = hookonly.projectRoot(data)
	budgetDir = hookonly.stateDir(root, "budget")
	config = homeconfig.load("budget")
	budgeted = config["fallback_minutes"]

	if tool == "SendMessage":
		target = str(toolInput.get("to") or "")
		agentType, key, text = agentTypeOf(budgetDir, target), target, toolInput.get("message")
	else:
		agentType = toolInput.get("subagent_type") or ""
		key, text = agentType, toolInput.get("prompt")
	modelled = modelBudget(root, agentType, text) if agentType in budgeted else None
	if modelled is not None:
		text = modelled
	budget, line = parseBudget(text)
	if budget is None:
		if agentType in budgeted:
			deny(f"a dispatch to {agentType} needs one line `{FORMAT}` (new work: your raw estimate) or "
				"`Remaining: <minutes> min, $<usd>` (a resume after CHECKPOINT) in its prompt; the hook turns it "
				"into the Budget line. Add it and call again.")
		return None

	mergeReview = re.search(r"^\s*Merge review:", text or "", re.MULTILINE) is not None and headIsMerge(root)
	checkWorkerRoom(data, root, budget, config, agentType, mergeReview and tool != "SendMessage")

	os.makedirs(budgetDir, exist_ok=True)
	with open(os.path.join(budgetDir, f"pending-{key}.json"), "w", encoding="utf-8") as f:
		json.dump({**budget, "agent_type": agentType, "written": time.time()}, f)

	if tool != "SendMessage" and agentType == "code-reviewer" and mergeReview:
		guardDir = hookonly.stateDir(root, "agent-guard")
		os.makedirs(guardDir, exist_ok=True)
		with open(os.path.join(guardDir, "merge-review-pending"), "w", encoding="utf-8") as f:
			f.write(str(time.time()))
		mutationDir = hookonly.stateDir(root, "mutation", strict=True)
		os.makedirs(mutationDir, exist_ok=True)
		with open(os.path.join(mutationDir, "merge-review-pending"), "w", encoding="utf-8") as f:
			f.write(str(time.time()))
	if tool != "SendMessage" and agentType == "code-reviewer" and not mergeReview:
		return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
			"permissionDecision": "allow",
			"permissionDecisionReason": "dispatch-guard: fixed plan-blind review prompt",
			"updatedInput": {**toolInput, "prompt": REVIEW_PROMPT.format(budget=line,
				base=BASE_LOCAL if homeconfig.flowMode(root) == "local" else BASE_ORIGIN)}}}
	elif modelled is not None:
		field = "message" if tool == "SendMessage" else "prompt"
		return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
			"permissionDecision": "allow",
			"permissionDecisionReason": f"dispatch-guard: added `{line}` from the estimate model",
			"updatedInput": {**toolInput, field: text}}}
	return None


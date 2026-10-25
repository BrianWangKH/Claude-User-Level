#!/usr/bin/env python3
"""PreToolUse hook (matcher "Agent|Task|SendMessage"): budgets travel in the dispatch prompt.

Main session only (a call with agent_id comes from a subagent and is ignored).

Every dispatch or resume of a budgeted agent type (budget-defaults.json "minutes") must carry one line
  Budget: <minutes> min, $<usd>[, full]
(usd optional; "full" lets the agent run to 100% without the 80% CHECKPOINT stop). Without it the call
is denied with the format. The budget goes to <checkout>/.hook-only/budget/pending-<key>.json (key =
the SendMessage target, else the agent type), which subagent-budget.py claims at SubagentStart. The
SubagentStart input carries no prompt, hence the hand-over file.
SendMessage targets are resolved to an agent type through .hook-only/budget/events.jsonl (the
SubagentStart lines subagent-budget.py logs); an unknown target is not checked.

Headless worker (CLAUDE_WORKER_MINUTES set): the dispatch must also fit what is left of the run:
minutes + worker_dispatch_margin_minutes within the minutes left (from the newest .hook-only/runs/start-<n>),
usd + worker_dispatch_margin_usd within the newest budget_usd attachment's `remaining`. Otherwise the
call is denied with the instruction to end the run with STATUS: blocked.

code-reviewer spawn (Agent): the prompt is replaced with the fixed plan-blind review prompt plus the
Budget line, so nothing from the plan reaches the reviewer. A prompt with a line starting
"Merge review:" (feature-flow merge.md) is kept as written.

Fails open on its own errors (stderr): subagent-budget.py then applies the default time budget.
"""
import json
import os
import re
import sys
import time
from datetime import datetime

import hookonly

DEFAULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget-defaults.json")
BUDGET_LINE = re.compile(
	r"^\s*Budget:\s*(\d+(?:\.\d+)?)\s*min(?:\s*,\s*\$(\d+(?:\.\d+)?))?(?:\s*,\s*(full|checkpoint))?\s*$",
	re.MULTILINE | re.IGNORECASE)
START_FILE = re.compile(r"^start-(\d+)$")
FORMAT = "Budget: <minutes> min, $<usd>   (optional third part: `, full` to skip the 80% checkpoint)"
REVIEW_PROMPT = (
	"Review the current branch. Find the base yourself: the merge-base of `HEAD` with origin's default "
	"branch (`git merge-base origin/master HEAD`, or `origin/main` if there is no `origin/master`; never "
	"the local branch, which the main checkout doesn't pull). Review everything from that base to the "
	"working tree, following your agent instructions. End with your full report.\n\n"
	"{budget}\nFollow the budget rules in `agent-common`: `CHECKPOINT` at 80%, `BLOCKED` early.")
TIME_MESSAGE = ("Not enough worker time for this dispatch: {need:g} min for the agent plus {margin:g} min "
	"to wrap up, {left:.0f} of {limit:g} min left. Don't dispatch. Write the planned dispatch and its estimate "
	"into plans/<topic>.md, then end your run with a last message starting with `STATUS: blocked`: what is "
	"done, the next dispatch and its estimate.")
USD_MESSAGE = ("Not enough worker budget for this dispatch: ${need:.2f} for the agent plus ${margin:.2f} "
	"to wrap up, ${left:.2f} left in this run. Don't dispatch. Write the planned dispatch and its estimate into "
	"plans/<topic>.md, then end your run with a last message starting with `STATUS: blocked`: what is done, "
	"the next dispatch and its estimate.")


def deny(reason):
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
		"permissionDecision": "deny", "permissionDecisionReason": f"dispatch-guard: {reason}"}}))
	sys.exit(0)


def parseBudget(text):
	"""({minutes, usd, phase}, the line as written), or (None, None) without a Budget line."""
	match = BUDGET_LINE.search(text or "")
	if not match:
		return None, None
	minutes, usd, phase = match.groups()
	budget = {"minutes": float(minutes), "usd": float(usd) if usd else None,
		"phase": (phase or "checkpoint").lower()}
	return budget, match.group(0).strip()


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


def runStart(runsDir):
	"""Start time (epoch seconds) from the highest-numbered start-<n>, or None if there is none."""
	if not os.path.isdir(runsDir):
		return None
	numbered = [(int(m.group(1)), name) for name in os.listdir(runsDir) if (m := START_FILE.match(name))]
	if not numbered:
		return None
	with open(os.path.join(runsDir, max(numbered)[1]), encoding="utf-8") as f:
		return datetime.fromisoformat(f.read().strip()).timestamp()


def usdRemaining(transcript):
	"""`remaining` of the newest budget_usd attachment in the session transcript, or None."""
	if not transcript or not os.path.isfile(transcript):
		return None
	remaining = None
	with open(transcript, encoding="utf-8") as f:
		for line in f:
			if '"budget_usd"' not in line:
				continue
			try:
				attachment = json.loads(line).get("attachment") or {}
			except ValueError:
				continue
			if attachment.get("type") == "budget_usd" and attachment.get("remaining") is not None:
				remaining = float(attachment["remaining"])
	return remaining


def checkWorkerRoom(data, root, budget, config):
	raw = os.environ.get("CLAUDE_WORKER_MINUTES")
	if not raw or float(raw) <= 0:
		return
	limit = float(raw)
	started = runStart(os.path.join(root, hookonly.NAME, "runs"))
	if started is not None:
		left = limit - (time.time() - started) / 60
		margin = float(config.get("worker_dispatch_margin_minutes", 0))
		if budget["minutes"] + margin > left:
			deny(TIME_MESSAGE.format(need=budget["minutes"], margin=margin, left=left, limit=limit))
	if budget["usd"]:
		left = usdRemaining(data.get("transcript_path"))
		margin = float(config.get("worker_dispatch_margin_usd", 0))
		if left is not None and budget["usd"] + margin > left:
			deny(USD_MESSAGE.format(need=budget["usd"], margin=margin, left=left))


def main():
	data = json.load(sys.stdin)
	if data.get("agent_id"):
		sys.exit(0)
	tool = data.get("tool_name")
	toolInput = data.get("tool_input") or {}
	root = hookonly.projectRoot(data)
	budgetDir = hookonly.stateDir(root, "budget")
	with open(DEFAULTS_FILE, encoding="utf-8") as f:
		config = json.load(f)
	budgeted = config["minutes"]

	if tool == "SendMessage":
		target = str(toolInput.get("to") or "")
		agentType, key, text = agentTypeOf(budgetDir, target), target, toolInput.get("message")
	else:
		agentType = toolInput.get("subagent_type") or ""
		key, text = agentType, toolInput.get("prompt")
	budget, line = parseBudget(text)
	if budget is None:
		if agentType in budgeted:
			deny(f"a dispatch to {agentType} needs one line `{FORMAT}` in its prompt (the budget hook "
				"enforces it and the agent reads it). Add the line with your corrected estimate and call again.")
		sys.exit(0)

	checkWorkerRoom(data, root, budget, config)

	os.makedirs(budgetDir, exist_ok=True)
	with open(os.path.join(budgetDir, f"pending-{key}.json"), "w", encoding="utf-8") as f:
		json.dump({**budget, "agent_type": agentType, "written": time.time()}, f)

	if tool != "SendMessage" and agentType == "code-reviewer" \
			and not re.search(r"^\s*Merge review:", text or "", re.MULTILINE):
		print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
			"permissionDecision": "allow",
			"permissionDecisionReason": "dispatch-guard: fixed plan-blind review prompt",
			"updatedInput": {**toolInput, "prompt": REVIEW_PROMPT.format(budget=line)}}}))
	sys.exit(0)


if __name__ == "__main__":
	try:
		main()
	except SystemExit:
		raise
	except Exception as error:
		print(f"dispatch-guard error (dispatch not checked): {error}", file=sys.stderr)
		sys.exit(0)

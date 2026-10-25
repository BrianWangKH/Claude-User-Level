"""Budget guard for flow subagents (SubagentStart + PreToolUse).

SubagentStart: claim the budget dispatch-guard.py parsed from the dispatch prompt's `Budget:` line:
  <checkout>/.hook-only/budget/pending-<agent_id>.json (a SendMessage resume), else
  pending-<agent_type>.json ({"minutes": N, "usd": U, "phase": "checkpoint" | "full"}; usd optional),
  delete it so the next dispatch must bring a fresh one, and store the budget with the start time
  under .hook-only/budget/<agent_id>.json (agents can read it but not edit it; see hookonly.py).
  No pending budget: fall back to the time-only default in budget-defaults.json. A resume fires
  SubagentStart again, so every round is claimed and timed anew.
PreToolUse (subagents only): deny the tool call once the round has used
  - 80% of its time, USD or tokens while phase is "checkpoint" -> stop and report CHECKPOINT
  - 100% in any phase -> stop and report
  SubagentHandback (the tool a background subagent returns its report with) is always allowed: it
  is the way out, and denying it only makes the agent retry and burn requests (seen 2026-10-24: two
  denied handbacks, then the agent ended its turn, SubagentStop deleted the state, and the third
  handback passed only through the no-state branch below).
  No state for the agent (seen in practice: a resumed round's tool calls all got "no budget state";
  likely a late SubagentStop from the previous round deleting the new round's state): start a
  time-only default round instead of denying, so the round can still work and report.
Every event is appended to .hook-only/budget/events.jsonl to diagnose such cases.

SubagentStop: delete that agent's state (a resume claims a new one). SubagentStart also removes
  state files older than 7 days, left behind when a session died before SubagentStop.

Tokens are weighted by price relative to plain input (tokenweights.py): cache reads cost a tenth, so
a resumed agent re-reading its context doesn't burn its budget in a few requests. subagent-cost.py
records the same weighted count, so estimates, budgets and actuals share one unit.
USD, the preferred unit, is priced per model and cache TTL by prices.py (prices.json), the same
pricing subagent-cost.py records as `usd`; requests of a model missing from prices.json are not
counted and the deny message says how many.

Only agent types listed in budget-defaults.json are budgeted. Fail-closed: any error while checking
a budgeted agent denies the call with the error as the reason.
Token use is summed from the subagent's transcript, which lags by about one request.
"""
import json
import os
import sys
import time

import hookonly
import prices
import tokenweights

DEFAULTS_FILE = tokenweights.DEFAULTS_FILE
TOKEN_WEIGHTS = tokenweights.BASE
STALE_SECONDS = 7 * 24 * 3600
ALWAYS_ALLOWED = {"SubagentHandback"}


def logEvent(data, note="", extra=None):
	"""Append one line per event; never fails the hook. `extra` adds fields (the claimed budget)."""
	try:
		stateDir = stateDirFor(data)
		os.makedirs(stateDir, exist_ok=True)
		stateFile = os.path.join(stateDir, f"{data.get('agent_id')}.json")
		with open(os.path.join(stateDir, "events.jsonl"), "a", encoding="utf-8") as f:
			f.write(json.dumps({
				"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": data.get("hook_event_name"),
				"agent_id": data.get("agent_id"), "agent_type": data.get("agent_type"),
				"tool": data.get("tool_name"), "state": os.path.isfile(stateFile), "note": note,
				**(extra or {}),
			}) + "\n")
	except Exception:
		pass


def deny(reason, data=None):
	if data is not None:
		logEvent(data, "deny: " + reason[:120])
	print(json.dumps({
		"hookSpecificOutput": {
			"hookEventName": "PreToolUse",
			"permissionDecision": "deny",
			"permissionDecisionReason": reason,
		}
	}))
	sys.exit(0)


def loadConfig():
	with open(DEFAULTS_FILE, encoding="utf-8") as f:
		config = json.load(f)
	return config["minutes"], {**TOKEN_WEIGHTS, **config.get("token_weights", {})}


def stateDirFor(data):
	return hookonly.stateDir(hookonly.projectRoot(data), "budget")


def subagentTranscript(sessionTranscript, agentId):
	"""~/.claude/projects/<project>/<session>.jsonl -> .../<session>/subagents/agent-<id>.jsonl"""
	if not sessionTranscript:
		return None
	return os.path.join(os.path.splitext(sessionTranscript)[0], "subagents", f"agent-{agentId}.jsonl")


def tokensUsed(path, weights):
	requests = {}
	with open(path, encoding="utf-8") as f:
		for line in f:
			try:
				entry = json.loads(line)
			except ValueError:
				continue
			if entry.get("type") != "assistant":
				continue
			message = entry.get("message") or {}
			usage = message.get("usage")
			key = message.get("id") or entry.get("requestId") or entry.get("uuid")
			if usage and key:
				requests[key] = usage
	return sum(tokenweights.weigh(u, weights) for u in requests.values())


def usdUsed(path, table):
	"""(USD of the subagent's requests so far, number of requests of an unpriced model)."""
	requests = {}
	with open(path, encoding="utf-8") as f:
		for line in f:
			try:
				entry = json.loads(line)
			except ValueError:
				continue
			if entry.get("type") != "assistant":
				continue
			message = entry.get("message") or {}
			usage = message.get("usage")
			key = message.get("id") or entry.get("requestId") or entry.get("uuid")
			if usage and key:
				requests[key] = (usage, message.get("model"))
	costs = [prices.requestUsd(u, m, table) for u, m in requests.values()]
	return sum(c for c in costs if c is not None), sum(1 for c in costs if c is None)


def writeState(data, budget, weights):
	agentId = data.get("agent_id") or ""
	tokensAtStart = usdAtStart = 0
	transcript = subagentTranscript(data.get("transcript_path"), agentId)
	if transcript and os.path.isfile(transcript):
		if budget["tokens"]:
			tokensAtStart = tokensUsed(transcript, weights)
		if budget.get("usd"):
			usdAtStart = usdUsed(transcript, prices.load())[0]
	budget.update({"agent_type": data.get("agent_type") or "", "started": time.time(),
		"tokens_at_start": tokensAtStart, "usd_at_start": usdAtStart})
	stateDir = stateDirFor(data)
	os.makedirs(stateDir, exist_ok=True)
	with open(os.path.join(stateDir, f"{agentId}.json"), "w", encoding="utf-8") as f:
		json.dump(budget, f)


def onStart(data, budgeted, weights):
	agentType = data.get("agent_type") or ""
	agentId = data.get("agent_id") or ""
	if agentType not in budgeted or not agentId:
		return
	budget = {"minutes": budgeted[agentType], "tokens": None, "phase": "checkpoint", "source": "default"}
	stateDir = stateDirFor(data)
	for key in (agentId, agentType):
		budgetFile = os.path.join(stateDir, f"pending-{key}.json")
		if not os.path.isfile(budgetFile):
			continue
		with open(budgetFile, encoding="utf-8") as f:
			given = json.load(f)
		budget = {
			"minutes": float(given["minutes"]),
			"tokens": None,
			"usd": float(given["usd"]) if given.get("usd") else None,
			"phase": given.get("phase") or "checkpoint",
			"source": os.path.basename(budgetFile),
		}
		os.remove(budgetFile)
		break
	# cost-history.py rows reads these fields: the estimate side of the Cost table.
	logEvent(data, "budget: " + budget["source"], {"budget_minutes": budget["minutes"],
		"budget_usd": budget.get("usd"), "budget_source": budget["source"]})
	writeState(data, budget, weights)


def onPreToolUse(data, budgeted, weights):
	agentType = data.get("agent_type") or ""
	agentId = data.get("agent_id") or ""
	if agentType not in budgeted or not agentId:
		return
	stateFile = os.path.join(stateDirFor(data), f"{agentId}.json")
	if not os.path.isfile(stateFile):
		writeState(data, {"minutes": budgeted[agentType], "tokens": None, "phase": "checkpoint",
						  "source": "default (no state at PreToolUse)"}, weights)
		logEvent(data, "no-state: started default round")
		return
	with open(stateFile, encoding="utf-8") as f:
		budget = json.load(f)

	minutesUsed = (time.time() - budget["started"]) / 60
	ratio = minutesUsed / budget["minutes"]
	detail = f"{minutesUsed:.2f} of {budget['minutes']:g} min"
	if budget.get("tokens"):
		transcript = subagentTranscript(data.get("transcript_path"), agentId)
		if transcript and os.path.isfile(transcript):
			used = tokensUsed(transcript, weights) - int(budget.get("tokens_at_start") or 0)
			ratio = max(ratio, used / budget["tokens"])
			detail += f", {used} of {budget['tokens']} weighted tokens"
	if budget.get("usd"):
		transcript = subagentTranscript(data.get("transcript_path"), agentId)
		if transcript and os.path.isfile(transcript):
			spent, unpriced = usdUsed(transcript, prices.load())
			used = spent - float(budget.get("usd_at_start") or 0)
			ratio = max(ratio, used / budget["usd"])
			detail += f", ${used:.2f} of ${budget['usd']:g}"
			if unpriced:
				detail += f" ({unpriced} requests of a model missing from prices.json not counted)"

	if ratio >= 1:
		deny(f"Budget used up ({detail}). Stop now and report: status CHECKPOINT, what is done, what you tried, what remains, what finishing would cost.", data)
	if ratio >= 0.8 and budget.get("phase") != "full":
		deny(f"80% of the budget used ({detail}). Stop at a clean point and report CHECKPOINT: what is done, what you tried, your current hypothesis, what remains, what finishing would cost.", data)


def removeStaleStates(stateDir):
	if not os.path.isdir(stateDir):
		return
	cutoff = time.time() - STALE_SECONDS
	for name in os.listdir(stateDir):
		path = os.path.join(stateDir, name)
		try:
			if name.endswith(".json") and os.path.getmtime(path) < cutoff:
				os.remove(path)
		except OSError:
			pass


def onStop(data):
	agentId = data.get("agent_id") or ""
	if not agentId:
		return
	try:
		os.remove(os.path.join(stateDirFor(data), f"{agentId}.json"))
	except FileNotFoundError:
		pass


def writeErrorState(data, error):
	"""SubagentStart can't block, so leave a state that makes every tool call deny with the error."""
	agentId = data.get("agent_id") or ""
	if not agentId:
		return
	stateDir = stateDirFor(data)
	os.makedirs(stateDir, exist_ok=True)
	with open(os.path.join(stateDir, f"{agentId}.json"), "w", encoding="utf-8") as f:
		json.dump({"error": str(error)}, f)


def main():
	data = json.load(sys.stdin)
	event = data.get("hook_event_name")
	isSubagent = bool(data.get("agent_id"))
	if event in ("SubagentStart", "SubagentStop"):
		logEvent(data)
	if event == "SubagentStart":
		try:
			onStart(data, *loadConfig())
		except Exception as error:
			writeErrorState(data, error)
		removeStaleStates(stateDirFor(data))
	elif event == "SubagentStop":
		onStop(data)
	elif event == "PreToolUse" and isSubagent:
		if data.get("tool_name") in ALWAYS_ALLOWED:
			logEvent(data, "allow: " + data.get("tool_name"))
			return
		try:
			budgeted, weights = loadConfig()
		except Exception as error:
			deny(f"Budget guard can't read {DEFAULTS_FILE} ({error}). Stop and report BLOCKED with this message.", data)
		stateFile = os.path.join(stateDirFor(data), f"{data.get('agent_id')}.json")
		if (data.get("agent_type") or "") in budgeted and os.path.isfile(stateFile):
			with open(stateFile, encoding="utf-8") as f:
				if "error" in json.load(f):
					f.seek(0)
					deny(f"Budget guard failed at start ({json.load(f)['error']}). Stop and report BLOCKED with this message.", data)
		try:
			onPreToolUse(data, budgeted, weights)
		except SystemExit:
			raise
		except Exception as error:
			if (data.get("agent_type") or "") in budgeted:
				deny(f"Budget guard error ({error}). Stop and report BLOCKED with this message.", data)


if __name__ == "__main__":
	try:
		main()
	except SystemExit:
		raise
	except Exception as error:
		# Unreadable hook input: nothing to attribute it to, so don't block the main session.
		print(f"subagent-budget error: {error}", file=sys.stderr)
		sys.exit(0)

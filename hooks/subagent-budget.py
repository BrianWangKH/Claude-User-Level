"""Budget guard for flow subagents (SubagentStart + PreToolUse).

SubagentStart: claim the budget the orchestrator wrote to <repo>/plans/budget/<agent_type>.json
  ({"minutes": N, "tokens": M, "phase": "checkpoint" | "full"}; tokens and phase optional), delete
  that file so the next dispatch must write a fresh one, and store the budget with the start time
  under <checkout>/.hook-only/budget/<agent_id>.json (agents can read it but not edit it; see
  hookonly.py).
  No budget file: fall back to the time-only default in budget-defaults.json. A resume fires
  SubagentStart again, so every round is claimed and timed anew.
PreToolUse (subagents only): deny the tool call once the round has used
  - 80% of its time or tokens while phase is "checkpoint" -> stop and report CHECKPOINT
  - 100% in any phase -> stop and report
  Reporting is plain text, so a denied agent can still hand back.
  No state for the agent (seen in practice: a resumed round's tool calls all got "no budget state";
  likely a late SubagentStop from the previous round deleting the new round's state): start a
  time-only default round instead of denying, so the round can still work and report.
Every event is appended to .hook-only/budget/events.jsonl to diagnose such cases.

SubagentStop: delete that agent's state (a resume claims a new one). SubagentStart also removes
  state files older than 7 days, left behind when a session died before SubagentStop.

Tokens are weighted by price relative to plain input: cache reads cost a tenth, so a resumed agent
re-reading its context doesn't burn its budget in a few requests. Override the weights with
"token_weights" in budget-defaults.json.

Only agent types listed in budget-defaults.json are budgeted. Fail-closed: any error while checking
a budgeted agent denies the call with the error as the reason.
Token use is summed from the subagent's transcript, which lags by about one request.
"""
import json
import os
import sys
import time

import hookonly

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_FILE = os.environ.get("CLAUDE_BUDGET_DEFAULTS") or os.path.join(HERE, "budget-defaults.json")
TOKEN_WEIGHTS = {
	"input_tokens": 1.0,
	"cache_creation_input_tokens": 1.25,
	"cache_read_input_tokens": 0.1,
	"output_tokens": 1.0,
}
STALE_SECONDS = 7 * 24 * 3600


def logEvent(data, note=""):
	"""Append one line per event; never fails the hook."""
	try:
		stateDir = stateDirFor(data)
		os.makedirs(stateDir, exist_ok=True)
		stateFile = os.path.join(stateDir, f"{data.get('agent_id')}.json")
		with open(os.path.join(stateDir, "events.jsonl"), "a", encoding="utf-8") as f:
			f.write(json.dumps({
				"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": data.get("hook_event_name"),
				"agent_id": data.get("agent_id"), "agent_type": data.get("agent_type"),
				"tool": data.get("tool_name"), "state": os.path.isfile(stateFile), "note": note,
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
	return round(sum(int(u.get(k) or 0) * w for u in requests.values() for k, w in weights.items()))


def writeState(data, budget, weights):
	agentId = data.get("agent_id") or ""
	tokensAtStart = 0
	transcript = subagentTranscript(data.get("transcript_path"), agentId)
	if budget["tokens"] and transcript and os.path.isfile(transcript):
		tokensAtStart = tokensUsed(transcript, weights)
	budget.update({"agent_type": data.get("agent_type") or "", "started": time.time(), "tokens_at_start": tokensAtStart})
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
	root = hookonly.projectRoot(data)
	budgetFile = os.path.join(root, "plans", "budget", f"{agentType}.json")
	if os.path.isfile(budgetFile):
		with open(budgetFile, encoding="utf-8") as f:
			given = json.load(f)
		budget = {
			"minutes": float(given["minutes"]),
			"tokens": int(given["tokens"]) if given.get("tokens") else None,
			"phase": given.get("phase") or "checkpoint",
			"source": budgetFile,
		}
		os.remove(budgetFile)
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

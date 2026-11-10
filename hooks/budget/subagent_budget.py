"""SubagentStart, PreToolUse(*), SubagentStop [budgeted flow subagents]: the budget guard of a subagent round.

SubagentStart: claim the budget dispatch_guard.py parsed from the dispatch prompt's `Budget:` line:
  <checkout>/.hook-only/budget/pending-<agent_id>.json (a SendMessage resume), else
  pending-<agent_type>.json ({"minutes": N, "usd": U, "phase": "checkpoint" | "full"}; usd optional),
  delete it so the next dispatch must bring a fresh one, and store the budget with the start time
  under .hook-only/budget/<agent_id>.json (agents can read it but not edit it; see hookonly.py).
  No pending budget: fall back to the time-only default in home.json budget. A resume fires
  SubagentStart again, so every round is claimed and timed anew.
PreToolUse (subagents only): deny the tool call once the round has used budget_headroom_factor (home.json budget)
  times its time or USD -> stop and report CHECKPOINT. The budget is the estimate of the work; the
  agent aims to finish within it and the headroom lets a modest overrun finish, so the history records
  the real need instead of a round cut at 80% of a mean estimate (17-18 of 33 replayed
  rounds stopped that way). The old "phase" (checkpoint / full) is stored but no longer read.
  SubagentHandback (the tool a background subagent returns its report with) is always allowed: it
  is the way out, and denying it only makes the agent retry and burn requests (seen: two
  denied handbacks, then the agent ended its turn, SubagentStop deleted the state, and the third
  handback passed only through the no-state branch below).
  No state for the agent (seen in practice: a resumed round's tool calls all got "no budget state";
  likely a late SubagentStop from the previous round deleting the new round's state): start a
  time-only default round instead of denying, so the round can still work and report.
Every event is appended to .hook-only/budget/events.jsonl to diagnose such cases.

SubagentStop: delete that agent's state (a resume claims a new one). SubagentStart also removes
  state files older than 7 days, left behind when a session died before SubagentStop. Every write and removal of
  the state goes through budget_state.py.
Gate waits (budget_state.markWait / excludeWait): a quality gate that held the agent takes its wait out of
  the clock itself, unless Claude Code cancelled the gate's hook at its timeout first. budget_state.releaseOrphanWait
  settles the mark such a hook leaves: at SubagentStop always, before a tool call once it is older than
  the hook's timeout.

USD is priced per model and cache TTL by prices.py (home.json prices), the same pricing subagent-cost.py
records as `usd`; requests of a model missing from home.json prices are not counted and the deny message says
how many. The subagent's transcript is read through usage.py (usage.tracked, reader "budget"): each call
parses only the lines added since the previous one (a last line still missing its newline counted too:
final=True), from the offset kept in
.hook-only/hook-read-only/usage/budget-<agent_id>.json (kept across rounds: a resume reads on from there).

Only agent types listed in home.json budget are budgeted.
Spend is summed from the subagent's transcript, which lags by about one request.

Reads: the hook input, budget, prices, the subagent's transcript (usage.py), budget/pending-*.json.
Writes: budget/<agent_id>.json (budget_state.py, locked), budget/events.jsonl, removes pending-*.json.
Output: preToolUse(data) returns the PreToolUse deny, or None; onSubagentStart / onSubagentStop return None.
Fails: closed on PreToolUse — any error while checking a budgeted agent denies the call with the error as
the reason (its own catch), any other exception too (hook-entry.py); open on SubagentStart, where a failed
claim leaves an error state that denies every later tool call, and on SubagentStop (hook-entry.py: a
warning once and a hook-failed event).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import budget_state  # noqa: E402
import hookio  # noqa: E402
import homeconfig  # noqa: E402
import hookonly  # noqa: E402
import prices  # noqa: E402
import usage  # noqa: E402

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
	raise hookio.Answer(hookio.refuse("PreToolUse", reason))


def loadConfig():
	config = homeconfig.load("budget")
	return config["fallback_minutes"], float(config["budget_headroom_factor"])


def stateDirFor(data):
	return hookonly.stateDir(hookonly.projectRoot(data), "budget")


def usdUsed(data):
	"""(USD of the subagent's requests so far, number of requests of an unpriced model)."""
	agentId = data.get("agent_id") or ""
	reqs = usage.tracked(hookonly.projectRoot(data), "budget", agentId, usage.transcriptOf(data), final=True)
	return usage.usd(reqs, prices.load())


def writeState(data, budget):
	agentId = data.get("agent_id") or ""
	usdAtStart = usdUsed(data)[0] if budget.get("usd") else 0
	budget.update({"agent_type": data.get("agent_type") or "", "started": time.time(),
		"usd_at_start": usdAtStart})
	budget_state.write(hookonly.projectRoot(data), agentId, budget)


def onStart(data, budgeted):
	agentType = data.get("agent_type") or ""
	agentId = data.get("agent_id") or ""
	if agentType not in budgeted or not agentId:
		return
	budget = {"minutes": budgeted[agentType], "phase": "checkpoint", "source": "default"}
	stateDir = stateDirFor(data)
	for key in (agentId, agentType):
		budgetFile = os.path.join(stateDir, f"pending-{key}.json")
		if not os.path.isfile(budgetFile):
			continue
		with open(budgetFile, encoding="utf-8") as f:
			given = json.load(f)
		budget = {
			"minutes": float(given["minutes"]),
			"usd": float(given["usd"]) if given.get("usd") else None,
			"phase": given.get("phase") or "checkpoint",
			"source": os.path.basename(budgetFile),
			"raw_minutes": given.get("raw_minutes"),
			"raw_usd": given.get("raw_usd"),
		}
		os.remove(budgetFile)
		break
	# cost-history.py rows reads these fields: the estimate side of the Cost table.
	logEvent(data, "budget: " + budget["source"], {"budget_minutes": budget["minutes"],
		"budget_usd": budget.get("usd"), "budget_source": budget["source"],
		"raw_minutes": budget.get("raw_minutes"), "raw_usd": budget.get("raw_usd")})
	writeState(data, budget)


def onPreToolUse(data, budgeted, headroom):
	agentType = data.get("agent_type") or ""
	agentId = data.get("agent_id") or ""
	if agentType not in budgeted or not agentId:
		return
	stateFile = os.path.join(stateDirFor(data), f"{agentId}.json")
	if not os.path.isfile(stateFile):
		writeState(data, {"minutes": budgeted[agentType], "phase": "checkpoint",
						  "source": "default (no state at PreToolUse)"})
		logEvent(data, "no-state: started default round")
		return
	budget_state.releaseOrphanWait(hookonly.projectRoot(data), agentId)
	with open(stateFile, encoding="utf-8") as f:
		budget = json.load(f)

	minutesUsed = (time.time() - budget["started"]) / 60
	ratio = minutesUsed / budget["minutes"]
	detail = f"{minutesUsed:.2f} of {budget['minutes']:g} min"
	if budget.get("usd"):
		transcript = usage.transcriptOf(data)
		if transcript and os.path.isfile(transcript):
			spent, unpriced = usdUsed(data)
			used = spent - float(budget.get("usd_at_start") or 0)
			ratio = max(ratio, used / budget["usd"])
			detail += f", ${used:.2f} of ${budget['usd']:g}"
			if unpriced:
				detail += f" ({unpriced} requests of a model missing from the price table not counted)"

	# cost-history.py marks a round "budget-stopped" by this message's start ("Budget used up").
	if ratio >= headroom:
		deny(f"Budget used up: {ratio:.0%} of the budget ({detail}; the stop is at {headroom:.0%}). Stop now and report CHECKPOINT: what is done, what you tried, your current hypothesis, what remains, what finishing would cost.", data)


def onStop(data):
	agentId = data.get("agent_id") or ""
	if not agentId:
		return
	try:
		budget_state.releaseOrphanWait(hookonly.projectRoot(data), agentId, ended=True)
	except Exception as error:
		print(f"subagent-budget: wait not settled ({error})", file=sys.stderr)
	budget_state.remove(hookonly.projectRoot(data), agentId)


def writeErrorState(data, error):
	"""SubagentStart can't block, so leave a state that makes every tool call deny with the error."""
	agentId = data.get("agent_id") or ""
	if not agentId:
		return
	budget_state.write(hookonly.projectRoot(data), agentId, {"error": str(error)})


def onSubagentStart(data):
	logEvent(data)
	try:
		onStart(data, loadConfig()[0])
	except Exception as error:
		writeErrorState(data, error)
	budget_state.removeStaleStates(stateDirFor(data))


def onSubagentStop(data):
	logEvent(data)
	onStop(data)


def preToolUse(data):
	if not data.get("agent_id"):
		return None
	if data.get("tool_name") in ALWAYS_ALLOWED:
		logEvent(data, "allow: " + data.get("tool_name"))
		return None
	try:
		budgeted, headroom = loadConfig()
	except Exception as error:
		deny(f"Budget guard can't read {homeconfig.where('budget')} ({error}). Stop and report BLOCKED with this message.", data)
	stateFile = os.path.join(stateDirFor(data), f"{data.get('agent_id')}.json")
	if (data.get("agent_type") or "") in budgeted and os.path.isfile(stateFile):
		with open(stateFile, encoding="utf-8") as f:
			state = json.load(f)
		if "error" in state:
			deny(f"Budget guard failed at start ({state['error']}). Stop and report BLOCKED with this message.", data)
	try:
		onPreToolUse(data, budgeted, headroom)
	except hookio.Answer:
		raise
	except Exception as error:
		if (data.get("agent_type") or "") in budgeted:
			deny(f"Budget guard error ({error}). Stop and report BLOCKED with this message.", data)
	return None

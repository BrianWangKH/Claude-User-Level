"""The per-agent budget state file, <checkout>/.hook-only/budget/<agent_id>.json: every write of it goes through
here (subagent_budget.py writes and removes it; the quality gates, crap-gate.py and mutation.py, mark and settle
their waits in it). It has three writers, so every read-modify-write and every write holds the file's lock,
<agent_id>.json.lock beside it (fcntl.flock, exclusive; hooks/RULES.md S9), and the file is replaced atomically
(a temporary file in the same directory, then os.replace), so a reader never sees half a file. Readers
(subagent_budget.py's checks, skill repo-plan's worker-status.py, the wt-pane mod) read it directly. The file has
no "schema" field and no registry entry yet (S5, S8: the state-convergence stage).

- write(root, agentId, state): the whole state (subagent_budget.py: the claimed budget, or {"error"} when the
  claim failed); remove(root, agentId): the round ended (SubagentStop).
- removeStaleStates(stateDir): SubagentStart's sweep of the budget directory's .json files (and their .lock
  files) older than 7 days (left behind when a session died before SubagentStop).
- markWait / excludeWait / releaseOrphanWait: a gate holding the agent (below).
"""

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookonly

STALE_SECONDS = 7 * 24 * 3600
LOCK_SUFFIX = ".lock"


def statePath(root, agentId):
	return os.path.join(hookonly.stateDir(root, "budget"), f"{agentId}.json")


@contextlib.contextmanager
def locked(root, agentId):
	"""Hold the state file's lock for one read-modify-write (module doc)."""
	path = statePath(root, agentId) + LOCK_SUFFIX
	os.makedirs(os.path.dirname(path), exist_ok=True)
	handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
	try:
		fcntl.flock(handle, fcntl.LOCK_EX)
		yield
	finally:
		os.close(handle)


def replaceState(path, state):
	"""Write state to path atomically (module doc)."""
	directory = os.path.dirname(path)
	os.makedirs(directory, exist_ok=True)
	handle, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
	try:
		with os.fdopen(handle, "w", encoding="utf-8") as f:
			json.dump(state, f)
		os.replace(temporary, path)
	except BaseException:
		if os.path.exists(temporary):
			os.remove(temporary)
		raise


def readState(path):
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def write(root, agentId, state):
	"""The agent's whole budget state (subagent_budget.py writeState / writeErrorState)."""
	with locked(root, agentId):
		replaceState(statePath(root, agentId), state)


def remove(root, agentId):
	"""The round ended (subagent_budget.py at SubagentStop); no state is fine."""
	with locked(root, agentId):
		try:
			os.remove(statePath(root, agentId))
		except FileNotFoundError:
			pass


def removeStaleStates(stateDir):
	if not os.path.isdir(stateDir):
		return
	cutoff = time.time() - STALE_SECONDS
	for name in os.listdir(stateDir):
		path = os.path.join(stateDir, name)
		try:
			if name.endswith((".json", ".json" + LOCK_SUFFIX)) and os.path.getmtime(path) < cutoff:
				os.remove(path)
		except OSError:
			pass


def markWait(root, agentId, what, maxSeconds):
	"""Record that a gate now holds the agent, so status displays can tell waiting from working.

	Adds "hook_wait": {"since", "what", "max_seconds"} to subagent_budget.py's state
	(.hook-only/budget/<agent_id>.json); excludeWait removes it when the wait ends. maxSeconds is the
	hook's timeout (qg_common.hookTimeout): the hold never outlasts it, so a mark older than that was left by a hook
	Claude Code cancelled, which releaseOrphanWait settles. Never fails the gate."""
	if not agentId:
		return
	stateFile = statePath(root, agentId)
	try:
		with locked(root, agentId):
			state = readState(stateFile)
			state["hook_wait"] = {"since": time.time(), "what": what, "max_seconds": maxSeconds}
			replaceState(stateFile, state)
	except (OSError, ValueError, TypeError):
		pass


def excludeWait(root, agentId, seconds, at=None):
	"""Take a gate's wait out of the agent's time budget and its recorded minutes.

	A gate on SubagentHandback can hold the agent for minutes (the mutation scan, the coverage run). That
	is hook time, not the agent's work, and no plan can size it. Shifts `started` in subagent_budget.py's
	state (.hook-only/budget/<agent_id>.json) forward, drops the "hook_wait" mark markWait left, and logs
	a HookWait event that cost-history.py rows subtracts from the round's actual minutes. A wait under a
	second only drops the mark. `at` (epoch seconds) dates the event; default now."""
	if not agentId:
		return
	with locked(root, agentId):
		excludeLocked(root, agentId, seconds, at)


def excludeLocked(root, agentId, seconds, at):
	"""excludeWait with the lock already held."""
	budgetDir = hookonly.stateDir(root, "budget")
	stateFile = statePath(root, agentId)
	counted = seconds >= 1
	try:
		state = readState(stateFile)
		marked = state.pop("hook_wait", None) is not None
		if counted:
			state["started"] = float(state["started"]) + seconds
		if counted or marked:
			replaceState(stateFile, state)
	except (OSError, ValueError, KeyError, TypeError):
		pass
	if not counted:
		return
	os.makedirs(budgetDir, exist_ok=True)
	with open(os.path.join(budgetDir, "events.jsonl"), "a", encoding="utf-8") as f:
		f.write(
			json.dumps(
				{
					"t": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
					"event": "HookWait",
					"agent_id": agentId,
					"seconds": round(seconds, 1),
				}
			)
			+ "\n"
		)


def releaseOrphanWait(root, agentId, ended=False, now=None):
	"""Settle a wait mark its gate never cleared, as excludeWait would have; returns the seconds excluded.

	A gate clears its mark in a finally, but Claude Code cancels a hook that reaches its timeout and
	discards its output (code.claude.com/docs/en/hooks, Timeouts; how it ends the process isn't
	documented), so the finally may never run: the wait stays on the agent's clock and no HookWait is
	logged. subagent_budget.py calls this at SubagentStop (ended: any mark left is such a wait, since the
	agent can't stop while held) and before each tool call (a mark older than its max_seconds, the hook's
	timeout; a younger one may belong to a hook still holding a parallel call). The wait counts from its
	mark to now, at most max_seconds, and its event is dated at the mark, inside the round's
	started..stopped that cost-history.py matches it to."""
	if not agentId:
		return 0.0
	now = time.time() if now is None else now
	with locked(root, agentId):
		try:
			mark = readState(statePath(root, agentId)).get("hook_wait")
			since, limit = float(mark["since"]), float(mark.get("max_seconds") or 0)
		except (OSError, ValueError, KeyError, TypeError, AttributeError):
			return 0.0
		held = max(0.0, now - since)
		if not ended and (not limit or held <= limit):
			return 0.0
		seconds = min(held, limit) if limit else held
		excludeLocked(root, agentId, seconds, since)
	return seconds

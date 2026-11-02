#!/usr/bin/env python3
"""PreToolUse hook (matcher "*", async): what a headless worker run is doing, for the wt-pane mod.

Active only in worker runs: CLAUDE_WORKER_MINUTES is set by run-worker.sh, the same switch as
worker-budget.py, so planning and desktop sessions write nothing. On every tool call of the worker
(the orchestrator, key "main") or one of its subagents (key = agent_id) it rewrites
<checkout>/.hook-only/hook-read-only/progress/<key>.json:
  {"t", "agent_id", "agent_type", "tool", "what", "run", "usd", "usd_at_run_start", "usd_run", "unpriced"}
`what` is a short label of the call (Bash/Agent description, a path, a pattern). `usd` is the
agent's spend so far, priced by prices.py from its transcript; `usd_run` is the part spent since the
run (newest .hook-only/runs/start-<n>) began, so a resumed worker shows this run's spend.
The transcript is read from where the previous call stopped (offset and per-request costs kept in
progress/.offsets/<key>.json), so each call parses only the new lines.

Only the mod reads these files: agents can't (hook-read-only), and the hook prints nothing and never
blocks, so it costs the model nothing. Fails silently: progress is display only.
A call killed between writing a temporary file and renaming it (the run ending mid-call) leaves a
*.tmp behind; every call removes those older than STALE_TMP_SECONDS. The progress files themselves
go with the worktree when /finish-worktree removes it.
"""
import json
import os
import re
import sys
import time

import hookonly
import prices

WHAT_LIMIT = 100
STALE_TMP_SECONDS = 600
START_FILE = re.compile(r"^start-(\d+)$")
SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def currentRun(root):
	"""Number of the newest .hook-only/runs/start-<n>, or 0 without one."""
	runsDir = os.path.join(root, hookonly.NAME, "runs")
	try:
		numbers = [int(m.group(1)) for name in os.listdir(runsDir) if (m := START_FILE.match(name))]
	except OSError:
		return 0
	return max(numbers, default=0)


def agentTranscript(sessionTranscript, agentId):
	"""The orchestrator's transcript, or .../<session>/subagents/agent-<id>.jsonl for a subagent."""
	if not sessionTranscript or not agentId:
		return sessionTranscript
	return os.path.join(os.path.splitext(sessionTranscript)[0], "subagents", f"agent-{agentId}.jsonl")


def describe(tool, toolInput, root):
	"""One short line saying what the call does."""
	toolInput = toolInput if isinstance(toolInput, dict) else {}
	if tool == "Bash":
		text = toolInput.get("description") or toolInput.get("command") or ""
	elif tool in ("Agent", "Task"):
		text = f"{toolInput.get('subagent_type') or 'agent'}: {toolInput.get('description') or ''}"
	elif tool == "SendMessage":
		text = f"to {toolInput.get('to') or ''}"
	elif tool == "Skill":
		text = toolInput.get("skill") or ""
	elif toolInput.get("file_path") or toolInput.get("notebook_path"):
		path = toolInput.get("file_path") or toolInput.get("notebook_path")
		text = os.path.relpath(path, root) if path.startswith(root + os.sep) else path
	elif toolInput.get("pattern"):
		text = toolInput["pattern"]
	else:
		text = ""
	text = " ".join(str(text).split())
	return text if len(text) <= WHAT_LIMIT else text[:WHAT_LIMIT - 1] + "…"


def readJson(path, default):
	try:
		with open(path, encoding="utf-8") as f:
			return json.load(f)
	except (OSError, ValueError):
		return default


def writeJson(path, value):
	"""Write through a temporary file so the mod never reads half a file."""
	os.makedirs(os.path.dirname(path), exist_ok=True)
	temporary = f"{path}.{os.getpid()}.tmp"
	with open(temporary, "w", encoding="utf-8") as f:
		json.dump(value, f)
	os.replace(temporary, path)


def removeStaleTemporaries(progressDir, now):
	for folder in (progressDir, os.path.join(progressDir, ".offsets")):
		try:
			names = os.listdir(folder)
		except OSError:
			continue
		for name in names:
			path = os.path.join(folder, name)
			try:
				if name.endswith(".tmp") and now - os.path.getmtime(path) > STALE_TMP_SECONDS:
					os.remove(path)
			except OSError:
				pass


def spend(transcript, offsetFile, table):
	"""(USD so far, requests of an unpriced model), reading only the lines added since last time."""
	state = readJson(offsetFile, {})
	if state.get("path") != transcript:
		state = {"path": transcript, "offset": 0, "requests": {}}
	try:
		size = os.path.getsize(transcript)
	except (OSError, TypeError):
		size = 0
	if size < state["offset"]:
		state.update(offset=0, requests={})
	if size > state["offset"]:
		with open(transcript, "rb") as f:
			f.seek(state["offset"])
			chunk = f.read()
		end = chunk.rfind(b"\n") + 1  # a line still being written waits for the next call
		for line in chunk[:end].splitlines():
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
				# The same request appears once per content block; the last usage wins, as in usdUsed.
				state["requests"][key] = prices.requestUsd(usage, message.get("model"), table)
		state["offset"] += end
		writeJson(offsetFile, state)
	costs = state["requests"].values()
	return sum(c for c in costs if c is not None), sum(1 for c in costs if c is None)


def record(data, now=None):
	root = hookonly.projectRoot(data)
	agentId = data.get("agent_id") or ""
	key = agentId or "main"
	if not SAFE_KEY.match(key):
		return
	progressDir = hookonly.stateDir(root, "progress", strict=True)
	removeStaleTemporaries(progressDir, time.time())
	statePath = os.path.join(progressDir, f"{key}.json")
	previous = readJson(statePath, {})
	run = currentRun(root)
	transcript = agentTranscript(data.get("transcript_path"), agentId)
	usd, unpriced = spend(transcript, os.path.join(progressDir, ".offsets", f"{key}.json"), prices.load())
	if previous.get("run") == run:
		runStartUsd = previous.get("usd_at_run_start", 0)
	else:
		# First call of this agent in the run: what it spent before belongs to earlier runs.
		runStartUsd = previous.get("usd", 0)
	writeJson(statePath, {
		"t": now if now is not None else time.time(),
		"agent_id": agentId,
		"agent_type": data.get("agent_type") or ("orchestrator" if not agentId else ""),
		"tool": data.get("tool_name") or "",
		"what": describe(data.get("tool_name") or "", data.get("tool_input"), root),
		"run": run,
		"usd": round(usd, 6),
		"usd_at_run_start": round(runStartUsd, 6),
		"usd_run": round(usd - runStartUsd, 6),
		"unpriced": unpriced,
	})


def main():
	if not os.environ.get("CLAUDE_WORKER_MINUTES"):
		return
	try:
		record(json.load(sys.stdin))
	except Exception:
		pass


if __name__ == "__main__":
	main()
	sys.exit(0)

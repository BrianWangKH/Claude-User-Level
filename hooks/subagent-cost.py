"""SubagentStop hook: record how many tokens a subagent actually consumed.

The completion notification only reports the context size of the last request. This hook sums the
usage of every API request in the subagent's own transcript (agent_transcript_path): input, cache
writes, cache reads and output. One line per stop is appended to
<checkout>/.hook-only/cost/<agent_id>.jsonl (see hookonly.py), with the running total and the delta since the previous
stop (= that round, since a resumed agent keeps one transcript).

When SubagentStop fires, the final response is not in the transcript yet. So the hook hands the
work to a detached background process and returns at once; that process waits (up to 30 s) until
the transcript contains last_assistant_message, or stops growing, then writes the record.

Records older than 30 days are deleted on each write. A worktree's records also go away with
`git worktree remove`, which deletes the worktree directory.

Informational only: it never blocks and never prints, so a failure can't stop a subagent.
Outside a git repo it writes into the project directory's .hook-only/.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import hookonly

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
WAIT_SECONDS = float(os.environ.get("CLAUDE_COST_WAIT", "30"))
KEEP_SECONDS = 30 * 24 * 3600


def removeOldRecords(outDir):
	cutoff = time.time() - KEEP_SECONDS
	for name in os.listdir(outDir):
		path = os.path.join(outDir, name)
		try:
			if name.endswith(".jsonl") and os.path.getmtime(path) < cutoff:
				os.remove(path)
		except OSError:
			pass


def readTranscript(path):
	"""Usage per API request (last line wins per message id), tool calls, assistant texts, timestamps."""
	requests = {}
	toolCalls = 0
	texts = []
	first = last = None
	with open(path, encoding="utf-8") as f:
		for line in f:
			try:
				entry = json.loads(line)
			except ValueError:
				continue
			stamp = entry.get("timestamp")
			if stamp:
				first = first or stamp
				last = stamp
			if entry.get("type") != "assistant":
				continue
			message = entry.get("message") or {}
			usage = message.get("usage")
			key = message.get("id") or entry.get("requestId") or entry.get("uuid")
			if usage and key:
				requests[key] = usage
			for part in message.get("content") or []:
				if not isinstance(part, dict):
					continue
				if part.get("type") == "tool_use":
					toolCalls += 1
				elif part.get("type") == "text":
					texts.append(part.get("text") or "")
	totals = {k: sum(int(u.get(k) or 0) for u in requests.values()) for k in USAGE_KEYS}
	totals["requests"] = len(requests)
	totals["tool_calls"] = toolCalls
	totals["all_tokens"] = sum(totals[k] for k in USAGE_KEYS)
	return totals, texts, first, last


def waitForFinalMessage(path, lastMessage):
	"""Return (complete, transcript data). Complete once the final text is in the transcript,
	or, without a final text, once the file has stopped growing for 2 s."""
	wanted = (lastMessage or "").strip()
	deadline = time.time() + WAIT_SECONDS
	lastSize, stableSince = -1, time.time()
	while True:
		result = readTranscript(path)
		texts = result[1]
		if wanted and any(t.strip() and (t.strip() in wanted or wanted in t.strip()) for t in texts[-3:]):
			return True, result
		size = os.path.getsize(path)
		if size != lastSize:
			lastSize, stableSince = size, time.time()
		elif not wanted and time.time() - stableSince >= 2:
			return True, result
		if time.time() >= deadline:
			return False, result
		time.sleep(0.5)


def lastRecord(path):
	if not os.path.exists(path):
		return None
	with open(path, encoding="utf-8") as f:
		lines = [l for l in f if l.strip()]
	return json.loads(lines[-1]) if lines else None


def record(data):
	transcript = data.get("agent_transcript_path")
	agentId = data.get("agent_id") or ""
	if not transcript or not agentId or not os.path.isfile(transcript):
		return
	outDir = hookonly.stateDir(hookonly.projectRoot(data), "cost")

	complete, (totals, _, first, last) = waitForFinalMessage(transcript, data.get("last_assistant_message"))
	os.makedirs(outDir, exist_ok=True)
	outFile = os.path.join(outDir, f"{agentId}.jsonl")
	previous = lastRecord(outFile)
	prevTotals = (previous or {}).get("total") or {}
	delta = {k: v - int(prevTotals.get(k) or 0) for k, v in totals.items()}

	entry = {
		"stopped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"agent_type": data.get("agent_type"),
		"round": int((previous or {}).get("round") or 0) + 1,
		"complete": complete,
		"transcript_first": first,
		"transcript_last": last,
		"total": totals,
		"this_round": delta,
	}
	with open(outFile, "a", encoding="utf-8") as f:
		f.write(json.dumps(entry) + "\n")
	removeOldRecords(outDir)


def main():
	if len(sys.argv) > 1 and sys.argv[1] == "--deferred":
		record(json.loads(os.environ["CLAUDE_COST_HOOK_INPUT"]))
		return
	raw = sys.stdin.read()
	json.loads(raw)  # fail fast on bad input; nothing to record
	env = dict(os.environ, CLAUDE_COST_HOOK_INPUT=raw)
	subprocess.Popen([sys.executable, os.path.abspath(__file__), "--deferred"], env=env,
		stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
		start_new_session=True, close_fds=True)


if __name__ == "__main__":
	try:
		main()
	except Exception:
		pass
	sys.exit(0)

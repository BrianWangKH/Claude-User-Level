"""SubagentStart + SubagentStop hook: record how many tokens a subagent actually consumed.

The completion notification only reports the context size of the last request. This hook sums the
usage of every API request in the subagent's own transcript (agent_transcript_path; read through
usage.py): input, cache writes, cache reads and output, their plain sum (all_tokens) and their USD cost
(usd, prices.py: the unit of budgets and of the plan's Cost table; unpriced_requests counts requests of
a model missing from home.json prices). Older records may also carry weighted_tokens; nothing reads it.
One line per stop is appended to
<checkout>/.hook-only/cost/<agent_id>.jsonl (see hookonly.py), with the running total and the delta since the previous
stop (= that round, since a resumed agent keeps one transcript).

Rounds: SubagentStart (a fresh spawn or a resume) stores the transcript's totals at that moment in
<agent_id>.start.json; every stop's this_round is "now - that start". A background subagent fires
SubagentStop twice (its turn ends, then again after SubagentHandback), so both lines carry the whole
round instead of the second one holding only the last request (seen: 19.6k of ~650k).
The start file is kept, and the next SubagentStart overwrites it. Without one (an agent started
before this hook listened to SubagentStart), this_round falls back to the delta since the previous
record.

When SubagentStop fires, the final response is not in the transcript yet. So the hook hands the
work to a detached background process and returns at once; that process waits (up to 30 s) until
the transcript contains last_assistant_message, or stops growing, then writes the record. Each poll
parses only the lines added since the previous one (usage.py; the read state is kept in memory for
the wait); a last line whose newline isn't written yet is counted too.

Records older than 30 days are deleted on each write. A worktree's records also go away with
`git worktree remove`, which deletes the worktree directory.

Informational only: it never blocks and never prints, so a failure can't stop a subagent.
Outside a git repo it writes into the project directory's .hook-only/.
"""
import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookonly
import prices
import usage

TEXTS_KEPT = 3  # waitForFinalMessage compares the final message with the last few texts only
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


def scanEntry(entry, state):
	"""Tool calls, the last assistant texts and the first and last timestamps, kept in state["scan"]."""
	scan = state.setdefault("scan", {"tool_calls": 0, "texts": [], "first": None, "last": None})
	stamp = entry.get("timestamp")
	if stamp:
		scan["first"] = scan["first"] or stamp
		scan["last"] = stamp
	if entry.get("type") != "assistant":
		return
	message = entry.get("message")
	for part in (message.get("content") if isinstance(message, dict) else None) or []:
		if not isinstance(part, dict):
			continue
		if part.get("type") == "tool_use":
			scan["tool_calls"] += 1
		elif part.get("type") == "text":
			scan["texts"] = (scan["texts"] + [part.get("text") or ""])[-TEXTS_KEPT:]


def readTranscript(path, state=None):
	"""(state, (totals, the last assistant texts, first timestamp, last timestamp)): usage per API request
	(last line wins per message id, usage.py) and tool calls, reading on from state. The returned state
	covers the complete lines; the result also counts a last line still missing its newline (a final
	read on a copy, so the next poll's scanEntry doesn't see that line twice)."""
	state, _ = usage.requests(path, state, scanEntry)
	view, reqs = usage.requests(path, copy.deepcopy(state), scanEntry, final=True)
	scan = view.get("scan") or {"tool_calls": 0, "texts": [], "first": None, "last": None}
	totals = usage.totals(reqs)
	totals["tool_calls"] = scan["tool_calls"]
	totals["all_tokens"] = sum(totals[k] for k in usage.USAGE_KEYS)
	spent, unpriced = usage.usd(reqs, prices.load())
	totals["usd"] = round(spent, 6)
	totals["unpriced_requests"] = unpriced
	return state, (totals, scan["texts"], scan["first"], scan["last"])


def waitForFinalMessage(path, lastMessage):
	"""Return (complete, transcript data). Complete once the final text is in the transcript,
	or, without a final text, once the file has stopped growing for 2 s."""
	wanted = (lastMessage or "").strip()
	deadline = time.time() + WAIT_SECONDS
	lastSize, stableSince = -1, time.time()
	state = None
	while True:
		state, result = readTranscript(path, state)
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


def recordStart(data):
	agentId = data.get("agent_id") or ""
	if not agentId:
		return
	outDir = hookonly.stateDir(hookonly.projectRoot(data), "cost")
	os.makedirs(outDir, exist_ok=True)
	transcript = usage.transcriptOf(data)
	totals = readTranscript(transcript)[1][0] if transcript and os.path.isfile(transcript) else {}
	startFile = os.path.join(outDir, f"{agentId}.start.json")
	previous = lastStart(startFile) or lastRecord(os.path.join(outDir, f"{agentId}.jsonl")) or {}
	with open(startFile, "w", encoding="utf-8") as f:
		json.dump({
			"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
			"round": int(previous.get("round") or 0) + 1,
			"total": totals,
		}, f)


def lastStart(path):
	if not os.path.isfile(path):
		return None
	with open(path, encoding="utf-8") as f:
		return json.load(f)


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
	start = lastStart(os.path.join(outDir, f"{agentId}.start.json"))
	base = start if start else (previous or {})
	prevTotals = base.get("total") or {}
	delta = {k: round(v - (prevTotals.get(k) or 0), 6) for k, v in totals.items()}
	round_ = int(start["round"]) if start else int((previous or {}).get("round") or 0) + 1

	entry = {
		"stopped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"agent_type": data.get("agent_type"),
		"round": round_,
		"started_at": (start or {}).get("started_at"),
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
		data = json.loads(os.environ["CLAUDE_COST_HOOK_INPUT"])
		if data.get("hook_event_name") == "SubagentStart":
			recordStart(data)
		else:
			record(data)
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

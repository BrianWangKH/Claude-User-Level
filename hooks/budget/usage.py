"""Usage of API requests from Claude Code transcripts, read incrementally: the one transcript reader of
subagent_budget.py, subagent-cost.py, worker-progress.py and dispatch_guard.py.

- requests(path, state=None, onEntry=None, final=False) -> (state, {key: (usage, model, ts)}): every API
  request of the transcript.
  A request is logged once per content block under one key (message.id, else requestId, else uuid); the
  last line wins. usage keeps the fields totals() and prices.requestUsd read (USAGE_KEYS and the
  cache_creation TTL split); ts is the line's timestamp. Only complete lines are read: a line still being
  written waits for the next call. state (a plain dict, schema SCHEMA) is where the previous call
  stopped; another path, another file at that path (inode) or a file shorter than the offset starts
  again from 0. onEntry(entry, state), when given, sees every new complete line (a dict) too: fields a
  caller keeps in state come back while the read goes on and are gone when it starts again (nested
  values are shared with the state passed in).
  final=True, for a value that gets recorded or decides something: bytes after the last newline that
  parse as one JSON object (a last line whose newline isn't written yet) count too, but the returned
  state's offset stays before them, so the next read reads them again. Requests are keyed, so that is
  harmless for them; onEntry would see the line twice, so a caller with onEntry reads on from a
  non-final state (subagent-cost.readTranscript). Without final such a line waits for its newline.- totals(reqs) -> the four raw token counts summed, plus "requests".
- usd(reqs, table) -> (USD, requests of a model missing from the price table), priced by prices.py.
- transcriptOf(data): the agent's own transcript: agent_transcript_path when the input has it (Claude
  Code gives it at SubagentStop), else <session>/subagents/agent-<agent_id>.jsonl derived from
  transcript_path, else (no agent_id: the main session) transcript_path itself.
- latestAttachment(path, kind, state=None, field=None, final=False) -> (state, attachment or None): the
  newest `attachment` object of that type (with `field` not null, when given), read the same way.
- tracked(root, reader, key, path, final=False) / trackedAttachment(root, reader, key, path, kind, field,
  final=False): the same,
  resuming from and saving the reader's state for key in its registered kind (READERS: kind
  usage-<reader>, file .hook-only/hook-read-only/usage/<reader>-<key>.json). One kind per reader, so
  every file has one writer program; two calls of the same hook at once each write a complete state
  (statefile.writeJson replaces the file), and any state written is a valid point to resume from. A key
  that isn't a plain name is read in full and nothing is saved.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import prices
import statefile

SCHEMA = 1
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
SPLIT_KEYS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
READERS = {"budget": "usage-budget", "progress": "usage-progress", "dispatch": "usage-dispatch"}
MOVED = ("path", "inode", "offset")


def fresh(path):
	return {"schema": SCHEMA, "path": path, "inode": None, "offset": 0}


def resumable(state, path, inode, size):
	"""Whether state continues the file at path: the same file, not shorter than the offset."""
	return (
		isinstance(state, dict)
		and state.get("schema") == SCHEMA
		and state.get("path") == path
		and state.get("inode") == inode
		and isinstance(state.get("offset"), int)
		and state["offset"] <= size
	)


def newLines(path, state, initial, final=False):
	"""(state, the complete lines added since state). initial(state) adds the reader's own fields to a
	new state; a missing file gives a new state and no lines. With final, the bytes after the last newline
	come last when they parse as a JSON object; the offset doesn't move past them."""
	try:
		f = open(path, "rb")
	except (OSError, TypeError):
		return initial(fresh(path)), []
	with f:
		info = os.fstat(f.fileno())
		if resumable(state, path, info.st_ino, info.st_size):
			state = dict(state)
		else:
			state = initial(fresh(path))
			state["inode"] = info.st_ino
		if info.st_size <= state["offset"]:
			return state, []
		f.seek(state["offset"])
		chunk = f.read(info.st_size - state["offset"])
	end = chunk.rfind(b"\n") + 1
	state["offset"] += end
	lines = chunk[:end].splitlines()
	if final and end < len(chunk) and entryOf(chunk[end:]) is not None:
		lines.append(chunk[end:])
	return state, lines


def entryOf(line):
	"""The transcript line as a dict, or None for a torn or foreign line."""
	try:
		entry = json.loads(line)
	except ValueError:
		return None
	return entry if isinstance(entry, dict) else None


def slim(usage):
	"""The usage fields this module's callers read."""
	out = {k: usage[k] for k in USAGE_KEYS if k in usage}
	split = usage.get("cache_creation")
	if isinstance(split, dict):
		out["cache_creation"] = {k: split[k] for k in SPLIT_KEYS if k in split}
	return out


def withRequests(state):
	state["requests"] = {}
	return state


def requests(path, state=None, onEntry=None, final=False):
	state, lines = newLines(path, state, withRequests, final)
	found = dict(state["requests"])
	for line in lines:
		entry = entryOf(line)
		if entry is not None and onEntry is not None:
			onEntry(entry, state)
		if entry is None or entry.get("type") != "assistant":
			continue
		message = entry.get("message")
		message = message if isinstance(message, dict) else {}
		usage = message.get("usage")
		key = message.get("id") or entry.get("requestId") or entry.get("uuid")
		if usage and key and isinstance(usage, dict):
			found[key] = [slim(usage), message.get("model"), entry.get("timestamp")]
	state["requests"] = found
	return state, {k: tuple(v) for k, v in found.items()}


def totals(reqs):
	out = {k: sum(int(u.get(k) or 0) for u, _, _ in reqs.values()) for k in USAGE_KEYS}
	out["requests"] = len(reqs)
	return out


def usd(reqs, table):
	costs = [prices.requestUsd(u, m, table) for u, m, _ in reqs.values()]
	return sum(c for c in costs if c is not None), sum(1 for c in costs if c is None)


def transcriptOf(data):
	if data.get("agent_transcript_path"):
		return data["agent_transcript_path"]
	session = data.get("transcript_path")
	agentId = data.get("agent_id")
	if not session or not agentId:
		return session
	return os.path.join(os.path.splitext(session)[0], "subagents", f"agent-{agentId}.jsonl")


def withLatest(state):
	state["latest"] = None
	return state


def latestAttachment(path, kind, state=None, field=None, final=False):
	state, lines = newLines(path, state, withLatest, final)
	marker = json.dumps(kind).encode("utf-8")
	for line in lines:
		if marker not in line:
			continue
		entry = entryOf(line)
		attachment = entry.get("attachment") if entry is not None else None
		if not isinstance(attachment, dict) or attachment.get("type") != kind:
			continue
		if field is None or attachment.get(field) is not None:
			state["latest"] = attachment
	return state, state["latest"]


def savable(key):
	return statefile.KEY.match(str(key)) is not None


def resume(root, reader, key, read):
	"""read(saved state or None) -> (state, result); the state is saved again when it moved."""
	kind = READERS[reader]
	saved = statefile.read(root, kind, key) if savable(key) else None
	state, result = read(saved)
	if savable(key) and (saved is None or any(saved.get(k) != state.get(k) for k in MOVED)):
		statefile.write(root, kind, state, key)
	return result


def tracked(root, reader, key, path, final=False):
	return resume(root, reader, key, lambda saved: requests(path, saved, final=final))


def trackedAttachment(root, reader, key, path, kind, field=None, final=False):
	return resume(root, reader, key, lambda saved: latestAttachment(path, kind, saved, field, final))

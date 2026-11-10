#!/usr/bin/env python3
"""What happened to the workers and the queue, for the person and the planning session.

Usage:
  wt_events.py emit <main checkout> <kind> <topic> <text>   (run-worker.sh; the Python hooks import emit())
  UserPromptSubmit (hook-entry.py's `prompt` entry calls hookOutput; stdin JSON when run alone): in the main checkout, adds the events since the last prompt to the
  person's prompt as context, so the planning session knows what changed without a turn of its own.
The log is <main>/.hook-only/events.jsonl, one {"t", "kind", "topic", "text"} per line, appended by hooks only
(agents can't write .hook-only/). Kinds: run-ended (its STATUS line), merged, refused, retry (a refusal the
flow answers itself with a merge or fix run; worker-control.py emits "refused" when it can't), verified
(verifier.py's verdict), queue-started, queue-failed (a queued topic's start failed, or the tick run-steps.py
runs after a wrap-up or auto-finish.py after a finish did), finished, finish-failed, step-failed (a wrap-up step
run-steps.py closed failed), wrapup-failed (the wrap-up itself crashed outside its steps; the run ended
wrapup-failed and its steps left are kept for `wt wrapup`; also a run record that exists but can't be read, where
no step runs), heartbeat-stalled (reconciler.py: a wrapper the process check finds alive whose heartbeat stopped,
once per run; it needs the person, nothing is stopped), step-at-limit (reconciler.py: a step, or a decided next
run's start, used step_attempts_max attempts; once per run, nothing retries it), merge-slow (run-steps.py: a merge
ran past its time limit; it is never stopped), limit-unreadable (run-steps.py: the settings can't give the wrap-up
steps a time limit, once per run; they run unbounded), setting-loosened (topic "config":
config/local.json made a safety setting less strict, wt_controls.announceLoosened, once per distinct change),
setting-invalid (topic "config": a config/local.json value is invalid and home.json's is used,
wt_controls.announceInvalid, once per distinct problem). Readers keep their own place: the prompt hook in
.hook-only/events.prompt-offset (bytes read), the wt-pane mod in memory (it starts at the end, so a new
session shows only what happens from then on, as toasts). Kinds the person must act on: needs-attention().
Fails open (stderr): a missing event never stops a run or a prompt.
"""
import json
import os
import sys
import time
from pathlib import Path

ATTENTION = {"refused", "queue-failed", "finish-failed", "step-failed", "wrapup-failed", "heartbeat-stalled",
	"step-at-limit", "merge-slow", "limit-unreadable"}
MAX_CONTEXT_EVENTS = 20


def logFile(mainDir):
	return Path(mainDir) / ".hook-only" / "events.jsonl"


def emit(mainDir, kind, topic, text):
	"""Append one event; never raises."""
	try:
		path = logFile(mainDir)
		path.parent.mkdir(parents=True, exist_ok=True)
		with open(path, "a", encoding="utf-8") as f:
			f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "topic": topic,
				"text": str(text)[:300]}, ensure_ascii=False) + "\n")
	except OSError as error:
		print(f"wt_events: not recorded ({error})", file=sys.stderr)


def needsAttention(event):
	"""Whether the person has to act: a refusal, a failed start, step, wrap-up or finish, a live wrapper whose
	heartbeat stopped, or a run that stopped short of done."""
	if event.get("kind") in ATTENTION:
		return True
	return event.get("kind") == "run-ended" and not str(event.get("text", "")).split(": ", 1)[-1].startswith("STATUS: done")


def since(mainDir, offset):
	"""(events after byte offset, new offset). A log shorter than offset (replaced) is read from the start."""
	path = logFile(mainDir)
	try:
		size = path.stat().st_size
	except OSError:
		return [], 0
	if offset > size:
		offset = 0
	with open(path, "rb") as f:
		f.seek(offset)
		data = f.read()
	end = data.rfind(b"\n") + 1  # a line still being written waits for the next read
	events = []
	for line in data[:end].decode("utf-8", errors="replace").splitlines():
		try:
			events.append(json.loads(line))
		except ValueError:
			continue
	return events, offset + end


def promptContext(mainDir):
	"""The context line for the person's next prompt, advancing the hook's place; None when nothing new."""
	place = Path(mainDir) / ".hook-only" / "events.prompt-offset"
	try:
		offset = int(place.read_text().strip())
	except (OSError, ValueError):
		offset = 0
	events, offset = since(mainDir, offset)
	place.parent.mkdir(parents=True, exist_ok=True)
	place.write_text(f"{offset}\n")
	if not events:
		return None
	shown = events[-MAX_CONTEXT_EVENTS:]
	lines = [("! " if needsAttention(e) else "- ") + f"{e.get('t', '')[11:16]} {e.get('topic', '')} {e.get('kind', '')}: {e.get('text', '')}"
		for e in shown]
	more = f" ({len(events) - len(shown)} earlier ones left out)" if len(events) > len(shown) else ""
	return ("wt events since the last prompt" + more + " (lines with ! need the person; mention them):\n"
		+ "\n".join(lines))


def hookOutput(data):
	"""The UserPromptSubmit output for this prompt (hook-entry.py's `prompt` entry), or None."""
	if data.get("hook_event_name") != "UserPromptSubmit":
		return None
	cwd = Path(data.get("cwd") or ".").resolve()
	if not (cwd / ".git").is_dir():  # the main checkout only: a worktree's .git is a file
		return None
	text = promptContext(cwd)
	if text:
		return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
	return None


def hook(data):
	output = hookOutput(data)
	if output:
		print(json.dumps(output))


def main(argv):
	if len(argv) == 6 and argv[1] == "emit":
		emit(argv[2], argv[3], argv[4], argv[5])
		return
	if len(argv) == 1:
		hook(json.load(sys.stdin))
		return
	sys.exit("usage: wt_events.py emit <main> <kind> <topic> <text>, or as a UserPromptSubmit hook")


if __name__ == "__main__":
	try:
		main(sys.argv)
	except Exception as error:
		print(f"wt_events error (ignored): {error}", file=sys.stderr)
	sys.exit(0)

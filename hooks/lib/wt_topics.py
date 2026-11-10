"""The wt topics' names and places, shared by wt_controls.py, wt_queue.py, worker-control.py and the planning
session's run-result.py and worker-status.py (each imported it from wt_controls.py, or kept its own copy,
before; wt_queue.py importing wt_controls.py while wt_controls.py imported wt_queue.py made a cycle).

- TOPIC: a topic name (lower-case letters, digits and dashes, at most 64); UUID: a Claude Code session id.
- worktreeOf(mainDir, topic): where wt puts a topic's worktree, <main>/../worktrees/<topic> (a Path; the
  place, not whether it exists: runstate.worktreeOf asks git which worktree holds the topic).
- topics(mainDir): every topic with a worktree, an estimate (plans/worker/<topic>.json) or a wt state
  (.hook-only/workers/<topic>.json), by name.
- loadJson(path): a JSON object file, or None when it is missing, unreadable, not JSON or not an object.
"""
import json
import os
import re
from pathlib import Path

import hookonly

TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def loadJson(path):
	try:
		value = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None
	return value if isinstance(value, dict) else None


def worktreeOf(mainDir, topic):
	return Path(mainDir).parent / "worktrees" / topic


def topics(mainDir):
	"""Every topic with a worktree, an estimate or a wt state, by name."""
	names = set()
	for folder, suffix in ((worktreeOf(mainDir, "x").parent, ""), (Path(mainDir) / "plans" / "worker", ".json"),
			(Path(hookonly.stateDir(str(mainDir), "workers")), ".json")):
		try:
			entries = os.listdir(folder)
		except OSError:
			continue
		names |= {n[:len(n) - len(suffix)] for n in entries if n.endswith(suffix)}
	return sorted(n for n in names if TOPIC.match(n))

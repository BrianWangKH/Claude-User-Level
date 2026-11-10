"""The per-checkout event stream (hooks/RULES.md E1-E4): <checkout>/.hook-only/events/log.jsonl, state kind
"event-log" of config/registry/state-registry.json, one line per fact that happened and won't change.

Every line: {"schema", "t", "event", "source", [topic, run, agent_id, agent_type, session_id], "data"}.
- event: a name registered in config/registry/events.json (homeconfig "events"), kebab-case noun-past-participle
  (run-ended, hook-failed); its data object must have the registered keys.
- source: the component that wrote it (a hook or tool name, kebab-case), so one stream serves every
  reader, each filtering by source or event.
emit() raises on a programming error (unregistered event, missing data key, bad source) and on a failed
write; a record-keeping hook lets that reach hookio.run(policy="record"), which tells the person once.
The streams that exist today (.hook-only/events.jsonl, budget/events.jsonl, ...) move here later.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import homeconfig  # noqa: E402
import statefile  # noqa: E402

KIND = "event-log"
REGISTRY = "events"
IDS = ("topic", "run", "agent_id", "agent_type", "session_id")
NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")
SOURCE = re.compile(r"^[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*$")


class EventError(Exception):
	pass


def registered(event):
	"""The registry entry of an event name; EventError for an unregistered or misnamed one."""
	if not NAME.match(event or ""):
		raise EventError(f"eventlog: event name {event!r} is not kebab-case <noun>-<past participle>")
	events = homeconfig.load(REGISTRY)["events"]
	if event not in events:
		raise EventError(f"eventlog: event {event!r} is not registered in {homeconfig.path(REGISTRY)}")
	return events[event]


def record(event, source, data=None, **ids):
	"""The line emit() would write (checked, not written)."""
	entry = registered(event)
	if not SOURCE.match(source or ""):
		raise EventError(f"eventlog: source {source!r} is not a component name")
	unknown = sorted(set(ids) - set(IDS))
	if unknown:
		raise EventError(f"eventlog: unknown id fields {unknown}; put other fields in data")
	data = dict(data or {})
	missing = [k for k in entry["data"] if k not in data]
	if missing:
		raise EventError(f"eventlog: event {event!r} needs data keys {missing}")
	line = {"schema": statefile.kind(KIND)["schema"], "t": clock.isoNow(), "event": event, "source": source}
	line.update({k: v for k, v in ids.items() if v is not None})
	line["data"] = data
	return line


def emit(root, event, source, data=None, **ids):
	"""Append one event to the checkout's stream; returns the line."""
	line = record(event, source, data, **ids)
	statefile.write(root, KIND, line)
	return line


def read(root, event=None, source=None):
	"""The checkout's events of the current schema, oldest first, optionally of one event name or source."""
	lines = statefile.read(root, KIND)
	return [e for e in lines if (event is None or e["event"] == event) and (source is None or e["source"] == source)]

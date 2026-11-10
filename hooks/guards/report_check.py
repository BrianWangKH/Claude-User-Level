"""PreToolUse(SubagentHandback) [the report-schema roles]: the report must start with its header.

The header (report-schema.json, parsed by report_schema.py) is what the hooks read: flow-next.py decides the
next step from it, checkpoint_status.py and agent_guard.py read STATUS and VERDICT. A hand-back whose header
is missing or invalid is denied with the problems and the role's template, so the same agent fixes it at
once (the mechanism crap-gate uses for the developer). After `max_denials` denials of one agent the hand-back
goes through (a stderr note): the orchestrator then can't read it either and stops for the person, instead of
the agent looping. Roles outside the schema and the main session are ignored. It is the first step of
hook-entry.py's `handback` entry: a hand-back it denies never reaches crap-gate or mutation.

Reads: the hook input, report-schema.json, report-check/<agent_id>.json.
Writes: report-check/<agent_id>.json (the denial count).
Output: check(data) returns the PreToolUse deny, or None.
Fails: closed — an exception denies the hand-back (hook-entry.py).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio
import hookonly
import report_schema


def check(data):
	role, agentId = data.get("agent_type"), data.get("agent_id")
	if data.get("tool_name") != "SubagentHandback" or not role or not agentId:
		return None
	schema = report_schema.load()
	if role not in schema["roles"]:
		return None
	message = (data.get("tool_input") or {}).get("message")
	problems = report_schema.check(role, message if isinstance(message, str) else "", schema)
	directory = hookonly.stateDir(hookonly.projectRoot(data), "report-check")
	counter = os.path.join(directory, f"{agentId}.json")
	if not problems:
		if os.path.exists(counter):
			os.remove(counter)
		return None
	try:
		with open(counter, encoding="utf-8") as f:
			count = json.load(f)["denials"]
	except (OSError, ValueError, KeyError):
		count = 0
	if count >= int(schema.get("max_denials", 2)):
		print(f"report-check: {role} {agentId} still lacks a valid header after {count} denials; let through: "
			+ "; ".join(problems), file=sys.stderr)
		return None
	os.makedirs(directory, exist_ok=True)
	with open(counter, "w", encoding="utf-8") as f:
		json.dump({"denials": count + 1}, f)
	reason = ("report-check: your report must start with these header lines, one per line, before anything "
		f"else (the hooks read only them):\n\n{report_schema.template(role, schema)}\n\nProblems: "
		+ "; ".join(problems) + "\nThen hand back your full report again, the header first.")
	return hookio.refuse("PreToolUse", reason)


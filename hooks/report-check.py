#!/usr/bin/env python3
"""PreToolUse SubagentHandback (flow agents' frontmatter): the report must start with its header.

The header (report-schema.json, parsed by report_schema.py) is what the hooks read: flow-next.py decides the
next step from it, checkpoint-status.py and agent-guard.py read STATUS and VERDICT. A hand-back whose header
is missing or invalid is denied with the problems and the role's template, so the same agent fixes it at
once (the mechanism crap-gate uses for the developer). After `max_denials` denials of one agent the hand-back
goes through (a stderr note): the orchestrator then can't read it either and stops for the person, instead of
the agent looping. Roles outside the schema and the main session are ignored. Fails open (stderr).
"""
import json
import os
import sys

import hookonly
import report_schema


def main():
	data = json.load(sys.stdin)
	role, agentId = data.get("agent_type"), data.get("agent_id")
	if data.get("tool_name") != "SubagentHandback" or not role or not agentId:
		return
	schema = report_schema.load()
	if role not in schema["roles"]:
		return
	message = (data.get("tool_input") or {}).get("message")
	problems = report_schema.check(role, message if isinstance(message, str) else "", schema)
	directory = hookonly.stateDir(hookonly.projectRoot(data), "report-check")
	counter = os.path.join(directory, f"{agentId}.json")
	if not problems:
		if os.path.exists(counter):
			os.remove(counter)
		return
	try:
		with open(counter, encoding="utf-8") as f:
			count = json.load(f)["denials"]
	except (OSError, ValueError, KeyError):
		count = 0
	if count >= int(schema.get("max_denials", 2)):
		print(f"report-check: {role} {agentId} still lacks a valid header after {count} denials; let through: "
			+ "; ".join(problems), file=sys.stderr)
		return
	os.makedirs(directory, exist_ok=True)
	with open(counter, "w", encoding="utf-8") as f:
		json.dump({"denials": count + 1}, f)
	reason = ("report-check: your report must start with these header lines, one per line, before anything "
		f"else (the hooks read only them):\n\n{report_schema.template(role, schema)}\n\nProblems: "
		+ "; ".join(problems) + "\nThen hand back your full report again, the header first.")
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
		"permissionDecisionReason": reason}}))


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"report-check error (not checked): {error}", file=sys.stderr)
	sys.exit(0)

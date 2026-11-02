"""The report header of flow agents (report-schema.json): parse and check. Shared by report-check.py, which
enforces it at hand-back, and flow-next.py, which reads only these fields."""
import json
import os
import re

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report-schema.json")
FIELD_LINE = re.compile(r"^\s*([A-Z][A-Z-]*):[ \t]*(.*?)\s*$")


def load():
	with open(SCHEMA_FILE, encoding="utf-8") as f:
		return json.load(f)


def header(text, schema=None):
	"""{FIELD: [values]} from the leading field lines (blank lines before them are skipped); a line that isn't
	a known field ends the header."""
	known = (schema or load())["fields"]
	out = {}
	started = False
	for line in (text or "").splitlines():
		if not line.strip():
			if started:
				break
			continue
		match = FIELD_LINE.match(line)
		if not match or match.group(1) not in known:
			break
		started = True
		out.setdefault(match.group(1), []).append(match.group(2))
	return out


def first(fields, name):
	values = fields.get(name) or []
	return values[0] if values else None


def check(role, text, schema=None):
	"""Problems with the report's header for this role ([] = fine; a role outside the schema is fine)."""
	schema = schema or load()
	rule = schema["roles"].get(role)
	if rule is None:
		return []
	fields = header(text, schema)
	problems = []
	need = list(rule.get("required", []))
	if first(fields, "STATUS") == "DONE":
		need += rule.get("when_done", [])
	for name in need:
		if name not in fields:
			problems.append(f"missing `{name}:` line")
	for name, values in fields.items():
		spec = schema["fields"][name]
		if len(values) > 1 and not spec.get("repeat"):
			problems.append(f"`{name}:` appears {len(values)} times")
		for value in values:
			if not re.match(spec["pattern"], value):
				problems.append(f"`{name}: {value}` is not allowed (expected like `{name}: {spec['example']}`)")
	changes = rule.get("when_changes")
	if changes and first(fields, "VERDICT") == "CHANGES REQUESTED":
		named = [n for n in changes if (first(fields, n) or "none") != "none"]
		if not named:
			problems.append(f"CHANGES REQUESTED needs at least one of {', '.join(f'`{n}:`' for n in changes)} naming "
				"the must-fix finding ids")
	return problems


def template(role, schema=None):
	"""The header lines a role writes, for the denial message."""
	schema = schema or load()
	rule = schema["roles"][role]
	lines = []
	for name in rule.get("required", []) + rule.get("when_done", []) + rule.get("when_changes", []) + rule.get("optional", []):
		spec = schema["fields"][name]
		note = []
		if name in rule.get("when_done", []):
			note.append("when STATUS is DONE")
		if name in rule.get("when_changes", []):
			note.append("when VERDICT is CHANGES REQUESTED; `none` if no finding of that route")
		if name in rule.get("optional", []):
			note.append("only when needed")
		if spec.get("repeat"):
			note.append("one line each")
		lines.append(f"{name}: {spec['example']}" + (f"   ({'; '.join(note)})" if note else ""))
	return "\n".join(lines)

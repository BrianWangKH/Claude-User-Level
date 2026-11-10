"""Header of any message a program reads (hooks/RULES.md R1, R2, R4): agent hand-backs, the verifier's and
flow-judge's reports, a worker's last message, the orchestrator's dispatch messages.

A header is the message's first lines, one `KEY: value` each, contiguous, before any other text (blank
lines before it are skipped). KEY is upper-case kebab (STATUS, FIX-QA); values are case-sensitive.
- parse(text): {KEY: [values]} of the header, in order; a line that isn't `KEY: value` ends it.
- value(text, key, allowed=None): the key's single value, or None when the key is missing, repeated or
  (with allowed) not one of the allowed values. A gate treats None as a refusal, never as a pass.
- leading(text): (key, value) of the header's first line when it is written exactly `KEY: value` (one space
  after the colon; trailing blanks aside), else None: the strictest reading, for a gate that may take no other
  spelling (runstate.leadStatus).
report_schema.py keeps checking flow-agent reports against report-schema.json at hand-back; this module is
the parser every other reader uses instead of its own regex or startswith.
"""

import re

LINE = re.compile(r"^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*):[ \t]*(.*?)\s*$")


def parse(text):
	fields = {}
	for line in (text or "").splitlines():
		if not line.strip():
			if fields:
				break
			continue
		match = LINE.match(line)
		if not match:
			break
		fields.setdefault(match.group(1), []).append(match.group(2))
	return fields


def leading(text):
	for line in (text or "").splitlines():
		if not line.strip():
			continue
		match = LINE.match(line)
		if not match or line.rstrip() != f"{match.group(1)}: {match.group(2)}":
			return None
		return match.group(1), match.group(2)
	return None


def value(text, key, allowed=None):
	values = parse(text).get(key) or []
	if len(values) != 1:
		return None
	if allowed is not None and values[0] not in allowed:
		return None
	return values[0]

"""Text mutants for changed lines, and the order they are run in (used by mutation.py).

Operators, applied only where the language profile's masking leaves code (never inside strings or
comments), one mutant per occurrence:
  0 comparison   <  <=  >  >=  ==  !=   (each to its boundary or negated twin)
  1 boolean      and <-> or, && <-> ||; drop a negation (not / !)
  2 delete       replace a simple statement with the profile's `delete_with` (pass / empty)
  3 literal      True <-> False, true <-> false
  4 arithmetic   " + " <-> " - " (binary, spaced)
Selection takes lines round-robin, each line's highest-priority mutant first, so a cap of N spreads
over N lines before taking a second mutant from any line.
"""
import re

import qg_complexity

COMPARISON = re.compile(r"(?<![<>=!:\-])(<=|>=|==|!=|<|>)(?![=<>\-])")
SWAP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "=="}
NO_DELETE_START = re.compile(r"^(def|async def|class|func|if|elif|else|for|while|try|except|finally|with|match|case|default|switch|select|@|\}|\)|\]|pass$|\.\.\.$)")


def _sub(line, start, end, text):
	return line[:start] + text + line[end:]


def lineMutants(raw, masked, profile, depthBefore):
	"""Mutants of one line: [(priority, op, mutated line)]."""
	out = []
	for m in COMPARISON.finditer(masked):
		out.append((0, f"{m.group(1)} -> {SWAP[m.group(1)]}", _sub(raw, m.start(), m.end(), SWAP[m.group(1)])))
	for a, b in profile.get("bool_swap") or []:
		for old, new in ((a, b), (b, a)):
			pattern = rf"\b{re.escape(old)}\b" if old.isalpha() else re.escape(old)
			for m in re.finditer(pattern, masked):
				out.append((1, f"{old} -> {new}", _sub(raw, m.start(), m.end(), new)))
	if profile.get("negation"):
		for m in re.finditer(profile["negation"], masked):
			out.append((1, f"drop {m.group(0).strip()}", _sub(raw, m.start(), m.end(), "")))
	stripped = masked.strip()
	if (stripped and depthBefore == 0 and qg_complexity.bracketDelta(masked) == 0
			and not stripped.endswith(profile.get("block_open") or "\0") and not NO_DELETE_START.match(stripped)
			and not stripped.endswith(("\\", ",", "("))):
		indent = raw[:len(raw) - len(raw.lstrip())]
		replacement = profile.get("delete_with") or ""
		if raw.strip() != replacement:
			out.append((2, "delete statement", indent + replacement))
	for a, b in profile.get("literals") or []:
		for old, new in ((a, b), (b, a)):
			for m in re.finditer(rf"\b{re.escape(old)}\b", masked):
				out.append((3, f"{old} -> {new}", _sub(raw, m.start(), m.end(), new)))
	for old, new in ((" + ", " - "), (" - ", " + ")):
		for m in re.finditer(re.escape(old), masked):
			out.append((4, f"{old.strip()} -> {new.strip()}", _sub(raw, m.start(), m.end(), new)))
	return out


def fileMutants(path, text, lines, profile, skip=()):
	"""Mutants for the given 1-based line numbers of one file, skipping lines in skip."""
	rawLines = text.splitlines()
	maskedLines = qg_complexity.mask(text, profile).splitlines()
	depth, depths = 0, []
	for masked in maskedLines:
		depths.append(depth)
		depth = max(0, depth + qg_complexity.bracketDelta(masked))
	out = []
	for number in sorted(lines):
		if number in skip or number > len(rawLines):
			continue
		raw, masked = rawLines[number - 1], maskedLines[number - 1]
		for priority, op, mutated in lineMutants(raw, masked, profile, depths[number - 1]):
			if mutated != raw:
				out.append({"file": path, "line": number, "priority": priority, "op": op,
					"original": raw, "mutated": mutated})
	return out


def select(mutants, limit):
	"""Round-robin over lines (ordered by their best priority), best mutant of each line first."""
	groups = {}
	for mutant in mutants:
		groups.setdefault((mutant["file"], mutant["line"]), []).append(mutant)
	ordered = sorted(groups.values(), key=lambda g: (min(m["priority"] for m in g), g[0]["file"], g[0]["line"]))
	for group in ordered:
		group.sort(key=lambda m: m["priority"])
	chosen, round_ = [], 0
	while len(chosen) < limit and any(round_ < len(g) for g in ordered):
		for group in ordered:
			if round_ < len(group) and len(chosen) < limit:
				chosen.append(group[round_])
		round_ += 1
	return chosen


def applyManual(text, spec):
	"""Mutated text for one manual mutant {line, find, replace}, or (None, why not)."""
	lines = text.splitlines(keepends=True)
	number = int(spec["line"])
	if not 1 <= number <= len(lines):
		return None, f"line {number} out of range"
	line = lines[number - 1]
	if line.count(spec["find"]) != 1:
		return None, f"`{spec['find']}` occurs {line.count(spec['find'])} times on line {number}, needs exactly 1"
	lines[number - 1] = line.replace(spec["find"], spec["replace"], 1)
	return "".join(lines), None

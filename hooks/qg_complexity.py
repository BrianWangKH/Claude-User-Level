"""Cyclomatic complexity (CCN) per function: {name, start, end, ccn}, lines 1-based and inclusive.

One counting rule for every engine: CCN = 1 + decision points, where a decision point is
  - if / elif, ternary if, comprehension if               (Python IfExp, comprehension.ifs)
  - for / while loops, comprehension for                  (Go: for, range)
  - except clauses                                        (Python)
  - case clauses of match / switch / select               (Go: default not counted)
  - each boolean operator: and / or, && / ||              (Python BoolOp counts len(values) - 1)
Nested functions and classes are separate entries and don't count toward the enclosing function;
lambdas and Go function literals do. Names are qualified with their enclosing classes and functions
("Class.method", "outer.inner"); Go methods as "Receiver.Name". start is the def / func line
(decorators excluded), matching coverage.py's FN records.

Engines: Python uses the standard library `ast`; Go runs qg_goccn.go (go/ast) through `go run`; any
other language in qg_langs.json uses the generic engine: blank out comments and strings, find
function headers with the profile's regex, take the body by indentation or braces, count the
profile's decision keywords and boolean operators. Its accuracy is tested against the two parsers.
"""
import ast
import json
import os
import re
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GO_HELPER = os.path.join(HERE, "qg_goccn.go")


class PythonCounter(ast.NodeVisitor):
	def __init__(self):
		self.count = 1

	def visit_FunctionDef(self, node):
		pass

	visit_AsyncFunctionDef = visit_FunctionDef
	visit_ClassDef = visit_FunctionDef

	def visit_If(self, node):
		self.count += 1
		self.generic_visit(node)

	visit_IfExp = visit_For = visit_AsyncFor = visit_While = visit_ExceptHandler = visit_match_case = visit_If

	def visit_comprehension(self, node):
		self.count += 1 + len(node.ifs)
		self.generic_visit(node)

	def visit_BoolOp(self, node):
		self.count += len(node.values) - 1
		self.generic_visit(node)


def pythonFunctions(text):
	out = []

	def walk(node, prefix):
		for child in ast.iter_child_nodes(node):
			if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
				counter = PythonCounter()
				for statement in child.body:
					counter.visit(statement)
				name = prefix + child.name
				out.append({"name": name, "start": child.lineno, "end": child.end_lineno, "ccn": counter.count})
				walk(child, name + ".")
			elif isinstance(child, ast.ClassDef):
				walk(child, prefix + child.name + ".")
			else:
				walk(child, prefix)

	walk(ast.parse(text), "")
	return out


def goFunctions(texts, goBin):
	"""{path: functions} for several Go sources at once (one `go run`)."""
	if not texts:
		return {}
	with tempfile.TemporaryDirectory() as tmp:
		names = {}
		for i, (path, text) in enumerate(texts.items()):
			name = os.path.join(tmp, f"{i}_{os.path.basename(path)}")
			with open(name, "w", encoding="utf-8") as f:
				f.write(text)
			names[name] = path
		result = subprocess.run([goBin, "run", GO_HELPER], input=json.dumps(list(names)), cwd=tmp,
			capture_output=True, text=True, timeout=300)
		if result.returncode != 0:
			raise RuntimeError(f"qg_goccn failed: {result.stderr.strip()[-500:]}")
		out = {path: [] for path in texts}
		for fn in json.loads(result.stdout):
			out[names[fn.pop("file")]].append(fn)
		return out


def mask(text, profile):
	"""Text with comment and string contents replaced by spaces (newlines kept)."""
	out, i, n = list(text), 0, len(text)
	line, block = profile.get("line_comment"), profile.get("block_comment")
	quotes = sorted(profile.get("strings") or [], key=len, reverse=True)
	raw, escape = set(profile.get("raw_strings") or []), profile.get("escape")

	def blank(a, b):
		for k in range(a, b):
			if out[k] != "\n":
				out[k] = " "

	while i < n:
		if line and text.startswith(line, i):
			end = text.find("\n", i)
			end = n if end < 0 else end
			blank(i, end)
			i = end
			continue
		if block and text.startswith(block[0], i):
			end = text.find(block[1], i + len(block[0]))
			end = n if end < 0 else end + len(block[1])
			blank(i, end)
			i = end
			continue
		quote = next((q for q in quotes if text.startswith(q, i)), None)
		if quote:
			j = i + len(quote)
			while j < n and not text.startswith(quote, j):
				if escape and quote not in raw and text[j] == escape:
					j += 1
				elif len(quote) == 1 and quote not in raw and text[j] == "\n":
					break
				j += 1
			end = min(n, j + len(quote))
			blank(i + len(quote), min(j, n))
			i = end
			continue
		i += 1
	return "".join(out)


def indentOf(line):
	return len(line) - len(line.lstrip())


def bracketDelta(line):
	return sum(line.count(c) for c in "([{") - sum(line.count(c) for c in ")]}")


def indentEnd(lines, start):
	"""Last line (0-based) of the indented block whose header starts at lines[start]."""
	base, depth, i = indentOf(lines[start]), 0, start
	while True:
		depth += bracketDelta(lines[i])
		if depth <= 0 or i + 1 >= len(lines):
			break
		i += 1
	end = i
	for j in range(i + 1, len(lines)):
		if not lines[j].strip():
			continue
		if indentOf(lines[j]) <= base and depth <= 0:
			break
		depth += bracketDelta(lines[j])
		end = j
	return end


def braceEnd(lines, start):
	depth, opened = 0, False
	for i in range(start, len(lines)):
		for char in lines[i]:
			if char == "{":
				depth += 1
				opened = True
			elif char == "}":
				depth -= 1
				if opened and depth == 0:
					return i
	return len(lines) - 1


def countTokens(text, profile):
	count = 0
	for word in profile.get("decisions") or []:
		count += len(re.findall(rf"\b{re.escape(word)}\b", text))
	for op in profile.get("bool_ops") or []:
		pattern = rf"\b{re.escape(op)}\b" if op.isalpha() else re.escape(op)
		count += len(re.findall(pattern, text))
	return count


def genericFunctions(text, profile):
	lines = mask(text, profile).splitlines()
	funcRe = re.compile(profile["function"])
	classRe = re.compile(profile["class"]) if profile.get("class") else None
	endOf = indentEnd if profile.get("body") == "indent" else braceEnd
	blocks = []
	for i, line in enumerate(lines):
		if (match := funcRe.match(line)):
			blocks.append({"kind": "function", "name": match.group(1), "start": i, "end": endOf(lines, i)})
		elif classRe and (match := classRe.match(line)):
			blocks.append({"kind": "class", "name": match.group(1), "start": i, "end": endOf(lines, i)})
	out = []
	for block in blocks:
		if block["kind"] != "function":
			continue
		inner = [b for b in blocks if b is not block and block["start"] < b["start"] <= block["end"]]
		outer = [b for b in blocks if b is not block and b["start"] < block["start"] <= b["end"]]
		own = [l for k, l in enumerate(lines[block["start"]:block["end"] + 1], block["start"])
			if not any(b["start"] <= k <= b["end"] for b in inner)]
		name = ".".join([b["name"] for b in sorted(outer, key=lambda b: b["start"])] + [block["name"]])
		out.append({"name": name, "start": block["start"] + 1, "end": block["end"] + 1,
			"ccn": 1 + countTokens("\n".join(own), profile)})
	return out


def markedLines(text, profile):
	"""Lines an exclusion marker covers (1-based). Empty when the coverage tool applies its own (marker_native)."""
	if not profile.get("marker") or profile.get("marker_native"):
		return set()
	marker = re.compile(profile["marker"])
	lines = text.splitlines()
	masked = mask(text, {**profile, "line_comment": None}).splitlines()
	out = set()
	for i, line in enumerate(lines):
		if marker.search(line):
			code = masked[i].split(profile["line_comment"])[0].rstrip() if profile.get("line_comment") else masked[i]
			end = braceEnd(lines, i) if code.endswith("{") else i
			out.update(range(i + 1, end + 2))
	return out


def measure(lang, texts, langs, goBin="go"):
	"""{path: functions} for {path: source text} of one language."""
	if lang == "python":
		return {path: pythonFunctions(text) for path, text in texts.items()}
	if lang == "go":
		return goFunctions(texts, goBin)
	return {path: genericFunctions(text, langs[lang]) for path, text in texts.items()}

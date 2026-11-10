"""Hand-kept copies of a settings file in code that can't read it at run time (the wt-pane mod's fallback
DEFAULTS): a block between
	// mirror:begin <NAME> <source, relative to the file>
	const <NAME>: <Type> = <the source's JSON>
	// mirror:end <NAME>
in a mod's TypeScript. tests/test_mirrors.py fails while a block's JSON differs from its source (the source's
"_" keys left out); config-inventory.py doesn't count a block's lines as a second copy of a settings value.
"""

import json
import re
from pathlib import Path

BEGIN = re.compile(r"^\s*// mirror:begin (\S+) (\S+)\s*$")
END = "// mirror:end "
GLOBS = ("mods/**/*.ts", "mods/**/*.tsx")


def blocks(root):
	"""[{"path", "name", "source", "first", "last", "text"}]: first/last are the 1-based marker lines."""
	root = Path(root)
	found = []
	for pattern in GLOBS:
		for path in sorted(root.glob(pattern)):
			if "node_modules" in path.parts:
				continue
			lines = path.read_text(encoding="utf-8").split("\n")
			for i, line in enumerate(lines):
				match = BEGIN.match(line)
				if not match:
					continue
				name, source = match.groups()
				end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == END + name), None)
				if end is None:
					raise ValueError(f"{path}: mirror {name} has no end marker")
				found.append(
					{
						"path": path.relative_to(root).as_posix(),
						"name": name,
						"source": (path.parent / source).resolve(),
						"first": i + 1,
						"last": end + 1,
						"text": "\n".join(lines[i + 1 : end]),
					}
				)
	return found


def value(block):
	"""The JSON value of a block's `const <NAME>: <Type> = ...`."""
	text = block["text"].strip()
	return json.loads(text[text.index("=") + 1 :])


def sourceValue(block):
	data = json.loads(Path(block["source"]).read_text(encoding="utf-8"))
	return {k: v for k, v in data.items() if not k.startswith("_")} if isinstance(data, dict) else data


def mirroredLines(root):
	"""{file: set of 1-based line numbers} of every block, markers included."""
	out = {}
	for block in blocks(root):
		out.setdefault(block["path"], set()).update(range(block["first"], block["last"] + 1))
	return out

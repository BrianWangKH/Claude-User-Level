"""Reads a coverage.py data file for mutation.py, run by the project's own Python (the hook's Python has
no coverage package): python qg_cov_contexts.py <data file> <want.json> <out.json>

want.json: {"root": <checkout the tests ran in>, "files": {<relative path>: [line, ...]}}.
out.json:  {<relative path>: {"executed": [line, ...], "contexts": {"<line>": [context, ...]}}} for the
wanted lines only. A context is pytest-cov's "<node id>|setup|run|teardown"; "" is code run with no test
context (a subprocess the coverage patch measured, or import time). Uses coverage's public API
(CoverageData.measured_files, lines, contexts_by_lineno)."""
import json
import os
import sys

from coverage import CoverageData


def main():
	dataFile, wantFile, outFile = sys.argv[1:4]
	with open(wantFile, encoding="utf-8") as f:
		want = json.load(f)
	root = os.path.realpath(want["root"])
	data = CoverageData(basename=dataFile)
	data.read()
	out = {}
	for measured in data.measured_files():
		rel = os.path.relpath(os.path.realpath(measured), root)
		lines = want["files"].get(rel)
		if lines is None:
			continue
		wanted = set(lines)
		executed = sorted(n for n in (data.lines(measured) or []) if n in wanted)
		contexts = data.contexts_by_lineno(measured)
		out[rel] = {"executed": executed,
			"contexts": {str(n): sorted(contexts.get(n) or []) for n in executed}}
	with open(outFile, "w", encoding="utf-8") as f:
		json.dump(out, f)


if __name__ == "__main__":
	main()

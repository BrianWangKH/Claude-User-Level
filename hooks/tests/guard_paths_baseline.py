"""How guard-paths entries were matched before the shared glob rule, kept as test data (skill refactor, section 2).

Never imported by a program. Transcribed from the code before the change:
- hooks/guards/agent_guard.py loadGuardPaths and matches: every entry lost its trailing "/", then a path matched
  an entry when it was the entry itself or below it; a glob was compared as a literal path.
- hooks/lib/qg_common.py pathMatches and globRegex: "dir/" a prefix (checked before the glob test, so a glob with
  a trailing "/" was a literal prefix too), a pattern with * or ? a glob over the whole path, else exact or prefix.
"""

import re


def oldAgentGuardMatches(path, rawEntries):
	entries = [str(e).rstrip("/") for e in rawEntries]
	return any(path == e or path.startswith(e + "/") for e in entries)


def oldGlobRegex(pattern):
	out, i = "", 0
	while i < len(pattern):
		if pattern.startswith("**/", i):
			out += "(?:.*/)?"
			i += 3
		elif pattern.startswith("**", i):
			out += ".*"
			i += 2
		elif pattern[i] == "*":
			out += "[^/]*"
			i += 1
		elif pattern[i] == "?":
			out += "[^/]"
			i += 1
		else:
			out += re.escape(pattern[i])
			i += 1
	return re.compile(out + r"\Z")


def oldPathMatches(path, pattern):
	if pattern.endswith("/"):
		return path.startswith(pattern)
	if "*" in pattern or "?" in pattern:
		return oldGlobRegex(pattern).match(path) is not None
	return path == pattern or path.startswith(pattern + "/")

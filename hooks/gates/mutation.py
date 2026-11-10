#!/usr/bin/env python3
"""Mutation testing for the code reviewer: the hook runs the mutants, the reviewer only judges.

Events (all for agent_type code-reviewer; a mutate-request of any other caller is refused, every other event of
theirs returns at once):
- PreToolUse on any tool, run as `mutation.py start`, started detached by hook-entry.py's `pre` entry (step
  mutation-start, only when no earlier step denied the call): the automatic scan, started by the
  reviewer's first tool call (later calls find it running or done and return at once). Scratch copy of the checkout, the
  coverage command as baseline (must pass; {changed_dirs} filled with the changed files' directories,
  qg_common.measureCoverage), then text mutants (qg_mutate) on changed lines that the
  baseline executed and no exclusion marker covers, mutation_max of them, each run with the test
  command and a timeout of max(baseline x factor, baseline + min_extra). Result:
  .hook-only/hook-read-only/mutation/auto-<fingerprint>.json. Prints nothing.
  A language with "select" (kind pytest-contexts) narrows the work: the baseline runs its tests with
  per-test coverage contexts over the changed files' directories only, each mutant runs only the tests
  that executed its line (one with no known test runs the "fallback" tests), with the whole test
  command's accuracy: every survivor is rerun with it, and a catch counts only when the same selection
  passes on the unmutated code (else the whole test command decides); a later scan where only
  test files changed (guard-paths tests, named like tests) keeps the invalid mutants and the caught ones
  whose catching test file is unchanged, and reruns the rest on their tests plus the changed files.
- PreToolUse Bash `mutate-request <spec.json>` (sync, plain `mutation.py`; its own settings registration,
  `if: Bash(mutate-request *)`): the reviewer's own
  mutants, a JSON list of {"file", "line", "find", "replace"} (at most manual_max). Run in a fresh
  scratch copy; the call is denied with the result table as the reason (that is the answer). Run like
  the scan's mutants (runOne): a language with "select" gets one context baseline over the mutated
  lines and each mutant runs only the tests that executed its line, with the same catch and survivor
  rules; without "select", a baseline of the test command and every mutant on it. The reviewer's Bash
  call is held meanwhile, so the wait is marked (markWait, limit hook_timeouts) and taken out of
  its time budget (excludeWait), as at the hand-back. Without both, the reviewer's
  minutes run on for 10+ min with nothing shown, and it is stopped on time.
- PreToolUse SubagentHandback (sync; hook-entry.py's `handback` entry, after report_check.py): waits for the automatic scan of the current fingerprint (or runs
  it if none is running), then, once per reviewer and fingerprint, denies the hand-back with the
  survivors and any failing baseline, so the report the orchestrator gets already holds the judgement.
  The wait is taken out of the reviewer's time budget (budget_state.excludeWait, in a finally); while it lasts,
  budget_state.markWait marks it in the budget state so status displays show waiting, not overrun. A hook
  Claude Code cancels at its timeout runs no finally: subagent_budget.py settles the mark it leaves
  (budget_state.releaseOrphanWait). At Stop after a
  hand-back the report was already out: the reviewer "finished twice", and in `claude -p` the second
  finish (scan ~10 min) came after the orchestrator's last turn.
- SubagentStop (hook-entry.py's `stop` entry): the same, as a block, for a reviewer that ends without a hand-back (and a no-op
  once the hand-back gate delivered for this fingerprint).
Every outcome of that delivery point is also written, counts only, to
.hook-only/quality/mutation-<agent_id>-<fingerprint>.json, which sessions can read (hook-read-only
can't be): all caught (nothing to judge, so the reviewer hears nothing), survivors delivered, no
result within mutation_wait_seconds, or no sandbox. Without that, the all-caught and no-result
cases leave no trace anyone can read, and a review without survivors looks like a scan that never
reached the reviewer. No result within the wait now denies the hand-back once too, so
the report says the scan didn't finish instead of saying nothing.
A review whose dispatch prompt starts with "Merge review:" (dispatch-guard leaves merge-review-pending)
gets no automatic scan; manual mutants still run. The flag is claimed by that reviewer's first start
call or its stop (merge-<agent_id>); seen-<agent_id> marks a reviewer that already passed that point.

Status: caught (tests failed), timeout (counts as caught), survived (tests passed), invalid (the
mutant doesn't build or breaks collection: the language's invalid_exit_codes / invalid_patterns, or
a Python syntax error). State and results live in hook-read-only: agents can't read them, the reviewer
gets only what the hook returns. Config: qg_common.loadConfig (without .claude/quality-gates.json the
languages detected from the tracked files run on their home profiles; nothing to do only when there is
nothing to gate). Fails: closed on PreToolUse — any exception exits 2 with the error (hookrun.py), which
denies the hand-back or the mutate-request call; open at SubagentStop and in the start mode (stderr). Besides,
where a gate would otherwise say nothing:
- a baseline command that never starts (qg_common.NotStarted) or writes no coverage report is a failed
  baseline whose line names the command's key and where its value comes from (qg_common.commandHint), and
  a mutant run that never starts ends the language's mutants the same way: never a catch;
- a config that doesn't load (homeconfig.ConfigError: a tool a command needs is missing) holds the
  reviewer's hand-back once (Stop: blocks once) with the error, as a missing sandbox does;
- "select" that can't be used (its context run doesn't start or fails, or the data holds no per-test
  contexts: pytest-cov before 7.1) falls back to the whole test command per mutant (selectBaseline), and
  the result's "select_fallback" lines say so; they reach the reviewer with the survivors, and the
  delivery summary, and a reviewer's own mutants get the same line.
A language enabled automatically gets a line saying so in what reaches the reviewer.
"""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import budget_state
import hookonly
import qg_common
import qg_complexity
import qg_mutate

AGENT = "code-reviewer"
STALE_SECONDS = 7 * 86400
MERGE_FLAG = "merge-review-pending"
SELECT_KIND = "pytest-contexts"
PYTEST_FILES = ("test_*.py", "*_test.py")  # pytest's default python_files
NO_TESTS = 5  # pytest's exit code when nothing was collected
CAUGHT_BY = re.compile(r"^FAILED (.+?)(?: - .*)?$", re.M)


def mutationDir(root):
	directory = hookonly.stateDir(root, "mutation", strict=True)
	os.makedirs(directory, exist_ok=True)
	return directory


def copyCheckout(root, dest, excludes):
	"""Scratch copy without excluded paths ("dir/" = from the root, a bare name = anywhere). The hooks' own state
	(.hook-only, hookonly.NAME) is always left out: never test input, and other hooks rewrite it meanwhile, so
	a repo without scratch_exclude (a config of detected languages alone) can't break the copy."""
	anchored = [e.rstrip("/") for e in excludes if e.endswith("/")] + [hookonly.NAME]
	names = {e for e in excludes if not e.endswith("/")}

	def ignore(directory, entries):
		rel = os.path.relpath(directory, root)
		skip = []
		for entry in entries:
			path = entry if rel == "." else os.path.join(rel, entry)
			if entry in names or path in anchored:
				skip.append(entry)
		return skip

	shutil.copytree(root, dest, ignore=ignore, symlinks=True)


def classify(code, tail, langConfig):
	if code is None:
		return "timeout"
	if code == 0:
		return "survived"
	if code in (langConfig.get("invalid_exit_codes") or []) or any(p in tail for p in langConfig.get("invalid_patterns") or []):
		return "invalid"
	return "caught"


def runMutant(scratch, repo, path, mutatedText, langConfig, timeout, protect=(), argv=None):
	"""(status, exit code, output tail) of argv (default: the language's test command) on the mutant."""
	target = os.path.join(scratch, path)
	with open(target, encoding="utf-8") as f:
		original = f.read()
	if path.endswith(".py"):
		try:
			compile(mutatedText, path, "exec")
		except SyntaxError:
			return "invalid", None, ""
	try:
		with open(target, "w", encoding="utf-8") as f:
			f.write(mutatedText)
		mapping = {"repo": repo, "root": scratch}
		code, _, tail = qg_common.runCommand(argv or qg_common.expand(langConfig["test"], mapping),
			os.path.join(scratch, langConfig.get("cwd", "")), qg_common.commandEnv(langConfig, mapping), timeout,
			writable=[scratch], readable=[repo], protect=protect)
		return classify(code, tail, langConfig), code, tail
	finally:
		with open(target, "w", encoding="utf-8") as f:
			f.write(original)


def selectConfig(langConfig):
	"""The language's test selection ("select" with kind pytest-contexts), or None: every mutant runs the
	whole test command."""
	sel = langConfig.get("select")
	return sel if isinstance(sel, dict) and sel.get("kind") == SELECT_KIND else None


def withoutSelect(langConfig):
	return {k: v for k, v in langConfig.items() if k != "select"}


def hasTestContexts(info):
	"""False when executed lines exist but none ran under a test context (the data holds none)."""
	lineContexts = [contexts for entry in info.values() for contexts in (entry.get("contexts") or {}).values()]
	return not lineContexts or any(context for contexts in lineContexts for context in contexts)


def selectBaseline(scratch, root, langConfig, sel, files, tmp, limits, protect):
	"""(info, seconds, why): contextBaseline, and why its result can't drive the selection (None when it can):
	its command didn't start, it failed (maybe only the selection: the caller's whole-command baseline tells a
	red suite apart), or the coverage data holds no per-test contexts."""
	try:
		info, code, seconds, tail = contextBaseline(scratch, root, langConfig, sel, files, tmp, limits, protect)
	except qg_common.NotStarted as error:
		return None, 0, f"its command didn't start ({error})"
	if info is None:
		return None, seconds, f"its context run failed (exit {code}: {qg_common.lastLines(tail, 2)})"
	if not hasTestContexts(info):
		return None, seconds, "the coverage data holds no per-test contexts (pytest-cov 7.1+ needed)"
	return info, seconds, None


def fallbackNote(lang, why):
	return f"{lang}: test selection not used, {why}; every mutant ran the whole test command"


def contextBaseline(scratch, root, langConfig, sel, files, tmp, limits, protect):
	"""Baseline of a language with "select": its tests under coverage with per-test contexts, measuring only
	the directories of the changed files (the mutants' files), then qg_cov_contexts.py in the project's own
	Python. (info or None, exit code, seconds, tail); info: {path: {"executed", "contexts"}} for the
	changed lines."""
	out = tempfile.mkdtemp(prefix="cov-", dir=tmp)
	mapping = {"repo": root, "root": scratch}
	cwd = os.path.join(scratch, langConfig.get("cwd", ""))
	env = qg_common.commandEnv(langConfig, mapping)
	env["COVERAGE_FILE"] = os.path.join(out, ".coverage")
	targets = qg_common.changedDirs(scratch, cwd, files)
	argv = (pytestArgv(sel, root, scratch, langConfig) + ["--cov-branch", "--cov-context=test", "--cov-report="]
		+ [f"--cov={target}" for target in targets])
	code, seconds, tail = qg_common.runCommand(argv, cwd, env, limits["coverage_timeout_seconds"],
		writable=[scratch, out], readable=[root], protect=protect)
	if code != 0:
		return None, code, seconds, tail
	helper = os.path.join(out, "qg_cov_contexts.py")
	shutil.copy(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib", "qg_cov_contexts.py"), helper)
	want, report = os.path.join(out, "want.json"), os.path.join(out, "contexts.json")
	with open(want, "w", encoding="utf-8") as f:
		json.dump({"root": scratch, "files": {path: sorted(lines) for path, lines in files.items()}}, f)
	readCode, _, readTail = qg_common.runCommand(qg_common.expand(sel["python"], mapping) + [helper, env["COVERAGE_FILE"],
		want, report], cwd, env, limits["coverage_timeout_seconds"], writable=[scratch, out], readable=[root],
		protect=protect)
	if readCode != 0 or not os.path.isfile(report):
		return None, readCode if readCode else 1, seconds, "reading coverage contexts: " + readTail
	return qg_common.loadJson(report), 0, seconds, tail


def pytestArgv(sel, root, scratch, langConfig):
	"""The selection's pytest command, rooted at its working directory: node ids (contexts, -x output) are
	relative to pytest's rootdir, which an ini file above the scratch copy could otherwise move."""
	cwd = os.path.join(scratch, langConfig.get("cwd", ""))
	return qg_common.expand(sel["pytest"], {"repo": root, "root": scratch}) + [f"--rootdir={os.path.normpath(cwd)}"]


def nodesOf(contexts):
	"""Test node ids from pytest-cov contexts ("<node id>|run"); "" (no test context) is left out."""
	return sorted({context.rsplit("|", 1)[0] for context in contexts if context})


def fallbackArgv(scratch, root, langConfig, sel, path):
	"""The tests of the mutant's component ("fallback", e.g. "{component}/tests", the path's first
	directory), or the whole test command when there is no such directory."""
	mapping = {"repo": root, "root": scratch}
	pattern = sel.get("fallback")
	if pattern:
		target = pattern.format(component=path.split("/")[0])
		if os.path.isdir(os.path.join(scratch, langConfig.get("cwd", ""), target)):
			return pytestArgv(sel, root, scratch, langConfig) + ["-x", target]
	return qg_common.expand(langConfig["test"], mapping)


def caughtBy(tail):
	match = CAUGHT_BY.search(tail)
	return match.group(1).strip() if match else None


def passesUnmutated(scratch, root, langConfig, argv, timeout, protect, unmutated):
	"""True when argv passes on the unmutated scratch copy (cached in unmutated by argv)."""
	key = tuple(argv)
	if key not in unmutated:
		code, _, _ = qg_common.runCommand(argv, os.path.join(scratch, langConfig.get("cwd", "")),
			qg_common.commandEnv(langConfig, {"repo": root, "root": scratch}), timeout,
			writable=[scratch], readable=[root], protect=protect)
		unmutated[key] = code == 0
	return unmutated[key]


def runSelected(scratch, root, mutant, mutatedText, langConfig, sel, timeout, protect, nodes, partial, unmutated):
	"""Runs only the tests that executed the mutated line (nodes; none known: the fallback tests), with the
	same verdict as the whole test command:
	- a survivor may only mean the contexts missed a test (code run at import time is credited to whichever
	  test imported the module first, code run in a subprocess to no test at all), so every survivor, and a
	  run that collected nothing, is rerun with the whole test command;
	- a catch may only mean the selected tests fail together without the mutant (one relies on state a test
	  left out of the selection left, or a test in it pollutes another), so a catch counts only when the
	  same selection passes on the unmutated code (unmutated caches the answer per selection); otherwise
	  the whole test command decides.
	partial (the line also ran with no test context) is kept for the record. Sets status, caught_by, tests
	and partial on mutant."""
	full = qg_common.expand(langConfig["test"], {"repo": root, "root": scratch})
	argv = pytestArgv(sel, root, scratch, langConfig) + ["-x", *nodes] if nodes else fallbackArgv(scratch, root, langConfig, sel, mutant["file"])
	status, code, tail = runMutant(scratch, root, mutant["file"], mutatedText, langConfig, timeout, protect, argv)
	if argv != full:
		recheck = code == NO_TESTS or status == "survived"
		if status == "caught":
			recheck = not passesUnmutated(scratch, root, langConfig, argv, timeout, protect, unmutated)
		if recheck:
			status, code, tail = runMutant(scratch, root, mutant["file"], mutatedText, langConfig, timeout, protect, full)
	mutant.update({"status": status, "caught_by": caughtBy(tail) if status == "caught" else None, "tests": nodes,
		"partial": partial})


def runOne(scratch, root, mutant, mutatedText, langConfig, timeout, protect, contexts, unmutated):
	"""Runs one mutant ({"file", "line"}) as the scan and mutate-request both do and sets its status and
	caught_by: with "select", only the tests whose coverage contexts (contexts: contextBaseline's info for
	the language) executed its line (runSelected); without, the whole test command."""
	sel = selectConfig(langConfig)
	if sel:
		lineContexts = (contexts or {}).get(mutant["file"], {}).get("contexts", {}).get(str(mutant["line"]), [])
		runSelected(scratch, root, mutant, mutatedText, langConfig, sel, timeout, protect, nodesOf(lineContexts),
			"" in lineContexts, unmutated)
	else:
		status, _, _ = runMutant(scratch, root, mutant["file"], mutatedText, langConfig, timeout, protect)
		mutant.update({"status": status, "caught_by": None})


def snapshot(root, base):
	"""{path: sha256 or None (deleted)} of every path the branch changed, untracked files included."""
	paths = {p for p in qg_common.git(root, "diff", "--name-only", "--no-renames", base, "--").splitlines() if p}
	paths |= set(qg_common.untracked(root))
	out = {}
	for path in sorted(paths):
		try:
			with open(os.path.join(root, path), "rb") as f:
				out[path] = hashlib.sha256(f.read()).hexdigest()
		except OSError:
			out[path] = None
	return out


def isTestFile(path, sel):
	name = os.path.basename(path)
	return any(fnmatch.fnmatch(name, pattern) for pattern in (sel.get("test_files") or PYTEST_FILES))


def reusable(directory, base, snap, config):
	"""(previous result, changed paths) when, since the newest clean scan of the same base, only test files
	changed: in the guard-paths tests (or qaConfig), named like tests, of a language with "select". A
	changed conftest, helper or fixture can change tests that didn't change, so it means a full scan."""
	best = None
	for name in os.listdir(directory):
		if not (name.startswith("auto-") and name.endswith(".json")):
			continue
		try:
			old = qg_common.loadJson(os.path.join(directory, name))
		except (OSError, ValueError):
			continue
		if (old.get("base") != base or "snapshot" not in old or not old.get("results") or old.get("baseline_failed")
				or old.get("sandbox_unavailable") or old.get("select_fallback")):
			continue
		if best is None or old["at"] > best["at"]:
			best = old
	if best is None:
		return None
	changed = sorted(p for p in set(snap) | set(best["snapshot"]) if snap.get(p) != best["snapshot"].get(p))
	if not changed:
		return None
	for path in changed:
		lang = qg_common.languageOf(path, config)
		sel = selectConfig(config["languages"][lang]) if lang else None
		if not (sel and qg_common.isExcluded(path, config.get("test_paths") or []) and isTestFile(path, sel)):
			return None
	return best, changed


def rescan(root, config, limits, scratch, previous, changed, result):
	"""A later scan where only test files changed: keeps what the change can't affect (invalid mutants, and
	caught ones whose catching test file is unchanged) and reruns the rest on their old tests plus the
	changed test files. The changed tests must pass unmutated first, or every rerun would count as caught."""
	protect = config.get("protect") or []
	present = {}
	for path in changed:
		if os.path.isfile(os.path.join(scratch, path)):
			present.setdefault(qg_common.languageOf(path, config), []).append(path)
	for lang, paths in present.items():
		langConfig = config["languages"][lang]
		mapping = {"repo": root, "root": scratch}
		code, _, tail = qg_common.runCommand(pytestArgv(selectConfig(langConfig), root, scratch, langConfig) + paths,
			os.path.join(scratch, langConfig.get("cwd", "")), qg_common.commandEnv(langConfig, mapping),
			limits["coverage_timeout_seconds"], writable=[scratch], readable=[root], protect=protect)
		if code not in (0, NO_TESTS):
			result["baseline_failed"].append({"lang": lang, "exit": code,
				"tail": " | ".join(tail.strip().splitlines()[-3:])[:400]})
	if result["baseline_failed"]:
		return result
	result["candidates"], result["timeouts"] = previous["candidates"], previous["timeouts"]
	changedSet, unmutated = set(changed), {}
	for old in previous["results"]:
		mutant = {k: v for k, v in old.items() if k != "reused"}
		catcher = (old.get("caught_by") or "").split("::")[0]
		if old["status"] == "invalid" or (old["status"] == "caught" and catcher and catcher not in changedSet):
			mutant["reused"] = True
			result["reused"] += 1
			result["results"].append(mutant)
			continue
		lang = mutant["lang"]
		langConfig = config["languages"][lang]
		sel = selectConfig(langConfig)
		mutated = replaceLine(qg_common.readText(scratch, mutant["file"]), mutant["line"], mutant["mutated"])
		timeout = previous["timeouts"][lang]
		if sel and "tests" in old:
			nodes = [n for n in old["tests"] if n.split("::")[0] not in changedSet] + present.get(lang, [])
			runSelected(scratch, root, mutant, mutated, langConfig, sel, timeout, protect, nodes, old.get("partial", False),
				unmutated)
		else:
			status, _, _ = runMutant(scratch, root, mutant["file"], mutated, langConfig, timeout, protect)
			mutant.update({"status": status, "caught_by": None})
		result["results"].append(mutant)
	return result


def replaceLine(text, number, newLine):
	lines = text.splitlines(keepends=True)
	ending = lines[number - 1][len(lines[number - 1].rstrip("\r\n")):]
	lines[number - 1] = newLine + ending
	return "".join(lines)


def mutantTimeout(seconds, limits):
	return max(seconds * limits["mutation_timeout_factor"], seconds + limits["mutation_timeout_min_extra_seconds"])


def scan(root, config, limits, directory):
	base = qg_common.defaultBase(root)
	changes = qg_common.sourceChanges(root, base, config)
	result = {"baseline_failed": [], "sandbox_unavailable": None, "results": [], "candidates": 0, "at": time.time(),
		"base": base, "snapshot": snapshot(root, base), "timeouts": {}, "reused": 0, "select_fallback": []}
	if not changes:
		return result
	with tempfile.TemporaryDirectory(prefix="qg-mutation-") as tmp:
		scratch = os.path.join(tmp, "repo")
		copyCheckout(root, scratch, config.get("scratch_exclude") or [])
		previous = reusable(directory, base, result["snapshot"], config)
		try:
			if previous:
				return rescan(root, config, limits, scratch, previous[0], previous[1], result)
			fullScan(root, config, limits, scratch, tmp, changes, result)
		except qg_common.SandboxError as error:
			result["sandbox_unavailable"] = str(error)[:400]
		except qg_common.NotStarted as error:
			result["baseline_failed"].append(notStartedEntry(config, "", "a command", error))
	return result


def notStartedEntry(config, lang, what, error, key="test"):
	"""A baseline_failed entry for a command that never started: never a red suite or a catch."""
	hint = f"; {qg_common.commandHint(config, lang, key)}" if lang else ""
	return {"lang": lang or "?", "exit": None, "not_started": True, "tail": f"{what} didn't start: {error}"[:400] + hint}


def coverageBaseline(scratch, root, config, lang, langConfig, tmp, limits, protect, files):
	"""(cover or None, seconds, failed entry or None): the language's coverage command on the scratch copy,
	files (the changed paths) filling {changed_dirs}. A changed file the report leaves out gets no mutants,
	like an unexecuted line; crap-gate reports it."""
	try:
		cover, code, seconds, tail = qg_common.measureCoverage(scratch, lang, langConfig, os.path.join(tmp, "work"),
			limits["coverage_timeout_seconds"], repo=root, protect=protect, files=files)
	except qg_common.NotStarted as error:
		return None, 0, notStartedEntry(config, lang, "the coverage command", error, "coverage")
	if cover is None:
		return None, seconds, {"lang": lang, "exit": code, "tail": f"no coverage report: {qg_common.lastLines(tail, 3)}; "
			+ qg_common.commandHint(config, lang, "coverage")}
	if code != 0:
		return None, seconds, {"lang": lang, "exit": code, "tail": qg_common.lastLines(tail, 3)}
	return cover, seconds, None


def languageBaseline(scratch, root, config, lang, files, tmp, limits, result):
	"""(effective language config, cover, contexts, seconds) for one language, or None after recording a failed
	baseline. A language whose "select" can't be used runs without it (selectBaseline, a fallback line)."""
	langConfig, protect = config["languages"][lang], config.get("protect") or []
	sel = selectConfig(langConfig)
	if sel:
		info, seconds, why = selectBaseline(scratch, root, langConfig, sel, files, tmp, limits, protect)
		if why is None:
			return langConfig, {p: {n: 1 for n in v["executed"]} for p, v in info.items()}, info, seconds
		result["select_fallback"].append(fallbackNote(lang, why))
		langConfig = withoutSelect(langConfig)
	cover, seconds, failed = coverageBaseline(scratch, root, config, lang, langConfig, tmp, limits, protect, files)
	if failed:
		result["baseline_failed"].append(failed)
		return None
	return langConfig, cover, None, seconds


def fullScan(root, config, limits, scratch, tmp, changes, result):
	"""Baselines, candidates and the selected mutants' runs into result (module doc)."""
	langs, protect = qg_common.loadLangs(), config.get("protect") or []
	candidates, timeouts, contexts, effective, unmutated = [], result["timeouts"], {}, {}, {}
	for lang, files in changes.items():
		baseline = languageBaseline(scratch, root, config, lang, files, tmp, limits, result)
		if baseline is None:
			continue
		effective[lang], cover, contexts[lang], seconds = baseline
		timeouts[lang] = mutantTimeout(seconds, limits)
		profile = langs.get(lang) or {}
		for path, lines in files.items():
			text = qg_common.readText(scratch, path)
			covered = {n for n in lines if cover.get(path, {}).get(n, 0) > 0}
			skip = qg_complexity.markedLines(text, profile)
			for mutant in qg_mutate.fileMutants(path, text, covered, profile, skip):
				mutant["lang"] = lang
				candidates.append(mutant)
	result["candidates"] = len(candidates)
	stopped = set()
	for mutant in qg_mutate.select(candidates, limits["mutation_max"]):
		lang = mutant["lang"]
		if lang in stopped:
			continue
		mutated = replaceLine(qg_common.readText(scratch, mutant["file"]), mutant["line"], mutant["mutated"])
		try:
			runOne(scratch, root, mutant, mutated, effective[lang], timeouts[lang], protect, contexts[lang], unmutated)
		except qg_common.NotStarted as error:
			result["baseline_failed"].append(notStartedEntry(config, lang, "a mutant run", error))
			stopped.add(lang)
			continue
		result["results"].append(mutant)


def pidAlive(pid):
	try:
		os.kill(pid, 0)
		return True
	except ProcessLookupError:
		return False
	except PermissionError:
		return True


def lockHolder(lockFile):
	try:
		with open(lockFile, encoding="utf-8") as f:
			pid = int(f.read().strip() or "0")
	except (OSError, ValueError):
		return None
	return pid if pid and pidAlive(pid) else None


def runAuto(root, config, limits, directory, fp):
	"""Runs the automatic scan for fp unless it exists or another process is running it."""
	resultFile = os.path.join(directory, f"auto-{fp}.json")
	lockFile = os.path.join(directory, f"auto-{fp}.lock")
	if os.path.isfile(resultFile):
		return
	if os.path.exists(lockFile):
		if lockHolder(lockFile):
			return
		os.remove(lockFile)
	try:
		fd = os.open(lockFile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
	except FileExistsError:
		return
	try:
		os.write(fd, str(os.getpid()).encode())
		os.close(fd)
		result = scan(root, config, limits, directory)
		tmpFile = resultFile + ".tmp"
		with open(tmpFile, "w", encoding="utf-8") as f:
			json.dump(result, f)
		os.replace(tmpFile, resultFile)
	finally:
		try:
			os.remove(lockFile)
		except FileNotFoundError:
			pass


def removeStale(directory):
	limit = time.time() - STALE_SECONDS
	for name in os.listdir(directory):
		path = os.path.join(directory, name)
		try:
			if os.path.getmtime(path) < limit:
				os.remove(path)
		except OSError:
			pass


def isMergeReview(directory, agentId):
	"""True for a merge review: claims a fresh merge-review-pending flag on the reviewer's first call."""
	mine = os.path.join(directory, f"merge-{agentId}")
	if os.path.isfile(mine):
		return True
	seen = os.path.join(directory, f"seen-{agentId}")
	if os.path.isfile(seen):
		return False
	open(seen, "w").close()
	flag = os.path.join(directory, MERGE_FLAG)
	if os.path.isfile(flag) and time.time() - os.path.getmtime(flag) < 3600:
		try:
			os.replace(flag, mine)
			return True
		except FileNotFoundError:
			return False
	return False


def onStart(data, root, config, limits, directory):
	if isMergeReview(directory, data.get("agent_id") or "unknown"):
		return
	fp = qg_common.fingerprint(root, qg_common.defaultBase(root))
	runAuto(root, config, limits, directory, fp)


def survivorReason(data, root, config, limits, directory):
	"""The survivors (and red baselines) for the reviewer to judge, once per fingerprint, or None."""
	agentId = data.get("agent_id") or "unknown"
	if isMergeReview(directory, agentId):
		return None
	fp = qg_common.fingerprint(root, qg_common.defaultBase(root))
	delivered = os.path.join(directory, f"delivered-{agentId}-{fp}")
	if os.path.isfile(delivered):
		return None
	resultFile = os.path.join(directory, f"auto-{fp}.json")
	left = qg_common.remaining()
	deadline = time.time() + min(limits["mutation_wait_seconds"], left if left is not None else float("inf"))
	while not os.path.isfile(resultFile) and time.time() < deadline:
		if lockHolder(os.path.join(directory, f"auto-{fp}.lock")):
			time.sleep(5)
		else:
			try:
				runAuto(root, config, limits, directory, fp)
			except qg_common.DeadlineReached as error:  # no result file: reported below as no result
				qg_common.deadlineNotice(root, "mutation", error)
				break
	if not os.path.isfile(resultFile):
		print(f"mutation: no automatic result for {fp} within the wait limit", file=sys.stderr)
		markDelivered(delivered)
		summarize(root, agentId, fp, "no_result", waitSeconds=limits["mutation_wait_seconds"])
		return (f"mutation: the automatic scan gave no result within {limits['mutation_wait_seconds'] / 60:g} min. "
			"Write in your report that the mutation scan didn't finish (no survivors to judge, not a pass), "
			"then give your full report again.")
	result = qg_common.loadJson(resultFile)
	markDelivered(delivered)
	if result.get("sandbox_unavailable"):
		summarize(root, agentId, fp, "sandbox_unavailable")
		return ("mutation: not run, environment problem on this machine, not the code: "
			f"{result['sandbox_unavailable']}. Write in your report that the mutation scan didn't run because bwrap "
			"is unavailable, judge no mutants, then give your full report again.")
	results = result["results"]
	survivors = [m for m in results if m["status"] == "survived"]
	counts = {s: sum(1 for m in results if m["status"] == s) for s in ("caught", "timeout", "survived", "invalid")}
	fallback = result.get("select_fallback") or []
	if not survivors and not result["baseline_failed"]:
		summarize(root, agentId, fp, "all_caught", counts=counts, candidates=result["candidates"], selectFallback=fallback)
		return None
	summarize(root, agentId, fp, "delivered", counts=counts, candidates=result["candidates"],
		baselineFailed=len(result["baseline_failed"]), selectFallback=fallback)
	lines = [baselineLine(b) for b in result["baseline_failed"]]
	lines += [f"{m['file']}:{m['line']}  {m['op']}  (line: {m['original'].strip()[:120]})" for m in survivors]
	summary = (f"mutation (automatic scan of the changed lines): {len(results)} run of {result['candidates']} candidates; "
		f"caught {counts['caught']}, timeout {counts['timeout']}, survived {counts['survived']}, invalid {counts['invalid']}."
		+ (f" {result['reused']} kept from the previous scan (only test files changed since)." if result.get("reused") else ""))
	notes = [f"note: {line}" for line in fallback] + [f"note: {qg_common.detectedNote(lang)}" for lang in config.get("detected") or []]
	return (summary + "\n" + qg_common.truncate(lines, limits["survivor_max_chars"]) + "".join("\n" + n for n in notes)
		+ "\nJudge each survivor: equivalent (no input can tell the difference; say why) or a test gap (a finding "
		"routed to QA, the mutant as its failure scenario). A failed baseline means the suite is red: report it; "
		"one that didn't run is an environment or config problem: report it as that. "
		"Add them to your mutation table, then give your full report again.")


def baselineLine(entry):
	if entry.get("not_started"):
		return f"baseline didn't run for {entry['lang']}: {entry['tail']}"
	return f"baseline failed for {entry['lang']} (exit {entry['exit']}): {entry['tail']}"


def markDelivered(path):
	with open(path, "w", encoding="utf-8") as f:
		f.write(str(time.time()))


def summarize(root, agentId, fp, status, **fields):
	"""The readable trace of one delivery point (module doc); never fails the hook."""
	try:
		work = hookonly.stateDir(root, "quality")
		os.makedirs(work, exist_ok=True)
		with open(os.path.join(work, f"mutation-{agentId}-{fp}.json"), "w", encoding="utf-8") as f:
			json.dump({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent_id": agentId, "fingerprint": fp,
				"status": status, **fields}, f)
	except OSError as error:
		print(f"mutation: summary not written ({error})", file=sys.stderr)


def onStop(data, root, config, limits, directory):
	qg_common.startDeadline("review_handback_seconds")
	reason = survivorReason(data, root, config, limits, directory)
	if reason:
		print(json.dumps({"decision": "block", "reason": reason}))


def onHandback(data, root, config, limits, directory):
	qg_common.startDeadline("review_handback_seconds")
	agentId = data.get("agent_id")
	started = time.time()
	budget_state.markWait(root, agentId, "mutation scan", qg_common.hookTimeout("review_handback_seconds"))
	try:
		reason = survivorReason(data, root, config, limits, directory)
	finally:
		budget_state.excludeWait(root, agentId, time.time() - started)
	if reason:
		print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
			"permissionDecisionReason": "mutation: hand-back held until the automatic scan is judged. " + reason}}))


def onMutateRequest(data, root, config, limits, directory):
	command = (data.get("tool_input") or {}).get("command") or ""
	try:
		words = shlex.split(command)
	except ValueError:
		return
	if not words or words[0] != "mutate-request":
		return

	def answer(text):
		print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
			"permissionDecisionReason": text}}))

	if data.get("agent_type") != AGENT:
		answer("mutate-request is only for the code reviewer.")
		return
	if len(words) != 2:
		answer("usage: mutate-request <spec.json>  (a JSON list of {\"file\", \"line\", \"find\", \"replace\"})")
		return
	try:
		specs = qg_common.loadJson(words[1])
		specs = specs.get("mutants", specs) if isinstance(specs, dict) else specs
		if not isinstance(specs, list):
			raise ValueError("not a list")
	except (OSError, ValueError) as error:
		answer(f"mutate-request: can't read {words[1]}: {error}")
		return
	agentId = data.get("agent_id")
	started = time.time()
	budget_state.markWait(root, agentId, "reviewer's mutants", qg_common.hookTimeout("mutate_request_seconds"))
	qg_common.startDeadline("mutate_request_seconds")
	try:
		results = runManual(root, config, limits, specs[:limits["manual_max"]])
	except qg_common.SandboxError as error:
		answer(f"mutate-request: not run, environment problem on this machine, not the code: {error}")
		return
	except qg_common.DeadlineReached as error:
		qg_common.deadlineNotice(root, "mutation", error)
		answer(f"mutate-request: didn't finish, {error}. Write in your report that your mutants weren't run "
			"(not caught, not survived); a smaller list may fit.")
		return
	finally:
		budget_state.excludeWait(root, agentId, time.time() - started)
	with open(os.path.join(directory, f"manual-{agentId}.log"), "a", encoding="utf-8") as f:
		f.write("\n".join(results) + "\n")
	answer("mutate-request results (the hook ran your mutants; this denial is the answer, not an error):\n"
		+ "\n".join(results))


def runManual(root, config, limits, specs):
	"""One result line per spec, in order: rejected ones first sorted out, then per language one baseline
	(manualBaseline) and each mutant run as the scan runs it (runOne); then a line per language whose
	"select" couldn't be used."""
	protect = config.get("protect") or []
	results, byLang = [None] * len(specs), {}
	with tempfile.TemporaryDirectory(prefix="qg-manual-") as tmp:
		scratch = os.path.join(tmp, "repo")
		copyCheckout(root, scratch, config.get("scratch_exclude") or [])
		for index, spec in enumerate(specs):
			path = os.path.normpath(str(spec.get("file", "")))
			label = f"{path}:{spec.get('line')}  `{spec.get('find')}` -> `{spec.get('replace')}`"
			lang = qg_common.languageOf(path, config)
			if path.startswith("..") or os.path.isabs(path) or not lang or not os.path.isfile(os.path.join(scratch, path)):
				results[index] = f"{label}  -> rejected (not a source file of a configured language)"
				continue
			mutated, why = qg_mutate.applyManual(qg_common.readText(scratch, path), spec)
			if mutated is None:
				results[index] = f"{label}  -> rejected ({why})"
				continue
			byLang.setdefault(lang, []).append((index, label, {"file": path, "line": int(spec["line"])}, mutated))
		unmutated, notes = {}, []
		for lang, items in byLang.items():
			files = {}
			for _, _, mutant, _ in items:
				files.setdefault(mutant["file"], set()).add(mutant["line"])
			baseline = manualBaseline(scratch, root, config, lang, files, tmp, limits)
			notes += baseline["notes"]
			for index, label, mutant, mutated in items:
				results[index] = f"{label}  -> " + manualResult(scratch, root, lang, mutant, mutated, baseline, protect,
					unmutated)
	return results + notes


def manualResult(scratch, root, lang, mutant, mutated, baseline, protect, unmutated):
	"""The result of one manual mutant after its language's baseline (manualBaseline)."""
	if baseline["failed"]:
		return f"not run: {baseline['failed']}"
	if baseline["code"] != 0:
		return f"not run: the {lang} suite fails without the mutant (exit {baseline['code']}): {baseline['tail']}"
	try:
		runOne(scratch, root, mutant, mutated, baseline["langConfig"], baseline["timeout"], protect, baseline["contexts"],
			unmutated)
	except qg_common.NotStarted as error:
		return f"not run: the mutant run didn't start: {error}"
	return mutant["status"]


def manualBaseline(scratch, root, config, lang, files, tmp, limits):
	"""The unmutated scratch copy for one language's manual mutants (files: {path: lines}): with "select" the
	scan's context baseline over those lines (selectBaseline; unusable: a fallback note and the whole test
	command), else the whole test command. {"code", "timeout", "tail", "contexts", "langConfig", "notes",
	"failed": why the baseline never ran, or None}."""
	langConfig, protect = config["languages"][lang], config.get("protect") or []
	out = {"code": 0, "timeout": 0, "tail": "", "contexts": None, "langConfig": langConfig, "notes": [], "failed": None}
	sel = selectConfig(langConfig)
	if sel:
		contexts, seconds, why = selectBaseline(scratch, root, langConfig, sel, files, tmp, limits, protect)
		if why is None:
			return {**out, "timeout": mutantTimeout(seconds, limits), "contexts": contexts}
		out["notes"].append("note: " + fallbackNote(lang, why))
		out["langConfig"] = langConfig = withoutSelect(langConfig)
	mapping = {"repo": root, "root": scratch}
	try:
		code, seconds, tail = qg_common.runCommand(qg_common.expand(langConfig["test"], mapping),
			os.path.join(scratch, langConfig.get("cwd", "")), qg_common.commandEnv(langConfig, mapping),
			limits["coverage_timeout_seconds"], writable=[scratch], readable=[root], protect=protect)
	except qg_common.NotStarted as error:
		return {**out, "failed": f"the {lang} test command didn't start: {error}; "
			+ qg_common.commandHint(config, lang, "test")}
	return {**out, "code": code, "timeout": mutantTimeout(seconds, limits), "tail": qg_common.lastLines(tail, 2)}


def onConfigError(data, root, error):
	"""The config doesn't load (module doc): the reviewer's hand-back held once (Stop: blocked once) with the
	error, a mutate-request answered with it; everything else only on stderr."""
	print(f"mutation: not run, config problem: {error}", file=sys.stderr)
	event, tool = data.get("hook_event_name"), data.get("tool_name")
	text = (f"mutation: not run, the quality-gate config doesn't load (environment or config, not the code): {error}. "
		"Write in your report that the mutation scan didn't run and why, judge no mutants, then give your full report again.")
	if event == "PreToolUse" and tool == "Bash" and sys.argv[1:] != ["start"]:
		command = ((data.get("tool_input") or {}).get("command") or "").strip()
		if command.startswith("mutate-request"):
			print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
				"permissionDecisionReason": f"mutate-request: not run: {error}"}}))
		return
	isHandback = event == "PreToolUse" and tool == "SubagentHandback" and sys.argv[1:] != ["start"]
	if data.get("agent_type") != AGENT or not (isHandback or event == "SubagentStop"):
		return
	marker = os.path.join(mutationDir(root), f"config-error-{data.get('agent_id') or 'unknown'}")
	if os.path.isfile(marker):
		return
	markDelivered(marker)
	if isHandback:
		print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
			"permissionDecisionReason": text}}))
	else:
		print(json.dumps({"decision": "block", "reason": text}))


def main():
	data = json.load(sys.stdin)
	if data.get("agent_type") != AGENT and data.get("hook_event_name") != "PreToolUse":
		return
	root = hookonly.gitRoot(data)
	if not root:
		return
	try:
		config, limits = qg_common.loadConfig(root)
	except qg_common.homeconfig.ConfigError as error:
		onConfigError(data, root, error)
		return
	if config is None:
		return
	directory = mutationDir(root)
	event = data.get("hook_event_name")
	startMode = sys.argv[1:] == ["start"]
	if event == "SubagentStart" or (event == "PreToolUse" and startMode):
		if data.get("agent_type") == AGENT:
			removeStale(directory)
			onStart(data, root, config, limits, directory)
	elif event == "SubagentStop":
		onStop(data, root, config, limits, directory)
	elif event == "PreToolUse" and data.get("tool_name") == "SubagentHandback" and not startMode:
		if data.get("agent_type") == AGENT:
			onHandback(data, root, config, limits, directory)
	elif event == "PreToolUse" and data.get("tool_name") == "Bash":
		onMutateRequest(data, root, config, limits, directory)


if __name__ == "__main__":
	def onError(error):
		print(f"mutation error (not run): {error}", file=sys.stderr)
		sys.exit(0)

	import hookrun
	hookrun.run("mutation", main, onError)
	sys.exit(0)

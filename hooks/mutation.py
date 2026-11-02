#!/usr/bin/env python3
"""Mutation testing for the code reviewer: the hook runs the mutants, the reviewer only judges.

Events (all for agent_type code-reviewer, all registered in code-reviewer.md's frontmatter):
- PreToolUse on any tool, run as `mutation.py start` with async: the automatic scan, started by the
  reviewer's first tool call (later calls find it running or done and return at once). Scratch copy of the checkout, the
  coverage command as baseline (must pass), then text mutants (qg_mutate) on changed lines that the
  baseline executed and no exclusion marker covers, mutation_max of them, each run with the test
  command and a timeout of max(baseline x factor, baseline + min_extra). Result:
  .hook-only/hook-read-only/mutation/auto-<fingerprint>.json. Prints nothing.
  A language with "select" (kind pytest-contexts) narrows the work: the baseline runs its tests with
  per-test coverage contexts over the changed files' directories only, each mutant runs only the tests
  that executed its line (one with no known test runs the "fallback" tests), with the whole test
  command's accuracy: every survivor is rerun with it, and a catch counts only when the same selection
  passes on the unmutated code (else the whole test command decides); a later scan where only
  test files changed (guard-paths tests, named like tests) keeps the invalid mutants and the caught ones
  whose catching test file is unchanged, and reruns the rest on their tests plus the changed files. (A SubagentStart
  registration in settings.json does the same; the frontmatter is used because the docs don't say
  whether an agent's own SubagentStart hook fires.)
- PreToolUse Bash `mutate-request <spec.json>` (sync, plain `mutation.py`): the reviewer's own
  mutants, a JSON list of {"file", "line", "find", "replace"} (at most manual_max). Run in a fresh
  scratch copy; the call is denied with the result table as the reason (that is the answer). Run like
  the scan's mutants (runOne): a language with "select" gets one context baseline over the mutated
  lines and each mutant runs only the tests that executed its line, with the same catch and survivor
  rules; without "select", a baseline of the test command and every mutant on it. The reviewer's Bash
  call is held meanwhile, so the wait is marked (markWait, limit manual_wait_seconds) and taken out of
  its time budget (excludeWait), as at the hand-back. Before 2026-11-02 it was neither: the reviewer's
  minutes ran on for 10+ min with nothing shown, and it was stopped on time.
- PreToolUse SubagentHandback (sync): waits for the automatic scan of the current fingerprint (or runs
  it if none is running), then, once per reviewer and fingerprint, denies the hand-back with the
  survivors and any failing baseline, so the report the orchestrator gets already holds the judgement.
  The wait is taken out of the reviewer's time budget (qg_common.excludeWait); while it lasts,
  qg_common.markWait marks it in the budget state so status displays show waiting, not overrun. At Stop after a
  hand-back the report was already out: the reviewer "finished twice", and in `claude -p` the second
  finish (scan ~10 min) came after the orchestrator's last turn (inferred-report-dates, 2026-10-31).
- SubagentStop (Stop): the same, as a block, for a reviewer that ends without a hand-back (and a no-op
  once the hand-back gate delivered for this fingerprint).
Every outcome of that delivery point is also written, counts only, to
.hook-only/quality/mutation-<agent_id>-<fingerprint>.json, which sessions can read (hook-read-only
can't be): all caught (nothing to judge, so the reviewer hears nothing), survivors delivered, no
result within mutation_wait_seconds, or no sandbox. Before 2026-11-01 the all-caught and no-result
cases left no trace anyone could read, and a review without survivors looked like a scan that never
reached the reviewer (signal-eval). No result within the wait now denies the hand-back once too, so
the report says the scan didn't finish instead of saying nothing.
A review whose dispatch prompt starts with "Merge review:" (dispatch-guard leaves merge-review-pending)
gets no automatic scan; manual mutants still run. The flag is claimed by that reviewer's first start
call or its stop (merge-<agent_id>); seen-<agent_id> marks a reviewer that already passed that point.

Status: caught (tests failed), timeout (counts as caught), survived (tests passed), invalid (the
mutant doesn't build or breaks collection: the language's invalid_exit_codes / invalid_patterns, or
a Python syntax error). State and results live in hook-read-only: agents can't read them, the reviewer
gets only what the hook returns. Without .claude/quality-gates.json: nothing. Fails open (stderr).
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
	"""Scratch copy without excluded paths ("dir/" = from the root, a bare name = anywhere)."""
	anchored = [e.rstrip("/") for e in excludes if e.endswith("/")]
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
	targets = sorted({os.path.relpath(os.path.join(scratch, os.path.dirname(path)), cwd) for path in files})
	argv = (pytestArgv(sel, root, scratch, langConfig) + ["--cov-branch", "--cov-context=test", "--cov-report="]
		+ [f"--cov={target}" for target in targets])
	code, seconds, tail = qg_common.runCommand(argv, cwd, env, limits["coverage_timeout_seconds"],
		writable=[scratch, out], readable=[root], protect=protect)
	if code != 0:
		return None, code, seconds, tail
	helper = os.path.join(out, "qg_cov_contexts.py")
	shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "qg_cov_contexts.py"), helper)
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
				or old.get("sandbox_unavailable")):
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
	langs = qg_common.loadLangs()
	base = qg_common.defaultBase(root)
	changes = qg_common.sourceChanges(root, base, config)
	result = {"baseline_failed": [], "sandbox_unavailable": None, "results": [], "candidates": 0, "at": time.time(),
		"base": base, "snapshot": snapshot(root, base), "timeouts": {}, "reused": 0}
	if not changes:
		return result
	protect = config.get("protect") or []
	with tempfile.TemporaryDirectory(prefix="qg-mutation-") as tmp:
		scratch = os.path.join(tmp, "repo")
		copyCheckout(root, scratch, config.get("scratch_exclude") or [])
		previous = reusable(directory, base, result["snapshot"], config)
		if previous:
			try:
				return rescan(root, config, limits, scratch, previous[0], previous[1], result)
			except qg_common.SandboxError as error:
				result["sandbox_unavailable"] = str(error)[:400]
				return result
		candidates, timeouts, contexts, unmutated = [], result["timeouts"], {}, {}
		for lang, files in changes.items():
			langConfig, profile = config["languages"][lang], langs.get(lang) or {}
			sel = selectConfig(langConfig)
			try:
				if sel:
					info, code, seconds, tail = contextBaseline(scratch, root, langConfig, sel, files, tmp, limits, protect)
					cover = None if info is None else {p: {n: 1 for n in v["executed"]} for p, v in info.items()}
					contexts[lang] = info or {}
				else:
					cover, code, seconds, tail = qg_common.measureCoverage(scratch, lang, langConfig, os.path.join(tmp, "work"),
						limits["coverage_timeout_seconds"], repo=root, protect=protect)
			except qg_common.SandboxError as error:
				result["sandbox_unavailable"] = str(error)[:400]
				return result
			if cover is None or code != 0:
				result["baseline_failed"].append({"lang": lang, "exit": code, "tail": " | ".join(tail.strip().splitlines()[-3:])[:400]})
				continue
			timeouts[lang] = mutantTimeout(seconds, limits)
			for path, lines in files.items():
				text = qg_common.readText(scratch, path)
				covered = {n for n in lines if cover.get(path, {}).get(n, 0) > 0}
				skip = qg_complexity.markedLines(text, profile)
				for mutant in qg_mutate.fileMutants(path, text, covered, profile, skip):
					mutant["lang"] = lang
					candidates.append(mutant)
		result["candidates"] = len(candidates)
		for mutant in qg_mutate.select(candidates, limits["mutation_max"]):
			lang = mutant["lang"]
			mutated = replaceLine(qg_common.readText(scratch, mutant["file"]), mutant["line"], mutant["mutated"])
			runOne(scratch, root, mutant, mutated, config["languages"][lang], timeouts[lang], protect, contexts.get(lang),
				unmutated)
			result["results"].append(mutant)
	return result


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
	deadline = time.time() + limits["mutation_wait_seconds"]
	while not os.path.isfile(resultFile) and time.time() < deadline:
		if lockHolder(os.path.join(directory, f"auto-{fp}.lock")):
			time.sleep(5)
		else:
			runAuto(root, config, limits, directory, fp)
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
	if not survivors and not result["baseline_failed"]:
		summarize(root, agentId, fp, "all_caught", counts=counts, candidates=result["candidates"])
		return None
	summarize(root, agentId, fp, "delivered", counts=counts, candidates=result["candidates"],
		baselineFailed=len(result["baseline_failed"]))
	lines = [f"baseline failed for {b['lang']} (exit {b['exit']}): {b['tail']}" for b in result["baseline_failed"]]
	lines += [f"{m['file']}:{m['line']}  {m['op']}  (line: {m['original'].strip()[:120]})" for m in survivors]
	summary = (f"mutation (automatic scan of the changed lines): {len(results)} run of {result['candidates']} candidates; "
		f"caught {counts['caught']}, timeout {counts['timeout']}, survived {counts['survived']}, invalid {counts['invalid']}."
		+ (f" {result['reused']} kept from the previous scan (only test files changed since)." if result.get("reused") else ""))
	return (summary + "\n" + qg_common.truncate(lines, limits["survivor_max_chars"])
		+ "\nJudge each survivor: equivalent (no input can tell the difference; say why) or a test gap (a finding "
		"routed to QA, the mutant as its failure scenario). A failed baseline means the suite is red: report it. "
		"Add them to your mutation table, then give your full report again.")


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
	reason = survivorReason(data, root, config, limits, directory)
	if reason:
		print(json.dumps({"decision": "block", "reason": reason}))


def onHandback(data, root, config, limits, directory):
	started = time.time()
	qg_common.markWait(root, data.get("agent_id"), "mutation scan", limits["mutation_wait_seconds"])
	reason = survivorReason(data, root, config, limits, directory)
	qg_common.excludeWait(root, data.get("agent_id"), time.time() - started)
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
	qg_common.markWait(root, agentId, "reviewer's mutants", limits["manual_wait_seconds"])
	try:
		results = runManual(root, config, limits, specs[:limits["manual_max"]])
	except qg_common.SandboxError as error:
		answer(f"mutate-request: not run, environment problem on this machine, not the code: {error}")
		return
	finally:
		qg_common.excludeWait(root, agentId, time.time() - started)
	with open(os.path.join(directory, f"manual-{agentId}.log"), "a", encoding="utf-8") as f:
		f.write("\n".join(results) + "\n")
	answer("mutate-request results (the hook ran your mutants; this denial is the answer, not an error):\n"
		+ "\n".join(results))


def runManual(root, config, limits, specs):
	"""One result line per spec, in order: rejected ones first sorted out, then per language one baseline
	(manualBaseline) and each mutant run as the scan runs it (runOne)."""
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
		unmutated = {}
		for lang, items in byLang.items():
			langConfig = config["languages"][lang]
			files = {}
			for _, _, mutant, _ in items:
				files.setdefault(mutant["file"], set()).add(mutant["line"])
			code, timeout, tail, contexts = manualBaseline(scratch, root, langConfig, files, tmp, limits, protect)
			for index, label, mutant, mutated in items:
				if code != 0:
					results[index] = f"{label}  -> not run: the {lang} suite fails without the mutant (exit {code}): {tail}"
					continue
				runOne(scratch, root, mutant, mutated, langConfig, timeout, protect, contexts, unmutated)
				results[index] = f"{label}  -> {mutant['status']}"
	return results


def manualBaseline(scratch, root, langConfig, files, tmp, limits, protect):
	"""(exit code, mutant timeout, output tail, contexts) of the unmutated scratch copy for one language's
	manual mutants (files: {path: lines}): with "select" the scan's context baseline over those lines,
	else the whole test command (contexts None)."""
	sel = selectConfig(langConfig)
	if sel:
		contexts, code, seconds, tail = contextBaseline(scratch, root, langConfig, sel, files, tmp, limits, protect)
	else:
		mapping = {"repo": root, "root": scratch}
		contexts = None
		code, seconds, tail = qg_common.runCommand(qg_common.expand(langConfig["test"], mapping),
			os.path.join(scratch, langConfig.get("cwd", "")), qg_common.commandEnv(langConfig, mapping),
			limits["coverage_timeout_seconds"], writable=[scratch], readable=[root], protect=protect)
	return code, mutantTimeout(seconds, limits), " | ".join(tail.strip().splitlines()[-2:])[:300], contexts


def main():
	data = json.load(sys.stdin)
	if data.get("agent_type") != AGENT and data.get("hook_event_name") != "PreToolUse":
		return
	root = hookonly.gitRoot(data)
	if not root:
		return
	config, limits = qg_common.loadConfig(root)
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
	try:
		main()
	except Exception as error:
		print(f"mutation error (not run): {error}", file=sys.stderr)
	sys.exit(0)

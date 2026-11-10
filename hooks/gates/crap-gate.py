#!/usr/bin/env python3
"""Quality gate on changed code: complexity, CRAP and unexecuted lines. No agent judgement.

Rules, only for functions and lines the change touches (qg_common: diff against the merge-base with
origin's default branch; test paths excluded):
  1. CCN > ccn_max -> "split X". A function already over ccn_max on the base (same qualified name)
     may not grow instead; CRAP is not applied to it.
  2. CRAP = ccn^2 * (1 - cov)^3 + ccn > crap_max, cov = executed / executable lines of the function.
  3. A changed executable line no test runs -> delete it, or mark it (`# pragma: no cover -- <why>`,
     Go `// coverage:ignore <why>`; a marker on a line ending in `{` covers its block).
  4. New markers: a reason is required, at most marker_new_max per change.
  5. With coverage targets derived from the diff ({changed_dirs}, qg_common module doc), a changed file
     the coverage report doesn't hold at all is a finding: it was not measured, so it never passes as
     covered. A repo's own coverage command keeps the old reading (a file outside its --cov list is
     not checked for rules 2-3).
Rules 2-4 need coverage; the presentation class ("class" in plans/worker/<topic>.json, main checkout)
gets rule 1 only.

Events:
- PreToolUse SubagentHandback of developer (hook-entry.py's `handback` entry, after report_check.py: a
  hand-back whose header is refused never starts the suite): the same check before the report leaves, failing -> deny the hand-back with the list. A block at Stop after a hand-back came too late:
  the orchestrator already had the report and the developer never acted on the block (the block came
  45 s after the hand-back, nothing after). The wait is taken out of its budget.
- SubagentStop of developer (hook-entry.py's `stop` entry, its first step), for a developer that ends without
  a hand-back: failing -> block with the list, so the same developer fixes it. Either way, after developer_blocks_max blocks it lets the developer stop and appends an
  ALERT to agent-guard, whose HOLD stops dispatches and commits until the planning session decides
  (`wt resume` / a user prompt lifts it). A last message with a line starting `NEEDS-TEST:` stops
  without a block: the orchestrator sends that to qa, and the commit check below still applies.
- PreToolUse Bash `git commit` in the main session (its own settings registration, `if: Bash(git *)`). First,
  read only: when the call is held by agent-guard (agent_guard.held: an ALERT pending or a HOLD) or refused by
  merge_commit_guard.check, it says nothing and runs nothing: hook-entry.py's `pre` entry denies that call, and
  the suite (up to hook_timeouts.commit_gate_seconds) would only delay the denial. Then, by what is staged:
  - source of a configured language (an implementation commit): the rules above, and the suite must
    pass (the coverage run's exit code): test-first.md wants the implementation commit green, and this
    makes it the hook's word, not the orchestrator's report. A red suite never blocks the developer
    itself (it may be reporting a wrong test); only the commit.
  - only test files (configured language, excluded paths), and the branch has no committed source
    and no committed tests since the base yet (test-first's first tests commit): the language's
    `test` command must fail, and
    not for a reason in its `red_invalid_patterns` (e.g. SyntaxError): new tests that pass without the
    implementation test nothing. A missing module or name (ImportError, Go "undefined:") is the expected
    red. Later test-only commits (review gaps, NEEDS-TEST, and test fixes before the implementation
    commit) get neither check: their tests may pass, and an unfinished implementation left in the
    working tree is the developer's, not theirs. (Formerly "later" meant "after a source
    commit", so fixing tests while the implementation was still uncommitted was refused as a first
    tests commit that passes, and the fixes had to ride in the implementation commit.)
  - anything else (docs, or nothing staged as with `git commit <paths>`): the rules above.
  Failing -> deny the commit.
Results are cached per fingerprint of the working tree, so the second SubagentStop of a background
agent and the commit right after a passing developer don't rerun the suite.
Config: qg_common.loadConfig, from the diff base, never the working tree. Without .claude/quality-gates.json
the languages detected from the tracked files run on their home profiles; nothing is checked only when
there is nothing to gate (no repo file, no detected language). Block messages say when a language was
enabled automatically and how to turn it off. Fails: closed on PreToolUse — any exception exits 2 with the
error (hookrun.py), which denies the commit or the hand-back; open at SubagentStop (stderr, the developer may
stop). Besides: a coverage
run that produces no report is a finding, naming the command's key and where its value comes from
(qg_common.commandHint); a machine or config problem the developer can't fix never counts as a developer
block (the developer may finish) and refuses the commit with a message, so the orchestrator stops for the
user: bwrap unavailable (qg_common.SandboxError), a gate command that never started (qg_common.NotStarted:
program missing or not executable; the message names the key), a config that doesn't load (a tool a
command needs is missing: homeconfig.ConfigError). At the first tests commit a test command that never
started, or exits with one of the language's red_invalid_exit_codes (pytest: usage error, no tests
collected), is refused too: the tests didn't run, so they can't be the expected red.
The suite is never run outside the sandbox.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import agent_guard
import budget_state
import hookonly
import merge_commit_guard
import qg_common
import qg_complexity

COMMIT = re.compile(r"\bgit\b[^|;&]*\bcommit\b")
MAIN_TYPES = {None, "claude"}
REASON_SUFFIX = re.compile(r"^[\s:\-–—]*(.*)$")


def markerFindings(texts, changes, langs, limits):
	findings, count = [], 0
	for lang, files in changes.items():
		pattern = (langs.get(lang) or {}).get("marker")
		if not pattern:
			continue
		marker = re.compile(pattern)
		for path, lines in files.items():
			for number in sorted(lines):
				source = texts[path].splitlines()
				if number > len(source) or not (match := marker.search(source[number - 1])):
					continue
				count += 1
				if not REASON_SUFFIX.match(source[number - 1][match.end():]).group(1).strip():
					findings.append(f"{path}:{number}: exclusion marker without a reason")
	if count > limits["marker_new_max"]:
		findings.append(f"{count} new exclusion markers; at most {limits['marker_new_max']} per change")
	return findings


def evaluate(root, config, limits, ccnOnly):
	"""(findings, failing): quality findings, and the languages whose suite ran red (exit != 0 with a
	report). Only the commit check acts on failing."""
	failing = []
	langs = qg_common.loadLangs()
	base = qg_common.defaultBase(root)
	changes = qg_common.sourceChanges(root, base, config)
	findings, texts = [], {}
	work = hookonly.stateDir(root, "quality")
	for lang, files in changes.items():
		langConfig, profile = config["languages"][lang], langs.get(lang) or {}
		goBin = langConfig.get("go_bin")  # Go only; the home go profile always has it (home.json toolchains)
		current = {}
		for path in files:
			texts[path] = qg_common.readText(root, path)
			try:
				current.update(qg_complexity.measure(lang, {path: texts[path]}, langs, goBin))
			except (SyntaxError, RuntimeError) as error:
				findings.append(f"{path}: can't parse ({str(error)[:200]})")
		olds = {}
		for path in files:
			text = qg_common.baseText(root, base, path)
			if text is not None:
				try:
					olds.update(qg_complexity.measure(lang, {path: text}, langs, goBin))
				except (SyntaxError, RuntimeError):
					pass
		cover = None
		if not ccnOnly:
			cover, code, tail = coverage(root, config, limits, lang, work, files)
			last = " | ".join(tail.strip().splitlines()[-3:])
			if cover is None:
				findings.append(f"coverage for {lang} produced no report (exit {code}): {last[:300]}; "
					+ qg_common.commandHint(config, lang, "coverage"))
			elif code != 0:
				failing.append(f"{lang} (exit {code}): {last[:300]}")
		for path, lines in files.items():
			baseByName = {fn["name"]: fn for fn in olds.get(path, [])}
			fileCover = cover.get(path) if cover else None
			marked = qg_complexity.markedLines(texts[path], profile)
			for fn in current.get(path, []):
				if not any(fn["start"] <= n <= fn["end"] for n in lines):
					continue
				old = baseByName.get(fn["name"])
				legacy = old is not None and old["ccn"] > limits["ccn_max"]
				where = f"{fn['name']} ({path}:{fn['start']})"
				if legacy and fn["ccn"] > old["ccn"]:
					findings.append(f"{where}: CCN {fn['ccn']}, was {old['ccn']} on the base; an over-limit function must not grow")
				elif not legacy and fn["ccn"] > limits["ccn_max"]:
					findings.append(f"{where}: CCN {fn['ccn']} > {limits['ccn_max']}; split it")
				if fileCover is None or legacy:
					continue
				executable = [n for n in range(fn["start"], fn["end"] + 1) if n in fileCover and n not in marked]
				if executable:
					cov = sum(1 for n in executable if fileCover[n] > 0) / len(executable)
					crap = fn["ccn"] ** 2 * (1 - cov) ** 3 + fn["ccn"]
					if crap > limits["crap_max"]:
						findings.append(f"{where}: CRAP {crap:.0f} > {limits['crap_max']} (CCN {fn['ccn']}, coverage {cov:.0%}); split it or delete untested branches")
			if fileCover is not None:
				missed = [n for n in lines if fileCover.get(n) == 0 and n not in marked]
				if missed:
					findings.append(f"{path}:{qg_common.ranges(missed)}: changed, never executed by a test; delete, or mark with a reason")
			elif cover is not None and qg_common.derivesTargets(langConfig):
				findings.append(unmeasured(config, lang, path))
	if not ccnOnly:
		findings += markerFindings(texts, changes, langs, limits)
	return findings, failing


def unmeasured(config, lang, path):
	"""Rule 5: a changed file the derived-target coverage report left out (e.g. the coverage config's omit,
	or skip_empty for a file without statements) counts as not covered, never as passing."""
	return (f"{path}: changed, but the coverage run reported nothing for it, so none of its lines count as "
		f"covered; check the coverage config's omit and skip_empty, or add the path to \"exclude\" in "
		f"{qg_common.CONFIG_FILE}; "
		+ qg_common.commandHint(config, lang, "coverage"))


def coverage(root, config, limits, lang, work, files):
	"""(cover map or None, exit code, tail) of the language's coverage command over the changed files
	(files fill {changed_dirs}); a NotStarted names the key."""
	try:
		cover, code, _, tail = qg_common.measureCoverage(root, lang, config["languages"][lang], work,
			limits["coverage_timeout_seconds"], protect=config.get("protect") or [], files=files)
	except qg_common.NotStarted as error:
		raise qg_common.NotStarted(f"coverage for {lang}: {error}; {qg_common.commandHint(config, lang, 'coverage')}") \
			from error
	return cover, code, tail


def detectedNotes(config):
	"""One line per language enabled automatically (qg_common.detectedNote), for block messages."""
	return "".join("\n" + qg_common.detectedNote(lang) for lang in config.get("detected") or [])


def flowClass(root):
	"""The topic's class from plans/worker/<topic>.json, which handoff writes in the main checkout (the
	worktree is checked too). Topic = third part of the branch name."""
	branch = qg_common.git(root, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip()
	parts = branch.split("/")
	if len(parts) != 3:
		return None
	common = qg_common.git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).strip()
	for checkout in (root, os.path.dirname(common) if common else None):
		if not checkout:
			continue
		try:
			return qg_common.loadJson(os.path.join(checkout, "plans", "worker", f"{parts[2]}.json")).get("class")
		except (OSError, ValueError):
			continue
	return None


def check(root, config, limits, work):
	"""(fingerprint, findings, failing), cached per fingerprint."""
	base = qg_common.defaultBase(root)
	fp = qg_common.fingerprint(root, base)
	cacheFile = os.path.join(work, f"result-{fp}.json")
	if os.path.isfile(cacheFile):
		cached = qg_common.loadJson(cacheFile)
		return fp, cached["findings"], cached.get("failing", [])
	findings, failing = evaluate(root, config, limits, ccnOnly=flowClass(root) == "presentation")
	os.makedirs(work, exist_ok=True)
	with open(cacheFile, "w", encoding="utf-8") as f:
		json.dump({"findings": findings, "failing": failing, "at": time.time()}, f)
	return fp, findings, failing


def removeStale(work, days=7):
	limit = time.time() - days * 86400
	for name in os.listdir(work):
		path = os.path.join(work, name)
		try:
			if name.startswith(("result-", "dev-")) and os.path.getmtime(path) < limit:
				os.remove(path)
		except OSError:
			pass


def instructions(limits):
	return ("How to fix: split the function, delete code no test needs, or mark a line that truly can't run "
		f"under tests (`# pragma: no cover -- <why>` / `// coverage:ignore <why>`, at most {limits['marker_new_max']} "
		"new markers). You don't write tests: if one is needed, end your report with a line "
		"`NEEDS-TEST: <what to test>` and stop.")


REPORT_STATUS = re.compile(r"^\W*(DONE|CHECKPOINT|BLOCKED)\b")


def firstLine(text):
	return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def reportStatuses(data):
	"""The status a report starts with (agent-common Report: its first line), as a set. A hand-back's
	tool_input field names aren't documented, so each string field's first line counts; a Stop has
	last_assistant_message. A line inside the report body never counts."""
	if data.get("tool_name") == "SubagentHandback":
		texts = qg_common.handbackStrings(data)
	else:
		texts = [data.get("last_assistant_message") or ""]
	return {m.group(1) for text in texts if (m := REPORT_STATUS.match(firstLine(text)))}


def developerGate(data, root, config, limits, work, message):
	"""The block reason for a developer about to finish, or None to let it finish.

	A CHECKPOINT or BLOCKED report isn't checked: the work is unfinished on purpose (budget stop, or a
	problem only the orchestrator can route), the orchestrator handles it by feature-flow agents.md, and
	blocking would only burn the agent's remaining budget toward an ALERT hold (seen as
	three blocks after the budget stop). The commit check still runs."""
	agentId = data.get("agent_id") or "unknown"
	if any(line.strip().startswith("NEEDS-TEST:") for line in message.splitlines()):
		return None
	statuses = reportStatuses(data)
	if statuses and statuses <= {"CHECKPOINT", "BLOCKED"}:
		return None
	try:
		fp, findings, failing = check(root, config, limits, work)
	except (qg_common.SandboxError, qg_common.NotStarted) as error:
		print(f"crap-gate: not checked, environment problem: {error}; the commit check will stop the flow", file=sys.stderr)
		return None
	except qg_common.DeadlineReached as error:  # no pass file: the commit check runs it again
		qg_common.deadlineNotice(root, "crap-gate", error)
		return None
	if not findings:
		if not failing:
			with open(os.path.join(work, "pass"), "w", encoding="utf-8") as f:
				f.write(fp)
		return None
	stateFile = os.path.join(work, f"dev-{agentId}.json")
	state = qg_common.loadJson(stateFile) if os.path.isfile(stateFile) else {"blocks": 0}
	listing = qg_common.truncate(findings, limits["reason_max_chars"])
	if state["blocks"] < limits["developer_blocks_max"]:
		state["blocks"] += 1
		with open(stateFile, "w", encoding="utf-8") as f:
			json.dump(state, f)
		return (f"crap-gate (block {state['blocks']} of {limits['developer_blocks_max']}): the changed code breaks the quality "
			f"limits. Fix every item, re-run the tests, then finish again.\n{listing}\n{instructions(limits)}"
			+ detectedNotes(config))
	if state.get("alerted") != fp:
		guardDir = hookonly.stateDir(root, "agent-guard")
		os.makedirs(guardDir, exist_ok=True)
		with open(os.path.join(guardDir, "ALERT"), "a", encoding="utf-8") as f:
			f.write(f"crap-gate: developer ({agentId}) still fails after {limits['developer_blocks_max']} blocks; the planning "
				f"session decides (raise a limit, split, or send to qa): {' / '.join(findings)[:1500]}\n")
		state["alerted"] = fp
		with open(stateFile, "w", encoding="utf-8") as f:
			json.dump(state, f)
	return None


def onDeveloperStop(data, root, config, limits, work):
	reason = developerGate(data, root, config, limits, work, data.get("last_assistant_message") or "")
	if reason:
		print(json.dumps({"decision": "block", "reason": reason}))


def onDeveloperHandback(data, root, config, limits, work):
	agentId = data.get("agent_id")
	started = time.time()
	budget_state.markWait(root, agentId, "coverage run", qg_common.hookTimeout("developer_gate_seconds"))
	try:
		reason = developerGate(data, root, config, limits, work, qg_common.handbackText(data))
	finally:
		budget_state.excludeWait(root, agentId, time.time() - started)
	if reason:
		print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
			"permissionDecisionReason": reason + "\nThen hand back your full report again."}}))


def stagedPaths(root):
	return [p for p in qg_common.git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if p]


def isTest(path, config):
	return bool(qg_common.languageOf(path, config)) and qg_common.isExcluded(path, config["exclude"])


def committedSince(root, base):
	return [p for p in qg_common.git(root, "diff", "--name-only", "-z", base, "HEAD").split("\0") if p]


def branchHasSource(root, config, base):
	"""Whether a commit since the base already holds source of a configured language."""
	return any(qg_common.isSource(p, config) for p in committedSince(root, base))


def isFirstTestsCommit(root, config, base):
	"""Whether nothing of a configured language, source or test, is committed since the base yet."""
	return not any(qg_common.isSource(p, config) or isTest(p, config) for p in committedSince(root, base))


def redFindings(root, config, limits, langs):
	"""Why the first tests commit isn't the expected red, per language; [] when it is (module doc)."""
	findings = []
	for lang in sorted(langs):
		langConfig = config["languages"][lang]
		mapping = {"repo": root, "root": root}
		try:
			code, _, tail = qg_common.runCommand(qg_common.expand(langConfig["test"], mapping),
				os.path.join(root, langConfig.get("cwd", "")), qg_common.commandEnv(langConfig, mapping),
				limits["coverage_timeout_seconds"], writable=[root], protect=config.get("protect") or [])
		except qg_common.NotStarted as error:
			findings.append(f"{lang}: the test command didn't start ({error}); {qg_common.commandHint(config, lang, 'test')}")
			continue
		last = " | ".join(tail.strip().splitlines()[-3:])[:300]
		wrong = [pattern for pattern in langConfig.get("red_invalid_patterns") or [] if pattern in tail]
		if code == 0:
			findings.append(f"{lang}: the suite passes with the new tests and no implementation (exit 0): they don't test the missing behaviour")
		elif code is None:
			findings.append(f"{lang}: the test run timed out")
		elif code in (langConfig.get("red_invalid_exit_codes") or []):
			findings.append(f"{lang}: the tests didn't run (exit {code}, red_invalid_exit_codes): {last}; "
				+ qg_common.commandHint(config, lang, "test"))
		elif wrong:
			findings.append(f"{lang}: the tests fail for the wrong reason ({', '.join(wrong)}): {last}")
	return findings


def deny(reason):
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
		"permissionDecisionReason": reason}}))


def deniedBefore(data):
	"""Whether hook-entry.py's `pre` entry denies this commit anyway (module doc): held by agent-guard, or a merge
	commit merge-commit-guard refuses. A check that fails here counts as not denied, so the gate still runs."""
	try:
		root = hookonly.gitRoot(data)
		return bool(root and agent_guard.held(data, root)) or merge_commit_guard.check(data) is not None
	except Exception as error:
		print(f"crap-gate: pre-check failed, gate runs ({error})", file=sys.stderr)
		return False


def onCommit(data, root, config, limits, work):
	try:
		commitCheck(root, config, limits, work)
	except qg_common.SandboxError as error:
		deny(f"crap-gate: commit refused, environment problem on this machine, not the code: {error}. The test "
			"suite never runs outside the sandbox. Stop and ask the user to fix bwrap (`bwrap --ro-bind / / "
			"--unshare-net true` in their terminal shows the error); don't change the code for this.")
	except qg_common.NotStarted as error:
		deny(f"crap-gate: commit refused, a gate command never started (environment or config, not the code): "
			f"{error}. Stop and ask the user to install the tool or change the command; don't change the code for this.")
	except qg_common.DeadlineReached as error:
		qg_common.deadlineNotice(root, "crap-gate", error)
		deny(f"crap-gate: commit refused, the check didn't finish: {error}. Stop and report it; the user decides "
			"(a longer deadline in config/local.json quality.hook_timeouts, or a faster suite).")


def onConfigError(handler, error):
	"""The quality-gate config doesn't load (a tool a command needs is missing): refuse a commit, let a
	developer finish (module doc)."""
	if handler is onCommit:
		deny(f"crap-gate: commit refused, the quality-gate config doesn't load (environment or config, not the "
			f"code): {error}. Stop and ask the user; don't change the code for this.")
	else:
		print(f"crap-gate: not checked, config problem: {error}; the commit check will stop the flow", file=sys.stderr)


def commitCheck(root, config, limits, work):
	staged = stagedPaths(root)
	source = [p for p in staged if qg_common.isSource(p, config)]
	base = qg_common.defaultBase(root)
	if staged and not source:
		tests = [p for p in staged if isTest(p, config)]
		if tests and isFirstTestsCommit(root, config, base):
			findings = redFindings(root, config, limits, {qg_common.languageOf(p, config) for p in tests})
			if findings:
				deny("crap-gate: commit refused, the first tests commit must fail only because the implementation "
					"is missing:\n" + qg_common.truncate(findings, limits["reason_max_chars"])
					+ "\nSend this to a fresh qa (verbatim) and commit its corrected tests.")
			return
		if tests:
			return
	passFile = os.path.join(work, "pass")
	if os.path.isfile(passFile):
		with open(passFile, encoding="utf-8") as f:
			if f.read().strip() == qg_common.fingerprint(root, base):
				return
	_, findings, failing = check(root, config, limits, work)
	if findings:
		deny("crap-gate: commit refused, the changed code breaks the quality limits:\n"
			+ qg_common.truncate(findings, limits["reason_max_chars"])
			+ "\nSend the items to a fresh developer (verbatim); a needed test goes to qa first." + detectedNotes(config))
	elif source and failing:
		deny("crap-gate: commit refused, the test suite fails: " + "; ".join(failing)
			+ "\nAn implementation commit must leave the suite green (test-first.md). Send the failures to a "
			"fresh developer (verbatim), or a test the plan shows is wrong to qa.")


def handlerFor(data):
	"""The function for this hook input, or None when crap-gate has nothing to do with it."""
	event = data.get("hook_event_name")
	if event == "SubagentStop" and data.get("agent_type") == "developer":
		return onDeveloperStop
	if event == "PreToolUse" and data.get("agent_type") == "developer" and data.get("tool_name") == "SubagentHandback":
		return onDeveloperHandback
	if event == "PreToolUse" and data.get("agent_type") in MAIN_TYPES and not data.get("agent_id") \
			and data.get("tool_name") == "Bash" and COMMIT.search((data.get("tool_input") or {}).get("command") or ""):
		return onCommit
	return None


def main():
	data = json.load(sys.stdin)
	root = hookonly.gitRoot(data)
	handler = handlerFor(data)
	if not root or handler is None:
		return
	if handler is onCommit and deniedBefore(data):
		return
	try:
		config, limits = qg_common.loadConfig(root)
	except qg_common.homeconfig.ConfigError as error:
		onConfigError(handler, error)
		return
	if config is None:
		return
	work = hookonly.stateDir(root, "quality")
	os.makedirs(work, exist_ok=True)
	qg_common.startDeadline("commit_gate_seconds" if handler is onCommit else "developer_gate_seconds")
	removeStale(work)
	handler(data, root, config, limits, work)


if __name__ == "__main__":
	def onError(error):
		print(f"crap-gate error (not checked): {error}", file=sys.stderr)
		sys.exit(0)

	import hookrun
	hookrun.run("crap-gate", main, onError)
	sys.exit(0)

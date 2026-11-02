"""Shared pieces of the quality gates (crap-gate.py, mutation.py): config, diff ranges, coverage.

Config: the repo's .claude/quality-gates.json (template: ~/.claude/templates/quality-gates.json) holds
the per-language commands (checkCommands: the optional "checks" auto-merge.py runs after `test`), source
and exclude paths; its "limits" override quality-defaults.json next
to this file. Without the repo file the gates do nothing. It and .claude/guard-paths.json are read from
the diff base commit, never the working tree: the commands in it run here, outside Claude Code's
sandbox, so a branch can't change what runs; its config changes take effect once merged.

Commands run in bwrap (qg_sandbox.json next to this file; runCommand): read-only /, an empty $HOME with
only the configured tool paths put back, other homes and /mnt hidden, a fresh /tmp, no network, a
cleared environment. Hooks run
with the user's full access (code.claude.com/docs/en/sandboxing), and these commands run code agents
wrote. No usable bwrap -> SandboxError; the command is never run unsandboxed.
QG_UNSANDBOXED_FOR_TESTS=1 skips bwrap only while pytest runs (PYTEST_CURRENT_TEST is set too): for this
module's own test suite inside Claude Code's sandbox, where bwrap can't nest. Set anywhere else it is
ignored with a warning on stderr. A repo's quality-gates.json may add "protect" entries (read-only inside
the checkout) on top of qg_sandbox.json's.

Changed lines: working tree (uncommitted and untracked included) against the merge-base of HEAD with
the integration branch (integrationRef: origin/master, else origin/main, never the local branch; in
local mode, FLOW_INTEGRATION=local, the local main or master, as ~/.claude/skills/feature-flow/base-ref.sh). Test paths from
.claude/guard-paths.json ("tests", "qaConfig") are always excluded, plus the config's "exclude".

Coverage is parsed into {path: {line: hits}} holding executable lines only:
- LCOV (coverage.py / pytest-cov): `SF:` and `DA:<line>,<hits>`; lines excluded with
  `# pragma: no cover` never appear in DA (measured 2026-10-30, pytest-cov 7.1.0).
- Go coverprofile: `mode: ...` then `<import path>/<file>:<l1>.<c1>,<l2>.<c2> <stmts> <count>`; the
  import path is mapped back through the module line of go.mod. A block ending at column 1 does not
  cover its last line.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import hookonly

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_FILE = os.path.join(HERE, "quality-defaults.json")
CONFIG_FILE = os.path.join(".claude", "quality-gates.json")
GUARD_PATHS_FILE = os.path.join(".claude", "guard-paths.json")
LANGS_FILE = os.path.join(HERE, "qg_langs.json")
SANDBOX_FILE = os.path.join(HERE, "qg_sandbox.json")
UNSANDBOXED_ENV = "QG_UNSANDBOXED_FOR_TESTS"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
GO_BLOCK = re.compile(r"^(.+):(\d+)\.(\d+),(\d+)\.(\d+) (\d+) (\d+)$")


def handbackText(data):
	"""Every string in a SubagentHandback tool_input, joined: its field names aren't documented, so a
	marker line like `NEEDS-TEST:` is looked for in all of them."""
	found = []

	def walk(value):
		if isinstance(value, str):
			found.append(value)
		elif isinstance(value, dict):
			for item in value.values():
				walk(item)
		elif isinstance(value, list):
			for item in value:
				walk(item)

	walk(data.get("tool_input") or {})
	return "\n".join(found)


def markWait(root, agentId, what, maxSeconds):
	"""Record that a gate now holds the agent, so status displays can tell waiting from working.

	Adds "hook_wait": {"since", "what", "max_seconds"} to subagent-budget.py's state
	(.hook-only/budget/<agent_id>.json); excludeWait removes it when the wait ends. Display only: the
	budget hook never reads it (the agent makes no tool call while held). Never fails the gate."""
	if not agentId:
		return
	stateFile = os.path.join(hookonly.stateDir(root, "budget"), f"{agentId}.json")
	try:
		with open(stateFile, encoding="utf-8") as f:
			state = json.load(f)
		state["hook_wait"] = {"since": time.time(), "what": what, "max_seconds": maxSeconds}
		with open(stateFile, "w", encoding="utf-8") as f:
			json.dump(state, f)
	except (OSError, ValueError, TypeError):
		pass


def excludeWait(root, agentId, seconds):
	"""Take a gate's wait out of the agent's time budget and its recorded minutes.

	A gate on SubagentHandback can hold the agent for minutes (the mutation scan, the coverage run). That
	is hook time, not the agent's work, and no plan can size it. Shifts `started` in subagent-budget.py's
	state (.hook-only/budget/<agent_id>.json) forward, drops the "hook_wait" mark markWait left, and logs
	a HookWait event that cost-history.py rows subtracts from the round's actual minutes. A wait under a
	second only drops the mark."""
	if not agentId:
		return
	budgetDir = hookonly.stateDir(root, "budget")
	stateFile = os.path.join(budgetDir, f"{agentId}.json")
	counted = seconds >= 1
	try:
		with open(stateFile, encoding="utf-8") as f:
			state = json.load(f)
		marked = state.pop("hook_wait", None) is not None
		if counted:
			state["started"] = float(state["started"]) + seconds
		if counted or marked:
			with open(stateFile, "w", encoding="utf-8") as f:
				json.dump(state, f)
	except (OSError, ValueError, KeyError, TypeError):
		pass
	if not counted:
		return
	os.makedirs(budgetDir, exist_ok=True)
	with open(os.path.join(budgetDir, "events.jsonl"), "a", encoding="utf-8") as f:
		f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "HookWait",
			"agent_id": agentId, "seconds": round(seconds, 1)}) + "\n")


def git(root, *args, check=True):
	result = subprocess.run(["git", "-C", root, *args], capture_output=True, check=check)
	return result.stdout.decode("utf-8", "surrogateescape")


def loadJson(path):
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def committedJson(root, relPath):
	"""A repo config file as committed at the diff base, or None (no base, or no file there). Never the
	working tree: see the module docstring."""
	try:
		base = defaultBase(root)
	except (RuntimeError, subprocess.CalledProcessError):
		return None
	text = baseText(root, base, relPath)
	return None if text is None else json.loads(text)


def loadConfig(root):
	"""(config, limits), or (None, None) when the diff base has no quality-gates.json."""
	config = committedJson(root, CONFIG_FILE)
	if config is None:
		return None, None
	limits = {**loadJson(DEFAULTS_FILE), **(config.get("limits") or {})}
	excludes = list(config.get("exclude") or [])
	guard = committedJson(root, GUARD_PATHS_FILE)
	testPaths = []
	if guard is not None:
		testPaths = list(guard.get("tests") or []) + list(guard.get("qaConfig") or [])
		excludes += testPaths
	config["exclude"] = excludes
	# What only QA writes (guard-paths "tests" and "qaConfig"): mutation.py reuses a scan when nothing else changed.
	config["test_paths"] = testPaths
	return config, limits


def loadLangs():
	return loadJson(LANGS_FILE)


def globRegex(pattern):
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


def pathMatches(path, pattern):
	"""guard-paths style: "dir/" is a prefix, a pattern with * or ? is a glob, else exact or prefix."""
	if pattern.endswith("/"):
		return path.startswith(pattern)
	if "*" in pattern or "?" in pattern:
		return globRegex(pattern).match(path) is not None
	return path == pattern or path.startswith(pattern + "/")


def isExcluded(path, patterns):
	return any(pathMatches(path, p) for p in patterns)


def languageOf(path, config):
	ext = os.path.splitext(path)[1]
	for name, lang in (config.get("languages") or {}).items():
		if ext in lang.get("extensions", []):
			return name
	return None


def localMode():
	return os.environ.get("FLOW_INTEGRATION") == "local"


def integrationRef(root):
	"""The branch features start from and merge into (module doc), or None."""
	candidates = ("main", "master") if localMode() else ("origin/master", "origin/main")
	for branch in candidates:
		ref = f"refs/heads/{branch}" if localMode() else branch
		if git(root, "rev-parse", "-q", "--verify", ref, check=False).strip():
			return branch
	return None


def defaultBase(root):
	branch = integrationRef(root)
	if branch is None:
		raise RuntimeError("no integration branch to diff against (" + ("local main or master" if localMode()
			else "origin/master or origin/main") + ")")
	return git(root, "merge-base", "HEAD", branch).strip()


def parseDiff(text):
	"""{path: set of added or changed line numbers} from `git diff -U0` output."""
	changed, path = {}, None
	for line in text.splitlines():
		if line.startswith("+++ "):
			target = line[4:]
			path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
			if path is not None:
				changed.setdefault(path, set())
		elif path is not None and (match := HUNK.match(line)):
			start, count = int(match.group(1)), int(match.group(2) or "1")
			changed[path].update(range(start, start + count))
	return changed


def untracked(root):
	out = git(root, "ls-files", "--others", "--exclude-standard", "-z")
	return [p for p in out.split("\0") if p and not hookonly.isHookOnlyPath(p)]


def changedLines(root, base):
	"""{path: set of lines} changed in the working tree since base, untracked files included."""
	changed = parseDiff(git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", "--diff-filter=AMR", base, "--"))
	for path in untracked(root):
		try:
			with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
				count = len(f.read().splitlines())
		except OSError:
			continue
		changed[path] = set(range(1, count + 1))
	return {p: lines for p, lines in changed.items() if lines}


def sourceChanges(root, base, config):
	"""{language: {path: lines}} for changed files of a configured language that aren't excluded."""
	out = {}
	for path, lines in changedLines(root, base).items():
		lang = languageOf(path, config)
		if lang and not isExcluded(path, config["exclude"]) and os.path.isfile(os.path.join(root, path)):
			out.setdefault(lang, {})[path] = lines
	return out


def fingerprint(root, base):
	"""Identifies the code under check: base commit, tracked diff and untracked files."""
	digest = hashlib.sha256(base.encode())
	digest.update(subprocess.run(["git", "-C", root, "diff", "--binary", base, "--"], capture_output=True, check=True).stdout)
	for path in sorted(untracked(root)):
		digest.update(path.encode())
		try:
			with open(os.path.join(root, path), "rb") as f:
				digest.update(hashlib.sha256(f.read()).digest())
		except OSError:
			digest.update(b"unreadable")
	return digest.hexdigest()[:20]


def baseText(root, base, path):
	result = subprocess.run(["git", "-C", root, "show", f"{base}:{path}"], capture_output=True)
	return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def readText(root, path):
	with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
		return f.read()


def parseLcov(text, root):
	cover, path = {}, None
	for line in text.splitlines():
		if line.startswith("SF:"):
			sf = line[3:].strip()
			path = os.path.relpath(sf, root) if os.path.isabs(sf) else os.path.normpath(sf)
			cover.setdefault(path, {})
		elif line.startswith("DA:") and path is not None:
			number, hits = line[3:].split(",")[:2]
			cover[path][int(number)] = int(hits)
		elif line == "end_of_record":
			path = None
	return cover


def goModule(moduleDir):
	with open(os.path.join(moduleDir, "go.mod"), encoding="utf-8") as f:
		for line in f:
			if line.startswith("module "):
				return line.split()[1]
	raise RuntimeError(f"no module line in {moduleDir}/go.mod")


def parseGoCover(text, module, moduleRel):
	"""moduleRel: the module directory relative to the repo root ("" for the root)."""
	cover = {}
	for line in text.splitlines():
		match = GO_BLOCK.match(line.strip())
		if not match:
			continue
		name, l1, _, l2, c2, _, count = match.groups()
		if not name.startswith(module + "/"):
			continue
		path = os.path.normpath(os.path.join(moduleRel, name[len(module) + 1:]))
		lines = cover.setdefault(path, {})
		last = int(l2) if int(c2) > 1 else int(l2) - 1
		for number in range(int(l1), max(int(l1), last) + 1):
			lines[number] = max(lines.get(number, 0), int(count))
	return cover


def expand(value, mapping):
	return [expand(v, mapping) for v in value] if isinstance(value, list) else str(value).format(**mapping)


class SandboxError(RuntimeError):
	"""bwrap is missing or can't create its namespaces; nothing was run."""


def sandboxArgv(argv, cwd, env, writable, readable, settings, home, outer, protect=()):
	"""The bwrap command line for argv (pure, so it can be tested without bwrap).

	Mount order matters, later mounts cover earlier ones: read-only /, then empty tmpfs over /tmp, the
	'hide' paths and $HOME, then the configured tool paths, the paths to read, the writable roots, and
	last the protected entries inside each writable root, so those stay read-only. Missing paths are
	skipped. The
	environment is cleared: only the env_pass names (from outer, the hook's environment), HOME, the
	env_set values and the command's own env reach the command."""
	def full(path):
		return os.path.abspath(os.path.expanduser(path)) if path.startswith("~") else os.path.abspath(path)

	args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
	for path in settings.get("hide") or []:
		if os.path.isdir(path):
			args += ["--tmpfs", path]
	args += ["--tmpfs", home]
	for path in settings.get("home_read") or []:
		if os.path.exists(full(path)):
			args += ["--ro-bind", full(path), full(path)]
	for path in settings.get("home_write") or []:
		if os.path.exists(full(path)):
			args += ["--bind", full(path), full(path)]
	for path in readable:
		if path and os.path.exists(path):
			args += ["--ro-bind", full(path), full(path)]
	for path in writable:
		if path and os.path.exists(path):
			args += ["--bind", full(path), full(path)]
	for path in writable:
		for entry in [*(settings.get("protect") or []), *protect]:
			inner = os.path.join(full(path), entry) if path else ""
			if inner and os.path.lexists(inner):
				args += ["--ro-bind", inner, inner]
	args += ["--unshare-net", "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--clearenv"]
	values = {name: outer[name] for name in settings.get("env_pass") or [] if name in outer}
	values.update({"HOME": home, **(settings.get("env_set") or {}), **(env or {})})
	for name, value in values.items():
		args += ["--setenv", name, str(value)]
	return args + ["--chdir", full(cwd), "--", *argv]


_probed = []


def checkSandbox(settings, home):
	"""Raises SandboxError unless bwrap starts with the same options a real run uses; once per process."""
	if _probed:
		return
	probe = sandboxArgv(["true"], "/", {}, [], [], settings, home, {})
	try:
		result = subprocess.run(probe, capture_output=True, stdin=subprocess.DEVNULL,
			timeout=settings.get("probe_timeout_seconds", 10))
	except (OSError, subprocess.TimeoutExpired) as error:
		raise SandboxError(f"bwrap unavailable: {error}") from error
	if result.returncode != 0:
		raise SandboxError("bwrap unavailable: " + result.stderr.decode("utf-8", "replace").strip()[-500:])
	_probed.append(True)


def unsandboxedForTests():
	if os.environ.get(UNSANDBOXED_ENV) != "1":
		return False
	if os.environ.get("PYTEST_CURRENT_TEST"):
		return True
	print(f"qg_common: {UNSANDBOXED_ENV}=1 ignored outside pytest; running in bwrap", file=sys.stderr)
	return False


def runCommand(argv, cwd, env=None, timeout=None, writable=(), readable=(), protect=()):
	"""(exit code or None on timeout, seconds, output tail). A leading ~ in the program is expanded; the whole
	process group is killed on timeout. Runs in bwrap (sandboxArgv) with writable and readable put back;
	raises SandboxError rather than run unsandboxed."""
	start = time.time()
	argv = [os.path.expanduser(argv[0]), *argv[1:]]
	cwd = os.path.abspath(cwd)
	writable = [os.path.abspath(p) for p in writable]
	readable = [os.path.abspath(p) for p in readable if p]
	if unsandboxedForTests():
		command, procEnv = argv, {**os.environ, **(env or {})}
	else:
		settings, home = loadJson(SANDBOX_FILE), os.path.expanduser("~")
		checkSandbox(settings, home)
		command = sandboxArgv(argv, cwd, env, list(writable) or [cwd], readable, settings, home, os.environ,
			protect)
		procEnv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
	proc = subprocess.Popen(command, cwd=cwd, env=procEnv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT, start_new_session=True)
	try:
		out, _ = proc.communicate(timeout=timeout)
		code = proc.returncode
	except subprocess.TimeoutExpired:
		os.killpg(proc.pid, signal.SIGKILL)
		out, _ = proc.communicate()
		code = None
	return code, time.time() - start, out.decode("utf-8", "replace")[-4000:]


def commandEnv(langConfig, mapping):
	env = {k: str(v).format(**mapping) for k, v in (langConfig.get("env") or {}).items()}
	env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
	return env


def checkCommands(lang, langConfig):
	"""The language's optional "checks" in quality-gates.json, as [(argv, emptyOutput)]: more completion
	checks (format, lint) that auto-merge.py runs after `test`, the same way. An entry is an argv list like
	`test`, or {"argv": [...], "empty_output": true} when it passes only with exit 0 and no output (e.g.
	`gofmt -l`). ValueError naming the entry for any other shape: a malformed check never passes silently."""
	entries = langConfig.get("checks") or []
	if not isinstance(entries, list):
		raise ValueError(f"{lang}: \"checks\" must be a list")
	commands = []
	for i, entry in enumerate(entries):
		argv, emptyOutput = entry, False
		if isinstance(entry, dict):
			if set(entry) - {"argv", "empty_output"} or not isinstance(entry.get("empty_output", False), bool):
				raise ValueError(f"{lang}: checks[{i}] takes only \"argv\" and a boolean \"empty_output\"")
			argv, emptyOutput = entry.get("argv"), entry.get("empty_output", False)
		if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
			raise ValueError(f"{lang}: checks[{i}] needs a non-empty argv list of strings")
		commands.append((argv, emptyOutput))
	return commands


def measureCoverage(root, lang, langConfig, workDir, timeout, repo=None, protect=()):
	"""Runs the language's coverage command in root (the repo or a scratch copy of it); repo is the real
	checkout for `{repo}` in the config (shared virtualenvs). (cover map or None, exit code, seconds, tail).
	The report and coverage data go to a fresh temporary directory, not workDir: workDir is under
	.hook-only, which the sandboxed command must not write (gate state such as block counts lives there)."""
	os.makedirs(workDir, exist_ok=True)
	out = tempfile.mkdtemp(prefix=f"qg-cov-{lang}-")
	try:
		report = os.path.join(out, f"coverage-{lang}.out")
		mapping = {"repo": repo or root, "root": root, "report": report}
		env = commandEnv(langConfig, mapping)
		env.setdefault("COVERAGE_FILE", os.path.join(out, f".coverage-{lang}"))
		cwd = os.path.join(root, langConfig.get("cwd", ""))
		readable = [repo] if repo and os.path.abspath(repo) != os.path.abspath(root) else []
		code, seconds, tail = runCommand(expand(langConfig["coverage"], mapping), cwd, env, timeout,
			writable=[root, out], readable=readable, protect=protect)
		if not os.path.isfile(report):
			return None, code, seconds, tail
		with open(report, encoding="utf-8") as f:
			text = f.read()
	finally:
		shutil.rmtree(out, ignore_errors=True)
	if langConfig.get("coverage_format") == "gocover":
		cover = parseGoCover(text, goModule(cwd), langConfig.get("cwd", ""))
	else:
		cover = parseLcov(text, root)
	return cover, code, seconds, tail


def truncate(lines, limit):
	"""Joins lines until the character limit, then says how many were left out."""
	out, used = [], 0
	for i, line in enumerate(lines):
		if used + len(line) + 1 > limit:
			out.append(f"... and {len(lines) - i} more")
			break
		out.append(line)
		used += len(line) + 1
	return "\n".join(out)


def ranges(numbers):
	"""[3, 4, 5, 9] -> "3-5, 9"."""
	numbers, parts, start = sorted(numbers), [], None
	for i, n in enumerate(numbers):
		if start is None:
			start = n
		if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
			parts.append(str(start) if start == n else f"{start}-{n}")
			start = None
	return ", ".join(parts)

"""Shared pieces of the quality gates (crap-gate.py, mutation.py): config, diff ranges, coverage.

Config: the repo's .claude/quality-gates.json (template: ~/.claude/templates/quality-gates.json) holds
the per-language commands (checkCommands: the optional "checks" auto-merge.py runs after `test`), source
and exclude paths; its "limits" override the home.json quality (homeconfig.load,
~/.claude/config/). Each language it lists is laid over the home toolchain profile of that name
(config/home.json toolchains; languageConfigs): a repo key replaces the profile's wholesale, and the {tool}
placeholders in command keys become the configured programs. A missing config never turns a gate off:
every home profile whose extensions match a file tracked at the diff base or added or changed in the
working tree since (outside the config's "exclude") is enabled on the profile alone (detectLanguages:
only home commands run, so a branch adding a file can't change what runs), whether or not the repo file exists or
lists it; `"languages": {"<lang>": false}` turns a language off. config["detected"] names the languages
enabled that way, so gate messages can say so and how to turn one off (detectedNote), and
config["from_profile"] the keys each language took from its profile (commandHint). Both are left out
when empty. Only neither the repo file nor a detected language means nothing to gate ((None, None)).
The repo file and .claude/guard-paths.json are read from
the diff base commit, never the working tree: the commands in it run here, outside Claude Code's
sandbox, so a branch can't change what runs; its config changes take effect once merged.

A command that never starts (its program missing or not executable, the shell's or bwrap's "not found":
gate_sandbox not_started_exit_codes / not_started_patterns) raises NotStarted instead of returning an exit
code, so no caller can read it as a red suite, a caught mutant or an expected red.

Commands run in bwrap (home.json gate_sandbox settings, homeconfig.load; runCommand): read-only /, an empty $HOME with
only the configured tool paths put back, other homes and /mnt hidden, a fresh /tmp, no network, a
cleared environment. Hooks run
with the user's full access (code.claude.com/docs/en/sandboxing), and these commands run code agents
wrote. No usable bwrap -> SandboxError; the command is never run unsandboxed.
bwrap is the absolute path home.json gate_sandbox bwrap_path names (never a PATH lookup), checked to be an
executable file before every use (fail closed). No environment variable turns the sandbox off, not even while pytest runs: a repo's settings env reaches the
hooks, so such a switch would let a repo run its own tests with the person's full access. (The home's own tests,
where bwrap can't nest inside Claude Code's sandbox, name a stand-in bwrap in their tree copy's local.json.) Inside every root a command reads or writes, the gate_sandbox
"hide_inside" entries (the hooks' state .hook-only, plans) are hidden and the "protect" entries read-only;
a repo's quality-gates.json may add "protect" entries on top of the home.json gate_sandbox settings'.

Changed lines: working tree (uncommitted and untracked included) against the merge-base of HEAD with
the integration branch (homeconfig.integrationBranch, the one rule every hook and skill script uses: the
local main in local mode, origin's default branch in PR mode). Test paths from .claude/guard-paths.json
("tests", "qaConfig") are always excluded, plus the config's "exclude".
No diff base (diffBase): in local mode a ConfigError, which the gates report (crap-gate refuses the commit,
mutation holds the reviewer's hand-back once), never a skipped gate; in PR mode, a repo whose origin has
no default branch yet (not pushed) has nothing to diff against, and loadConfig / committedJson give None.

Coverage is parsed into {path: {line: hits}} holding executable lines only:
- LCOV (coverage.py / pytest-cov): `SF:` and `DA:<line>,<hits>`; lines excluded with
  `# pragma: no cover` never appear in DA (measured with pytest-cov 7.1.0). A relative `SF:` is
  relative to the command's cwd (coverage.py writes paths under it that way), not the repo root.
- Go coverprofile: `mode: ...` then `<import path>/<file>:<l1>.<c1>,<l2>.<c2> <stmts> <count>`; the
  import path is mapped back through the module line of go.mod. A block ending at column 1 does not
  cover its last line.

Coverage targets from the diff: in the coverage key only, an argv item holding {changed_dirs} is repeated
once per directory of the language's changed files, relative to the command's cwd (changedDirs,
expandCoverage), so `--cov={changed_dirs}` becomes `--cov=pkg/a --cov=pkg/b` and a repo needn't list its
packages. A directory, not the file: coverage.py reads a source value as "directories or importable names
(packages or modules)" (coverage.readthedocs.io/en/latest/source.html), so `--cov=pkg/mod.py` is taken as
a module name, never imported, and no report is written (measured with pytest-cov 7.1.0, coverage
7.17.0); a source directory also reports its never-executed files at its root (same page: "Only importable
files (ones at the root of the tree, or in directories with a __init__.py file)"), which bare --cov never
does. A changed file is always at the root of its own target, so it is reported, executed or not. With
derived targets (derivesTargets) a changed file still missing from the report (e.g. the coverage config
omits it) is a crap-gate finding, never a silent pass. A command without {changed_dirs} (a repo's own
--cov list) runs as written.
"""
import copy
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import hookonly  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(".claude", "quality-gates.json")
GUARD_PATHS_FILE = os.path.join(".claude", "guard-paths.json")
LANGS_FILE = os.path.join(HERE, "qg_langs.json")
# {name} in a command key: a home tool ({uv}) is replaced at load, a runtime one is left for expand();
# {{ and }} are str.format escapes and stay as they are.
PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
RUNTIME_PLACEHOLDERS = ("repo", "root", "report")
# Runtime placeholders only the coverage key takes: one argv item per value (module doc).
CHANGED_DIRS = "changed_dirs"
COVERAGE_PLACEHOLDERS = RUNTIME_PLACEHOLDERS + (CHANGED_DIRS,)
COMMAND_KEYS = ("coverage", "test", "checks", "go_bin", "env")
SELECT_COMMAND_KEYS = ("pytest", "python")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
GO_BLOCK = re.compile(r"^(.+):(\d+)\.(\d+),(\d+)\.(\d+) (\d+) (\d+)$")


def handbackStrings(data):
	"""Every string in a SubagentHandback tool_input, in order: its field names aren't documented, so a
	marker line like `NEEDS-TEST:` (handbackText) or a report's first line (crap-gate.py reportStatuses) is
	looked for in all of them."""
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
	return found


def handbackText(data):
	"""Every string in a SubagentHandback tool_input (handbackStrings), joined."""
	return "\n".join(handbackStrings(data))


def hookTimeout(name):
	"""A gate's own deadline in seconds (home.json quality.hook_timeouts, local.json's valid value applied; a
	repo's limits can't change it). Never above the settings.json timeout of its registration, the outer
	ceiling (registry rules hook_ceilings_seconds; homeconfig refuses a deadline above it)."""
	return float(homeconfig.load("quality")["hook_timeouts"][name])


class DeadlineReached(Exception):
	"""The gate's deadline (startDeadline) ran out: the check didn't finish. Never a pass, never a caught mutant."""


_deadline = None  # (monotonic end, hook_timeouts key) of this process's gate, or None (not a gate run)


def startDeadline(name):
	"""Start this process's gate deadline: hookTimeout(name) seconds from now. runCommand gives each command at
	most what is left and raises DeadlineReached when it runs out."""
	global _deadline
	_deadline = (time.monotonic() + hookTimeout(name), name)


def remaining():
	"""Seconds left before the gate's deadline, or None without one."""
	return None if _deadline is None else _deadline[0] - time.monotonic()


def deadlineError(what):
	key = _deadline[1] if _deadline else "?"
	return DeadlineReached(f"{what}: stopped at the gate's deadline (quality.hook_timeouts.{key} "
		f"{hookTimeout(key) if _deadline else '?'} s) before it finished; not a pass")


def deadlineNotice(root, gate, error):
	"""The person's notice of a gate that hit its deadline: a gate-deadline event (eventlog) and stderr. Never
	raises (the gate's own refusal or message carries the failure)."""
	print(f"{gate}: {error}", file=sys.stderr)
	try:
		import eventlog

		key = _deadline[1] if _deadline else ""
		eventlog.emit(root, "gate-deadline", gate, {"gate": gate, "key": key, "seconds": hookTimeout(key) if key else None})
	except Exception as failure:  # the notice is best effort; the gate's answer already says it didn't finish
		print(f"{gate}: gate-deadline event not written ({failure})", file=sys.stderr)


def git(root, *args, check=True):
	result = subprocess.run(["git", "-C", root, *args], capture_output=True, check=check)
	return result.stdout.decode("utf-8", "surrogateescape")


def loadJson(path):
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def diffBase(root):
	"""The diff base commit; None only in PR mode without one (module doc). Local mode without one: ConfigError."""
	mode = homeconfig.flowMode(root)
	try:
		return defaultBase(root, mode)
	except (RuntimeError, subprocess.CalledProcessError) as error:
		if mode == "local":
			raise homeconfig.ConfigError(f"local mode, but no diff base: {error}") from error
		return None


def committedJson(root, relPath):
	"""A repo config file as committed at the diff base, or None (no file there, or PR mode without a base:
	diffBase). Never the working tree: see the module docstring."""
	base = diffBase(root)
	if base is None:
		return None
	text = baseText(root, base, relPath)
	return None if text is None else json.loads(text)


def loadConfig(root):
	"""(config, limits), or (None, None) when there is nothing to gate: PR mode without a diff base
	(diffBase), or neither quality-gates.json at the base nor a file of a language with a home profile
	(detectLanguages). ConfigError when it can't tell (diffBase, the flow mode, a config that doesn't load)."""
	base = diffBase(root)
	if base is None:
		return None, None
	text = baseText(root, base, CONFIG_FILE)
	config = {} if text is None else json.loads(text)
	listed, off = repoLanguages(config.get("languages"))
	detected = [lang for lang in detectLanguages(root, base, config.get("exclude") or [])
		if lang not in listed and lang not in off]
	if text is None and not detected:
		return None, None
	limits = {**homeconfig.load("quality"), **repoLimits(config.get("limits"))}
	if "languages" in config or detected:
		languages, profileScratch = languageConfigs(listed)
		for lang in detected:
			languages[lang], scratch = detectedConfig(lang)
			profileScratch += [p for p in scratch if p not in profileScratch]
		config["languages"] = languages
		markOrigins(config, listed, detected)
		if "scratch_exclude" not in config and profileScratch:
			config["scratch_exclude"] = profileScratch
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


def repoLimits(limits):
	"""The repo's "limits", checked only for keys the config merge renamed: an old name is a
	ConfigError naming the new one, so a repo value never silently stops applying."""
	for key in limits or {}:
		new = homeconfig.renamedKey(("quality", key))
		if new:
			raise homeconfig.ConfigError(f"{CONFIG_FILE}: limits.{key} was renamed to limits.{new.split('.', 1)[1]} "
				f"({homeconfig.where('quality')}); rename it")
	return limits or {}


def repoLanguages(entries):
	"""({lang: entry} the repo enables, {langs} it turns off with false). ConfigError for any other value,
	so a typo never silently means off."""
	listed, off = {}, set()
	for lang, entry in (entries or {}).items():
		if entry is False:
			off.add(lang)
		elif isinstance(entry, dict):
			listed[lang] = entry
		else:
			raise homeconfig.ConfigError(f"{CONFIG_FILE}: languages.{lang} must be an object or false")
	return listed, off


def homeProfiles():
	"""{lang: profile} of the home toolchain profiles, "_" keys left out."""
	profiles = homeconfig.load("toolchains").get("languages") or {}
	return {lang: profile for lang, profile in profiles.items() if not lang.startswith("_")}


def detectionFiles(root, base):
	"""The files detection looks at: tracked at base, plus what the working tree added or changed since (a
	branch bringing a repo's first file of a language is checked too; the commands still come from the home
	profile, never the branch)."""
	files = {p for p in git(root, "ls-tree", "-r", "-z", "--name-only", base).split("\0") if p}
	files.update(p for p in git(root, "diff", "--name-only", "-z", "--diff-filter=AMR", base, "--").split("\0") if p)
	files.update(untracked(root))
	return sorted(files)


def detectLanguages(root, base, excludes):
	"""Sorted names of the home profiles whose extensions match a detection file (detectionFiles) outside
	excludes."""
	byExtension = {}
	for lang, profile in homeProfiles().items():
		for extension in profile.get("extensions") or []:
			byExtension.setdefault(extension, lang)
	found = set()
	for path in detectionFiles(root, base):
		lang = byExtension.get(os.path.splitext(path)[1])
		if lang and lang not in found and not isExcluded(path, excludes):
			found.add(lang)
	return sorted(found)


def detectedConfig(lang):
	"""(profile config, scratch_exclude) of a language enabled by detection; a ConfigError (a tool its
	commands need is missing) also says how to turn the language off."""
	try:
		languages, scratch = languageConfigs({lang: {}})
	except homeconfig.ConfigError as error:
		raise homeconfig.ConfigError(f"{error} ({detectedNote(lang)})") from error
	return languages[lang], scratch


def markOrigins(config, listed, detected):
	"""Sets config["detected"] and config["from_profile"] (module doc), each only when non-empty."""
	if detected:
		config["detected"] = list(detected)
	origins = {}
	for lang, merged in config["languages"].items():
		keys = sorted(set(merged) - set(listed.get(lang) or {}))
		if keys:
			origins[lang] = keys
	if origins:
		config["from_profile"] = origins


def detectedNote(lang):
	"""How a gate message says a language was enabled automatically and how to turn it off."""
	return (f"{lang} was enabled automatically: files with its extensions are tracked and the home profile "
		f"{homeconfig.where('toolchains')} has it; turn it off with \"languages\": {{\"{lang}\": false}} in {CONFIG_FILE}")


def commandHint(config, lang, key):
	"""For a gate message about a command that couldn't run: the key, where its value comes from (the home
	profile or the repo file) and where to override it; for a detected language also how to turn it off."""
	top = key.split(".")[0]
	fromProfile = top in ((config.get("from_profile") or {}).get(lang) or [])
	source = f"the home profile {homeconfig.where('toolchains')}" if fromProfile else CONFIG_FILE
	hint = f"the command is languages.{lang}.{key} (value from {source}); override it in {CONFIG_FILE}"
	if lang in (config.get("detected") or []):
		hint += "; " + detectedNote(lang)
	return hint


def toolPrograms(toolchains):
	"""{tool: program as configured} from home.json toolchains "tools" (local.json applied), checked for shape."""
	tools = {}
	for name, program in (toolchains.get("tools") or {}).items():
		if name.startswith("_"):
			continue
		if not isinstance(program, str) or not program or "{" in program or "}" in program:
			raise homeconfig.ConfigError(f"{homeconfig.where('toolchains')}: tools.{name} must be a program "
				"name or path without braces")
		tools[name] = program
	return tools


def toolPath(name, program, where):
	"""The absolute path of a tool a command needs. A path (absolute or ~) is expanded and must be an
	executable file; a bare name is looked up on this process's PATH (shutil.which). The gates run commands
	in bwrap, where a name may resolve differently, so the command gets the path found here. Never guesses
	an install location: ConfigError naming the tool, the key that needs it and the local.json fix."""
	isPath = program.startswith("~") or os.path.isabs(program)
	if isPath:
		found = os.path.expanduser(program)
		found = found if os.path.isfile(found) and os.access(found, os.X_OK) else None
	elif "/" in program or os.sep in program:
		found = None
	else:
		found = shutil.which(program)
	if found:
		return os.path.abspath(found)
	why = f"{program!r} is not an executable file" if isPath else (f"{program!r} is not on PATH" if "/" not in program
		else f"{program!r} is neither a bare name nor an absolute or ~ path")
	raise homeconfig.ConfigError(f"tool {{{name}}} needed by {where}: {why}; set it in "
		f"{homeconfig.path(homeconfig.LOCAL)}: {{\"toolchains\": {{\"tools\": {{\"{name}\": \"<absolute path>\"}}}}}}")


def resolveTools(value, tools, where, resolved=None, runtime=RUNTIME_PLACEHOLDERS):
	"""value with every {tool} replaced by the tool's absolute path (toolPath, once per tool and call through
	`resolved`); the runtime placeholders the key takes (`runtime`) and {{ }} kept. ConfigError naming any
	other placeholder, so a typo like {vu} fails at load instead of running a wrong command. Only tools a
	command uses are resolved."""
	resolved = {} if resolved is None else resolved
	if isinstance(value, list):
		return [resolveTools(v, tools, f"{where}[{i}]", resolved, runtime) for i, v in enumerate(value)]
	if isinstance(value, dict):
		return {k: resolveTools(v, tools, f"{where}.{k}", resolved, runtime) for k, v in value.items()}
	if not isinstance(value, str):
		return value

	def substitute(match):
		name = match.group(1)
		if name is None or name in runtime:
			return match.group(0)
		if name in tools:
			if name not in resolved:
				resolved[name] = toolPath(name, tools[name], where)
			return resolved[name]
		raise homeconfig.ConfigError(f"unknown placeholder {{{name}}} in {where} (tools in "
			f"{homeconfig.where('toolchains')}: {', '.join(sorted(tools))}; runtime: "
			f"{', '.join(runtime)})")

	return PLACEHOLDER.sub(substitute, value)


def languageConfigs(repoLanguages):
	"""(languages, scratch_exclude): the repo's languages.<lang> entries laid over the home toolchain
	profiles, and the profiles' scratch_exclude for the listed languages (used when the repo has none).

	Every reader of the languages config gets it through loadConfig, which calls this. Per language: the
	profile's keys ("_" comments and scratch_exclude aside), then the repo's, each replacing the profile's
	key wholesale (a list replaces, [] turns it off). A language without a profile is the repo's entry
	alone; loadConfig adds the detected languages (detectedConfig: an empty entry). Tool placeholders in the command keys become
	absolute paths (resolveTools, toolPath: a missing tool fails the load, naming it); {changed_dirs} is
	accepted in coverage only; other keys are left alone (patterns may hold braces)."""
	toolchains = homeconfig.load("toolchains")
	tools, profiles = toolPrograms(toolchains), toolchains.get("languages") or {}
	languages, scratch, resolved = {}, [], {}
	for lang, entry in (repoLanguages or {}).items():
		profile = {k: v for k, v in (profiles.get(lang) or {}).items() if not k.startswith("_")}
		scratch += [p for p in profile.pop("scratch_exclude", None) or [] if p not in scratch]
		merged = {**profile, **copy.deepcopy(entry)}
		for key in COMMAND_KEYS:
			if key in merged:
				merged[key] = resolveTools(merged[key], tools, f"languages.{lang}.{key}", resolved,
					COVERAGE_PLACEHOLDERS if key == "coverage" else RUNTIME_PLACEHOLDERS)
		select = merged.get("select")
		if isinstance(select, dict):
			for key in SELECT_COMMAND_KEYS:
				if key in select:
					select[key] = resolveTools(select[key], tools, f"languages.{lang}.select.{key}",
						resolved)
		languages[lang] = merged
	return languages, scratch


def loadLangs():
	return loadJson(LANGS_FILE)


def isGlob(pattern):
	"""Whether a guard-paths or exclude entry is a glob (pathMatches)."""
	return "*" in pattern or "?" in pattern


def globRegex(pattern):
	"""The regex of a glob entry (pathMatches): anchored at both ends, or only at the start for a directory glob
	(a trailing "/"), which then matches every path below a directory the glob names."""
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
	return re.compile(out if pattern.endswith("/") else out + r"\Z")


def pathMatches(path, pattern):
	"""The one rule for guard-paths.json entries and quality-gates exclude patterns (repo-relative, "/"-separated).
	A glob (with * or ?): `*` and `?` stay within one path segment, `**/` is any number of directories (none
	included, so `**/test_*.py` matches `test_a.py` at the root too), `**` elsewhere anything; it matches the whole
	path, or with a trailing "/" every path below a directory it matches. Otherwise "dir/" is a directory prefix,
	and a bare entry is the path itself or a directory prefix."""
	if isGlob(pattern):
		return globRegex(pattern).match(path) is not None
	if pattern.endswith("/"):
		return path.startswith(pattern)
	return path == pattern or path.startswith(pattern + "/")


def isExcluded(path, patterns):
	return any(pathMatches(path, p) for p in patterns)


def languageOf(path, config):
	ext = os.path.splitext(path)[1]
	for name, lang in (config.get("languages") or {}).items():
		if ext in lang.get("extensions", []):
			return name
	return None


def isSource(path, config):
	return bool(languageOf(path, config)) and not isExcluded(path, config["exclude"])


def defaultBase(root, mode=None):
	"""The merge-base of HEAD with the integration branch (homeconfig.integrationBranch in mode, default the
	checkout's); RuntimeError without the branch."""
	mode = mode or homeconfig.flowMode(root)
	branch = homeconfig.integrationBranch(root, mode)
	if branch is None:
		raise RuntimeError("no integration branch to diff against (" + ("local main or master" if mode == "local"
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


def parseLcov(text, root, cwd=None):
	"""{repo-relative path: {line: hits}}; a relative SF is relative to cwd (default root; module doc)."""
	cover, path = {}, None
	for line in text.splitlines():
		if line.startswith("SF:"):
			sf = line[3:].strip()
			path = os.path.relpath(sf if os.path.isabs(sf) else os.path.join(cwd or root, sf), root)
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


def placeholderNames(item):
	return {match.group(1) for match in PLACEHOLDER.finditer(item) if match.group(1) is not None}


def derivesTargets(langConfig):
	"""Whether the language's coverage command measures the changed files' directories ({changed_dirs})."""
	return any(CHANGED_DIRS in placeholderNames(str(item)) for item in langConfig.get("coverage") or [])


def changedDirs(root, cwd, files):
	"""Sorted directories of files (repo-relative paths under root), relative to cwd; ["."] (all of cwd)
	when files is empty: never an empty target list, which would measure nothing."""
	return sorted({os.path.relpath(os.path.join(root, os.path.dirname(path)), cwd) for path in files or ()}) or ["."]


def expandCoverage(argv, mapping, dirs):
	"""expand() for the coverage command: an item holding {changed_dirs} becomes one item per directory."""
	out = []
	for item in argv:
		if CHANGED_DIRS in placeholderNames(str(item)):
			out += [str(item).format(**mapping, **{CHANGED_DIRS: directory}) for directory in dirs]
		else:
			out.append(expand(item, mapping))
	return out


class SandboxError(RuntimeError):
	"""bwrap is missing or can't create its namespaces; nothing was run."""


class NotStarted(RuntimeError):
	"""A command never ran: its program is missing or can't execute (module doc). Never a test result."""


def startProblem(program, cwd, env):
	"""Why a command's program can't start, or None: a path must be an executable file, a bare name must be
	on the PATH the command gets (its env's, else this process's, which gate_sandbox env_pass hands on)."""
	if os.sep in program:
		path = program if os.path.isabs(program) else os.path.join(cwd, program)
		return None if os.path.isfile(path) and os.access(path, os.X_OK) else f"{program!r} is not an executable file"
	return None if shutil.which(program, path=(env or {}).get("PATH")) else f"{program!r} is not on PATH"


def startFailed(code, output, settings):
	"""Whether a finished command's result means its program never started (gate_sandbox not_started_*)."""
	if code is None or code == 0:
		return False
	return code in settings["not_started_exit_codes"] or any(p in output for p in settings["not_started_patterns"])


def sandboxArgv(argv, cwd, env, writable, readable, settings, home, outer, protect=()):
	"""The bwrap command line for argv (pure, so it can be tested without bwrap).

	Mount order matters, later mounts cover earlier ones: read-only /, then empty tmpfs over /tmp, the
	'hide' paths and $HOME, then the configured tool paths, the paths to read, the writable roots, then
	the protected entries inside each writable root, so those stay read-only, and last the 'hide_inside'
	entries inside every root, readable or writable: an empty read-only tmpfs over a directory, /dev/null
	over a file, so the command sees nothing of them. Missing paths are skipped. The
	environment is cleared: only the env_pass names (from outer, the hook's environment), HOME, the
	env_set values and the command's own env reach the command."""
	def full(path):
		return os.path.abspath(os.path.expanduser(path)) if path.startswith("~") else os.path.abspath(path)

	args = [settings["bwrap_path"], "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
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
	for path in [*readable, *writable]:
		for entry in settings.get("hide_inside") or []:
			inner = os.path.join(full(path), entry) if path else ""
			if inner and os.path.isdir(inner):
				args += ["--tmpfs", inner, "--remount-ro", inner]
			elif inner and os.path.lexists(inner):
				args += ["--ro-bind", "/dev/null", inner]
	args += ["--unshare-net", "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--clearenv"]
	values = {name: outer[name] for name in settings.get("env_pass") or [] if name in outer}
	values.update({"HOME": home, **(settings.get("env_set") or {}), **(env or {})})
	for name, value in values.items():
		args += ["--setenv", name, str(value)]
	return args + ["--chdir", full(cwd), "--", *argv]


_probed = []


def bwrapProblem(settings):
	"""Why gate_sandbox bwrap_path can't be the sandbox, or None: it must be an absolute path to an executable file.
	bwrap is never looked up on PATH: a repo's settings env reaches the hooks and could put a fake one first."""
	path = settings.get("bwrap_path")
	if not isinstance(path, str) or not os.path.isabs(path):
		return f"gate_sandbox bwrap_path {path!r} is not an absolute path"
	if not os.path.isfile(path) or not os.access(path, os.X_OK):
		return f"gate_sandbox bwrap_path {path} is not an executable file"
	return None


def checkSandbox(settings, home):
	"""Raises SandboxError unless the configured bwrap (bwrapProblem) starts with the same options a real run uses;
	once per process."""
	problem = bwrapProblem(settings)
	if problem:
		raise SandboxError(f"bwrap unavailable: {problem}")
	if _probed:
		return
	probe = sandboxArgv(["true"], "/", {}, [], [], settings, home, {})
	try:
		result = subprocess.run(probe, capture_output=True, stdin=subprocess.DEVNULL,
			timeout=settings["probe_timeout_seconds"])
	except (OSError, subprocess.TimeoutExpired) as error:
		raise SandboxError(f"bwrap unavailable: {error}") from error
	if result.returncode != 0:
		raise SandboxError("bwrap unavailable: " + result.stderr.decode("utf-8", "replace").strip()[-500:])
	_probed.append(True)


def runCommand(argv, cwd, env=None, timeout=None, writable=(), readable=(), protect=()):
	"""(exit code or None on timeout, seconds, output tail). A leading ~ in the program is expanded; the whole
	process group is killed on timeout. Within a gate (startDeadline) the timeout is at most what is left of the
	deadline, and running out of it raises DeadlineReached instead of returning None. Runs in bwrap (sandboxArgv) with writable and readable put back;
	raises SandboxError rather than run unsandboxed, and NotStarted when the program can't start (checked
	before the run, and from the result: startFailed)."""
	start = time.time()
	left = remaining()
	capped = False
	if left is not None:
		if left <= 0:
			raise deadlineError(argv[0])
		if timeout is None or left < timeout:
			timeout, capped = left, True
	argv = [os.path.expanduser(argv[0]), *argv[1:]]
	cwd = os.path.abspath(cwd)
	writable = [os.path.abspath(p) for p in writable]
	readable = [os.path.abspath(p) for p in readable if p]
	settings = homeconfig.load("gate_sandbox")
	problem = startProblem(argv[0], cwd, env)
	if problem:
		raise NotStarted(problem)
	home = os.path.expanduser("~")
	checkSandbox(settings, home)
	command = sandboxArgv(argv, cwd, env, list(writable) or [cwd], readable, settings, home, os.environ, protect)
	procEnv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
	try:
		proc = subprocess.Popen(command, cwd=cwd, env=procEnv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT, start_new_session=True)
	except OSError as error:
		raise NotStarted(f"{argv[0]!r} can't start: {error}") from error
	try:
		out, _ = proc.communicate(timeout=timeout)
		code = proc.returncode
	except subprocess.TimeoutExpired:
		os.killpg(proc.pid, signal.SIGKILL)
		out, _ = proc.communicate()
		if capped:  # the gate's deadline, not the command's own timeout: never read as a timed-out mutant
			raise deadlineError(argv[0])
		code = None
	tail = out.decode("utf-8", "replace")[-4000:]
	if startFailed(code, tail, settings):
		raise NotStarted(f"{argv[0]!r} didn't start (exit {code}): " + " | ".join(tail.strip().splitlines()[-2:])[:300])
	return code, time.time() - start, tail


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


def measureCoverage(root, lang, langConfig, workDir, timeout, repo=None, protect=(), files=None):
	"""Runs the language's coverage command in root (the repo or a scratch copy of it); repo is the real
	checkout for `{repo}` in the config (shared virtualenvs); files: the language's changed paths, whose
	directories fill {changed_dirs} (module doc). (cover map or None, exit code, seconds, tail).
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
		argv = expandCoverage(langConfig["coverage"], mapping, changedDirs(root, cwd, files))
		code, seconds, tail = runCommand(argv, cwd, env, timeout, writable=[root, out], readable=readable,
			protect=protect)
		if not os.path.isfile(report):
			return None, code, seconds, tail
		with open(report, encoding="utf-8") as f:
			text = f.read()
	finally:
		shutil.rmtree(out, ignore_errors=True)
	if langConfig.get("coverage_format") == "gocover":
		cover = parseGoCover(text, goModule(cwd), langConfig.get("cwd", ""))
	else:
		cover = parseLcov(text, root, cwd)
	return cover, code, seconds, tail


def lastLines(tail, count):
	return " | ".join(tail.strip().splitlines()[-count:])[:300]


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

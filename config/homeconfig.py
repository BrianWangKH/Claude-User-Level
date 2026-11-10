#!/usr/bin/env python3
"""The one loader for the home settings in ~/.claude/config/, the directory it lives in.

Two kinds of files, each read in one place:
- values: config/home.json holds every default, one section per area (SECTIONS), tracked; a section's keys
  are documented by its own `_note_<key>` entries. config/local.json, untracked (the home .gitignore and
  config/.gitignore), may override any value: the same sections, dicts merge recursively, anything else
  replaces: {"budget": {"budget_headroom_factor": 1.5}, "toolchains": {"tools": {"uv": "~/.local/bin/uv"}}}.
  Per value: local.json's when it is there and valid, else home.json's. Invalid (problems(), each
  {"key", "why"}): a key home.json doesn't have at that depth (keys starting with "_" are comments), an old
  section or key name (RENAMED_SECTIONS, RENAMED_KEYS: the why names the new one; there is no alias), a
  registry section, a section that isn't an object, a value of another type than home.json's (a number for
  a number, a list for a list, ...), a negative number where home.json's isn't, a fraction where home.json
  has a whole number, a value above its registry ceiling (CEILINGS: a gate deadline above its settings.json
  hook timeout). Broken JSON (or not an object) makes the whole file one problem: home.json for
  everything. An invalid value never raises: the reader gets home.json's value, and the wt-pane mod warns
  once per distinct problem (wt_controls.announceInvalid, a setting-invalid event); `homeconfig.py check`
  lists them and exits 1.
- config mode: config/mode holds DEV or TEST (configMode(); no file = DEV). DEV reads config/local.json as
  above. TEST (for an external test harness, not included) reads config/local.test.json in its place, strictly:
  a missing file, broken JSON, anything but an object, or any problem a local.json would have is a
  ConfigError, never a fallback to local.json or to home.json's value. Any other content of config/mode is a
  ConfigError too. Both files are untracked (the home .gitignore); no environment variable switches either.
- registries: config/registry/<name>.json (REGISTRIES: environment variables, path-check rules, state kinds,
  events, the home conventions), each with its own schema and readers; never overridden by local.json (a
  section of that name is a problem, ignored).
home.json itself (tracked, the defaults) is strict: broken JSON, a missing or unknown section, a value above its
ceiling is a ConfigError.
- repo settings (.claude/quality-gates.json "limits" and "languages", .claude/flow.json "queue"): merged by
  their own readers on top of what load() returns (languages over toolchains: qg_common.languageConfigs;
  queue: wt_queue.limits, the smaller of the repo's and the person's); this module doesn't read them, but
  renamedKey() tells their readers an old key name.

source(section, dotted) says where a value comes from: "local", "home", or "home (local invalid)" when
local.json sets it (or a key above or below it) invalidly. loosened() lists the
safety-related values (SAFETY) a local.json override makes less strict: it never refuses one, the wt-pane
mod announces each distinct set once (wt_controls.announceLoosened, a setting-loosened event).

Environment variables: config/registry/env.json registers every variable home code reads or sets (who
provides it, its default, optionally its allowed values). env(name) is the one way to read one: the
variable's value, else the registry default; empty counts as unset; an unregistered name or a value outside
"allowed" raises ConfigError, so a typo never silently means the default.

Flow mode and integration branch (the only readers; every hook, skill script and the wt-pane mod asks
these, in Python or through the CLI below):
- flowMode(root, recorded=None): "origin" (PR flow: branches start from and merge into origin's default
  branch) or "local" (the local main is the integration branch, auto-merge.py merges into it). root is any
  path inside a checkout, main or worktree. Precedence, first that applies:
    1. the topic record: <worktree>/.hook-only/flow-mode when root is in a linked worktree (worker-control.py
       writes it at the topic's first launch, so `wt local on|off` never changes a launched topic)
    2. the main checkout record: <main>/.hook-only/flow-mode (the parent of git's common dir; the wt-pane
       mod's `wt local on|off` writes it)
    3. the default, FLOW_DEFAULT (origin); also outside git
  Only hooks can write the records (.hook-only/), and no environment variable is a source: a repo's settings
  env can't choose or change the mode, not even while pytest runs (tests write the records, or replace
  FLOW_DEFAULT in-process). A record holding anything but "origin" or "local" (empty included), or a root that
  is no directory, is a ConfigError, never a default. recorded: the mode a run record kept (integration_mode);
  when given and different from the answer, a ConfigError.
- integrationBranch(root, mode=None): the branch features start from, merge into and are compared with, in
  the mode given or flowMode(root). local: main, else master (local branches); None without either.
  origin: origin/master, else origin/main; a ConfigError when origin/HEAD is set and names another (or a
  missing) branch, or when both exist and origin/HEAD is unset (the PR-mode scripts and gates used to pick
  differently there; PR mode gets no new behaviour, so the ambiguity is an error); None without either.
  Refs are shared by every checkout of a repository, so root only picks the repository and the mode.
- integrationBase(root): (the merge-base of HEAD with integrationBranch(root), or "" with why there is none):
  the base the planning session's run-result.py and worker-status.py diff a topic's worktree against.

The config directory is this file's own (it lives in config/, so a sandbox that may read config/ reaches
loader and settings with hooks/ closed); nothing at run time moves it (tests replace configDir itself, or run
a copy of the tree). Importers put config/ on sys.path relative to their own realpath, never through ~.
Results are cached per process and directory.

CLI (for shell scripts):
  homeconfig.py get <name> <dotted.key>   prints the value of a section or registry: plain for strings, JSON
                                          otherwise (3, 0.5, true, {...}); exit 1 with a message when the key
                                          is missing
  homeconfig.py check                     prints the config mode line, then validates home.json and the mode's
                                          local file; exit 1 listing each local problem (DEV: readers fall
                                          back to home.json for those; TEST: readers refuse)
  homeconfig.py env <NAME>                prints the variable's value or its registry default (empty line
                                          for a null default); exit 1 with a message when the name is
                                          unregistered or the value is not allowed
  homeconfig.py flow-mode [<path>]        prints flowMode(<path>, default the working directory); exit 1
                                          with the error
  homeconfig.py integration-branch [<path>]
                                          prints integrationBranch(<path>); exit 1 with a message when
                                          there is none or on an error
"""
import copy
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
HOME = "home"
LOCAL = "local"
# config/mode (configMode): the person's overrides file per mode; no file = DEV.
MODE_FILE = "mode"
CONFIG_MODES = {"DEV": "local.json", "TEST": "local.test.json"}
DEFAULT_CONFIG_MODE = "DEV"
REGISTRY_DIR = "registry"
SECTIONS = ("budget", "quality", "prices", "toolchains", "gate_sandbox", "auto_merge_hold", "hooks")
REGISTRIES = ("env", "path-refs", "state-registry", "events", "rules")
NAMES = SECTIONS + REGISTRIES
# The files the config merge folded into home.json sections, by their old local.json section.
RENAMED_SECTIONS = {"budget-defaults": "budget", "quality-defaults": "quality", "qg_sandbox": "gate_sandbox",
	"auto-merge-hold": "auto_merge_hold"}
# Keys renamed in the config merge (hooks/RULES.md C3, Q8: the unit in the name), as (old path, new path)
# inside a section; "*" matches any key (a model id).
RENAMED_KEYS = (
	(("budget", "minutes"), ("budget", "fallback_minutes")),
	(("budget", "worker_max_phases"), ("budget", "worker_phases_max")),
	(("budget", "budget_headroom"), ("budget", "budget_headroom_factor")),
	(("budget", "estimate_window"), ("budget", "estimate_window_count")),
	(("budget", "queue_max_workers"), ("budget", "queue_workers_max")),
	(("quality", "marker_max_new"), ("quality", "marker_new_max")),
	(("quality", "developer_blocks"), ("quality", "developer_blocks_max")),
	(("quality", "hook_timeout_seconds"), ("quality", "hook_timeouts")),
	(("quality", "hook_timeouts", "mutate_request"), ("quality", "hook_timeouts", "mutate_request_seconds")),
	(("quality", "hook_timeouts", "review_handback"), ("quality", "hook_timeouts", "review_handback_seconds")),
	(("quality", "hook_timeouts", "developer_gate"), ("quality", "hook_timeouts", "developer_gate_seconds")),
	(("prices", "models", "*", "input"), ("prices", "models", "*", "input_usd_per_mtok")),
	(("prices", "models", "*", "output"), ("prices", "models", "*", "output_usd_per_mtok")),
	(("prices", "models", "*", "cache_write_5m"), ("prices", "models", "*", "cache_write_5m_usd_per_mtok")),
	(("prices", "models", "*", "cache_write_1h"), ("prices", "models", "*", "cache_write_1h_usd_per_mtok")),
	(("prices", "models", "*", "cache_read"), ("prices", "models", "*", "cache_read_usd_per_mtok")),
)
# Safety-related values and the change that loosens them: "shrink" = an entry of home.json is missing,
# "grow" = an entry home.json doesn't have, "change" = any entry differs (a dict), "replace" = a single value differs.
SAFETY = {
	("gate_sandbox", "bwrap_path"): "replace",
	("gate_sandbox", "hide"): "shrink",
	("gate_sandbox", "hide_inside"): "shrink",
	("gate_sandbox", "protect"): "shrink",
	("gate_sandbox", "home_read"): "grow",
	("gate_sandbox", "home_write"): "grow",
	("gate_sandbox", "env_pass"): "grow",
	("gate_sandbox", "env_set"): "change",
	("auto_merge_hold", "paths"): "shrink",
	("hooks", "home_draft_small_paths"): "grow",
	("hooks", "home_draft_small_excluded_paths"): "shrink",
}
# Values with an upper bound kept in a registry: the gate deadlines can't pass the settings.json ceiling of their
# hook registration (registry rules hook_ceilings_seconds, keyed like the deadlines).
CEILINGS = {("quality", "hook_timeouts"): ("rules", "hook_ceilings_seconds")}
ENV_SOURCES = ("system", "claude-code", "repo", "internal", "test")
FLOW_MODES = ("origin", "local")
FLOW_DEFAULT = "origin"  # a main checkout without a record works in the PR flow until `wt local on`
# hooks/lib/hookonly.py NAME: config/ can't import hooks/ (closed to a sandboxed caller such as base-ref.sh)
FLOW_RECORD = (".hook-only", "flow-mode")

_cache = {}


class ConfigError(Exception):
	pass


def configDir():
	return HERE


def configMode():
	"""DEV or TEST, from config/mode (module doc); DEV without the file, a ConfigError for anything else."""
	file = os.path.join(configDir(), MODE_FILE)
	try:
		with open(file, encoding="utf-8") as f:
			value = f.read().strip()
	except FileNotFoundError:
		return DEFAULT_CONFIG_MODE
	except OSError as error:
		raise ConfigError(f"{file}: can't read the config mode ({error})") from error
	if value not in CONFIG_MODES:
		raise ConfigError(f"{file}: config mode {value!r} is not one of {', '.join(CONFIG_MODES)}")
	return value


def path(name):
	"""The file that holds name: home.json for a section (and HOME), registry/<name>.json for a registry,
	the config mode's overrides file for LOCAL (local.json, or local.test.json in TEST)."""
	if name in REGISTRIES:
		return os.path.join(configDir(), REGISTRY_DIR, f"{name}.json")
	if name in SECTIONS or name == HOME:
		return os.path.join(configDir(), f"{HOME}.json")
	if name == LOCAL:
		return os.path.join(configDir(), CONFIG_MODES[configMode()])
	raise ConfigError(f"unknown home settings name {name!r}; known: {', '.join(NAMES)}")


def where(name):
	"""How a message names a section or registry: the file, plus the section in home.json."""
	return f"{path(name)} section {name!r}" if name in SECTIONS else path(name)


def _readJson(file):
	try:
		with open(file, encoding="utf-8") as f:
			return json.load(f)
	except ValueError as error:
		raise ConfigError(f"{file}: not valid JSON ({error})") from error


def _home():
	key = ("home", configDir())
	if key not in _cache:
		file = path(HOME)
		data = _readJson(file)
		if not isinstance(data, dict):
			raise ConfigError(f"{file}: must be a JSON object of sections")
		missing = [s for s in SECTIONS if not isinstance(data.get(s), dict)]
		if missing:
			raise ConfigError(f"{file}: section(s) {', '.join(missing)} missing or not an object")
		unknown = [s for s in data if not s.startswith("_") and s not in SECTIONS]
		if unknown:
			raise ConfigError(f"{file}: unknown section(s) {', '.join(unknown)} (known: {', '.join(SECTIONS)})")
		for (section, key), _registry in CEILINGS.items():
			for leaf, value in (data[section].get(key) or {}).items():
				why = None if leaf.startswith("_") else _aboveCeiling((section, key, leaf), value)
				if why:
					raise ConfigError(f"{file}: {section}.{key}.{leaf} {why}")
		_cache[key] = data
	return _cache[key]


def _registry(name):
	key = ("registry", configDir(), name)
	if key not in _cache:
		_cache[key] = _readJson(path(name))
	return _cache[key]


def _matches(pattern, parts):
	return len(pattern) == len(parts) and all(p in ("*", q) for p, q in zip(pattern, parts))


def _spell(parts, like):
	"""The new path, its "*" segments filled in from the old path the person wrote."""
	return ".".join(q if p == "*" else p for p, q in zip(like, parts)) if len(like) == len(parts) else ".".join(like)


def renamedKey(parts):
	"""The new dotted name of an old (renamed) key path, a tuple starting with the section, or None.
	Also matches an old leaf under its new parent (hook_timeouts.mutate_request)."""
	parts = tuple(parts)
	for old, new in RENAMED_KEYS:
		if _matches(old, parts):
			return _spell(parts, new)
		if len(old) == len(parts) and old[-1] == parts[-1] and _matches(new[:-1], parts[:-1]):
			return _spell(parts[:-1] + (new[-1],), new)
	return None


def _local():
	"""(the valid part of local.json, its problems); ({}, []) without the file (module doc). In config mode TEST
	the file is local.test.json and must exist and be valid: a ConfigError otherwise (_strictTest)."""
	mode = configMode()
	key = ("local", configDir(), mode)
	if key not in _cache:
		file = path(LOCAL)
		problems = []
		if mode == "TEST" and not os.path.exists(file):
			raise ConfigError(f"{file}: missing; config mode TEST ({os.path.join(configDir(), MODE_FILE)}) reads it "
				"in place of local.json and never falls back (create it, or write DEV to the mode file)")
		try:
			local = _readJson(file) if os.path.exists(file) else {}
		except ConfigError as error:
			local, problems = {}, [{"key": "local.json", "why": str(error).replace(f"{file}: ", "")}]
		if not isinstance(local, dict):
			local, problems = {}, [{"key": "local.json", "why": "not a JSON object of sections"}]
		home = _home()
		valid = {}
		for section, override in local.items():
			if section.startswith("_"):
				continue
			if section in REGISTRIES:
				problems.append({"key": section, "why": f"a registry ({REGISTRY_DIR}/{section}.json), not a value"})
			elif section in RENAMED_SECTIONS:
				problems.append({"key": section, "why": f"renamed to {RENAMED_SECTIONS[section]}"})
			elif section not in SECTIONS:
				problems.append({"key": section, "why": "no such section in home.json"})
			elif not isinstance(override, dict):
				problems.append({"key": section, "why": "a section must be an object"})
			else:
				valid[section] = _validated(override, home[section], (section,), problems)
		if mode == "TEST":
			_strictTest(file, problems)
		_cache[key] = (valid, problems)
	return _cache[key]


def _strictTest(file, found):
	"""Config mode TEST never runs on a fallback: any problem of local.test.json is a ConfigError."""
	if found:
		raise ConfigError(f"{file}: " + "; ".join(f"{p['key']} invalid ({p['why']})" for p in found)
			+ " — config mode TEST refuses instead of using the home.json value")


def _category(value):
	if isinstance(value, bool):
		return "boolean"
	if isinstance(value, (int, float)):
		return "number"
	return {str: "string", list: "list", dict: "object"}.get(type(value), "null")


def _invalid(value, base):
	"""Why value can't replace home.json's base value, or None (module doc)."""
	if base is None:
		return None
	want, got = _category(base), _category(value)
	if want != got:
		article = lambda word: ("an " if word[0] in "aeiou" else "a ") + word
		return f"{article(got)} where home.json has {article(want)}"
	if want == "number":
		if not math.isfinite(value):
			return "not a finite number"
		if base >= 0 and value < 0:
			return "below 0"
		if isinstance(base, int) and not float(value).is_integer():
			return "not a whole number"
	return None


def _aboveCeiling(parts, value):
	"""Why value passes its registry ceiling (CEILINGS), or None."""
	bound = CEILINGS.get(parts[:-1])
	if bound is None or isinstance(value, bool) or not isinstance(value, (int, float)):
		return None
	ceilings = _registry(bound[0]).get(bound[1]) or {}
	if parts[-1] not in ceilings:
		return f"has no ceiling in {path(bound[0])} {bound[1]}"
	if value > ceilings[parts[-1]]:
		return f"above its settings.json ceiling {ceilings[parts[-1]]} s ({REGISTRY_DIR}/{bound[0]}.json {bound[1]})"
	return None


def _validated(override, base, at, problems):
	"""The keys of override that may replace base's, recursively; every other one appended to problems."""
	out = {}
	for k, v in override.items():
		if k.startswith("_"):
			continue
		parts = at + (k,)
		if not isinstance(base, dict) or k not in base:
			new = renamedKey(parts)
			problems.append({"key": ".".join(parts), "why": f"renamed to {new}" if new else "no such key in home.json"})
		elif isinstance(v, dict) and isinstance(base[k], dict):
			inner = _validated(v, base[k], parts, problems)
			if inner:  # nothing valid inside: local sets nothing there
				out[k] = inner
		else:
			why = _invalid(v, base[k]) or _aboveCeiling(parts, v)
			if why:
				problems.append({"key": ".".join(parts), "why": why})
			else:
				out[k] = v
	return out


def problems():
	"""The invalid parts of local.json, each {"key", "why"}: readers use home.json's values for them."""
	return copy.deepcopy(_local()[1])


def _merge(base, override):
	out = copy.deepcopy(base)
	for k, v in override.items():
		if k.startswith("_"):
			continue
		out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
	return out


def load(name, defaultsOnly=False):
	"""A section of home.json with local.json's valid section of that name merged in (defaultsOnly: home.json's
	alone), or a registry as it is; a fresh copy each call."""
	if name in REGISTRIES:
		return copy.deepcopy(_registry(name))
	if name not in SECTIONS:
		if name in RENAMED_SECTIONS:
			raise ConfigError(f"home settings {name!r} is now the section {RENAMED_SECTIONS[name]!r} of {path(HOME)}")
		raise ConfigError(f"unknown home settings name {name!r}; known: {', '.join(NAMES)}")
	return _merge(_home()[name], {} if defaultsOnly else _local()[0].get(name) or {})


def get(name, dotted, defaultsOnly=False):
	"""One value by dotted key; KeyError naming the key and file when it is missing."""
	value = load(name, defaultsOnly)
	for part in dotted.split("."):
		if not isinstance(value, dict) or part not in value:
			raise KeyError(f"no key {dotted!r} in {where(name)}")
		value = value[part]
	return value


def source(name, dotted):
	"""Where a section value comes from: "local" when a valid local.json value sets it (or a dict above it),
	"home (local invalid)" when local.json sets it, or a key above or below it, invalidly, else "home"."""
	if name not in SECTIONS:
		raise ConfigError(f"source() is for home.json sections; {name!r} is not one")
	get(name, dotted)  # the key exists
	node = _local()[0].get(name) or {}
	for part in dotted.split("."):
		if not isinstance(node, dict) or part not in node:
			break
		node = node[part]
	else:
		return "local"
	full = f"{name}.{dotted}"
	related = [p for p in _local()[1] if p["key"] == "local.json" or p["key"] == full
		or full.startswith(p["key"] + ".") or p["key"].startswith(full + ".")]
	return "home (local invalid)" if related else "home"


def _loosening(rule, home, effective):
	"""What the effective value gives up against home.json's, as text, or None."""
	if rule == "replace":
		return f"replaces {home!r} with {effective!r}" if home != effective else None
	if rule == "change":
		home, effective = home or {}, effective or {}
		changed = sorted(k for k in set(home) | set(effective) if home.get(k) != effective.get(k))
		return f"changes {', '.join(changed)}" if changed else None
	homeItems = {json.dumps(x): x for x in home or []}
	effectiveItems = {json.dumps(x): x for x in effective or []}
	if rule == "shrink":
		given, verb = [homeItems[k] for k in homeItems.keys() - effectiveItems.keys()], "removes"
	else:
		given, verb = [effectiveItems[k] for k in effectiveItems.keys() - homeItems.keys()], "adds"
	return f"{verb} {', '.join(sorted(map(str, given)))}" if given else None


def loosened():
	"""The safety-related values local.json makes less strict than home.json (SAFETY), each
	{"key", "home", "effective", "change"}; [] when none. Never raises for a loosening: it is allowed."""
	out = []
	for (section, key), rule in SAFETY.items():
		home = _home()[section].get(key)
		effective = load(section).get(key)
		change = _loosening(rule, home, effective)
		if change:
			out.append({"key": f"{section}.{key}", "home": home, "effective": effective, "change": change})
	return out


def _envEntry(name):
	"""config/registry/env.json's entry for <name>, checked; ConfigError when it isn't registered or is malformed."""
	registry = load("env")
	entry = registry.get(name) if not name.startswith("_") else None
	if not isinstance(entry, dict):
		raise ConfigError(f"environment variable {name!r} is not registered in {path('env')}")
	if entry.get("source") not in ENV_SOURCES:
		raise ConfigError(f"{path('env')}: {name}.source must be one of {', '.join(ENV_SOURCES)}")
	allowed = entry.get("allowed")
	if allowed is not None and (not isinstance(allowed, list) or entry.get("default") not in allowed):
		raise ConfigError(f"{path('env')}: {name}.allowed must be a list containing its default")
	if entry.get("source") == "repo" and entry.get("default") is None:
		raise ConfigError(f"{path('env')}: {name} is repo-provided and needs a default")
	return entry


def env(name):
	"""The variable's value, else its registry default (module doc); empty counts as unset."""
	entry = _envEntry(name)
	value = os.environ.get(name) or entry.get("default")
	allowed = entry.get("allowed")
	if allowed is not None and value not in allowed:
		raise ConfigError(f"environment variable {name}={value!r} is not allowed "
			f"(allowed: {', '.join(map(str, allowed))}; {path('env')})")
	return value


def _git(root, *args):
	"""git's stdout, stripped, or None when it fails (not a repository, a missing ref)."""
	try:
		result = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)  # local reads only
	except OSError:
		return None
	return result.stdout.strip() if result.returncode == 0 else None


def _checkouts(root):
	"""(the linked worktree holding root or None, the main checkout or None); (None, None) outside git."""
	out = _git(root, "rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
	lines = out.splitlines() if out else []
	if len(lines) != 3:
		return None, None
	top, gitDir, common = (os.path.realpath(line) for line in lines)
	return (top if gitDir != common else None), os.path.dirname(common)


def _record(checkout):
	"""The mode a checkout's flow-mode record holds, or None without the file; ConfigError for anything else."""
	if checkout is None:
		return None
	file = os.path.join(checkout, *FLOW_RECORD)
	try:
		with open(file, encoding="utf-8") as f:
			value = f.read().strip()
	except FileNotFoundError:
		return None
	except OSError as error:
		raise ConfigError(f"{file}: can't read the flow mode ({error})") from error
	if value not in FLOW_MODES:
		raise ConfigError(f"{file}: flow mode {value!r} is not one of {', '.join(FLOW_MODES)}")
	return value


def _mainMode(main):
	"""The main checkout's mode: its record, else FLOW_DEFAULT."""
	return _record(main) or FLOW_DEFAULT


def flowMode(root, recorded=None):
	"""The flow mode of the checkout holding root (module doc: precedence and errors)."""
	if not os.path.isdir(root):
		raise ConfigError(f"flow mode of {root}: no such directory")
	topic, main = _checkouts(root)
	mode = _record(topic) or _mainMode(main)
	if recorded is not None and recorded != mode:
		raise ConfigError(f"the run record says {recorded!r} but the checkout {root} is in {mode} mode")
	return mode


def _refExists(root, ref):
	return _git(root, "rev-parse", "-q", "--verify", ref) is not None


def integrationBranch(root, mode=None):
	"""The integration branch in mode (default flowMode(root)), or None when it doesn't exist (module doc)."""
	mode = mode or flowMode(root)
	if mode not in FLOW_MODES:
		raise ConfigError(f"flow mode {mode!r} is not one of {', '.join(FLOW_MODES)}")
	if mode == "local":
		return next((b for b in ("main", "master") if _refExists(root, f"refs/heads/{b}")), None)
	found = [b for b in ("origin/master", "origin/main") if _refExists(root, f"refs/remotes/{b}")]
	head = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
	branch = found[0] if found else None
	if head and head != branch:
		raise ConfigError(f"origin/HEAD names {head} but the integration branch is {branch or 'missing'} "
			"(origin/master, else origin/main): point origin/HEAD at it (`git remote set-head origin <branch>`) "
			"or delete the stale remote branch")
	if len(found) == 2 and not head:
		raise ConfigError("both origin/master and origin/main exist and origin/HEAD is unset: set it "
			"(`git remote set-head origin --auto`) or delete the stale remote branch")
	return branch


def integrationBase(root):
	"""(merge-base of HEAD with the topic's integration branch or "", why there is none):
	integrationBranch of root, in the topic's flow mode."""
	try:
		branch = integrationBranch(root)
	except ConfigError as error:
		return "", str(error)
	if branch is None:
		return "", "no integration branch"
	base = _git(root, "merge-base", "HEAD", branch) or ""
	return base, "" if base else f"no merge-base with {branch}"


def check():
	"""Raises ConfigError when home.json is broken, or listing local.json's problems (readers still run on
	home.json's values for those)."""
	found = _local()[1]
	if found:
		raise ConfigError(f"{path(LOCAL)}: " + "; ".join(f"{p['key']} invalid ({p['why']})" for p in found)
			+ " — readers use the home.json value for each")


def main(argv):
	try:
		if len(argv) == 3 and argv[0] == "get":
			value = get(argv[1], argv[2])
			print(value if isinstance(value, str) else json.dumps(value))
		elif len(argv) == 2 and argv[0] == "env":
			value = env(argv[1])
			print("" if value is None else value)
		elif len(argv) in (1, 2) and argv[0] == "flow-mode":
			print(flowMode(argv[1] if len(argv) == 2 else os.getcwd()))
		elif len(argv) in (1, 2) and argv[0] == "integration-branch":
			root = argv[1] if len(argv) == 2 else os.getcwd()
			mode = flowMode(root)
			branch = integrationBranch(root, mode)
			if branch is None:
				missing = "no local main or master" if mode == "local" else "no origin/master or origin/main"
				print(f"homeconfig: no integration branch in {root} ({mode} mode: {missing})", file=sys.stderr)
				return 1
			print(branch)
		elif argv == ["check"]:
			mode = configMode()
			print(f"config mode: {mode} (overrides from {path(LOCAL)})")
			check()
			print(f"ok: {path(LOCAL)}" if os.path.exists(path(LOCAL)) else f"ok: no {path(LOCAL)}")
		else:
			print("usage: homeconfig.py get <name> <dotted.key> | env <NAME> | flow-mode [<path>] | "
				"integration-branch [<path>] | check", file=sys.stderr)
			return 2
	except KeyError as error:
		print(f"homeconfig: {error.args[0]}", file=sys.stderr)
		return 1
	except (ConfigError, OSError) as error:
		print(f"homeconfig: {error}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))

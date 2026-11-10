"""Hook input, output and failure policy for new hooks (hooks/RULES.md H4, H6, H8-H10, X3, X4).

A hook built on this module (hooks/tools/new-artifact.py writes the skeleton) is:
	def main(hook):            # hook: Hook, the parsed input
		...                    # return None (no say) or one output below
	sys.exit(hookio.run(NAME, main, policy))
Input (read once from stdin): hook.data (the dict), hook.event, hook.root() (the checkout: only
CLAUDE_PROJECT_DIR through homeconfig.env, its git top level; missing = RootError, never the input's cwd),
hook.mainRoot() (the main checkout: the parent of git's common dir), hook.role() ("main",
"subagent:<type>" or "worker"; one signal each: agent_type in the input, CLAUDE_WORKER_MINUTES).
Output, the official JSON on stdout with exit 0 (hooks docs, "JSON output"), one channel per hook:
- refuse(event, reason): PreToolUse -> hookSpecificOutput.permissionDecision "deny"; UserPromptSubmit,
  PostToolUse, Stop, SubagentStop -> top-level decision "block" (hooks docs, "Decision control").
- context(event, text): hookSpecificOutput.additionalContext (Claude sees it).
- warnUser(text): top-level systemMessage (the person sees it; Claude doesn't).
Messages start with "<hook name>: ".
A check deep in its own calls can end with an output by raising Answer(output); hook-entry.py takes it as the
check's return value.
Failure policy (run's policy argument):
- "guard": any exception (bad input, a crash, a ConfigError) refuses the action with the error, on the
  events of registry/rules.json guard_events; on any other event it exits 2 with the error on stderr. On Stop and
  SubagentStop (block_once_events) a refusal keeps the agent working, so a crash blocks at most once per
  distinct error per agent (SubagentStop) or session (Stop), recorded in the stop-block state; the next
  identical crash lets the stop through with a systemMessage and a hook-failed event. Settings add
  onFailure "block" for what this can't catch (no start, import error, timeout).
- "record" (cost, progress, session logs): the action goes ahead; the person is told once per distinct
  error through systemMessage, and a hook-failed event is recorded. The hook-error state
  (config/registry/state-registry.json) remembers the last error's fingerprint, so the same error stays quiet.
  recordFailureAt(root, name, error) is the same bookkeeping for a program that is not a hook (a command
  line tool that knows its checkout, runstate.dualWrite): True when the error is new and was recorded.
"""

import hashlib
import json
import os
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import homeconfig  # noqa: E402

SETTINGS = "hooks"  # numbers (home.json section hooks)
RULES = "rules"  # event lists (config/registry/rules.json)
POLICIES = ("guard", "record")
ERROR_KIND = "hook-error"
ERROR_SCHEMA = 1
FAILED_EVENT = "hook-failed"
STOP_KIND = "stop-block"
STOP_SCHEMA = 1


class RootError(Exception):
	pass


class InputError(Exception):
	pass


class Answer(Exception):
	"""A check's output (refuse / context / warnUser), raised where returning it would take many returns."""

	def __init__(self, output):
		super().__init__("answer")
		self.output = output


def setting(key):
	return homeconfig.get(SETTINGS, key)


def rule(key):
	return homeconfig.get(RULES, key)


def git(directory, *args):
	"""git's stdout (stripped) in directory, or None when it fails."""
	try:
		out = subprocess.run(
			["git", "-C", directory, *args],
			capture_output=True,
			text=True,
			check=True,
			timeout=setting("git_timeout_seconds"),
		).stdout.strip()
	except (subprocess.SubprocessError, OSError):
		return None
	return out or None


class Hook:
	def __init__(self, data):
		if not isinstance(data, dict):
			raise InputError("hook input is not a JSON object")
		self.data = data
		self.event = data.get("hook_event_name")

	def root(self):
		"""The session's checkout: CLAUDE_PROJECT_DIR's git top level (the directory itself outside git)."""
		project = homeconfig.env("CLAUDE_PROJECT_DIR")
		if not project:
			raise RootError("CLAUDE_PROJECT_DIR is not set; the checkout is never guessed from cwd")
		project = os.path.realpath(project)
		top = git(project, "rev-parse", "--show-toplevel")
		return os.path.realpath(top) if top else project

	def mainRoot(self):
		"""The main checkout of the session's repository (a worktree's main one); RootError outside git."""
		root = self.root()
		common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
		if not common:
			raise RootError(f"{root} is not in a git repository")
		return os.path.dirname(os.path.realpath(common))

	def role(self):
		if homeconfig.env("CLAUDE_WORKER_MINUTES"):
			return "worker"
		agentType = self.data.get("agent_type")
		return f"subagent:{agentType}" if self.data.get("agent_id") and agentType else "main"


def read(stream=None):
	"""The hook input from stdin (or stream), read once; InputError for anything but a JSON object."""
	raw = (stream or sys.stdin).read()
	try:
		data = json.loads(raw)
	except ValueError as error:
		raise InputError(f"hook input is not JSON ({error})") from error
	return Hook(data)


def refuse(event, reason):
	if event == "PreToolUse":
		return {
			"hookSpecificOutput": {
				"hookEventName": "PreToolUse",
				"permissionDecision": "deny",
				"permissionDecisionReason": reason,
			}
		}
	if event in ("UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop"):
		return {"decision": "block", "reason": reason}
	raise ValueError(f"hookio: {event!r} has no JSON refusal")


def context(event, text):
	return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def warnUser(text):
	return {"systemMessage": text}


def errorText(error):
	text = f"{type(error).__name__}: {error}".splitlines()[0] if str(error) else type(error).__name__
	return text[: setting("error_chars")]


def fingerprint(name, error):
	"""Same hook, same error type and message -> same fingerprint (the place in the code included)."""
	frames = traceback.extract_tb(error.__traceback__)
	where = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else ""
	return hashlib.sha256(f"{name}|{type(error).__name__}|{error}|{where}".encode()).hexdigest()[:16]


def recordEvent(root, name, policy, error, data, outcome):
	import eventlog

	eventlog.emit(
		root,
		FAILED_EVENT,
		name,
		{"hook": name, "policy": policy, "error": errorText(error), "outcome": outcome},
		session_id=data.get("session_id") if isinstance(data, dict) else None,
		agent_id=data.get("agent_id") if isinstance(data, dict) else None,
	)


def recordFailureAt(root, name, error, data=None):
	"""Record that name failed with error in checkout root: True when the error is new (hook-error state written,
	hook-failed event recorded), False when it is the last one again (only its count grows)."""
	import statefile

	mark = fingerprint(name, error)
	previous = statefile.read(root, ERROR_KIND, name)
	if previous is not None and previous["fingerprint"] == mark:
		previous["count"] += 1
		previous["last_at"] = clock.isoNow()
		statefile.write(root, ERROR_KIND, previous, name)
		return False
	statefile.write(
		root,
		ERROR_KIND,
		{
			"schema": ERROR_SCHEMA,
			"fingerprint": mark,
			"error": errorText(error),
			"count": 1,
			"first_at": clock.isoNow(),
			"last_at": clock.isoNow(),
		},
		name,
	)
	recordEvent(root, name, "record", error, data or {}, "skipped")
	return True


def recordFailure(name, error, data):
	"""A record-keeping hook failed: the warning to show, or None when this error was already shown."""
	message = f"{name}: record-keeping failed and was skipped ({errorText(error)})"
	try:
		root = Hook(data if isinstance(data, dict) else {}).root()
		if not recordFailureAt(root, name, error, data):
			return None
	except Exception as inner:
		return warnUser(f"{message}; recording that failed too ({errorText(inner)})")
	return warnUser(message + "; shown once until the error changes")


def stopBlockedBefore(name, error, data):
	"""Whether this guard already blocked a stop with this error for this agent (SubagentStop) or session (Stop);
	records the block when not. A stop the guard can't record is treated as blocked before: without the record
	the next crash would block again, and a guard that crashes on every stop would keep the agent running."""
	import statefile

	try:
		root = Hook(data).root()
		owner = data.get("agent_id") if data.get("hook_event_name") == "SubagentStop" else data.get("session_id")
		key = f"{name}.{owner}"
		mark = fingerprint(name, error)
		previous = statefile.read(root, STOP_KIND, key)
		if previous is not None and mark in previous["fingerprints"]:
			return True
		marks = (previous["fingerprints"] if previous is not None else []) + [mark]
		statefile.write(root, STOP_KIND, {"schema": STOP_SCHEMA, "fingerprints": marks, "last_at": clock.isoNow()}, key)
	except Exception:
		return True
	return False


def guardFailure(name, error, data):
	"""(output, exit code) for a guard that failed: refused where the event allows it, else exit 2. On the
	block_once_events (Stop, SubagentStop) a refusal keeps the agent working, so each distinct error blocks
	once per agent or session; after that the stop goes through with a warning to the person."""
	event = data.get("hook_event_name") if isinstance(data, dict) else None
	reason = f"{name}: {errorText(error)} — blocked (fail closed)"
	letThrough = event in rule("block_once_events") and stopBlockedBefore(name, error, data)
	try:
		recordEvent(Hook(data).root(), name, "guard", error, data, "let-through" if letThrough else "blocked")
	except Exception:
		pass  # the decision below is what matters; the event is a convenience
	if letThrough:
		return warnUser(
			f"{name}: {errorText(error)} — crashed again on {event}; let through (it already blocked "
			"this stop once for the same error)"
		), 0
	if event in rule("guard_events"):
		return refuse(event, reason), 0
	print(reason, file=sys.stderr)
	return None, 2


def run(name, main, policy, stream=None):
	"""Read the input, call main(hook), print its output; returns the exit code (sys.exit(run(...)))."""
	if policy not in POLICIES:
		raise ValueError(f"hookio: policy must be one of {POLICIES}")
	data = None
	try:
		hook = read(stream)
		data = hook.data
		output, code = main(hook), 0
	except Exception as error:
		if policy == "guard":
			output, code = guardFailure(name, error, data)
		else:
			output, code = recordFailure(name, error, data), 0
	if output:
		print(json.dumps(output, ensure_ascii=False))
	return code

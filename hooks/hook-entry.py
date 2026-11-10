#!/usr/bin/env python3
"""Every event [every session, every agent]: run one entry of hooks/lib/hook_entries.py, its steps in order.

`python3 ~/.claude/hooks/hook-entry.py <entry>` is the settings command of every entry (pre, handback, start,
stop, prompt, post; the hand-kept hooks of settings.json, checked by hook-entries.py --check-settings). The steps of an entry used to be separate hooks, which Claude
Code runs in parallel (hooks docs, Hook handler fields: "When an event fires, its matching hooks run in
parallel"), so a write of one could happen while another denied the same call. Here they run one after the
other, and on PreToolUse the first deny ends the entry.

Combining the steps' outputs:
- PreToolUse: the first deny wins (its reason); else the strongest decision by the documented precedence
  (deny > defer > ask > allow) with its reason and updatedInput (only dispatch_guard sets one);
  additionalContext of every step that ran, joined.
- UserPromptSubmit, PostToolUse, SubagentStop: the first `decision: block` with its reason; contexts joined.
- systemMessage of every step joined (the person's warnings).
A program step's exit 2 is its refusal (its stderr the reason), as Claude Code reads a hook's exit 2.
Unknown agent_type (neither an agents/<name>.md of this home or the project nor rules builtin_agent_types): the
entry runs as for any other agent, and the person is warned once per new name with a hook-failed event.

`hook-entry.py check <step> [<entry>]` runs one step alone on stdin's input, its filters included, and prints
its output (tests, debugging).

Reads: the hook input (stdin), hooks/lib/hook_entries.py, rules builtin_agent_types, quality
hook_timeouts (a program step's wait).
Writes: what its steps write; hook-error state and hook-failed events (hookio).
Output: the combined JSON on stdout, exit 0.
Fails: closed on PreToolUse — an exception of a guard step (or of the entry itself, or input that is not
JSON) denies the call with `<step>: <error> — blocked (fail closed)`; settings add onFailure "block" to the
`pre` entry for what this can't catch. Open on the other events: a step's exception warns the person once
(systemMessage) and records a hook-failed event, and the other steps still run.
"""

import importlib
import io
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import hook_entries  # noqa: E402
import hookio  # noqa: E402

NAME = "hook-entry"
HERE = os.path.dirname(os.path.realpath(__file__))
PRECEDENCE = ("deny", "defer", "ask", "allow")
BLOCK_EVENTS = ("UserPromptSubmit", "PostToolUse", "SubagentStop", "Stop")
AGENT_CHECK = "hook-entry-agent-type"


class ProgramError(Exception):
	pass


class UnknownAgentType(Exception):
	pass


def policyOf(event):
	return "guard" if event == "PreToolUse" else "record"


def programTimeout(s):
	"""The outer limit of a program step: its registration's ceiling (registry rules hook_ceilings_seconds); the
	program enforces its own, shorter deadline (qg_common.startDeadline)."""
	if not s["timeout_key"]:
		return None
	return float(homeconfig.get(*hook_entries.CEILINGS)[s["timeout_key"]])


def parseOutput(name, text):
	text = text.strip()
	if not text:
		return None
	try:
		output = json.loads(text)
	except ValueError as error:
		raise ProgramError(f"{name} printed something that is not JSON: {text[:200]}") from error
	if not isinstance(output, dict):
		raise ProgramError(f"{name} printed JSON that is not an object")
	return output


def refusalOf(event, reason):
	if event == "PreToolUse":
		return hookio.refuse(event, reason)
	if event in BLOCK_EVENTS:
		return {"decision": "block", "reason": reason}
	return None


def runProgram(s, data, event):
	"""A program step: hooks/<target> with the input on stdin; its output (module doc)."""
	argv = [sys.executable, os.path.join(HERE, s["target"]), *s["argv"]]
	try:
		result = subprocess.run(argv, input=json.dumps(data), capture_output=True, text=True, timeout=programTimeout(s))
	except subprocess.TimeoutExpired as error:
		# As a hook that reaches its timeout without onFailure: the action goes ahead (hooks docs, Timeouts).
		raise TimeoutError(f"{s['target']} did not finish within {error.timeout:g} s; stopped") from error
	if result.stderr:
		sys.stderr.write(result.stderr)
	if result.returncode == 2:
		return refusalOf(event, result.stderr.strip() or f"{s['target']}: exit 2")
	if result.returncode != 0:
		lastLine = (result.stderr.strip().splitlines() or [""])[-1]
		raise ProgramError(f"{s['target']} exit {result.returncode}: {lastLine}")
	return parseOutput(s["target"], result.stdout)


def spawnProgram(s, data):
	"""A spawn step: started detached with the input on stdin, not waited for."""
	process = subprocess.Popen(
		[sys.executable, os.path.join(HERE, s["target"]), *s["argv"]],
		stdin=subprocess.PIPE,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		start_new_session=True,
		close_fds=True,
	)
	process.stdin.write(json.dumps(data).encode())
	process.stdin.close()
	return None


def runStep(s, data, event):
	if s["kind"] == "module":
		module = importlib.import_module(s["target"])
		try:
			return getattr(module, s["func"])(data)
		except hookio.Answer as answer:
			return answer.output
	if s["kind"] == "program":
		return runProgram(s, data, event)
	return spawnProgram(s, data)


def failure(s, error, data, event):
	"""The output for a step that raised: a refusal (guard policy on PreToolUse) or a warning once."""
	if s["policy"] == "guard" and event == "PreToolUse" and not isinstance(error, TimeoutError):
		return hookio.refuse(event, f"{s['name']}: {type(error).__name__}: {error} — blocked (fail closed)")
	print(f"{s['name']}: {hookio.errorText(error)}", file=sys.stderr)
	return hookio.recordFailure(s["name"], error, data)


class Combined:
	def __init__(self, event):
		self.event = event
		self.decision = None
		self.reason = None
		self.updatedInput = None
		self.block = None
		self.contexts = []
		self.messages = []

	def add(self, output):
		"""Takes one step's output; True when it ends the entry (a deny on PreToolUse)."""
		if not output:
			return False
		if output.get("systemMessage"):
			self.messages.append(output["systemMessage"])
		specific = output.get("hookSpecificOutput") or {}
		if specific.get("additionalContext"):
			self.contexts.append(specific["additionalContext"])
		if output.get("decision") == "block" and self.block is None:
			self.block = output.get("reason") or ""
		decision = specific.get("permissionDecision")
		if decision in PRECEDENCE and (
			self.decision is None or PRECEDENCE.index(decision) < PRECEDENCE.index(self.decision)
		):
			self.decision, self.reason = decision, specific.get("permissionDecisionReason")
			self.updatedInput = specific.get("updatedInput")
		return decision == "deny"

	def output(self):
		out = {}
		specific = {}
		if self.decision:
			specific["permissionDecision"] = self.decision
			if self.reason is not None:
				specific["permissionDecisionReason"] = self.reason
			if self.updatedInput is not None and self.decision == "allow":
				specific["updatedInput"] = self.updatedInput
		if self.contexts and self.event in ("PreToolUse", "UserPromptSubmit", "PostToolUse", "SubagentStart"):
			specific["additionalContext"] = "\n".join(self.contexts)
		if specific:
			out["hookSpecificOutput"] = {"hookEventName": self.event, **specific}
		if self.block is not None:
			out["decision"], out["reason"] = "block", self.block
		if self.messages:
			out["systemMessage"] = "\n".join(self.messages)
		return out or None


def checkAgentType(data):
	"""Warn once per new unknown agent_type (module doc); None or the warning output."""
	agentType = data.get("agent_type")
	if not agentType or agentType in hook_entries.knownAgents(
		homeconfig.env("CLAUDE_PROJECT_DIR"), homeconfig.get("rules", "builtin_agent_types")
	):
		return None
	error = UnknownAgentType(
		f"agent_type {agentType!r} has no agents/{agentType}.md and is not a built-in "
		"(rules builtin_agent_types); its agent-specific hook steps don't run"
	)
	print(f"{NAME}: {error}", file=sys.stderr)
	try:
		root = hookio.Hook(data).root()
		if not hookio.recordFailureAt(root, AGENT_CHECK, error, data):
			return None
	except Exception as inner:
		return hookio.warnUser(f"{NAME}: {error}; recording that failed too ({hookio.errorText(inner)})")
	return hookio.warnUser(f"{NAME}: {error}; shown once until the name changes")


def runEntry(name, data, only=None):
	entry = hook_entries.ENTRIES[name]
	event = entry["event"]
	combined = Combined(event)
	combined.add(checkAgentType(data))
	for s in entry["steps"]:
		if (only is not None and s["name"] != only) or not hook_entries.applies(s, data):
			continue
		try:
			output = runStep(s, data, event)
		except Exception as error:
			output = failure(s, error, data, event)
		if combined.add(output) and event == "PreToolUse":
			break
	return combined.output()


def checkOne(only, entryName):
	"""`check <step> [<entry>]`: that step alone, with its filters, on stdin's input (module doc)."""
	raw = sys.stdin.read()
	try:
		event = json.loads(raw).get("hook_event_name")
	except (ValueError, AttributeError):
		event = None
	names = (
		[entryName]
		if entryName
		else [n for n, e in hook_entries.ENTRIES.items() if any(s["name"] == only for s in e["steps"])]
	)
	found = [n for n in names if hook_entries.ENTRIES[n]["event"] == event] or names[:1]
	if not found:
		print(f"{NAME}: no step {only!r}", file=sys.stderr)
		return 2
	return hookio.run(NAME, lambda hook: runEntry(found[0], hook.data, only), policyOf(event), io.StringIO(raw))


def main(argv):
	if len(argv) in (3, 4) and argv[1] == "check":
		return checkOne(argv[2], argv[3] if len(argv) == 4 else None)
	if len(argv) != 2 or argv[1] not in hook_entries.ENTRIES:
		print(f"usage: hook-entry.py <{'|'.join(hook_entries.ENTRIES)}> | check <step> [<entry>]", file=sys.stderr)
		return 2
	name = argv[1]
	return hookio.run(NAME, lambda hook: runEntry(name, hook.data), policyOf(hook_entries.ENTRIES[name]["event"]))


if __name__ == "__main__":
	sys.exit(main(sys.argv))

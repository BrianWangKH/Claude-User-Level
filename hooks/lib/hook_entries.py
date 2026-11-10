"""The hook registrations of the home settings.json, as one table: one entry per event (and matcher group), its
checks in a fixed order, and the few programs that stay registrations of their own.

hook-entry.py runs an entry (`hook-entry.py <entry>`). The `hooks` of settings.json are kept by hand (in git);
hook-entries.py --check-settings checks them against REGISTRATIONS (settingsHooks: the same events, matchers,
commands, if, async and onFailure, and a timeout at least the registration's ceiling).

An entry's steps run in the order listed, in one process:
- kind "module": function `func` of the importable module `target` (hooks/<role>/<target>.py, imported by name
  through hooks/lib/homepath.py), called with the hook input; it returns the step's output (hookio.refuse / context / warnUser) or None, or raises hookio.Answer.
- kind "program": hooks/<target> (target: <role>/<file>) with `argv`, run with the hook input on stdin and waited for (a program other
  callers start too: worker-control.py, flow-next.py, crap-gate.py, mutation.py, subagent-cost.py); its JSON
  output is the step's output, exit 2 a refusal with its stderr. `timeout_key` (a key of registry rules
  hook_ceilings_seconds, the same keys as the gate deadlines in home.json quality.hook_timeouts) bounds the wait
  at the ceiling; the program enforces its own deadline below it.
- kind "spawn": the same, started detached with the input on stdin and not waited for (mutation.py start: the
  automatic scan, which outlives the call).
A step runs only for its `tools` (exact tool names; None = every tool), its `agents` (agent_type values; None =
every caller, the main session included) and, with `prompt`, only for a prompt that matches it.
On PreToolUse the first step that denies ends the entry: later steps, and their writes, never happen for a
denied call. A step's `policy`: "guard" (an exception denies the call, as hookrun.py did for each hook) or
"record" (an exception warns the person once and records a hook-failed event; the other steps go on).

`replaces` names the registrations of the snapshot (hooks/tests/old-registrations.json) a step or registration
took over, REMOVED the ones dropped and why; hooks/tests/test_hook_entries.py checks that every old
registration is in one of them.
"""

import os
import re

HOOKS = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HOME_ROOT = os.path.dirname(HOOKS)
ENTRY_PROGRAM = "hook-entry.py"
COMMAND = "python3 ~/.claude/hooks/{program}"  # program: its path under hooks/ (<role>/<file>, or hook-entry.py)
# The snapshot's commands (old-registrations.json, before M1): the same template, every program then a file name
# in hooks/ itself.
OLD_COMMAND = COMMAND
OLD_SNAPSHOT = os.path.join(HOOKS, "tests", "old-registrations.json")
CEILINGS = ("rules", "hook_ceilings_seconds")  # homeconfig: the registry's outer timeouts per gate key
EVENT_ORDER = ("PreToolUse", "SubagentStart", "SubagentStop", "UserPromptSubmit", "PostToolUse", "SessionStart", "Stop")

REPORT_ROLES = ("qa-planner", "spec-reviewer", "qa", "developer", "code-reviewer", "flow-judge")  # report-schema.json
DISPATCH = ("Agent", "Task", "SendMessage")
WRITES = ("Write", "Edit", "NotebookEdit")
PLAN_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit", "Agent", "Skill")
HANDBACK = ("SubagentHandback",)
HOME_AGENTS = ("home-drafter", "home-verifier")  # skill home-draft SAFETY 3: their Bash deletes nothing
REPO_CHECK = "python3 ~/.claude/skills/repo-plan/repo-check.py --hook"
SETTINGS_SOURCE = "settings"


def old(source, event, command, matcher=None, condition=None):
	"""The id of an old registration (oldIds)."""
	return f"{source} {event} {matcher or '*'} {condition or '-'} {command}"


def cmd(program, *args):
	"""A settings command of today's layout: program is its path under hooks/."""
	return " ".join([COMMAND.format(program=program), *args])


def oldCmd(program, *args):
	"""A command as the old snapshot spells it (program: a file name in hooks/ itself)."""
	return " ".join([OLD_COMMAND.format(program=program), *args])


def fm(agent):
	return f"agents/{agent}.md"


def inSettings(event, program, matcher=None, condition=None):
	return old(SETTINGS_SOURCE, event, oldCmd(program), matcher, condition)


def step(
	name,
	kind,
	target,
	replaces,
	func="check",
	policy="guard",
	tools=None,
	agents=None,
	argv=(),
	prompt=None,
	timeoutKey=None,
):
	return {
		"name": name,
		"kind": kind,
		"target": target,
		"func": func,
		"policy": policy,
		"tools": tools,
		"agents": agents,
		"argv": list(argv),
		"prompt": prompt,
		"timeout_key": timeoutKey,
		"replaces": replaces,
	}


ENTRIES = {
	"pre": {
		"event": "PreToolUse",
		"steps": [
			step(
				"agent-guard", "module", "agent_guard", [inSettings("PreToolUse", "agent-guard.py")], func="preToolUse"
			),
			step("worker-budget", "module", "worker_budget", [inSettings("PreToolUse", "worker-budget.py")]),
			step(
				"subagent-budget",
				"module",
				"subagent_budget",
				[inSettings("PreToolUse", "subagent-budget.py")],
				func="preToolUse",
			),
			step(
				"plan-guard",
				"module",
				"plan_guard",
				[inSettings("PreToolUse", "plan-guard.py", "|".join(PLAN_TOOLS))],
				tools=PLAN_TOOLS,
			),
			step(
				"write-scope",
				"module",
				"write_scope",
				[inSettings("PreToolUse", "block-write-outside-project.py", "|".join(WRITES))],
				tools=WRITES,
			),
			step(
				"bash-guard",
				"module",
				"bash_guard",
				[inSettings("PreToolUse", "bash-guard.py", "Bash")],
				tools=("Bash",),
			),
			step(
				"home-delete-guard",
				"module",
				"bash_guard",
				[],
				func="homeDeletes",
				tools=("Bash",),
				agents=HOME_AGENTS,
			),
			step(
				"merge-commit-guard",
				"module",
				"merge_commit_guard",
				[inSettings("PreToolUse", "merge-commit-guard.py", "Bash")],
				tools=("Bash",),
			),
			step(
				"fetch-origin",
				"module",
				"fetch_origin",
				[inSettings("PreToolUse", "fetch-origin.py", "Skill")],
				tools=("Skill",),
			),
			step(
				"mutation-start",
				"spawn",
				"gates/mutation.py",
				[old(fm("code-reviewer"), "PreToolUse", oldCmd("mutation.py", "start"), "*")],
				policy="record",
				agents=("code-reviewer",),
				argv=["start"],
			),
			step(
				"dispatch-guard",
				"module",
				"dispatch_guard",
				[inSettings("PreToolUse", "dispatch-guard.py", "|".join(DISPATCH))],
				tools=DISPATCH,
			),
		],
	},
	"handback": {
		"event": "PreToolUse",
		"matcher": "SubagentHandback",
		"steps": [
			step(
				"report-check",
				"module",
				"report_check",
				[old(fm(a), "PreToolUse", oldCmd("report-check.py"), "SubagentHandback") for a in REPORT_ROLES],
				agents=REPORT_ROLES,
			),
			step(
				"crap-gate",
				"program",
				"gates/crap-gate.py",
				[old(fm("developer"), "PreToolUse", oldCmd("crap-gate.py"), "SubagentHandback")],
				agents=("developer",),
				timeoutKey="developer_gate_seconds",
			),
			step(
				"mutation",
				"program",
				"gates/mutation.py",
				[old(fm("code-reviewer"), "PreToolUse", oldCmd("mutation.py"), "SubagentHandback")],
				agents=("code-reviewer",),
				timeoutKey="review_handback_seconds",
			),
		],
	},
	"start": {
		"event": "SubagentStart",
		"steps": [
			step(
				"subagent-budget",
				"module",
				"subagent_budget",
				[inSettings("SubagentStart", "subagent-budget.py")],
				func="onSubagentStart",
				policy="record",
			),
			step(
				"agent-guard",
				"module",
				"agent_guard",
				[inSettings("SubagentStart", "agent-guard.py")],
				func="onSubagentStart",
				policy="record",
			),
			step(
				"subagent-cost",
				"program",
				"budget/subagent-cost.py",
				[inSettings("SubagentStart", "subagent-cost.py")],
				policy="record",
			),
		],
	},
	"stop": {
		"event": "SubagentStop",
		"steps": [
			step(
				"crap-gate",
				"program",
				"gates/crap-gate.py",
				[old(fm("developer"), "Stop", oldCmd("crap-gate.py"))],
				policy="record",
				agents=("developer",),
				timeoutKey="developer_gate_seconds",
			),
			step(
				"mutation",
				"program",
				"gates/mutation.py",
				[old(fm("code-reviewer"), "Stop", oldCmd("mutation.py"))],
				policy="record",
				agents=("code-reviewer",),
				timeoutKey="review_handback_seconds",
			),
			step(
				"agent-guard",
				"module",
				"agent_guard",
				[inSettings("SubagentStop", "agent-guard.py")],
				func="onSubagentStop",
				policy="record",
			),
			step(
				"subagent-cost",
				"program",
				"budget/subagent-cost.py",
				[inSettings("SubagentStop", "subagent-cost.py")],
				policy="record",
			),
			step(
				"subagent-budget",
				"module",
				"subagent_budget",
				[inSettings("SubagentStop", "subagent-budget.py")],
				func="onSubagentStop",
				policy="record",
			),
		],
	},
	"prompt": {
		"event": "UserPromptSubmit",
		"steps": [
			step(
				"fetch-origin",
				"module",
				"fetch_origin",
				[inSettings("UserPromptSubmit", "fetch-origin.py")],
				policy="record",
			),
			# Every prompt worker-control.py answers starts with `wt ` (its COMMAND, MERGE, QUEUE and STATUS patterns).
			step(
				"worker-control",
				"program",
				"worker/worker-control.py",
				[inSettings("UserPromptSubmit", "worker-control.py")],
				policy="record",
				prompt=r"^\s*wt\s",
			),
			step(
				"agent-guard",
				"module",
				"agent_guard",
				[inSettings("UserPromptSubmit", "agent-guard.py")],
				func="onPrompt",
				policy="record",
			),
			step(
				"checkpoint-status",
				"module",
				"checkpoint_status",
				[inSettings("UserPromptSubmit", "checkpoint-status.py")],
				policy="record",
			),
			step(
				"wt-events",
				"module",
				"wt_events",
				[inSettings("UserPromptSubmit", "wt_events.py")],
				func="hookOutput",
				policy="record",
			),
		],
	},
	"post": {
		"event": "PostToolUse",
		"matcher": "Bash|Agent|SendMessage|SubagentHandback",
		"steps": [
			step(
				"finish-worktree",
				"module",
				"finish_worktree",
				[inSettings("PostToolUse", "finish-worktree.py", "Bash")],
				policy="record",
				tools=("Bash",),
			),
			step(
				"merge-origin",
				"module",
				"merge_origin",
				[inSettings("PostToolUse", "merge-origin.py", "Bash")],
				policy="record",
				tools=("Bash",),
			),
			step(
				"flow-next",
				"program",
				"worker/flow-next.py",
				[inSettings("PostToolUse", "flow-next.py", "Agent|SendMessage")],
				policy="record",
				tools=("Agent", "SendMessage"),
			),
			step(
				"flow-next-report",
				"program",
				"worker/flow-next.py",
				[old(fm(a), "PostToolUse", oldCmd("flow-next.py", "report"), "SubagentHandback") for a in REPORT_ROLES],
				policy="record",
				tools=HANDBACK,
				agents=REPORT_ROLES,
				argv=["report"],
			),
			step(
				"agent-guard",
				"module",
				"agent_guard",
				[old(fm("code-reviewer"), "PostToolUse", oldCmd("agent-guard.py"), "SubagentHandback")],
				func="onHandback",
				policy="record",
				tools=HANDBACK,
				agents=("code-reviewer",),
			),
		],
	},
}

# Settings registrations, in the order the generated hooks object lists them; an "entry" one runs hook-entry.py.
REGISTRATIONS = [
	{"event": "PreToolUse", "entry": "pre", "onFailure": "block"},
	{
		"event": "PreToolUse",
		"command": cmd("budget/worker-progress.py"),
		"async": True,
		"replaces": [inSettings("PreToolUse", "worker-progress.py")],
	},
	{
		"event": "PreToolUse",
		"matcher": "Bash",
		"if": "Bash(git *)",
		"command": cmd("gates/crap-gate.py"),
		"timeout_key": "commit_gate_seconds",
		"onFailure": "block",
		"replaces": [inSettings("PreToolUse", "crap-gate.py", "Bash", "Bash(git *)")],
	},
	{
		"event": "PreToolUse",
		"matcher": "Bash",
		"if": "Bash(mutate-request *)",
		"command": cmd("gates/mutation.py"),
		"timeout_key": "mutate_request_seconds",
		"replaces": [old(fm("code-reviewer"), "PreToolUse", oldCmd("mutation.py"), "Bash", "Bash(mutate-request *)")],
	},
	{"event": "PreToolUse", "entry": "handback", "timeout_key": ("developer_gate_seconds", "review_handback_seconds")},
	{"event": "SubagentStart", "entry": "start"},
	{"event": "SubagentStop", "entry": "stop", "timeout_key": ("developer_gate_seconds", "review_handback_seconds")},
	{"event": "UserPromptSubmit", "entry": "prompt"},
	{"event": "PostToolUse", "entry": "post"},
	{
		"event": "PostToolUse",
		"matcher": "Bash",
		"if": "Bash(git commit *)",
		"command": cmd("worker/flow-next.py"),
		"replaces": [inSettings("PostToolUse", "flow-next.py", "Bash", "Bash(git commit *)")],
	},
	{"event": "SessionStart", "command": REPO_CHECK, "replaces": [old(SETTINGS_SOURCE, "SessionStart", REPO_CHECK)]},
	{"event": "Stop", "command": cmd("worker/worker-stop.py"), "replaces": [inSettings("Stop", "worker-stop.py")]},
]

REMOVED = {
	inSettings("SessionStart", "session-mark.py"): "session-mark.py has no reader since run-record migration step 3 "
	"(worker-status.py reads runstate.status); archived in M1",
	inSettings("SessionEnd", "session-mark.py"): "the same program at SessionEnd; archived in M1",
}


def matcherOf(registration):
	entry = ENTRIES.get(registration.get("entry"), {})
	return registration.get("matcher") or entry.get("matcher")


def commandOf(registration):
	if "entry" in registration:
		return cmd(ENTRY_PROGRAM, registration["entry"])
	return registration["command"]


def timeoutOf(registration, timeouts):
	"""The least seconds the registration's `timeout` may have (the largest of its keys' ceilings), or None for
	the event's default (hooks docs, Common fields: 600, lowered to 30 on UserPromptSubmit)."""
	keys = registration.get("timeout_key")
	if keys is None:
		return None
	keys = (keys,) if isinstance(keys, str) else keys
	return max(int(timeouts[k]) for k in keys)


def settingsHooks(timeouts):
	"""The `hooks` object settings.json must hold for REGISTRATIONS, each timeout the registration's ceiling
	(timeouts = registry rules hook_ceilings_seconds; the installed one may be larger)."""
	out = {}
	for registration in REGISTRATIONS:
		hook = {"type": "command"}
		if registration.get("if"):
			hook["if"] = registration["if"]
		hook["command"] = commandOf(registration)
		timeout = timeoutOf(registration, timeouts)
		if timeout is not None:
			hook["timeout"] = timeout
		if registration.get("async"):
			hook["async"] = True
		if registration.get("onFailure"):
			hook["onFailure"] = registration["onFailure"]
		groups = out.setdefault(registration["event"], [])
		matcher = matcherOf(registration)
		group = next((g for g in groups if g.get("matcher") == matcher), None)
		if group is None:
			group = {"matcher": matcher, "hooks": []} if matcher else {"hooks": []}
			groups.append(group)
		group["hooks"].append(hook)
	return {event: out[event] for event in EVENT_ORDER if event in out}


def oldIds(snapshot):
	"""Every registration of the old snapshot (settings and agent frontmatter), as ids (old())."""
	ids = []
	for event, groups in snapshot["settings"].items():
		for group in groups:
			for hook in group["hooks"]:
				ids.append(old(SETTINGS_SOURCE, event, hook["command"], group.get("matcher"), hook.get("if")))
	for agent, events in snapshot["frontmatter"].items():
		for event, groups in events.items():
			for group in groups:
				for hook in group["hooks"]:
					ids.append(old(fm(agent), event, hook["command"], group.get("matcher"), hook.get("if")))
	return ids


def replacedIds():
	ids = [i for entry in ENTRIES.values() for s in entry["steps"] for i in s["replaces"]]
	return ids + [i for registration in REGISTRATIONS for i in registration.get("replaces", [])]


def tableAgents():
	"""Every agent name a step filters on."""
	return sorted({a for entry in ENTRIES.values() for s in entry["steps"] for a in (s["agents"] or ())})


def knownAgents(projectDir, builtin):
	"""Agent types the hooks know: agents/<name>.md of this home and of the project's .claude/agents/, and the
	built-in ones (rules builtin_agent_types)."""
	names = set(builtin)
	directories = [os.path.join(HOME_ROOT, "agents")] + (
		[os.path.join(projectDir, ".claude", "agents")] if projectDir else []
	)
	for directory in directories:
		try:
			names |= {n[:-3] for n in os.listdir(directory) if n.endswith(".md")}
		except OSError:
			pass
	return names


def applies(s, data):
	"""Whether step s runs for this hook input (its tools, agents and prompt filters)."""
	if s["tools"] is not None and data.get("tool_name") not in s["tools"]:
		return False
	if s["agents"] is not None and data.get("agent_type") not in s["agents"]:
		return False
	return s["prompt"] is None or re.match(s["prompt"], data.get("prompt") or "") is not None

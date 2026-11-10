"""hook_entries.py, hook-entry.py and hook-entries.py: one entry per event, its steps in a fixed order (M1).

- Nothing dropped: every registration of the old snapshot (old-registrations.json: settings.json's hooks as
  they were before M1 and the agent frontmatter hooks) is replaced by a step or registration, or listed as
  removed; every agent the table names has an agents/<name>.md; an unknown agent_type is reported.
- Order: on PreToolUse the first deny ends the entry, so a later step's writes (dispatch-guard's
  pending-<key>.json, auto-resume-<run>.json and merge-review-pending, mutation's automatic scan) never happen
  for a denied call; a hand-back report-check refuses never reaches the gates; crap-gate doesn't run for a
  commit the pre entry denies.
- Settings: the hooks of settings.json are kept by hand; --check-settings checks their registrations against
  hook_entries.py and every timeout against its ceiling (registry rules hook_ceilings_seconds).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_hook_entries.py -q -p no:cacheprovider'
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import time

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
ROOT = os.path.dirname(HOOKS)
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
sys.path.insert(0, os.path.join(ROOT, "config"))  # homeconfig of the tree under test
import hook_entries  # noqa: E402
import homeconfig  # noqa: E402
import report_schema  # noqa: E402

ENTRY = os.path.join(RUN_HOOKS, "hook-entry.py")
TOOL = os.path.join(RUN_HOOKS, "tools", "hook-entries.py")
_spec = importlib.util.spec_from_file_location("hook_entries_tool", TOOL)
TOOL_MODULE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TOOL_MODULE)
PRE_ORDER = [
	"agent-guard",
	"worker-budget",
	"subagent-budget",
	"plan-guard",
	"write-scope",
	"bash-guard",
	"home-delete-guard",
	"merge-commit-guard",
	"fetch-origin",
	"mutation-start",
	"dispatch-guard",
]


def loadEntry():
	spec = importlib.util.spec_from_file_location("hook_entry_under_test", ENTRY)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.fixture
def home(tmp_path):
	"""A home whose ~/.claude/hooks and ~/.claude/skills are the files under test (dispatch-guard finds
	cost-history.py under ~)."""
	h = tmp_path / "home"
	(h / ".claude").mkdir(parents=True)
	os.symlink(RUN_HOOKS, h / ".claude" / "hooks")
	os.symlink(os.path.join(ROOT, "skills"), h / ".claude" / "skills")
	return h


def git(repo, *args):
	subprocess.run(
		["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True
	)


@pytest.fixture
def repo(tmp_path):
	r = tmp_path / "repo"
	r.mkdir()
	git(r, "init", "-q")
	git(r, "commit", "-q", "--allow-empty", "-m", "base")
	return r


def run(entry, data, home, repo, **extra):
	environ = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	environ.update({"HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo), **extra})
	result = subprocess.run(
		[sys.executable, ENTRY, *entry],
		input=json.dumps({"cwd": str(repo), "session_id": "s", **data}),
		capture_output=True,
		text=True,
		env=environ,
		timeout=120,
		cwd=str(repo),
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout) if result.stdout.strip() else {}, result.stderr


def decision(out):
	return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def reason(out):
	return (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""


def hold(repo, text="agent-guard: qa (a1): files outside tests/qaConfig changed: x."):
	directory = repo / ".hook-only" / "agent-guard"
	directory.mkdir(parents=True, exist_ok=True)
	(directory / "HOLD").write_text(text + "\n")


def dispatch(agent, prompt):
	return {
		"hook_event_name": "PreToolUse",
		"tool_name": "Agent",
		"tool_input": {"subagent_type": agent, "prompt": prompt},
	}


# -- nothing dropped ---------------------------------------------------------------------------------------


def snapshot():
	with open(hook_entries.OLD_SNAPSHOT, encoding="utf-8") as f:
		return json.load(f)


def hookNames():
	"""What a test names each hook by: a module step's module (agent_guard), a program step's or a registration's
	program file (crap-gate.py, repo-check.py)."""
	names = set()
	for entry in hook_entries.ENTRIES.values():
		for s in entry["steps"]:
			names.add(s["target"] if s["kind"] == "module" else os.path.basename(s["target"]))
	for registration in hook_entries.REGISTRATIONS:
		program = next(w for w in hook_entries.commandOf(registration).split() if w.endswith(".py"))
		names.add(os.path.basename(program))
	return names


def test_every_hook_step_and_registered_program_is_named_by_a_test():
	"""hooks/RULES.md H12, T9: each step of ENTRIES and each program a settings registration runs is named by at
	least one test module (hooks/tests/test_*.py) by its module or program name."""
	tests = os.path.join(HOOKS, "tests")
	texts = []
	for name in sorted(os.listdir(tests)):
		if name.startswith("test_") and name.endswith(".py"):
			with open(os.path.join(tests, name), encoding="utf-8") as f:
				texts.append(f.read())
	untested = sorted(n for n in hookNames() if not any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", t) for t in texts))
	assert untested == [], "write a test that runs these (hooks/RULES.md H12)"


def test_every_old_registration_is_replaced_or_listed_as_removed():
	old = hook_entries.oldIds(snapshot())
	covered = set(hook_entries.replacedIds()) | set(hook_entries.REMOVED)
	assert [i for i in old if i not in covered] == []
	assert sorted(covered - set(old)) == []  # every replaces/removed id names a real old registration
	replaced = hook_entries.replacedIds()
	assert len(replaced) == len(set(replaced)) and not set(replaced) & set(hook_entries.REMOVED)


def test_the_snapshot_holds_the_settings_and_all_agent_hooks():
	ids = hook_entries.oldIds(snapshot())
	assert sum(i.startswith("settings ") for i in ids) == 30
	assert sum(i.startswith("agents/") for i in ids) == 19


def test_every_agent_the_table_names_has_an_agent_file():
	"""The home agents are checked by the next test (their files are the private layer's)."""
	missing = [a for a in hook_entries.tableAgents()
		if a not in hook_entries.HOME_AGENTS and not os.path.isfile(os.path.join(ROOT, "agents", f"{a}.md"))]
	assert missing == []


@pytest.mark.private_layer("agents/home-drafter.md", "agents/home-verifier.md")
def test_the_home_agents_the_table_names_have_agent_files():
	assert set(hook_entries.HOME_AGENTS) <= set(hook_entries.tableAgents())
	missing = [a for a in hook_entries.HOME_AGENTS if not os.path.isfile(os.path.join(ROOT, "agents", f"{a}.md"))]
	assert missing == []


def test_the_report_check_roles_are_the_report_schema_roles():
	roles = report_schema.load()["roles"]
	assert sorted(hook_entries.REPORT_ROLES) == sorted(roles)


def test_no_agent_file_registers_hooks_itself():
	"""The agent hooks live in the settings entries (filtered by agent_type), not in agents/*.md frontmatter."""
	for name in os.listdir(os.path.join(ROOT, "agents")):
		with open(os.path.join(ROOT, "agents", name), encoding="utf-8") as f:
			front = f.read().split("---")[1]
		assert "\nhooks:" not in front, name


def test_every_module_step_names_a_function_and_every_program_exists():
	for entry in hook_entries.ENTRIES.values():
		for s in entry["steps"]:
			if s["kind"] == "module":
				path = homepath.locate(f"{s['target']}.py")
				assert path is not None, s["name"]
				with open(path, encoding="utf-8") as f:
					assert f"def {s['func']}(data)" in f.read(), s["name"]
			else:
				assert os.path.isfile(os.path.join(HOOKS, s["target"])), s["name"]


def test_an_unknown_agent_type_warns_once_and_records_an_event(home, repo):
	data = {
		"hook_event_name": "PreToolUse",
		"tool_name": "Read",
		"tool_input": {"file_path": "x"},
		"agent_id": "a9",
		"agent_type": "nobody-here",
	}
	first, err = run(["pre"], data, home, repo)
	assert "nobody-here" in first.get("systemMessage", "") and "nobody-here" in err
	again, _ = run(["pre"], data, home, repo)
	assert "systemMessage" not in again
	events = [json.loads(line) for line in (repo / ".hook-only" / "events" / "log.jsonl").read_text().splitlines()]
	assert [e["data"]["hook"] for e in events if e["event"] == "hook-failed"] == ["hook-entry-agent-type"]


@pytest.mark.parametrize("agentType", ["developer", "general-purpose", "claude"])
def test_a_known_agent_type_is_not_reported(home, repo, agentType):
	out, _ = run(
		["pre"],
		{
			"hook_event_name": "PreToolUse",
			"tool_name": "Read",
			"tool_input": {"file_path": "x"},
			"agent_id": "a1",
			"agent_type": agentType,
		},
		home,
		repo,
	)
	assert "systemMessage" not in out


# -- order -------------------------------------------------------------------------------------------------


def test_the_pre_entry_runs_its_checks_in_the_agreed_order():
	assert [s["name"] for s in hook_entries.ENTRIES["pre"]["steps"]] == PRE_ORDER


@pytest.mark.parametrize("entryName", ["pre", "handback"])
def test_the_first_deny_ends_a_pre_tool_use_entry(monkeypatch, entryName):
	module = loadEntry()
	steps = [s["name"] for s in hook_entries.ENTRIES[entryName]["steps"]]
	monkeypatch.setattr(module.hook_entries, "applies", lambda s, data: True)
	monkeypatch.setattr(module, "checkAgentType", lambda data: None)
	for denier in steps:
		ran = []

		def fake(s, data, event, denier=denier):
			ran.append(s["name"])
			return module.hookio.refuse(event, f"{s['name']} says no") if s["name"] == denier else None

		monkeypatch.setattr(module, "runStep", fake)
		out = module.runEntry(entryName, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
		assert ran == steps[: steps.index(denier) + 1]
		assert reason(out) == f"{denier} says no"


def test_other_entries_run_every_step_after_a_block(monkeypatch):
	module = loadEntry()
	monkeypatch.setattr(module.hook_entries, "applies", lambda s, data: True)
	monkeypatch.setattr(module, "checkAgentType", lambda data: None)
	ran = []

	def fake(s, data, event):
		ran.append(s["name"])
		return {"decision": "block", "reason": "gate"} if not ran[:-1] else module.hookio.context(event, s["name"])

	monkeypatch.setattr(module, "runStep", fake)
	out = module.runEntry("stop", {"hook_event_name": "SubagentStop"})
	assert ran == [s["name"] for s in hook_entries.ENTRIES["stop"]["steps"]]
	assert out["decision"] == "block" and out["reason"] == "gate"


def test_a_held_dispatch_writes_no_pending_budget(home, repo):
	data = dispatch("qa", "Write the tests.\nBudget: 5 min, $1")
	out, err = run(["pre"], data, home, repo)
	assert decision(out) is None, err
	pending = repo / ".hook-only" / "budget" / "pending-qa.json"
	assert pending.exists()  # control: an allowed dispatch hands its budget over
	pending.unlink()
	hold(repo)
	out, _ = run(["pre"], data, home, repo)
	assert decision(out) == "deny" and "agent-guard hold" in reason(out)
	assert not pending.exists()


def test_a_dispatch_denied_by_worker_time_writes_no_pending_budget(home, repo):
	state = repo / ".hook-only" / "worker-budget"
	state.mkdir(parents=True)
	(state / "s.json").write_text(json.dumps({"started": time.time() - 3600, "minutes": 10}))
	out, _ = run(["pre"], dispatch("qa", "Budget: 5 min, $1"), home, repo, CLAUDE_WORKER_MINUTES="10")
	assert decision(out) == "deny" and reason(out).startswith("Worker time estimate reached")
	assert not (repo / ".hook-only" / "budget" / "pending-qa.json").exists()


def startedRun(repo, minutesAgo):
	runs = repo / ".hook-only" / "runs"
	runs.mkdir(parents=True)
	start = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - minutesAgo * 60))
	(runs / "start-1").write_text(start[:-2] + ":" + start[-2:] + "\n")
	cost = repo / ".claude-cost"
	cost.mkdir()
	(cost / "history.tsv").write_text("")
	return runs


@pytest.mark.parametrize("held", [False, True])
def test_a_held_dispatch_writes_no_auto_resume_record(home, repo, held):
	runs = startedRun(repo, 30)  # 32 - 30 = 2 min left: a 5 min dispatch doesn't fit
	if held:
		hold(repo)
	out, _ = run(["pre"], dispatch("qa", "Budget: 5 min, $1"), home, repo, CLAUDE_WORKER_MINUTES="32")
	assert decision(out) == "deny"
	assert ("agent-guard hold" in reason(out)) == held
	assert (runs / "auto-resume-1.json").exists() == (not held)


def mergeHead(repo):
	git(repo, "checkout", "-q", "-b", "side")
	git(repo, "commit", "-q", "--allow-empty", "-m", "side")
	git(repo, "checkout", "-q", "-")
	git(repo, "commit", "-q", "--allow-empty", "-m", "main")
	git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")


@pytest.mark.parametrize("held", [False, True])
def test_a_held_merge_review_leaves_no_merge_review_flags(home, repo, held):
	mergeHead(repo)
	if held:
		hold(repo)
	out, _ = run(["pre"], dispatch("code-reviewer", "Merge review: x\nBudget: 5 min, $1"), home, repo)
	assert (decision(out) == "deny") == held
	flags = [
		repo / ".hook-only" / "agent-guard" / "merge-review-pending",
		repo / ".hook-only" / "hook-read-only" / "mutation" / "merge-review-pending",
	]
	assert [f.exists() for f in flags] == [not held, not held]


def test_a_denied_reviewer_call_starts_no_scan(monkeypatch):
	module = loadEntry()
	spawned = []
	monkeypatch.setattr(module, "spawnProgram", lambda s, data: spawned.append(s["target"]))
	monkeypatch.setattr(module, "checkAgentType", lambda data: None)
	data = {
		"hook_event_name": "PreToolUse",
		"tool_name": "Read",
		"tool_input": {},
		"agent_id": "r1",
		"agent_type": "code-reviewer",
	}
	monkeypatch.setattr(module.hook_entries, "applies", lambda s, d: s["name"] in ("subagent-budget", "mutation-start"))
	real = module.runStep

	def denyBudget(s, d, event):
		if s["name"] == "subagent-budget":
			return module.hookio.refuse(event, "Budget used up")
		return real(s, d, event)

	monkeypatch.setattr(module, "runStep", denyBudget)
	module.runEntry("pre", data)
	assert spawned == []
	monkeypatch.setattr(
		module, "runStep", lambda s, d, event: None if s["name"] == "subagent-budget" else real(s, d, event)
	)
	module.runEntry("pre", data)
	assert spawned == ["gates/mutation.py"]


def test_a_handback_report_check_refuses_never_reaches_the_gate(home, repo):
	data = {
		"hook_event_name": "PreToolUse",
		"tool_name": "SubagentHandback",
		"tool_input": {"message": "no header"},
		"agent_id": "d1",
		"agent_type": "developer",
	}
	out, _ = run(["handback"], data, home, repo, FLOW_INTEGRATION="bogus")  # crap-gate would deny on the config
	assert decision(out) == "deny" and reason(out).startswith("report-check:")
	assert not (repo / ".hook-only" / "quality").exists()


def badFlowRecord(repo):
	"""The repo's flow-mode record holding no mode: any gate that reads the mode fails on it (it ran)."""
	(repo / ".hook-only").mkdir(exist_ok=True)
	(repo / ".hook-only" / "flow-mode").write_text("bogus\n")


def crapGate(repo, home, command, **extra):
	environ = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	environ.update({"HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo), **extra})
	result = subprocess.run(
		[sys.executable, os.path.join(RUN_HOOKS, "gates", "crap-gate.py")],
		input=json.dumps(
			{
				"hook_event_name": "PreToolUse",
				"tool_name": "Bash",
				"tool_input": {"command": command},
				"cwd": str(repo),
				"session_id": "s",
			}
		),
		capture_output=True,
		text=True,
		env=environ,
		timeout=120,
		cwd=str(repo),
	)
	return json.loads(result.stdout) if result.stdout.strip() else {}


@pytest.mark.parametrize("held", [False, True])
def test_crap_gate_runs_nothing_for_a_held_commit(home, repo, held):
	if held:
		hold(repo)
	badFlowRecord(repo)  # the config error shows it ran
	out = crapGate(repo, home, "git commit -m x")
	assert (decision(out) == "deny") == (not held)


def test_crap_gate_runs_nothing_for_a_merge_commit_with_conflicts(home, repo):
	(repo / "f.txt").write_text("base\n")
	git(repo, "add", "f.txt")
	git(repo, "commit", "-q", "-m", "f")
	git(repo, "checkout", "-q", "-b", "side")
	(repo / "f.txt").write_text("side\n")
	git(repo, "commit", "-q", "-am", "side")
	git(repo, "checkout", "-q", "-")
	(repo / "f.txt").write_text("main\n")
	git(repo, "commit", "-q", "-am", "main")
	subprocess.run(["git", "-C", str(repo), "merge", "side"], capture_output=True)
	badFlowRecord(repo)
	assert crapGate(repo, home, "git commit -m x") == {}


# -- combining ---------------------------------------------------------------------------------------------


def test_a_deny_keeps_the_context_of_the_steps_before_it(monkeypatch):
	module = loadEntry()
	monkeypatch.setattr(module, "checkAgentType", lambda data: None)
	monkeypatch.setattr(module.hook_entries, "applies", lambda s, d: s["name"] in ("agent-guard", "bash-guard"))
	monkeypatch.setattr(
		module,
		"runStep",
		lambda s, d, e: (
			module.hookio.context(e, "notice") if s["name"] == "agent-guard" else module.hookio.refuse(e, "no cd")
		),
	)
	out = module.runEntry("pre", {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
	assert out["hookSpecificOutput"] == {
		"hookEventName": "PreToolUse",
		"permissionDecision": "deny",
		"permissionDecisionReason": "no cd",
		"additionalContext": "notice",
	}


def test_a_program_step_exit_2_is_its_refusal(monkeypatch, tmp_path):
	module = loadEntry()
	program = tmp_path / "gate.py"
	program.write_text("import sys\nsys.stdin.read()\nprint('gate: no', file=sys.stderr)\nsys.exit(2)\n")
	monkeypatch.setattr(module, "HERE", str(tmp_path))
	s = hook_entries.step("gate", "program", "gate.py", [])
	assert reason(module.runStep(s, {}, "PreToolUse")) == "gate: no"
	assert module.runStep(s, {}, "SubagentStop") == {"decision": "block", "reason": "gate: no"}


def test_a_crashing_worker_control_lets_the_prompt_through_and_warns_once(home, repo):
	data = {"hook_event_name": "UserPromptSubmit", "prompt": "wt launch some-topic"}
	environ = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	environ.update({"HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)})
	outputs = []
	for _ in range(2):  # no "cwd": worker-control.py raises KeyError
		result = subprocess.run(
			[sys.executable, ENTRY, "prompt"],
			input=json.dumps(data),
			capture_output=True,
			text=True,
			env=environ,
			timeout=120,
			cwd=str(repo),
		)
		assert result.returncode == 0 and "worker-control" in result.stderr
		outputs.append(json.loads(result.stdout) if result.stdout.strip() else {})
	assert "decision" not in outputs[0] and "worker-control" in outputs[0]["systemMessage"]
	assert "systemMessage" not in outputs[1]
	events = [json.loads(line) for line in (repo / ".hook-only" / "events" / "log.jsonl").read_text().splitlines()]
	assert [e["data"]["hook"] for e in events if e["event"] == "hook-failed"] == ["worker-control"]


def test_a_prompt_that_is_no_wt_command_never_starts_worker_control(monkeypatch):
	steps = {s["name"]: s for s in hook_entries.ENTRIES["prompt"]["steps"]}
	assert not hook_entries.applies(steps["worker-control"], {"prompt": "please review"})
	assert hook_entries.applies(steps["worker-control"], {"prompt": "  wt status"})


# -- settings ------------------------------------------------------------------------------------------------


def tool(*args, env=None):
	return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, timeout=60,
		env={**os.environ, **(env or {})})


def ceilings():
	return homeconfig.get(*hook_entries.CEILINGS)


def settingsFixture(tmp_path, hooks):
	path = tmp_path / "settings.json"
	path.write_text(json.dumps({"model": "x", "hooks": hooks}))
	return str(path)


def test_every_gate_deadline_sits_at_or_below_its_registration_ceiling():
	deadlines = homeconfig.load("quality")["hook_timeouts"]
	assert set(deadlines) == set(ceilings())
	for key, seconds in deadlines.items():
		assert seconds <= ceilings()[key], key
	timed = [r for r in hook_entries.REGISTRATIONS if r.get("timeout_key")]
	assert {hook_entries.timeoutOf(r, ceilings()) for r in timed} == {2700, 5400}


def test_the_settings_tool_only_checks():
	assert tool("--settings").returncode == 2 and tool("--write").returncode == 2
	assert not os.path.exists(os.path.join(HOOKS, "settings-hooks.json"))  # nothing generated, nothing tracked


def test_only_the_guards_block_on_failure_and_no_stop_entry_claims_it():
	hooks = hook_entries.settingsHooks(ceilings())
	blocking = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"] if h.get("onFailure")]
	assert blocking == [hook_entries.cmd("hook-entry.py", "pre"), hook_entries.cmd("gates/crap-gate.py")]
	assert all(
		not h.get("onFailure") for event in ("Stop", "SubagentStop") for g in hooks.get(event, []) for h in g["hooks"]
	)


def test_the_registration_count():
	hooks = hook_entries.settingsHooks(ceilings())
	assert sum(len(g["hooks"]) for groups in hooks.values() for g in groups) == len(hook_entries.REGISTRATIONS) == 12


def changed(hooks, event, which, **fields):
	for group in hooks[event]:
		for hook in group["hooks"]:
			if hook["command"] == which:
				hook.update(fields)
	return hooks


@pytest.mark.parametrize("change, ok, words", [
	(lambda h: h, True, ["fit hooks/lib/hook_entries.py"]),
	(lambda h: changed(h, "SubagentStop", hook_entries.cmd("hook-entry.py", "stop"), timeout=9000), True, ["fit"]),
	(lambda h: changed(h, "SubagentStop", hook_entries.cmd("hook-entry.py", "stop"), timeout=3600), False,
		["timeout 3600 is below its ceiling 5400 s"]),
	(lambda h: changed(h, "Stop", h["Stop"][0]["hooks"][0]["command"], command=hook_entries.cmd("other.py")), False,
		["registrations differ", "other.py"]),
	(lambda h: changed(h, "PreToolUse", hook_entries.cmd("hook-entry.py", "pre"), onFailure=None), False,
		["registrations differ"]),
])
def test_check_settings_compares_registrations_and_ceilings(tmp_path, change, ok, words):
	hooks = change(hook_entries.settingsHooks(ceilings()))
	result = tool("--check-settings", "--file", settingsFixture(tmp_path, hooks))
	assert result.returncode == (0 if ok else 1), result.stdout
	for word in words:
		assert word in result.stdout


def test_settings_whose_four_gate_timeouts_are_below_their_ceilings_fail_the_check(tmp_path):
	"""A settings timeout below its ceiling (here two thirds of it, as when the timeout was the gate's own
	deadline) is reported once per gate, and the check fails."""
	hooks = hook_entries.settingsHooks({k: v // 3 * 2 for k, v in ceilings().items()})
	result = tool("--check-settings", "--file", settingsFixture(tmp_path, hooks))
	assert result.returncode == 1 and result.stdout.count("below its ceiling") == 4

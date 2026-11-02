"""Scripted flow (2026-11-02): flow-next.py turns agent reports and the orchestrator's own calls into the next
step, and worker-control.py switches a topic to it only with "flow": "scripted".

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_flow_next.py
The hook runs as a subprocess on a temporary git repo; HOME points at a temporary home whose
~/.claude/hooks and ~/.claude/skills link to the files under test (cost-history.py is found under ~).
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(os.path.dirname(HOOKS), "skills")
FLOW = os.path.join(HOOKS, "flow-next.py")
COLUMNS = ["date", "topic", "branch", "class", "agent", "round", "estTok", "actTok",
	"estMin", "actMin", "estUsd", "actUsd", "note"]
TOPIC = "demo"


@pytest.fixture
def home(tmp_path):
	h = tmp_path / "home"
	(h / ".claude").mkdir(parents=True)
	os.symlink(HOOKS, h / ".claude" / "hooks")
	os.symlink(SKILLS, h / ".claude" / "skills")
	return h


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def historyRow(agent, round_, estMin, estUsd):
	return {"date": "2026-11-01", "topic": "t", "branch": "feature/x/t", "class": "logic", "agent": agent,
		"round": str(round_), "estMin": str(estMin), "actMin": "3", "estUsd": str(estUsd), "actUsd": "0.3",
		"note": f"est=raw (budget ${estUsd}, {estMin} min)"}


@pytest.fixture
def repo(tmp_path):
	r = tmp_path / "wt"
	r.mkdir()
	git(r, "init", "-q", "-b", "feature/x/demo")
	git(r, "config", "user.email", "t@t")
	git(r, "config", "user.name", "t")
	(r / ".gitignore").write_text("plans/\n.hook-only/\n.claude-cost/\n")
	(r / "src.py").write_text("x = 1\n")
	git(r, "add", ".")
	git(r, "commit", "-q", "-m", "base")
	(r / "plans").mkdir()
	(r / "plans" / f"{TOPIC}.md").write_text("# demo\n\n## Rules\n\n- R1\n")
	rows = [historyRow(a, n, m, u) for a, n, m, u in [
		("qa-planner", 1, 6, 0.8), ("qa-planner", 2, 3, 0.4), ("spec-reviewer", 1, 3, 0.3),
		("qa", 1, 10, 1.2), ("qa", 2, 4, 0.3), ("developer", 1, 12, 1.5), ("developer", 2, 4, 0.4),
		("code-reviewer", 1, 10, 1.0), ("code-reviewer", 2, 3, 0.3)]]
	(r / ".claude-cost").mkdir()
	lines = ["\t".join(COLUMNS)] + ["\t".join(row.get(c, "") for c in COLUMNS) for row in rows]
	(r / ".claude-cost" / "history.tsv").write_text("\n".join(lines) + "\n")
	return r


def env(home, repo):
	out = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
	out.update({"HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)})
	return out


def cli(home, repo, *args):
	return subprocess.run([sys.executable, FLOW, *args], capture_output=True, text=True, env=env(home, repo))


def event(home, repo, mode, data):
	args = [sys.executable, FLOW] + (["report"] if mode == "report" else [])
	result = subprocess.run(args, input=json.dumps({"cwd": str(repo), "session_id": "s", **data}),
		capture_output=True, text=True, env=env(home, repo))
	assert "error" not in result.stderr, result.stderr
	return result.stdout.strip()


def nextMd(repo):
	return (repo / ".hook-only" / "flow" / "next.md").read_text()


def state(repo):
	return json.loads((repo / ".hook-only" / "flow" / "state.json").read_text())


def promptFile(repo, n):
	return (repo / ".hook-only" / "flow" / "prompts" / f"{n}.md").read_text()


def contextText(out):
	"""The additionalContext a main-session event returned."""
	return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def lastPromptNumber(repo):
	return state(repo)["n"]


class Driver:
	"""Plays the orchestrator and the agents against the hook."""

	def __init__(self, home, repo):
		self.home, self.repo, self.ids = home, repo, 0

	def init(self, klass="logic"):
		return cli(self.home, self.repo, "init", str(self.repo), TOPIC, klass)

	def dispatch(self, role):
		"""Make the dispatch next.md asks for (Agent or SendMessage); returns the agent id."""
		text = nextMd(self.repo)
		assert "SendMessage(" in text or f'subagent_type="{role}"' in text, text
		n = text.split(".hook-only/flow/prompts/")[1].split(".md")[0] if "prompts/" in text else None
		message = f"Read .hook-only/flow/prompts/{n}.md and follow it." if n else "Estimate: 10 min, $1"
		if "SendMessage(" in text:
			agent = text.split('SendMessage(to="')[1].split('"')[0]
			out = event(self.home, self.repo, "main", {"hook_event_name": "PostToolUse", "tool_name": "SendMessage",
				"tool_input": {"to": agent, "message": message}, "tool_response": {"success": True}})
			return agent, out
		self.ids += 1
		agent = f"a{self.ids:016x}"
		out = event(self.home, self.repo, "main", {"hook_event_name": "PostToolUse", "tool_name": "Agent",
			"tool_input": {"subagent_type": role, "prompt": message, "run_in_background": True},
			"tool_response": {"status": "async_launched", "agentId": agent}})
		return agent, out

	def report(self, agent, role, text):
		return event(self.home, self.repo, "report", {"hook_event_name": "PostToolUse", "tool_name": "SubagentHandback",
			"agent_id": agent, "agent_type": role, "tool_input": {"message": text}})

	def round(self, role, text, files=None):
		agent, out = self.dispatch(role)
		assert "Wait" in out, out
		for path, content in (files or {}).items():
			(self.repo / path).parent.mkdir(parents=True, exist_ok=True)
			(self.repo / path).write_text(content)
		self.report(agent, role, text)
		return agent

	def commit(self):
		text = nextMd(self.repo)
		message = text.split('git commit -m "')[1].split('"')[0]
		git(self.repo, "commit", "-q", "-m", message)
		return event(self.home, self.repo, "main", {"hook_event_name": "PostToolUse", "tool_name": "Bash",
			"tool_input": {"command": f'git commit -m "{message}"'}, "tool_response": {"stdout": ""}})


PLANNER_OK = "STATUS: DONE\nQUESTIONS: none\n\nplans/tests/demo.md: 12 cases\n**Effort:** 5\n**Friction:** None"


def test_without_the_marker_nothing_happens(home, repo):
	out = event(home, repo, "main", {"hook_event_name": "PostToolUse", "tool_name": "Agent",
		"tool_input": {"subagent_type": "qa", "prompt": "x"}, "tool_response": {"agentId": "a1234567"}})
	assert out == "" and not (repo / ".hook-only" / "flow" / "next.md").exists()


def test_init_asks_for_the_planner_with_an_estimate_from_history(home, repo):
	assert Driver(home, repo).init().returncode == 0
	text = nextMd(repo)
	assert 'Agent(subagent_type="qa-planner", run_in_background=true' in text
	assert "Estimate: 6 min, $0.8" in text
	assert "plans/tests/demo.md" in promptFile(repo, 1)


def test_a_class_outside_the_script_leaves_the_written_flow(home, repo):
	assert Driver(home, repo).init("presentation").returncode == 1
	assert not (repo / ".hook-only" / "flow" / "scripted").exists()


def test_the_whole_test_first_path(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", PLANNER_OK)
	spec = d.round("spec-reviewer", "STATUS: DONE\nVERDICT: CHANGES REQUESTED\nQUESTIONS: none\n\n1. R1: no boundary case\n**Friction:**\n- slow grep")
	assert "no boundary case" in promptFile(repo, lastPromptNumber(repo))
	plan = (repo / "plans" / f"{TOPIC}.md").read_text()
	assert "- spec-reviewer 1: slow grep (flow-next)" in plan
	d.round("qa-planner", PLANNER_OK)
	assert f'SendMessage(to="{spec}"' in nextMd(repo)
	d.round("spec-reviewer", "STATUS: DONE\nVERDICT: APPROVE\nQUESTIONS: none\n\nno findings")
	d.round("qa", "STATUS: DONE\n\n3 failing as expected", files={"tests/test_demo.py": "def test_a():\n    assert False\n"})
	assert "tests/test_demo.py" in git(repo, "diff", "--cached", "--name-only")
	assert 'git commit -m "Add tests for demo"' in nextMd(repo)
	out = contextText(d.commit())
	assert "PHASE" not in out and "PHASE" not in nextMd(repo)
	assert out.startswith("flow-next: # Next step") and 'Agent(subagent_type="developer"' in out
	assert 'Agent(subagent_type="developer"' in nextMd(repo)
	qaCommit = git(repo, "rev-parse", "HEAD").strip()[:12]
	assert f"- {qaCommit}" in (repo / "plans" / f"{TOPIC}.md").read_text()
	assert qaCommit in promptFile(repo, lastPromptNumber(repo))
	d.round("developer", "STATUS: DONE\n\nall pass", files={"src.py": "x = 2\n"})
	assert 'git commit -m "Implement demo"' in nextMd(repo)
	out = contextText(d.commit())
	assert "PHASE" not in out and 'Agent(subagent_type="code-reviewer"' in out
	review = nextMd(repo)
	assert 'Agent(subagent_type="code-reviewer"' in review and "prompts/" not in review and "Estimate:" in review
	reviewer = d.round("code-reviewer", "STATUS: DONE\nVERDICT: CHANGES REQUESTED\nFIX-QA: 1\nFIX-DEVELOPER: 2\n\n## Must fix\n"
		"1. major src.py:1 weak test. Route: QA\n2. major src.py:1 off by one. Route: Developer\n## Follow-up\n3. nit")
	assert f"- {reviewer} (flow-next)" in (repo / "plans" / f"{TOPIC}.md").read_text()
	fix = promptFile(repo, lastPromptNumber(repo))
	assert "Your findings: 1 " in fix and "weak test" in fix
	d.round("qa", "STATUS: DONE\n\n1 new failing", files={"tests/test_demo.py": "def test_a():\n    assert True\n"})
	assert 'git commit -m "Add requested tests for demo"' in nextMd(repo)
	assert "flow-next:" in d.commit()
	assert 'Agent(subagent_type="developer"' in nextMd(repo)
	d.round("developer", "STATUS: DONE\n\nfixed", files={"src.py": "x = 3\n"})
	assert 'git commit -m "Fix review findings in demo"' in nextMd(repo)
	d.commit()
	again = nextMd(repo)
	assert f'SendMessage(to="{reviewer}"' in again
	assert "Commits since your last round" in promptFile(repo, lastPromptNumber(repo))
	d.round("code-reviewer", "STATUS: DONE\nVERDICT: APPROVE\n\n**Friction:** None")
	assert 'Agent(subagent_type="flow-judge"' in nextMd(repo) and "Estimate" not in nextMd(repo)
	d.round("flow-judge", "DECISION: none\nwhy: intent matches")
	assert "Wrap up" in nextMd(repo) and state(repo)["phase"] == "wrapup"
	out = event(home, repo, "main", {"hook_event_name": "PostToolUse", "tool_name": "Bash",
		"tool_input": {"command": "git status"}, "tool_response": {}})
	assert out == ""


def test_a_plan_reviewer_with_questions_goes_to_the_judge_and_its_answers_to_the_planner(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", "STATUS: DONE\nQUESTIONS: Q1\n\nQ1: which key name?\n")
	assert "Kind: questions" in promptFile(repo, lastPromptNumber(repo))
	d.round("flow-judge", "DECISION: answer Q1 use `failures`\nwhy: convention")
	assert "use `failures`" in promptFile(repo, lastPromptNumber(repo))
	assert 'Agent(subagent_type="qa-planner"' in nextMd(repo)


def test_a_question_for_the_user_stops_the_run(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", "STATUS: DONE\nQUESTIONS: Q1\n\nQ1: show it on the chart?\n")
	d.round("flow-judge", "DECISION: ask-user Q1 Should the chart show it?")
	text = nextMd(repo)
	assert "STATUS: needs-decision" in text and "Should the chart show it?" in text
	assert state(repo)["phase"] == "stopped"


def test_a_checkpoint_resumes_once_then_stops(home, repo):
	d = Driver(home, repo)
	d.init()
	planner = d.round("qa-planner", "STATUS: CHECKPOINT\n\nhalf done")
	text = nextMd(repo)
	assert f'SendMessage(to="{planner}"' in text and "Remaining: 6 min, $0.8" in text
	assert "prompts/1.md" in promptFile(repo, lastPromptNumber(repo))
	d.round("qa-planner", "STATUS: CHECKPOINT\n\nstill not done")
	assert "second CHECKPOINT" in nextMd(repo)


def test_a_report_without_a_status_line_stops(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", "I wrote the plan.")
	assert "doesn't follow the report schema (missing `STATUS:` line)" in nextMd(repo)


def test_a_report_from_an_agent_nobody_waits_for_is_ignored(home, repo):
	d = Driver(home, repo)
	d.init()
	before = nextMd(repo)
	d.report("a9999999", "qa", "DONE")
	assert nextMd(repo) == before


def test_needs_test_goes_to_qa_then_the_developer_finishes(home, repo):
	d = Driver(home, repo)
	d.init("small fix")
	d.round("qa", "STATUS: DONE\n\n1 failing", files={"tests/test_bug.py": "def test_b():\n    assert False\n"})
	d.commit()
	d.round("developer", "STATUS: DONE\nNEEDS-TEST: a test that executes src.py:1")
	assert "NEEDS-TEST: a test that executes src.py:1" in promptFile(repo, lastPromptNumber(repo))
	d.round("qa", "STATUS: DONE\n\nadded", files={"tests/test_more.py": "def test_c():\n    assert True\n"})
	d.commit()
	assert "dev-finish" in nextMd(repo)
	d.round("developer", "STATUS: DONE\n\nall pass", files={"src.py": "x = 5\n"})
	d.commit()
	assert "/code-review low" in nextMd(repo)


def test_a_blocked_developer_goes_to_the_judge_and_its_fix_to_the_named_role(home, repo):
	d = Driver(home, repo)
	d.init("small fix")
	d.round("qa", "STATUS: DONE\n\n1 failing", files={"tests/test_bug.py": "def test_b():\n    assert False\n"})
	d.commit()
	d.round("developer", "STATUS: BLOCKED\n\ntest_b contradicts DESIGN.md:12")
	assert "Kind: blocked" in promptFile(repo, lastPromptNumber(repo))
	d.round("flow-judge", "DECISION: must-fix qa fix test_b's expected value per DESIGN.md:12")
	assert 'Agent(subagent_type="qa"' in nextMd(repo)
	assert "fix test_b's expected value" in promptFile(repo, lastPromptNumber(repo))


def test_the_spec_review_stops_at_the_round_cap(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", PLANNER_OK)
	for _ in range(3):
		d.round("spec-reviewer", "STATUS: DONE\nVERDICT: CHANGES REQUESTED\nQUESTIONS: none\n\n1. missing case")
		if "STATUS: needs-decision" in nextMd(repo):
			break
		d.round("qa-planner", PLANNER_OK)
	assert "3 rounds without APPROVE" in nextMd(repo)


def test_a_refused_commit_names_the_fallback_call(home, repo):
	d = Driver(home, repo)
	d.init("small fix")
	d.round("qa", "STATUS: DONE\n\n1 failing", files={"tests/test_bug.py": "def test_b():\n    assert False\n"})
	text = nextMd(repo)
	assert "If the commit is denied" in text and "Refusal:" in text
	n = text.split("instead")[1].split(".hook-only/flow/prompts/")[1].split(".md")[0]
	assert "refused by the quality gate" in promptFile(repo, n)


def test_an_unreadable_judge_answer_stops(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", "STATUS: DONE\nQUESTIONS: Q1\n\nQ1: x?\n")
	d.round("flow-judge", "I think Q1 is internal.")
	assert "missing `DECISION:` line" in nextMd(repo)


def loadWorkerControl():
	spec = importlib.util.spec_from_file_location("worker_control", os.path.join(HOOKS, "worker-control.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_worker_control_switches_a_topic_on_and_off(home, repo, monkeypatch):
	monkeypatch.setenv("HOME", str(home))
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
	wc = loadWorkerControl()
	monkeypatch.setattr(wc, "FLOW_NEXT", __import__("pathlib").Path(FLOW))
	assert wc.scripted_init(repo, TOPIC, "logic")
	assert wc.scripted(repo)
	wc.scripted_off(repo)
	assert not wc.scripted(repo)
	assert not wc.scripted_init(repo, TOPIC, "docs")


def test_dispatch_guard_accepts_the_dispatch_next_md_asks_for(home, repo):
	Driver(home, repo).init()
	text = nextMd(repo)
	message = text.split('prompt="')[1].rsplit('")', 1)[0].replace("\\n", "\n")
	data = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(repo), "session_id": "s",
		"tool_input": {"subagent_type": "qa-planner", "prompt": message}}
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "dispatch-guard.py")], input=json.dumps(data),
		capture_output=True, text=True, env=env(home, repo))
	out = json.loads(result.stdout)["hookSpecificOutput"]
	assert out["permissionDecision"] == "allow", result
	prompt = out["updatedInput"]["prompt"]
	assert prompt.startswith("Read .hook-only/flow/prompts/1.md and follow it.\nBudget: ") and "Estimate: 6 min, $0.8" in prompt


# retry after a fix, and verdicts after the status word (model-config-empty run 1, 2026-11-02)

def test_a_report_without_its_header_stops_with_the_problems(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", PLANNER_OK)
	d.round("spec-reviewer", "DONE — APPROVE (minor suggestions only)")
	text = nextMd(repo)
	assert "STATUS: needs-decision" in text and "missing `STATUS:` line" in text


def stopOnSpec(home, repo):
	d = Driver(home, repo)
	d.init()
	d.round("qa-planner", PLANNER_OK)
	spec = d.round("spec-reviewer", "STATUS: DONE\n\nlooks fine")
	assert "missing `VERDICT:` line" in nextMd(repo)
	return d, spec


def flowState(repo):
	return repo / ".hook-only" / "flow" / "state.json"


def test_retry_handles_the_stopping_report_again_and_the_resume_stays_scripted(home, repo):
	d, spec = stopOnSpec(home, repo)
	assert cli(home, repo, "runnable", str(repo)).returncode == 1
	s = state(repo)
	assert s["stopped_from"]["payload"]["agent_id"] == spec
	s["stopped_from"]["payload"]["text"] = "STATUS: DONE\nVERDICT: APPROVE\nQUESTIONS: none"  # a report the current code reads
	flowState(repo).write_text(json.dumps(s))
	out = cli(home, repo, "retry", str(repo))
	assert out.returncode == 0, out.stderr
	assert 'Agent(subagent_type="qa"' in nextMd(repo) and state(repo)["phase"] == "running"
	assert "stopped_from" not in state(repo)
	assert cli(home, repo, "runnable", str(repo)).returncode == 0


def test_retry_of_a_stop_saved_before_retry_existed(home, repo):
	d, spec = stopOnSpec(home, repo)
	s = state(repo)
	s.pop("stopped_from")
	flowState(repo).write_text(json.dumps(s))
	reports = repo / ".hook-only" / "flow" / "reports.jsonl"
	lines = reports.read_text().splitlines()
	last = json.loads(lines[-1])
	last["text"] = "STATUS: DONE\nVERDICT: APPROVE\nQUESTIONS: none"
	reports.write_text("\n".join(lines[:-1] + [json.dumps(last)]) + "\n")
	assert cli(home, repo, "retry", str(repo)).returncode == 0
	assert 'Agent(subagent_type="qa"' in nextMd(repo)


def test_retry_that_still_cannot_read_the_report_stays_stopped_and_can_be_retried(home, repo):
	stopOnSpec(home, repo)
	out = cli(home, repo, "retry", str(repo))
	assert out.returncode == 0
	assert "missing `VERDICT:` line" in nextMd(repo) and state(repo)["phase"] == "stopped"
	assert state(repo)["stopped_from"]["payload"]["text"] == "STATUS: DONE\n\nlooks fine"


def seqs(repo):
	log = (repo / ".hook-only" / "flow" / "log.jsonl").read_text().splitlines()
	return [json.loads(line)["seq"] for line in log]


def test_retry_keeps_the_step_numbers_increasing(home, repo):
	stopOnSpec(home, repo)
	stopSeq = state(repo)["seq"]
	assert f"(flow-next #{stopSeq})" in nextMd(repo)
	s = state(repo)
	s["stopped_from"]["payload"]["text"] = "STATUS: DONE\nVERDICT: APPROVE\nQUESTIONS: none"
	flowState(repo).write_text(json.dumps(s))
	assert cli(home, repo, "retry", str(repo)).returncode == 0
	assert state(repo)["seq"] == stopSeq + 1
	assert f"(flow-next #{stopSeq + 1})" in nextMd(repo)
	numbers = seqs(repo)
	assert numbers == sorted(set(numbers)), numbers


def test_nothing_to_retry_without_a_stop(home, repo):
	Driver(home, repo).init()
	assert cli(home, repo, "retry", str(repo)).returncode == 1


def test_worker_control_resumes_scripted_only_when_the_flow_can_go_on(home, repo, monkeypatch):
	monkeypatch.setenv("HOME", str(home))
	wc = loadWorkerControl()
	monkeypatch.setattr(wc, "FLOW_NEXT", __import__("pathlib").Path(FLOW))
	stopOnSpec(home, repo)
	assert not wc.scripted_runnable(repo)
	s = state(repo)
	s["stopped_from"]["payload"]["text"] = "STATUS: DONE\nVERDICT: APPROVE\nQUESTIONS: none"
	flowState(repo).write_text(json.dumps(s))
	assert wc.scripted_runnable(repo)  # the resume retries the stop by itself
	assert 'Agent(subagent_type="qa"' in nextMd(repo)

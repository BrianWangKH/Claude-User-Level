#!/usr/bin/env python3
"""Scripted flow (feature-flow scripted.md): the hook decides the orchestrator's next step.

Only for topics started with "flow": "scripted" in plans/worker/<topic>.json: worker-control.py runs
`flow-next.py init <worktree> <topic> <class>` at launch, which writes .hook-only/flow/scripted and the
first .hook-only/flow/next.md. Without that marker every event is a no-op, so other topics are untouched.

The orchestrator still makes every dispatch and commit itself, so dispatch-guard, agent-guard, crap-gate,
merge-commit-guard and the budget hooks apply unchanged; this hook only says what to do next.

Events (stdin JSON):
  report  (hook-entry.py's `post` entry, for the report-schema roles: PostToolUse SubagentHandback, tool_input.message): only an agent id
          this hook is waiting for counts. PostToolUse runs only after the hand-back went through, so a
          report a quality gate sent back (crap-gate, mutation) never counts. Stop is not used: crap-gate
          and mutation may block a Stop and hooks of one event run in parallel, so the last message at Stop
          may not be final. An agent that ends without a hand-back leaves next.md at "wait", and
          scripted.md makes the orchestrator stop with `STATUS: needs-decision`.
          The report decides the next step, written to next.md (the orchestrator Reads it when the report
          arrives).
  main    (PostToolUse Agent|SendMessage through hook-entry.py's `post` entry, Bash `git commit` through its own
          settings registration; main session): a dispatch records the agent id
          it now waits for; a commit (HEAD moved while a commit is pending) records it and moves on. The
          next step also goes back as additionalContext.
CLI: init <worktree> <topic> <class> | off <worktree> (worker-control.py: a run the script doesn't drive,
such as a fix run or merge mode, goes back to the written flow) | retry <worktree> (planning session, after
flow-next.py was fixed: a stop caused by a report is undone and that report is handled again by the current
code; the state before the stop and the report are kept with every stop; the step counter `seq` keeps
counting). A `wt resume` keeps the scripted flow while the state can go on (after a retry, or in the wrap-up)
and turns it off after a stop that wasn't retried: that stop waits for the person's decision, which the
written flow carries.

Reports are read only through their header (report-schema.json, report_schema.py), which report_check.py
enforces at hand-back; a report without a valid header (report-check lets one through after max_denials)
stops the flow.
Next steps: dispatch (assignment in .hook-only/flow/prompts/<n>.md, read by the agent; every assignment names the
agent's round: its dispatches in that role on this topic, this one included), commit (this hook stages exactly
the files the committing role changed, below; the orchestrator runs one `git commit`), STATUS stop, MANUAL (the
written flow: wrap-up, /code-review for a small fix). There is no PHASE step: the orchestrator's context stays
small here (next.md and reports), so a phase compaction would cost more than it saves; the tests commit leads
straight to the developer and the implementation commit straight to the first code review. Anything it
can't read (no status line, no verdict, no DECISION line, a second CHECKPOINT, the round cap) is a
`STATUS: needs-decision` stop. Round caps (budget settings): flow_review_rounds_max spec or code review rounds
without APPROVE and decision rounds of open questions; flow_refused_commits_max refused commits of one kind
(tests or implementation) in a row: the commit step that would meet the last refusal tells the orchestrator to
end with `STATUS: needs-decision` on a denial instead of sending it to a fresh agent (a denied commit fires no
hook, so the hook can't see it; the state stays at that commit step, and a resume tries it again).
What a commit stages: the committing role (qa for tests, developer for the implementation) owns what changed in
the working tree since its first dispatch after its last commit (a snapshot of every dirty path and its content,
taken when that dispatch is written); a path dirty then and unchanged since is someone else's and stays out.
A developer that reports NEEDS-TEST mid-implementation keeps its unfinished change uncommitted in the working
tree, outside the requested tests' commit (crap-gate checks neither red nor green on that later test-only commit);
its own next commit takes it in. Committing it first is refused by crap-gate (the uncovered lines NEEDS-TEST is
about, a red suite); stashing or moving it out happens inside the developer's round (the report arrives before
its second SubagentStop), where agent-guard reads a stash entry appearing on the branch as an ALERT.
Decisions go to the flow-judge agent (Opus), which answers only in `DECISION:` lines.
Fails open (stderr): the orchestrator then sees no new next.md and stops on its own rule.
"""
import fcntl
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402  (config/ and the hooks/ role directories on sys.path; HOME_ROOT)
import homeconfig  # noqa: E402
import hookonly  # noqa: E402
import report_schema  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "lib", "flow-templates.json")
COST_HISTORY = os.path.join(homepath.HOME_ROOT, "skills", "repo-plan", "cost-history.py")  # this tree's (T3)
PROMPT_REF = re.compile(r"\.hook-only/flow/prompts/(\d+)\.md")
AGENT_ID = re.compile(r"agentId\W+([0-9a-z]{6,})", re.IGNORECASE)
FRICTION = re.compile(r"^[\W_]*friction[\W_]*:?[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE)
CLASSES = ("test-first", "small-fix")
COMMIT_ROLES = {"tests": "qa", "impl": "developer"}  # who makes each kind of commit


class Stop(Exception):
	"""A stop the orchestrator reports as `STATUS: needs-decision`."""


# ---------- files

def flowDir(root):
	return hookonly.stateDir(root, "flow")


def git(root, *args):
	return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout


def head(root):
	return git(root, "rev-parse", "-q", "--verify", "HEAD").strip()


def changedPaths(root):
	"""Paths git reports as changed or untracked (renames: the new path)."""
	paths = []
	for line in git(root, "status", "--porcelain", "--untracked-files=all").splitlines():
		path = line[3:]
		if " -> " in path:
			path = path.split(" -> ", 1)[1]
		path = path.strip('"')
		if path and not hookonly.isHookOnlyPath(path):
			paths.append(path)
	return paths


def contentOf(root, path):
	"""What path holds in the working tree: the sha256 of its bytes, a symlink's target, or "absent"."""
	full = os.path.join(root, path)
	if os.path.islink(full):
		return "link:" + os.readlink(full)
	if not os.path.isfile(full):
		return "absent"
	with open(full, "rb") as f:
		return hashlib.sha256(f.read()).hexdigest()


def dirtyState(root):
	"""{path: contentOf} of every changed or untracked path (changedPaths)."""
	return {path: contentOf(root, path) for path in changedPaths(root)}


def roundCaps():
	"""(flow_review_rounds_max, flow_refused_commits_max) of the budget settings."""
	budget = homeconfig.load("budget")
	return budget["flow_review_rounds_max"], budget["flow_refused_commits_max"]


def appendUnder(root, topic, heading, line):
	"""Append a line under `## <heading>` of plans/<topic>.md (the section is added when missing)."""
	path = os.path.join(root, "plans", f"{topic}.md")
	if not os.path.isfile(path):
		return
	with open(path, encoding="utf-8") as f:
		text = f.read()
	marker = f"## {heading}"
	if marker not in text:
		text = text.rstrip("\n") + f"\n\n{marker}\n\n{line}\n"
	else:
		start = text.index(marker) + len(marker)
		nxt = text.find("\n## ", start)
		end = len(text) if nxt < 0 else nxt
		body = text[start:end].rstrip("\n")
		text = text[:start] + body + f"\n{line}\n" + ("\n" if nxt >= 0 else "") + text[end:].lstrip("\n")
	with open(path, "w", encoding="utf-8") as f:
		f.write(text)


def loadCostHistory():
	import importlib.util
	spec = importlib.util.spec_from_file_location("cost_history", COST_HISTORY)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


# ---------- the flow

class Flow:
	def __init__(self, root, state):
		self.root, self.s = root, state
		with open(TEMPLATES, encoding="utf-8") as f:
			self.templates = json.load(f)

	# --- building blocks

	def prompt(self, template, purpose, role, **fields):
		"""Write the next assignment file; return its number."""
		self.s["n"] = n = self.s.get("n", 0) + 1
		fields = {"topic": self.s["topic"], "commits": ", ".join(self.s.get("qa_commits") or []) or "none",
			"text": "", **fields}
		body = self.templates[template].format(**fields)
		body += "\n\n" + self.templates["round"].format(round=(self.s.get("dispatched") or {}).get(role, 0) + 1,
			role=role)
		if role != "flow-judge":
			body += "\n\n" + self.templates["common"]
		directory = os.path.join(flowDir(self.root), "prompts")
		os.makedirs(directory, exist_ok=True)
		with open(os.path.join(directory, f"{n}.md"), "w", encoding="utf-8") as f:
			f.write(body + "\n")
		self.s.setdefault("prompts", {})[str(n)] = {"role": role, "purpose": purpose, "n": n}
		return n

	def estimate(self, role, resume=False):
		"""`Estimate:` (or `Remaining:`) line from the median raw estimate of this role's rounds of the same
		kind (first round / later rounds) in the cost history; dispatch-guard models it into the Budget."""
		later = self.s.setdefault("dispatched", {}).get(role, 0) > 0
		minutes = usd = None
		try:
			ch = loadCostHistory()
			rows = [r for r in ch.newestFirst(ch.load(os.path.join(self.root, ch.HISTORY)))
				if ch.isValid(r) and r["agent"] == role and ch.RAW_MARK in (r.get("note") or "")]
			kind = [r for r in rows if (r.get("round") != "1") == later] or rows
			window = ch.modelSettings()[0]
			mins = [ch.number(r["estMin"]) for r in kind[:window] if ch.number(r["estMin"])]
			usds = [ch.number(r["estUsd"]) for r in kind[:window] if ch.number(r["estUsd"])]
			minutes = statistics.median(mins) if mins else None
			usd = statistics.median(usds) if usds else None
		except Exception as error:
			print(f"flow-next: no history estimate for {role} ({error})", file=sys.stderr)
		if minutes is None:
			minutes = homeconfig.load("budget")["fallback_minutes"].get(role)
		word = "Remaining" if resume else "Estimate"
		if minutes is None:
			return ""
		if usd is None:
			return f"Budget: {round(minutes):g} min"
		return f"{word}: {minutes:g} min, ${usd:g}"

	def dispatch(self, role, template, purpose, resume=None, refusal=False, checkpoint=False, **fields):
		if role in COMMIT_ROLES.values():  # what the role's next commit may take starts here (module doc)
			self.s.setdefault("since", {}).setdefault(role, dirtyState(self.root))
		n = None if (role == "code-reviewer" and not resume) else self.prompt(template, purpose, role, **fields)
		if n is None:
			self.s.setdefault("prompts", {})["review"] = {"role": role, "purpose": purpose}
		meta = self.s["prompts"][str(n) if n is not None else "review"]
		if role == "flow-judge":
			line = ""
		elif checkpoint:
			# The round after a CHECKPOINT: the remaining work, sized like the round itself (agents.md).
			line = (fields.get("estimate") or self.estimate(role)).replace("Estimate:", "Remaining:")
		else:
			line = self.estimate(role)
		meta["estimate"] = line
		return {"do": "dispatch", "role": role, "n": n, "resume": resume, "purpose": purpose,
			"refusal": refusal, "estimate": line}

	def commit(self, kind, after):
		"""Stage what the committing role changed (module doc) and ask for the commit; a refused commit goes back
		to that role, until flow_refused_commits_max refusals in a row, where a denial ends the run instead."""
		role = COMMIT_ROLES[kind]
		before = (self.s.get("since") or {}).get(role, {})
		paths = [p for p in changedPaths(self.root) if before.get(p) != contentOf(self.root, p)]
		if not paths:
			raise Stop(f"the {'tests' if kind == 'tests' else 'implementation'} round reported DONE but changed no file")
		subprocess.run(["git", "-C", self.root, "add", "-A", "--", *paths], capture_output=True, check=True)
		attempts = self.s.setdefault("commit_attempts", {})
		attempts[kind] = attempts.get(kind, 0) + 1
		cap = roundCaps()[1]
		if attempts[kind] >= cap:
			refused = {"do": "status", "status": "needs-decision", "text": f"the {kind} commit was refused {cap} "
				"times in a row (budget flow_refused_commits_max); quote this last denial verbatim"}
		else:
			refused = self.dispatch(role, "qa-refused" if kind == "tests" else "dev-refused", f"refused-{kind}",
				refusal=True)
		self.s["commit"] = {"kind": kind, "after": after, "head": head(self.root)}
		self.s[f"refused_after_{kind}"] = after
		if kind == "tests":
			verb = "Add tests for" if after in ("phase-tests", "small-dev") else "Add requested tests for"
		else:
			verb = "Implement" if after in ("phase-dev", "small-review") else "Fix review findings in"
		return {"do": "commit", "message": f"{verb} {self.s['topic']}", "paths": paths, "refused": refused}

	def stop(self, text):
		self.s["phase"] = "stopped"
		return {"do": "status", "status": "needs-decision", "text": text}

	def nextFix(self):
		"""The next queued fix (review or judge findings), else the re-review."""
		queue = self.s.get("queue") or []
		if queue:
			item = queue.pop(0)
			self.s["queue"] = queue
			if item["role"] == "qa":
				return self.dispatch("qa", "qa-fix", "tests-fix", text=item["text"])
			return self.dispatch("developer", "dev-fix", "dev-fix", text=item["text"])
		return self.review()

	def review(self):
		rid = self.s.get("reviewer_id")
		rounds = self.s.get("review_round", 0)
		cap = roundCaps()[0]
		if rid and rounds >= cap:
			raise Stop(f"code review: {cap} rounds without APPROVE (flow_review_rounds_max); the last "
				"report is in .hook-only/flow/reports/")
		since = self.s.get("review_commits") or []
		self.s["review_commits"] = []
		if rid:
			return self.dispatch("code-reviewer", "review-again", "review", resume=rid, commits=", ".join(since) or "none")
		return self.dispatch("code-reviewer", "", "review")

	def judge(self, kind, text, **fields):
		return self.dispatch("flow-judge", f"judge-{kind}", f"judge-{kind}", text=text, **fields)

	# --- start

	def start(self):
		if self.s["class"] == "small-fix":
			return self.dispatch("qa", "qa-regression", "tests")
		return self.dispatch("qa-planner", "planner", "plan")

	# --- events

	def dispatched(self, role, agentId, n):
		meta = (self.s.get("prompts") or {}).get(str(n) if n is not None else "review")
		if not meta or meta["role"] != role:
			return None
		self.s.setdefault("waiting", {})[agentId] = meta
		self.s.setdefault("dispatched", {})[role] = self.s["dispatched"].get(role, 0) + 1
		if role == "code-reviewer":
			if self.s.get("reviewer_id") != agentId:
				self.s["reviewer_id"] = agentId
				appendUnder(self.root, self.s["topic"], "Reviewer", f"- {agentId} (flow-next)")
			self.s["review_round"] = self.s.get("review_round", 0) + 1
		if role == "spec-reviewer":
			self.s["spec_id"] = agentId
			self.s["spec_round"] = self.s.get("spec_round", 0) + 1
		return {"do": "wait", "role": role, "agent": agentId}

	def committed(self):
		pending = self.s.get("commit")
		now = head(self.root)
		if not pending or not now or now == pending["head"]:
			return None
		self.s.pop("commit")
		(self.s.get("since") or {}).pop(COMMIT_ROLES[pending["kind"]], None)
		(self.s.get("commit_attempts") or {}).pop(pending["kind"], None)
		short = now[:12]
		if pending["kind"] == "tests":
			self.s.setdefault("qa_commits", []).append(short)
			appendUnder(self.root, self.s["topic"], "QA commits", f"- {short}")
		if self.s.get("reviewer_id"):
			self.s.setdefault("review_commits", []).append(short)
		after = pending["after"]
		# "phase-tests" / "phase-dev" name the first tests / implementation commit of a test-first topic (kept
		# as labels so saved states still read); neither stops at a phase any more (module doc).
		if after in ("phase-tests", "small-dev"):
			return self.dispatch("developer", "dev", "dev")
		if after == "phase-dev":
			return self.review()
		if after == "dev-finish":
			return self.dispatch("developer", "dev-finish", "dev-finish")
		if after == "small-review":
			self.s["phase"] = "wrapup"
			return {"do": "manual", "text": "Small fix: run `/code-review low` on the branch; route its must-fix "
				"findings as small-fix.md says (the scripted flow ends here), then follow \"Wrap up\" in "
				"feature-flow SKILL.md and end with `STATUS: done`."}
		return self.nextFix()

	def reported(self, agentId, role, text):
		meta = (self.s.get("waiting") or {}).pop(agentId, None)
		if not meta:
			return None
		self.friction(role, text)
		purpose = meta["purpose"]
		problems = report_schema.check(role, text)
		if problems:
			raise Stop(f"the {role} report doesn't follow the report schema ({'; '.join(problems)}); report:\n\n{text}")
		fields = report_schema.header(text)
		if role == "flow-judge":
			return self.decided(purpose, fields, text)
		status = report_schema.first(fields, "STATUS")
		if role in ("spec-reviewer", "code-reviewer"):
			if status != "DONE":
				raise Stop(f"the {role} reported {status}; report:\n\n{text}")
			return self.verdict(role, agentId, purpose, fields, text)
		if status == "CHECKPOINT":
			count = self.s.setdefault("checkpoints", {}).get(purpose, 0) + 1
			self.s["checkpoints"][purpose] = count
			if count > 1:
				raise Stop(f"second CHECKPOINT of the {role} on the same task ({purpose}); report:\n\n{text}")
			original = f".hook-only/flow/prompts/{meta['n']}.md" if meta.get("n") else "your assignment"
			return self.dispatch(role, "resume", purpose, resume=agentId, checkpoint=True, text=original,
				estimate=meta.get("estimate", ""))
		if status == "BLOCKED":
			self.s["blocked"] = {"role": role, "purpose": purpose, "agent": agentId}
			return self.judge("blocked", text, who=role)
		return self.done(role, purpose, fields, text)

	def done(self, role, purpose, fields, text):
		if role == "qa-planner":
			if report_schema.first(fields, "QUESTIONS") != "none":
				return self.questions(text)
			return self.toSpec(text)
		if role == "qa":
			after = {"tests": "phase-tests" if self.s["class"] == "test-first" else "small-dev",
				"tests-needs": "dev-finish", "tests-fix": "fix",
				"refused-tests": self.s.get("refused_after_tests")}.get(purpose)
			if not after:
				raise Stop(f"unexpected DONE from qa ({purpose})")
			return self.commit("tests", after)
		if role == "developer":
			need = report_schema.first(fields, "NEEDS-TEST")
			if need:
				return self.dispatch("qa", "qa-needs-test", "tests-needs", text=f"NEEDS-TEST: {need}")
			if purpose == "refused-impl":
				after = self.s.get("refused_after_impl") or "fix"
			elif purpose in ("dev", "dev-finish") and not self.s.get("reviewer_id"):
				after = "phase-dev" if self.s["class"] == "test-first" else "small-review"
			else:
				after = "fix"
			return self.commit("impl", after)
		raise Stop(f"unexpected DONE from {role} ({purpose})")

	def questions(self, text):
		self.s["question_rounds"] = self.s.get("question_rounds", 0) + 1
		cap = roundCaps()[0]
		if self.s["question_rounds"] > cap:
			raise Stop(f"open questions keep coming back after {cap} decision rounds; last report:\n\n{text}")
		return self.judge("questions", text)

	def toSpec(self, plannerReport):
		if self.s.get("spec_id"):
			return self.dispatch("spec-reviewer", "spec-again", "spec", resume=self.s["spec_id"], text=plannerReport)
		return self.dispatch("spec-reviewer", "spec", "spec")

	def verdict(self, role, agentId, purpose, fields, text):
		verdict = report_schema.first(fields, "VERDICT")
		if role == "spec-reviewer":
			if verdict == "APPROVE":
				self.s["spec_open"] = False
				if report_schema.first(fields, "QUESTIONS") != "none":
					return self.questions(text)
				return self.dispatch("qa", "qa", "tests")
			self.s["spec_open"] = True
			cap = roundCaps()[0]
			if self.s.get("spec_round", 0) >= cap:
				raise Stop(f"spec review: {cap} rounds without APPROVE (flow_review_rounds_max); last report:\n\n{text}")
			return self.dispatch("qa-planner", "planner-revise", "plan-revise", text=text)
		if verdict == "APPROVE":
			return self.judge("approve", text)
		queue = []
		for name, who in (("FIX-QA", "qa"), ("FIX-DEVELOPER", "developer")):
			ids = report_schema.first(fields, name) or "none"
			if ids != "none":
				queue.append({"role": who, "text": f"Your findings: {ids} (the reviewer's report follows).\n\n{text}"})
		if not queue:
			return self.judge("route", text)
		cap = roundCaps()[0]
		if self.s.get("review_round", 0) >= cap:
			raise Stop(f"code review: {cap} rounds without APPROVE (flow_review_rounds_max); last report:\n\n{text}")
		self.s["queue"] = queue
		return self.nextFix()

	def decided(self, purpose, fields, text):
		decisions = []
		for line in fields.get("DECISION") or []:
			kind, _, rest = line.partition(" ")
			decisions.append((kind.lower(), rest.strip()))
		if not decisions:
			raise Stop(f"flow-judge ({purpose}) gave no DECISION line; its report:\n\n{text}")
		asks = [d for d in decisions if d[0] == "ask-user"]
		if asks:
			raise Stop("questions for the user (flow-judge):\n" + "\n".join(f"- {a[1]}" for a in asks)
				+ f"\n\nThe judge's report:\n\n{text}")
		if purpose == "judge-questions":
			answers = "\n".join(f"- {d[1]}" for d in decisions if d[0] == "answer")
			if not answers:
				raise Stop(f"flow-judge (questions) answered nothing; its report:\n\n{text}")
			return self.dispatch("qa-planner", "planner-decisions", "plan-decisions", text=answers)
		fixes = [d for d in decisions if d[0] == "must-fix"]
		if purpose == "judge-approve" and decisions[0][0] == "none" and not fixes:
			self.s["phase"] = "wrapup"
			return {"do": "manual", "text": "The code reviewer approved and flow-judge found nothing open. Wrap up: "
				"follow \"Wrap up\" in feature-flow SKILL.md (`QA commits`, `Reviewer` and agent `Friction` are "
				"already in the plan), then end with `STATUS: done`."}
		queue = []
		for _, rest in fixes:
			role, _, body = rest.partition(" ")
			role = role.lower()
			if role not in ("qa", "developer") or not body.strip():
				raise Stop(f"flow-judge must-fix line not `must-fix qa|developer <text>`: {rest}")
			queue.append({"role": role, "text": body.strip()})
		if purpose == "judge-blocked":
			blocked = self.s.pop("blocked", {})
			todos = [d[1] for d in decisions if d[0] == "todo"]
			for todo in todos:
				appendUnder(self.root, self.s["topic"], "TODO items", f"- {todo} (flow-judge, blocked {blocked.get('role')})")
			if not queue:
				if todos:
					raise Stop("the blocked step was judged TODO; nothing left to dispatch for it:\n"
						+ "\n".join(f"- {t}" for t in todos))
				raise Stop(f"flow-judge (blocked) decided nothing actionable; its report:\n\n{text}")
		if not queue:
			raise Stop(f"flow-judge ({purpose}) decided nothing actionable; its report:\n\n{text}")
		self.s["queue"] = queue
		return self.nextFix()

	# --- report parsing

	def friction(self, role, text):
		m = FRICTION.search(text)
		if not m:
			return
		lines = [m.group(1).strip()] + [l.strip() for l in text[m.end():].splitlines()]
		items = []
		for line in lines:
			if not line:
				if items:
					break
				continue
			if re.match(r"^[\W_]*(none|n/a)\b", line, re.IGNORECASE) or re.match(r"^\*\*\w", line):
				break
			items.append(line.lstrip("-* ").strip())
		count = self.s.setdefault("rounds", {}).get(role, 0) + 1
		self.s["rounds"][role] = count
		for item in items:
			appendUnder(self.root, self.s["topic"], "Friction", f"- {role} {count}: {item} (flow-next)")


# ---------- rendering

WAIT = "then end your turn without a STATUS line and wait for the report."


def call(action):
	"""The one tool call a dispatch action asks for."""
	lines = []
	if action["n"] is not None:
		lines.append(f"Read .hook-only/flow/prompts/{action['n']}.md and follow it.")
	if action["refusal"]:
		lines.append("Refusal:\n<the commit's denial message, verbatim>")
	if action["estimate"]:
		lines.append(action["estimate"])
	message = "\\n".join(l.replace("\n", "\\n") for l in lines)
	if action["resume"]:
		return f'SendMessage(to="{action["resume"]}", message="{message}")'
	return (f'Agent(subagent_type="{action["role"]}", run_in_background=true, '
		f'description="{action["role"]}: {action["purpose"]}", prompt="{message}")')


def render(action, seq):
	top = f"# Next step (flow-next #{seq})\n\n"
	do = action["do"]
	if do == "dispatch":
		return top + f"Make exactly this call, {WAIT}\n\n{call(action)}\n"
	if do == "commit":
		refused = action["refused"]
		if refused["do"] == "status":
			denied = (f"If the commit is denied, don't send it back: end the run: your last message starts with "
				f"`STATUS: {refused['status']}`, then, in the user's preferred language, the reason below with the "
				f"denial message, a recommendation, what is done, and the next step.\n\nReason:\n\n{refused['text']}\n")
		else:
			denied = ("If the commit is denied, put the denial message in place of the `<...>` line and make this call "
				f"instead, {WAIT}\n\n{call(refused)}\n")
		return top + ("The hook staged the agent's files:\n" + "\n".join(f"- {p}" for p in action["paths"])
			+ f"\n\nRun exactly:\n\ngit commit -m \"{action['message']}\"\n\n" + denied)
	if do == "wait":
		return top + (f"Wait: end your turn without a STATUS line; the report of the {action['role']} {action['agent']} "
			"starts the next turn. Then Read this file again.\n")
	if do == "status":
		return top + (f"End the run: your last message starts with `STATUS: {action['status']}`, then, in the user's "
			f"preferred language, the reason below with a recommendation, what is done, and the next step.\n\nReason:\n\n{action['text']}\n")
	if do == "manual":
		return top + action["text"] + "\n"
	raise ValueError(do)


# ---------- state and IO

class Locked:
	def __init__(self, root):
		self.root = root
		os.makedirs(flowDir(root), exist_ok=True)
		self.lock = open(os.path.join(flowDir(root), ".lock"), "w")

	def __enter__(self):
		fcntl.flock(self.lock, fcntl.LOCK_EX)
		path = os.path.join(flowDir(self.root), "state.json")
		with open(path, encoding="utf-8") as f:
			self.state = json.load(f)
		return self

	def save(self, action, event):
		self.state["seq"] = seq = self.state.get("seq", 0) + 1
		text = render(action, seq)
		tmp = os.path.join(flowDir(self.root), "state.json.tmp")
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(self.state, f, indent=1)
		os.replace(tmp, os.path.join(flowDir(self.root), "state.json"))
		with open(os.path.join(flowDir(self.root), "next.md"), "w", encoding="utf-8") as f:
			f.write(text)
		with open(os.path.join(flowDir(self.root), "log.jsonl"), "a", encoding="utf-8") as f:
			f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "seq": seq, "event": event,
				"do": action["do"], "detail": {k: v for k, v in action.items() if k in ("role", "purpose", "n", "status")}}) + "\n")
		return text

	def __exit__(self, *exc):
		fcntl.flock(self.lock, fcntl.LOCK_UN)
		self.lock.close()


def active(root):
	return bool(root) and os.path.isfile(os.path.join(flowDir(root), "scripted"))


def run(root, event, fn, payload=None):
	"""Apply one event under the lock; returns the rendered next step or None. A stop keeps the state before
	the event and the event's payload (a report), so `retry` can handle it again after a fix."""
	with Locked(root) as locked:
		if locked.state.get("phase") in ("stopped", "wrapup", "off"):
			return None
		before = json.loads(json.dumps({k: v for k, v in locked.state.items() if k != "stopped_from"}))
		flow = Flow(root, locked.state)
		try:
			action = fn(flow)
		except Stop as stop:
			action = flow.stop(str(stop))
		if action is None:
			return None
		if action["do"] == "status":
			locked.state["stopped_from"] = {"state": before, "payload": payload}
		return locked.save(action, event)


def onReport(data):
	root = hookonly.projectRoot(data)
	if not active(root):
		return
	agentId, role = data.get("agent_id"), data.get("agent_type")
	message = (data.get("tool_input") or {}).get("message")
	text = message if isinstance(message, str) else ""
	if data.get("hook_event_name") != "PostToolUse" or not agentId or not role:
		return
	with open(os.path.join(flowDir(root), "reports.jsonl"), "a", encoding="utf-8") as f:
		f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent_id": agentId, "agent_type": role,
			"event": data.get("hook_event_name"), "text": text}) + "\n")
	run(root, f"report {role}", lambda flow: flow.reported(agentId, role, text),
		{"kind": "report", "agent_id": agentId, "agent_type": role, "text": text})


def onMain(data):
	if data.get("agent_id"):
		return
	root = hookonly.projectRoot(data)
	if not active(root):
		return
	tool, toolInput = data.get("tool_name"), data.get("tool_input") or {}
	if tool == "Agent":
		role, text = toolInput.get("subagent_type") or "", toolInput.get("prompt") or ""
		found = AGENT_ID.search(json.dumps(data.get("tool_response"), ensure_ascii=False))
		agentId = found.group(1) if found else None
	elif tool == "SendMessage":
		agentId, text = str(toolInput.get("to") or ""), toolInput.get("message") or ""
		role = None
	elif tool == "Bash":
		context(run(root, "bash", lambda flow: flow.committed()))
		return
	else:
		return
	if not agentId:
		return
	ref = PROMPT_REF.search(text)
	n = int(ref.group(1)) if ref else None

	def apply(flow):
		who = role or ((flow.s.get("prompts") or {}).get(str(n)) or {}).get("role")
		return flow.dispatched(who, agentId, n) if who else None
	context(run(root, f"dispatch {tool}", apply))


def context(text):
	"""The next step back to the orchestrator as PostToolUse additionalContext."""
	if text:
		print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "flow-next: " + text}}))


def init(worktree, topic, klass):
	root = os.path.realpath(worktree)
	klass = {"logic": "test-first", "wiring": "test-first", "small fix": "small-fix"}.get(klass, klass)
	os.makedirs(flowDir(root), exist_ok=True)
	if klass not in CLASSES:
		print(f"flow-next: class {klass} isn't scripted; the written flow runs", file=sys.stderr)
		return 1
	state = {"topic": topic, "class": klass, "phase": "running"}
	with open(os.path.join(flowDir(root), "state.json"), "w", encoding="utf-8") as f:
		json.dump(state, f)
	with open(os.path.join(flowDir(root), "scripted"), "w", encoding="utf-8") as f:
		f.write(json.dumps({"topic": topic, "class": klass, "at": time.time()}) + "\n")
	run(root, "init", lambda flow: flow.start())
	return 0


def retry(worktree):
	"""Undo the last stop and handle its report again with the current code (module doc)."""
	root = os.path.realpath(worktree)
	with Locked(root) as locked:
		stopped = locked.state.get("stopped_from") or legacyStop(root, locked.state)
		payload = stopped.get("payload") or {}
		if locked.state.get("phase") != "stopped" or payload.get("kind") != "report":
			print("flow-next: nothing to retry (no stop caused by a report)", file=sys.stderr)
			return 1
		seq = locked.state.get("seq", 0)
		locked.state.clear()
		locked.state.update(json.loads(json.dumps(stopped["state"])))  # a copy: the retry mustn't change the saved stop
		locked.state["seq"] = seq  # step numbers keep increasing; the saved state has the count before the stop
		flow = Flow(root, locked.state)
		try:
			action = flow.reported(payload["agent_id"], payload["agent_type"], payload["text"])
		except Stop as stop:
			action = flow.stop(str(stop))
			locked.state["stopped_from"] = stopped
		if action is None:
			print("flow-next: the report no longer matches a waiting agent", file=sys.stderr)
			return 1
		print(locked.save(action, "retry"))
	return 0


def legacyStop(root, state):
	"""A stop saved before `stopped_from` existed: the last report in reports.jsonl, waited for again under
	the newest assignment of its role (the state the report found, as far as a report changes it)."""
	if state.get("phase") != "stopped":
		return {}
	try:
		with open(os.path.join(flowDir(root), "reports.jsonl"), encoding="utf-8") as f:
			last = [json.loads(l) for l in f if l.strip()][-1]
	except (OSError, ValueError, IndexError):
		return {}
	metas = [(int(k) if k.isdigit() else 0, m) for k, m in (state.get("prompts") or {}).items()
		if m.get("role") == last["agent_type"]]
	if not metas:
		return {}
	before = json.loads(json.dumps(state))
	before["phase"] = "running"
	before.setdefault("waiting", {})[last["agent_id"]] = max(metas, key=lambda x: x[0])[1]
	return {"state": before, "payload": {"kind": "report", "agent_id": last["agent_id"],
		"agent_type": last["agent_type"], "text": last["text"]}}


def runnable(worktree):
	"""True when a resume can go on in the scripted flow (module doc)."""
	try:
		with open(os.path.join(flowDir(os.path.realpath(worktree)), "state.json"), encoding="utf-8") as f:
			return json.load(f).get("phase") in ("running", "wrapup")
	except (OSError, ValueError):
		return False


def off(worktree):
	root = os.path.realpath(worktree)
	try:
		os.remove(os.path.join(flowDir(root), "scripted"))
	except FileNotFoundError:
		pass
	return 0


def main():
	if len(sys.argv) > 1 and sys.argv[1] == "init":
		sys.exit(init(*sys.argv[2:5]))
	if len(sys.argv) > 1 and sys.argv[1] == "off":
		sys.exit(off(sys.argv[2]))
	if len(sys.argv) > 1 and sys.argv[1] == "retry":
		sys.exit(retry(sys.argv[2]))
	if len(sys.argv) > 1 and sys.argv[1] == "runnable":
		sys.exit(0 if runnable(sys.argv[2]) else 1)
	data = json.load(sys.stdin)
	if len(sys.argv) > 1 and sys.argv[1] == "report":
		onReport(data)
	else:
		onMain(data)


if __name__ == "__main__":
	try:
		main()
	except SystemExit:
		raise
	except Exception as error:
		print(f"flow-next error (no next step written): {error}", file=sys.stderr)
	sys.exit(0)

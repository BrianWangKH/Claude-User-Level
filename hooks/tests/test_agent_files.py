"""The agents and skills a home (~/.claude) change is drafted and verified with, and the agent registrations every
agent type needs.

- Every budgeted agent type (home.json budget fallback_minutes) has an agents/<name>.md, so a budget never names
  an agent that doesn't exist; the three home agents are known to hook-entry.py and stay out of the worker flow's
  budget table (they belong to the home-config flow).
- home-drafter and home-verifier preload skill home-draft (home-drafter also refactor and clean-code); the skills
  they preload exist in this home and allow model invocation (disable-model-invocation prevents preloading).
- Tools: home-drafter writes only through Bash (no Write or Edit: those reach the project, not the draft) and may
  dispatch; home-verifier and investigator can't dispatch; investigator can't write.
- skill home-draft carries its procedure and the apply templates (they parse); the suite commands of the template
  and of SKILL.md's targeted run have no `--with ruff` (the person's offline uv cache has none) and run in parallel
  with pytest-xdist, the worker count from hooks/tools/suite-workers.py and `--max-worker-restart 0` (a dying
  worker fails the run instead of being replaced); verify.md step 1's full run is one command,
  hooks/tools/home-draft-suite.py inside the ulimit wrapper.
- The tests of the private layer's files (the home-change flow, which the public export leaves out) carry a
  private_layer mark (conftest.py): skipped only in a tree without any of those files; the investigator's own
  checks don't depend on them.
- verify.md starts each verification round with a snapshot, re-verifies only the changes since the last one, and the
  main session checks the verifier's evidence (suite logs) instead of rerunning the suite; the main session's skill
  files point to that check.
- The full suite runs once per draft state, in the default mode, by the drafter (home-draft-suite.py), its log
  sealed; the verifier checks that log and never reruns it; the main session's check is home-draft-main-check.py; the fake HOME and both apply paths run only when the apply script or templates change; a fix
  round continues the same drafter (SendMessage); a small change takes the main session's own draft and check and
  the verifier's small review of its removed-behaviour table alone (no suite).

Run (from the home root): bash -c 'ulimit -u $(( $(ps -u $(id -u) -L --no-headers | wc -l) + 400 )); ulimit -v
4000000; exec uv run --offline --no-project --with pytest python -m pytest hooks/tests/test_agent_files.py -q
-p no:cacheprovider'
"""

import ast
import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(HOOKS)
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import hook_entries  # noqa: E402
import homeconfig  # noqa: E402

HOME_AGENTS = ("home-drafter", "home-verifier", "investigator")
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
DISPATCH_TOOLS = {"Agent", "Task"}
HOME_DRAFT = os.path.join(ROOT, "skills", "home-draft")
# Run only as copies in a draft directory (apply.sh, tools/build_fake.py), whose files their paths name.
TEMPLATES = (os.path.join("templates", "apply.sh.template"), os.path.join("templates", "build_fake.py.template"))


def frontmatter(path):
	"""{key: value} of a markdown file's frontmatter; a `key:` followed by `  - item` lines gives a list."""
	with open(path, encoding="utf-8") as f:
		lines = f.read().split("\n")
	assert lines[0] == "---", path
	fields, key = {}, None
	for line in lines[1 : lines.index("---", 1)]:
		if line.startswith("  - ") and key is not None:
			fields[key].append(line[4:].strip())
		elif ":" in line:
			key, value = (part.strip() for part in line.split(":", 1))
			fields[key] = value if value else []
	return fields


def agentFile(name):
	return os.path.join(ROOT, "agents", f"{name}.md")


def toolsOf(name):
	return {t.strip() for t in frontmatter(agentFile(name))["tools"].split(",")}


def test_every_budgeted_agent_type_has_an_agent_file():
	budgeted = homeconfig.load("budget")["fallback_minutes"]
	assert sorted(a for a in budgeted if not os.path.isfile(agentFile(a))) == []


@pytest.mark.parametrize("name", [
	pytest.param("home-drafter", marks=pytest.mark.private_layer("agents/home-drafter.md")),
	pytest.param("home-verifier", marks=pytest.mark.private_layer("agents/home-verifier.md")),
	"investigator",
])
def test_the_home_agents_are_known_and_kept_out_of_the_worker_budget(name):
	assert name in HOME_AGENTS
	budgeted = homeconfig.load("budget")["fallback_minutes"]
	known = hook_entries.knownAgents(None, homeconfig.get("rules", "builtin_agent_types"))
	assert name not in budgeted and name in known, name
	assert frontmatter(agentFile(name))["name"] == name


def test_the_parameters_above_cover_every_home_agent():
	assert sorted(HOME_AGENTS) == ["home-drafter", "home-verifier", "investigator"]


@pytest.mark.parametrize("agent, skills", [
	pytest.param("home-drafter", ["home-draft", "refactor", "clean-code"], marks=pytest.mark.private_layer(
		"agents/home-drafter.md")),
	pytest.param("home-verifier", ["home-draft"], marks=pytest.mark.private_layer("agents/home-verifier.md")),
])
def test_the_home_agents_preload_the_home_draft_skill(agent, skills):
	assert frontmatter(agentFile(agent))["skills"] == skills


@pytest.mark.parametrize("skill", [pytest.param("home-draft", marks=pytest.mark.private_layer("skills/home-draft/")),
	"refactor", "clean-code"])
def test_a_skill_a_home_agent_preloads_exists_and_allows_model_invocation(skill):
	fields = frontmatter(os.path.join(ROOT, "skills", skill, "SKILL.md"))
	assert fields["name"] == skill
	assert fields.get("disable-model-invocation", "false") != "true"


def test_the_investigator_can_t_write_or_dispatch():
	assert (WRITE_TOOLS | DISPATCH_TOOLS) & toolsOf("investigator") == set()


@pytest.mark.private_layer("agents/home-drafter.md", "agents/home-verifier.md")
def test_only_the_home_drafter_may_dispatch_and_no_home_agent_writes_through_write_or_edit():
	assert DISPATCH_TOOLS & toolsOf("home-drafter") == {"Agent"}
	for name in HOME_AGENTS:
		assert WRITE_TOOLS & toolsOf(name) == set(), name
	for name in ("home-verifier", "investigator"):
		assert DISPATCH_TOOLS & toolsOf(name) == set(), name


@pytest.mark.private_layer("skills/home-draft/")
def test_the_home_draft_skill_carries_its_procedure_and_templates():
	for rel in ("SKILL.md", "verify.md", *TEMPLATES):
		assert os.path.isfile(os.path.join(HOME_DRAFT, rel)), rel
	script, fake = (os.path.join(HOME_DRAFT, rel) for rel in TEMPLATES)
	assert subprocess.run(["bash", "-n", script], capture_output=True).returncode == 0
	with open(fake, encoding="utf-8") as f:
		ast.parse(f.read())
	with open(script, encoding="utf-8") as f:
		text = f.read()
	assert "--with pytest-cov==7.1.0" in text and "--with ruff" not in text
	tests = [line for line in text.splitlines() if line.startswith("TESTS=(")]
	assert len(tests) == 1 and "--with pytest-xdist==3.8.0" in tests[0] and "--max-worker-restart 0" in tests[0]
	assert '-n "$workers"' in text and "hooks/tools/suite-workers.py" in text


def sectionOf(text, heading):
	start = text.index(heading + "\n")
	end = text.find("\n## ", start + len(heading))
	return text[start : len(text) if end < 0 else end]


@pytest.mark.private_layer("skills/home-draft/verify.md", "skills/route/ops/home-draft.md",
	"skills/route/playbooks/home-config.md")
def test_the_home_verify_procedure_reverifies_from_a_snapshot_and_the_main_session_checks_evidence():
	with open(os.path.join(ROOT, "skills", "home-draft", "verify.md"), encoding="utf-8") as f:
		verify = f.read()
	snapshot = sectionOf(verify, "## 0. Round snapshot")
	assert "round-<r>" in snapshot and "home-draft/templates" in snapshot and "home.sha256" in snapshot
	# The only parts of the home left out: the logs the main session appends to while a draft is open.
	assert snapshot.count("skills/home-draft/fingerprint.py") == 1 and "never followed" in snapshot
	assert "round-<r>.report" in snapshot
	assert "highest `<r>` of any round name" in snapshot and "`check-<r>/` never numbers a round" in snapshot
	suite = sectionOf(verify, "## 1. Suite on the overlay")
	assert "python -m pytest" not in suite and "FLOW_INTEGRATION=local" not in suite and ".local.log" not in suite
	commands = [line for line in suite.splitlines() if "home-draft-suite.py <draft>" in line]
	assert len(commands) == 1, commands
	assert commands[0].startswith("bash -c 'ulimit -u $(( $(ps -u $(id -u) -L --no-headers | wc -l) + 400 )); "
		"ulimit -v 4000000; exec python3 ") and commands[0].endswith("/hooks/tools/home-draft-suite.py <draft>'")
	with open(os.path.join(HOME_DRAFT, "SKILL.md"), encoding="utf-8") as f:
		targeted = [line.strip() for line in f if "python -m pytest" in line]
	assert len(targeted) == 1, targeted
	assert targeted[0].startswith("bash -c 'ulimit -u $(( $(ps -u $(id -u) -L --no-headers | wc -l) + 400 )); ")
	assert "--with pytest-xdist==3.8.0" in targeted[0] and "--max-worker-restart 0" in targeted[0]
	assert "w=$(python3 <draft>/overlay-<n>/hooks/tools/suite-workers.py) || exit 1" in targeted[0]
	assert '-n "$w"' in targeted[0] and "--with ruff" not in targeted[0]
	reverify = sectionOf(verify, "## Re-verification after a fix round")
	assert "diff -r <draft>/round-<b> <draft>/round-<r>" in reverify
	assert "a void round is never a base and never cited" in reverify
	assert "every unchanged row whose owner or locking test lives in a changed file" in reverify
	assert "In full every round: step 1" in reverify
	check = sectionOf(verify, "## Main-session check")
	assert "pytest" not in check and "sha256sum" in check and "home tree the overlay copied" in check
	assert "/hooks/tools/home-draft-main-check.py <draft>" in check
	for rel in (("route", "ops", "home-draft.md"), ("route", "playbooks", "home-config.md")):
		with open(os.path.join(ROOT, "skills", *rel), encoding="utf-8") as f:
			assert "Main-session check" in f.read(), rel


@pytest.mark.private_layer("skills/home-draft/verify.md", "skills/home-draft/SKILL.md", "agents/home-verifier.md",
	"agents/home-drafter.md", "skills/route/ops/home-draft.md", "skills/route/playbooks/home-config.md")
def test_the_full_suite_runs_once_per_draft_state_by_the_drafter_and_the_verifier_checks_its_sealed_logs():
	def read(*rel):
		with open(os.path.join(ROOT, *rel), encoding="utf-8") as f:
			return f.read()

	verify = read("skills", "home-draft", "verify.md")
	suite = sectionOf(verify, "## 1. Suite on the overlay")
	drafter, verifier = suite.split("**Verifier: the drafter's sealed log, never a rerun.**")
	assert drafter.count("home-draft-suite.py <draft>") == 1 and "python -m pytest" not in suite
	assert "`<draft>/suite-<n>-sealed/`" in drafter and "mkdir <draft>/round-<r>-sealed" in verifier
	assert "`new:` line equals" in verifier and "FLOW_INTEGRATION=unset" in verifier
	assert "FLOW_INTEGRATION=local" not in verify and ".local.log" not in verify
	assert "Steps 5 and 6 run only when the draft changes `apply.sh`" in sectionOf(verify, "## 5. Fake HOME")
	reverify = sectionOf(verify, "## Re-verification after a fix round")
	assert "no suite of the verifier's own" in reverify and "step 6.2" not in reverify
	skill = read("skills", "home-draft", "SKILL.md")
	assert "Before handing back, exactly once" in skill and "never the full suite" in skill
	small = sectionOf(skill, "## Small changes")
	assert "home-draft-size.py" in small and "home-draft-check.py" in small and "never a marker file" in small
	verifierAgent = read("agents", "home-verifier.md")
	assert "Never rerun the full suite" in verifierAgent and "run the full suite yourself" not in verifierAgent
	assert "exactly once per hand-back" in read("agents", "home-drafter.md")
	ops = read("skills", "route", "ops", "home-draft.md")
	assert "SendMessage" in ops and "only when that agent is gone" in ops and "home-draft-check.py" in ops
	assert "home-draft-main-check.py" in ops
	assert "home-draft-check.py" in read("skills", "route", "playbooks", "home-config.md")
	review = sectionOf(verify, "## Small review")
	assert "python -m pytest" not in review and "small-review-<n>.report" in review and "set -C" in review
	assert "mkdir <draft>/small-review-<n>-sealed" in review and "No suite, no overlay, no fake HOME" in review
	assert "Small review" in verifierAgent and "Small review" in ops and "dispatch `home-verifier` for the small review" in ops


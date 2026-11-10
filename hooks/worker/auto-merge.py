#!/usr/bin/env python3
"""Local mode: merge a finished worktree branch into the local default branch without a person.

Usage: auto-merge.py <worktree> <topic> <run> [--person]   (run-steps.py's merge step, after a run that ended
`STATUS: done`)
--person (`wt merge <topic>`: worker-control.py starts run-steps.py step merge --person, the person's press after
reading a refusal): the person is the
review, so the three checks that ask for one are skipped (5: held paths and new URLs; 6: the code
reviewer's APPROVE) and so is 3's "main moved during the run" (the person merges now; 4 still needs the
branch to contain main, else a merge run as below), and so is the stop of a run stopped only during its wrap-up,
after its claude part completed (runstate.WRAPUP_STOP: the person continued that wrap-up with `wt wrapup`; the
automatic merge refuses it, a stop of claude's part refuses both). Every other check stands (STATUS, ALERT/HOLD,
clean worktree, tests and checks, real-data checks). The decision records "review": "person". Each decision is
also an event (wt_events.py: merged, refused).
Only in local mode (the topic's flow mode, homeconfig.flowMode of the worktree: its record, which agents
can't write); the main checkout comes from WT_MAIN_DIR (worker-control.py). Runs outside Claude Code's sandbox; the tests it
runs go through qg_common.runCommand (bwrap). The decision goes to .hook-only/runs/merge-<run>.json
({"decision": "merged", "commit"} or {"decision": "refused", "reason"}), read by run-result.py.

A person only approves plans; this replaces the PR click, so every condition is something the hooks
recorded or git shows, never the orchestrator's report:
  1. the run ended `STATUS: done`; no agent-guard ALERT or HOLD is pending
  2. the repo has quality gates (qg_common.loadConfig at the base: .claude/quality-gates.json, or languages
     detected from the tracked files, which run on their home profiles' test and checks): they define the
     test commands. Nothing to gate (no repo file, no detected language), or a config that doesn't load (a
     tool a command needs is missing), refuses
  3. the main checkout is on the integration branch (homeconfig.integrationBranch), and that branch is where
     the run found it (runs/main-<run>, written by run-worker.sh at the start). When it moved only by
     earlier automatic merges (merges.jsonl), the answer is "a merge run is needed"; anything else moved
     it, maybe an agent, so a person looks
  4. the branch already contains the integration branch (otherwise a merge run is needed first); the
     worktree is clean apart from PR_DESCRIPTION*.md and sandbox placeholders
  5. no path a person must see: the home list in auto_merge_hold (homeconfig, ~/.claude/config/) (.claude/, CI,
     dependency manifests), plus the repo's "auto_merge_hold" in quality-gates.json; and no added line
     with a URL in any changed text file except docs (.md, .txt, .rst) and test paths (a new outbound
     address can hide in a script or config as well as in code)
  6. the last full code-reviewer verdict agent-guard recorded (.hook-only/reviews.jsonl) is APPROVE, and
     the branch's own source changes after that commit are covered: walking the first-parent commits,
     a plain commit may not touch source; a merge commit counts only what differs from git's automatic
     merge of its parents (hand resolution, git merge-tree), and that needs a merge-review APPROVE at that
     commit (feature-flow merge.md step 7; reviews of kind "merge").
     Exception, a docs-only branch: every changed file is a doc (.md, .txt, .rst) outside the test paths
     and no full code-reviewer verdict is recorded (docs topics get no reviewer). A config file is not a
     doc (it changes behaviour), nor is a .txt under a test path (test data); a recorded verdict is always
     honoured. The decision records "review": "docs-only" so these merges can be found later
  7. every configured language's `test` command passes on the branch, in the sandbox; then every entry of
     each language's optional "checks" (format, lint: qg_common.checkCommands) passes the same way, with
     the same cwd, env and placeholders; an {"argv", "empty_output": true} entry also fails on any output
     (e.g. `gofmt -l`). A malformed "checks" refuses, and so does a command that never starts
     (qg_common.NotStarted), naming its key and where the value comes from. Without "checks" only `test` runs
  8. when the planning session wrote real-data checks (<main>/plans/verify/<topic>.json), verify.py's
     result for this run (runs/verify-<run>.json) says "passed"
  9. when that spec has a "verifier" section, verifier.py's result for this run (runs/verifier-<run>.json)
     says "passed": the verifier agent found every rule of the approved plan and every judge item holding
     on the real data
Then `git merge --no-ff` in the main checkout (the repo's git hooks off), the message from
PR_DESCRIPTION.md. Right before it, the merge intent (state kind merge-intent, <main>/.hook-only/auto-merge/
intent-<topic>-<run>.json: the branch, base = the integration branch's head, tip = the branch's head, the
message's subject, the review), which only hooks can write: proof that every condition above was checked.
A crash between the merge and its merges.jsonl line (merge seam) leaves main moved without a record; an evaluation
of the same run that finds the integration branch moved past the intent's base proves the merge (provenMerge) and
only then records it, decision "merged" with "recovered": true: an intent for this topic and run; the head a merge
commit whose parents are exactly (base, tip), base where the run started (main_head_at_start; not asked of the
person's `wt merge`), tip the worktree's head now; its subject the intent's; its tree the one `git merge-tree
--write-tree base tip` computes now (so even a commit made by hand holds exactly what this merge would have made);
nothing on the branch after it. Any of it failing refuses with what failed, asking the person to check and `wt
merge` (the person's `wt merge` itself then goes through the checks as usual). Without an intent, or with the
branch still at the intent's base, nothing is proven and the checks run as before. A bad merge is one `git revert -m 1 <commit>` away. This program only decides (migration step
6 of the run record): what follows a decision is run-steps.py's, which reads merge-<run>.json after this exits.
After a merge its finish step starts auto-finish.py detached: once the run's wrapper is gone it archives the
worktree's records, imports the cost, queues the friction for the next planning session and removes the worktree.
When the only obstacle is that the branch lacks the latest main (3: moved only by automatic merges, or 4:
not contained), the refusal says so ("merge_run": true) and run-steps.py's next step has worker-control.py
auto-merge resume the worker in merge mode by itself (feature-flow merge.md: main merged into the branch,
conflicts resolved by the worker, a hand-resolved hunk merge-reviewed), within worker_auto_merge_max / _minutes /
_usd (home.json budget); that run's `STATUS: done` comes back here. Likewise when the real data shows the
branch wrong (8: a check failed; 9: the verifier's verdict is "fail"; not an error of the spec, the environment
or the verifier itself), the refusal says "fix_run": true and the next step has worker-control.py auto-fix resume
the worker with the findings, within worker_auto_fix_max; never on the person's `wt merge`, who then decides.
Those two refusals are events of kind "retry" (the flow handles them); every other refusal waits for a person.
Checks, merge
and the decision file are written under an exclusive flock on <main>/.hook-only/auto-merge/lock: two workers
finishing together can't both pass the "main unchanged" check and stack an untested combination, and an
evaluation waiting for the lock sees the decision of the one before it.

Already merged, checked first under the lock, before any condition above: a duplicate evaluation must not
undo a merge (seen: a second `wt merge` waited for the lock, found main holding the first one's merge
commit, judged "the branch doesn't contain main", overwrote merge-<run>.json with "merge_run", so the finish
failed and merge runs with nothing to merge looped). The run is already merged when its merge-<run>.json
says merged, or when the branch tip is an ancestor of the integration branch and merges.jsonl records this
branch with a commit that is in it (a branch just created from main has no record, and goes through the
checks as before). Then:
  - merge-<run>.json exists (any decision): nothing is rewritten, no event, nothing started
  - it doesn't (a later run of a merged branch, e.g. a merge run that found nothing to merge): the worktree
    must be clean (else refused, waiting for a person); the decision is {"decision": "merged", "commit":
    the recorded merge commit, "review": "already-merged"}, an event "merged" says so, and the finish step
    starts auto-finish; never a merge run
A `wt merge` approval record (runs/approve-<run>.json, worker-control.py) belongs to the run-steps.py process
that runs this one, which removes it when its steps end (wt_controls.releaseApproval); this program removes it
only when its owner is gone.
The run record (hooks/lib/runstate.py, migration step 2: dual write): every merge-<run>.json written here is also the
record's steps.merge (read back from the file as the legacy adapter reads it: outcome merged, already-merged or
refused, the reason, the commit, the merge_run and fix_run flags), through runstate.dualWrite: a record write that
fails is reported once and changes nothing here. run-steps.py marks the step running before it starts this
program (an earlier attempt's decision stays in it until this one's replaces it); only a done merge step counts
as a decision here (recordedMerge).
The run's facts (migration step 5): conditions 1 (STATUS, ALERT/HOLD), 3 (where main was at the start), 8 and 9
come from runstate.gateView, both stories of the run at once: the record (runs/<run>.json) and today's files
(result-, main-, verify-, verifier-<run>.json, read by the legacy adapter), each through runstate.status. While
today's files are still written they are still the truth, so every fact must hold in both: a run without a
usable record (none, an unknown schema, a missing field, a value outside its enum, a time without an offset), a
phase other than finishing or ended (an orphan included: no pid check, so a stale heartbeat refuses), a stop, an
exit other than completed, a worker outcome other than a STATUS, a STATUS other than done or not leading the
header written exactly, an integration mode other than local, no main head or two different ones, a real-data
step that didn't PASS where the topic's verify spec (as it reads now) asks for it, or a spec that can't be read
(plans/ not visible included) where the verifier step would decide: each is a refusal written like any other.
A fix run is asked for only when both stories hold the failed step (verify FAIL naming its failed checks, as
verify.py writes them; verifier FAIL under a spec whose "verifier" section is set); any disagreement between them
waits for a person. The gate is never looser than the checks of today's files alone (the old check, frozen in
tests/legacy_rules.py; tests/test_auto_merge.py runs both over every phase and step outcome).
Any exception (migration step 5, the audit's S7): the decision {"decision": "error", "reason": <one line>} and a
"refused" event, unless the decision file changed since this evaluation began (its own decision written, or
another evaluation's): that one stands and only the event says what failed; then exit 1.
Fails: closed — any missing, unknown or contradicting fact refuses; a crash leaves an error decision.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import homeconfig  # noqa: E402
import hookonly  # noqa: E402
import qg_common  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402
import wt_controls  # noqa: E402
import wt_events  # noqa: E402

URL = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
INTENT = "merge-intent"  # <main>/.hook-only/auto-merge/intent-<topic>-<run>.json (config/registry/state-registry.json)
DOC_EXTENSIONS = (".md", ".txt", ".rst")


class Refused(Exception):
	pass


class NeedsMergeRun(Refused):
	"""The branch lacks the latest main and nothing else is known to be wrong yet: a merge run fixes it."""


class NeedsFixRun(Refused):
	"""The real data shows the branch wrong (a failed check or the verifier's "fail"): the worker fixes it."""


class Unproven(Refused):
	"""The integration branch moved past this run's merge intent, and the merge can't be proven (module doc)."""


def git(cwd, *args, check=True):
	result = subprocess.run(["git", "-C", cwd, "-c", "core.hooksPath=/dev/null", *args], capture_output=True,
		text=True)
	if check and result.returncode != 0:
		raise Refused(f"git {' '.join(args[:3])} failed: {(result.stderr or result.stdout).strip()[:300]}")
	return result.stdout.strip()


def leftovers(worktree):
	out = []
	for line in git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines():
		path = line[3:]
		if line.startswith("?? ") and (re.fullmatch(r"PR_DESCRIPTION[^/]*\.md", path)
				or os.path.exists(os.path.join(worktree, path)) and not os.path.isfile(os.path.join(worktree, path))
				and not os.path.isdir(os.path.join(worktree, path))):
			continue
		out.append(line)
	return out


def heldPaths(changed, config):
	patterns = list(homeconfig.load("auto_merge_hold")["paths"]) + list(config.get("auto_merge_hold") or [])
	return [p for p in changed if qg_common.isExcluded(p, patterns)]


def addedUrls(worktree, base, config):
	"""Added lines with a URL in changed text files, docs and test paths left out (module doc, 5)."""
	found, path = [], None
	for line in git(worktree, "diff", "-U0", "--no-color", f"{base}...HEAD").splitlines():
		if line.startswith("+++ "):
			path = line[6:] if line.startswith("+++ b/") else None
			if path and (path.endswith(DOC_EXTENSIONS) or qg_common.isExcluded(path, config["exclude"])):
				path = None
		elif path and line.startswith("+") and URL.search(line):
			found.append(f"{path}: {line[1:].strip()[:120]}")
	return found


def reviews(worktree):
	path = os.path.join(worktree, ".hook-only", "reviews.jsonl")
	try:
		with open(path, encoding="utf-8") as f:
			return [json.loads(l) for l in f if l.strip()]
	except (OSError, ValueError):
		return []


def ownChanges(worktree, commit):
	"""Files the branch itself changed in commit: everything for a plain commit; for a merge commit only
	what differs from git's automatic merge of its parents (hand resolution)."""
	parents = git(worktree, "rev-list", "--parents", "-n", "1", commit).split()[1:]
	if len(parents) < 2:
		return [p for p in git(worktree, "diff", "--name-only", f"{commit}^", commit).splitlines() if p]
	result = subprocess.run(["git", "-C", worktree, "merge-tree", "--write-tree", parents[0], parents[1]],
		capture_output=True, text=True)
	tree = (result.stdout.splitlines() or [""])[0].strip()
	if not tree:
		raise Refused(f"git merge-tree failed for {commit[:10]}: {result.stderr.strip()[:200]}")
	return [p for p in git(worktree, "diff", "--name-only", tree, commit).splitlines() if p]


def docsOnly(changed, config):
	"""Module doc, 6, exception: every changed file is a doc outside the test paths."""
	return bool(changed) and all(p.endswith(DOC_EXTENSIONS) and not qg_common.isExcluded(p, config["exclude"])
		for p in changed)


def checkApproval(worktree, config, changed):
	"""Module doc, 6: returns how the branch passed ("approved" or "docs-only"); raises Refused unless the
	branch's source changes are all reviewed."""
	recorded = reviews(worktree)
	full = [r for r in recorded if r.get("kind", "full") != "merge"]
	if not full and docsOnly(changed, config):
		return "docs-only"
	if not full or full[-1].get("verdict") != "APPROVE":
		raise Refused("no APPROVE from the code reviewer recorded by agent-guard (.hook-only/reviews.jsonl)")
	approvedMerges = {r.get("head") for r in recorded if r.get("kind") == "merge" and r.get("verdict") == "APPROVE"}
	approved = full[-1]["head"]
	for commit in git(worktree, "rev-list", "--first-parent", "--reverse", f"{approved}..HEAD").splitlines():
		source = [p for p in ownChanges(worktree, commit) if qg_common.isSource(p, config)]
		if not source:
			continue
		isMerge = len(git(worktree, "rev-list", "--parents", "-n", "1", commit).split()) > 2
		if not isMerge:
			raise Refused(f"source changed after the approved commit {approved[:10]} (in {commit[:10]}: "
				+ ", ".join(source[:5]) + ")")
		if commit not in approvedMerges:
			raise Refused(f"merge {commit[:10]} resolved source by hand ({', '.join(source[:5])}) without a "
				"merge-review APPROVE (feature-flow merge.md step 7)")
	return "approved"


def movedOnlyByAutoMerges(mainDir, started, now):
	"""Whether every first-parent commit main gained since started is a recorded automatic merge."""
	try:
		with open(os.path.join(mainDir, ".hook-only", "auto-merge", "merges.jsonl"), encoding="utf-8") as f:
			merged = {json.loads(l)["commit"] for l in f if l.strip()}
	except (OSError, ValueError, KeyError):
		return False
	gained = git(mainDir, "rev-list", "--first-parent", f"{started}..{now}", check=False).split()
	return bool(gained) and all(c in merged for c in gained)


def readDecision(path):
	try:
		with open(path, encoding="utf-8") as f:
			value = json.load(f)
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		return {}  # there but unreadable: still never overwritten by an already-merged run
	return value if isinstance(value, dict) else {}


def alreadyMerged(worktree, mainDir):
	"""Module doc, already merged: (target, the merge commit that brought the branch tip in) or None."""
	target = homeconfig.integrationBranch(worktree, "local")
	branch = git(worktree, "symbolic-ref", "-q", "--short", "HEAD", check=False)
	head = git(worktree, "rev-parse", "-q", "--verify", "HEAD", check=False)
	if not target or not branch or not head:
		return None
	if not isAncestor(mainDir, head, target):
		return None
	try:
		with open(os.path.join(mainDir, ".hook-only", "auto-merge", "merges.jsonl"), encoding="utf-8") as f:
			records = [json.loads(l) for l in f if l.strip()]
	except (OSError, ValueError):
		return None
	for record in reversed(records):
		if not isinstance(record, dict) or record.get("branch") != branch or not isinstance(record.get("commit"), str):
			continue
		commit = record["commit"]
		if isAncestor(mainDir, commit, target):
			return target, commit
	return None


def isAncestor(repo, commit, of):
	return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", commit, of],
		capture_output=True).returncode == 0


def repoGate(worktree, mainDir):
	"""Condition 2 and the first half of 3 (module doc): (the quality-gate config, the integration branch)."""
	try:
		config, _ = qg_common.loadConfig(worktree)
	except homeconfig.ConfigError as error:
		raise Refused(f"the quality-gate config doesn't load: {error}")
	if config is None:
		raise Refused("nothing to gate: no .claude/quality-gates.json at the base and no tracked file of a language "
			f"with a home profile ({homeconfig.where('toolchains')}): no test commands to trust")
	target = homeconfig.integrationBranch(worktree, "local")
	if target is None:
		raise Refused("no local main or master")
	if git(mainDir, "symbolic-ref", "-q", "HEAD", check=False) != f"refs/heads/{target}":
		raise Refused(f"the main checkout is not on {target}")
	return config, target


def branchGate(worktree, mainDir, target, started, config, person):
	"""Conditions 3 (main where the run found it: started), 4, 5 and 6 (module doc); returns how the branch was
	reviewed."""
	now = git(mainDir, "rev-parse", target)
	if now != started and not person:
		if movedOnlyByAutoMerges(mainDir, started, now):
			raise NeedsMergeRun(f"other topics were merged into {target} automatically during the run: a merge "
				"run is needed first")
		raise Refused(f"{target} moved during the run ({started[:10]} -> {now[:10]}), not by an automatic merge; "
			"check who moved it (`git reflog " + target + "`) before merging by hand")
	if subprocess.run(["git", "-C", worktree, "merge-base", "--is-ancestor", target, "HEAD"]).returncode != 0:
		raise NeedsMergeRun(f"the branch doesn't contain {target}: a merge run is needed first")
	dirty = leftovers(worktree)
	if dirty:
		raise Refused("the worktree isn't clean: " + "; ".join(dirty[:5]))
	base = git(worktree, "merge-base", target, "HEAD")
	changed = [p for p in git(worktree, "diff", "--name-only", f"{base}...HEAD").splitlines() if p]
	held = heldPaths(changed, config)
	if held and not person:
		raise Refused("a person must review these paths: " + ", ".join(held[:10]))
	urls = addedUrls(worktree, base, config)
	if urls and not person:
		raise Refused("new URLs in source code (a new outbound address?): " + "; ".join(urls[:5]))
	return "person" if person else checkApproval(worktree, config, changed)


GATED_LATER = ("verify", "verifier")  # conditions 8 and 9: judged in their place, after the review


def unique(items):
	return list(dict.fromkeys(items))


def runGate(worktree, topic, run, mainDir, person=False):
	"""Conditions 1 and 3's run facts (module doc, "The run's facts"): the gate view of both stories; Refused for
	every block but the real-data steps' and, for the person's approval, a stop after claude's part completed
	(runstate.WRAPUP_STOP). Returns the view."""
	view = runstate.gateView(mainDir, topic, run, root=worktree)
	passed = GATED_LATER + ((runstate.WRAPUP_STOP,) if person else ())
	early = unique(b["reason"] for b in view["blocks"] if b["fact"] not in passed)
	if early:
		raise Refused("the run's state doesn't allow a merge: " + "; ".join(early[:4]))
	return view


def stepBlocks(view, name):
	"""{story: outcome} of the step's blocks in the gate view (an outcome None: the step isn't done)."""
	return {b["story"]: b.get("outcome") for b in view["blocks"] if b["fact"] == name}


def bothFailed(outcomes):
	return set(outcomes) == set(runstate.STORIES) and set(outcomes.values()) == {"FAIL"}


def verifyFound(worktree, run):
	"""runstate.verifyDetail of run, {} when there is none or its checks aren't the shape verify.py writes."""
	try:
		return runstate.verifyDetail(runstate.runsDir(worktree), run) or {}
	except (AttributeError, TypeError):
		return {}


def failedChecks(worktree, run):
	"""verify.py's failed checks of run as "name: why", [] unless the result is a plain failure in the shape
	verify.py writes (not passed, no error, every failed check with a text name and why)."""
	found = verifyFound(worktree, run)
	if not found or found.get("passed") or found.get("error"):
		return []
	items = found.get("failed") or []
	if not all(isinstance(i["name"], str) and isinstance(i["why"], str) for i in items):
		return []
	return [i["name"] + ": " + i["why"] for i in items]


def checkVerifiedFrom(view, worktree, run):
	"""Condition 8 from the gate view (module doc, "The run's facts")."""
	outcomes = stepBlocks(view, "verify")
	if not outcomes:
		return
	reasons = "; ".join(unique(b["reason"] for b in view["blocks"] if b["fact"] == "verify"))
	if bothFailed(outcomes) and isinstance(view["spec"], dict):
		failed = failedChecks(worktree, run)
		if failed:
			raise NeedsFixRun("real-data checks failed: " + "; ".join(failed[:3]))
		raise Refused("real-data checks didn't pass and name no failed check")
	if "ERROR" in outcomes.values():
		raise Refused("real-data checks could not run: " + str(verifyFound(worktree, run).get("error") or reasons))
	raise Refused(f"real-data checks are planned (plans/verify) but verify.py left no result for this run that "
		f"both the record and today's files hold as passed ({reasons})")


def checkVerifierFrom(view, worktree, run):
	"""Condition 9 from the gate view (module doc, "The run's facts")."""
	outcomes = stepBlocks(view, "verifier")
	if not outcomes:
		return
	reasons = "; ".join(unique(b["reason"] for b in view["blocks"] if b["fact"] == "verifier"))
	spec = view["spec"]
	found = runstate.verifierDetail(runstate.runsDir(worktree), run) or {}
	if bothFailed(outcomes) and isinstance(spec, dict) and spec.get("verifier") and found.get("verdict") == "failed":
		failing = "; ".join(found.get("failed")[:3]) or f"a failing check (verifier-{run}.json)"
		raise NeedsFixRun(("the verifier found: " + failing)[:600])
	if "ERROR" in outcomes.values():
		raise Refused("the verifier could not judge: " + str(found.get("why") or found.get("verdict") or reasons))
	raise Refused(f"a verifier is planned (plans/verify verifier) but verifier.py left no result for this run that "
		f"both the record and today's files hold as passed ({reasons})")


def checkRun(worktree, topic, run, mainDir, person=False):
	"""Every condition (module doc), the run's facts from runstate.gateView (migration step 5); the rest as before."""
	if homeconfig.flowMode(worktree) != "local":
		raise Refused("not in local mode (the topic's flow mode, homeconfig.flowMode)")
	view = runGate(worktree, topic, run, mainDir, person)
	config, target = repoGate(worktree, mainDir)
	review = branchGate(worktree, mainDir, target, view["record"]["main_head_at_start"], config, person)
	checkVerifiedFrom(view, worktree, run)
	checkVerifierFrom(view, worktree, run)
	checkSuites(worktree, config)
	return target, review


def checkSuites(worktree, config):
	"""Module doc, 7: every language's `test`, then every language's "checks", in the sandbox."""
	languages = sorted((config.get("languages") or {}).items())
	try:
		checks = {lang: qg_common.checkCommands(lang, langConfig) for lang, langConfig in languages}
	except ValueError as e:
		raise Refused(f"quality-gates.json has a malformed check: {e}")
	mapping = {"repo": worktree, "root": worktree}
	timeout = homeconfig.load("quality")["coverage_timeout_seconds"]

	def run(lang, langConfig, argv, key):
		try:
			return qg_common.runCommand(qg_common.expand(argv, mapping), os.path.join(worktree, langConfig.get("cwd", "")),
				qg_common.commandEnv(langConfig, mapping), timeout, writable=[worktree], protect=config.get("protect") or [])
		except qg_common.NotStarted as error:
			raise Refused(f"the {lang} {key} command didn't start: {error}; {qg_common.commandHint(config, lang, key)}")

	for lang, langConfig in languages:
		code, _, tail = run(lang, langConfig, langConfig["test"], "test")
		if code != 0:
			raise Refused(f"the {lang} suite fails on the branch (exit {code}): " + qg_common.lastLines(tail, 3))
	for lang, langConfig in languages:
		for argv, emptyOutput in checks[lang]:
			code, _, tail = run(lang, langConfig, argv, "checks")
			name = " ".join(argv)[:120]
			if code != 0:
				raise Refused(f"the {lang} check `{name}` fails on the branch (exit {code}): " + qg_common.lastLines(tail, 3))
			if emptyOutput and tail.strip():
				raise Refused(f"the {lang} check `{name}` must print nothing but printed: " + qg_common.lastLines(tail, 3))


def subjectOf(branch, person):
	"""The first line of the merge commit's message."""
	how = "approved by the person" if person else "automatic"
	return f"Merge branch '{branch}' ({how}, local mode)"


def intentKey(topic, run):
	return f"{topic}-{run}"


def writeIntent(mainDir, topic, run, intent):
	"""The merge intent (module doc), written right before `git merge`."""
	statefile.write(mainDir, INTENT, {"schema": statefile.kind(INTENT)["schema"], "topic": topic, "run": run,
		**intent, "written_at": clock.isoNow()}, intentKey(topic, run))


def recordMerge(mainDir, topic, branch, commit, by):
	"""One merges.jsonl line: the automatic merges main holds (movedOnlyByAutoMerges, alreadyMerged)."""
	stateDir = os.path.join(mainDir, ".hook-only", "auto-merge")
	os.makedirs(stateDir, exist_ok=True)
	with open(os.path.join(stateDir, "merges.jsonl"), "a", encoding="utf-8") as f:
		f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "topic": topic, "branch": branch,
			"commit": commit, "by": by}) + "\n")


def mergeTree(repo, base, tip):
	"""The tree git's merge of base and tip makes (git merge-tree --write-tree), None when it conflicts or fails."""
	result = subprocess.run(["git", "-C", repo, "merge-tree", "--write-tree", base, tip], capture_output=True, text=True)
	tree = (result.stdout.splitlines() or [""])[0].strip()
	return tree if result.returncode == 0 and tree else None


def proofProblems(worktree, mainDir, head, intent, started, person):
	"""Every way head fails to be this intent's merge (module doc, merge seam); [] when it is proven."""
	problems = []
	base, tip = intent.get("base"), intent.get("tip")
	if not person and base != started:
		problems.append(f"the intent's base {str(base)[:10]} is not where the run started ({str(started)[:10]})")
	parents = git(mainDir, "rev-list", "--parents", "-n", "1", head).split()[1:]
	if parents != [base, tip]:
		problems.append(f"{head[:10]}'s parents are not exactly the intent's base and tip (after the merge, main "
			"gained another commit, or the merge is someone else's)")
	if git(worktree, "rev-parse", "-q", "--verify", "HEAD", check=False) != tip:
		problems.append("the branch moved since the intent")
	if git(mainDir, "log", "-1", "--format=%s", head) != intent.get("subject"):
		problems.append(f"{head[:10]}'s subject is not the automatic merge's")
	expected = mergeTree(mainDir, base, tip) if base and tip else None
	if expected is None or git(mainDir, "rev-parse", f"{head}^{{tree}}") != expected:
		problems.append(f"{head[:10]}'s content is not what merging the intent's base and tip makes")
	return problems


def provenMerge(worktree, topic, run, mainDir, person):
	"""(commit, intent) when the merge seam is proven, None when there is nothing to prove (no intent, or the
	integration branch still at its base); raises Unproven with every failed condition (module doc)."""
	intent = statefile.read(mainDir, INTENT, intentKey(topic, run))
	if not intent:
		return None
	target = homeconfig.integrationBranch(worktree, "local")
	head = git(mainDir, "rev-parse", "-q", "--verify", target, check=False) if target else ""
	if not head or head == intent.get("base"):
		return None
	record, _problem = runstate.readRecord(worktree, run)
	problems = proofProblems(worktree, mainDir, head, intent, (record or {}).get("main_head_at_start"), person)
	if problems:
		raise Unproven(f"{target} moved past this run's merge intent and the merge can't be proven: "
			+ "; ".join(problems) + f"; check {target} (`git log -3 {target}`), then `wt merge {topic}`")
	return head, intent


def merge(worktree, topic, run, mainDir, target, review, person=False):
	branch = git(worktree, "symbolic-ref", "--short", "HEAD")
	try:
		with open(os.path.join(worktree, "PR_DESCRIPTION.md"), encoding="utf-8") as f:
			body = f.read().strip()
	except OSError:
		body = ""
	subject = subjectOf(branch, person)
	writeIntent(mainDir, topic, run, {"branch": branch, "base": git(mainDir, "rev-parse", target),
		"tip": git(worktree, "rev-parse", "HEAD"), "subject": subject, "review": review,
		"by": "person" if person else "auto-merge"})
	with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
		f.write(f"{subject}\n\n{body}\n")
		message = f.name
	try:
		result = subprocess.run(["git", "-C", mainDir, "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "-F",
			message, branch], capture_output=True, text=True)
		if result.returncode != 0:
			subprocess.run(["git", "-C", mainDir, "merge", "--abort"], capture_output=True)
			raise Refused(f"git merge failed: {(result.stderr or result.stdout).strip()[:300]}")
	finally:
		os.remove(message)
	commit = git(mainDir, "rev-parse", "HEAD")
	recordMerge(mainDir, topic, branch, commit, "person" if person else "auto-merge")
	return commit


# This evaluation's own trace for the crash handler (module doc): the decision file as it was when it began, and
# whether this evaluation wrote its decision.
TRACE = {"seen": None, "writing": False, "wrote": False}


def mainDirOf():
	return os.environ.get("WT_MAIN_DIR") or ""


def decisionPath(worktree, run):
	return os.path.join(runstate.runsDir(worktree), f"merge-{run}.json")


def rawDecision(path):
	try:
		with open(path, "rb") as f:
			return f.read()
	except OSError:
		return None


def writeDecision(worktree, run, decision):
	TRACE["writing"] = True
	with open(decisionPath(worktree, run), "w", encoding="utf-8") as f:
		json.dump({"run": run, **decision}, f)
	TRACE["wrote"] = True
	recordDecision(worktree, run)


def oneLine(text):
	return " ".join(str(text).split())[:300]


def recordCrash(worktree, topic, run, error):
	"""Any exception (module doc): the error decision unless the decision file changed since this evaluation
	began, then a "refused" event; each part tried on its own, so a failing one still leaves the others."""
	reason = oneLine(f"auto-merge.py failed: {type(error).__name__}: {error}")
	mainDir = mainDirOf()
	try:
		lockDir = hookonly.stateDir(mainDir, "auto-merge") if os.path.isdir(mainDir) else None
		if lockDir:
			os.makedirs(lockDir, exist_ok=True)
		with open(os.path.join(lockDir, "lock") if lockDir else os.devnull, "w") as lock:
			if lockDir:
				fcntl.flock(lock, fcntl.LOCK_EX)
			unchanged = rawDecision(decisionPath(worktree, run)) == TRACE["seen"]
			if not TRACE["wrote"] and (unchanged or TRACE["writing"]):
				writeDecision(worktree, run, {"decision": "error", "reason": reason})
			else:
				reason += " (after the decision in merge-" + str(run) + ".json, which stands)"
	except Exception as inner:
		reason += oneLine(f"; the error decision could not be written either ({type(inner).__name__}: {inner})")
	if os.path.isdir(mainDir):
		try:
			wt_events.emit(mainDir, "refused", topic, reason)
		except Exception as inner:
			print(f"auto-merge.py: the refused event could not be written: {inner}", file=sys.stderr)
	print(reason, file=sys.stderr)


def main():
	worktree, topic, run = os.path.abspath(sys.argv[1]), sys.argv[2], int(sys.argv[3])
	person = sys.argv[4:] == ["--person"]
	TRACE["seen"] = rawDecision(decisionPath(worktree, run))
	try:
		decide(worktree, topic, run, person)
	except Exception as error:
		traceback.print_exc()
		recordCrash(worktree, topic, run, error)
		sys.exit(1)
	finally:
		wt_controls.releaseApproval(worktree, run, os.getpid())


def evaluate(worktree, topic, run, mainDir, person, earlier):
	"""The decision for run (under the lock), or None when it is already merged and decided (module doc)."""
	try:
		merged = alreadyMerged(worktree, mainDir) if homeconfig.flowMode(worktree) == "local" else None
		if merged and earlier is not None:
			return None
		if merged:
			dirty = leftovers(worktree)
			if dirty:
				raise Refused("the branch is already merged but the worktree isn't clean: " + "; ".join(dirty[:5]))
			return {"decision": "merged", "into": merged[0], "commit": merged[1], "review": "already-merged"}
		recovered = recoveredMerge(worktree, topic, run, mainDir, person)
		if recovered:
			return recovered
		target, review = checkRun(worktree, topic, run, mainDir, person)
		return {"decision": "merged", "into": target, "commit": merge(worktree, topic, run, mainDir, target, review,
			person), "review": review}
	except NeedsMergeRun as refusal:
		return {"decision": "refused", "reason": str(refusal), "merge_run": True}
	except NeedsFixRun as refusal:
		return {"decision": "refused", "reason": str(refusal), **({} if person else {"fix_run": True})}
	except Refused as refusal:
		return {"decision": "refused", "reason": str(refusal)}
	except qg_common.SandboxError as error:
		return {"decision": "refused", "reason": f"environment problem, not the code: {error}"}


def recoveredMerge(worktree, topic, run, mainDir, person):
	"""The merge seam (module doc): the decision of a proven merge, its merges.jsonl line written first; None when
	there is nothing to prove, or for the person's `wt merge` when the proof fails (the checks then decide)."""
	if homeconfig.flowMode(worktree) != "local":
		return None
	try:
		proven = provenMerge(worktree, topic, run, mainDir, person)
	except Unproven:
		if person:
			return None
		raise
	if not proven:
		return None
	commit, intent = proven
	recordMerge(mainDir, topic, intent["branch"], commit, intent.get("by") or "auto-merge")
	return {"decision": "merged", "into": homeconfig.integrationBranch(worktree, "local"), "commit": commit,
		"review": intent.get("review"), "recovered": True}


def recordDecision(worktree, run):
	"""Dual write of the merge-<run>.json just written as the record's steps.merge (module doc, "The run record")."""
	runstate.dualWrite(worktree, "auto-merge", runstate.recordStepFromFile, run, "merge")


def recordedMerge(worktree, topic, run, mainDir):
	"""The run record's merge decision (runstate.status from the record, migration step 5): its merge step when done,
	None without one. A step run-steps.py marked running (this evaluation, migration step 6) is no decision."""
	view = runstate.status(mainDir, topic, root=worktree, run=run)
	if view["source"] != "record":
		return None
	step = (view["steps"] or {}).get("merge")
	return step if (step or {}).get("state") == "done" else None


def decide(worktree, topic, run, person):
	decisionFile = decisionPath(worktree, run)
	mainDir = mainDirOf()
	if not os.path.isdir(mainDir):
		writeDecision(worktree, run, {"decision": "refused", "reason": "no main checkout (WT_MAIN_DIR)"})
		return
	lockDir = os.path.join(mainDir, ".hook-only", "auto-merge")
	os.makedirs(lockDir, exist_ok=True)
	with open(os.path.join(lockDir, "lock"), "w") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		TRACE["seen"] = rawDecision(decisionFile)
		earlier = readDecision(decisionFile)
		recorded = recordedMerge(worktree, topic, run, mainDir)
		if (earlier or {}).get("decision") == "merged" or (recorded or {}).get("outcome") in runstate.MERGED:
			return  # a duplicate evaluation: the one before it merged (module doc, already merged)
		if earlier is None and recorded is not None:
			earlier = {}  # the record holds a decision: never overwritten by an already-merged run
		decision = evaluate(worktree, topic, run, mainDir, person, earlier)
		if decision is None:
			return
		writeDecision(worktree, run, decision)
	if decision["decision"] == "merged":
		how = {"docs-only": " (docs only, no code review)",
			"already-merged": " (already in it: nothing new to merge)"}.get(decision.get("review"), "")
		if decision.get("recovered"):
			how += " (recorded after a crash: the merge commit was proven to be this run's)"
		wt_events.emit(mainDir, "merged", topic, f"into {decision['into']} as {decision['commit'][:10]}" + how
			+ (" (approved by the person)" if person else ""))
	elif decision.get("merge_run"):
		wt_events.emit(mainDir, "retry", topic, decision["reason"] + " (a merge run starts by itself)")
	elif decision.get("fix_run"):
		wt_events.emit(mainDir, "retry", topic, decision["reason"] + " (a fix run starts by itself)")
	else:
		wt_events.emit(mainDir, "refused", topic, decision["reason"])


if __name__ == "__main__":
	if len(sys.argv) not in (4, 5) or sys.argv[4:] not in ([], ["--person"]):
		sys.exit("usage: auto-merge.py <worktree> <topic> <run> [--person]")
	main()

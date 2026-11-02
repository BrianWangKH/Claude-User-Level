#!/usr/bin/env python3
"""Local mode: merge a finished worktree branch into the local default branch without a person.

Usage: auto-merge.py <worktree> <topic> <run> [--person]   (run-worker.sh, after a run that ended `STATUS: done`)
--person (worker-control.py `wt merge <topic>`, the person's press after reading a refusal): the person is the
review, so the three checks that ask for one are skipped (5: held paths and new URLs; 6: the code
reviewer's APPROVE) and so is 3's "main moved during the run" (the person merges now; 4 still needs the
branch to contain main, else a merge run as below). Every other check stands (STATUS, ALERT/HOLD, clean
worktree, tests and checks, real-data checks). The decision records "review": "person". Each decision is also an event
(wt_events.py: merged, refused).
Only with FLOW_INTEGRATION=local (the repo's .claude/settings.json env, which agents can't change); the
main checkout comes from WT_MAIN_DIR (worker-control.py). Runs outside Claude Code's sandbox; the tests it
runs go through qg_common.runCommand (bwrap). The decision goes to .hook-only/runs/merge-<run>.json
({"decision": "merged", "commit"} or {"decision": "refused", "reason"}), read by run-result.py.

A person only approves plans; this replaces the PR click, so every condition is something the hooks
recorded or git shows, never the orchestrator's report:
  1. the run ended `STATUS: done`; no agent-guard ALERT or HOLD is pending
  2. the repo has quality gates (.claude/quality-gates.json at the base): they define the test commands
  3. the main checkout is on the integration branch (qg_common.integrationRef), and that branch is where
     the run found it (runs/main-<run>, written by run-worker.sh at the start). When it moved only by
     earlier automatic merges (merges.jsonl), the answer is "a merge run is needed"; anything else moved
     it, maybe an agent, so a person looks
  4. the branch already contains the integration branch (otherwise a merge run is needed first); the
     worktree is clean apart from PR_DESCRIPTION*.md and sandbox placeholders
  5. no path a person must see: the home list in auto-merge-hold.json next to this file (.claude/, CI,
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
     (e.g. `gofmt -l`). A malformed "checks" refuses. Without "checks" only `test` runs
  8. when the planning session wrote real-data checks (<main>/plans/verify/<topic>.json), verify.py's
     result for this run (runs/verify-<run>.json) says "passed"
  9. when that spec has a "verifier" section, verifier.py's result for this run (runs/verifier-<run>.json)
     says "passed": the verifier agent found every rule of the approved plan and every judge item holding
     on the real data
Then `git merge --no-ff` in the main checkout (the repo's git hooks off), the message from
PR_DESCRIPTION.md. A bad merge is one `git revert -m 1 <commit>` away. After a merge, auto-finish.py is
started detached: once the run's wrapper is gone it archives the worktree's records, imports the cost,
queues the friction for the next planning session and removes the worktree.
When the only obstacle is that the branch lacks the latest main (3: moved only by automatic merges, or 4:
not contained), the refusal says so ("merge_run": true) and worker-control.py auto-merge resumes the
worker in merge mode by itself (feature-flow merge.md: main merged into the branch, conflicts resolved by
the worker, a hand-resolved hunk merge-reviewed), within worker_auto_merge_max / _minutes / _usd
(budget-defaults.json); that run's `STATUS: done` comes back here. Likewise when the real data shows the branch
wrong (8: a check failed; 9: the verifier's verdict is "fail"; not an error of the spec, the environment or
the verifier itself), the refusal says "fix_run": true and worker-control.py auto-fix resumes the worker with
the findings, within worker_auto_fix_max; never on the person's `wt merge`, who then decides. Those two
refusals are events of kind "retry" (the flow handles them); every other refusal waits for a person. Checks, merge
and the decision file are written under an exclusive flock on <main>/.hook-only/auto-merge/lock: two workers
finishing together can't both pass the "main unchanged" check and stack an untested combination, and an
evaluation waiting for the lock sees the decision of the one before it.

Already merged, checked first under the lock, before any condition above: a duplicate evaluation must not
undo a merge (2026-11-02: a second `wt merge` waited for the lock, found main holding the first one's merge
commit, judged "the branch doesn't contain main", overwrote merge-<run>.json with "merge_run", so the finish
failed and merge runs with nothing to merge looped). The run is already merged when its merge-<run>.json
says merged, or when the branch tip is an ancestor of the integration branch and merges.jsonl records this
branch with a commit that is in it (a branch just created from main has no record, and goes through the
checks as before). Then:
  - merge-<run>.json exists (any decision): nothing is rewritten, no event, nothing started
  - it doesn't (a later run of a merged branch, e.g. a merge run that found nothing to merge): the worktree
    must be clean (else refused, waiting for a person); the decision is {"decision": "merged", "commit":
    the recorded merge commit, "review": "already-merged"}, an event "merged" says so, and auto-finish
    starts; never a merge run
A `wt merge` approval record (runs/approve-<run>.json, worker-control.py) is removed when this ends, whatever
the outcome (wt_controls.releaseApproval).
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qg_common  # noqa: E402
import wt_controls  # noqa: E402
import wt_events  # noqa: E402

HOLD_FILE = os.path.join(HERE, "auto-merge-hold.json")
URL = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
DOC_EXTENSIONS = (".md", ".txt", ".rst")


class Refused(Exception):
	pass


class NeedsMergeRun(Refused):
	"""The branch lacks the latest main and nothing else is known to be wrong yet: a merge run fixes it."""


class NeedsFixRun(Refused):
	"""The real data shows the branch wrong (a failed check or the verifier's "fail"): the worker fixes it."""


def git(cwd, *args, check=True):
	result = subprocess.run(["git", "-C", cwd, "-c", "core.hooksPath=/dev/null", *args], capture_output=True,
		text=True)
	if check and result.returncode != 0:
		raise Refused(f"git {' '.join(args[:3])} failed: {(result.stderr or result.stdout).strip()[:300]}")
	return result.stdout.strip()


def firstLine(path):
	try:
		with open(path, encoding="utf-8") as f:
			text = str(json.load(f).get("result") or "").strip()
	except (OSError, ValueError):
		return ""
	return (text.splitlines() or [""])[0].strip()


def isSource(path, config):
	return bool(qg_common.languageOf(path, config)) and not qg_common.isExcluded(path, config["exclude"])


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
	patterns = list(qg_common.loadJson(HOLD_FILE).get("paths") or []) + list(config.get("auto_merge_hold") or [])
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
		source = [p for p in ownChanges(worktree, commit) if isSource(p, config)]
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


def checkVerified(worktree, topic, run, mainDir):
	"""Condition 8: a real-data check spec means a passed verify result for this run."""
	if not os.path.isfile(os.path.join(mainDir, "plans", "verify", f"{topic}.json")):
		return
	try:
		with open(os.path.join(worktree, ".hook-only", "runs", f"verify-{run}.json"), encoding="utf-8") as f:
			result = json.load(f)
	except (OSError, ValueError):
		raise Refused("real-data checks are planned (plans/verify) but verify.py left no result for this run")
	if result.get("passed"):
		return
	if result.get("error"):
		raise Refused("real-data checks could not run: " + str(result["error"]))
	failed = [c["name"] + ": " + c["why"] for c in result.get("checks") or [] if not c.get("ok")]
	if not failed:
		raise Refused("real-data checks didn't pass and name no failed check")
	raise NeedsFixRun("real-data checks failed: " + "; ".join(failed[:3]))


def checkVerifier(worktree, topic, run, mainDir):
	"""Condition 9: a spec with a "verifier" section means a passed verifier result for this run."""
	try:
		spec = qg_common.loadJson(os.path.join(mainDir, "plans", "verify", f"{topic}.json"))
	except (OSError, ValueError):
		return
	if not spec.get("verifier"):
		return
	try:
		with open(os.path.join(worktree, ".hook-only", "runs", f"verifier-{run}.json"), encoding="utf-8") as f:
			result = json.load(f)
	except (OSError, ValueError):
		raise Refused("a verifier is planned (plans/verify verifier) but verifier.py left no result for this run")
	verdict = result.get("verdict")
	if verdict == "passed":
		return
	if verdict == "failed":
		failing = [l.strip() for l in str(result.get("report") or "").splitlines() if re.match(r"^\s*V\d+\s+fail", l)]
		found = "; ".join(failing[:3]) or f"a failing check (verifier-{run}.json)"
		raise NeedsFixRun(("the verifier found: " + found)[:600])
	raise Refused("the verifier could not judge: " + str(result.get("why") or verdict))


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
	target = qg_common.integrationRef(mainDir)
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


def check(worktree, topic, run, mainDir, person=False):
	runs = os.path.join(worktree, ".hook-only", "runs")
	if not qg_common.localMode():
		raise Refused("not in local mode (FLOW_INTEGRATION=local)")
	if not firstLine(os.path.join(runs, f"result-{run}.json")).startswith("STATUS: done"):
		raise Refused("the run didn't end with STATUS: done")
	guard = os.path.join(worktree, ".hook-only", "agent-guard")
	if os.path.exists(os.path.join(guard, "ALERT")) or os.path.exists(os.path.join(guard, "HOLD")):
		raise Refused("an agent-guard ALERT or HOLD is pending")
	config, _ = qg_common.loadConfig(worktree)
	if config is None:
		raise Refused("no .claude/quality-gates.json at the base: no test commands to trust")
	target = qg_common.integrationRef(mainDir)
	if target is None:
		raise Refused("no local main or master")
	if git(mainDir, "symbolic-ref", "-q", "HEAD", check=False) != f"refs/heads/{target}":
		raise Refused(f"the main checkout is not on {target}")
	try:
		with open(os.path.join(runs, f"main-{run}"), encoding="utf-8") as f:
			started = f.read().strip()
	except OSError:
		raise Refused(f"no record of where {target} was when the run started (runs/main-{run})")
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
	review = "person" if person else checkApproval(worktree, config, changed)
	checkVerified(worktree, topic, run, mainDir)
	checkVerifier(worktree, topic, run, mainDir)
	checkSuites(worktree, config)
	return target, review


def lastLines(text):
	return " | ".join(text.strip().splitlines()[-3:])[:300]


def checkSuites(worktree, config):
	"""Module doc, 7: every language's `test`, then every language's "checks", in the sandbox."""
	languages = sorted((config.get("languages") or {}).items())
	try:
		checks = {lang: qg_common.checkCommands(lang, langConfig) for lang, langConfig in languages}
	except ValueError as e:
		raise Refused(f"quality-gates.json has a malformed check: {e}")
	mapping = {"repo": worktree, "root": worktree}
	timeout = qg_common.loadJson(qg_common.DEFAULTS_FILE)["coverage_timeout_seconds"]

	def run(langConfig, argv):
		return qg_common.runCommand(qg_common.expand(argv, mapping), os.path.join(worktree, langConfig.get("cwd", "")),
			qg_common.commandEnv(langConfig, mapping), timeout, writable=[worktree], protect=config.get("protect") or [])

	for lang, langConfig in languages:
		code, _, tail = run(langConfig, langConfig["test"])
		if code != 0:
			raise Refused(f"the {lang} suite fails on the branch (exit {code}): " + lastLines(tail))
	for lang, langConfig in languages:
		for argv, emptyOutput in checks[lang]:
			code, _, tail = run(langConfig, argv)
			name = " ".join(argv)[:120]
			if code != 0:
				raise Refused(f"the {lang} check `{name}` fails on the branch (exit {code}): " + lastLines(tail))
			if emptyOutput and tail.strip():
				raise Refused(f"the {lang} check `{name}` must print nothing but printed: " + lastLines(tail))


def merge(worktree, topic, mainDir, target, person=False):
	branch = git(worktree, "symbolic-ref", "--short", "HEAD")
	try:
		with open(os.path.join(worktree, "PR_DESCRIPTION.md"), encoding="utf-8") as f:
			body = f.read().strip()
	except OSError:
		body = ""
	with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
		how = "approved by the person" if person else "automatic"
		f.write(f"Merge branch '{branch}' ({how}, local mode)\n\n{body}\n")
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
	stateDir = os.path.join(mainDir, ".hook-only", "auto-merge")
	os.makedirs(stateDir, exist_ok=True)
	with open(os.path.join(stateDir, "merges.jsonl"), "a", encoding="utf-8") as f:
		f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "topic": topic, "branch": branch,
			"commit": commit, "by": "person" if person else "auto-merge"}) + "\n")
	return commit


def main():
	worktree, topic, run = os.path.abspath(sys.argv[1]), sys.argv[2], int(sys.argv[3])
	person = sys.argv[4:] == ["--person"]
	try:
		decide(worktree, topic, run, person)
	finally:
		wt_controls.releaseApproval(worktree, run, os.getpid())


def evaluate(worktree, topic, run, mainDir, person, earlier):
	"""The decision for run (under the lock), or None when it is already merged and decided (module doc)."""
	try:
		merged = alreadyMerged(worktree, mainDir) if qg_common.localMode() else None
		if merged and earlier is not None:
			return None
		if merged:
			dirty = leftovers(worktree)
			if dirty:
				raise Refused("the branch is already merged but the worktree isn't clean: " + "; ".join(dirty[:5]))
			return {"decision": "merged", "into": merged[0], "commit": merged[1], "review": "already-merged"}
		target, review = check(worktree, topic, run, mainDir, person)
		return {"decision": "merged", "into": target, "commit": merge(worktree, topic, mainDir, target, person),
			"review": review}
	except NeedsMergeRun as refusal:
		return {"decision": "refused", "reason": str(refusal), "merge_run": True}
	except NeedsFixRun as refusal:
		return {"decision": "refused", "reason": str(refusal), **({} if person else {"fix_run": True})}
	except Refused as refusal:
		return {"decision": "refused", "reason": str(refusal)}
	except qg_common.SandboxError as error:
		return {"decision": "refused", "reason": f"environment problem, not the code: {error}"}


def decide(worktree, topic, run, person):
	decisionFile = os.path.join(worktree, ".hook-only", "runs", f"merge-{run}.json")
	mainDir = os.environ.get("WT_MAIN_DIR") or ""
	if not os.path.isdir(mainDir):
		decision = {"decision": "refused", "reason": "no main checkout (WT_MAIN_DIR)"}
		with open(decisionFile, "w", encoding="utf-8") as f:
			json.dump({"run": run, **decision}, f)
		return
	lockDir = os.path.join(mainDir, ".hook-only", "auto-merge")
	os.makedirs(lockDir, exist_ok=True)
	with open(os.path.join(lockDir, "lock"), "w") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		earlier = readDecision(decisionFile)
		if earlier is not None and earlier.get("decision") == "merged":
			return  # a duplicate evaluation: the one before it merged (module doc, already merged)
		decision = evaluate(worktree, topic, run, mainDir, person, earlier)
		if decision is None:
			return
		with open(decisionFile, "w", encoding="utf-8") as f:
			json.dump({"run": run, **decision}, f)
	if decision["decision"] == "merged":
		how = {"docs-only": " (docs only, no code review)",
			"already-merged": " (already in it: nothing new to merge)"}.get(decision.get("review"), "")
		wt_events.emit(mainDir, "merged", topic, f"into {decision['into']} as {decision['commit'][:10]}" + how
			+ (" (approved by the person)" if person else ""))
	elif decision.get("merge_run"):
		wt_events.emit(mainDir, "retry", topic, decision["reason"] + " (a merge run starts by itself)")
	elif decision.get("fix_run"):
		wt_events.emit(mainDir, "retry", topic, decision["reason"] + " (a fix run starts by itself)")
	else:
		wt_events.emit(mainDir, "refused", topic, decision["reason"])
	if decision.get("decision") == "merged":
		subprocess.Popen([sys.executable, os.path.join(HERE, "auto-finish.py"), worktree, topic, str(run)],
			start_new_session=True, env=os.environ.copy(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL)
	if decision.get("merge_run") or decision.get("fix_run"):
		# Outside the lock: worker-control only records its decision and starts a detached waiter.
		subprocess.run([sys.executable, os.path.join(HERE, "worker-control.py"),
			"auto-merge" if decision.get("merge_run") else "auto-fix", worktree, topic, str(run)], env=os.environ.copy())


if __name__ == "__main__":
	if len(sys.argv) not in (4, 5) or sys.argv[4:] not in ([], ["--person"]):
		sys.exit("usage: auto-merge.py <worktree> <topic> <run> [--person]")
	main()

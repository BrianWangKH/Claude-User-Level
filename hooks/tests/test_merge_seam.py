"""auto-merge.py's merge seam (worker self-repair 3b): a crash between `git merge` and its merges.jsonl line leaves
main moved without a record; the next evaluation records it only when it can prove the merge was this run's.

1. Proven: the merge intent written right before the merge (only hooks can write it), the head a merge commit with
   exactly (base, tip) as parents, base where the run started, tip the branch's head, the automatic subject, and the
   tree `git merge-tree` computes: the decision is merged ("recovered"), its merges.jsonl line written once.
2. The dangerous direction, every forgery refused (Unproven, nothing recorded): the content swapped, no intent (then
   nothing is proven at all and the checks decide), the parents wrong, the subject wrong, main moved on after the
   merge, the branch moved, the intent's base not the run's start. The person's `wt merge` with an unproven seam
   falls back to its checks; main still at the intent's base proves nothing.
3. merge() writes the intent before it merges, with the commit's subject.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_merge_seam.py -q -p no:cacheprovider'
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import runstate  # noqa: E402
import statefile  # noqa: E402

TOPIC = "seam"
BRANCH = f"feature/x/{TOPIC}"


def loadAutoMerge():
	spec = importlib.util.spec_from_file_location("auto_merge_seam", os.path.join(HOOKS, "worker", "auto-merge.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


AM = loadAutoMerge()


def git(cwd, *args):
	return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
		capture_output=True, text=True, timeout=60).stdout.strip()


@pytest.fixture
def world(tmp_path):
	"""A main checkout on main, the topic's worktree one commit ahead, its run record started at main's head, the
	topic's flow mode local."""
	main = tmp_path / "main"
	git(tmp_path, "init", "-q", "-b", "main", str(main))
	(main / "a.txt").write_text("a\n")
	git(main, "add", "a.txt")
	git(main, "commit", "-q", "-m", "a")
	wt = tmp_path / "worktrees" / TOPIC
	git(main, "worktree", "add", "-q", "-b", BRANCH, str(wt))
	(wt / "b.txt").write_text("b\n")
	git(wt, "add", "b.txt")
	git(wt, "commit", "-q", "-m", "b")
	main, wt = str(main.resolve()), str(wt.resolve())
	os.makedirs(os.path.join(wt, ".hook-only"), exist_ok=True)
	with open(os.path.join(wt, ".hook-only", "flow-mode"), "w") as f:
		f.write("local\n")
	base = git(main, "rev-parse", "main")
	run = runstate.create(wt, {"topic": TOPIC, "started_by": "person", "purpose": "build", "flow": "written",
		"launch": {"launched_at": "2026-11-05T08:00:00+08:00", "by_pid": 1}})
	runstate.update(wt, run, lambda r: r.update(main_head_at_start=base))
	return {"main": main, "wt": wt, "run": run, "base": base, "tip": git(wt, "rev-parse", "HEAD")}


def intend(world, **change):
	AM.writeIntent(world["main"], TOPIC, world["run"], {"branch": BRANCH, "base": world["base"], "tip": world["tip"],
		"subject": AM.subjectOf(BRANCH, False), "review": "approved", "by": "auto-merge", **change})


def mergeByHand(world, subject=None):
	"""What the crashed auto-merge did: `git merge --no-ff` with the automatic subject; no merges.jsonl line."""
	git(world["main"], "merge", "--no-ff", "-q", "-m", subject or AM.subjectOf(BRANCH, False), BRANCH)
	return git(world["main"], "rev-parse", "HEAD")


def mergesLines(world):
	path = os.path.join(world["main"], ".hook-only", "auto-merge", "merges.jsonl")
	return [json.loads(line) for line in open(path)] if os.path.exists(path) else []


def recover(world, person=False):
	return AM.recoveredMerge(world["wt"], TOPIC, world["run"], world["main"], person)


def test_a_proven_merge_is_recorded_once_as_this_run_s(world):
	intend(world)
	commit = mergeByHand(world)
	decision = recover(world)
	assert decision == {"decision": "merged", "into": "main", "commit": commit, "review": "approved", "recovered": True}
	assert [(m["branch"], m["commit"], m["by"]) for m in mergesLines(world)] == [(BRANCH, commit, "auto-merge")]
	assert AM.alreadyMerged(world["wt"], world["main"]) == ("main", commit)  # a later evaluation: already merged


def amendContent(world):
	(open(os.path.join(world["main"], "evil.txt"), "w")).write("x\n")
	git(world["main"], "add", "evil.txt")
	git(world["main"], "commit", "-q", "--amend", "--no-edit")


def otherParents(world):
	git(world["main"], "reset", "-q", "--hard", world["base"])
	(open(os.path.join(world["main"], "c.txt"), "w")).write("c\n")
	git(world["main"], "add", "c.txt")
	git(world["main"], "commit", "-q", "-m", "c")
	git(world["main"], "merge", "--no-ff", "-q", "-m", AM.subjectOf(BRANCH, False), BRANCH)


def moreAfter(world):
	git(world["main"], "commit", "-q", "--allow-empty", "-m", "later")


def branchMoved(world):
	git(world["wt"], "commit", "-q", "--allow-empty", "-m", "later on the branch")


FORGERIES = {
	"content swapped": (lambda w: None, amendContent, "content is not what merging"),
	"parents wrong": (lambda w: None, otherParents, "parents are not exactly"),
	"main moved on": (lambda w: None, moreAfter, "parents are not exactly"),
	"branch moved": (lambda w: None, branchMoved, "the branch moved"),
	"subject wrong": ("subject", None, "subject is not the automatic merge's"),
	"base not the run's start": ("base", None, "is not where the run started"),
}


@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_a_forged_or_unprovable_merge_is_refused_and_never_recorded(world, name):
	before, after, why = FORGERIES[name]
	if before == "subject":
		intend(world)
		mergeByHand(world, subject="Merge branch 'feature/x/seam' (by hand)")
	elif before == "base":
		moreAfter(world)  # main moved before the intent: the intent's base is not where the run started
		intend(world, base=git(world["main"], "rev-parse", "HEAD"))
		mergeByHand(world)
	else:
		intend(world)
		mergeByHand(world)
		after(world)
	with pytest.raises(AM.Unproven) as refused:
		recover(world)
	assert why in str(refused.value) and f"wt merge {TOPIC}" in str(refused.value)
	assert mergesLines(world) == []


def test_without_an_intent_nothing_is_proven(world):
	mergeByHand(world)
	assert recover(world) is None and mergesLines(world) == []


def test_main_still_at_the_intent_s_base_proves_nothing(world):
	intend(world)
	assert recover(world) is None and mergesLines(world) == []


def test_the_person_s_merge_with_an_unproven_seam_falls_back_to_its_checks(world):
	intend(world)
	mergeByHand(world)
	amendContent(world)
	assert recover(world, person=True) is None and mergesLines(world) == []


def test_a_proof_also_holds_for_the_person_s_merge_from_a_moved_main(world):
	"""The person's merge may start where main moved since the run began (wt merge skips that check): its intent's
	base is main at the merge, and the run's start isn't asked."""
	moreAfter(world)
	base = git(world["main"], "rev-parse", "HEAD")
	intend(world, base=base, subject=AM.subjectOf(BRANCH, True), by="person", review="person")
	commit = mergeByHand(world, subject=AM.subjectOf(BRANCH, True))
	assert recover(world, person=True)["commit"] == commit and mergesLines(world)[0]["by"] == "person"


def test_merge_writes_the_intent_before_it_merges(world, monkeypatch):
	seen = []
	real = AM.writeIntent

	def spy(mainDir, topic, run, intent):
		seen.append(git(world["main"], "rev-parse", "main"))
		real(mainDir, topic, run, intent)

	monkeypatch.setattr(AM, "writeIntent", spy)
	commit = AM.merge(world["wt"], TOPIC, world["run"], world["main"], "main", "approved")
	intent = statefile.read(world["main"], AM.INTENT, AM.intentKey(TOPIC, world["run"]))
	assert seen == [world["base"]] and (intent["base"], intent["tip"]) == (world["base"], world["tip"])
	assert git(world["main"], "log", "-1", "--format=%s", commit) == intent["subject"]
	assert AM.proofProblems(world["wt"], world["main"], commit, intent, world["base"], False) == []

#!/usr/bin/env python3
"""Local mode: the mechanical half of /finish-worktree, right after auto-merge.py merged a branch.

Usage: auto-finish.py <worktree> <topic> <run>   (run-steps.py's finish step starts it detached after a merge)
Waits until run <run> has ended (hooks/lib/runstate.py status: phase ended, its exit written with exit-<run>; a later
run's files alone never end a run of today's files here) and its wrapper is gone (runstate.verifyProcess: the
recorded wrapper pid, command line and start ticks, so a reused pid is not waited on; the planning session's
`run-result.py --wait` and run-worker.sh still use the worktree until then), polling every
status_wait_poll_seconds (budget run_state), then, under the main checkout (WT_MAIN_DIR), outside
Claude Code's sandbox:
  1. archives the worktree's .hook-only/ (runs, quality, budget, cost, reviews), plans/<topic>.md,
     plans/tests/<topic>.md and PR_DESCRIPTION*.md to <main>/.hook-only/archive/<topic>/, the same layout,
     so run-result.py and cost-history.py read the archive as the checkout once the worktree is gone; the
     approved plan (<main>/.hook-only/approved/<topic>.md, worker-control.py) moves in as approved.md
  2. `cost-history.py import <archive> <date> <topic> <branch> <class>` (class from
     <main>/plans/worker/<topic>.json); import marks the checkout, so a later /finish-worktree can't import twice
  3. appends the plan's `## Friction` section and the `## Cost` gap reasons to <main>/plans/friction.md
     under a heading for the topic: the judgement half (where each item goes, the incident log) waits for
     the user's next planning session, which reads that file
  4. `git worktree remove --force` (auto-merge.py already found it clean apart from PR_DESCRIPTION and
     sandbox placeholders); the branch stays, as with /finish-worktree (deleting it writes .git/config)
Before step 1 the run must say merged twice (migration step 5 of the run record): merge-<run>.json's decision is
"merged" and the run record's steps.merge (runstate.status from the record) is merged or already-merged; a run
without a usable record, or a record that says otherwise, does nothing (the check step names why).
Run again for the same run (the finish step retried after a failure), every sub-action is done at most once
(archive, import, friction, remove): each one done is recorded at once in the progress file
<main>/.hook-only/finish/<topic>-<run>.json (state kind finish-progress, schema 1: {schema, topic, run, commit,
done: {<sub-action>: <time>}}, outside the archive so re-archiving can't erase it) and skipped next time while the
merge commit is the same (a topic name used again starts afresh), and each also
checks its own result, for a crash between doing it and recording it: the archive keeps an approved.md it already
moved in (put back before the copy is replaced) and cost-history.py's marker (.hook-only/imported, written back
after the copy is replaced), the import counts as done while that marker is there, the friction when
plans/friction.md already holds the topic's heading for this merge commit, the removal when the worktree is gone.
A worktree already gone with the friction or the removal recorded for the merge commit the archive's
merge-<run>.json names is a finish done by an earlier attempt: the same outcome, nothing done again. A progress
file that exists but can't be read is the step "error" and nothing is done (what was done is unknown).
Time limits (worker self-repair 3c): the finish's limit (step_history.stepLimit "finish": from the durations of
earlier finishes in this repository's archive, else step_limit_default_seconds) bounds each program it runs (git,
the cost import, the worktree removal, the queue tick), in its own session: one still running then is ended (its
process group: TERM, KILL after step_kill_grace_seconds) and its sub-action fails ("exceeded <n> min, from
<basis>"), so the finish fails and is retried like any failed one. finish.json records started_at (once the run has
ended, the work's start) and ended_at, the durations later limits come from.
Each step's outcome goes to <main>/.hook-only/archive/<topic>.finish.json (state kind finish-record, schema 1:
{schema, topic, run, steps, started_at, ended_at}, written through statefile); a failed step stops the later ones, so the worktree is
never removed before its records are safe, and an exception anywhere in the steps is the step "error"
("auto-finish.py crashed: ..."), never a silent end. The outcome is an event (wt_events.py: finished, or
finish-failed with the step), and a failure is written back to the run record's finish step (failed, with the
step; run-steps.py had recorded it launched). Then, whatever the outcome, the queue gets a tick (wt_queue.py) so
a finished topic leaves it and the next may start; finish.json is written before the tick (the tick may start a
run, and a tick that hangs still leaves the finish's record) and again with its outcome as steps.queue_tick
(runstate.queueTickOutcome, as run-steps.py records its own tick). A failed tick is the event queue-failed,
naming finish.json. A failure finish.json can't hold (WT_MAIN_DIR missing or not a directory, finish.json itself
unwritable) is a stderr line (run-steps.py appends this program's output to the run's stderr-<run>.log) and the
run record's finish step failed; exit 1.
"""
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
COST_HISTORY = os.path.join(HOME, ".claude", "skills", "repo-plan", "cost-history.py")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import hookonly  # noqa: E402


class TimedOut(Exception):
	"""A program of the finish ran past the finish's limit and was ended (runLimited)."""


def finishLimit(mainDir):
	"""(seconds, basis): the finish's time limit (module doc)."""
	import step_history

	return step_history.stepLimit(mainDir, "finish")


def runLimited(argv, limit, **options):
	"""subprocess.run's result for argv (text output captured) in its own session, ended with its process group
	when it runs past limit = (seconds, basis): TimedOut then."""
	import runstate

	seconds, basis = limit
	child = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
		errors="replace", **options)
	try:
		out, err = child.communicate(timeout=seconds)
	except subprocess.TimeoutExpired:
		for stop, wait in ((signal.SIGTERM, runstate.settings()["step_kill_grace_seconds"]), (signal.SIGKILL, None)):
			with contextlib.suppress(ProcessLookupError, PermissionError):
				os.killpg(child.pid, stop)
			try:
				child.communicate(timeout=wait)
				break
			except subprocess.TimeoutExpired:
				continue
		raise TimedOut(f"{os.path.basename(argv[1] if argv[0] == sys.executable else argv[0])} exceeded "
			f"{seconds / 60:.0f} min, from {basis}; ended") from None
	return subprocess.CompletedProcess(argv, child.returncode, out, err)


def runEnded(mainDir, worktree, topic, run):
	"""Run has ended and its wrapper is gone (module doc), from runstate.status; False while it can't be read. A
	run of today's files only that a later run "superseded" has no end of its own (no exit-<run>): not ended
	here, since removing the worktree needs the run's real end."""
	import runstate

	try:
		status = runstate.status(mainDir, topic, root=worktree, run=run)
	except Exception:  # unreadable now, maybe readable at the next poll: keep waiting
		return False
	ended = status["phase"] == "ended" and (status["outcome"] or {}).get("reason") != "superseded"
	return ended and runstate.verifyProcess(status["process"] or {}, topic, run) is not True


def recordNotMerged(mainDir, worktree, topic, run):
	"""Why the run record doesn't say run was merged (module doc), None when its merge step does."""
	import runstate

	try:
		status = runstate.status(mainDir, topic, root=worktree, run=run)
	except Exception as error:  # any failure to read the state: nothing is removed
		return f"the run state can't be read ({type(error).__name__}: {error})"
	if status["phase"] == "unknown" or status["source"] != "record":
		return f"run {run} has no usable run record ({status.get('error') or 'none'})"
	merge = (status["steps"] or {}).get("merge") or {}
	if merge.get("outcome") not in runstate.MERGED:
		return f"the run record's merge step is {merge.get('outcome')!r}, not merged"
	return None


def waitForRun(mainDir, worktree, topic, run, waitSeconds, pollSeconds):
	"""Poll runEnded every pollSeconds until it holds (True) or waitSeconds pass (False)."""
	deadline = time.time() + waitSeconds
	while time.time() < deadline:
		if runEnded(mainDir, worktree, topic, run):
			return True
		time.sleep(pollSeconds)
	return False


def section(text, heading):
	"""The body of `## <heading>` up to the next `## `, stripped; '' without one."""
	match = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
	return match.group(1).strip() if match else ""


def archive(worktree, topic, dest):
	approved = os.path.join(os.path.dirname(os.path.dirname(dest)), "approved", f"{topic}.md")
	kept = os.path.join(dest, "approved.md")
	if os.path.isfile(kept) and not os.path.exists(approved):
		# An earlier attempt moved the approved plan in already: put it back before the copy is replaced.
		os.makedirs(os.path.dirname(approved), exist_ok=True)
		shutil.move(kept, approved)
	marker = importMarker(dest)
	imported = open(marker, "rb").read() if os.path.isfile(marker) else None
	if os.path.exists(dest):
		shutil.rmtree(dest)
	shutil.copytree(os.path.join(worktree, ".hook-only"), os.path.join(dest, ".hook-only"), symlinks=True)
	if imported is not None:
		# cost-history.py imported this archive already: its marker survives the copy being replaced, or the
		# import would run again and double the rows.
		with open(marker, "wb") as f:
			f.write(imported)
	if os.path.isfile(approved):
		shutil.move(approved, os.path.join(dest, "approved.md"))
	for rel in (f"plans/{topic}.md", f"plans/tests/{topic}.md"):
		src = os.path.join(worktree, rel)
		if os.path.isfile(src):
			os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
			shutil.copy2(src, os.path.join(dest, rel))
	for name in os.listdir(worktree):
		if name.startswith("PR_DESCRIPTION") and name.endswith(".md"):
			shutil.copy2(os.path.join(worktree, name), os.path.join(dest, name))


def importMarker(dest):
	"""cost-history.py's marker that the archive dest was imported (it refuses a second import while it exists)."""
	return hookonly.stateDir(dest, "imported")


def archivedCommit(dest, run):
	"""The merge commit the archive's copy of merge-<run>.json names when it says merged, else None."""
	try:
		with open(os.path.join(hookonly.stateDir(dest, "runs"), f"merge-{run}.json"), encoding="utf-8") as f:
			merged = json.load(f)
	except (OSError, ValueError):
		return None
	if not isinstance(merged, dict) or merged.get("decision") != "merged":
		return None
	return str(merged.get("commit") or "") or None


def frictionHeading(topic, commit):
	"""The start of the topic's heading in plans/friction.md for one merge commit (friction, frictionAppended)."""
	return f"## {topic} (auto-merge {commit[:10]}, "


def olderFrictionHeading(topic, commit):
	"""The same heading as an earlier version wrote it, in Chinese: a friction.md written before the English
	heading still counts for frictionAppended, so a rerun never appends the topic twice."""
	return f"## {topic}（自動 merge {commit[:10]}，"


def frictionAppended(frictionFile, topic, commit):
	"""Whether plans/friction.md holds the topic's heading for this merge commit already (a rerun's check), in
	the current or the older form."""
	headings = (frictionHeading(topic, commit), olderFrictionHeading(topic, commit))
	try:
		with open(frictionFile, encoding="utf-8") as f:
			return any(line.startswith(headings) for line in f)
	except FileNotFoundError:
		return False


def friction(dest, topic, commit, today):
	try:
		with open(os.path.join(dest, "plans", f"{topic}.md"), encoding="utf-8") as f:
			text = f.read()
	except OSError:
		text = ""
	parts = [f"{frictionHeading(topic, commit)}{today}; the planning session decides where each item goes)"]
	items = section(text, "Friction")
	parts.append("### Friction\n\n" + (items or "(the plan has no ## Friction)"))
	cost = section(text, "Cost")
	if cost:
		parts.append("### Cost\n\n" + cost)
	return "\n\n".join(parts) + "\n"


def queueTick(mainDir, limit):
	"""`wt_queue.py tick <main>`: the queue_tick outcome (runstate.queueTickOutcome) with the tick's last stderr
	line, which names the error (an uncaught exception's last traceback line); one past limit is ended."""
	import runstate

	try:
		ticked = runLimited([sys.executable, os.path.join(HERE, "wt_queue.py"), "tick", mainDir], limit)
	except (OSError, TimedOut) as error:  # the tick couldn't start or was ended: recorded like a failed one
		return runstate.queueTickOutcome(None, f"{type(error).__name__}: {error}"[:300])
	lines = ticked.stderr.strip().splitlines()
	return runstate.queueTickOutcome(ticked.returncode, lines[-1][-300:] if lines else "")


FINISH_RECORD = "finish-record"  # <main>/.hook-only/archive/<topic>.finish.json (config/registry/state-registry.json)
SOURCE = "auto-finish"
PROGRESS = "finish-progress"  # <main>/.hook-only/finish/<topic>-<run>.json (config/registry/state-registry.json)
FAILED_KEYS = ("wait", "check", "error", "remove", "import", "timeout")


class ProgressUnreadable(Exception):
	"""The progress file exists but can't be read: what was done is unknown, so nothing is done again."""


class Progress:
	"""The sub-actions of this topic's run already done (module doc), read once, each new one written at once."""

	def __init__(self, mainDir, topic, run):
		import statefile

		self.mainDir, self.key = mainDir, f"{topic}-{run}"
		path = statefile.pathOf(mainDir, PROGRESS, self.key)
		found = statefile.read(mainDir, PROGRESS, self.key)
		if found is None and os.path.lexists(path):
			raise ProgressUnreadable(f"{path} exists but can't be read (or is another schema version)")
		found = found or {}
		if not isinstance(found.get("done") or {}, dict):
			raise ProgressUnreadable(f"{path}: done is not an object")
		self.body = {"schema": statefile.kind(PROGRESS)["schema"], "topic": topic, "run": run,
			"commit": found.get("commit"), "done": dict(found.get("done") or {})}

	def forCommit(self, commit):
		"""Keep what was done only for the same merge commit (a topic name used again starts afresh)."""
		if self.body["commit"] != commit:
			self.body.update(commit=commit, done={})

	def done(self, name):
		return name in self.body["done"]

	def mark(self, name):
		import clock
		import statefile

		self.body["done"][name] = clock.isoNow()
		statefile.write(self.mainDir, PROGRESS, self.body, self.key)


def finishSteps(worktree, topic, run, mainDir, steps, times):
	"""Steps 1-4 (module doc), each outcome into steps; returns at the first one that fails. times gets started_at
	once the run has ended."""
	dest = os.path.join(mainDir, ".hook-only", "archive", topic)
	try:
		waitSeconds = float(homeconfig.get("budget", "auto_finish_wait_seconds"))
		pollSeconds = float(homeconfig.get("budget", "run_state.status_wait_poll_seconds"))
	except (homeconfig.ConfigError, KeyError, OSError, ValueError, TypeError) as error:
		steps["wait"] = f"home settings unreadable ({error}); nothing done"
		return
	try:
		progress = Progress(mainDir, topic, run)
	except ProgressUnreadable as error:
		steps["error"] = f"{error}; nothing done (check it, then run the finish again)"
		return
	finished = progress.done("remove") or progress.done("friction")
	commit = progress.body["commit"]
	if not os.path.isdir(worktree) and finished and commit and commit == archivedCommit(dest, run):
		# Finished by an earlier attempt for this merge commit (the worktree gone, its removal or the sub-action
		# before it recorded): the same outcome as that attempt, nothing done again.
		if not progress.done("remove"):
			progress.mark("remove")
		steps.update(archive=dest, friction=os.path.join(mainDir, "plans", "friction.md"), remove="removed")
		steps["import"] = "imported by an earlier attempt"
		return
	if not waitForRun(mainDir, worktree, topic, run, waitSeconds, pollSeconds):
		steps["wait"] = f"the wrapper of run {run} didn't finish within {waitSeconds:g} s; nothing done"
		return
	try:
		merged = json.load(open(os.path.join(worktree, ".hook-only", "runs", f"merge-{run}.json"), encoding="utf-8"))
		if merged.get("decision") != "merged":
			steps["check"] = "merge-<run>.json doesn't say merged; nothing done"
			return
		notMerged = recordNotMerged(mainDir, worktree, topic, run)
		if notMerged:
			steps["check"] = f"{notMerged}; nothing done"
			return
		import clock

		times["started_at"] = clock.isoNow()
		subActions(worktree, topic, mainDir, dest, str(merged.get("commit", "")), steps, progress, finishLimit(mainDir))
	except TimedOut as error:
		steps["timeout"] = str(error)
	except (OSError, ValueError) as error:
		steps["error"] = f"{type(error).__name__}: {error}"


def subActions(worktree, topic, mainDir, dest, commit, steps, progress, limit):
	"""archive, import, friction, remove (module doc), each at most once across reruns (progress); returns at the
	first one that fails; each program it runs is bounded by limit (runLimited: TimedOut)."""
	progress.forCommit(commit)
	if not progress.done("archive"):
		archive(worktree, topic, dest)
		progress.mark("archive")
	steps["archive"] = dest
	if not progress.done("import") and not os.path.isfile(hookonly.stateDir(dest, "imported")):
		branch = runLimited(["git", "-C", worktree, "symbolic-ref", "--short", "HEAD"], limit).stdout.strip()
		try:
			klass = json.load(open(os.path.join(mainDir, "plans", "worker", f"{topic}.json"), encoding="utf-8"))["class"]
		except (OSError, ValueError, KeyError):
			klass = "unknown"
		imported = runLimited([sys.executable, COST_HISTORY, "import", dest, date.today().isoformat(), topic,
			branch, klass], limit, cwd=mainDir)
		steps["import"] = (imported.stdout + imported.stderr).strip()[-500:]
		if imported.returncode != 0:
			return
	elif "import" not in steps:
		steps["import"] = "imported by an earlier attempt"
	if not progress.done("import"):
		progress.mark("import")
	frictionFile = os.path.join(mainDir, "plans", "friction.md")
	if not progress.done("friction") and not frictionAppended(frictionFile, topic, commit):
		os.makedirs(os.path.dirname(frictionFile), exist_ok=True)
		with open(frictionFile, "a", encoding="utf-8") as f:
			f.write("\n" + friction(dest, topic, commit, date.today().isoformat()))
	if not progress.done("friction"):
		progress.mark("friction")
	steps["friction"] = frictionFile
	if os.path.isdir(worktree):
		removed = runLimited(["git", "-C", mainDir, "worktree", "remove", "--force", worktree], limit)
		if removed.returncode != 0:
			steps["remove"] = (removed.stderr.strip() or "failed")[-300:]
			return
	progress.mark("remove")
	steps["remove"] = "removed"


def failureOf(steps):
	"""The first failed step as '<step>: <why>', '' when the finish removed the worktree."""
	if steps.get("remove") == "removed":
		return ""
	return next((f"{k}: {v}" for k, v in steps.items() if k in FAILED_KEYS), "") or "no step recorded an outcome"


def writeRecord(mainDir, topic, run, steps, times):
	"""finish.json (state kind finish-record, schema 1: {schema, topic, run, steps, started_at, ended_at}), replaced
	on every write; the times only once known."""
	import statefile

	statefile.write(mainDir, FINISH_RECORD, {"schema": statefile.kind(FINISH_RECORD)["schema"], "topic": topic,
		"run": run, "steps": steps, **times}, topic)


def failBack(worktree, run, reason):
	"""A finish that failed is written back to the run record's finish step (failed, the reason), which run-steps.py
	recorded launched when it started this program; a worktree already gone has no record to write."""
	import runstate

	if os.path.isdir(worktree):
		runstate.dualWrite(worktree, SOURCE, runstate.recordStep, run, "finish",
			{"state": "failed", "reason": f"auto-finish.py: {reason}"[:300]})


def report(worktree, topic, run, mainDir, steps, times):
	"""finish.json, the finish's event, a failure written back to the run record, then the queue tick (module doc)."""
	import clock
	import wt_events

	if "started_at" in times:
		times["ended_at"] = clock.isoNow()
	writeRecord(mainDir, topic, run, steps, times)
	failed = failureOf(steps)
	if failed:
		wt_events.emit(mainDir, "finish-failed", topic, f"{failed[:200]} (finish.json)")
		failBack(worktree, run, f"{failed} (finish.json)")
	else:
		wt_events.emit(mainDir, "finished", topic, "archived, cost imported, friction queued, worktree removed")
	try:
		limit = finishLimit(mainDir)
	except Exception as error:  # the history can't be read: the tick still runs, under the default of the setting
		import runstate

		limit = (runstate.settings()["step_limit_default_seconds"]["finish"], f"the default ({error})")
	steps["queue_tick"] = queueTick(mainDir, limit)
	writeRecord(mainDir, topic, run, steps, times)
	if steps["queue_tick"]["outcome"] != "ticked":
		wt_events.emit(mainDir, "queue-failed", topic,
			f"queue tick after auto-finish: {steps['queue_tick']['reason'][:200]} (finish.json)")


def finish(worktree, topic, run, mainDir):
	"""finishSteps then report; any exception in the steps is the finish's error, never a silent end."""
	steps, times = {}, {}
	try:
		finishSteps(worktree, topic, run, mainDir, steps, times)
	except Exception as error:
		steps["error"] = f"auto-finish.py crashed: {type(error).__name__}: {error}"[:300]
	report(worktree, topic, run, mainDir, steps, times)


def main():
	"""The finish; whatever fails where finish.json or the events can't say it (no main checkout, report itself
	failing) is a stderr line (run-steps.py appends this program's output to the run's stderr log) and the run
	record's finish step failed. Exit 1 then."""
	worktree, topic, run = os.path.abspath(sys.argv[1]), sys.argv[2], None
	mainDir = os.environ.get("WT_MAIN_DIR") or ""
	try:
		run = int(sys.argv[3])
		if not os.path.isdir(mainDir):
			raise ValueError(f"WT_MAIN_DIR {mainDir!r} is not a directory: no main checkout to finish in; nothing done")
		finish(worktree, topic, run, mainDir)
	except Exception as error:
		text = f"{type(error).__name__}: {error}"
		print(f"auto-finish: {topic} run {sys.argv[3]}: {text}", file=sys.stderr)
		if run is not None:  # a run argument that isn't a number names no record to write back to
			failBack(worktree, run, text)
		return 1
	return 0


if __name__ == "__main__":
	if len(sys.argv) != 4:
		sys.exit("usage: auto-finish.py <worktree> <topic> <run>")
	sys.exit(main())

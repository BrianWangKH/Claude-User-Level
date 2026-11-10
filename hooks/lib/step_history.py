"""Wrap-up step time limits from history (worker self-repair stage 3c): how long a step may run before it is ended
(run-steps.py) or told as slow (the merge, which is never ended), from how long the same step took in this
repository's earlier runs, never a fixed time.

limitOf(samples, name, limits) is the rule (pure): the newest step_limit_history_count samples (seconds, newest
first by end time); with fewer than step_limit_min_samples_count of them, the step's default
(step_limit_default_seconds, lenient on purpose: one or two quick runs must not make a later normal one look hung);
otherwise their median x step_limit_factor. No floor: only the sample count guards a short limit. All in budget
run_state (home.json; config/local.json may override them).
Samples (stepSamples): the last attempt of each run's step that ended done, ended_at minus started_at, from the run
records of every linked worktree of the main checkout (`git worktree list`) and of every archived topic
(<main>/.hook-only/archive/<topic>/, a real directory; a symlink there is skipped). The finish step's samples come
from the archive's finish.json files instead (auto-finish.py's own started_at and ended_at, a finish that removed
the worktree), since run-steps.py only starts auto-finish.py detached. A record or file that can't be read gives
no sample (the default then covers it: a lenient limit, never a short one).
Reads: run records and finish.json files of the main checkout's worktrees and archive. Writes: nothing.
Fails: open toward lenient — whatever can't be read lowers the sample count, which only ever leads to the default.
"""

import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402

FINISH = "finish"
FINISH_RECORD = "finish-record"


def secondsOf(item):
	"""(ended_at, seconds) of a dict with started_at and ended_at stamps, ended after it started; None otherwise."""
	started, ended = runstate.parseStamp(item.get("started_at")), runstate.parseStamp(item.get("ended_at"))
	if started is None or ended is None or ended < started:
		return None
	return ended, (ended - started).total_seconds()


def limitOf(samples, name, limits):
	"""(seconds, basis): the limit of step name from samples [(ended_at, seconds)] (module doc); basis says which
	rule gave it, for the reason a person reads."""
	newest = [s for _, s in sorted(samples, key=lambda pair: pair[0], reverse=True)][: limits["step_limit_history_count"]]
	if len(newest) < limits["step_limit_min_samples_count"]:
		seconds = limits["step_limit_default_seconds"][name]
		return seconds, f"the default {seconds / 60:.0f} min ({len(newest)} earlier run(s) of {name}, fewer than " \
			f"{limits['step_limit_min_samples_count']})"
	median = statistics.median(newest)
	factor = limits["step_limit_factor"]
	return median * factor, f"{factor:g} x the median {median / 60:.1f} min of the last {len(newest)} {name} step(s)"


def checkoutsOf(mainDir):
	"""The roots whose run records count: every linked worktree (real paths) and every archived topic directory."""
	roots = []
	listing = hookio.git(str(mainDir), "worktree", "list", "--porcelain") or ""
	main = os.path.realpath(str(mainDir))
	for line in listing.splitlines():
		if line.startswith("worktree ") and os.path.realpath(line[len("worktree "):]) != main:
			roots.append(os.path.realpath(line[len("worktree "):]))
	archive = runstate.archiveOf(mainDir, "")
	try:
		names = sorted(os.listdir(archive))
	except OSError:
		names = []
	for name in names:
		path = os.path.join(archive, name)
		if os.path.isdir(path) and not os.path.islink(path):
			roots.append(path)
	return roots


def recordSamples(roots, name):
	"""[(ended_at, seconds)] of step name's done last attempts in every run record under roots."""
	samples = []
	for root in roots:
		records, _legacy = runstate.runNumbers(root)
		for run in sorted(records):
			record, _problem = runstate.readRecord(root, run)
			step = ((record or {}).get("steps") or {}).get(name) or {}
			found = secondsOf(step) if step.get("state") == "done" else None
			if found:
				samples.append(found)
	return samples


def finishSamples(mainDir):
	"""[(ended_at, seconds)] of every archived finish.json whose finish removed the worktree."""
	samples = []
	archive = runstate.archiveOf(mainDir, "")
	try:
		names = sorted(n for n in os.listdir(archive) if n.endswith(".finish.json"))
	except OSError:
		return samples
	for name in names:
		topic = name[: -len(".finish.json")]
		try:
			data = statefile.read(str(mainDir), FINISH_RECORD, topic) or {}
		except (statefile.StateError, OSError, ValueError):
			continue
		found = secondsOf(data) if (data.get("steps") or {}).get("remove") == "removed" else None
		if found:
			samples.append(found)
	return samples


def stepSamples(mainDir, name):
	"""The samples of step name in the main checkout's repository (module doc); [] without a main checkout."""
	if not mainDir or not os.path.isdir(str(mainDir)):
		return []
	if name == FINISH:
		return finishSamples(mainDir)
	return recordSamples(checkoutsOf(mainDir), name)


def stepLimit(mainDir, name, limits=None):
	"""(seconds, basis) for step name now (limitOf over stepSamples)."""
	return limitOf(stepSamples(mainDir, name), name, limits or runstate.settings())

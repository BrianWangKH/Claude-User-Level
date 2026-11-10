"""Old decision rules, frozen: functions removed from hooks/worker/wt_controls.py and hooks/worker/auto-merge.py,
kept here verbatim, as text, only as the baselines tests hold today's rules to:
- WT_CONTROLS: the file rules before run record migration step 4 (isRunning, doneWithoutNewEstimate and its
  helpers firstLine, lastUserEstimate); test_decision_readers.py compares the action they gave with the row
  today's runstate.status gives.
- AUTO_MERGE: the merge gate on today's files before migration step 5 (check, with checkVerified, checkVerifier
  and its own firstLine); test_auto_merge.py runs it next to checkRun over every phase and step outcome and
  requires the new gate never to let through what this one refused.
This file never changes: a rule change is compared against it, never written into it. Test data, never home code:
each source runs in a copy of the namespace of the module under test (rules()), so the old rules call that
module's own helpers (wt_controls' loadJson and Path; auto-merge's Refused classes, repoGate, branchGate,
checkSuites and git, as the test left them) and the tree under test's homeconfig and qg_common.
"""

WT_CONTROLS = r'''
def isRunning(worktree, run):
	"""The run's start file exists and its exit file doesn't (or is empty). Unused since migration step 4
	(replaced by runstate.status "phase"); kept for the archive."""
	if not isinstance(run, int):
		return False
	runs = Path(worktree) / ".hook-only" / "runs"
	if not (runs / f"start-{run}").exists():
		return False
	try:
		return (runs / f"exit-{run}").read_text(encoding="utf-8").strip() == ""
	except OSError:
		return True


def firstLine(path):
	"""Unused since migration step 4 (doneWithoutNewEstimate's helper); kept for the archive."""
	text = str((loadJson(path) or {}).get("result") or "").strip()
	return (text.splitlines() or [""])[0].strip()


def lastUserEstimate(runs, run):
	"""est-<k>.json of the last run the user started (automatic runs write "auto": true), or None. Unused since
	migration step 4 (doneWithoutNewEstimate's helper); kept for the archive."""
	for k in range(run, 0, -1):
		path = Path(runs) / f"est-{k}.json"
		est = loadJson(path)
		if est is not None and not est.get("auto"):
			return path
	return None


def doneWithoutNewEstimate(worktree, run, estimateFile):
	"""The run ended `STATUS: done` and the estimate file is not newer than the est-<k>.json of the last run the
	user started. Unused since migration step 4 (replaced by runstate.status "resumable", code done); kept for
	the archive."""
	runs = Path(worktree) / ".hook-only" / "runs"
	if not isinstance(run, int) or not firstLine(runs / f"result-{run}.json").startswith("STATUS: done"):
		return False
	last = lastUserEstimate(runs, run)
	try:
		return last is not None and Path(estimateFile).stat().st_mtime <= last.stat().st_mtime
	except OSError:
		return False
'''

AUTO_MERGE = r'''
def firstLine(path):
	"""Condition 1 from today's file. Unused since migration step 5 (replaced by runGate: runstate.gateView);
	kept for the archive."""
	try:
		with open(path, encoding="utf-8") as f:
			text = str(json.load(f).get("result") or "").strip()
	except (OSError, ValueError):
		return ""
	return (text.splitlines() or [""])[0].strip()


def checkVerified(worktree, topic, run, mainDir):
	"""Condition 8: a real-data check spec means a passed verify result for this run. Unused since migration step 5
	(replaced by checkVerifiedFrom); kept for the archive."""
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
	"""Condition 9: a spec with a "verifier" section means a passed verifier result for this run. Unused since
	migration step 5 (replaced by checkVerifierFrom); kept for the archive."""
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


def check(worktree, topic, run, mainDir, person=False):
	"""Every condition, the run's facts from today's files. Unused since migration step 5 (replaced by checkRun);
	kept for the archive and as the baseline tests/test_auto_merge.py holds checkRun to. The conditions that read
	no run state are repoGate and branchGate, which checkRun calls the same way."""
	runs = os.path.join(worktree, ".hook-only", "runs")
	if homeconfig.flowMode(worktree) != "local":
		raise Refused("not in local mode (the topic's flow mode, homeconfig.flowMode)")
	if not firstLine(os.path.join(runs, f"result-{run}.json")).startswith("STATUS: done"):
		raise Refused("the run didn't end with STATUS: done")
	guard = os.path.join(worktree, ".hook-only", "agent-guard")
	if os.path.exists(os.path.join(guard, "ALERT")) or os.path.exists(os.path.join(guard, "HOLD")):
		raise Refused("an agent-guard ALERT or HOLD is pending")
	config, target = repoGate(worktree, mainDir)
	try:
		with open(os.path.join(runs, f"main-{run}"), encoding="utf-8") as f:
			started = f.read().strip()
	except OSError:
		raise Refused(f"no record of where {target} was when the run started (runs/main-{run})")
	review = branchGate(worktree, mainDir, target, started, config, person)
	checkVerified(worktree, topic, run, mainDir)
	checkVerifier(worktree, topic, run, mainDir)
	checkSuites(worktree, config)
	return target, review
'''


def rules(source, module):
	"""The functions of source, defined over a copy of module's namespace (taken now: set what the test replaces
	on module first)."""
	namespace = dict(vars(module))
	exec(compile(source, f"<legacy rules over {module.__name__}>", "exec"), namespace)
	return namespace


def wtControls(module):
	"""isRunning, doneWithoutNewEstimate (with firstLine, lastUserEstimate) over module (wt_controls)."""
	return rules(WT_CONTROLS, module)


def autoMerge(module):
	"""check (with checkVerified, checkVerifier, firstLine) over module (auto-merge.py loaded under test)."""
	return rules(AUTO_MERGE, module)

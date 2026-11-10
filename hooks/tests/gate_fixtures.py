"""Shared fixtures of the merge gate tests (test_auto_merge.py, run record migration step 5): a git world (a main
checkout whose last commit is a recorded automatic merge, and a reviewed worktree branch on it) and one run of it,
told both ways the way its writers tell it during the dual write: the record through runstate's writers and
today's runs/ files in the shapes run-worker.sh, worker-control.py, verify.py and verifier.py write
(hooks/RULES.md T4, T9); every fact can be bent on its own (a case), and cases() is the matrix both gates run over.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import clock  # noqa: E402
import qg_common  # noqa: E402
import runstate  # noqa: E402
import statefile  # noqa: E402

TOPIC = "topic"
RUN = 1
GONE_PID = 999999999  # the wrapper of a test run: no such process (the gate makes no pid check)
STALE_SECONDS = 4 * 3600  # older than the heartbeat limit and the legacy wrap-up allowance

BASE = {
	"source": "both",
	"phase": "finishing",
	"status": "STATUS: done\n\nall green",
	"head": "same",
	"spec": None,
	"verify": None,
	"verifier": None,
	"held": None,
	"corrupt": None,
}
SOURCES = ("both", "legacy", "record")
PHASES = (
	"launching",
	"running",
	"finishing",
	"ended",
	"ended-stopped",
	"orphan-stale",
	"orphan-exited",
	"no-claude-end",
)
STATUSES = (
	"STATUS: done\n\nall green",
	"\nSTATUS: done   \nall green",
	"STATUS: DONE\n",
	"STATUS: Done\n",
	"STATUS: needs-decision\n",
	"STATUS: blocked\n",
	"STATUS:done\n",
	"STATUS:  done\n",
	"  STATUS: done\n",
	"STATUS: done-ish\n",
	"STATUS: done and more\n",
	"PHASE: build\nSTATUS: done\n",
	"NOTE: x\nSTATUS: done\n",
	"STATUS: done\nSTATUS: done\n",
	"missing",
	"not-json",
	"not-an-object",
)
# A done STATUS in another case than the old gate's lower case: the one cell class the new gate opens on purpose
# (hooks/RULES.md R2, runstate.workerStatus); test_auto_merge.py holds it to the lower-case cell's old answer.
CASE_ONLY = {"STATUS: DONE\n": "STATUS: done\n\nall green", "STATUS: Done\n": "STATUS: done\n\nall green"}
HEADS = ("same", "auto", "other", "missing", "differs")
VERIFY_FILES = {
	"pass": {"passed": True, "checks": [{"name": "c", "ok": True, "why": ""}]},
	"fail": {"passed": False, "checks": [{"name": "det", "ok": False, "why": "differs between two runs: out.txt"}]},
	"fail-nochecks": {"passed": False, "checks": []},
	"fail-noname": {"passed": False, "checks": [{"ok": False, "why": "x"}]},
	"error": {"passed": False, "checks": [], "error": "data path 'nope' not in data"},
	"truthy": {"passed": 1, "checks": []},
	"malformed": "{half",
}
VERIFIER_FILES = {
	"passed": {"run": RUN, "verdict": "passed", "report": "DONE\nVERDICT: pass\nV1 ok"},
	"failed": {"run": RUN, "verdict": "failed", "report": "DONE\nVERDICT: fail\nV2 fail: rule E2"},
	"error": {"run": RUN, "verdict": "error", "why": "no report"},
	"odd": {"run": RUN, "verdict": "maybe"},
	"malformed": "{half",
}
SPECS = {
	"checks": {"data_paths": [], "checks": [{"name": "c", "run": ["true"]}]},
	"verifier": {"data_paths": [], "checks": [{"name": "c", "run": ["true"]}], "verifier": {"usd": 1, "minutes": 5}},
	"verifier-null": {"data_paths": [], "checks": [], "verifier": None},
	"not-json": "{half",
	"a-list": "[1, 2]",
}
CORRUPTIONS = ("not-json", "schema-2", "no-started-by", "status-enum", "naive-time", "step-outcome")


def git(repo, *args):
	return subprocess.run(
		["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=60
	).stdout.strip()


def write(path, text):
	os.makedirs(os.path.dirname(str(path)), exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		f.write(text if isinstance(text, str) else json.dumps(text))


def makeWorld(root):
	"""{main, wt, heads}: main with commits c0 -> c1 -> c2 (c2 a recorded automatic merge of another topic),
	quality gates that load, plans/ visible; the worktree on feature/x/topic from c2 with one reviewed commit."""
	main = root / "main"
	main.mkdir(parents=True)
	git(main, "init", "-q", "-b", "main")
	git(main, "config", "user.email", "t@t")
	git(main, "config", "user.name", "t")
	write(main / ".gitignore", ".hook-only/\nplans/\n__pycache__/\n")
	write(main / "pkg" / "__init__.py", "")
	write(main / "tests" / "test_calc.py", "def test_calc():\n    assert True\n")
	gates = {"extensions": [".py"], "coverage": ["true"], "coverage_format": "lcov", "test": ["true"]}
	write(main / qg_common.CONFIG_FILE, {"languages": {"python": gates}, "exclude": ["tests/"]})
	heads = []
	for name in ("c0", "c1", "c2"):
		write(main / "pkg" / f"{name}.py", f"X = '{name}'\n")
		git(main, "add", ".")
		git(main, "commit", "-q", "-m", name)
		heads.append(git(main, "rev-parse", "HEAD"))
	record = {"t": "2026-11-04T10:00:00+08:00", "topic": "other", "branch": "feature/x/other", "commit": heads[2]}
	write(main / ".hook-only" / "auto-merge" / "merges.jsonl", json.dumps(record) + "\n")
	write(main / "plans" / "worker" / f"{TOPIC}.json", {"minutes": 20, "est_usd": 2, "class": "logic"})
	wt = root / "worktrees" / TOPIC
	git(main, "worktree", "add", "-q", "-b", f"feature/x/{TOPIC}", str(wt))
	write(wt / "pkg" / "more.py", "def more(x):\n    return x + 1\n")
	git(wt, "add", ".")
	git(wt, "commit", "-q", "-m", "implement more")
	write(wt / "PR_DESCRIPTION.md", "Add more()\n")
	return {"main": main, "wt": wt, "heads": {"same": heads[2], "auto": heads[1], "other": heads[0]}}


def runsOf(world):
	return world["wt"] / ".hook-only" / "runs"


def reset(world):
	"""Forget every run fact of the world (the worktree's .hook-only, the topic's verify spec), keep the review."""
	shutil.rmtree(world["wt"] / ".hook-only", ignore_errors=True)
	shutil.rmtree(world["main"] / "plans" / "verify", ignore_errors=True)
	if not (world["main"] / "plans" / "worker").is_dir():
		write(world["main"] / "plans" / "worker" / f"{TOPIC}.json", {"minutes": 20, "est_usd": 2, "class": "logic"})
	review = {"agent_id": "r1", "verdict": "APPROVE", "head": git(world["wt"], "rev-parse", "HEAD")}
	write(world["wt"] / ".hook-only" / "reviews.jsonl", json.dumps(review) + "\n")
	runsOf(world).mkdir(parents=True)


def tellRun(world, **bent):
	"""Run 1 of the world as its writers leave it when auto-merge.py starts (BASE), with the given facts bent."""
	case = {**BASE, **bent}
	reset(world)
	root, runs, recorded = str(world["wt"]), runsOf(world), case["source"] != "legacy"
	write(world["wt"] / ".hook-only" / "flow-mode", "local\n")
	write(runs / "est-1.json", {"minutes": 20, "usd": 2, "cap": 2.6, "cap_minutes": 26})
	if recorded:
		fields = {
			"topic": TOPIC,
			"started_by": "person",
			"purpose": "build",
			"flow": "written",
			"integration_mode": "local",
			"prompt_kind": "feature-flow",
			"launch": {"launched_at": clock.isoNow(), "by_pid": 1},
			"estimate": {"minutes": 20, "usd": 2, "cap_minutes": 26, "cap_usd": 2.6},
		}
		runstate.launchRecord(root, RUN, fields)
	if case["phase"] != "launching":
		started(world, case, recorded)
	if case["phase"] not in ("launching", "running"):
		ended(world, case, recorded)
	if case["held"]:
		write(world["wt"] / ".hook-only" / "agent-guard" / case["held"], "x")
	if case["spec"] == "hidden":
		shutil.rmtree(world["main"] / "plans")
	elif case["spec"]:
		write(world["main"] / "plans" / "verify" / f"{TOPIC}.json", SPECS[case["spec"]])
	if case["source"] == "record":
		for name in ("est-1.json", "start-1", "main-1", "exit-1", "stopped-1"):
			if (runs / name).exists():
				(runs / name).unlink()
		(world["wt"] / ".hook-only" / "flow-mode").unlink()
	if case["phase"] == "orphan-stale":
		stale(world, recorded)
	if case["corrupt"] and recorded:
		corrupt(world, case["corrupt"])
	return case


def started(world, case, recorded):
	root, runs = str(world["wt"]), runsOf(world)
	write(runs / "start-1", clock.isoNow() + "\n")
	if recorded:
		runstate.recordStarted(root, RUN, GONE_PID)
		runstate.beat(root, RUN, runstate.settings()["heartbeat_interval_seconds"])
	head = case["head"]
	if head == "missing":
		return
	write(runs / "main-1", world["heads"]["other" if head == "differs" else head] + "\n")
	if recorded:
		runstate.recordMainHead(root, RUN)
	if head == "differs":
		write(runs / "main-1", world["heads"]["same"] + "\n")


def ended(world, case, recorded):
	"""claude's end and the wrap-up steps of BASE's finishing run; the end of the run for the ended phases."""
	root, runs = str(world["wt"]), runsOf(world)
	if case["status"] == "not-json":
		write(runs / "result-1.json", "{half")
	elif case["status"] == "not-an-object":
		write(runs / "result-1.json", "[1]")
	elif case["status"] != "missing":
		write(runs / "result-1.json", {"result": case["status"]})
	if recorded and case["phase"] != "no-claude-end":
		runstate.recordClaudeEnded(root, RUN, 0, case["phase"] == "ended-stopped")
	for name, files in (("verify", VERIFY_FILES), ("verifier", VERIFIER_FILES)):
		if case[name]:
			stepFiles(world, name, files, case[name], recorded)
	if case["phase"] == "orphan-exited" and recorded:
		runstate.recordWrapperExit(root, RUN, 0)
	if case["phase"] == "ended-stopped":
		write(runs / "stopped-1", clock.isoNow())
		if recorded:
			runstate.recordStop(root, RUN, "person")
	if case["phase"] in ("ended", "ended-stopped"):
		code = 130 if case["phase"] == "ended-stopped" else 0
		write(runs / "exit-1", f"{code} {clock.isoNow()}\n")
		if recorded:
			runstate.recordRunEnded(root, RUN, "stopped" if code else "completed", code)


def stepFiles(world, name, files, pair, recorded):
	"""pair (file, record): the step file today's story holds and the one the record's step was read back from (as
	verify.py and verifier.py write both); None leaves that side without the step."""
	fileValue, recordValue = pair
	path = runsOf(world) / f"{name}-1.json"
	if recordValue and recorded:
		write(path, files[recordValue])
		runstate.recordStepFromFile(str(world["wt"]), RUN, name)
	if fileValue:
		write(path, files[fileValue])
	elif path.exists():
		path.unlink()


def stale(world, recorded):
	"""The wrapper stopped living long ago: an old heartbeat, every file of today's runs/ old."""
	root, runs = str(world["wt"]), runsOf(world)
	old = clock.now() - timedelta(seconds=STALE_SECONDS)
	if recorded:
		beat = statefile.read(root, runstate.HEARTBEAT, RUN)
		statefile.write(root, runstate.HEARTBEAT, {**beat, "beat_at": clock.isoOf(old)}, RUN)
	for name in os.listdir(runs):
		os.utime(runs / name, (old.timestamp(), old.timestamp()))


def corrupt(world, how):
	path = statefile.pathOf(str(world["wt"]), runstate.RECORD, RUN)
	with open(path, encoding="utf-8") as f:
		record = json.load(f)
	if how == "not-json":
		write(path, "{half")
		return
	if how == "schema-2":
		record["schema"] = 2
	elif how == "no-started-by":
		del record["started_by"]
	elif how == "status-enum" and record.get("claude"):
		record["claude"]["status"] = "done-ish"
	elif how == "naive-time":
		record["launch"]["launched_at"] = "2026-11-04T10:00:00"
	elif how == "step-outcome":
		record.setdefault("steps", {})["verify"] = {"state": "done", "outcome": "passed"}
	write(path, record)


def realData():
	"""(spec, verify, verifier) of the matrix: every step outcome on both sides, agreeing and not, under every spec."""
	pairs = lambda names: [(n, n) for n in names] + [(None, None)]  # noqa: E731
	found = [(None, None, None)]
	for spec in ("checks", "verifier", "verifier-null", "not-json", "a-list", "hidden", None):
		for verify in pairs(VERIFY_FILES) + [("pass", None), (None, "pass"), ("fail", "pass"), ("pass", "fail")]:
			found.append((spec, verify, None))
	for spec in ("verifier", "verifier-null", "checks", "not-json", "a-list", "hidden", None):
		for verifier in pairs(VERIFIER_FILES) + [
			("passed", None),
			(None, "passed"),
			("failed", "passed"),
			("passed", "failed"),
		]:
			found.append((spec, ("pass", "pass"), verifier))
	for verify in (("fail", "fail"), ("error", "error"), (None, None)):
		for verifier in (("failed", "failed"), ("passed", "passed"), (None, None)):
			found.append(("verifier", verify, verifier))
	return found


def cases():
	"""The matrix (module doc): every fact bent on its own from BASE, for each source, plus the pairs whose order
	decides between a merge run and a fix run, and every phase with every STATUS."""
	found = []
	for source in SOURCES:
		found.append({"source": source})
		found += [{"source": source, "phase": p} for p in PHASES]
		found += [{"source": source, "status": s} for s in STATUSES]
		found += [{"source": source, "head": h} for h in HEADS]
		found += [{"source": source, "held": h} for h in ("ALERT", "HOLD")]
		found += [{"source": source, "corrupt": c} for c in CORRUPTIONS]
	for spec, verify, verifier in realData():
		for source in SOURCES:
			found.append({"source": source, "spec": spec, "verify": verify, "verifier": verifier})
	for head in HEADS:
		for verify in (("fail", "fail"), ("error", "error"), (None, None)):
			found.append({"head": head, "spec": "verifier", "verify": verify, "verifier": ("failed", "failed")})
	for phase in PHASES:
		for status in STATUSES:
			found.append({"phase": phase, "status": status})
	return [dict(c) for c in {json.dumps(c, sort_keys=True): c for c in found}.values()]


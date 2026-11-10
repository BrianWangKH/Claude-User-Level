"""SubagentStart, SubagentStop, PreToolUse(*), PostToolUse(SubagentHandback), UserPromptSubmit [flow subagents, main session]: after-the-fact checks on what a flow subagent changed.

Nothing is intercepted; SubagentStart snapshots the checkout and SubagentStop compares, so git run
through Bash, scripts, `python -c` or aliases is caught alike (refs move whatever the caller).

| agent_type          | must stay unchanged                                              |
|---------------------|------------------------------------------------------------------|
| contains "reviewer" | refs and every file                                              |
| developer           | refs, "tests" and "qaConfig" paths                               |
| qa                  | refs; may change only "tests" and "qaConfig" paths               |

This hook knows no project: every path comes from the repo's .claude/guard-paths.json
(template: ~/.claude/templates/guard-paths.json), e.g.
  {"tests": ["tests/", "**/*_test.go"], "qaConfig": ["jest.config.js"], "shared": ["package.json"]}
  - tests:    test code; qa writes it, developer must not touch it
  - qaConfig: test-runner config and test-only dependencies; same rule as tests
  - shared:   files both roles may legitimately change (a project file both code and test setup
              live in); each change sends a reminder, not an ALERT, so the code reviewer reads
              the diff. The hook never parses file contents.
  An entry is matched as qg_common.pathMatches says, the rule the gates use for the same file: a plain entry
  is the path itself or a directory prefix ("tests" and "tests/" alike); a glob keeps `*` within one path
  segment, `**/` is any depth including the root (`**/*_test.go` matches `a_test.go` and `pkg/a_test.go`).
Without the file only refs are checked, and a reminder says so.
guard-paths.json is read at SubagentStart, as committed at the diff base (qg_common.committedJson,
never the working tree), and kept in the snapshot: neither the agent nor anyone earlier on the branch
can loosen the check by editing or deleting it; a change takes effect once merged. Any change under .claude/ (guard-paths, agents, settings,
skills) by developer or qa is an ALERT: project config is the planning session's job.

Review verdicts: the verdict line of each code-reviewer report (APPROVE or CHANGES REQUESTED,
code-reviewer.md) is appended with HEAD to <checkout>/.hook-only/reviews.jsonl. A reviewer that hands
its report back (SubagentHandback, Claude Code v2.1.271+) is recorded at PostToolUse on that tool
(hook-entry.py's `post` entry, code-reviewer only), from tool_input.message: the hooks docs say last_assistant_message
"then holds the subagent's closing text, if any, which is not the delivered report", and a
hand-back the mutation gate denies never reaches PostToolUse ("After a tool call succeeds"), so only
the delivered report counts. Its SubagentStop then records nothing for the same HEAD (the closing
text would land as NONE after the APPROVE, and auto-merge reads the last full verdict); a reviewer
that ends without a hand-back is recorded at SubagentStop from last_assistant_message. Each line has kind
"merge" when dispatch-guard accepted it as a merge review (merge-review-pending in this hook's
directory, claimed at that reviewer's SubagentStart) and "full" otherwise. auto-merge.py
(local mode) merges only on an APPROVE recorded here, never on the plan's ## Reviewer section, which the
orchestrator writes.

Refs: HEAD and the checked-out branch must not change (ALERT). Stash entries made on this
checkout's branch, or whose branch can't be read, must not appear or disappear (ALERT); stash,
tags and other local branches are shared by every worktree, so their other changes only send a
reminder. refs/remotes is ignored (fetches). A move something else accounts for sends nothing (parallel
workers: every round of one topic reported the other topic's branch): the branch checked out
in another worktree where a worker run was active during the round (runstate.activeSince: a run launching,
running, finishing or unreadable, or one that ended during the round; a run whose wrapper died without an end
only until its last sign of life, so a move after that is not explained), or a commit auto-merge.py recorded in
<main>/.hook-only/auto-merge/merges.jsonl.
Both records are hook-only files, so an agent can't explain its own move away; any other move of another
branch still sends the reminder: branches live in the main checkout's .git, which every worktree shares
and a worker writes to commit, so `git branch -f` or `git update-ref` in one worktree moves another's.

Files: only paths that are dirty at start or at stop are compared (status code + content hash), so
edits that were already there before the round (fix rounds) don't count as changes.

PreToolUse, main session only:
  - ALERT pending: deny that one call with the alert as the reason, move it to HOLD.
  - HOLD present: deny every agent dispatch (Agent, Task, SendMessage) and every Bash `git commit`
    or `git merge` with the held alert as the reason; other calls (reading, reporting) pass. Before
    the hold, an ALERT blocked one call only, and the orchestrator committed right after it.
  - reminders pending: add them as additionalContext (no permission decision), then clear them.
  - Agent call spawning a fresh developer with a dirty working tree: add a warning. Resumes go
    through SendMessage, so fix rounds (dirty by design) are not checked.

The hold ends when a person answers: UserPromptSubmit in an interactive session removes HOLD. In a
headless worker (CLAUDE_WORKER_MINUTES set) the wrapper's own prompts don't count; worker-control.py
removes the worktree's HOLD on `wt resume`, which the user types after deciding.

State lives in <checkout>/.hook-only/agent-guard/ (see hookonly.py). Every checkout (main or
worktree) has its own, so one worktree's ALERT is never consumed by another worktree's session.
The checkout comes from CLAUDE_PROJECT_DIR, so a reviewer that cds into a scratch copy is still
checked against the real checkout.

Fail-closed: a failed snapshot is stored as an error and turns into an ALERT at stop; a failed
check at stop writes an ALERT with the error; if even that fails, hook-entry.py warns the person once and
records a hook-failed event (a block on SubagentStop would keep the subagent running).
Cleanup: snapshots older than 7 days and alerts older than 30 days are removed at SubagentStart.

The snapshot is kept after SubagentStop, not deleted: a background subagent fires SubagentStop
twice, once when its turn ends and again after it calls SubagentHandback. Deleting at the first
stop made the second one report "no start snapshot" (qa clean, then ALERT
9 s later). The second stop compares against the same baseline; the next SubagentStart of that
agent overwrites it. If a round's SubagentStart never fires, its stop compares against the previous
round's snapshot, so changes made between rounds count as this round's: a false ALERT, never a miss.

Reads: the hook input, git, .claude/guard-paths.json at the diff base, runstate.activeSince, auto-merge's
merges.jsonl.
Writes: .hook-only/agent-guard/ (snapshots, ALERT, NOTICE, HOLD, guard.log), .hook-only/reviews.jsonl.
Output: preToolUse(data) returns the PreToolUse deny or context, or None; the other entry functions return None.
Fails: closed on PreToolUse (hook-entry.py denies the call on an exception); the other events as above.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import hookio
import hookonly
import qg_common

GUARD_PATHS_FILE = qg_common.GUARD_PATHS_FILE
PROJECT_CONFIG_DIR = ".claude"
MAIN_TYPES = {None, "claude"}
SNAPSHOT_STALE_SECONDS = 7 * 24 * 3600
DISPATCH_TOOLS = {"Agent", "Task", "SendMessage"}
HELD_GIT = re.compile(r"\bgit\b[^|;&]*\b(commit|merge)\b")
ALERT_STALE_SECONDS = 30 * 24 * 3600


def git(cwd, *args, check=True):
	result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=check)
	return result.stdout.decode("utf-8", "surrogateescape")


def roleOf(agentType):
	if not agentType:
		return None
	if "reviewer" in agentType:
		return "reviewer"
	if agentType in ("developer", "qa"):
		return agentType
	return None


def loadGuardPaths(root):
	given = qg_common.committedJson(root, GUARD_PATHS_FILE)
	if given is None:
		return None
	return {key: [entry(p) for p in given.get(key) or []] for key in ("tests", "qaConfig", "shared")}


def entry(value):
	"""A plain entry loses its trailing "/" (so "tests/" also covers a path that is exactly "tests"); a glob is kept
	as written, where a trailing "/" makes it a directory glob (qg_common.pathMatches)."""
	value = str(value)
	return value if qg_common.isGlob(value) else value.rstrip("/")


def matches(path, entries):
	return any(qg_common.pathMatches(path, e) for e in entries)


def refSnapshot(root):
	head = git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).strip() or "none"
	branch = git(root, "symbolic-ref", "-q", "HEAD", check=False).strip() or "detached"
	refs = {}
	for line in git(root, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
		name, _, sha = line.partition(" ")
		if not name.startswith("refs/remotes/"):
			refs[name] = sha
	stash = []
	for line in git(root, "stash", "list", "--format=%H%x09%gs", check=False).splitlines():
		sha, _, subject = line.partition("\t")
		if sha:
			stash.append([sha, subject])
	return {"head": head, "branch": branch, "refs": refs, "stash": stash}


STASH_BRANCH = re.compile(r"^(?:WIP on|On) (.+?): ")


def stashEntries(entries):
	"""{sha: subject}; snapshots taken before subjects were recorded hold bare shas."""
	return {e[0]: e[1] for e in entries if isinstance(e, list)} | {e: "" for e in entries if isinstance(e, str)}


def stashFindings(before, after, ownBranches):
	"""refs/stash is shared by every worktree of the repo. A stash entry names the branch it was made
	on ("WIP on <branch>: ..."), so only entries of this checkout's branch count as this round's;
	entries of other branches are another worktree's and only send a reminder. An entry whose branch
	can't be read counts as this round's (fail-closed)."""
	alerts, notes = [], []
	old, new = stashEntries(before), stashEntries(after)
	changes = [("created", sha, new[sha]) for sha in new if sha not in old]
	changes += [("removed", sha, old[sha]) for sha in old if sha not in new]
	for what, sha, subject in changes:
		match = STASH_BRANCH.match(subject)
		branch = match.group(1) if match else None
		line = f"stash {what}: {subject or sha[:10]}"
		if branch is None or branch in ownBranches:
			alerts.append(line)
		else:
			notes.append(f"{line} (another worktree)")
	return alerts, notes


def fileHash(path):
	if os.path.islink(path):
		return "link:" + os.readlink(path)
	if not os.path.isfile(path):
		return "missing"
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			digest.update(chunk)
	return digest.hexdigest()


def dirtyFiles(root):
	"""{path: "<XY> <content hash>"} for every path git status reports (untracked included)."""
	out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
	entries = out.split("\0")
	files = {}
	i = 0
	while i < len(entries):
		entry = entries[i]
		i += 1
		if len(entry) < 4:
			continue
		code, path = entry[:2], entry[3:]
		if hookonly.isHookOnlyPath(path):
			# Hook state, even when the gitignore entry is missing.
			continue
		files[path] = f"{code} {fileHash(os.path.join(root, path))}"
		if code[0] in "RC" and i < len(entries):
			# Rename/copy: the next entry is the source path.
			source = entries[i]
			i += 1
			files[source] = f"{code}-source {fileHash(os.path.join(root, source))}"
	return files


def snapshot(root):
	return {"refs": refSnapshot(root), "files": dirtyFiles(root), "taken": time.time()}


def changedFiles(before, after):
	paths = set(before) | set(after)
	return sorted(p for p in paths if before.get(p, "clean") != after.get(p, "clean"))


def explainedMoves(root, since):
	"""(branches, commits) whose moves another process accounts for (module doc, Refs)."""
	import runstate  # only here: the other checks never need the run state

	common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).strip()
	mainDir = os.path.dirname(common) if common.endswith("/.git") else None
	commits = set()
	if mainDir:
		try:
			with open(os.path.join(mainDir, ".hook-only", "auto-merge", "merges.jsonl"), encoding="utf-8") as f:
				commits = {json.loads(line).get("commit") for line in f if line.strip()}
		except (OSError, ValueError):
			commits = set()
	branches, path, here = set(), None, os.path.realpath(root)
	for line in git(root, "worktree", "list", "--porcelain", check=False).splitlines() + [""]:
		if line.startswith("worktree "):
			path = line[len("worktree "):]
		elif line.startswith("branch ") and path and os.path.realpath(path) != here and runstate.activeSince(
				path, datetime.fromtimestamp(since).astimezone()):
			branches.add(line[len("branch "):])
		elif not line:
			path = None
	return branches, commits


def refFindings(before, after, explained=(set(), set())):
	alerts, notes = [], []
	explainedBranches, explainedCommits = explained
	if before["head"] != after["head"]:
		alerts.append(f"HEAD moved {before['head'][:10]} -> {after['head'][:10]}")
	if before["branch"] != after["branch"]:
		alerts.append(f"checked-out branch changed {before['branch']} -> {after['branch']}")
	own = {before["branch"], after["branch"]}
	ownBranches = {b[len("refs/heads/"):] for b in own if b.startswith("refs/heads/")}
	if "detached" in own:
		ownBranches.add("(no branch)")
	stashAlerts, stashNotes = stashFindings(before["stash"], after["stash"], ownBranches)
	alerts += stashAlerts
	notes += stashNotes
	names = set(before["refs"]) | set(after["refs"])
	for name in sorted(names):
		old, new = before["refs"].get(name), after["refs"].get(name)
		if old == new or name in own or name == "refs/stash":
			continue
		what = "created" if old is None else "deleted" if new is None else "moved"
		if what == "moved" and (name in explainedBranches or new in explainedCommits):
			continue
		# Tags and other branches are shared by every worktree: a change may be another
		# worktree's, so it is a reminder, not an ALERT.
		if name.startswith("refs/tags/"):
			notes.append(f"tag {name[len('refs/tags/'):]} {what} (maybe another worktree)")
		else:
			notes.append(f"other branch {name} {what} (maybe another worktree)")
	return alerts, notes


def evaluate(role, before, after, guardPaths, explained=(set(), set())):
	"""Returns (alerts, reminders) as lists of short findings."""
	alerts, reminders = refFindings(before["refs"], after["refs"], explained)
	changed = changedFiles(before["files"], after["files"])
	if role == "reviewer":
		if changed:
			alerts.append("files changed: " + ", ".join(changed))
		return alerts, reminders
	config = [p for p in changed if matches(p, [PROJECT_CONFIG_DIR])]
	if config:
		alerts.append("project config under .claude/ changed: " + ", ".join(config))
	if guardPaths is None:
		reminders.append(f"{GUARD_PATHS_FILE} not found at the diff base; only refs were checked")
		return alerts, reminders
	tests, qaConfig, shared = guardPaths["tests"], guardPaths["qaConfig"], guardPaths["shared"]
	sharedChanged = [p for p in changed if matches(p, shared) and not matches(p, tests + qaConfig)]
	if sharedChanged:
		reminders.append("shared files changed, review the diff: " + ", ".join(sharedChanged))
	if role == "developer":
		protected = [p for p in changed if matches(p, tests + qaConfig)]
		if protected:
			alerts.append("protected test files changed: " + ", ".join(protected))
	elif role == "qa":
		outside = [p for p in changed if not matches(p, tests + qaConfig + shared + [PROJECT_CONFIG_DIR])]
		if outside:
			alerts.append("files outside tests/qaConfig changed: " + ", ".join(outside))
	return alerts, reminders


def append(path, text):
	with open(path, "a", encoding="utf-8") as f:
		f.write(text + "\n")


def removeStale(directory):
	now = time.time()
	for entry in os.listdir(directory):
		path = os.path.join(directory, entry)
		limit = ALERT_STALE_SECONDS if entry in ("ALERT", "NOTICE", "HOLD") else SNAPSHOT_STALE_SECONDS
		try:
			if (entry.endswith(".json") or entry in ("ALERT", "NOTICE", "HOLD")) and os.path.getmtime(path) < now - limit:
				os.remove(path)
		except OSError:
			pass


def onStart(data, root, directory):
	agentId = data["agent_id"]
	os.makedirs(directory, exist_ok=True)
	try:
		state = snapshot(root)
	except Exception as error:
		state = {"error": f"snapshot failed: {error}"}
	state["agent_type"] = data.get("agent_type")
	pending = os.path.join(directory, "merge-review-pending")
	if data.get("agent_type") == "code-reviewer" and os.path.isfile(pending):
		os.remove(pending)
		state["review_kind"] = "merge"
	try:
		state["guard_paths"] = loadGuardPaths(root)
	except Exception as error:
		state["error"] = f"guard-paths unreadable at start: {error}"
	with open(os.path.join(directory, f"{agentId}.json"), "w", encoding="utf-8") as f:
		json.dump(state, f)
	removeStale(directory)


VERDICT = re.compile(r"^[\W_]*(?:verdict[\W_]*)?(APPROVE|CHANGES REQUESTED)\b", re.MULTILINE | re.IGNORECASE)


def reviewsPath(root):
	return os.path.join(root, hookonly.NAME, "reviews.jsonl")


def handedBack(root, agentId, head):
	"""True when this reviewer's last recorded verdict came from its hand-back at this HEAD."""
	try:
		with open(reviewsPath(root), encoding="utf-8") as f:
			lines = [json.loads(l) for l in f if l.strip()]
	except (OSError, ValueError):
		return False
	mine = [r for r in lines if r.get("agent_id") == agentId]
	return bool(mine) and mine[-1].get("via") == "handback" and mine[-1].get("head") == head


def recordVerdict(data, root, kind="full", message=None, via="stop"):
	"""Append the code reviewer's verdict with HEAD to .hook-only/reviews.jsonl (module doc)."""
	if data.get("agent_type") != "code-reviewer":
		return
	head = git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).strip()
	if via == "stop" and handedBack(root, data.get("agent_id"), head):
		return
	if message is None:
		message = data.get("last_assistant_message") or ""
	found = [m.group(1).upper() for m in VERDICT.finditer(message)]
	# Conservative: APPROVE only when no "CHANGES REQUESTED" appears anywhere (a report that echoes the
	# verdict template would otherwise pass) and no verdict line asks for changes in any case (VERDICT
	# ignores case, so "verdict: changes requested" beside an APPROVE line counts); a wrong refusal costs
	# one look, a wrong APPROVE a merge. Guessing from free text until reports carry a fixed VERDICT field.
	if "CHANGES REQUESTED" in message or "CHANGES REQUESTED" in found:
		verdict = "CHANGES REQUESTED"
	else:
		verdict = "APPROVE" if "APPROVE" in found else "NONE"
	os.makedirs(os.path.join(root, hookonly.NAME), exist_ok=True)
	append(reviewsPath(root), json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
		"agent_id": data.get("agent_id"), "verdict": verdict, "head": head, "kind": kind, "via": via}))


def handbackMessage(data):
	"""The delivered report of a SubagentHandback call: tool_input.message (hooks docs, SubagentStop)."""
	message = (data.get("tool_input") or {}).get("message")
	return message if isinstance(message, str) else ""


def reviewKind(directory, agentId):
	try:
		with open(os.path.join(directory, f"{agentId}.json"), encoding="utf-8") as f:
			return json.load(f).get("review_kind", "full")
	except (OSError, ValueError):
		return "full"


def onStop(data, root, directory):
	recordVerdict(data, root, reviewKind(directory, data.get("agent_id")))
	agentType = data.get("agent_type")
	agentId = data["agent_id"]
	label = f"agent-guard: {agentType} ({agentId})"
	stateFile = os.path.join(directory, f"{agentId}.json")
	try:
		if not os.path.isfile(stateFile):
			raise RuntimeError("no start snapshot (SubagentStart did not run or its state was removed)")
		with open(stateFile, encoding="utf-8") as f:
			before = json.load(f)
		if "error" in before:
			raise RuntimeError(before["error"])
		guardPaths = before["guard_paths"] if "guard_paths" in before else loadGuardPaths(root)
		explained = explainedMoves(root, float(before.get("taken") or 0))
		alerts, reminders = evaluate(roleOf(agentType), before, snapshot(root), guardPaths, explained)
	except Exception as error:
		alerts, reminders = [f"guard could not check this round ({error})"], []
	os.makedirs(directory, exist_ok=True)
	append(os.path.join(directory, "guard.log"),
		f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {agentType} {agentId} "
		+ ("ALERT" if alerts else "notice" if reminders else "clean"))
	if alerts:
		append(os.path.join(directory, "ALERT"), f"{label}: " + "; ".join(alerts) + ".")
	if reminders:
		append(os.path.join(directory, "NOTICE"), f"{label}: " + "; ".join(reminders) + ".")


def takeFile(path):
	if not os.path.isfile(path):
		return ""
	with open(path, encoding="utf-8") as f:
		text = f.read().strip()
	os.remove(path)
	return text


def isHeld(data):
	"""The calls a HOLD denies: agent dispatches and git commits or merges."""
	tool = data.get("tool_name")
	command = (data.get("tool_input") or {}).get("command") or ""
	return tool in DISPATCH_TOOLS or (tool == "Bash" and HELD_GIT.search(command) is not None)


def onMainPreToolUse(data, root, directory):
	alert = takeFile(os.path.join(directory, "ALERT"))
	notice = takeFile(os.path.join(directory, "NOTICE"))
	holdFile = os.path.join(directory, "HOLD")
	if alert:
		append(holdFile, alert)
		reason = alert + (f"\nAlso: {notice}" if notice else "")
		return hookio.refuse("PreToolUse", f"{reason}\nTell the user and stop. Until the user answers, every agent dispatch and git commit or merge is denied; reading and reporting still work.")
	if os.path.isfile(holdFile) and isHeld(data):
		with open(holdFile, encoding="utf-8") as f:
			held = f.read().strip()
		return hookio.refuse("PreToolUse", f"agent-guard hold, until the user answers: {held}\nReport the alert to the user (in a headless worker: end the run with `STATUS: blocked`, quoting it).")
	context = []
	if notice:
		context.append(f"Reminder only, not an alert. {notice} Mention it to the user.")
	toolInput = data.get("tool_input") or {}
	if data.get("tool_name") in ("Agent", "Task") and toolInput.get("subagent_type") == "developer":
		dirty = git(root, "status", "--porcelain", "--untracked-files=all").strip()
		if dirty:
			context.append("agent-guard: spawning a new developer with a dirty working tree; the tests are usually committed first. Uncommitted:\n" + dirty)
	if context:
		return hookio.context("PreToolUse", "\n".join(context))
	return None


def onUserPrompt(directory):
	"""A person answered: lift the hold. A headless worker's prompts come from its wrapper, not a
	person; worker-control.py lifts its hold on `wt resume`."""
	if os.environ.get("CLAUDE_WORKER_MINUTES"):
		return
	try:
		os.remove(os.path.join(directory, "HOLD"))
	except FileNotFoundError:
		pass


def held(data, root):
	"""Whether this call would be denied by the hold, read only (crap-gate.py checks it before it runs the
	suite): main session, and an ALERT pending (it denies the next call, whatever it is) or a HOLD that
	covers this call (isHeld)."""
	if data.get("agent_type") not in MAIN_TYPES:
		return False
	directory = hookonly.stateDir(root, "agent-guard")
	alert = os.path.join(directory, "ALERT")
	if os.path.isfile(alert):
		with open(alert, encoding="utf-8") as f:
			if f.read().strip():
				return True
	return os.path.isfile(os.path.join(directory, "HOLD")) and isHeld(data)


def subagentState(data):
	"""(root, state directory) for a flow subagent's event, or None (not a guarded role, or outside git)."""
	if not roleOf(data.get("agent_type")) or not data.get("agent_id"):
		return None
	root = hookonly.gitRoot(data)
	return (root, hookonly.stateDir(root, "agent-guard")) if root else None


def mainState(data):
	if data.get("agent_type") not in MAIN_TYPES:
		return None
	root = hookonly.gitRoot(data)
	return (root, hookonly.stateDir(root, "agent-guard")) if root else None


def onSubagentStart(data):
	found = subagentState(data)
	if found:
		onStart(data, *found)


def onSubagentStop(data):
	found = subagentState(data)
	if found:
		onStop(data, *found)


def onHandback(data):
	"""PostToolUse SubagentHandback: the delivered report's verdict (module doc)."""
	found = subagentState(data)
	if found and data.get("tool_name") == "SubagentHandback":
		root, directory = found
		recordVerdict(data, root, reviewKind(directory, data.get("agent_id")), handbackMessage(data), via="handback")


def preToolUse(data):
	found = mainState(data)
	return onMainPreToolUse(data, *found) if found else None


def onPrompt(data):
	found = mainState(data)
	if found:
		onUserPrompt(found[1])

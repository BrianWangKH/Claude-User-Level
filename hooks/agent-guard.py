"""Agent guard: after-the-fact checks on what a flow subagent changed (replaces reviewer-guard.py).

Nothing is intercepted; SubagentStart snapshots the checkout and SubagentStop compares, so git run
through Bash, scripts, `python -c` or aliases is caught alike (refs move whatever the caller).

| agent_type          | must stay unchanged                                              |
|---------------------|------------------------------------------------------------------|
| contains "reviewer" | refs and every file                                              |
| developer           | refs, "tests" and "qaConfig" paths                               |
| qa                  | refs; may change only "tests" and "qaConfig" paths               |

This hook knows no project: every path comes from the repo's .claude/guard-paths.json
(template: ~/.claude/templates/guard-paths.json), e.g.
  {"tests": ["tests/"], "qaConfig": ["jest.config.js"], "shared": ["package.json"]}
  - tests:    test code; qa writes it, developer must not touch it
  - qaConfig: test-runner config and test-only dependencies; same rule as tests
  - shared:   files both roles may legitimately change (a project file both code and test setup
              live in); each change sends a reminder, not an ALERT, so the code reviewer reads
              the diff. The hook never parses file contents.
Without the file only refs are checked, and a reminder says so.
guard-paths.json is read at SubagentStart and kept in the snapshot, so an agent that edits or
deletes it can't loosen its own check. Any change under .claude/ (guard-paths, agents, settings,
skills) by developer or qa is an ALERT: project config is the planning session's job.

Refs: HEAD and the checked-out branch must not change (ALERT). Stash entries made on this
checkout's branch, or whose branch can't be read, must not appear or disappear (ALERT); stash,
tags and other local branches are shared by every worktree, so their other changes only send a
reminder. refs/remotes is ignored (fetches).

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
check at stop writes an ALERT with the error; if even that fails, print to stderr and exit 1
(exit 2 on SubagentStop would keep the subagent running).
Cleanup: snapshots older than 7 days and alerts older than 30 days are removed at SubagentStart.

The snapshot is kept after SubagentStop, not deleted: a background subagent fires SubagentStop
twice, once when its turn ends and again after it calls SubagentHandback. Deleting at the first
stop made the second one report "no start snapshot" (seen 2026-10-25: qa clean at 01:42:37, ALERT
at 01:42:46). The second stop compares against the same baseline; the next SubagentStart of that
agent overwrites it. If a round's SubagentStart never fires, its stop compares against the previous
round's snapshot, so changes made between rounds count as this round's: a false ALERT, never a miss.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import hookonly

GUARD_PATHS_FILE = os.path.join(".claude", "guard-paths.json")
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
	path = os.path.join(root, GUARD_PATHS_FILE)
	if not os.path.isfile(path):
		return None
	with open(path, encoding="utf-8") as f:
		given = json.load(f)
	return {key: [str(p).rstrip("/") for p in given.get(key) or []] for key in ("tests", "qaConfig", "shared")}


def matches(path, entries):
	return any(path == e or path.startswith(e + "/") for e in entries)


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


def refFindings(before, after):
	alerts, notes = [], []
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
		# Tags and other branches are shared by every worktree: a change may be another
		# worktree's, so it is a reminder, not an ALERT.
		if name.startswith("refs/tags/"):
			notes.append(f"tag {name[len('refs/tags/'):]} {what} (maybe another worktree)")
		else:
			notes.append(f"other branch {name} {what} (maybe another worktree)")
	return alerts, notes


def evaluate(role, before, after, guardPaths):
	"""Returns (alerts, reminders) as lists of short findings."""
	alerts, reminders = refFindings(before["refs"], after["refs"])
	changed = changedFiles(before["files"], after["files"])
	if role == "reviewer":
		if changed:
			alerts.append("files changed: " + ", ".join(changed))
		return alerts, reminders
	config = [p for p in changed if matches(p, [PROJECT_CONFIG_DIR])]
	if config:
		alerts.append("project config under .claude/ changed: " + ", ".join(config))
	if guardPaths is None:
		reminders.append(f"{GUARD_PATHS_FILE} not found; only refs were checked")
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
	try:
		state["guard_paths"] = loadGuardPaths(root)
	except Exception as error:
		state["error"] = f"guard-paths unreadable at start: {error}"
	with open(os.path.join(directory, f"{agentId}.json"), "w", encoding="utf-8") as f:
		json.dump(state, f)
	removeStale(directory)


def onStop(data, root, directory):
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
		alerts, reminders = evaluate(roleOf(agentType), before, snapshot(root), guardPaths)
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


def deny(reason):
	print(json.dumps({"hookSpecificOutput": {
		"hookEventName": "PreToolUse",
		"permissionDecision": "deny",
		"permissionDecisionReason": reason,
	}}))


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
		deny(f"{reason}\nTell the user and stop. Until the user answers, every agent dispatch and git commit or merge is denied; reading and reporting still work.")
		return
	if os.path.isfile(holdFile) and isHeld(data):
		with open(holdFile, encoding="utf-8") as f:
			held = f.read().strip()
		deny(f"agent-guard hold, until the user answers: {held}\nReport the alert to the user (in a headless worker: end the run with `STATUS: blocked`, quoting it).")
		return
	context = []
	if notice:
		context.append(f"Reminder only, not an alert. {notice} Mention it to the user.")
	toolInput = data.get("tool_input") or {}
	if data.get("tool_name") in ("Agent", "Task") and toolInput.get("subagent_type") == "developer":
		dirty = git(root, "status", "--porcelain", "--untracked-files=all").strip()
		if dirty:
			context.append("agent-guard: spawning a new developer with a dirty working tree; the tests are usually committed first. Uncommitted:\n" + dirty)
	if context:
		print(json.dumps({"hookSpecificOutput": {
			"hookEventName": "PreToolUse",
			"additionalContext": "\n".join(context),
		}}))


def onUserPrompt(directory):
	"""A person answered: lift the hold. A headless worker's prompts come from its wrapper, not a
	person; worker-control.py lifts its hold on `wt resume`."""
	if os.environ.get("CLAUDE_WORKER_MINUTES"):
		return
	try:
		os.remove(os.path.join(directory, "HOLD"))
	except FileNotFoundError:
		pass


def main():
	data = json.load(sys.stdin)
	event = data.get("hook_event_name")
	agentType = data.get("agent_type")
	if event in ("PreToolUse", "UserPromptSubmit"):
		if agentType not in MAIN_TYPES:
			return
	elif not roleOf(agentType) or not data.get("agent_id"):
		return
	root = hookonly.gitRoot(data)
	if not root:
		return
	directory = hookonly.stateDir(root, "agent-guard")
	if event == "SubagentStart":
		onStart(data, root, directory)
	elif event == "SubagentStop":
		onStop(data, root, directory)
	elif event == "PreToolUse":
		onMainPreToolUse(data, root, directory)
	elif event == "UserPromptSubmit":
		onUserPrompt(directory)


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"agent-guard error: {error}", file=sys.stderr)
		sys.exit(1)

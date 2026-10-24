"""PreToolUse guard for the test-first flow.

- code-reviewer: may not read anything under plans/ (plan-blind review).
- qa-planner / qa: may only write under plans/tests/.
- developer: may not write under plans/.
- main session: may not pass arguments to blind-review.

The main session may spawn code-reviewer with the Agent tool, so later rounds can resume it.
For the stricter true-blind mode (fork via skill blind-review only), see ~/.claude/docs/blind-review.md.

Main session = no agent_type, or agent_type "claude". Empty agent_type (internal agents) is ignored.
Fail-closed: any error denies the call with the error as the reason.
"""
import json
import os
import sys

READ_TOOLS = {"Read", "Grep", "Glob"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
MAIN_TYPES = {None, "claude"}


def deny(reason):
	print(json.dumps({
		"hookSpecificOutput": {
			"hookEventName": "PreToolUse",
			"permissionDecision": "deny",
			"permissionDecisionReason": reason,
		}
	}))
	sys.exit(0)


def targetPath(toolInput):
	for key in ("file_path", "notebook_path", "path"):
		value = toolInput.get(key)
		if value:
			return value
	return None


def isUnder(path, root, cwd):
	if not os.path.isabs(path):
		path = os.path.join(cwd, path)
	path = os.path.realpath(path)
	root = os.path.realpath(root)
	return path == root or path.startswith(root + os.sep)


def main():
	data = json.load(sys.stdin)
	tool = data.get("tool_name", "")
	toolInput = data.get("tool_input") or {}
	agentType = data.get("agent_type")
	if agentType == "":
		return

	cwd = data.get("cwd") or os.getcwd()
	projectDir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
	plansDir = os.path.join(projectDir, "plans")
	testPlansDir = os.path.join(plansDir, "tests")

	if agentType in MAIN_TYPES:
		if tool == "Skill" and toolInput.get("skill") == "blind-review" and str(toolInput.get("args") or "").strip():
			deny("blind-review takes no arguments; the reviewer finds the base itself.")
		return

	path = targetPath(toolInput)
	if not path:
		return

	if agentType == "code-reviewer" and tool in READ_TOOLS | WRITE_TOOLS and isUnder(path, plansDir, cwd):
		deny("Plan-blind review: code-reviewer may not access plans/.")

	if tool not in WRITE_TOOLS or not isUnder(path, plansDir, cwd):
		return

	if agentType in ("qa-planner", "qa") and not isUnder(path, testPlansDir, cwd):
		deny(f"{agentType} may only write under plans/tests/.")
	if agentType == "developer":
		deny("developer may not write under plans/.")


if __name__ == "__main__":
	try:
		main()
	except SystemExit:
		raise
	except Exception as error:
		deny(f"plan-guard failed ({type(error).__name__}: {error}); the call was blocked to be safe. Stop and report BLOCKED with this message.")

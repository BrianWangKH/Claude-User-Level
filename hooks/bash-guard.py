#!/usr/bin/env python3
"""PreToolUse(Bash) hook: no Bash command that starts with `cd <path> &&` or `cd <path>;`.

Global CLAUDE.md 「工具」 says so: the working directory is already the checkout, and a compound `cd`
triggers a permission prompt, which stops auto mode and headless workers (excess-returns, 2026-11-01:
the rule existed as text and agents kept breaking it). A lone `cd <path>` and `cd` later in a command are
left alone. The denial names the alternatives. Every session, every agent. Fails open on its own errors.
"""
import json
import re
import sys

CD_PREFIX = re.compile(r"""^\s*cd\s+("[^"]*"|'[^']*'|[^\s;&|]+)\s*(&&|;)""")


def main():
	data = json.load(sys.stdin)
	if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") != "Bash":
		return
	command = (data.get("tool_input") or {}).get("command") or ""
	match = CD_PREFIX.match(command)
	if not match:
		return
	print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
		"permissionDecisionReason": f"bash-guard: don't start a Bash command with `cd {match.group(1)} "
			f"{match.group(2)}` (global CLAUDE.md, 工具): the working directory is already the checkout. Use "
			"absolute paths, `git -C <path>`, a tool's own directory flag (`go -C`, `uv --directory`), or "
			"`env -C <path> <command>` for a tool that needs its working directory."}}))


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		print(f"bash-guard error (not checked): {error}", file=sys.stderr)
	sys.exit(0)

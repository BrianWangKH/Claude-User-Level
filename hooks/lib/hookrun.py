"""Run a hook's main() with the failure policy of the event it was invoked for.

Claude Code treats only exit 2 as blocking; any other non-zero exit is a non-blocking error and the action
goes ahead (hooks docs, "Exit code output": "If your hook is meant to enforce a policy, use exit 2").
- PreToolUse: any exception main() lets escape (a ConfigError from config/homeconfig.py included) prints
  "<hook>: <error> — blocked (fail closed)" to stderr and exits 2, so a guard that crashes blocks the tool
  call instead of letting it through unchecked.
- Every other event (UserPromptSubmit, Stop, SubagentStop, SessionStart, ...), and input that isn't JSON:
  the hook's own on_error(error) (its message and exit code, unchanged), or the exception propagates when
  it has none. Exit 2 there would block the person's prompt or keep an agent running.
SystemExit passes through: a hook's own exit code is its decision. The event comes from the hook input's
hook_event_name; stdin is read once here and handed to main() unchanged.
"""
import io
import json
import sys


def event_of(raw):
	try:
		data = json.loads(raw)
	except ValueError:
		return None
	return data.get("hook_event_name") if isinstance(data, dict) else None


def run(name, main, on_error=None):
	raw = sys.stdin.read()
	sys.stdin = io.StringIO(raw)
	event = event_of(raw)
	try:
		main()
	except SystemExit:
		raise
	except Exception as error:
		if event == "PreToolUse":
			print(f"{name}: {type(error).__name__}: {error} — blocked (fail closed)", file=sys.stderr)
			sys.exit(2)
		if on_error is None:
			raise
		on_error(error)

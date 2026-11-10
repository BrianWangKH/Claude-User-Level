#!/usr/bin/env python3
"""Checks the hand-kept `hooks` of settings.json against hooks/lib/hook_entries.py (a tool, not a hook).

Usage: hook-entries.py --check-settings [--file PATH]
  --check-settings  exit 1, printing each difference, unless the `hooks` of the settings file (default settings.json
                    of this home; --file another) hold exactly the registrations of hook_entries.REGISTRATIONS
                    (events, matchers, if, commands, async, onFailure, in order) and every registration with a
                    ceiling has a timeout at least that ceiling (registry rules hook_ceilings_seconds). The
                    timeouts are outer safety nets the person keeps in settings.json; the gates enforce their own
                    deadlines (home.json quality.hook_timeouts). The sandbox can't read settings.json: run it in
                    the person's terminal (apply.sh does).
Exit 0 ok, 1 differs or unreadable, 2 usage.
"""

import argparse
import copy
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import hook_entries  # noqa: E402


def expected():
	return hook_entries.settingsHooks(homeconfig.get(*hook_entries.CEILINGS))


def withoutTimeouts(hooks):
	"""(hooks without their timeouts, [timeouts in order])."""
	shape, timeouts = copy.deepcopy(hooks), []
	for groups in shape.values() if isinstance(shape, dict) else []:
		for group in groups if isinstance(groups, list) else []:
			for hook in group.get("hooks", []) if isinstance(group, dict) else []:
				timeouts.append(hook.pop("timeout", None) if isinstance(hook, dict) else None)
	return shape, timeouts


def dumps(obj):
	return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def differences(installed):
	"""Every way installed differs from the registrations (module doc); [] when it fits."""
	want = expected()
	wantShape, ceilings = withoutTimeouts(want)
	gotShape, timeouts = withoutTimeouts(installed)
	if gotShape != wantShape:
		return ["registrations differ from hooks/lib/hook_entries.py:\n" + "".join(difflib.unified_diff(
			dumps(wantShape).splitlines(True), dumps(gotShape).splitlines(True), "hook_entries.py", "settings.json"))]
	found = []
	commands = [h["command"] for groups in want.values() for g in groups for h in g["hooks"]]
	for command, ceiling, timeout in zip(commands, ceilings, timeouts):
		if ceiling is None:
			continue
		if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < ceiling:
			found.append(f"{command}: timeout {timeout!r} is below its ceiling {ceiling} s")
	return found


def checkSettings(path):
	try:
		with open(path, encoding="utf-8") as f:
			installed = json.load(f).get("hooks")
	except (OSError, ValueError, AttributeError) as error:
		print(f"FAIL: {path} unreadable: {error}")
		return 1
	found = differences(installed)
	if not found:
		print(f"ok: the hooks of {path} fit hooks/lib/hook_entries.py and its ceilings")
		return 0
	print(f"FAIL: the hooks of {path}: " + "\n".join(found))
	return 1


def main(argv=None):
	parser = argparse.ArgumentParser(description="check the settings hooks against hooks/lib/hook_entries.py")
	parser.add_argument("--check-settings", action="store_true", required=True)
	parser.add_argument("--file", default=os.path.join(hook_entries.HOME_ROOT, "settings.json"))
	args = parser.parse_args(argv)
	return checkSettings(args.file)


if __name__ == "__main__":
	sys.exit(main())

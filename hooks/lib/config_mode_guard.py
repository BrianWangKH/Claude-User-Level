"""Config mode TEST (homeconfig.configMode) starts workers only in rehearsal repos.

TEST runs on config/local.test.json (an external test harness, not in this repository: shorter waits, the
usage guard off). Left behind (a killed run, a power loss) it would drive the person's real workers with those
values, so while the mode is TEST no worker starts in a repo without the rehearsal marker:
<main>/.hook-only/rehearsal/repo.json (state kind rehearsal-repo, written only by that harness when it creates
a rehearsal repo; .hook-only/ is closed to agents).

- refusal(mainDir): None when a worker may start in the main checkout mainDir (DEV, or TEST in a marked repo);
  else why not, with the command that restores DEV. An unreadable mode or marker is a refusal (fails closed).
- statusLine(): one line naming the mode while it isn't DEV (TEST, or unreadable), else None: state shown with
  every wt answer, not a reminder of its own.
- mark(mainDir, group): write the marker (the harness only).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import clock  # noqa: E402
import homeconfig  # noqa: E402
import statefile  # noqa: E402

KIND = "rehearsal-repo"
SCHEMA = 1
DEV = "DEV"


def modeFile():
	return os.path.join(homeconfig.configDir(), homeconfig.MODE_FILE)


def restoreHint():
	return f"if no rehearsal runs (an earlier one was killed), restore DEV: printf 'DEV\\n' > {modeFile()}"


def refusal(mainDir):
	"""Why no worker may start in mainDir now, or None (module doc)."""
	try:
		if homeconfig.configMode() == DEV:
			return None
		if statefile.read(str(mainDir), KIND) is not None:
			return None
		why = f"config mode is TEST ({modeFile()}) and {mainDir} is no rehearsal repo"
	except (homeconfig.ConfigError, statefile.StateError, OSError, ValueError) as error:
		why = f"config mode or rehearsal marker unreadable ({error})"
	return f"{why}: no worker starts here; {restoreHint()}"


def statusLine():
	"""The config mode while it isn't DEV, as one line; None in DEV."""
	try:
		mode = homeconfig.configMode()
	except homeconfig.ConfigError as error:
		return f"config mode unreadable: {error}"
	if mode == DEV:
		return None
	return f"config mode: {mode} (only rehearsal repos start workers; {restoreHint()})"


def mark(mainDir, group):
	return statefile.write(str(mainDir), KIND, {"schema": SCHEMA, "group": group, "marked_at": clock.isoNow()})

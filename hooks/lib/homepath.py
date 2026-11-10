"""The import path of every home program: the one place that knows how hooks/ is laid out.

A program puts this directory (hooks/lib) on sys.path, one line relative to its own realpath, and imports
homepath; the import adds config/ (homeconfig) and every role directory of hooks/, so a home module is
imported by its bare name from any working directory:

	sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
	import homepath  # noqa: E402,F401

(from hooks/<role>/x.py; hooks/hook-entry.py and the skills join their own way to hooks/lib).

hooks/ by role (README.md, "Home layout"):
- lib:    shared modules with no event of their own (hookio, statefile, runstate, homepath itself, ...)
- guards: allow or deny a tool call or prompt (steps of hook-entry.py)
- gates:  quality gates that run tests or checks
- budget: time, cost and usage
- worker: the worker lifecycle and the wt commands
- tools:  developer tools, not hooks
Module basenames are unique across these directories and config/ (hooks/tests/test_layout.py), so the order
of the entries never decides which file an import finds. The entries are this tree's own: a test that loads
another tree's homepath first gets that tree's directories (sys.modules keeps the first one).
"""

import os
import sys

LIB = os.path.dirname(os.path.realpath(__file__))
HOOKS = os.path.dirname(LIB)
HOME_ROOT = os.path.dirname(HOOKS)
ROLE_DIRS = ("lib", "guards", "gates", "budget", "worker", "tools")
CONFIG_DIR = "config"


def searchDirs():
	"""The directories homepath puts on sys.path, relative to the home root, in sys.path order."""
	return [f"hooks/{role}" for role in ROLE_DIRS] + [CONFIG_DIR]


def locate(name):
	"""The path of hooks/<role>/<name> for a file name (basenames are unique across the role directories), or None."""
	for role in ROLE_DIRS:
		candidate = os.path.join(HOOKS, role, name)
		if os.path.isfile(candidate):
			return candidate
	return None


def install(path=None):
	"""Put searchDirs() (absolute) on path (default sys.path) in that order, ahead of what was there; an entry
	already present stays where it is."""
	path = sys.path if path is None else path
	missing = [d for d in (os.path.join(HOME_ROOT, rel) for rel in searchDirs()) if d not in path]
	path[:0] = missing


install()

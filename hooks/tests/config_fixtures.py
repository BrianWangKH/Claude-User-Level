"""Test helpers for the home settings layout (config/home.json sections, config/registry/*.json): copy a tree's
config without the person's files, point this process's homeconfig at a config directory, copy a tree's code
so a subprocess runs on its own settings and state, and replace one home.json section in such a copy.

No variable moves the config directory or the home state directory (homeconfig.configDir, statefile.homeStateDir):
in the test process the module functions are replaced (conftest, useConfig); a home program started as a
subprocess reads the config/ and state/ of the tree it runs from (copyHome)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The person's files in config/ (homeconfig LOCAL per config mode, MODE_FILE): never part of a test's settings.
PERSONAL = ("local.json", "local.test.json", "mode")
IGNORED = shutil.ignore_patterns(*PERSONAL, "__pycache__")
ROLE_DIRS = ("lib", "guards", "gates", "budget", "worker", "tools")  # homepath.ROLE_DIRS


def copyConfig(source, target):
	"""A copy of the config directory source (home.json, registry/, the loader) at target, without the person's
	files (PERSONAL)."""
	shutil.copytree(source, target, ignore=IGNORED)
	return Path(target)


def useConfig(monkeypatch, configDir):
	"""This test process's homeconfig (the module the test imported) reads configDir (homeconfig.configDir replaced
	for the test)."""
	monkeypatch.setattr("homeconfig.configDir", lambda: str(configDir))


# conftest sets it to the session's scratch directory (removed when the session ends).
SESSION_DIR = None
TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the tree under test
_clean = []


def cleanHome():
	"""The session's copy of the tree under test (copyHome with skills/ and agents/): the place to run a home program
	that needs no settings of its own, so the person's files in the tree (config/local.json, local.test.json, mode,
	state/) never change a result. Built once per process, in SESSION_DIR."""
	if not _clean:
		_clean.append(copyHome(TREE, Path(SESSION_DIR) / "home", tops=("skills", "agents")))
	return _clean[0]


def copyHome(source, target, tops=()):
	"""A copy of the home tree source's code at target: hooks/ (its top-level files and the role directories, tests
	aside), config/ (copyConfig) and each top-level directory in tops whole (skills, templates). A program started
	from the copy reads the copy's config/ and keeps its home state in the copy's state/; returns target."""
	target = Path(target)
	for folder in ["", *ROLE_DIRS]:
		(target / "hooks" / folder).mkdir(parents=True, exist_ok=True)
		for entry in os.listdir(os.path.join(source, "hooks", folder)):
			path = os.path.join(source, "hooks", folder, entry)
			if os.path.isfile(path):
				shutil.copy2(path, target / "hooks" / folder / entry)
	copyConfig(os.path.join(source, "config"), target / "config")
	for top in tops:
		shutil.copytree(os.path.join(source, top), target / top, ignore=shutil.ignore_patterns("__pycache__"))
	return target


def writeSection(configDir, section, data):
	"""Replace one section of configDir/home.json with data, or with what data returns for the current section when
	it is a function (the other sections stay as they are)."""
	path = Path(configDir) / "home.json"
	home = json.loads(path.read_text(encoding="utf-8"))
	home[section] = data(home[section]) if callable(data) else data
	path.write_text(json.dumps(home, ensure_ascii=False), encoding="utf-8")


# The tests' stand-in for bwrap: where the configured one can't nest (inside Claude Code's sandbox) or would hide a
# fake tool under the test's tmp, a test names this one as gate_sandbox bwrap_path in its config copy's local.json
# (qg_common never looks bwrap up on PATH). It runs the command after `--` in the --chdir directory with the
# --clearenv / --setenv environment, as bwrap would, and ignores the mounts and namespaces.
STAND_IN_BWRAP = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
TAKES = {"--tmpfs": 1, "--dev": 1, "--proc": 1, "--remount-ro": 1, "--chdir": 1, "--ro-bind": 2, "--bind": 2,
    "--dev-bind": 2, "--setenv": 2}
env, cwd, at = dict(os.environ), None, 0
while at < len(args) and args[at] != "--":
    option = args[at]
    if option == "--clearenv":
        env = {}
    elif option == "--setenv":
        env[args[at + 1]] = args[at + 2]
    elif option == "--chdir":
        cwd = args[at + 1]
    at += 1 + TAKES.get(option, 0)
command = args[at + 1:]
if cwd:
    os.chdir(cwd)
try:
    os.execvpe(command[0], command, env)
except OSError as error:
    print(f"bwrap: execvp {command[0]}: {error.strerror}", file=sys.stderr)
    sys.exit(1)
'''
_standIn = []
_nests = []
_gate = []


def standInBwrap():
	"""The stand-in bwrap's absolute path (written once per process, in SESSION_DIR)."""
	if not _standIn:
		folder = Path(SESSION_DIR) / f"stand-in-bwrap-{os.getpid()}"
		folder.mkdir(parents=True, exist_ok=True)
		(folder / "bwrap").write_text(STAND_IN_BWRAP)
		(folder / "bwrap").chmod(0o755)
		_standIn.append(str(folder / "bwrap"))
	return _standIn[0]


def bwrapNests():
	"""Whether the tree's configured bwrap (gate_sandbox bwrap_path, asked through the clean copy's loader, so the
	person's local.json plays no part) can make its namespaces here."""
	if not _nests:
		loader = os.path.join(cleanHome(), "config", "homeconfig.py")
		path = subprocess.run([sys.executable, loader, "get", "gate_sandbox", "bwrap_path"], capture_output=True,
			text=True, check=True).stdout.strip()
		ok = os.access(path, os.X_OK) and subprocess.run([path, "--ro-bind", "/", "/", "--unshare-net", "true"],
			capture_output=True).returncode == 0
		_nests.append(ok)
	return _nests[0]


def testUv():
	"""The uv the tests run their fixture repos' suites with: the one on PATH. Never a path built from HOME: under a
	fake HOME (public-check.py's clean install) ~/.local/bin is empty. RuntimeError when PATH has none."""
	found = shutil.which("uv")
	if not found:
		raise RuntimeError("config_fixtures: uv is not on PATH; the gate tests run their fixture repos' suites with it")
	return os.path.abspath(found)


_toolPaths = []


def toolPaths():
	"""(read, write): the directories the tests' uv (testUv) needs inside the gate sandbox, wherever it is installed:
	its own directory (and its link target's), the managed Pythons it reports, and the cache it reports (`uv cache
	dir`, which follows UV_CACHE_DIR). gate_sandbox home_read and home_write are ~ paths of the HOME the gate sees,
	so under a fake HOME they no longer cover a uv installed under the real one. Asked once per process."""
	if not _toolPaths:
		uv = testUv()

		def report(*args):
			return subprocess.run([uv, *args], capture_output=True, text=True, check=True,
				stdin=subprocess.DEVNULL).stdout.strip()

		read = list(dict.fromkeys([os.path.dirname(uv), os.path.dirname(os.path.realpath(uv)),
			report("python", "dir")]))
		_toolPaths.append((read, [report("cache", "dir")]))
	return _toolPaths[0]


def exposeTools(configDir):
	"""configDir's home.json gate_sandbox puts the tests' tool paths (toolPaths) back as absolute paths, after its own
	home_read and home_write, so a gate command finds the tests' uv under any HOME."""
	read, write = toolPaths()

	def withTools(sandbox):
		return {**sandbox, "home_read": list(dict.fromkeys([*sandbox["home_read"], *read])),
			"home_write": list(dict.fromkeys([*sandbox["home_write"], *write]))}

	writeSection(configDir, "gate_sandbox", withTools)


def useStandIn(configDir):
	"""configDir's local.json (merged) names the stand-in as gate_sandbox bwrap_path."""
	local = Path(configDir) / "local.json"
	data = json.loads(local.read_text()) if local.exists() else {}
	data.setdefault("gate_sandbox", {})["bwrap_path"] = standInBwrap()
	local.write_text(json.dumps(data))


def standInHere(monkeypatch, homeconfig):
	"""This test process's gate sandbox is the stand-in: the config copy of its homeconfig module names it and the
	loader's cache is dropped (a fake tool under tmp, which a real bwrap hides, or a test that needs no isolation)."""
	useStandIn(homeconfig.configDir())
	monkeypatch.setattr(homeconfig, "_cache", {})


def usableSandboxHere(monkeypatch, homeconfig):
	"""The tests' tool paths put back in this test process's gate sandbox (exposeTools), and standInHere only where
	the configured bwrap can't nest (inside Claude Code's sandbox)."""
	exposeTools(homeconfig.configDir())
	monkeypatch.setattr(homeconfig, "_cache", {})
	if not bwrapNests():
		standInHere(monkeypatch, homeconfig)


def gateHome():
	"""cleanHome for programs that run gate commands: the same copy, with the tests' tool paths put back in its gate
	sandbox (exposeTools) and, where the configured bwrap can't nest, the stand-in in its local.json. Built once per
	process, in SESSION_DIR."""
	if not _gate:
		home = copyHome(TREE, Path(SESSION_DIR) / "gate-home", tops=("skills", "agents"))
		exposeTools(home / "config")
		if not bwrapNests():
			useStandIn(home / "config")
		_gate.append(home)
	return _gate[0]

"""The hooks/ layout (stage 7a): hook-entry.py, RULES.md and ruff.toml at the top, every other program and module
in one role directory (hooks/lib/homepath.py ROLE_DIRS), each file name used once, and every home program finding
its home imports from any working directory through its own sys.path lines (hooks/lib + homepath).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_layout.py -q -p no:cacheprovider'
"""

import ast
import json
import os
import subprocess
import sys

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME_ROOT = os.path.dirname(HOOKS)
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402  (the tree under test)

TOP_FILES = {"hook-entry.py", "RULES.md", "ruff.toml"}
NOT_LIVE = {"__pycache__", ".pytest_cache"}
# Home programs outside hooks/ that import hooks modules (the planning skill's scripts).
SKILL_DIRS = ("skills",)


def roleFiles():
	"""(role, file name) for every file of the role directories."""
	out = []
	for role in homepath.ROLE_DIRS:
		for name in sorted(os.listdir(os.path.join(HOOKS, role))):
			if name not in NOT_LIVE:
				out.append((role, name))
	return out


def homeModules():
	"""Module name -> path of every importable home module: hooks/<role>/*.py and config/*.py."""
	found = {}
	for role, name in roleFiles():
		if name.endswith(".py"):
			found.setdefault(name[:-3], []).append(os.path.join(HOOKS, role, name))
	config = os.path.join(HOME_ROOT, "config")
	for name in sorted(os.listdir(config)):
		if name.endswith(".py"):
			found.setdefault(name[:-3], []).append(os.path.join(config, name))
	return found


def programs():
	"""Every Python file of hooks/ (tests aside) and of the skills, outside dot-directories."""
	out = [os.path.join(HOOKS, "hook-entry.py")] + [
		os.path.join(HOOKS, role, name) for role, name in roleFiles() if name.endswith(".py")
	]
	for top in SKILL_DIRS:
		for folder, dirs, names in os.walk(os.path.join(HOME_ROOT, top)):
			dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in NOT_LIVE)
			out += [os.path.join(folder, n) for n in sorted(names) if n.endswith(".py")]
	return out


def importedNames(tree):
	names = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			names |= {alias.name.split(".")[0] for alias in node.names}
		elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
			names.add(node.module.split(".")[0])
	return names


def setsPath(node):
	"""A module-level sys.path.insert / append call, or an assignment to sys.path or a slice of it."""
	if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
		return ast.unparse(node.value.func) in ("sys.path.insert", "sys.path.append")
	if isinstance(node, ast.Assign):
		return any(ast.unparse(t.value if isinstance(t, ast.Subscript) else t) == "sys.path" for t in node.targets)
	return False


def usedNames(node):
	return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} - {"os", "sys", "Path", "__file__"}


def bootstrap(tree, source):
	"""The source of the module-level statements that set up sys.path (sys.path.insert, an assignment to it) and of
	the module-level assignments of the names they use, in file order: what the program runs before its first home
	import."""
	assigned = {}
	for node in tree.body:
		if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
			assigned[node.targets[0].id] = node
	keep = [n for n in tree.body if setsPath(n)]
	todo = set().union(*(usedNames(n) for n in keep)) if keep else set()
	while todo:
		name = todo.pop()
		node = assigned.get(name)
		if node is not None and node not in keep:
			keep.append(node)
			todo |= usedNames(node.value)
	return "\n".join(ast.get_source_segment(source, n) for n in sorted(keep, key=lambda n: n.lineno))


def test_the_top_of_hooks_holds_only_the_entry_its_rules_and_the_role_directories():
	extra = sorted(set(os.listdir(HOOKS)) - TOP_FILES - set(homepath.ROLE_DIRS) - {"tests"} - NOT_LIVE)
	assert extra == [], f"hooks/ holds {extra}: put it in a role directory (hooks/lib/homepath.py ROLE_DIRS)"


def test_every_file_name_is_used_once_across_the_role_directories_and_config():
	names = {}
	for role, name in roleFiles():
		names.setdefault(name, []).append(role)
	twice = {n: roles for n, roles in names.items() if len(roles) > 1}
	assert twice == {}
	modules = {m: paths for m, paths in homeModules().items() if len(paths) > 1}
	assert modules == {}, "an import by name would depend on the sys.path order"
	assert all(homepath.locate(name) == os.path.join(HOOKS, role, name) for role, name in roleFiles())


def test_homepath_puts_the_role_directories_and_config_on_the_path_once():
	path = ["/elsewhere"]
	homepath.install(path)
	homepath.install(path)
	assert path == [os.path.join(HOME_ROOT, d) for d in homepath.searchDirs()] + ["/elsewhere"]
	assert homepath.searchDirs()[-1] == "config" and all(os.path.isdir(os.path.join(HOME_ROOT, d)) for d in path[:-1])


def test_every_program_finds_its_home_imports_from_any_working_directory(tmp_path):
	"""Each program's own sys.path lines (the file's directory, as when it runs, plus its module-level inserts),
	run with __file__ set and nothing else on the path, from an unrelated directory: every home module it imports
	is found, in this tree. find_spec only locates; no program code runs."""
	modules = homeModules()
	checks = []
	for path in programs():
		with open(path, encoding="utf-8") as f:
			source = f.read()
		tree = ast.parse(source)
		wanted = sorted(importedNames(tree) & set(modules))
		if wanted:
			checks.append({"file": path, "setup": bootstrap(tree, source), "names": wanted})
	assert checks
	probe = (
		"import importlib.util, json, os, sys\n"
		"from pathlib import Path\n"
		"checks = json.loads(sys.argv[1])\n"
		"base = [p for p in sys.path if p not in ('', os.getcwd())]\n"
		"out = {}\n"
		"for c in checks:\n"
		"\tsys.path[:] = [os.path.dirname(c['file'])] + base\n"
		"\tfor name in [m for m in sys.modules if m not in BUILTIN]:\n"
		"\t\tdel sys.modules[name]\n"
		"\texec(c['setup'], {'__file__': c['file'], 'os': os, 'sys': sys, 'Path': Path, '__name__': 'probe'})\n"
		"\tif 'homepath' in c['names']:\n"
		"\t\timport homepath  # what the program's own `import homepath` adds\n"
		"\tfound = {n: getattr(importlib.util.find_spec(n), 'origin', None) for n in c['names']}\n"
		"\tout[c['file']] = found\n"
		"print(json.dumps(out))\n"
	)
	probe = "import sys\nBUILTIN = set(sys.modules)\n" + probe
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	result = subprocess.run([sys.executable, "-c", probe, json.dumps(checks)], capture_output=True, text=True,
		timeout=60, cwd=tmp_path, env=env)
	assert result.returncode == 0, result.stderr
	found = json.loads(result.stdout)
	wrong = {
		os.path.relpath(f, HOME_ROOT): {n: o for n, o in names.items() if o not in modules[n]}
		for f, names in found.items()
	}
	assert {f: names for f, names in wrong.items() if names} == {}

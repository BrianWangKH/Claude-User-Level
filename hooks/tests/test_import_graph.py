"""The import graph of the home's Python modules has no cycle (hooks/, config/, skills/; tests included).

An edge is any import a module can make, read statically with ast: `import x` and `from x import y` anywhere in
the file (a lazy import inside a function counts: it runs too), importlib.import_module("x"), and
importlib.util.spec_from_file_location(name, path) when the path is built from string literals naming a home
file (a module-level constant such as COST_HISTORY counts through its assignment). A name resolves to a home
file the way the scripts' sys.path does: the importer's own directory first, then hooks/lib/homepath.py's
directories (the hooks/ role directories, then config/).
Directories starting with "." (skills/.trash) are not live code. A cycle (as when wt_controls.py imported
wt_queue.py inside main(), wt_queue.py imported wt_controls.py at the top) fails with its path.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_import_graph.py -q -p no:cacheprovider'
"""

import ast
import os
import sys
import textwrap

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME_ROOT = os.path.dirname(HOOKS)
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402  (the tree under test)

SCOPE = ("hooks", "config", "skills")
SEARCH = tuple(homepath.searchDirs())


def homeFiles(root):
	"""Every .py file under SCOPE, relative to root, outside dot-directories and __pycache__."""
	out = []
	for top in SCOPE:
		for folder, dirs, names in os.walk(os.path.join(root, top)):
			dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
			out += [os.path.relpath(os.path.join(folder, n), root) for n in sorted(names) if n.endswith(".py")]
	return out


def resolveModule(name, importer, known):
	"""The home file a module name imports from importer, or None (a library)."""
	for folder in (os.path.dirname(importer), *SEARCH):
		rel = os.path.normpath(os.path.join(folder, name.split(".")[0] + ".py"))
		if rel in known:
			return rel
	return None


def resolvePath(text, importer, known):
	"""The home file a path literal names (spec_from_file_location), or None: a name next to the importer
	("cost-history.py"), else the home file the literal ends with (any home prefix: "<home>/skills/x/y.py")."""
	if not text.endswith(".py"):
		return None
	beside = os.path.normpath(os.path.join(os.path.dirname(importer), text))
	if beside in known:
		return beside
	return next((rel for rel in sorted(known) if text == rel or text.endswith("/" + rel)), None)


def literals(node):
	return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def callName(node):
	func = node.func
	return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None


def importsOf(rel, tree, known):
	"""{home file} the module at rel can import."""
	assigned = {}
	for node in tree.body:
		if isinstance(node, ast.Assign):
			for target in node.targets:
				if isinstance(target, ast.Name):
					assigned[target.id] = node.value
	names, paths = [], []
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			names += [alias.name for alias in node.names]
		elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
			names.append(node.module)
		elif isinstance(node, ast.Call) and callName(node) == "import_module" and node.args:
			names += literals(node.args[0])[:1]
		elif isinstance(node, ast.Call) and callName(node) == "spec_from_file_location" and len(node.args) > 1:
			pathNode = node.args[1]
			sources = [pathNode] + [
				assigned[n.id] for n in ast.walk(pathNode) if isinstance(n, ast.Name) and n.id in assigned
			]
			paths += [text for source in sources for text in literals(source)]
	found = {resolveModule(name, rel, known) for name in names} | {resolvePath(text, rel, known) for text in paths}
	return {f for f in found if f and f != rel}


def importGraph(root):
	known = set(homeFiles(root))
	graph = {}
	for rel in sorted(known):
		with open(os.path.join(root, rel), encoding="utf-8") as f:
			graph[rel] = importsOf(rel, ast.parse(f.read(), filename=rel), known)
	return graph


def findCycle(graph):
	"""One import cycle as [a, b, ..., a], or None."""
	state, stack = {}, []

	def visit(node):
		state[node] = "open"
		stack.append(node)
		for nxt in sorted(graph.get(node, ())):
			if state.get(nxt) == "open":
				return stack[stack.index(nxt) :] + [nxt]
			if nxt not in state:
				cycle = visit(nxt)
				if cycle:
					return cycle
		stack.pop()
		state[node] = "done"
		return None

	for node in sorted(graph):
		if node not in state:
			cycle = visit(node)
			if cycle:
				return cycle
	return None


def write(root, rel, text):
	path = os.path.join(root, rel)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		f.write(textwrap.dedent(text))


def test_the_home_modules_import_each_other_without_a_cycle():
	cycle = findCycle(importGraph(HOME_ROOT))
	assert cycle is None, "import cycle: " + " -> ".join(cycle or [])


def test_the_graph_sees_the_imports_the_home_makes():
	graph = importGraph(HOME_ROOT)
	assert "hooks/lib/wt_topics.py" in graph["hooks/worker/wt_queue.py"]
	assert "hooks/worker/wt_queue.py" in graph["hooks/worker/wt_controls.py"]  # lazy, inside main()
	assert "config/homeconfig.py" in graph["hooks/lib/runstate.py"]
	assert "skills/repo-plan/cost-history.py" in graph["hooks/guards/dispatch_guard.py"]  # spec_from_file_location
	assert "skills/repo-plan/run-result.py" in graph["skills/repo-plan/cost-history.py"]


# config-inventory.py is a home-development tool the public export leaves out (conftest.py private_layer).
@pytest.mark.private_layer("hooks/tools/config-inventory.py")
def test_the_graph_sees_a_lazy_import_of_a_lib_module_from_a_tool():
	assert "hooks/lib/mirrors.py" in importGraph(HOME_ROOT)["hooks/tools/config-inventory.py"]


def test_a_lazy_import_cycle_fails(tmp_path):
	write(tmp_path, "hooks/guards/a_mod.py", "import b_mod\n")
	write(tmp_path, "hooks/lib/b_mod.py", "def main():\n    import a_mod\n")
	write(tmp_path, "config/c_mod.py", "import json\n")
	assert findCycle(importGraph(str(tmp_path))) == ["hooks/guards/a_mod.py", "hooks/lib/b_mod.py", "hooks/guards/a_mod.py"]


def test_a_cycle_through_a_loaded_script_fails(tmp_path):
	write(
		tmp_path,
		"hooks/worker/some-hook.py",
		'TOOL = "/any/home/skills/x/some-tool.py"\nspec = u.spec_from_file_location("t", TOOL)\n',
	)
	write(tmp_path, "skills/x/some-tool.py", "import hookmod\n")
	write(tmp_path, "hooks/worker/hookmod.py", 'spec = u.spec_from_file_location("h", os.path.join(HERE, "some-hook.py"))\n')
	assert findCycle(importGraph(str(tmp_path))) == [
		"hooks/worker/hookmod.py",
		"hooks/worker/some-hook.py",
		"skills/x/some-tool.py",
		"hooks/worker/hookmod.py",
	]


def test_libraries_and_dot_directories_are_not_home_modules(tmp_path):
	write(tmp_path, "hooks/lib/a_mod.py", "import json\nimport b_mod\n")
	write(tmp_path, "skills/.trash/old/b_mod.py", "import a_mod\n")
	assert importGraph(str(tmp_path)) == {"hooks/lib/a_mod.py": set()}

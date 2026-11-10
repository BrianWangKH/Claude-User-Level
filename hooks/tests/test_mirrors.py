"""mirrors.py: a mod's hand-kept copy of its settings file (the wt-pane fallback DEFAULTS) equals the file.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_mirrors.py -q -p no:cacheprovider'
"""

import os
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import mirrors  # noqa: E402

ROOT = os.path.dirname(HOOKS)


def test_the_tree_has_the_pane_defaults_mirror():
	assert [(b["path"], b["name"]) for b in mirrors.blocks(ROOT)] == [("mods/wt-pane/hooks/register.tsx", "DEFAULTS")]


@pytest.mark.parametrize("block", mirrors.blocks(ROOT), ids=lambda b: f"{b['path']}:{b['name']}")
def test_every_mirror_equals_its_source(block):
	assert mirrors.value(block) == mirrors.sourceValue(block), f"{block['path']} {block['name']}: change both"


def test_a_block_without_its_end_marker_is_an_error(tmp_path):
	path = tmp_path / "mods" / "m" / "x.ts"
	path.parent.mkdir(parents=True)
	path.write_text("// mirror:begin D ../c.json\nconst D: C = {}\n")
	with pytest.raises(ValueError, match="no end marker"):
		mirrors.blocks(tmp_path)


def test_the_lines_of_a_block_are_listed(tmp_path):
	path = tmp_path / "mods" / "m" / "x.ts"
	path.parent.mkdir(parents=True)
	path.write_text("const A = 1\n// mirror:begin D ../c.json\nconst D: C = {}\n// mirror:end D\n")
	assert mirrors.mirroredLines(tmp_path) == {"mods/m/x.ts": {2, 3, 4}}

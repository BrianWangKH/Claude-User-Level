"""hookio.git's time limit comes from home.json hooks.git_timeout_seconds, by that key: every hook that finds its
checkout (Hook.root, Hook.mainRoot) goes through it, so a renamed or dropped key must fail loudly, never fall back
to some default.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_hookio_git.py -q -p no:cacheprovider'
"""

import json
import os
import subprocess
import sys

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(os.path.dirname(HOOKS), "config")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import homeconfig  # noqa: E402
import hookio  # noqa: E402

KEY = "git_timeout_seconds"


@pytest.fixture
def config(tmp_path, monkeypatch):
	"""A copy of the tree's config (no local.json) as the home settings."""
	directory = config_fixtures.copyConfig(CONFIG, tmp_path / "config")
	config_fixtures.useConfig(monkeypatch, directory)
	return directory


def hooksSection(directory):
	"""The hooks section as the file in directory holds it (homeconfig.path: the file of that section)."""
	assert os.path.dirname(homeconfig.path(hookio.SETTINGS)) == str(directory)
	with open(homeconfig.path(hookio.SETTINGS), encoding="utf-8") as f:
		return json.load(f)[hookio.SETTINGS]


@pytest.fixture
def repo(tmp_path, monkeypatch):
	r = tmp_path / "repo"
	r.mkdir()
	subprocess.run(["git", "init", "-q", str(r)], check=True, capture_output=True)
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(r))
	return r


def test_home_json_holds_the_git_time_limit_in_the_hooks_section(config):
	"""Unit: the key hookio reads is in home.json's hooks section, a number of seconds above 0."""
	value = hooksSection(config)[KEY]
	assert isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def test_every_git_query_of_a_hook_uses_the_configured_limit(config, repo, monkeypatch):
	"""Unit: hookio.git passes hooks.git_timeout_seconds as the subprocess timeout (set to 7 here)."""
	config_fixtures.writeSection(config, hookio.SETTINGS, {**hooksSection(config), KEY: 7})
	seen = []
	real = subprocess.run

	def spy(argv, **kwargs):
		seen.append(kwargs.get("timeout"))
		return real(argv, **kwargs)

	monkeypatch.setattr(hookio.subprocess, "run", spy)
	assert hookio.Hook({"hook_event_name": "PreToolUse"}).root() == os.path.realpath(repo)
	assert seen == [7]


def test_a_renamed_key_fails_loudly_instead_of_a_default(config, repo):
	"""Unit: without the key, finding the checkout raises a KeyError naming the key (the hook's failure policy
	then denies or warns), so a rename can't silently drop the limit."""
	section = {k: v for k, v in hooksSection(config).items() if k not in (KEY, f"_note_{KEY}")}
	config_fixtures.writeSection(config, hookio.SETTINGS, {**section, "git_timeout_secs": 5})
	with pytest.raises(KeyError, match=KEY):
		hookio.Hook({"hook_event_name": "PreToolUse"}).root()

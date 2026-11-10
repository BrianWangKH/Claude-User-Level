"""homeconfig.py: the one loader of the home settings (config/home.json + config/local.json, or config/local.test.json
in config mode TEST, and the registries in config/registry/).

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_homeconfig.py (loader: ~/.claude/config/homeconfig.py)
Each test points homeconfig.configDir at its own directory (config_fixtures.useConfig) and runs the CLI from a copy
of the loader in it, so the person's local.json, local.test.json and mode never matter.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)

import config_fixtures  # noqa: E402
import homeconfig  # noqa: E402

SCRIPT = homeconfig.__file__
assert os.path.dirname(os.path.realpath(SCRIPT)) == os.path.realpath(os.path.join(os.path.dirname(HOOKS), "config"))
REAL_CONFIG = os.path.join(os.path.dirname(HOOKS), "config")
BUDGET = {"_note_budget_headroom_factor": "x", "budget_headroom_factor": 1.25, "fallback_minutes": {"qa": 30, "developer": 30},
	"queue_quota_unknown": "wait", "queue_workers_max": 2, "nested": {"deep": {"a": 1, "b": 2}}, "list": [1, 2]}


def realHome():
	with open(os.path.join(REAL_CONFIG, "home.json"), encoding="utf-8") as f:
		return json.load(f)


@pytest.fixture
def config(tmp_path, monkeypatch):
	"""A config dir whose home.json has the budget section above and the real other sections, the real
	registries and a copy of the loader (cli runs it)."""
	d = tmp_path / "config"
	d.mkdir()
	home = realHome()
	home["budget"] = BUDGET
	(d / "home.json").write_text(json.dumps(home))
	shutil.copytree(os.path.join(REAL_CONFIG, "registry"), d / "registry")
	shutil.copy2(SCRIPT, d / "homeconfig.py")
	config_fixtures.useConfig(monkeypatch, d)
	return d


def local(config, data):
	(config / "local.json").write_text(json.dumps(data))


def cli(*args, env=None):
	"""The loader's CLI, run from the test's config directory (the copy the config fixture puts there)."""
	return subprocess.run([sys.executable, os.path.join(homeconfig.configDir(), "homeconfig.py"), *args],
		capture_output=True, text=True, timeout=30, env={**os.environ, **(env or {})})


def test_every_section_and_registry_loads_unchanged_without_local_json(tmp_path, monkeypatch):
	d = tmp_path / "copy"
	shutil.copytree(REAL_CONFIG, d, ignore=shutil.ignore_patterns(*config_fixtures.PERSONAL, "__pycache__", "*.py"))
	config_fixtures.useConfig(monkeypatch, d)
	home = realHome()
	assert sorted(k for k in home if not k.startswith("_")) == sorted(homeconfig.SECTIONS)
	for name in homeconfig.SECTIONS:
		assert homeconfig.load(name) == home[name], name
	for name in homeconfig.REGISTRIES:
		with open(os.path.join(REAL_CONFIG, "registry", f"{name}.json"), encoding="utf-8") as f:
			assert homeconfig.load(name) == json.load(f), name


def test_the_config_dir_holds_only_home_json_the_registries_and_the_loader():
	names = {n for n in os.listdir(REAL_CONFIG) if n not in ("__pycache__", ".gitignore", *config_fixtures.PERSONAL)}
	assert names == {"home.json", "homeconfig.py", "registry"}
	assert sorted(n[:-5] for n in os.listdir(os.path.join(REAL_CONFIG, "registry"))) == sorted(homeconfig.REGISTRIES)


def test_the_default_dir_is_config_next_to_the_hooks():
	"""A fresh load of the loader: conftest replaced configDir on the imported one."""
	spec = importlib.util.spec_from_file_location("homeconfig_fresh", SCRIPT)
	fresh = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(fresh)
	assert fresh.configDir() == os.path.realpath(REAL_CONFIG) == fresh.HERE


def test_use_config_redirects_the_dir_in_this_process(config):
	assert homeconfig.configDir() == str(config)
	assert homeconfig.load("budget") == BUDGET


def test_paths_name_home_json_for_sections_and_the_registry_file_for_registries(config):
	assert homeconfig.path("budget") == str(config / "home.json") == homeconfig.path(homeconfig.HOME)
	assert homeconfig.path("env") == str(config / "registry" / "env.json")
	assert homeconfig.path(homeconfig.LOCAL) == str(config / "local.json")
	assert homeconfig.where("toolchains") == f"{config / 'home.json'} section 'toolchains'"
	with pytest.raises(homeconfig.ConfigError, match="unknown home settings name"):
		homeconfig.path("budget-defaults")


def test_local_overrides_merge_dicts_and_replace_the_rest(config):
	local(config, {"budget": {"budget_headroom_factor": 2, "fallback_minutes": {"qa": 5}, "nested": {"deep": {"b": 9}},
		"list": [7]}})
	got = homeconfig.load("budget")
	assert got["budget_headroom_factor"] == 2 and got["list"] == [7]
	assert got["fallback_minutes"] == {"qa": 5, "developer": 30}
	assert got["nested"] == {"deep": {"a": 1, "b": 9}}
	assert homeconfig.get("budget", "nested.deep.b") == 9


def test_local_may_override_any_section_safety_ones_included(config):
	local(config, {"gate_sandbox": {"hide": ["/home"]}, "auto_merge_hold": {"paths": [".claude/"]},
		"prices": {"models": {"claude-opus-5-5": {"input_usd_per_mtok": 5.0}}}})
	assert homeconfig.load("gate_sandbox")["hide"] == ["/home"]
	assert homeconfig.load("auto_merge_hold")["paths"] == [".claude/"]
	assert homeconfig.load("prices")["models"]["claude-opus-5-5"]["input_usd_per_mtok"] == 5.0


def test_underscore_keys_are_comments(config):
	local(config, {"_comment": "mine", "budget": {"_why": "testing", "fallback_minutes": {"_n": 1, "qa": 6}}})
	got = homeconfig.load("budget")
	assert got["fallback_minutes"] == {"qa": 6, "developer": 30} and "_why" not in got


def test_load_returns_a_copy(config):
	homeconfig.load("budget")["fallback_minutes"]["qa"] = 0
	assert homeconfig.load("budget")["fallback_minutes"]["qa"] == 30
	homeconfig.load("events")["events"].clear()
	assert homeconfig.load("events")["events"]


def noted(data):
	"""The problems of local.json data, as {key: why}; the loader never raises for them."""
	return {p["key"]: p["why"] for p in homeconfig.problems()}


@pytest.mark.parametrize("data, key, why", [
	({"env": {"FLOW_INTEGRATION": {"default": "local"}}}, "env", "a registry (registry/env.json), not a value"),
	({"rules": {"scan_dirs": []}}, "rules", "a registry"),
	({"state-registry": {}}, "state-registry", "a registry"),
	({"budget-defaults": {"budget_headroom": 2}}, "budget-defaults", "renamed to budget"),
	({"qg_sandbox": {"hide": []}}, "qg_sandbox", "renamed to gate_sandbox"),
	({"budget-default": {"budget_headroom_factor": 2}}, "budget-default", "no such section in home.json"),
	({"budget": {"budget_headroom_x": 2}}, "budget.budget_headroom_x", "no such key in home.json"),
	({"budget": {"nested": {"deep": {"c": 1}}}}, "budget.nested.deep.c", "no such key in home.json"),
	({"budget": {"budget_headroom_factor": {"x": 1}}}, "budget.budget_headroom_factor", "an object where home.json has a number"),
	({"budget": {"budget_headroom_factor": "2"}}, "budget.budget_headroom_factor", "a string where home.json has a number"),
	({"budget": {"budget_headroom_factor": True}}, "budget.budget_headroom_factor", "a boolean where home.json has a number"),
	({"budget": {"budget_headroom_factor": -1}}, "budget.budget_headroom_factor", "below 0"),
	({"budget": {"queue_workers_max": 1.5}}, "budget.queue_workers_max", "not a whole number"),
	({"budget": {"list": "x"}}, "budget.list", "a string where home.json has a list"),
	({"quality": {"hook_timeouts": {"commit_gate_seconds": 2701}}}, "quality.hook_timeouts.commit_gate_seconds",
		"above its settings.json ceiling 2700 s"),
	({"budget": 3}, "budget", "a section must be an object"),
])
def test_an_invalid_local_value_falls_back_to_home_and_is_listed(config, data, key, why):
	local(config, data)
	assert homeconfig.load("budget") == BUDGET  # every reader gets home.json's value
	home = json.loads((config / "home.json").read_text())
	assert all(homeconfig.load(s) == home[s] for s in homeconfig.SECTIONS)
	assert why in noted(config)[key]
	result = cli("check")
	assert result.returncode == 1 and key in result.stderr and "use the home.json value" in result.stderr


def test_valid_and_invalid_values_mix_per_value(config):
	local(config, {"budget": {"budget_headroom_factor": 2, "queue_workers_max": "x", "fallback_minutes": {"qa": 5, "nope": 1}}})
	got = homeconfig.load("budget")
	assert got["budget_headroom_factor"] == 2 and got["queue_workers_max"] == 2
	assert got["fallback_minutes"] == {"qa": 5, "developer": 30}
	assert sorted(noted(config)) == ["budget.fallback_minutes.nope", "budget.queue_workers_max"]


@pytest.mark.parametrize("data, old, new", [
	({"budget": {"budget_headroom": 2}}, "budget.budget_headroom", "budget.budget_headroom_factor"),
	({"budget": {"minutes": {"qa": 5}}}, "budget.minutes", "budget.fallback_minutes"),
	({"budget": {"worker_max_phases": 2}}, "budget.worker_max_phases", "budget.worker_phases_max"),
	({"budget": {"estimate_window": 5}}, "budget.estimate_window", "budget.estimate_window_count"),
	({"budget": {"queue_max_workers": 1}}, "budget.queue_max_workers", "budget.queue_workers_max"),
	({"quality": {"marker_max_new": 1}}, "quality.marker_max_new", "quality.marker_new_max"),
	({"quality": {"developer_blocks": 1}}, "quality.developer_blocks", "quality.developer_blocks_max"),
	({"quality": {"hook_timeout_seconds": {"mutate_request": 1}}}, "quality.hook_timeout_seconds", "quality.hook_timeouts"),
	({"quality": {"hook_timeouts": {"review_handback": 1}}}, "quality.hook_timeouts.review_handback",
		"quality.hook_timeouts.review_handback_seconds"),
	({"prices": {"models": {"claude-opus-5-5": {"cache_read": 1.0}}}}, "prices.models.claude-opus-5-5.cache_read",
		"prices.models.claude-opus-5-5.cache_read_usd_per_mtok"),
])
def test_an_old_key_name_falls_back_to_home_naming_the_new_one(config, data, old, new):
	home = json.loads((config / "home.json").read_text())
	home["budget"] = realHome()["budget"]
	(config / "home.json").write_text(json.dumps(home))
	local(config, data)
	section = old.split(".")[0]
	assert homeconfig.load(section) == home[section]
	assert noted(config) == {old: f"renamed to {new}"}
	result = cli("check")
	assert result.returncode == 1 and new in result.stderr


@pytest.mark.parametrize("text, why", [('{"budget": ', "not valid JSON"), ("[1]", "not a JSON object of sections")])
def test_a_broken_local_json_means_home_for_everything(config, text, why):
	(config / "local.json").write_text(text)
	assert homeconfig.load("budget") == BUDGET
	assert why in noted(config)["local.json"]
	assert homeconfig.source("budget", "budget_headroom_factor") == "home (local invalid)"


@pytest.mark.parametrize("home, words", [
	({"budget": {}}, ["section(s)", "quality", "missing"]),
	({**{s: {} for s in homeconfig.SECTIONS}, "extra": {}}, ["unknown section(s) extra"]),
	([], ["must be a JSON object"]),
	({**{s: {} for s in homeconfig.SECTIONS}, "quality": {"hook_timeouts": {"commit_gate_seconds": 99999}}},
		["quality.hook_timeouts.commit_gate_seconds above its settings.json ceiling"]),
])
def test_a_broken_home_json_fails_loud(config, home, words):
	(config / "home.json").write_text(json.dumps(home))
	with pytest.raises(homeconfig.ConfigError) as error:
		homeconfig.load("budget")
	for word in words:
		assert word in str(error.value)


def test_a_missing_home_json_is_an_error_not_an_empty_default(config):
	(config / "home.json").rename(config / "moved.json")
	with pytest.raises(FileNotFoundError):
		homeconfig.load("quality")


def test_an_unknown_or_old_name_fails(config):
	with pytest.raises(homeconfig.ConfigError, match="unknown home settings name"):
		homeconfig.load("flow-templates")
	with pytest.raises(homeconfig.ConfigError, match="is now the section 'quality'"):
		homeconfig.load("quality-defaults")


def test_source_says_local_only_for_what_local_json_sets(config):
	local(config, {"budget": {"queue_workers_max": 1, "nested": {"deep": {"a": 5}}, "fallback_minutes": {"qa": "x"}}})
	assert homeconfig.source("budget", "queue_workers_max") == "local"
	assert homeconfig.source("budget", "budget_headroom_factor") == "home"
	assert homeconfig.source("budget", "nested.deep.a") == "local"
	assert homeconfig.source("budget", "nested.deep.b") == "home"
	assert homeconfig.source("budget", "fallback_minutes.qa") == "home (local invalid)"
	assert homeconfig.source("budget", "fallback_minutes") == "home (local invalid)"
	assert homeconfig.source("budget", "fallback_minutes.developer") == "home"
	with pytest.raises(KeyError):
		homeconfig.source("budget", "nope")
	with pytest.raises(homeconfig.ConfigError):
		homeconfig.source("env", "FLOW_INTEGRATION")


def test_defaults_only_leaves_local_json_out(config):
	local(config, {"budget": {"queue_workers_max": 1}})
	assert homeconfig.get("budget", "queue_workers_max") == 1
	assert homeconfig.get("budget", "queue_workers_max", defaultsOnly=True) == 2


def test_renamed_key_maps_old_paths_and_old_leaves_under_new_parents():
	assert homeconfig.renamedKey(("budget", "budget_headroom")) == "budget.budget_headroom_factor"
	assert homeconfig.renamedKey(("quality", "hook_timeout_seconds", "developer_gate")) is None
	assert homeconfig.renamedKey(("quality", "hook_timeouts", "developer_gate")) == "quality.hook_timeouts.developer_gate_seconds"
	assert homeconfig.renamedKey(("prices", "models", "m-1", "input")) == "prices.models.m-1.input_usd_per_mtok"
	assert homeconfig.renamedKey(("budget", "budget_headroom_factor")) is None
	assert homeconfig.renamedKey(("budget", "fallback_minutes", "qa")) is None


def test_every_renamed_key_exists_in_home_json_under_its_new_name():
	home = realHome()
	for _old, new in homeconfig.RENAMED_KEYS:
		node = home
		for part in new:
			node = next(iter(v for k, v in node.items() if not k.startswith("_"))) if part == "*" else node[part]


def test_loosened_is_empty_without_a_safety_override(config):
	assert homeconfig.loosened() == []
	local(config, {"gate_sandbox": {"hide": realHome()["gate_sandbox"]["hide"] + ["/opt"], "home_read": []},
		"auto_merge_hold": {"paths": realHome()["auto_merge_hold"]["paths"] + ["x/"]}})
	assert homeconfig.loosened() == []  # stricter is never a loosening


@pytest.mark.parametrize("override, key, change", [
	({"gate_sandbox": {"hide": ["/home"]}}, "gate_sandbox.hide", "removes /Users, /Volumes, /media, /mnt, /root, /run/WSL, /run/media, /run/user, /srv"),
	({"gate_sandbox": {"protect": [".git"]}}, "gate_sandbox.protect", "removes .claude"),
	({"gate_sandbox": {"hide_inside": ["plans"]}}, "gate_sandbox.hide_inside", "removes .hook-only"),
	({"gate_sandbox": {"home_write": ["~/.cache/uv", "~/.cache/go-build", "~/go", "~"]}}, "gate_sandbox.home_write", "adds ~"),
	({"gate_sandbox": {"home_read": ["~/.local/bin", "~/.local/share/uv/python", "~/godot", "~/.ssh"]}}, "gate_sandbox.home_read", "adds ~/.ssh"),
	({"gate_sandbox": {"env_pass": ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME", "AWS_SECRET"]}},
		"gate_sandbox.env_pass", "adds AWS_SECRET"),
	({"gate_sandbox": {"env_set": {"UV_OFFLINE": "0"}}}, "gate_sandbox.env_set", "changes UV_OFFLINE"),
	({"auto_merge_hold": {"paths": [".claude/"]}}, "auto_merge_hold.paths", None),
])
def test_loosened_names_each_safety_value_local_json_makes_less_strict(config, override, key, change):
	local(config, override)
	found = homeconfig.loosened()
	assert [item["key"] for item in found] == [key]
	assert found[0]["home"] == realHome()[key.split(".")[0]][key.split(".")[1]]
	assert found[0]["effective"] == homeconfig.load(key.split(".")[0])[key.split(".")[1]]
	if change:
		assert found[0]["change"] == change
	else:
		assert found[0]["change"].startswith("removes ") and ".mcp.json" in found[0]["change"]


def test_loosened_names_a_widened_home_draft_small_change_allowlist(config):
	"""The small-change allowlist loosens by growing, its exclusions by shrinking."""
	eligible = realHome()["hooks"]["home_draft_small_paths"]
	local(config, {"hooks": {"home_draft_small_paths": eligible + ["hooks/**"]}})
	found = homeconfig.loosened()
	assert [(item["key"], item["change"]) for item in found] == [("hooks.home_draft_small_paths", "adds hooks/**")]


def test_loosened_names_a_dropped_home_draft_small_change_exclusion(config):
	excluded = realHome()["hooks"]["home_draft_small_excluded_paths"]
	local(config, {"hooks": {"home_draft_small_excluded_paths": [p for p in excluded if p != "agents/home-*.md"]}})
	found = homeconfig.loosened()
	assert [(item["key"], item["change"]) for item in found] == [
		("hooks.home_draft_small_excluded_paths", "removes agents/home-*.md")]


def test_a_shorter_allowlist_or_more_exclusions_are_no_loosening(config):
	"""Entries compare as text: any entry home.json lacks, even a narrower pattern, is announced; dropping one is not."""
	local(config, {"hooks": {"home_draft_small_paths": [], "home_draft_small_excluded_paths":
		realHome()["hooks"]["home_draft_small_excluded_paths"] + ["README.md"]}})
	assert homeconfig.loosened() == []


def test_cli_get_prints_scalars_plain_and_the_rest_as_json(config):
	assert cli("get", "budget", "budget_headroom_factor").stdout == "1.25\n"
	assert cli("get", "budget", "queue_quota_unknown").stdout == "wait\n"
	nested = cli("get", "budget", "fallback_minutes")
	assert nested.returncode == 0 and json.loads(nested.stdout) == BUDGET["fallback_minutes"]
	assert cli("get", "budget", "fallback_minutes.qa").stdout == "30\n"
	assert cli("get", "hooks", "error_chars").stdout == "300\n"


def test_cli_get_applies_local_json(config):
	local(config, {"budget": {"fallback_minutes": {"qa": 12}}})
	assert cli("get", "budget", "fallback_minutes.qa").stdout == "12\n"


def test_cli_get_a_missing_key_exits_1_naming_it(config):
	result = cli("get", "budget", "fallback_minutes.verifier")
	assert result.returncode == 1 and result.stdout == ""
	assert "'fallback_minutes.verifier'" in result.stderr and homeconfig.where("budget") in result.stderr


def test_cli_get_an_old_section_name_exits_1_naming_the_new_one(config):
	result = cli("get", "budget-defaults", "budget_headroom")
	assert result.returncode == 1 and "'budget'" in result.stderr


def test_cli_check(config):
	assert cli("check").returncode == 0
	local(config, {"prices": {"models": {"claude-opus-5-5": {"input_usd_per_mtok": 5.0}}}})
	ok = cli("check")
	assert ok.returncode == 0 and "local.json" in ok.stdout
	assert homeconfig.load("prices")["models"]["claude-opus-5-5"]["input_usd_per_mtok"] == 5.0


# -- environment variables (config/registry/env.json) ------------------------------------------------

def registry(config, entries):
	with open(homeconfig.path("env"), "w", encoding="utf-8") as f:
		json.dump({"_comment": "x", **entries}, f)


def test_every_real_registry_entry_is_valid():
	for name in homeconfig.load("env"):
		if not name.startswith("_"):
			homeconfig._envEntry(name)


PICKED = "HOME_TEST_PICK"  # a variable with an allowed list, registered in the test's config copy only


def withPicked(config):
	"""The config copy's registry/env.json with PICKED added (default origin, allowed origin and local)."""
	with open(homeconfig.path("env"), encoding="utf-8") as f:
		entries = json.load(f)
	registry(config, {**{k: v for k, v in entries.items() if k != "_comment"}, PICKED: {"source": "internal",
		"purpose": "a test's", "set_by": "the test", "read_by": "the test", "default": "origin",
		"allowed": ["origin", "local"]}})


@pytest.mark.parametrize("value", [None, ""])
def test_env_unset_or_empty_gives_the_registry_default(config, monkeypatch, tmp_path, value):
	withPicked(config)
	monkeypatch.delenv(PICKED, raising=False)
	if value is not None:
		monkeypatch.setenv(PICKED, value)
	assert homeconfig.env(PICKED) == "origin"
	assert homeconfig.flowMode(str(tmp_path)) == "origin"  # outside git: the default


def test_env_a_null_default_gives_none(config, monkeypatch):
	monkeypatch.delenv("WT_MAIN_DIR", raising=False)
	assert homeconfig.env("WT_MAIN_DIR") is None


@pytest.mark.parametrize("value", ["origin", "local"])
def test_env_gives_the_set_value(config, monkeypatch, value):
	withPicked(config)
	monkeypatch.setenv(PICKED, value)
	assert homeconfig.env(PICKED) == value


@pytest.mark.parametrize("value", ["Local", "LOCAL", "pr", " local"])
def test_env_a_value_outside_allowed_fails_naming_it(config, monkeypatch, value):
	withPicked(config)
	monkeypatch.setenv(PICKED, value)
	with pytest.raises(homeconfig.ConfigError) as error:
		homeconfig.env(PICKED)
	message = str(error.value)
	assert PICKED in message and repr(value) in message and "origin, local" in message


@pytest.mark.parametrize("value", ["origin", "local", "bogus", ""])
def test_flow_integration_is_no_source_of_the_flow_mode(config, monkeypatch, tmp_path, value):
	"""The variable the tests once used as an override (and the pytest mark it needed) changes nothing: outside
	git the mode is the default, whatever it holds."""
	monkeypatch.setenv("FLOW_INTEGRATION", value)
	assert homeconfig.flowMode(str(tmp_path)) == homeconfig.FLOW_DEFAULT


@pytest.mark.parametrize("name", ["FLOW_INTEGRATON", "_comment"])
def test_env_an_unregistered_name_fails(config, name):
	with pytest.raises(homeconfig.ConfigError, match="not registered"):
		homeconfig.env(name)


@pytest.mark.parametrize("entry, words", [
	({"source": "repo", "default": None}, ["repo-provided", "needs a default"]),
	({"source": "repo", "default": "x", "allowed": ["a", "b"]}, ["allowed must be a list containing its default"]),
	({"source": "elsewhere", "default": None}, ["source must be one of"]),
])
def test_env_a_malformed_registry_entry_fails(config, monkeypatch, entry, words):
	registry(config, {"X_VAR": entry})
	monkeypatch.delenv("X_VAR", raising=False)
	with pytest.raises(homeconfig.ConfigError) as error:
		homeconfig.env("X_VAR")
	for word in words:
		assert word in str(error.value)


def test_cli_env(config):
	withPicked(config)
	assert cli("env", PICKED, env={PICKED: ""}).stdout == "origin\n"
	assert cli("env", PICKED, env={PICKED: "local"}).stdout == "local\n"
	bad = cli("env", PICKED, env={PICKED: "Local"})
	assert bad.returncode == 1 and bad.stdout == "" and "'Local'" in bad.stderr
	unknown = cli("env", "NOT_REGISTERED")
	assert unknown.returncode == 1 and unknown.stdout == "" and "not registered" in unknown.stderr


# -- config mode (config/mode: DEV or TEST) ----------------------------------------------------------

def mode(config, text):
	(config / homeconfig.MODE_FILE).write_text(text)


def everything():
	"""What every reader sees: each section, each problem, each source of a few keys, and check()'s outcome."""
	out = {name: homeconfig.load(name) for name in homeconfig.SECTIONS}
	out["problems"] = homeconfig.problems()
	out["source"] = [homeconfig.source("budget", key) for key in ("budget_headroom_factor", "fallback_minutes.qa", "nested.deep.a")]
	try:
		homeconfig.check()
		out["check"] = "ok"
	except homeconfig.ConfigError as error:
		out["check"] = str(error)
	return out


# local.json cases that cover each kind of answer the loader gives: absent, valid, invalid per value, broken.
LOCAL_CASES = {
	"absent": None,
	"empty": "{}",
	"valid": json.dumps({"budget": {"budget_headroom_factor": 1.5, "nested": {"deep": {"a": 7}}}}),
	"invalid value": json.dumps({"budget": {"budget_headroom_factor": "x", "fallback_minutes": {"qa": 12}}}),
	"unknown key": json.dumps({"budget": {"nope": 1}, "toolchains": {"tools": {"uv": "/x/uv"}}}),
	"old names": json.dumps({"budget-defaults": {}, "budget": {"minutes": {"qa": 1}}}),
	"registry": json.dumps({"env": {}}),
	"broken": "{not json",
	"not an object": "[1, 2]",
}


@pytest.mark.parametrize("case", sorted(LOCAL_CASES))
@pytest.mark.parametrize("modeText", ["DEV", "DEV\n", " DEV \n"])
def test_dev_mode_reads_exactly_what_no_mode_file_reads(config, case, modeText):
	if LOCAL_CASES[case] is not None:
		(config / "local.json").write_text(LOCAL_CASES[case])
	(config / "local.test.json").write_text(json.dumps({"budget": {"budget_headroom_factor": 9.0}}))  # never read in DEV
	assert homeconfig.configMode() == "DEV"
	without = everything()
	mode(config, modeText)
	assert homeconfig.configMode() == "DEV"
	assert everything() == without
	assert homeconfig.path(homeconfig.LOCAL) == str(config / "local.json")


def test_test_mode_reads_local_test_json_in_place_of_local_json(config):
	local(config, {"budget": {"budget_headroom_factor": 1.5, "queue_workers_max": 5}})
	(config / "local.test.json").write_text(json.dumps({"budget": {"budget_headroom_factor": 2.0}}))
	mode(config, "TEST\n")
	assert homeconfig.configMode() == "TEST"
	assert homeconfig.path(homeconfig.LOCAL) == str(config / "local.test.json")
	budget = homeconfig.load("budget")
	assert budget["budget_headroom_factor"] == 2.0 and budget["queue_workers_max"] == BUDGET["queue_workers_max"]
	assert homeconfig.source("budget", "budget_headroom_factor") == "local"
	assert homeconfig.source("budget", "queue_workers_max") == "home"  # local.json's 5 is not read
	assert homeconfig.load("budget", defaultsOnly=True) == BUDGET


def test_test_mode_without_local_test_json_is_an_error_never_local_json(config):
	local(config, {"budget": {"budget_headroom_factor": 1.5}})
	mode(config, "TEST")
	for call in (lambda: homeconfig.load("budget"), homeconfig.check, homeconfig.problems):
		with pytest.raises(homeconfig.ConfigError) as error:
			call()
		assert "local.test.json" in str(error.value) and "missing" in str(error.value)
	assert homeconfig.load("env") == homeconfig.load("env")  # registries don't depend on the mode


@pytest.mark.parametrize("text, words", [
	("{not json", "not valid JSON"),
	("[1, 2]", "not a JSON object"),
	(json.dumps({"budget": {"budget_headroom_factor": "x"}}), "budget.budget_headroom_factor invalid"),
	(json.dumps({"budget": {"nope": 1}}), "budget.nope invalid"),
	(json.dumps({"env": {}}), "env invalid"),
])
def test_test_mode_with_a_broken_local_test_json_is_an_error_never_a_fallback(config, text, words):
	local(config, {"budget": {"budget_headroom_factor": 1.5}})
	(config / "local.test.json").write_text(text)
	mode(config, "TEST")
	with pytest.raises(homeconfig.ConfigError) as error:
		homeconfig.load("budget")
	assert words in str(error.value) and "local.test.json" in str(error.value)
	result = cli("check")
	assert result.returncode == 1 and words in result.stderr
	assert cli("get", "budget", "budget_headroom_factor").returncode == 1


@pytest.mark.parametrize("text", ["test", "PROD", "", "DEV TEST", "local"])
def test_an_unknown_config_mode_is_an_error_never_dev(config, text):
	(config / "local.test.json").write_text("{}")
	mode(config, text)
	with pytest.raises(homeconfig.ConfigError, match="config mode .* is not one of DEV, TEST"):
		homeconfig.configMode()
	with pytest.raises(homeconfig.ConfigError, match="is not one of DEV, TEST"):
		homeconfig.load("budget")
	result = cli("check")
	assert result.returncode == 1 and "is not one of DEV, TEST" in result.stderr


def test_cli_check_prints_the_config_mode_first(config):
	result = cli("check")
	assert result.returncode == 0
	assert result.stdout.splitlines()[0] == f"config mode: DEV (overrides from {config / 'local.json'})"
	(config / "local.test.json").write_text("{}")
	mode(config, "TEST")
	result = cli("check")
	assert result.returncode == 0
	assert result.stdout.splitlines()[0] == f"config mode: TEST (overrides from {config / 'local.test.json'})"


def test_a_mode_switch_is_seen_by_a_running_process(config):
	(config / "local.test.json").write_text(json.dumps({"budget": {"budget_headroom_factor": 2.0}}))
	assert homeconfig.get("budget", "budget_headroom_factor") == 1.25
	mode(config, "TEST")
	assert homeconfig.get("budget", "budget_headroom_factor") == 2.0
	mode(config, "DEV")
	assert homeconfig.get("budget", "budget_headroom_factor") == 1.25


# -- no variable moves the config or the home state ----------------------------------------------------

OLD_DIR_VARIABLES = ("CLAUDE_HOME_CONFIG_DIR", "CLAUDE_HOME_STATE_DIR")
STATE_PROBE = ("import os, sys; sys.path.insert(0, sys.argv[1]); import homepath, homeconfig, statefile; "
	"print(homeconfig.configDir()); print(statefile.homeStateDir()); print(statefile.pathOf(None, 'quota-usage'))")


def test_the_old_dir_variables_change_nothing(tmp_path):
	"""A home program run with CLAUDE_HOME_CONFIG_DIR and CLAUDE_HOME_STATE_DIR set behaves exactly as without them."""
	home = config_fixtures.copyHome(os.path.dirname(HOOKS), tmp_path / "home")
	(home / "config" / "local.json").write_text(json.dumps({"budget": {"budget_headroom_factor": 1.5}}))
	elsewhere = config_fixtures.copyConfig(REAL_CONFIG, tmp_path / "elsewhere")
	(elsewhere / "local.json").write_text(json.dumps({"budget": {"budget_headroom_factor": 3.0}}))
	(elsewhere / "mode").write_text("BROKEN")
	loader = str(home / "config" / "homeconfig.py")
	snapshot = str(home / "hooks" / "budget" / "quota-snapshot.py")
	usage = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 1}}})
	runs = [
		([loader, "get", "budget", "budget_headroom_factor"], None),
		([loader, "check"], None),
		([loader, "env", "FLOW_INTEGRATION"], None),
		(["-c", STATE_PROBE, str(home / "hooks" / "lib")], None),
		([snapshot], usage),
	]
	base = {k: v for k, v in os.environ.items() if k not in OLD_DIR_VARIABLES}
	moved = {**base, "CLAUDE_HOME_CONFIG_DIR": str(elsewhere), "CLAUDE_HOME_STATE_DIR": str(tmp_path / "state-elsewhere")}

	def outcome(argv, stdin, env):
		result = subprocess.run([sys.executable, *argv], input=stdin, capture_output=True, text=True, timeout=60, env=env)
		return result.returncode, result.stdout, result.stderr

	for argv, stdin in runs:
		assert outcome(argv, stdin, moved) == outcome(argv, stdin, base), argv
	assert outcome(runs[0][0], None, moved)[1] == "1.5\n"
	probe = outcome(runs[3][0], None, moved)[1].splitlines()
	assert probe == [str(home / "config"), str(home / "state"), str(home / "state" / "quota" / "usage.json")]
	assert (home / "state" / "quota" / "usage.json").is_file() and not (tmp_path / "state-elsewhere").exists()
	for name in OLD_DIR_VARIABLES:
		with pytest.raises(homeconfig.ConfigError, match="not registered"):
			homeconfig.env(name)

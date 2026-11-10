"""Home toolchain profiles (config/toolchains.json) under a repo's quality-gates.json languages.

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_toolchains.py
Each test points homeconfig.configDir at a copy of the tree's config (config_fixtures.useConfig), so the
person's local.json never matters, and puts fake uv/go/gofmt first on PATH, so no result depends on what this machine has
installed; loadConfig cases commit the repo file, as the gates read it at the diff base.
"""
import json
import os
import subprocess
import sys

import pytest

import config_fixtures

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import homeconfig  # noqa: E402
import qg_common  # noqa: E402

REAL_CONFIG = os.path.join(os.path.dirname(HOOKS), "config")
TOOLS = ("uv", "go", "gofmt")
pytestmark = pytest.mark.flow_mode("origin")

PROFILES = {
	"tools": {"_comment": "x", "uv": "uv", "go": "go", "gofmt": "gofmt"},
	"languages": {
		"python": {"_comment": "x", "extensions": [".py"], "test": ["{uv}", "run", "pytest", "-x"],
			"coverage": ["{uv}", "run", "pytest", "--cov", "--cov-report=lcov:{report}"],
			"invalid_exit_codes": [2, 3, 4, 5], "red_invalid_patterns": ["SyntaxError"],
			"scratch_exclude": ["__pycache__", ".pytest_cache"]},
		"go": {"extensions": [".go"], "go_bin": "{go}", "test": ["{go}", "test", "./..."],
			"checks": [{"argv": ["{gofmt}", "-l", "."], "empty_output": True}, ["{go}", "vet", "./..."]],
			"scratch_exclude": ["__pycache__"]},
	},
}


def executable(path):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as f:
		f.write("#!/bin/sh\n")
	os.chmod(path, 0o755)
	return str(path)


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
	"""A directory with executable uv, go and gofmt, first on PATH; returns {tool: its path}."""
	d = tmp_path / "bin"
	paths = {tool: executable(d / tool) for tool in TOOLS}
	monkeypatch.setenv("PATH", os.pathsep.join([str(d), *os.get_exec_path()]))
	return paths


@pytest.fixture(autouse=True)
def isolatedConfig(tmp_path, monkeypatch):
	"""Every test reads a copy of the tree's config without local.json (module doc): the person's own
	overrides, such as pinned tool paths, never change a result."""
	d = config_fixtures.copyConfig(REAL_CONFIG, tmp_path / "config")
	config_fixtures.useConfig(monkeypatch, d)
	return d


@pytest.fixture
def config(isolatedConfig, fakebin):
	"""The isolated config dir with the toolchains section replaced by PROFILES."""
	config_fixtures.writeSection(isolatedConfig, "toolchains", PROFILES)
	return isolatedConfig


def local(config, data):
	(config / "local.json").write_text(json.dumps(data))


def git(root, *args):
	return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
	"""A repo whose origin/main holds .claude/quality-gates.json as given (commit(config))."""
	root = tmp_path / "repo"
	root.mkdir()
	git(root, "init", "-q", "-b", "feature/x/topic")
	git(root, "config", "user.email", "t@t")
	git(root, "config", "user.name", "t")

	def commit(gates):
		(root / ".claude").mkdir(exist_ok=True)
		(root / qg_common.CONFIG_FILE).write_text(json.dumps(gates))
		git(root, "add", ".")
		git(root, "commit", "-q", "-m", "gates")
		git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
		return str(root)

	return commit


def commands(langConfig):
	"""Every argv list of a language's command keys, and go_bin as a one-item argv."""
	found = [langConfig[k] for k in ("coverage", "test") if k in langConfig]
	found += [argv for argv, _ in qg_common.checkCommands("x", langConfig)]
	return found + ([[langConfig["go_bin"]]] if "go_bin" in langConfig else [])


def test_the_real_profiles_load_and_resolve_to_absolute_paths(fakebin):
	toolchains = homeconfig.load("toolchains")
	languages, _ = qg_common.languageConfigs({lang: {} for lang in toolchains["languages"] if not lang.startswith("_")})
	assert set(languages) == {"python", "go"}
	for lang, merged in languages.items():
		assert merged["extensions"] and merged["test"] and merged["coverage"], lang
		assert not any(k.startswith("_") for k in merged), lang
		assert all(argv[0] in fakebin.values() for argv in commands(merged)), lang


def test_a_repo_key_replaces_the_profile_key_and_lists_replace(config, fakebin):
	languages, _ = qg_common.languageConfigs({"python": {"test": ["pytest"], "red_invalid_patterns": []}})
	python = languages["python"]
	assert python["test"] == ["pytest"]
	assert python["red_invalid_patterns"] == []
	assert python["invalid_exit_codes"] == [2, 3, 4, 5]
	assert python["coverage"] == [fakebin["uv"], "run", "pytest", "--cov", "--cov-report=lcov:{report}"]
	assert "_comment" not in python and "scratch_exclude" not in python


def test_a_repo_spelling_out_every_key_is_unchanged_and_needs_no_tool(config, monkeypatch, tmp_path):
	monkeypatch.setenv("PATH", str(tmp_path / "empty"))
	entry = {"extensions": [".go"], "cwd": "mod", "go_bin": "/opt/go/bin/go", "test": ["/opt/go/bin/go", "test"],
		"coverage": ["~/go", "{report}"], "checks": [["/opt/go/bin/go", "vet", "./..."]], "env": {"X": "{repo}/x"}}
	languages, _ = qg_common.languageConfigs({"go": entry})
	assert languages["go"] == entry


def test_bare_tool_names_resolve_on_path_to_absolute_paths(config, fakebin):
	languages, _ = qg_common.languageConfigs({"go": {}})
	assert languages["go"]["go_bin"] == fakebin["go"]
	assert qg_common.checkCommands("go", languages["go"]) == [([fakebin["gofmt"], "-l", "."], True),
		([fakebin["go"], "vet", "./..."], False)]


def test_local_json_tool_paths_are_used_and_home_expanded(config, monkeypatch, tmp_path, fakebin):
	home = tmp_path / "home"
	uv = executable(home / ".local" / "bin" / "uv")
	go = executable(tmp_path / "opt" / "go" / "bin" / "go")
	monkeypatch.setenv("HOME", str(home))
	local(config, {"toolchains": {"tools": {"uv": "~/.local/bin/uv", "go": go}}})
	languages, _ = qg_common.languageConfigs({"python": {}, "go": {}})
	assert languages["python"]["test"][0] == uv
	assert languages["go"]["go_bin"] == go
	assert languages["go"]["checks"][0]["argv"][0] == fakebin["gofmt"]


@pytest.mark.parametrize("program,why", [
	("/nowhere/go", "is not an executable file"),
	("~/nowhere/go", "is not an executable file"),
	("bin/go", "neither a bare name nor an absolute or ~ path"),
])
def test_a_tool_path_that_does_not_exist_fails_naming_the_fix(config, program, why):
	local(config, {"toolchains": {"tools": {"go": program}}})
	with pytest.raises(homeconfig.ConfigError) as error:
		qg_common.languageConfigs({"go": {}})
	message = str(error.value)
	assert "tool {go} needed by languages.go.test[0]" in message and why in message
	assert '{"toolchains": {"tools": {"go": "<absolute path>"}}}' in message and "local.json" in message


def test_a_bare_name_not_on_path_fails_naming_tool_and_key(config, monkeypatch, tmp_path):
	monkeypatch.setenv("PATH", str(tmp_path / "empty"))
	with pytest.raises(homeconfig.ConfigError, match=r"tool \{uv\} needed by languages\.python\.coverage\[0\]: "
			r"'uv' is not on PATH"):
		qg_common.languageConfigs({"python": {}})


def test_a_missing_tool_no_command_uses_is_not_resolved(config):
	local(config, {"toolchains": {"tools": {"gofmt": "/nowhere/gofmt"}}})
	languages, _ = qg_common.languageConfigs({"go": {"checks": []}, "python": {}})
	assert languages["go"]["checks"] == []


def test_local_json_cannot_add_a_tool(config, fakebin):
	"""An unknown tool is an invalid local value: ignored (home's tools apply) and listed as a problem."""
	local(config, {"toolchains": {"tools": {"ruff": "ruff"}}})
	qg_common.languageConfigs({"python": {}})
	assert "ruff" not in homeconfig.load("toolchains")["tools"]
	assert [(p["key"], p["why"].startswith("no such key")) for p in homeconfig.problems()] == [("toolchains.tools.ruff", True)]


def test_repo_commands_may_use_tool_placeholders_too(config, fakebin):
	languages, _ = qg_common.languageConfigs({"python": {"env": {"V": "{repo}/.venv"},
		"select": {"pytest": ["{uv}", "run", "pytest"], "python": ["{uv}", "run", "python"], "fallback": "{component}/tests"}}})
	python = languages["python"]
	assert python["env"] == {"V": "{repo}/.venv"}
	assert python["select"] == {"pytest": [fakebin["uv"], "run", "pytest"], "python": [fakebin["uv"], "run", "python"],
		"fallback": "{component}/tests"}


@pytest.mark.parametrize("key,value,name", [
	("test", ["{vu}", "run", "pytest"], "vu"),
	("coverage", ["uv", "--cov-report=lcov:{reprot}"], "reprot"),
	("checks", [{"argv": ["{ruff}", "check"], "empty_output": False}], "ruff"),
	("env", {"X": "{home}"}, "home"),
	("test", ["pytest", "--cov={changed_dirs}"], "changed_dirs"),
	("checks", [["lint", "{changed_dirs}"]], "changed_dirs"),
])
def test_an_unknown_placeholder_fails_naming_it(config, key, value, name):
	with pytest.raises(homeconfig.ConfigError, match=rf"\{{{name}\}} in languages\.python\.{key}"):
		qg_common.languageConfigs({"python": {key: value}})


def test_changed_dirs_is_a_coverage_placeholder_and_the_python_profile_derives_its_targets(fakebin):
	languages, _ = qg_common.languageConfigs({"python": {}, "go": {}, "x": {"coverage": ["c", "--cov={changed_dirs}"]}})
	assert languages["x"]["coverage"] == ["c", "--cov={changed_dirs}"]
	assert qg_common.derivesTargets(languages["python"]) and "--cov={changed_dirs}" in languages["python"]["coverage"]
	assert not qg_common.derivesTargets(languages["go"])


def test_braces_outside_command_keys_and_format_escapes_are_left_alone(config):
	languages, _ = qg_common.languageConfigs({"python": {"invalid_patterns": ["{weird}"], "test": ["x", "{{lit}}"]}})
	assert languages["python"]["invalid_patterns"] == ["{weird}"]
	assert languages["python"]["test"] == ["x", "{{lit}}"]


def test_a_language_without_a_profile_is_the_repo_entry_alone(config):
	entry = {"extensions": [".gd"], "test": ["godot", "--test"], "coverage": ["cov", "{report}"]}
	languages, scratch = qg_common.languageConfigs({"gdscript": entry})
	assert languages == {"gdscript": entry}
	assert scratch == []


def test_load_config_enables_only_listed_languages(config, repo):
	root = repo({"languages": {"python": {"test": ["pytest"]}}})
	loaded, _ = qg_common.loadConfig(root)
	assert set(loaded["languages"]) == {"python"}
	assert qg_common.languageOf("main.go", loaded) is None
	assert qg_common.languageOf("a.py", loaded) == "python"


def test_a_base_spelling_out_every_key_loads_byte_identical(config, repo):
	gates = {"_comment": "c", "languages": {
		"python": {"extensions": [".py"], "test": ["/u", "x"], "coverage": ["/u", "{report}"], "invalid_exit_codes": [2],
			"red_invalid_patterns": ["E"]},
		"go": {"extensions": [".go"], "cwd": "m", "go_bin": "/g", "test": ["/g", "test"], "checks": [["/g", "vet"]]}},
		"exclude": ["t/"], "scratch_exclude": [".git/"], "limits": {}}
	loaded, _ = qg_common.loadConfig(repo(gates))
	assert json.dumps({**loaded, "exclude": gates["exclude"], "test_paths": None}, sort_keys=True) == \
		json.dumps({**gates, "test_paths": None}, sort_keys=True)


def test_a_base_without_checks_gets_only_the_profile_keys_it_lacks(config, repo, fakebin):
	entry = {"extensions": [".go"], "cwd": "m", "go_bin": "/g", "test": ["/g", "test"]}
	loaded, _ = qg_common.loadConfig(repo({"languages": {"go": entry}, "scratch_exclude": []}))
	go = loaded["languages"]["go"]
	assert {k: v for k, v in go.items() if k in entry} == entry
	assert set(go) - set(entry) == {"checks"}
	assert go["checks"] == [{"argv": [fakebin["gofmt"], "-l", "."], "empty_output": True}, [fakebin["go"], "vet", "./..."]]
	for argv, _ in qg_common.checkCommands("go", go):
		assert os.path.isabs(argv[0]) and os.access(argv[0], os.X_OK)


def test_profile_scratch_exclude_fills_in_only_without_the_repo_key(config, repo):
	root = repo({"languages": {"python": {}, "go": {}}})
	loaded, _ = qg_common.loadConfig(root)
	assert loaded["scratch_exclude"] == ["__pycache__", ".pytest_cache"]
	root = repo({"languages": {"python": {}}, "scratch_exclude": [".git/"]})
	loaded, _ = qg_common.loadConfig(root)
	assert loaded["scratch_exclude"] == [".git/"]


def test_load_config_without_languages_adds_nothing(config, repo):
	loaded, _ = qg_common.loadConfig(repo({"exclude": ["x/"]}))
	assert "languages" not in loaded and "scratch_exclude" not in loaded


# --- languages detected from the tracked files (stage 3b): a missing config never turns a gate off -----


@pytest.fixture
def tracked(tmp_path):
	"""A repo whose origin/main tracks the given files, and .claude/quality-gates.json only when gates is
	given (commit(files, gates=None)); returns the root."""
	root = tmp_path / "tracked"
	root.mkdir()
	git(root, "init", "-q", "-b", "feature/x/topic")
	git(root, "config", "user.email", "t@t")
	git(root, "config", "user.name", "t")

	def commit(files, gates=None):
		for path, text in files.items():
			(root / path).parent.mkdir(parents=True, exist_ok=True)
			(root / path).write_text(text)
		if gates is not None:
			(root / ".claude").mkdir(exist_ok=True)
			(root / qg_common.CONFIG_FILE).write_text(json.dumps(gates))
		git(root, "add", ".")
		git(root, "commit", "-q", "-m", "files")
		git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
		return str(root)

	return commit


def test_without_the_repo_file_a_tracked_language_runs_on_its_profile(config, tracked, fakebin):
	loaded, limits = qg_common.loadConfig(tracked({"pkg/a.py": "", "README.md": ""}))
	assert set(loaded["languages"]) == {"python"} and loaded["detected"] == ["python"]
	assert loaded["languages"]["python"]["test"] == [fakebin["uv"], "run", "pytest", "-x"]
	assert "test" in loaded["from_profile"]["python"]
	assert loaded["scratch_exclude"] == ["__pycache__", ".pytest_cache"]
	assert limits["ccn_max"] == homeconfig.load("quality")["ccn_max"]


def test_a_language_the_repo_file_does_not_list_is_added_on_its_profile(config, tracked, fakebin):
	root = tracked({"a.py": "", "cmd/main.go": ""}, {"languages": {"python": {"test": ["pytest"]}}})
	loaded, _ = qg_common.loadConfig(root)
	assert set(loaded["languages"]) == {"python", "go"} and loaded["detected"] == ["go"]
	assert loaded["languages"]["python"]["test"] == ["pytest"]
	assert loaded["languages"]["go"]["go_bin"] == fakebin["go"]
	assert "test" not in loaded["from_profile"]["python"] and "go_bin" in loaded["from_profile"]["go"]
	assert qg_common.languageOf("cmd/main.go", loaded) == "go"


def test_false_turns_a_detected_language_off_and_may_name_a_language_without_files(config, tracked):
	loaded, _ = qg_common.loadConfig(tracked({"a.py": "", "main.go": ""},
		{"languages": {"go": False, "gdscript": False, "python": {}}}))
	assert set(loaded["languages"]) == {"python"} and "detected" not in loaded


def test_only_false_languages_leave_an_empty_language_set(config, tracked):
	loaded, _ = qg_common.loadConfig(tracked({"main.go": ""}, {"languages": {"go": False}}))
	assert loaded["languages"] == {}


def test_no_repo_file_and_no_profiled_language_is_nothing_to_gate(config, tracked):
	assert qg_common.loadConfig(tracked({"README.md": "", "data/x.csv": ""})) == (None, None)


def test_a_file_the_branch_adds_enables_its_language(config, tracked):
	root = tracked({"README.md": ""})
	open(os.path.join(root, "new.go"), "w").close()  # untracked: the branch brings the first Go file
	loaded, _ = qg_common.loadConfig(root)
	assert loaded["detected"] == ["go"]


def test_excluded_paths_are_not_detected(config, tracked):
	loaded, _ = qg_common.loadConfig(tracked({"vendor/x.go": "", "docs/a.md": ""}, {"exclude": ["vendor/"]}))
	assert "languages" not in loaded and "detected" not in loaded


@pytest.mark.parametrize("value", [True, "off", None, ["go"]])
def test_a_language_entry_other_than_an_object_or_false_fails_naming_it(config, tracked, value):
	root = tracked({"a.py": ""}, {"languages": {"go": value}})
	with pytest.raises(homeconfig.ConfigError, match=r"languages\.go must be an object or false"):
		qg_common.loadConfig(root)


def test_a_detected_language_whose_tool_is_missing_says_how_to_turn_it_off(config, tracked):
	local(config, {"toolchains": {"tools": {"go": "/nowhere/go"}}})
	with pytest.raises(homeconfig.ConfigError) as error:
		qg_common.loadConfig(tracked({"main.go": ""}))
	assert "tool {go} needed by languages.go" in str(error.value)
	assert '"languages": {"go": false}' in str(error.value) and qg_common.CONFIG_FILE in str(error.value)


def test_command_hint_names_the_key_its_source_and_the_way_off(config, tracked):
	loaded, _ = qg_common.loadConfig(tracked({"a.py": "", "main.go": ""},
		{"languages": {"python": {"coverage": ["cov", "{report}"]}}}))
	python = qg_common.commandHint(loaded, "python", "coverage")
	assert "languages.python.coverage" in python and f"value from {qg_common.CONFIG_FILE}" in python
	assert "enabled automatically" not in python
	go = qg_common.commandHint(loaded, "go", "test")
	assert f"value from the home profile {homeconfig.where('toolchains')}" in go
	assert '"languages": {"go": false}' in go and "enabled automatically" in go


@pytest.mark.parametrize("key, new", [("marker_max_new", "marker_new_max"), ("developer_blocks", "developer_blocks_max"),
	("hook_timeout_seconds", "hook_timeouts")])
def test_a_repo_limit_with_an_old_key_name_is_an_error_naming_the_new_one(key, new):
	with pytest.raises(homeconfig.ConfigError, match=f"limits.{key} was renamed to limits.{new}"):
		qg_common.repoLimits({key: 1})


def test_repo_limits_with_current_names_pass_through():
	assert qg_common.repoLimits({"ccn_max": 12, "marker_new_max": 1}) == {"ccn_max": 12, "marker_new_max": 1}
	assert qg_common.repoLimits(None) == {}

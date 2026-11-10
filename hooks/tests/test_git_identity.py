"""conftest.py's git identity (hooks/RULES.md T2): every test commits as conftest.GIT_IDENTITY, whatever git
configuration the HOME it runs with has, so the suite passes in a fake HOME without one (public-check.py's clean
install).

Run (from the home root): bash -c 'ulimit -u $(( $(ps -u $(id -u) -L --no-headers | wc -l) + 400 )); ulimit -v
4000000; exec uv run --offline --no-project --with pytest --with pytest-xdist==3.8.0 python -m pytest
hooks/tests/test_git_identity.py -q -p no:cacheprovider'
"""

import os
import subprocess

import conftest


def test_the_identity_is_fake_and_complete():
	assert set(conftest.GIT_IDENTITY) == {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
		"GIT_COMMITTER_EMAIL"}
	assert all(conftest.GIT_IDENTITY[key].endswith("@example.invalid") for key in conftest.GIT_IDENTITY
		if key.endswith("_EMAIL"))
	assert all(os.environ.get(key) == value for key, value in conftest.GIT_IDENTITY.items())


def test_a_program_a_test_starts_commits_as_the_test_identity_in_a_home_without_git_configuration(tmp_path):
	home = tmp_path / "home"
	home.mkdir()
	repo = tmp_path / "repo"
	# HOME and XDG_CONFIG_HOME empty and no system configuration: no user.name or user.email anywhere.
	env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "GIT_CONFIG_NOSYSTEM": "1"}
	for args in (["init", "-q", str(repo)], ["-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"]):
		subprocess.run(["git", *args], check=True, capture_output=True, env=env)
	shown = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae> %cn <%ce>"], check=True,
		capture_output=True, text=True, env=env).stdout.strip()
	identity = conftest.GIT_IDENTITY
	assert shown == (f"{identity['GIT_AUTHOR_NAME']} <{identity['GIT_AUTHOR_EMAIL']}> "
		f"{identity['GIT_COMMITTER_NAME']} <{identity['GIT_COMMITTER_EMAIL']}>")

"""wt_topics.py: the topic names and places wt_controls.py, wt_queue.py, worker-control.py and the planning
session's scripts share.

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_wt_topics.py -q -p no:cacheprovider'
"""

import os
import sys
from pathlib import Path

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import wt_topics  # noqa: E402


def test_topics_come_from_worktrees_estimates_and_wt_states_by_name(tmp_path):
	main = tmp_path / "proj" / "main"
	(tmp_path / "proj" / "worktrees" / "b-topic").mkdir(parents=True)
	(main / "plans" / "worker").mkdir(parents=True)
	(main / "plans" / "worker" / "a-topic.json").write_text("{}")
	(main / "plans" / "worker" / "notes.md").write_text("")
	(main / ".hook-only" / "workers").mkdir(parents=True)
	(main / ".hook-only" / "workers" / "c-topic.json").write_text("{}")
	(main / ".hook-only" / "workers" / "Bad_Name.json").write_text("{}")
	assert wt_topics.topics(main) == ["a-topic", "b-topic", "c-topic"]
	assert wt_topics.worktreeOf(main, "a-topic") == tmp_path / "proj" / "worktrees" / "a-topic"
	assert wt_topics.topics(tmp_path / "nowhere" / "main") == []


def test_load_json_gives_an_object_or_none(tmp_path):
	(tmp_path / "ok.json").write_text('{"a": 1}')
	(tmp_path / "list.json").write_text("[1]")
	(tmp_path / "bad.json").write_text("{")
	assert wt_topics.loadJson(tmp_path / "ok.json") == {"a": 1}
	assert [wt_topics.loadJson(tmp_path / n) for n in ("list.json", "bad.json", "missing.json")] == [None] * 3
	assert wt_topics.loadJson(str(tmp_path / "ok.json")) == {"a": 1}


def test_names_and_session_ids():
	assert wt_topics.TOPIC.match("cash-account") and not wt_topics.TOPIC.match("-x")
	assert wt_topics.UUID.match("f4467c1a-ff29-420d-8174-31c15b576606") and not wt_topics.UUID.match("f4467c1a")
	assert isinstance(wt_topics.worktreeOf("/a/main", "t"), Path)

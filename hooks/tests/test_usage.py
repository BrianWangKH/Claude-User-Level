"""usage.py: the transcript reader of subagent_budget.py, subagent-cost.py, worker-progress.py and
dispatch_guard.py (per-request usage read incrementally, totals, USD, the transcript path, attachments).

Run (from the home root): bash -c 'ulimit -u 400; ulimit -v 4000000; exec uv run --offline --no-project
--with pytest python -m pytest hooks/tests/test_usage.py -q -p no:cacheprovider'
"""

import json
import os
import random
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import usage  # noqa: E402

TABLE = {
	"opus": {"input_usd_per_mtok": 5, "output_usd_per_mtok": 25, "cache_write_5m_usd_per_mtok": 6.25,
		"cache_write_1h_usd_per_mtok": 10, "cache_read_usd_per_mtok": 0.5},
	"sonnet": {"input_usd_per_mtok": 3, "output_usd_per_mtok": 15, "cache_write_5m_usd_per_mtok": 3.75,
		"cache_write_1h_usd_per_mtok": 6, "cache_read_usd_per_mtok": 0.3},
}


def assistant(requestId, model="opus", inputTokens=10, output=5, write=0, read=0, split=None, stamp=None):
	used = {
		"input_tokens": inputTokens,
		"output_tokens": output,
		"cache_creation_input_tokens": write,
		"cache_read_input_tokens": read,
		"service_tier": "standard",
	}
	if split:
		used["cache_creation"] = split
	entry = {
		"type": "assistant",
		"message": {"id": requestId, "model": model, "usage": used, "content": [{"type": "text", "text": "x"}]},
	}
	if stamp:
		entry["timestamp"] = stamp
	return json.dumps(entry) + "\n"


def transcriptLines(n, seed=1):
	"""n lines: assistant requests, repeated ids (one per content block), user lines and a budget_usd line."""
	rng = random.Random(seed)
	lines = []
	for i in range(n):
		roll = rng.random()
		if roll < 0.15:
			lines.append(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
		elif roll < 0.2:
			lines.append(
				json.dumps({"type": "attachment", "attachment": {"type": "budget_usd", "remaining": i}}) + "\n"
			)
		else:
			requestId = f"m{rng.randint(0, n // 3)}"
			lines.append(
				assistant(
					requestId,
					rng.choice(["opus", "sonnet", "opus[1m]"]),
					rng.randint(1, 50),
					rng.randint(1, 900),
					rng.randint(0, 5000),
					rng.randint(0, 90000),
					{"ephemeral_5m_input_tokens": rng.randint(0, 100), "ephemeral_1h_input_tokens": rng.randint(0, 100)}
					if rng.random() < 0.5
					else None,
					f"2026-11-05T10:{i % 60:02d}:00Z",
				)
			)
	return lines


def test_reading_in_pieces_gives_what_one_full_read_gives(tmp_path):
	path = tmp_path / "t.jsonl"
	text = "".join(transcriptLines(300)).encode()
	cuts = sorted(random.Random(7).sample(range(1, len(text)), 40)) + [len(text)]
	state, start = None, 0
	with open(path, "wb") as f:
		for cut in cuts:
			f.write(text[start:cut])
			f.flush()
			state, reqs = usage.requests(str(path), state)
			start = cut
	_, full = usage.requests(str(path))
	assert reqs == full
	assert usage.totals(reqs) == usage.totals(full)
	assert usage.usd(reqs, TABLE) == usage.usd(full, TABLE)
	assert state["offset"] == len(text)


def test_a_line_still_being_written_waits_for_the_next_read(tmp_path):
	path = tmp_path / "t.jsonl"
	line = assistant("r2", inputTokens=50)
	path.write_text(assistant("r1", inputTokens=100) + line[:-10])
	state, reqs = usage.requests(str(path))
	assert list(reqs) == ["r1"]
	assert state["offset"] == len(assistant("r1", inputTokens=100))
	with open(path, "a") as f:
		f.write(line[-10:])
	state, reqs = usage.requests(str(path), state)
	assert list(reqs) == ["r1", "r2"] and reqs["r2"][0]["input_tokens"] == 50


def test_a_request_logged_once_per_content_block_counts_once_and_its_last_line_wins(tmp_path):
	path = tmp_path / "t.jsonl"
	path.write_text(
		assistant("r1", output=1, stamp="2026-11-05T10:00:00Z")
		+ assistant("r2", output=7)
		+ assistant("r1", output=300, stamp="2026-11-05T10:00:05Z")
	)
	_, reqs = usage.requests(str(path))
	assert list(reqs) == ["r1", "r2"]  # first-seen order, last usage
	assert reqs["r1"] == (
		{"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 300},
		"opus",
		"2026-11-05T10:00:05Z",
	)
	assert usage.totals(reqs) == {
		"input_tokens": 20,
		"cache_creation_input_tokens": 0,
		"cache_read_input_tokens": 0,
		"output_tokens": 307,
		"requests": 2,
	}


def test_the_key_falls_back_to_request_id_then_uuid(tmp_path):
	path = tmp_path / "t.jsonl"
	used = {"input_tokens": 1}
	path.write_text(
		"".join(
			json.dumps(e) + "\n"
			for e in [
				{"type": "assistant", "requestId": "q1", "message": {"usage": used}},
				{"type": "assistant", "uuid": "u1", "message": {"usage": used}},
				{"type": "assistant", "message": {"usage": used}},  # no key: not counted
				{"type": "assistant", "uuid": "u2", "message": {"usage": {}}},  # no usage: not counted
				{"type": "assistant", "message": "not an object"},
				[1, 2],
			]
		)
		+ "{torn\n"
	)
	assert list(usage.requests(str(path))[1]) == ["q1", "u1"]


@pytest.mark.parametrize("change", ["truncated", "replaced"])
def test_a_shorter_or_replaced_file_is_read_again_from_the_start(tmp_path, change):
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1") + assistant("r2"))
	state, _ = usage.requests(str(path))
	if change == "truncated":
		path.write_text(assistant("r3"))
	else:
		other = tmp_path / "new.jsonl"
		other.write_text(assistant("r3") + assistant("r4") + assistant("r5"))
		os.replace(other, path)  # same path, another file, longer than the offset
	_, reqs = usage.requests(str(path), state)
	assert list(reqs) == (["r3"] if change == "truncated" else ["r3", "r4", "r5"])


def test_another_path_or_an_unknown_state_version_starts_again(tmp_path):
	first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
	first.write_text(assistant("r1"))
	second.write_text(assistant("r2"))
	state, _ = usage.requests(str(first))
	assert list(usage.requests(str(second), state)[1]) == ["r2"]
	assert list(usage.requests(str(first), {**state, "schema": 99})[1]) == ["r1"]


def test_a_missing_transcript_has_no_requests(tmp_path):
	state, reqs = usage.requests(str(tmp_path / "missing.jsonl"))
	assert reqs == {} and state["offset"] == 0
	assert usage.requests(None)[1] == {}


def test_requests_of_a_model_missing_from_the_table_are_counted_not_priced(tmp_path):
	path = tmp_path / "t.jsonl"
	path.write_text(
		assistant("r1", "opus[1m]", inputTokens=1_000_000, output=0) + assistant("r2", "unknown", inputTokens=1_000_000)
	)
	_, reqs = usage.requests(str(path))
	assert usage.usd(reqs, TABLE) == (5.0, 1)
	assert usage.totals(reqs)["requests"] == 2


def test_the_cache_write_split_is_kept_for_pricing(tmp_path):
	path = tmp_path / "t.jsonl"
	split = {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}
	path.write_text(assistant("r1", inputTokens=0, output=0, write=2_000_000, split=split))
	_, reqs = usage.requests(str(path))
	assert reqs["r1"][0]["cache_creation"] == split
	assert usage.usd(reqs, TABLE) == (16.25, 0)


def test_on_entry_sees_every_new_line_once(tmp_path):
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1") + json.dumps({"type": "user"}) + "\n")
	seen = []
	state, _ = usage.requests(str(path), None, lambda entry, st: seen.append(entry["type"]))
	with open(path, "a") as f:
		f.write(assistant("r2"))
	usage.requests(str(path), state, lambda entry, st: seen.append(entry["type"]))
	assert seen == ["assistant", "user", "assistant"]


@pytest.mark.parametrize(
	"data,expected",
	[
		(
			{
				"agent_transcript_path": "/p/s/subagents/agent-a1.jsonl",
				"transcript_path": "/p/s.jsonl",
				"agent_id": "a1",
			},
			"/p/s/subagents/agent-a1.jsonl",
		),
		({"transcript_path": "/p/s.jsonl", "agent_id": "a1"}, "/p/s/subagents/agent-a1.jsonl"),
		({"transcript_path": "/p/s.jsonl"}, "/p/s.jsonl"),
		({"agent_id": "a1"}, None),
	],
)
def test_the_agents_own_transcript_is_given_or_derived_from_the_session(data, expected):
	assert usage.transcriptOf(data) == expected


def test_the_newest_attachment_of_a_type_is_found_incrementally(tmp_path):
	path = tmp_path / "t.jsonl"
	line = lambda remaining: (
		json.dumps({"type": "attachment", "attachment": {"type": "budget_usd", "remaining": remaining}}) + "\n"
	)
	path.write_text(line(5.0) + json.dumps({"type": "attachment", "attachment": {"type": "other"}}) + "\n")
	state, found = usage.latestAttachment(str(path), "budget_usd", None, "remaining")
	assert found["remaining"] == 5.0
	with open(path, "a") as f:
		f.write(line(None) + line(3.5)[:-5])
	state, found = usage.latestAttachment(str(path), "budget_usd", state, "remaining")
	assert found["remaining"] == 5.0  # null remaining skipped, the torn line waits
	with open(path, "a") as f:
		f.write(line(3.5)[-5:])
	assert usage.latestAttachment(str(path), "budget_usd", state, "remaining")[1]["remaining"] == 3.5
	assert usage.latestAttachment(str(path), "budget_usd")[1]["remaining"] == 3.5
	assert usage.latestAttachment(str(tmp_path / "missing"), "budget_usd")[1] is None


def stateFile(root, name):
	return root / ".hook-only" / "hook-read-only" / "usage" / name


def test_each_reader_keeps_its_own_state_file(tmp_path):
	root = tmp_path / "wt"
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1"))
	assert list(usage.tracked(str(root), "budget", "a1", str(path))) == ["r1"]
	with open(path, "a") as f:
		f.write(assistant("r2"))
	assert list(usage.tracked(str(root), "progress", "a1", str(path))) == ["r1", "r2"]
	budget = json.loads(stateFile(root, "budget-a1.json").read_text())
	progress = json.loads(stateFile(root, "progress-a1.json").read_text())
	assert budget["schema"] == progress["schema"] == usage.SCHEMA
	assert list(budget["requests"]) == ["r1"] and list(progress["requests"]) == ["r1", "r2"]
	assert budget["offset"] < progress["offset"]
	assert list(usage.tracked(str(root), "budget", "a1", str(path))) == ["r1", "r2"]


def test_a_saved_state_resumes_where_the_last_call_stopped(tmp_path):
	root = tmp_path / "wt"
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1"))
	usage.tracked(str(root), "budget", "a1", str(path))
	saved = json.loads(stateFile(root, "budget-a1.json").read_text())
	saved["requests"]["r1"][0]["input_tokens"] = 999  # what the saved state says is not read again
	stateFile(root, "budget-a1.json").write_text(json.dumps(saved))
	assert usage.tracked(str(root), "budget", "a1", str(path))["r1"][0]["input_tokens"] == 999


def test_the_attachment_state_is_saved_for_its_reader(tmp_path):
	root = tmp_path / "wt"
	path = tmp_path / "t.jsonl"
	path.write_text(json.dumps({"attachment": {"type": "budget_usd", "remaining": 2}}) + "\n")
	assert usage.trackedAttachment(str(root), "dispatch", "main", str(path), "budget_usd", "remaining") == {
		"type": "budget_usd",
		"remaining": 2,
	}
	assert json.loads(stateFile(root, "dispatch-main.json").read_text())["latest"]["remaining"] == 2


def test_a_key_that_is_not_a_plain_name_is_read_in_full_and_not_saved(tmp_path):
	root = tmp_path / "wt"
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1"))
	assert list(usage.tracked(str(root), "budget", "../x", str(path))) == ["r1"]
	assert not (root / ".hook-only").exists()


def test_a_final_read_counts_a_last_line_whose_newline_is_not_written_yet(tmp_path):
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1", inputTokens=100) + assistant("r2", inputTokens=50)[:-1])
	state, reqs = usage.requests(str(path))
	assert list(reqs) == ["r1"]  # an incremental read waits for the newline
	final, reqs = usage.requests(str(path), state, final=True)
	assert list(reqs) == ["r1", "r2"]
	assert final["offset"] == state["offset"] == len(assistant("r1", inputTokens=100))
	with open(path, "a") as f:
		f.write("\n" + assistant("r3", inputTokens=7))
	for start in (state, final):  # read on from either: r2 once
		_, reqs = usage.requests(str(path), start, final=True)
		assert list(reqs) == ["r1", "r2", "r3"]
		assert usage.totals(reqs)["input_tokens"] == 157


def test_a_final_read_still_ignores_a_tail_that_does_not_parse(tmp_path):
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1") + assistant("r2")[:-10])
	assert list(usage.requests(str(path), final=True)[1]) == ["r1"]
	path.write_text(assistant("r1") + "[1, 2]")
	assert list(usage.requests(str(path), final=True)[1]) == ["r1"]


def test_a_final_attachment_read_counts_the_unterminated_last_line(tmp_path):
	path = tmp_path / "t.jsonl"
	line = lambda left: json.dumps({"attachment": {"type": "budget_usd", "remaining": left}})
	path.write_text(line(5) + "\n" + line(2))
	state, found = usage.latestAttachment(str(path), "budget_usd", None, "remaining")
	assert found["remaining"] == 5
	assert usage.latestAttachment(str(path), "budget_usd", state, "remaining", final=True)[1]["remaining"] == 2


def test_a_final_tracked_read_does_not_move_the_saved_offset_past_the_tail(tmp_path):
	root = tmp_path / "wt"
	path = tmp_path / "t.jsonl"
	path.write_text(assistant("r1") + assistant("r2")[:-1])
	assert list(usage.tracked(str(root), "budget", "a1", str(path), final=True)) == ["r1", "r2"]
	assert json.loads(stateFile(root, "budget-a1.json").read_text())["offset"] == len(assistant("r1"))
	with open(path, "a") as f:
		f.write("\n")
	assert list(usage.tracked(str(root), "budget", "a1", str(path), final=True)) == ["r1", "r2"]

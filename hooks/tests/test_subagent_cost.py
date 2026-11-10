"""subagent-cost.py: the record of what a subagent round used (raw token counts, requests, tool calls, USD).

The deferred step (the background process the hook starts) runs directly with its input in
CLAUDE_COST_HOOK_INPUT, CLAUDE_PROJECT_DIR at a scratch checkout (not a git repo: the checkout is that
directory); the tree's own price table prices the requests.
"""

import json
import os
import subprocess
import sys

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import config_fixtures  # noqa: E402

# Programs run from the session's clean copy of the tree (config_fixtures.cleanHome): the person's files in the
# tree under test (config/local.json, local.test.json, mode, state/) never change a result.
RUN_HOOKS = os.path.join(config_fixtures.cleanHome(), "hooks")
RUN_LIB = os.path.join(RUN_HOOKS, "lib")
HOOK = os.path.join(RUN_HOOKS, "budget", "subagent-cost.py")
sys.path.insert(0, os.path.join(HOOKS, "lib"))
import homepath  # noqa: E402,F401  (the tree under test: config/ and the hooks/ role directories)
import prices  # noqa: E402

MODEL = "claude-opus-5-5"


@pytest.fixture
def checkout(tmp_path):
	root = tmp_path / "wt"
	root.mkdir()
	session = tmp_path / "s1.jsonl"
	session.write_text("")
	agent = tmp_path / "s1" / "subagents" / "agent-a1.jsonl"
	agent.parent.mkdir(parents=True)
	return {"root": root, "session": session, "agent": agent}


def line(requestId, inputTokens, part, model=MODEL, stamp="2026-11-05T10:00:00Z"):
	return (
		json.dumps(
			{
				"type": "assistant",
				"timestamp": stamp,
				"message": {
					"id": requestId,
					"model": model,
					"content": [part],
					"usage": {
						"input_tokens": inputTokens,
						"output_tokens": 2,
						"cache_read_input_tokens": 1000,
						"cache_creation_input_tokens": 10,
					},
				},
			}
		)
		+ "\n"
	)


def usdOf(*inputTokens):
	used = {"output_tokens": 2, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 10}
	return round(sum(prices.requestUsd({**used, "input_tokens": n}, MODEL, prices.load()) for n in inputTokens), 6)


def deferred(checkout, data):
	env = {
		**os.environ,
		"CLAUDE_PROJECT_DIR": str(checkout["root"]),
		"CLAUDE_COST_HOOK_INPUT": json.dumps(data),
		"CLAUDE_COST_WAIT": "5",
		"PYTHONPATH": RUN_LIB,
	}
	subprocess.run([sys.executable, HOOK, "--deferred"], env=env, check=True, timeout=30)


def records(checkout):
	path = checkout["root"] / ".hook-only" / "cost" / "a1.jsonl"
	return [json.loads(x) for x in path.read_text().splitlines()]


def test_a_round_records_raw_tokens_requests_tool_calls_and_usd_and_no_weighted_count(checkout):
	tool = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
	checkout["agent"].write_text(line("r1", 100, tool) + line("r1", 300, {"type": "text", "text": "a"}))
	start = {
		"hook_event_name": "SubagentStart",
		"agent_id": "a1",
		"agent_type": "qa",
		"transcript_path": str(checkout["session"]),
	}
	deferred(checkout, start)
	with open(checkout["agent"], "a") as f:
		f.write(line("r2", 50, {"type": "text", "text": "Done: all green"}, stamp="2026-11-05T10:05:00Z"))
	deferred(
		checkout,
		{
			**start,
			"hook_event_name": "SubagentStop",
			"agent_transcript_path": str(checkout["agent"]),
			"last_assistant_message": "Done: all green",
		},
	)
	[record] = records(checkout)
	assert record["complete"] is True
	assert record["total"] == {
		"input_tokens": 350,
		"cache_creation_input_tokens": 20,
		"cache_read_input_tokens": 2000,
		"output_tokens": 4,
		"requests": 2,
		"tool_calls": 1,
		"all_tokens": 2374,
		"usd": usdOf(300, 50),
		"unpriced_requests": 0,
	}
	assert record["this_round"] == {
		"input_tokens": 50,
		"cache_creation_input_tokens": 10,
		"cache_read_input_tokens": 1000,
		"output_tokens": 2,
		"requests": 1,
		"tool_calls": 0,
		"all_tokens": 1062,
		"usd": round(usdOf(300, 50) - usdOf(300), 6),
		"unpriced_requests": 0,
	}
	assert (record["transcript_first"], record["transcript_last"]) == ("2026-11-05T10:00:00Z", "2026-11-05T10:05:00Z")


def test_a_start_after_an_old_record_with_weighted_tokens_still_counts_the_round(checkout):
	costDir = checkout["root"] / ".hook-only" / "cost"
	costDir.mkdir(parents=True)
	old = {
		"input_tokens": 100,
		"cache_creation_input_tokens": 10,
		"cache_read_input_tokens": 1000,
		"output_tokens": 2,
		"requests": 1,
		"tool_calls": 0,
		"all_tokens": 1112,
		"weighted_tokens": 215,
		"usd": 0.0001,
		"unpriced_requests": 0,
	}
	(costDir / "a1.jsonl").write_text(json.dumps({"round": 1, "total": old, "this_round": old}) + "\n")
	checkout["agent"].write_text(
		line("r1", 100, {"type": "text", "text": "x"}) + line("r2", 7, {"type": "text", "text": "end"})
	)
	deferred(
		checkout,
		{
			"hook_event_name": "SubagentStop",
			"agent_id": "a1",
			"agent_type": "qa",
			"agent_transcript_path": str(checkout["agent"]),
			"last_assistant_message": "end",
		},
	)
	record = records(checkout)[-1]
	assert record["round"] == 2
	assert record["this_round"]["input_tokens"] == 7 and "weighted_tokens" not in record["this_round"]


def test_the_stop_record_counts_a_last_line_whose_newline_is_not_written_yet(checkout):
	text = {"type": "text", "text": "x"}
	tool = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
	last = {"type": "text", "text": "Final report"}
	checkout["agent"].write_text(line("r1", 300, text) + line("r1", 300, tool) + line("r2", 50, last)[:-1])
	deferred(
		checkout,
		{
			"hook_event_name": "SubagentStop",
			"agent_id": "a1",
			"agent_type": "qa",
			"agent_transcript_path": str(checkout["agent"]),
			"last_assistant_message": "Final report",
		},
	)
	record = records(checkout)[-1]
	assert record["complete"] is True  # the final text was seen without waiting for its newline
	assert record["total"]["requests"] == 2 and record["total"]["tool_calls"] == 1
	assert record["total"]["usd"] == usdOf(300, 50)


def test_haiku_is_priced_at_its_higher_tier_so_estimates_err_high():
	"""Haiku 5.5 costs input 0.10, output 0.50, 5-minute write 0.125, 1-hour write
	0.20, cache read 0.01 USD per MTok up to 100k prompt tokens and five times that above
	(platform.claude.com/docs/en/about-claude/pricing, 2026-11-10); prices.py has one price per model, so the
	table holds the higher tier: a cost is never under-counted."""
	usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
		"cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}}
	assert prices.requestUsd(usage, "claude-haiku-5-5", prices.load()) == pytest.approx(0.50 + 2.50 + 0.05 + 0.625 + 1.0)

"""wt_events.py: what happened, for the person's next prompt and the pane's toasts (2026-11-01).

Run: uv run --offline --no-project --with pytest python -m pytest ~/.claude/hooks/tests/test_wt_events.py
"""
import json
import os
import subprocess
import sys

HOOKS = os.environ.get("QG_HOOKS_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOOKS)

import wt_events  # noqa: E402


def mainCheckout(tmp_path):
	m = tmp_path / "main"
	(m / ".git").mkdir(parents=True)
	return m


def hook(main, cwd=None):
	result = subprocess.run([sys.executable, os.path.join(HOOKS, "wt_events.py")], input=json.dumps(
		{"hook_event_name": "UserPromptSubmit", "prompt": "hi", "cwd": str(cwd or main), "session_id": "s"}),
		capture_output=True, text=True)
	assert result.returncode == 0 and "error" not in result.stderr, result.stderr
	return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] if result.stdout.strip() else None


def test_events_reach_the_next_prompt_once_with_the_ones_to_act_on_marked(tmp_path):
	main = mainCheckout(tmp_path)
	wt_events.emit(main, "run-ended", "alpha-t", "run 1: STATUS: done")
	wt_events.emit(main, "merged", "alpha-t", "into main as abc")
	wt_events.emit(main, "refused", "beta-t", "a person must review these paths: .claude/x")
	wt_events.emit(main, "run-ended", "gamma-t", "run 2: STATUS: needs-decision")
	text = hook(main)
	assert "- " in text and "alpha-t merged: into main as abc" in text
	assert "! " in text and "beta-t refused" in text and "gamma-t run-ended: run 2: STATUS: needs-decision" in text
	assert len([line for line in text.splitlines() if line.startswith("! ")]) == 2
	assert hook(main) is None  # each event once
	wt_events.emit(main, "finished", "alpha-t", "archived")
	assert "alpha-t finished" in hook(main)


def test_a_worktree_session_gets_nothing(tmp_path):
	main = mainCheckout(tmp_path)
	wt_events.emit(main, "merged", "alpha-t", "x")
	worktree = tmp_path / "wt"
	worktree.mkdir()
	(worktree / ".git").write_text("gitdir: x\n")
	assert hook(main, cwd=worktree) is None


def test_a_half_written_line_waits_and_a_replaced_log_starts_over(tmp_path):
	main = mainCheckout(tmp_path)
	wt_events.emit(main, "merged", "alpha-t", "x")
	with open(wt_events.logFile(main), "a", encoding="utf-8") as f:
		f.write('{"kind": "merg')
	events, offset = wt_events.since(main, 0)
	assert [e["topic"] for e in events] == ["alpha-t"]
	assert wt_events.since(main, offset)[0] == []
	wt_events.logFile(main).write_text("")
	assert wt_events.since(main, offset) == ([], 0)


def test_emit_from_the_command_line(tmp_path):
	main = mainCheckout(tmp_path)
	subprocess.run([sys.executable, os.path.join(HOOKS, "wt_events.py"), "emit", str(main), "run-ended", "alpha-t",
		"run 1: STATUS: done"], check=True)
	assert wt_events.since(main, 0)[0][0]["text"] == "run 1: STATUS: done"


def test_needs_attention():
	assert wt_events.needsAttention({"kind": "refused"})
	assert wt_events.needsAttention({"kind": "run-ended", "text": "run 1: STATUS: blocked"})
	assert wt_events.needsAttention({"kind": "run-ended", "text": "run 1: exit 1"})
	assert not wt_events.needsAttention({"kind": "run-ended", "text": "run 1: STATUS: done"})
	assert not wt_events.needsAttention({"kind": "merged"})

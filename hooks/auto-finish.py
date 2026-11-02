#!/usr/bin/env python3
"""Local mode: the mechanical half of /finish-worktree, right after auto-merge.py merged a branch.

Usage: auto-finish.py <worktree> <topic> <run>   (auto-merge.py starts it detached after a merge)
Waits until run <run>'s wrapper has written exit-<run> and is gone (the planning session's
`run-result.py --wait` and run-worker.sh still use the worktree until then), then, under the main
checkout (WT_MAIN_DIR), outside Claude Code's sandbox:
  1. archives the worktree's .hook-only/ (runs, quality, budget, cost, reviews), plans/<topic>.md,
     plans/tests/<topic>.md and PR_DESCRIPTION*.md to <main>/.hook-only/archive/<topic>/, the same layout,
     so run-result.py and cost-history.py read the archive as the checkout once the worktree is gone; the
     approved plan (<main>/.hook-only/approved/<topic>.md, worker-control.py) moves in as approved.md
  2. `cost-history.py import <archive> <date> <topic> <branch> <class>` (class from
     <main>/plans/worker/<topic>.json); import marks the checkout, so a later /finish-worktree can't import twice
  3. appends the plan's `## Friction` section and the `## Cost` gap reasons to <main>/plans/friction.md
     under a heading for the topic: the judgement half (where each item goes, the incident log) waits for
     the user's next planning session, which reads that file
  4. `git worktree remove --force` (auto-merge.py already found it clean apart from PR_DESCRIPTION and
     sandbox placeholders); the branch stays, as with /finish-worktree (deleting it writes .git/config)
Each step's outcome goes to <archive>/finish.json; a failed step stops the later ones, so the worktree is
never removed before its records are safe. The outcome is an event (wt_events.py: finished, or finish-failed
with the step), and the queue gets a tick (wt_queue.py) so a finished topic leaves it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
COST_HISTORY = os.path.join(HOME, ".claude", "skills", "repo-plan", "cost-history.py")
WAIT_SECONDS = 1800


def alive(pid):
	try:
		os.kill(int(pid), 0)
		return True
	except (OSError, ValueError, TypeError):
		return False


def waitForWrapper(mainDir, worktree, topic, run):
	exitFile = os.path.join(worktree, ".hook-only", "runs", f"exit-{run}")
	stateFile = os.path.join(mainDir, ".hook-only", "workers", f"{topic}.json")
	deadline = time.time() + WAIT_SECONDS
	while time.time() < deadline:
		try:
			with open(stateFile, encoding="utf-8") as f:
				pid = json.load(f).get("pid")
		except (OSError, ValueError):
			pid = None
		if os.path.isfile(exitFile) and os.path.getsize(exitFile) and not alive(pid):
			return True
		time.sleep(2)
	return False


def section(text, heading):
	"""The body of `## <heading>` up to the next `## `, stripped; '' without one."""
	match = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
	return match.group(1).strip() if match else ""


def archive(worktree, topic, dest):
	if os.path.exists(dest):
		shutil.rmtree(dest)
	shutil.copytree(os.path.join(worktree, ".hook-only"), os.path.join(dest, ".hook-only"), symlinks=True)
	approved = os.path.join(os.path.dirname(os.path.dirname(dest)), "approved", f"{topic}.md")
	if os.path.isfile(approved):
		shutil.move(approved, os.path.join(dest, "approved.md"))
	for rel in (f"plans/{topic}.md", f"plans/tests/{topic}.md"):
		src = os.path.join(worktree, rel)
		if os.path.isfile(src):
			os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
			shutil.copy2(src, os.path.join(dest, rel))
	for name in os.listdir(worktree):
		if name.startswith("PR_DESCRIPTION") and name.endswith(".md"):
			shutil.copy2(os.path.join(worktree, name), os.path.join(dest, name))


def friction(dest, topic, commit, today):
	try:
		with open(os.path.join(dest, "plans", f"{topic}.md"), encoding="utf-8") as f:
			text = f.read()
	except OSError:
		text = ""
	parts = [f"## {topic}（自動 merge {commit[:10]}，{today}；待規劃 session 決定去處）"]
	items = section(text, "Friction")
	parts.append("### Friction\n\n" + (items or "（plan 沒有 ## Friction）"))
	cost = section(text, "Cost")
	if cost:
		parts.append("### Cost\n\n" + cost)
	return "\n\n".join(parts) + "\n"


def finish(worktree, topic, run, mainDir):
	dest = os.path.join(mainDir, ".hook-only", "archive", topic)
	steps = {}
	record = os.path.join(mainDir, ".hook-only", "archive", f"{topic}.finish.json")

	def save():
		os.makedirs(os.path.dirname(record), exist_ok=True)
		with open(record, "w", encoding="utf-8") as f:
			json.dump({"topic": topic, "run": run, "steps": steps}, f, ensure_ascii=False, indent=1)
		sys.path.insert(0, HERE)
		import wt_events
		if steps.get("remove") == "removed":
			wt_events.emit(mainDir, "finished", topic, "archived, cost imported, friction queued, worktree removed")
		else:
			failed = next((f"{k}: {v}" for k, v in steps.items() if k in ("wait", "check", "error", "remove", "import")), "")
			wt_events.emit(mainDir, "finish-failed", topic, f"{failed[:200]} (finish.json)")
		subprocess.run([sys.executable, os.path.join(HERE, "wt_queue.py"), "tick", mainDir], capture_output=True)

	if not waitForWrapper(mainDir, worktree, topic, run):
		steps["wait"] = f"the wrapper of run {run} didn't finish within {WAIT_SECONDS} s; nothing done"
		return save()
	try:
		merged = json.load(open(os.path.join(worktree, ".hook-only", "runs", f"merge-{run}.json"), encoding="utf-8"))
		if merged.get("decision") != "merged":
			steps["check"] = "merge-<run>.json doesn't say merged; nothing done"
			return save()
		archive(worktree, topic, dest)
		steps["archive"] = dest
		branch = subprocess.run(["git", "-C", worktree, "symbolic-ref", "--short", "HEAD"], capture_output=True,
			text=True).stdout.strip()
		try:
			klass = json.load(open(os.path.join(mainDir, "plans", "worker", f"{topic}.json"), encoding="utf-8"))["class"]
		except (OSError, ValueError, KeyError):
			klass = "unknown"
		today = date.today().isoformat()
		imported = subprocess.run([sys.executable, COST_HISTORY, "import", dest, today, topic, branch, klass],
			cwd=mainDir, capture_output=True, text=True)
		steps["import"] = (imported.stdout + imported.stderr).strip()[-500:]
		if imported.returncode != 0:
			return save()
		frictionFile = os.path.join(mainDir, "plans", "friction.md")
		os.makedirs(os.path.dirname(frictionFile), exist_ok=True)
		with open(frictionFile, "a", encoding="utf-8") as f:
			f.write("\n" + friction(dest, topic, str(merged.get("commit", "")), today))
		steps["friction"] = frictionFile
		removed = subprocess.run(["git", "-C", mainDir, "worktree", "remove", "--force", worktree],
			capture_output=True, text=True)
		steps["remove"] = "removed" if removed.returncode == 0 else (removed.stderr.strip() or "failed")[-300:]
	except (OSError, ValueError) as error:
		steps["error"] = f"{type(error).__name__}: {error}"
	return save()


def main():
	worktree, topic, run = os.path.abspath(sys.argv[1]), sys.argv[2], int(sys.argv[3])
	mainDir = os.environ.get("WT_MAIN_DIR") or ""
	if os.path.isdir(mainDir):
		finish(worktree, topic, run, mainDir)


if __name__ == "__main__":
	if len(sys.argv) != 4:
		sys.exit("usage: auto-finish.py <worktree> <topic> <run>")
	main()

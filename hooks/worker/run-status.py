#!/usr/bin/env python3
"""run-status.py: what a topic's worker run is doing, read-only (hooks/lib/runstate.py status). Migration step 3 of
the run record: the planning session's run-result.py and worker-status.py print the same views (they are thin
wrappers that add their own lines, until run-status.py replaces them); nothing decides on this output.

Usage: run-status.py --topic <topic> [--main <main checkout>] [--run <n>] [--human [--lang <code>] | --agent |
	   --json] [--detail] [--wait] [--check-pid] [--compare]
  --main       the main checkout; default: CLAUDE_PROJECT_DIR's main checkout (hookio, X3)
  --human      one line and the to-do for people, in status_language (budget run_state) or --lang
  --agent      KEY lines and the evidence (the default)
  --json       the status object itself
  --detail     also what the run's evidence files say (runstate.detail: the worker's result, verify, verifier,
			   merge, the next run, every run's result line): a detail section under --agent, a "detail" key
			   under --json; ignored with --human
  --wait       read again every status_wait_poll_seconds until the run is ended, an orphan, unknown, or there
			   is none; then print. No time limit: a dead run becomes an orphan, so the wait always ends
  --check-pid  also check the wrapper's process (runstate.verifyProcess); only outside the sandbox, where
			   other processes are visible (inside, every process looks gone)
  --compare    instead: the run's status from its record against the one from today's files
			   (runstate.compareStories), one line per difference, "same" when there is none (--json: the
			   list). Prints only; run-worker.sh's EXIT trap is what reports a difference as an event
Reads: the topic's runs/ files (record, heartbeat, today's files), its flow and agent-guard state, the main
checkout's plans/ and workers state. Writes: nothing.
Output: stdout as above; exit 0. 1: the main checkout or the settings can't be read. 2: usage.
Fails: closed — a state it can't read prints as phase unknown, never as running or ended.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))  # homepath
import homepath  # noqa: E402,F401  (config/ and the hooks/ role directories on sys.path)
import homeconfig  # noqa: E402
import hookio  # noqa: E402
import runstate  # noqa: E402


def parser():
	top = argparse.ArgumentParser(description="What a topic's worker run is doing (read-only).")
	top.add_argument("--topic", required=True)
	top.add_argument("--main", help="the main checkout (default: CLAUDE_PROJECT_DIR's)")
	top.add_argument("--run", type=int)
	view = top.add_mutually_exclusive_group()
	view.add_argument("--human", action="store_true")
	view.add_argument("--agent", action="store_true")
	view.add_argument("--json", action="store_true")
	top.add_argument("--lang", help="a language of status_text (with --human)")
	top.add_argument("--detail", action="store_true")
	top.add_argument("--wait", action="store_true")
	top.add_argument("--check-pid", action="store_true")
	top.add_argument("--compare", action="store_true")
	return top


def renderCompare(args, differences):
	if args.json:
		return json.dumps(differences, ensure_ascii=False, indent=1)
	if not differences:
		return "same"
	return "\n".join(f"{d['field']}: record {d['record']!r}, legacy {d['legacy']!r}" for d in differences)


def render(args, result):
	if args.human:
		view = runstate.human(result, args.lang)
		todo = view["todo"] or {}
		rest = " ".join(p for p in (todo.get("text"), f"({todo['command']})" if todo.get("command") else None) if p)
		return view["line"] + (f"\n{rest}" if rest else "")
	found = runstate.detail(result) if args.detail else None
	if args.json:
		return json.dumps({**result, "detail": found} if found else result, ensure_ascii=False, indent=1)
	return runstate.agent(result, found)


def main(argv=None, *, clock=None, sleep=time.sleep, out=None):
	out = out or sys.stdout
	args = parser().parse_args(argv)
	try:
		mainDir = os.path.realpath(args.main) if args.main else hookio.Hook({}).mainRoot()
		poll = runstate.settings()["status_wait_poll_seconds"]
		check = runstate.verifyProcess if args.check_pid else None
		if args.compare:
			run = args.run or runstate.status(mainDir, args.topic, clock=clock)["run"]
			differences = runstate.compareStories(mainDir, args.topic, run, clock=clock) if run else []
			print(renderCompare(args, differences), file=out)
			return 0
		result = runstate.status(mainDir, args.topic, clock=clock, pidAlive=check, run=args.run)
		while args.wait and result["phase"] not in runstate.WAIT_ENDS:
			sleep(poll)
			result = runstate.status(mainDir, args.topic, clock=clock, pidAlive=check, run=args.run)
		print(render(args, result), file=out)
	except (hookio.RootError, homeconfig.ConfigError, OSError, KeyError) as error:
		print(f"run-status: {error}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())

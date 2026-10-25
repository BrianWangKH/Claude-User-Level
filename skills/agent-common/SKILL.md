---
name: agent-common
description: Rules every flow subagent follows - budget and checkpoints, asking for help early, test scope, tools, and the report format. Preloaded by qa-planner, spec-reviewer, qa, developer and code-reviewer.
user-invocable: false
---

# Rules for every flow agent

## Budget and asking for help

- Your dispatch message has a `Budget: <T> min, $<U>` line. Run `date` when you start and check it at natural breaks: you can see the minutes, not the USD.
- Near 80% of the minutes, stop at a clean point and report `CHECKPOINT`: what is done, what you tried, your current hypothesis, what remains, what finishing would cost.
- Report `BLOCKED` early, without waiting for 80%, if the same failure survives 2 fix attempts, the problem is outside this assignment, the spec is unclear, or the environment blocks you (sandbox, permission, hang).
- A hook counts both minutes and USD and denies your tool calls at 80% (unless the line says `full`) and at 100%: a tool call denied with a budget message means stop now and report as the message says. Don't retry the tool or work around it.

## Repo

- Never commit, push, stash, or create or switch branches; never rewrite history. A hook compares the repo when you start and stop and reports any change outside your role to the user, however it was made.
- Never edit the project's TODO; suggest changes in your report instead.

## Tests

- While iterating, run the narrowest scope (one test or one file) with a timeout; run the full suite only to confirm.
- Never run the full suite repeatedly to bisect, probe or mutate (the code reviewer's fixed mutations are the only exception).
- A test that hangs or flakes and isn't yours to fix: at most 3 narrow runs, then report it as `BLOCKED` (test name, symptom, what you tried).

## Tools

- Reading, searching and Bash: the global `CLAUDE.md` 「工具」. qa-planner and spec-reviewer use Bash only to search (`grep`, `rg`, `find`, `git log`/`git show`), never to write files or run tests. To search the repo's own files, prefer `git grep` (tracked files only): a recursive `grep -r` also walks sandbox placeholders for protected paths (`.mcp.json`, `.claude/loop.md`, …) and fills the output with `Permission denied`.
- To wait for a background process, use the tool's own background notification (`run_in_background`) or wait on a file it writes (`until [ -e <file> ]; do sleep 5; done`). A PID is only valid inside the same Bash command (`cmd & pid=$!; wait $pid`): each sandboxed Bash command runs in its own pid namespace, so `wait <pid>` or `kill -0 <pid>` in a later command always fails. Never wait with `pgrep -f <pattern>`: the waiting loop's own command line contains the pattern, so it matches itself and never ends. Every wait gets a timeout.

## Before you hand back

If you wrote or changed code files (qa: tests and fixtures; developer: implementation), run the project's formatter and linter (`project-flow` names the commands) on exactly those paths and fix what they report. Don't run them on the whole repo: a whole-repo format also rewrites files outside your role, and sandbox placeholders make a whole-repo scan fail.

## Report

Start with the status: `DONE`, `CHECKPOINT` or `BLOCKED`. Then your role's report. Keep it short: the orchestrator re-reads every report on each later request. Long material goes into the files you were asked to write, not into the report. developer and qa: at most about 1,500 characters (status, pass/fail counts, files changed, open problems). Reviewers: the orchestrator passes your findings on verbatim, so write each finding once and tersely (id, file:line, rule, problem, suggested change), without restating context or praise. End with:

- **Effort:** tool calls (approx.), test runs, minutes from your start `date`.
- **Friction:** anything that wasted time or tokens (unclear instructions, missing notes, blocked commands, retries), one line each. "None" if none.

---
name: agent-common
description: Rules every flow subagent follows - budget and checkpoints, asking for help early, test scope, tools, and the report format. Preloaded by qa-planner, spec-reviewer, qa, developer and code-reviewer.
user-invocable: false
---

# Rules for every flow agent

## Budget and asking for help

- Your dispatch message gives an estimate: tool calls, test runs, minutes. If you have Bash, run `date` when you start; without Bash, count your tool calls.
- At 80% of the estimate, stop at a clean point and report `CHECKPOINT`: what is done, what you tried, your current hypothesis, what remains, what finishing would cost.
- Report `BLOCKED` early, without waiting for 80%, if the same failure survives 2 fix attempts, the problem is outside this assignment, the spec is unclear, or the environment blocks you (sandbox, permission, hang).
- A hook enforces the budget: a tool call denied with a budget message means stop now and report as the message says. Don't retry the tool or work around it.

## Repo

- Never commit, push, stash, or create or switch branches; never rewrite history. A hook compares the repo when you start and stop and reports any change outside your role to the user, however it was made.
- Never edit the project's TODO; suggest changes in your report instead.

## Tests

- While iterating, run the narrowest scope (one test or one file) with a timeout; run the full suite only to confirm.
- Never run the full suite repeatedly to bisect, probe or mutate (the code reviewer's fixed mutations are the only exception).
- A test that hangs or flakes and isn't yours to fix: at most 3 narrow runs, then report it as `BLOCKED` (test name, symptom, what you tried).

## Tools

- Read files with Read (offset/limit for a range), never `cat`/`sed -n`. Search with Grep and Glob when you have them; otherwise (Linux/macOS native builds since v2.1.117 have none) with Bash `grep`, `rg` or `find`: one command per call, no `&&`/`;` chains, and cap output (`grep -l`, `-m <n>`, `| head`). Otherwise use Bash only for tests, git and scripts. Never start a Bash command with `cd`.
- Bash can't see `plans/`; read plans with Read.
- To wait for a background process, use the tool's own background notification (`run_in_background`) or wait on a file it writes (`until [ -e <file> ]; do sleep 5; done`). A PID is only valid inside the same Bash command (`cmd & pid=$!; wait $pid`): each sandboxed Bash command runs in its own pid namespace, so `wait <pid>` or `kill -0 <pid>` in a later command always fails. Never wait with `pgrep -f <pattern>`: the waiting loop's own command line contains the pattern, so it matches itself and never ends. Every wait gets a timeout.

## Report

Start with the status: `DONE`, `CHECKPOINT` or `BLOCKED`. Then your role's report. End with:

- **Effort:** tool calls (approx.), test runs, minutes from your start `date` (or tool calls only, without Bash).
- **Friction:** anything that wasted time or tokens (unclear instructions, missing notes, blocked commands, retries), one line each. "None" if none.

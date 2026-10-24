---
name: repo-plan
description: Plan with the user in the main checkout - set up a new repo (kickoff), fill missing setup, discuss design and TODO (with web research while the user is present), and pick the next topic; then hand each settled topic to a new worktree with a plan. Never develops.
when_to_use: In the main checkout whenever the user wants to plan, discuss design or TODO, or start a new repo; when the SessionStart repo-check says something is missing.
---

# Repo plan

You are the **planning session** (global `CLAUDE.md`, 「角色」). You discuss, research and write plans; worktree sessions do the work.

## Start (every time)

Repo check (run when this skill loaded):

!`python3 ~/.claude/skills/repo-plan/repo-check.py`

1. If it says `SKIP: worktree`, you are in a worktree: stop and tell the user to plan from the main checkout. Otherwise its `STATE` picks the mode below, and every `MISSING` line is a candidate topic. Re-run the command above after setup work to refresh it.
2. The main checkout only plans and wraps up; it never develops unless the user explicitly asks.
3. Read `plans/repo-plan.md` (create it with the four sections below if it doesn't exist).
4. Rebuild the in-progress picture from the repo (never from memory): `git worktree list`, then Read each worktree's `plans/<topic>.md` for its goal, scope and `## TODO items`; treat those as in progress. Mention worktrees whose branch is already merged: they wait for `/finish-worktree`.

## `plans/repo-plan.md`

The planning record, kept in the main checkout only (gitignored). It keeps the discussion out of the context and carries it across sessions.

| Section | Holds | Leaves when |
|---|---|---|
| `## 討論中` | open questions, research notes with source links | settled → next section, or → 不採用 |
| `## 已定案、待開 branch` | settled items waiting for a worktree | its worktree plan is written and handed off: delete it here |
| `## 不採用` | options dropped, with the reason | never (stops the same option coming back) |
| `## 未來` | later, not now | work starts → 討論中 |

Write to it as the discussion goes, not at the end.

## Classes

Every topic gets one class; it decides how the worktree session works (skill `feature-flow` has the steps for each).

| Class | What | How |
|---|---|---|
| logic | state machines, calculations, data validation | test-first, full flow |
| wiring | UI or scenes reacting to events | test-first with integration tests on real components |
| presentation | visuals, animation, feel, sound | incremental, manual checks; calculations in tested pure functions |
| small fix | a single bug fix | short flow: regression test first |
| docs | docs, flow, agents, skills, hooks in the repo | worktree session edits directly, no subagents. `~/.claude` and other config outside the repo: planning session and user, directly |
| investigation | option not settled, needs an experiment | worktree session first reads the source's official docs written for AI or developers (llms.txt, API schema, field reference) and quotes them; experiments only confirm or fill gaps (prototypes stay out of the repo); reports a comparison, user picks, only the decision is written to docs |

Principle: test decisions and numbers, not pixels. The project `CLAUDE.md` gives examples per class.

## Plan rules

- Before any branch starts, settle with the user its scope, what is out of scope, and every 「待討論」 item. 「待討論」 parts are never implemented or tested until the user answers.
- Plans are `plans/<topic>.md` and test plans `plans/tests/<topic>.md`; `plans/` is gitignored. Long-term decisions go into the project docs (DESIGN, TODO) and the PR description.
- Parallel topics must not overlap in scope; a topic that depends on unmerged work waits until it is merged.
- In-progress work is always rebuilt from `git worktree list` and each worktree's plan, never from a separate list.
- Only the orchestrator session updates TODO, at wrap-up on its branch.
- Network, only when the topic brings in a new external service or toolchain: list each host and why it's needed; the user reviews and adds them to the repo's `.claude/settings.json` `sandbox.network.allowedDomains` (Claude can't edit settings files, and per-command hosts are hard-denied). Then, before hand-off, run the real command once from the planning session (`uv sync`, `go mod download`, one API call): a redirect to an unlisted host shows up in the sandbox violation, and goes back to the user for review.
- Filesystem: repo `sandbox.filesystem` entries don't count while `blockReadsOutsideWorkingDirectories` is on. A new tool or cache outside the repo goes in home settings; tell the user before hand-off.
- Tests live under `<component>/tests/` for every language (Go tests as a separate package that uses only the public API), so `guard-paths.json` protects them by prefix.
- Never edit files in a worktree while its worker runs: agent-guard counts the change as the agent's. Wait for the run to end, then edit and resume.

## Modes

| `STATE` / situation | Mode |
|---|---|
| `new` | **Kickoff** |
| `incomplete` | **Fill setup**: propose the missing items as the top-priority topic |
| user wants to discuss design or TODO | **Discuss** |
| user wants the next feature | **Plan a feature** |

### Kickoff (new repo)

Load the checklist with Bash (the Read tool can't reach `~/.claude`): `cat ~/.claude/skills/repo-plan/kickoff-checklist.md`. Walk through it with the user, one group at a time; skip what the user says doesn't apply, and add what they bring up. Record the answers in `plans/repo-plan.md`. Then hand off branch `feature/docs/repo-init` (the fixed name for a repo with no scope table yet) as a docs branch whose plan lists every file to write and its content.

### Fill setup

Each `MISSING` item from repo-check becomes part of one docs topic (e.g. `feature/docs/<topic>`) unless the user splits it. It goes before any feature.

### Discuss (design / TODO)

- The user is present, so research is welcome: look up official docs and existing approaches on the web for feasibility, and note the source of each finding in `討論中`.
- Narrow to the options worth trying. Then pick one:
  - settled without testing → a **docs** branch: the plan names the sections of DESIGN / TODO to change and the new text
  - needs evidence → an **investigation** branch (see Classes): the plan lists the 2–3 candidate options, how to test each (experiment, measurement), the **decision criteria** (what decides between them), and a budget. The worktree session runs the experiments, reports a comparison table, the user picks, and only the decision is written into DESIGN
  - not settled → stays in `討論中`; nothing is written to repo docs
- A long-term decision always ends up in a repo doc through a branch; `plans/repo-plan.md` is only the working record.

### Plan a feature

1. Propose next topics that aren't in progress, don't overlap an active scope, and don't depend on unmerged work.
2. Discuss until scope, out-of-scope and every 「待討論」 item are settled. Decide the class (see Classes). Settle each input's bounds, whether each value really needs to be adjustable, and which data each state depends on. When a rule changes later, update the whole plan.
3. Never plan on a guess. Every external fact the plan relies on (a data source's field meaning, units, cumulative vs per-period, limits, deadlines, API behaviour) is checked now in the official docs and cited in the plan (link + quote). Never write "the worker checks first": verification is never deferred to a later step. If the docs don't answer it, it is a 「待討論」 item for the user.

## Hand off

1. Pick the branch `<type>/<scope>/<topic>` (global `CLAUDE.md`; kickoff uses `feature/docs/repo-init`) and run skill `new-worktree` with it.
2. Write the plan to the path it printed:
   - **all classes:** goal; class; in / out of scope; decisions made with the user; the source (link + quote) of every external fact the plan relies on, or "none"; empty `## Cost` and `## Friction` sections. Also write `plans/past-estimates.md` in the worktree with the output of `python3 ~/.claude/skills/repo-plan/cost-history.py summary` (one bias line per agent; the worktree session can't read the main checkout's history). Not in the plan itself: subagents read the plan and don't need it. `## Cost` starts with the header row `| agent | round | est tokens | actual tokens | est min | actual min | gap reason |`.
   - **test-first, presentation, small fix:** also `## TODO items` (the TODO entries this topic covers, quoted as written in TODO); numbered rules with links to design sections; manual verification steps; empty `## QA commits` and `## Reviewer`.
   - **docs:** the files and sections to change and the intended content.
   - **investigation:** candidate options, how to test each, decision criteria, budget, and which doc gets the decision.
3. Delete the item from `已定案、待開 branch` in `plans/repo-plan.md`.
4. Start the worker (below), or, if the user prefers, tell them to open a new desktop session in the worktree and send `/feature-flow <topic>`. You're free to plan the next topic.

## Worker (headless worktree session)

The worktree session can run headless with `claude -p`, so the user only talks to this planning session.

**Plan addition:** every worker plan gets a `## Worker 模式` section: wherever feature-flow says to ask or tell the user and wait, the worker ends its run instead; it never guesses and never works around a block. Its **last message** starts with exactly one line `STATUS: done`, `STATUS: needs-decision` or `STATUS: blocked`, followed by: the questions (with a recommendation each), or each blocked operation (permission, sandbox, ALERT) with its full message; what was done; the next step. Never push; the push command goes in the next step. Language: the last message is for the user, in Traditional Chinese; commit messages and `PR_DESCRIPTION.md` stay in English.

**Estimate:** before giving the start command, estimate the whole run in USD and minutes from the plan, then run `python3 ~/.claude/skills/repo-plan/cost-history.py bias branch <type>/<scope> <class>` and multiply each number by its bias (no pairs yet: keep your estimate and say so). Apply every bias that has at least one pair, for each metric separately, even with few pairs; never skip a metric because the sample is small (clean-financials skipped minutes at n=2 and overestimated 5–10×). A bias from a short history may be softened toward 1, never ignored. Write the corrected numbers in the worktree plan's `## Cost` as a `worker (run <n>)` row with the actual columns empty (USD in the gap-reason column), and tell the user both the raw and the corrected estimate. The start command gets `CLAUDE_WORKER_MINUTES=<corrected minutes>` (the worker-budget hook stops the run there and asks it to report `STATUS: blocked`) and `--max-budget-usd` = corrected USD × 1.2 (hard cap; the 20% is for that report). A resume gets a new estimate for the rest of the work.

**Start:** the sandbox has no `claude`, so the worker runs outside it.

- **With the worker-control hook** (`~/.claude/hooks/worker-control.py` exists): Write `plans/worker/<topic>.json` in the main checkout: `{"minutes": <corrected minutes>, "usd": <cap>}` (add `"session_id"` only to adopt a session started by hand). Tell the user to type `wt launch <topic>` for the first run only; every later run of the same topic (after `needs-decision`, `blocked` or `wt stop`) is `wt resume <topic>` (launch refuses a topic that already has a session). `wt stop <topic>` stops it. The hook starts the run in the background and adds the run number and the `exit-<n>` path to the prompt as context. Decisions go into the worktree plan while the worker is stopped; a resume always tells it to re-read the plan.
- **Without the hook**, give exactly this, with `<topic>`, the run number `<n>` (1, then 2, … for each resume) and absolute paths filled in:

```bash
claude auth status --text && cd <worktree> && mkdir -p .hook-only/runs && date -Is > .hook-only/runs/start-<n> && CLAUDE_WORKER_MINUTES=<min> claude -p "/feature-flow <topic>" --permission-mode auto --max-budget-usd <usd> --output-format json > .hook-only/runs/result-<n>.json; echo "$? $(date -Is)" > .hook-only/runs/exit-<n>
```

- `--output-format json` writes one result line when the run ends; no transcript is kept, so nothing large is ever read here.
- `exit-<n>` is always written (`;`), with the exit code and end time, also when claude crashes or is killed and writes no JSON.
- The run files live in the worktree's `.hook-only/` (agents can't write there, so the worker can't alter them) and are removed with the worktree; `/finish-worktree` copies the numbers into `plans/cost-history.md` first.
- No `--permission-prompts none`: in `-p` a step that needs approval is denied at once (no hang), and the worker reports it as `STATUS: blocked`.
- Resume after the user answers: the same command with `--resume <session_id>` (from the previous result file) right after `-p`, and the next `<n>`.

**Wait:** right after the run starts, run a background Bash (`run_in_background`) that sleeps until the exit file exists, so waiting costs nothing and the harness notifies you when the run ends:

```bash
until [ -s <worktree>/.hook-only/runs/exit-<n> ]; do sleep 30; done; cat <worktree>/.hook-only/runs/start-<n> <worktree>/.hook-only/runs/exit-<n>; head -c 2000 <worktree>/.hook-only/runs/result-<n>.json
```

Wait on this file only. Never check the worker's PID (`kill -0`, `ps`, `/proc`), even when the hook names it: each sandboxed Bash command runs in its own pid namespace, so the check always fails and a live worker looks dead. The PID is only for the hook's `wt stop`. Right after starting the wait, tell the user the run is going and that they can ask 「進度」 at any time (see Progress below).

**When it ends:** fill the actual columns of the `worker (run <n>)` row in the worktree plan's `## Cost` from the result file: tokens = the sum of every token count in `modelUsage` (it includes the subagents), minutes = end time in `exit-<n>` minus `start-<n>` (not `duration_ms`: it leaves out time spent in background subagents); put `total_cost_usd` and `num_turns` in the gap-reason column. A resumed run's `total_cost_usd` and `modelUsage` are cumulative for the whole session: subtract the previous run of the same session to get this run's tokens and USD. A run with `num_turns` 0 or `is_error` true gets `invalid` in that column (it says nothing about estimating). Judge the run from the result file only, never by `subtype` (it says `success` even when nothing ran):
- Exit code not 0 with an empty result file (90 = not logged in, from the hook's wrapper): the run never produced a result; tell the user the code and mark the row `invalid`.
- Stopped by `wt stop`: exit code 0 (claude exits 0 on SIGINT, not 130), `is_error` true, `subtype` `error_during_execution`, `result` empty. Mark the row `invalid`, tell the user where it stopped (`.hook-only/budget/events.jsonl`, git log), and that `wt resume <topic>` continues it.
- Otherwise `num_turns` 0, or `is_error` true: the run didn't start or failed (e.g. expired login); show `result` and `terminal_reason` to the user.
- Otherwise find the first line of `result` that starts with `STATUS:` (it should be the first line; if it isn't, log that in `## Friction`): `STATUS: done` → run `git -C <worktree> diff --stat origin/main...HEAD -- .claude .mcp.json` and point out any change to the user (settings, agents and MCP config are theirs to review), then relay the summary, the push command, and `/finish-worktree` after the merge; `STATUS: needs-decision` → ask the questions, then resume with the answers; `STATUS: blocked` → fix the cause with the user (or explain it), then resume.
- No `STATUS:` line: treat it as `blocked`, relay the full `result` text, and log the missing line in the plan's `## Friction`.

**Progress, when the user asks:** run `python3 ~/.claude/skills/repo-plan/worker-status.py <topic>` from the main checkout and summarise it. It reads only the session start/end records, the budget, agent-guard and cost hooks' state and git; never read worker transcripts.

Worktrees live at `<main repo>/../worktrees/<topic>` (the user creates `worktrees/` once; the sandbox can't). Worktrees never push: the user pushes and opens the PR. After the merge, `/finish-worktree <topic>` in the main checkout.

## Your own friction

If you hit time or token waste yourself while planning, ask the user whether to log it in `plans/friction.md` (read at the next `/finish-worktree`) or handle it now. Anything clearly recurring: propose a fix right away.

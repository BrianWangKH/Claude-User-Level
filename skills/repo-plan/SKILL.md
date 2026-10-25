---
name: repo-plan
description: Plan with the user in the main checkout (never in a worktree) - set up a new repo (kickoff), fill missing setup, discuss design and TODO (with web research while the user is present), and pick the next topic; then hand each settled topic to a new worktree with a plan. Never develops.
when_to_use: In the main checkout whenever the user wants to plan, discuss design or TODO, or start a new repo; when the SessionStart repo-check says something is missing.
disable-model-invocation: true
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

Every topic gets one class; it decides how the worktree session works.

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

## Hand off and workers

When a topic is settled (scope, out of scope and every 「待討論」 answered): `cat ~/.claude/skills/repo-plan/handoff.md` and follow it (again after a compaction: only this file is re-attached). It covers the branch, the plan per class, the estimate, starting and waiting on the headless worker.

- **A worker run ends** (the background `run-result.py --wait` notifies you): do what its `Next:` line says.
- **「進度」:** `python3 ~/.claude/skills/repo-plan/worker-status.py <topic>`, summarised; never read worker transcripts.
- Worktrees live at `<main repo>/../worktrees/<topic>` and never push: the user pushes and opens the PR. After the merge, `/finish-worktree <topic>`.

## Your own friction

If you hit time or token waste yourself while planning, ask the user whether to log it in `plans/friction.md` (read at the next `/finish-worktree`) or handle it now. Anything clearly recurring: propose a fix right away.

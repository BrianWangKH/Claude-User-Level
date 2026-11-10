---
name: repo-plan
description: Plan with the user in the main checkout (never in a worktree) - set up a new repo (kickoff), fill missing setup, discuss design and TODO (with web research while the user is present), and pick the next topic; then hand each settled topic to a new worktree with a plan. Never develops.
when_to_use: In the main checkout whenever the user wants to plan, discuss design or TODO, or start a new repo; when the SessionStart repo-check says something is missing.
disable-model-invocation: true
---

# Repo plan

You are the **planning session** (global `CLAUDE.md`, "Roles"). You discuss, research and write plans; worktree sessions do the work.

## Start (every time)

Repo check (run when this skill loaded):

!`python3 ~/.claude/skills/repo-plan/repo-check.py`

1. If it says `SKIP: worktree`, you are in a worktree: stop and tell the user to plan from the main checkout. Otherwise its `STATE` picks the mode below, and every `MISSING` line is a candidate topic. Re-run the command above after setup work to refresh it.
2. The main checkout only plans and wraps up; it never develops unless the user explicitly asks.
3. Read `plans/repo-plan.md` (create it with the four sections below if it doesn't exist).
   If the repo has `.claude/skills/repo-plan-notes/SKILL.md`, Read it too: the project's own planning rules (what each rule in a plan must state for this domain). They add to this skill; where they conflict, the project's file wins.
4. Rebuild the in-progress picture from the repo (never from memory): `git worktree list`, then Read each worktree's `plans/<topic>.md` for its goal, scope and `## TODO items`; treat those as in progress. Mention worktrees whose branch is already merged: they wait for `/finish-worktree`.

## `plans/repo-plan.md`

The planning record, kept in the main checkout only (gitignored). It keeps the discussion out of the context and carries it across sessions. A new file uses the English section names below; an existing file keeps its older Chinese names (given in brackets), which mean the same sections.

| Section | Holds | Leaves when |
|---|---|---|
| `## Under discussion` (older: `## 討論中`) | open questions, research notes with source links | settled → next section, or → Not adopted |
| `## Settled, waiting for a branch` (older: `## 已定案、待開 branch`) | settled items waiting for a worktree | its worktree plan is written and handed off: delete it here |
| `## Not adopted` (older: `## 不採用`) | options dropped, with the reason | never (stops the same option coming back) |
| `## Future` (older: `## 未來`) | later, not now | work starts → Under discussion |

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

- Before any branch starts, settle with the user its scope, what is out of scope, and every `to be discussed` item (older plans: 「待討論」) (only goal or scope choices are `to be discussed`; everything else follows "Decide, don't ask"). `to be discussed` parts are never implemented or tested until the user answers.
- Plans are `plans/<topic>.md` and test plans `plans/tests/<topic>.md`; `plans/` is gitignored. Long-term decisions go into the project docs (DESIGN, TODO) and the PR description.
- Parallel topics must not overlap in scope; a topic that depends on unmerged work waits until it is merged.
- In-progress work is always rebuilt from `git worktree list` and each worktree's plan, never from a separate list.
- Only the orchestrator session updates TODO, at wrap-up on its branch.
- Network, only when the topic brings in a new external service or toolchain: list each host and why it's needed; the user reviews and adds them to the repo's `.claude/settings.json` `sandbox.network.allowedDomains` (Claude can't edit settings files, and per-command hosts are hard-denied). Then, before hand-off, run the real command once from the planning session (`uv sync`, `go mod download`, one API call): a redirect to an unlisted host shows up in the sandbox violation, and goes back to the user for review.
- Filesystem: repo `sandbox.filesystem` entries don't count while `blockReadsOutsideWorkingDirectories` is on. A new tool or cache outside the repo goes in home settings; tell the user before hand-off.
- Tests live under `<component>/tests/` for every language (Go tests as a separate package that uses only the public API), so `guard-paths.json` protects them by prefix.
- Never edit files in a worktree while its worker runs: agent-guard counts the change as the agent's. Wait for the run to end, then edit and resume.

## Decide, don't ask

Flexibility is the point: values and lists always come from config, never hard-coded. First principle: **the temporary state must carry no risk**. Risk means, most serious first: a security risk (leaked secrets or credentials, widened permissions or access, a new exposed surface); lost, corrupted or unrecoverable data; anything sent or published outside that can't be taken back; wrong results someone would trust; a forced rewrite later; and breaking any hard rule in the project `CLAUDE.md`.

For any design choice, in this order:

1. Aim for the long-term best approach, designed flexibly enough to be split into stages.
2. It feels like a hassle → it isn't split finely enough; it can't be split → the design isn't flexible enough. Redesign and split finer; neither is a question.
3. Doing only the first stage now carries no risk → decide: build that stage with the extension point and put the later stages in TODO with how they plug in. Don't interrupt the user; record it (below).
4. **Ask the user** only when risk-free and long-term best can't both hold:
   - the best approach, even split, carries risk if only its first stage is done now; or
   - the best approach is risky now and the risk-free option isn't the long-term best.
   State the concrete risk and consequence of each option.

Common splits that need no question: how many items (records, entities, inputs) — one runs the pipeline end to end, more come later through config; a subset with its own rules (a category, a special case) — exclude it explicitly now and give it its own pipeline when its data is added.

Also decide yourself: choices with a standard answer, names, formats and internal structure. A choice that changes the product's goal or scope always goes to the user.

**Record every decision you make yourself**, sub-decisions included, as you go: a row in the topic's `## Decisions` table (in `plans/repo-plan.md` `Under discussion` until the plan is written). Columns: Event (the situation) | Choice (what was chosen, and what was split off to TODO) | Reason (why, including why the chosen stage alone is risk-free); older plans name them 事件 | 選擇 | 理由. When the topic is settled, show the user the whole table at once, in the user's preferred language, before hand-off.

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

- The user is present, so research is welcome: look up official docs and existing approaches on the web for feasibility, and note the source of each finding in `Under discussion`.
- Narrow to the options worth trying. Then pick one:
  - settled without testing → a **docs** branch: the plan names the sections of DESIGN / TODO to change and the new text
  - needs evidence → an **investigation** branch (see Classes): the plan lists the 2–3 candidate options, how to test each (experiment, measurement), the **decision criteria** (what decides between them), and a budget. The worktree session runs the experiments, reports a comparison table, the user picks, and only the decision is written into DESIGN
  - not settled → stays in `Under discussion`; nothing is written to repo docs
- A long-term decision always ends up in a repo doc through a branch; `plans/repo-plan.md` is only the working record.

### Plan a feature

1. Propose next topics that aren't in progress, don't overlap an active scope, and don't depend on unmerged work.
2. Discuss until scope, out-of-scope and every `to be discussed` item are settled. Decide the class (see Classes). Settle each input's bounds and which data each state depends on; every value and list comes from config. When a rule fixes what an output holds (a report, a file, a table, an API response), the plan names its fields with their types and units, so the interface is the plan's decision, not the tests' (a plan that listed only the contents left qa to pick the report's field names). When a rule changes later, update the whole plan.
3. Never plan on a guess. Every external fact the plan relies on (a data source's field meaning, units, cumulative vs per-period, limits, deadlines, API behaviour, and the API of every test tool and package manager the tests will use, e.g. a UI test harness's element accessors or a dependency-group default) is checked now in the official docs and cited in the plan (link + quote). Never write "the worker checks first": verification is never deferred to a later step. If the docs don't answer it, it is a `to be discussed` item for the user.

## Hand off and workers

When a topic is settled (scope, out of scope and every `to be discussed` item answered): `cat ~/.claude/skills/repo-plan/handoff.md` and follow it (again after a compaction: only this file is re-attached). It covers the branch, the plan per class, the estimate, starting and waiting on the headless worker.

- **A worker run ends** (the background `run-result.py --wait` notifies you): do what its `Next:` line says. It also returns when the run may have died (`PHASE: orphan`) or its state can't be read (`PHASE: unknown`): tell the user the status line and its `TODO:`; `wt status <topic>` (typed by the user) checks the process outside the sandbox, and `wt resume <topic>` checks it too: it closes a run whose process is gone (never merged) and resumes, and refuses while the process runs or can't be checked.
- **"progress" (or the same request in the user's language):** `python3 ~/.claude/skills/repo-plan/worker-status.py <topic>`, summarised: its `PHASE:` and the line under the KEY lines say what the run is doing, `progress:` what the worker does now; quote the evidence lines instead of reading run files; never read worker transcripts.
- Worktrees live at `<main repo>/../worktrees/<topic>` and never push: the user pushes and opens the PR. After the merge, `/finish-worktree <topic>`.

## Your own friction

If you hit time or token waste yourself while planning, ask the user whether to log it in `plans/friction.md` (read at the next `/finish-worktree`) or handle it now. Anything clearly recurring: propose a fix right away.

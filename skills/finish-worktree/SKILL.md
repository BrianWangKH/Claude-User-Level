---
name: finish-worktree
description: Review a merged feature's cost and friction with the user, then remove its worktree and local branch. Main checkout only.
when_to_use: After the user says a feature's PR has been merged.
argument-hint: "<topic>"
disable-model-invocation: true
---

## 1. Harvest cost and friction (before anything is removed)

The worktree, and with it `plans/<topic>.md` and its `.hook-only/runs/` result files, is deleted in step 2. Do this first.

1. Read `../worktrees/<topic>/plans/<topic>.md` with the Read tool (Bash can't see `plans/`): the plan's class and the `## Cost` and `## Friction` sections. Also Read `plans/friction.md` in the main checkout if it exists.
2. If `## Cost` has `worker (run <n>)` rows with empty actual columns, fill them first from `../worktrees/<topic>/.hook-only/runs/result-<n>.json` (repo-plan, "When it ends").
3. Append every `## Cost` row to the cost history with one command per row (Bash can't read `plans/`, so you pass the values you read):
   ```bash
   python3 ~/.claude/skills/repo-plan/cost-history.py add <date> <topic> <branch> <class> <agent> <round> <estTok> <actTok> <estMin> <actMin> <estUsd> <actUsd> "<note>"
   ```
   Copy the numbers as they are, estimate and actual alike; empty numbers as `""`. Rows without a number estimate get `est: none` in the note; failed runs keep `invalid`. Plans from before 2026-10-20 have the old header (`estimate | actual tokens | actual min`): put their estimate text in the note as `est: <text>`. Then add one `branch` row (agent `branch`, round `-`): the worker rows' estimated and actual USD and minutes summed, or, for a desktop session without worker rows, the actual minutes with `est: none`.
4. Summarise for the user, in Traditional Chinese: total cost, where it ran over or under its estimate and why, and each friction item.
5. For each friction item, propose a destination and discuss it with the user: project config (project `CLAUDE.md`, project skills), user config (`~/.claude`: global `CLAUDE.md`, agents, skills, hooks, settings), TODO, or nothing. Change nothing until the user agrees. Point out items that will clearly keep recurring. You can't write `~/.claude`: draft those changes under the main checkout's `plans/config-update/claude/` (same layout as `~/.claude`) and give the user one ordered list of commands in your reply.
6. Append the rows directly to `~/.claude/docs/lessons-log.md` with Bash (the sandbox allows writing `~/.claude/docs`), each at the end of its section's table, in the format of that log: keep the details, and write the 通則 column as one sentence anyone could reuse. Only add rows, never edit existing ones; correct an earlier row with a new row that says so. Tell the user which rows you added.
7. Remove the handled items from `plans/friction.md`.

If the user wants to skip this, say once that the worktree's records will be lost, then continue. Step 3 is never skipped: it takes no discussion.

## 2. Remove the worktree

A hook fetches origin when this skill is invoked. Run exactly this command, with nothing added (a second hook matches it):

```bash
bash ~/.claude/skills/finish-worktree/finish-worktree.sh <topic>
```

- `FAIL`: report the reason to the user and stop (not merged yet, uncommitted work, stale fetch…). Never delete anything yourself.
- `OK`: the finish-worktree hook then re-checks and removes the worktree and branch outside the sandbox, and its result arrives as additional context. Report it. If no hook result arrives, give the user the fallback command the script printed.

Never pass `--remove` yourself.

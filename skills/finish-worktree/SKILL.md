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
2. Record the cost history in one command:
   ```bash
   python3 ~/.claude/skills/repo-plan/cost-history.py import ../worktrees/<topic> <date> <topic> <branch> <class>
   ```
   It imports every subagent round and worker run from the hooks' records and adds the `branch` row (the worker runs summed). Only rows the hooks can't know need an `add` each (Bash can't read `plans/`, so you pass the values you read): the `orchestrator` rows of `## Cost`, and for a desktop session without worker runs, a `branch` row with the actual minutes and `est: none`:
   ```bash
   python3 ~/.claude/skills/repo-plan/cost-history.py add <date> <topic> <branch> <class> <agent> <round> "" "" <estMin> <actMin> <estUsd> <actUsd> "<note>"
   ```
   Put the gap reasons from `## Cost` into the summary below, not the history.
3. `python3 ~/.claude/skills/repo-plan/cost-history.py rows ../worktrees/<topic>` lists the subagent rounds for the summary.
4. Summarise for the user, in Traditional Chinese: total cost, where it ran over or under its estimate and why, and each friction item.
5. For each friction item, propose a destination and discuss it with the user: project config (project `CLAUDE.md`, project skills), user config (`~/.claude`: global `CLAUDE.md`, agents, skills, hooks, settings), TODO, or nothing. Change nothing until the user agrees. Point out items that will clearly keep recurring. You can't write `~/.claude`: build each changed file in full under `$TMPDIR/home-apply-<topic>/` (same layout as `~/.claude`; Bash can diff and test there), show the diff, and give the user the apply steps in your reply, one command per line (`cp`, then `git -C ~/.claude add <paths>` and the commit); for `settings.json`, which you can't read, give the exact block and where it goes.
6. Append the rows directly to `~/.claude/docs/lessons-log.md` with Bash (the sandbox allows writing `~/.claude/docs`), each at the end of its section's table, in the format of that log: keep the details, and write the 通則 column as one sentence anyone could reuse. Only add rows, never edit existing ones; correct an earlier row with a new row that says so. Tell the user which rows you added.
7. Remove the handled items from `plans/friction.md`.

If the user wants to skip this, say once that the worktree's records will be lost, then continue. Step 2 is never skipped: it takes no discussion.

## 2. Remove the worktree

A hook fetches origin when this skill is invoked. Run exactly this command, with nothing added (a second hook matches it):

```bash
bash ~/.claude/skills/finish-worktree/finish-worktree.sh <topic>
```

- `FAIL`: report the reason to the user and stop (not merged yet, uncommitted work, stale fetch…). Never delete anything yourself.
- `OK`: the finish-worktree hook then re-checks and removes the worktree and branch outside the sandbox, and its result arrives as additional context. Report it. If no hook result arrives, give the user the fallback command the script printed.

Never pass `--remove` yourself.

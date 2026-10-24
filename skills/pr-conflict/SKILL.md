---
name: pr-conflict
description: Check a topic's open PR for conflicts with origin's default branch after a fresh fetch, and hand the merge to a new worker session in its worktree. Main checkout only.
when_to_use: When the user says a topic's PR shows conflicts, or asks whether it still merges cleanly.
argument-hint: "<topic>"
disable-model-invocation: true
---

A hook fetches origin when this skill is invoked. Run exactly this command, with nothing added:

```bash
bash ~/.claude/skills/pr-conflict/pr-conflict.sh <topic>
```

- `FAIL`: report the reason to the user and stop.
- `MERGED` / `OK`: tell the user there is nothing to resolve. No merge commit is made just because the default branch moved.
- `CONFLICT:`: list the files for the user, then start a **new** worker session in the worktree (not `--resume`: the old context is long and its cache has expired; the plan and `PR_DESCRIPTION.md` carry what the new session needs). Use the worker command from skill `repo-plan` ("Worker"), with the prompt `/feature-flow <topic> merge` and the next run number. The worktree's `plans/<topic>.md` doesn't need a new section: `merge.md` carries its own worker rules.
- When the run ends, handle it as any worker run (repo-plan, "When it ends"): `done` → the push command; the PR updates itself.

The worktree session never fetches; the merge uses the `origin/<default>` this fetch brought in (worktrees share the repo's refs).

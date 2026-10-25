---
name: new-worktree
description: Create the feature branch and its git worktree for a planned topic from a fresh fetch of origin, then write the plan into it. Main checkout only.
when_to_use: From skill repo-plan, once scope and decisions are settled with the user.
argument-hint: "<type>/<scope>/<topic>"
---

A hook fetches origin when this skill is invoked; the script refuses if that fetch failed or is stale.

1. Run, with the branch from the arguments:
   ```bash
   bash ~/.claude/skills/new-worktree/new-worktree.sh <type>/<scope>/<topic>
   ```
   On `FAIL`, report it to the user and stop. Don't work around it.
2. Write the plan with the Write tool to the `plan:` path the script printed (format: `~/.claude/skills/repo-plan/handoff.md`, "Hand off", for the topic's class).
3. Continue with `handoff.md`, "Hand off" step 3 onward. The default there is to start the headless worker ("Worker"); don't tell the user to open a desktop session unless they asked for that.

Never start development in the main checkout.

---
name: feature-flow
description: Develop a planned topic in its git worktree (never in the main checkout) - run the test-first agents (qa-planner, spec-reviewer, qa, developer, code-reviewer) from the plan to the PR description, or carry out a small fix, presentation, docs or investigation branch. Planning happens in the main checkout with skill repo-plan.
when_to_use: In a worktree with its topic to develop it; whenever unsure which agent or reviewer runs next.
argument-hint: "<topic> [merge]"
disable-model-invocation: true
---

# Feature flow

You are the **orchestrator session** (指揮 session): you coordinate, the agents do the work. If the project has a `project-flow` skill, load it now: it holds the project's test command, protected test paths and other project-only steps.

## Mode

Checkout (checked when this skill loaded): !`bash ~/.claude/skills/feature-flow/where.sh`

If no result appears above, run `bash ~/.claude/skills/feature-flow/where.sh` yourself.

| Where | Plan | Do |
|---|---|---|
| main checkout | — | Stop: planning is `/repo-plan`. Never develop here unless the user explicitly asks for it. |
| worktree | `plans/<topic>.md` missing or lacks scope / rules / decisions | Stop and tell the user to plan it from the main checkout. Don't guess. |
| worktree | plan ready, argument `merge` | Load `agents.md` and `merge.md` (resolve conflicts with the default branch) instead of the class steps. |
| worktree | plan ready | Load the steps for its class, then follow them together with the rules below. |

The step files sit next to this file, outside the working directory, where the Read tool can't reach. Load the class's files with one Bash call: `cat ~/.claude/skills/feature-flow/<file> ~/.claude/skills/feature-flow/<file>`. After a compaction they are gone from your context (only this file is re-attached): load them again the same way before the next step.

| Class (in the plan) | Steps |
|---|---|
| logic, wiring (test-first) | `agents.md` `test-first.md` |
| small fix | `agents.md` `small-fix.md` |
| presentation | `presentation.md` |
| docs, investigation | `docs-investigation.md` |

## Rules that come first

- **Round cap:** each review loop (spec-reviewer ↔ qa-planner, code-reviewer ↔ qa/developer) gets at most 3 rounds. If the reviewer hasn't approved after round 3, stop and report to the user: what is open, what each side argues, your recommendation. Continue only when the user says so. Rounds caused by a rule the user changed mid-way count from that change.
- **Never guess open questions:** spec conflicts and 「待討論」 items go to the user. 「待討論」 parts are never implemented or tested.
- **Models** are fixed in each agent's frontmatter. The only override: if `developer` is stuck on the same tests for 2 rounds, or the change is architectural, spawn a new `developer` with `model: opus` and hand it the previous report. Tell the user when you do.

## Must-fix vs TODO

You decide, and tell the user why:

- **Must fix:** breaks a rule in the plan, a test of this branch fails, or this branch regresses existing behaviour.
- **TODO:** pre-existing problems, out of scope, test-environment flakiness this branch didn't cause, robustness beyond what the plan asks, nice-to-haves. Attach evidence (symptom, how to reproduce, cost so far); don't send anyone to dig deeper.
- **Ask the user:** it changes scope or a settled decision, the rule itself is unclear, or fixing it clearly costs more than the remaining budget.

## Records in `plans/<topic>.md`

- **`## Cost`:** subagent rounds are recorded by the hooks (estimate from the `Budget:` line, actual USD and minutes per round); `cost-history.py rows` prints them as table rows, and `/finish-worktree` imports them. The plan's `## Cost` holds only what the hooks can't know: one `orchestrator` row (your estimate before starting, actual minutes at the end) when you work directly without subagents, and a gap reason per subagent round that ran over or under (`<agent> <round>: <reason>`). Worker runs are recorded from their run files; don't add rows for them.
- **`## Friction`:** anything that wasted time or tokens, not only blockers: from agents' `Friction`, from your own work, and whenever the user says 「記一筆」. Each item: symptom, cost (estimate vs actual), cause, suggested destination (project config / user config / docs / TODO / none). The planning session reads both sections with the user before the worktree is removed.

## Wrap up (every class)

1. Update the project's TODO yourself: the developer's "TODO changes" plus the reviewer's follow-ups. Agents never edit it, so parallel branches rarely conflict there.
   Search TODO for this topic (feature name, class names, notes like "decide when doing X") and tick or reword every matching line, including follow-ups that earlier branches left for this topic.
2. In one response, the project's completion check (project-flow) and `git status --short`. Then commit with explicit paths, in logical commits (e.g. tests, implementation, TODO); never stage files the project marks as placeholders.
3. After the last commit, in one response:
   - write `PR_DESCRIPTION.md` (untracked) **in English**, like commit messages, even when the plan and the conversation are in Traditional Chinese: changes, intentional behaviour differences, test coverage, what isn't automated, manual verification steps, follow-ups, the PR attribution line. If a commit is added later, update it.
   - complete `## Cost` and `## Friction`.
   - check for conflicts without creating a commit: `bash ~/.claude/skills/pr-conflict/pr-conflict.sh <topic> --local` (uses the last fetch; the worktree can't fetch). Output starts with `CONFLICT:`: follow `merge.md` before pushing. `OK`: no merge, even if the default branch moved.
4. Give the user the push command (worktrees never push themselves). Never merge.
5. Tell the user: if the PR later shows conflicts, run `/pr-conflict <topic>` from the main checkout; after the PR is merged, run `/finish-worktree <topic>` from the main checkout.

## Practical notes

- **Fewer requests:** every request re-reads your whole context. Put independent tool calls in one response (several Bash, Edit or Read calls in one response cost one request; don't chain commands with `&&` or `;`, that gets blocked); Read a file once and don't re-read what is already in your context; don't poll. In a headless worker the conversation is compacted at the phase boundaries (test-first.md), so earlier reports and outputs don't stay.
- Each round can take 8–35 minutes. Run agents in the background and don't poll.
- Keep branches as small as the user agrees to; cost grows with rounds and with how much each agent reads. Every agent step resends its whole context, even when it is "just running tests".

---
name: feature-flow
description: Develop a planned topic in its git worktree - run the test-first agents (qa-planner, spec-reviewer, qa, developer, code-reviewer) from the plan to the PR description, or carry out a small fix, presentation, docs or investigation branch. Planning happens in the main checkout with skill repo-plan.
when_to_use: In a worktree with its topic to develop it; whenever unsure which agent or reviewer runs next.
argument-hint: "<topic> [merge]"
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
| worktree | plan ready, argument `merge` | Load `merge.md` (resolve conflicts with the default branch) instead of the class steps. |
| worktree | plan ready | Load the steps for its class, then follow them together with the rules below. |

The step files sit next to this file, outside the working directory, where the Read tool can't reach. Load them with Bash: `cat ~/.claude/skills/feature-flow/<file>`.

| Class (in the plan) | Steps |
|---|---|
| logic, wiring (test-first) | `test-first.md` |
| small fix | `small-fix.md` |
| presentation | `presentation.md` |
| docs, investigation | `docs-investigation.md` |

## Rules that come first

- **Round cap:** each review loop (spec-reviewer ↔ qa-planner, code-reviewer ↔ qa/developer) gets at most 3 rounds. If the reviewer hasn't approved after round 3, stop and report to the user: what is open, what each side argues, your recommendation. Continue only when the user says so. Rounds caused by a rule the user changed mid-way count from that change.
- **Resume, don't respawn:** continue the same agent with SendMessage (by agent id) so it keeps its context. Agent ids arrive in the completion notification of a background agent.
- **Never guess open questions:** spec conflicts and 「待討論」 items go to the user. 「待討論」 parts are never implemented or tested.
- **Models** are fixed in each agent's frontmatter. The only override: if `developer` is stuck on the same tests for 2 rounds, or the change is architectural, spawn a new `developer` with `model: opus` and hand it the previous report. Tell the user when you do.

## Dispatching an agent

Every first message to an agent, and every resume that starts new work, contains:

1. **Goal, scope, done condition.**
2. **Estimate:** tokens and minutes, converted into what the agent can track itself: about N tool calls, at most M test runs, about T minutes. First estimate from the task itself (step, round, size), then multiply by that agent's bias in `plans/past-estimates.md` (written at hand-off; `x0.8` means past estimates were 25% too high) and, once this branch has rows for the agent, by how far this branch's own estimates were off. Apply every bias that has at least one pair, for each metric separately, even with few pairs; never skip a metric because the sample is small (clean-financials skipped minutes at n=2 and overestimated 5–10×). A bias from a short history may be softened toward 1, never ignored. Write the corrected numbers into `## Cost` before dispatching; an empty estimate makes the row useless for the next estimate.
3. **The checkpoint rule:** "Follow the budget rules in `agent-common`: `CHECKPOINT` at 80%, `BLOCKED` early."
4. **Test scope:** narrowest runs while iterating, the full suite only to confirm.

Before the spawn or the resume, Write the same budget to `plans/budget/<agent_type>.json`: `{"minutes": T, "tokens": N, "phase": "checkpoint"}` (`tokens` optional). The budget hook claims and deletes that file when the agent starts, then denies its tool calls at 80% (phase `checkpoint`) and at 100% of the time or tokens. Without the file, only the default time limit in `~/.claude/hooks/budget-defaults.json` applies. Every agent also has `maxTurns: 100` as a last resort.

Then tell the user, in Traditional Chinese: which agent, what it does, the raw estimate, the bias applied and the corrected estimate.

While an agent works, you can keep discussing with the user, read files and edit `plans/` freely. Don't change files that git tracks or reports as untracked; if such a change can't wait, make it and tell the agent what changed in your next message to it. Never do so while a reviewer runs.

## When an agent returns

- Fill the actual columns of its row in `## Cost` (below).
- Copy its `Friction` items, plus anything you hit yourself, into `## Friction` (below).
- `CHECKPOINT`: read its approach. Either resume it with a direction (new budget file, phase `checkpoint`), let it finish if it clearly can (budget file with the remaining time and tokens, phase `full`), or ask the user. At 100% the hook stops it, and you always ask the user before writing any further budget; never extend a budget yourself.
- `BLOCKED`: decide must-fix / TODO / ask the user (below), and tell the user why.
- Tell the user actual vs estimate, and the reason when it ran over.
- Before routing a problem an agent reports to another agent (a test or doc it calls outdated or wrong), verify it yourself: Grep and quote the line.
- Agent reports are in English. Summarise them for the user in Traditional Chinese, with pass/fail counts, actual vs estimated cost, and anything the user must decide or tune by hand.

## Must-fix vs TODO

You decide, and tell the user why:

- **Must fix:** breaks a rule in the plan, a test of this branch fails, or this branch regresses existing behaviour.
- **TODO:** pre-existing problems, out of scope, test-environment flakiness this branch didn't cause, robustness beyond what the plan asks, nice-to-haves. Attach evidence (symptom, how to reproduce, cost so far); don't send anyone to dig deeper.
- **Ask the user:** it changes scope or a settled decision, the rule itself is unclear, or fixing it clearly costs more than the remaining budget.

## Records in `plans/<topic>.md`

- **`## Cost`:** one row per subagent round, with the header `| agent | round | est tokens | actual tokens | est min | actual min | gap reason |`: numbers only in the four number columns (no "—", no "about"), reason for any gap. A row written without an estimate gets `est: none` in its gap reason. When you work directly without subagents (docs, investigation, small direct edits), add one `orchestrator` row with your own estimate before starting and the actual minutes at the end. Actual tokens are `this_round.all_tokens` in the cost hook's record: Read `.hook-only/cost/<agent_id>.jsonl` at the checkout root (written a few seconds after the completion notification). Minutes come from the notification. The notification's `subagent_tokens` is only the context size of the last request. The cost hook only sees subagents, so your own share isn't in those records: when this session runs as a headless worker, the planning session writes one `worker (run <n>)` row per run (estimate at hand-off, actuals from its result file; `modelUsage` already includes the subagents). Don't add or edit that row yourself.
- **`## Friction`:** anything that wasted time or tokens, not only blockers: from agents' `Friction`, from your own work, and whenever the user says 「記一筆」. Each item: symptom, cost (estimate vs actual), cause, suggested destination (project config / user config / docs / TODO / none). The planning session reads both sections with the user before the worktree is removed.

## Wrap up (every class)

1. Update the project's TODO yourself: the developer's "TODO changes" plus the reviewer's follow-ups. Agents never edit it, so parallel branches rarely conflict there.
   Search TODO for this topic (feature name, class names, notes like "decide when doing X") and tick or reword every matching line, including follow-ups that earlier branches left for this topic.
2. Commit with explicit paths, in logical commits (e.g. tests, implementation, TODO). List files with `git status --short` first; never stage files the project marks as placeholders.
3. After the last commit, write `PR_DESCRIPTION.md` (untracked) **in English**, like commit messages, even when the plan and the conversation are in Traditional Chinese: changes, intentional behaviour differences, test coverage, what isn't automated, manual verification steps, follow-ups, the PR attribution line. If a commit is added later, update it.
4. Make sure `## Cost` and `## Friction` are complete.
5. Before the first push, check for conflicts without creating a commit: `bash ~/.claude/skills/pr-conflict/pr-conflict.sh <topic> --local` (uses the last fetch; the worktree can't fetch). Output starts with `CONFLICT:`: follow `merge.md` before pushing. `OK`: no merge, even if the default branch moved.
6. Give the user the push command (worktrees never push themselves). Never merge.
7. Tell the user: if the PR later shows conflicts, run `/pr-conflict <topic>` from the main checkout; after the PR is merged, run `/finish-worktree <topic>` from the main checkout.

## Practical notes

- Each round can take 8–35 minutes. Run agents in the background and don't poll.
- Keep branches as small as the user agrees to; cost grows with rounds and with how much each agent reads. Every agent step resends its whole context, even when it is "just running tests".

# Hand off and workers

Loaded by skill repo-plan (`cat ~/.claude/skills/repo-plan/handoff.md`) when a topic is settled and goes to a worktree. Follow it top to bottom.

## Hand off

1. Pick the branch `<type>/<scope>/<topic>` (global `CLAUDE.md`; kickoff uses `feature/docs/repo-init`) and run skill `new-worktree` with it.
2. Write the plan to the path it printed:
   - **all classes:** goal; class; in / out of scope; decisions made with the user; the source (link + quote) of every external fact the plan relies on, or "none"; empty `## Cost` and `## Friction` sections (new-worktree already copied the cost history into the worktree for the subagent estimates). `## Cost` starts with the header row `| agent | round | est usd | actual usd | est min | actual min | gap reason |`; the hooks record subagent rounds and worker runs, so it only gets the orchestrator's own rows and gap reasons.
   - **test-first, presentation, small fix:** also `## TODO items` (the TODO entries this topic covers, quoted as written in TODO); numbered rules with links to design sections; manual verification steps; empty `## QA commits` and `## Reviewer`.
   - **docs:** the files and sections to change and the intended content.
   - **investigation:** candidate options, how to test each, decision criteria, budget, and which doc gets the decision.
3. Delete the item from `已定案、待開 branch` in `plans/repo-plan.md`.
4. Start the worker (below), or, if the user prefers, tell them to open a new desktop session in the worktree and send `/feature-flow <topic>`. You're free to plan the next topic.

## Worker (headless worktree session)

The worktree session can run headless with `claude -p`, so the user only talks to this planning session.

**Plan addition:** every worker plan gets a `## Worker 模式` section: wherever the flow says to ask or tell the user and wait, the worker ends its run instead with a `STATUS:` line (the worker-stop hook enforces the format); it never guesses, never works around a block and never pushes (the push command goes in its next step). The last message is for the user, in Traditional Chinese; commit messages and `PR_DESCRIPTION.md` stay in English.

**Estimate:** before the start, estimate the whole run in USD and minutes from the plan, then run `python3 ~/.claude/skills/repo-plan/cost-history.py estimate branch <type>/<scope> <class> <usd> <minutes>`: it applies the bias and prints the corrected numbers, the cap and the `plans/worker/<topic>.json` content. Tell the user both the raw and the corrected estimate. A resume gets a new estimate for the rest of the work.

**Start:** the sandbox has no `claude`, so the worker runs outside it.

- **With the worker-control hook** (`~/.claude/hooks/worker-control.py` exists): Write `plans/worker/<topic>.json` in the main checkout before every launch and resume, with the content `estimate` printed: `{"minutes": <corrected minutes>, "est_usd": <corrected USD>}` (add `"session_id"` only to adopt a session started by hand). The hook records them per run (`.hook-only/runs/est-<n>.json`), sets `CLAUDE_WORKER_MINUTES` (the worker-budget hook stops the run there with `STATUS: blocked`) and passes est_usd × 1.2 as `--max-budget-usd` (the extra is for that report). Nothing goes into the plan's `## Cost`: run-result.py and `/finish-worktree` read the run files. Tell the user to type `wt launch <topic>` for the first run only; every later run of the same topic (after `needs-decision`, `blocked` or `wt stop`) is `wt resume <topic>` (launch refuses a topic that already has a session). `wt stop <topic>` stops it; `wt resume` also lifts an agent-guard hold in that worktree, so resume only after the user has decided on an ALERT. The hook starts the run in the background and adds the run number and the `exit-<n>` path to the prompt as context. Decisions go into the worktree plan while the worker is stopped; a resume always tells it to re-read the plan.
- **Without the hook:** give the user the manual start command from `~/.claude/skills/repo-plan/worker-manual-start.md` (`cat` it), filled in.

**Wait:** right after the run starts, run in the background (`run_in_background`), so the harness notifies you when the run ends:

```bash
python3 ~/.claude/skills/repo-plan/run-result.py <topic> --wait
```

It waits on the run's exit file only (a worker's PID is invisible from the sandbox). Tell the user the run is going and that they can ask 「進度」 at any time.

**When it ends:** do what the script's `Next:` line says.

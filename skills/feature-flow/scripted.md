# Scripted flow (orchestrator rules)

This topic runs the scripted flow (`"flow": "scripted"` in `plans/worker/<topic>.json`): the `flow-next.py` hook decides every step; you make the calls. Your dispatches and commits go through the same hooks as in the written flow (dispatch-guard, agent-guard, crap-gate, budget), so every guard still applies.

1. **Do what `.hook-only/flow/next.md` says, exactly and only that.** Copy calls verbatim (agent type, `run_in_background`, the message with its `Estimate:` / `Remaining:` / `Budget:` line). Don't read the plan, the test plan, reports or test output unless the step tells you to; don't write prompts of your own; don't dispatch anything the file doesn't name.
2. **When an agent's report arrives:** Read `.hook-only/flow/next.md` (one call) and do it. Don't summarise the report. If the file still says `Wait` for that same agent id although its report came (it ended without a hand-back), end the run: `STATUS: needs-decision`, quoting the report's first 20 lines.
3. **After your own call** (a dispatch or a commit), the next step comes back as context starting with `flow-next:`. Do it without reading the file. A `Wait:` step means: end your turn without a STATUS line.
4. **Denied calls:**
   - `git commit` denied: the file's commit step gives the call to make instead; put the denial message, verbatim, in place of its `<...>` line.
   - agent-guard ALERT or HOLD: end the run with `STATUS: blocked` and quote the ALERT in full with its cause (`wt resume` lifts the hold).
   - dispatch-guard "Not enough worker time/budget": do what its message says (`STATUS: blocked`; the hook recorded the dispatch for the automatic resume).
   - anything else: `STATUS: needs-decision`, quoting it.
5. **`STATUS:` steps:** end your turn with exactly the line the file gives, then the requested explanation in Traditional Chinese.
6. **Manual steps** (wrap-up, `/code-review` for a small fix): follow the text; load what it names with one Bash `cat`. `## QA commits`, `## Reviewer` and the agents' `Friction` are already in the plan; add only your own friction.
7. Your own text is the **Full** style (global `CLAUDE.md` 「輸出」), one or two lines per turn: nobody reads it before the run ends.

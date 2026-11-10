# Working with agents

Loaded together with the class steps (feature-flow SKILL.md, Mode) for every class that dispatches subagents.

## Fresh agent or resume

A subagent's prompt cache lives 5 minutes (subagents get the 5-minute TTL even on a subscription: https://code.claude.com/docs/en/prompt-caching.md), so a resume after a pause re-writes the agent's whole context at the cache-write rate on its first request. For a fix round (review findings, a wrong test, a failing case) spawn a new agent and give it the findings verbatim with files and lines. Resume with SendMessage (by agent id, from the background agent's completion notification) only when the remaining work is larger than the agent's context and needs what it learned, such as finishing a plan after `CHECKPOINT`, or when a reviewer re-checks its own findings (spec-reviewer, code-reviewer). A resume's budget is the remaining work plus one cache write of the agent's whole context (its context size at the model's 5-minute cache-write price in the home price table): the budget hook counts only this round's USD, so the re-write lands in the new round.

## Dispatching an agent

Every first message to an agent, and every resume that starts new work, contains:

1. **Goal, scope, done condition.**
2. **Estimate:** USD and minutes, judged from the plan: what this round has to do according to `plans/<topic>.md` and, from qa on, the test plan (its rules and cases, the files and behaviour they cover), and how big that is next to the topic's other rounds. Size it from that content, not from a fixed per-step figure. Cost is in **USD**, priced per model and cache TTL from `~/.claude/config/home.json` (`prices`): the unit the budget hook counts, the cost hook records (`this_round.usd`) and the worker's `--max-budget-usd` uses. To turn your judgment into USD, think of it as requests × (context × the cache-read price + new input × the cache-write price + output × the output price), plus a large file's size × the cache-write price once when it is first read; subagents write the 5-minute cache, a headless orchestrator the 1-hour one. Don't correct the estimate yourself: the dispatch-guard hook applies the agent's estimate model from the cost history (the copy new-worktree made: a fixed part every round of that agent needs plus a slope on your raw estimate, both fitted to earlier rounds' actuals against their raw estimates, so it corrects how you judge size, not earlier budgets) and adds the `Budget:` line itself. `cost-history.py estimate agent <agent> <usd> <minutes>` prints the same numbers, only for when you want to see the model. There are no floors and no headroom to add yourself: the budget is the expected need, and the hook stops the agent only at `budget_headroom_factor` × it (home.json budget). Leave the quality gates' waits out (the mutation scan the reviewer waits for, crap-gate's coverage run for the developer): they are taken out of the agent's budget, and dispatch-guard reserves them in the run's time (this branch's own actual/estimate is shown for reference only). When the hook denies for a missing history copy, don't estimate without it: in a headless worker run end with `STATUS: blocked`, otherwise ask the user.
3. **Estimate line:** your raw estimate alone on its line, `Estimate: <minutes> min, $<usd>`. The hook inserts the `Budget:` line and the budget-rules line of `agent-common` before it, and the hooks record the raw estimate, so the history bias compares actual with it, not with the budget. A resume after a `CHECKPOINT` writes `Remaining: <minutes> min, $<usd>` instead (below).
4. **Test scope:** narrowest runs while iterating, the full suite only to confirm.
5. **No git writes:** "Don't commit, stash or switch branches; I commit." Fix rounds too.

The dispatch-guard hook turns the `Estimate:` or `Remaining:` line into the `Budget:` line (a `Budget:` line you write yourself is kept as written): a dispatch or resume of a flow agent with none of them is denied. The budget hook then denies the agent's tool calls at `budget_headroom_factor` × the budget (time or USD), so a modest overrun finishes and the history records the real need; every agent also has `maxTurns: 100`. In a headless worker run the same hook denies a dispatch whose budget, plus the gate's wait for a code-reviewer (not a merge review) or developer round, plus the wrap-up margin, doesn't fit what is left of the run: write the planned dispatch and its estimate into `plans/<topic>.md` and end the run with `STATUS: blocked`. That denial also records the dispatch for one automatic resume (worker-control.py auto, bounded in home.json budget), so the next run starts without the user; end the run the same way, the hook decides.

Then tell the user, in the user's preferred language, in one line: which agent, what it does, and your raw estimate.

While an agent works, you can keep discussing with the user, read files and edit `plans/` freely. Don't change files that git tracks or reports as untracked; if such a change can't wait, make it and tell the agent what changed in your next message to it. Never do so while a reviewer runs.

## Doing an agent's step yourself

Only two steps may be done without the agent: revising the test plan after a spec-reviewer round, and fixing doc-only findings after a code-reviewer round. Tests and implementation never: agent-guard keeps those roles apart. Do it yourself only when both hold; otherwise dispatch:

1. **Every finding is local:** wording, or adding or adjusting cases under an existing rule. No new rule, no changed decision, no new external fact.
2. **Cost:** `direct ≤ 0.5 × dispatch`, both in USD:
   - `direct = edits × r × (C × read + 3000 × write)` at your own model's prices (`home.json` (`prices`); a headless orchestrator writes the 1-hour cache): `edits` = separate places to change; `r` = requests per edit (1 when the file's text is already in your context, else 2); `C` = your current context size; each request carries about 3k of new input and output.
   - `dispatch` = your corrected estimate for that agent's round (Dispatching an agent, step 2).
   - Half, because your context stays for the rest of the run: every token you add is paid again on each later request. Late in a run `C` is large, so the formula favours dispatching.

Before editing, add an `orchestrator` row to `## Cost` with `direct <n> vs dispatch <m>` in the gap reason; fill the actual minutes after. The review that follows (spec-reviewer re-review, next code-reviewer round) still runs.

## When an agent returns

- Note its `Friction` items and, when it ran over, why, plus anything you hit yourself. Write them into `## Cost` (gap reasons) and `## Friction` in one Edit at the end of the phase (before a `PHASE:` line or at the wrap-up), not one Edit per agent: every Edit is one more request that re-reads your whole context.
- `CHECKPOINT`: read its approach and the `git status --short` the checkpoint-status hook attaches to the report (run it yourself if none came): record in `plans/<topic>.md` the files that really exist, not the ones the report lists (a checkpoint report has named files it never wrote). Either resume it once with a direction and a `Remaining: <minutes> min, $<usd>` line, your raw estimate of the remaining work (the hook models it like `estimate agent ... --resume`: the round just proved the estimate low, so the budget is never below your raw estimate; nothing is recorded for the history), or ask the user. A second `CHECKPOINT` on the same task goes to the user (in a headless worker run: `STATUS: needs-decision`); never extend a budget again yourself.
- `BLOCKED`: decide must-fix / TODO / ask the user (below), and tell the user why.
- Tell the user actual vs estimate, and the reason when it ran over.
- Before routing a problem an agent reports to another agent (a test or doc it calls outdated or wrong), verify it yourself: Grep and quote the line.
- Agent reports are in English. Summarise them for the user in the user's preferred language, with pass/fail counts, actual vs estimated cost, and anything the user must decide or tune by hand.

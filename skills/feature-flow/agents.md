# Working with agents

Loaded together with the class steps (feature-flow SKILL.md, Mode) for every class that dispatches subagents.

## Fresh agent or resume

A subagent's prompt cache lives 5 minutes (subagents get the 5-minute TTL even on a subscription: https://code.claude.com/docs/en/prompt-caching.md), so a resume after a pause re-writes the agent's whole context at 1.25× on its first request. For a fix round (review findings, a wrong test, a failing case) spawn a new agent and give it the findings verbatim with files and lines. Resume with SendMessage (by agent id, from the background agent's completion notification) only when the remaining work is larger than the agent's context and needs what it learned, such as finishing a plan after `CHECKPOINT`, or when a reviewer re-checks its own findings (spec-reviewer, code-reviewer). A resume's budget is the remaining work plus 1.25 × the agent's context: the budget hook counts only this round's tokens, so the re-write lands in the new round.

## Dispatching an agent

Every first message to an agent, and every resume that starts new work, contains:

1. **Goal, scope, done condition.**
2. **Estimate:** USD and minutes, from the task itself (step, round, size). Cost is in **USD**, priced per model and cache TTL from `~/.claude/hooks/prices.json`: the unit the budget hook counts, the cost hook records (`this_round.usd`) and the worker's `--max-budget-usd` uses. Estimate it as requests × (context × the cache-read price + new input × the cache-write price + output × the output price), plus a large file's size × the cache-write price once when it is first read; subagents write the 5-minute cache, a headless orchestrator the 1-hour one. Then run `python3 ~/.claude/skills/repo-plan/cost-history.py estimate agent <agent> <usd> <minutes>`: it multiplies by the agent's bias from the cost history (the copy new-worktree made) and by this branch's own actual/estimate for that agent so far, and prints the `Budget:` line. On `FAIL` (no history copy), don't estimate without it: in a headless worker run end with `STATUS: blocked`, otherwise ask the user.
3. **Budget line:** the `Budget: <T> min, $<U>` line `estimate` printed (add `, full` to let it run to 100% without the 80% checkpoint), then "Follow the budget rules in `agent-common`: `CHECKPOINT` at 80%, `BLOCKED` early."
4. **Test scope:** narrowest runs while iterating, the full suite only to confirm.
5. **No git writes:** "Don't commit, stash or switch branches; I commit." Fix rounds too.

The dispatch-guard hook reads the `Budget:` line: a dispatch or resume of a flow agent without it is denied. The budget hook then denies the agent's tool calls at 80% (unless `full`) and at 100% of the time or USD; every agent also has `maxTurns: 100`. In a headless worker run the same hook denies a dispatch whose budget plus the wrap-up margin doesn't fit what is left of the run: write the planned dispatch and its estimate into `plans/<topic>.md` and end the run with `STATUS: blocked`.

Then tell the user, in Traditional Chinese: which agent, what it does, the raw estimate, the bias applied and the corrected estimate.

While an agent works, you can keep discussing with the user, read files and edit `plans/` freely. Don't change files that git tracks or reports as untracked; if such a change can't wait, make it and tell the agent what changed in your next message to it. Never do so while a reviewer runs.

## Doing an agent's step yourself

Only two steps may be done without the agent: revising the test plan after a spec-reviewer round, and fixing doc-only findings after a code-reviewer round. Tests and implementation never: agent-guard keeps those roles apart. Do it yourself only when both hold; otherwise dispatch:

1. **Every finding is local:** wording, or adding or adjusting cases under an existing rule. No new rule, no changed decision, no new external fact.
2. **Cost:** `direct ≤ 0.5 × dispatch`, both in USD:
   - `direct = edits × r × (C × read + 3000 × write)` at your own model's prices (`prices.json`; a headless orchestrator writes the 1-hour cache): `edits` = separate places to change; `r` = requests per edit (1 when the file's text is already in your context, else 2); `C` = your current context size; each request carries about 3k of new input and output.
   - `dispatch` = your corrected estimate for that agent's round (Dispatching an agent, step 2).
   - Half, because your context stays for the rest of the run: every token you add is paid again on each later request. Late in a run `C` is large, so the formula favours dispatching.

Before editing, add an `orchestrator` row to `## Cost` with `direct <n> vs dispatch <m>` in the gap reason; fill the actual minutes after. The review that follows (spec-reviewer re-review, next code-reviewer round) still runs.

## When an agent returns

- Note its `Friction` items and, when it ran over, why, plus anything you hit yourself. Write them into `## Cost` (gap reasons) and `## Friction` in one Edit at the end of the phase (before a `PHASE:` line or at the wrap-up), not one Edit per agent: every Edit is one more request that re-reads your whole context.
- `CHECKPOINT`: read its approach. Either resume it with a direction (a new `Budget:` line), let it finish if it clearly can (`Budget:` with the remaining time and USD, `, full`), or ask the user. At 100% the hook stops it, and you always ask the user before writing any further budget; never extend a budget yourself.
- `BLOCKED`: decide must-fix / TODO / ask the user (below), and tell the user why.
- Tell the user actual vs estimate, and the reason when it ran over.
- Before routing a problem an agent reports to another agent (a test or doc it calls outdated or wrong), verify it yourself: Grep and quote the line.
- Agent reports are in English. Summarise them for the user in Traditional Chinese, with pass/fail counts, actual vs estimated cost, and anything the user must decide or tune by hand.

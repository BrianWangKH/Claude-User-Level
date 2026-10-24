# Test-first (logic, wiring)

Logic: state machines, calculations, data validation. Wiring: UI or scenes reacting to events, real components together.

## 1. Test plan

1. First check that `plans/<topic>.md` cites a source for every external fact it relies on (data source semantics, units, limits, API behaviour). If one is missing or marked "check first", don't plan around it: read the official docs and quote them in the plan when the answer matches the plan; otherwise, or if the docs don't answer it, stop with `needs-decision` (worker) or ask the user. Then spawn `qa-planner`: "Write `plans/tests/<topic>.md` for `plans/<topic>.md`". In auto mode the harness may refuse a subagent's Write of a report file ("Subagents should return findings as text"). Then the planner returns the full plan under `PLAN TEXT`, and you Write it verbatim to `plans/tests/<topic>.md`, without summarising or editing it. In revision rounds it returns only the changed sections, each under its original heading, and you apply them with Edit. Before this step, tell the user that in acceptEdits mode the planner can write the file itself, which saves you retyping the plan.
2. Spawn `spec-reviewer` on both files.
3. Revision rounds: spawn a **new** `qa-planner` (don't resume the old one: a resumed agent re-reads its whole context on every request and runs out of budget in 2–3 requests). Give it an explicit edit list (case ids and what changes) plus the spec-reviewer's report **verbatim**, never a summary. Then resume the same `spec-reviewer` until it APPROVEs.
4. Ask the user only questions that change what they see or do, in product terms. Decide internal contracts yourself and tell the user what you decided. Pass the answers on.

## 2. Tests

1. Spawn `qa` pointing at the reviewed test plan.
2. Run the tests yourself (project-flow). New tests must fail only on missing implementation, with no parse errors; existing tests must pass.
3. Commit the test files with explicit paths: "Add tests for <feature>".
4. Append the commit hash to `## QA commits` in `plans/<topic>.md`. Later steps always take the hash from there, never from `git log`.

## 3. Implementation

1. Spawn `developer` with the plan paths. The working tree should be clean (tests just committed); `agent-guard` adds a warning if it isn't. Fix rounds resume it with SendMessage and aren't checked.
2. If it reports a test as wrong, judge it against the plan: either the test goes back to `qa`, or the developer complies.
3. Verify yourself:
   - `agent-guard` checks every developer and qa round on its own (refs, and the paths in the project's `.claude/guard-paths.json`). An ALERT blocks your next tool call once: tell the user and stop. A reminder (e.g. a shared file changed) arrives as context: mention it to the user and have the code reviewer look at that diff
   - without `.claude/guard-paths.json`, only refs are checked; then also run `bash ~/.claude/skills/feature-flow/verify-untouched.sh <latest qa-commit> <protected paths...>`, which must exit 0
   - the suite passes, plus any project "done" checks in project-flow

## 4. Review

1. Spawn `code-reviewer` with the Agent tool, in the background, with exactly this prompt and nothing added (no plan content, no summary of the feature):
   > Review the current branch. Find the base yourself: the merge-base of `HEAD` with origin's default branch (`git merge-base origin/master HEAD`, or `origin/main` if there is no `origin/master`; never the local branch, which the main checkout doesn't pull). Review everything from that base to the working tree, following your agent instructions. End with your full report.

   Add only the estimate and the checkpoint rule from "Dispatching an agent".

   While a reviewer runs (every round), don't change files git tracks or reports as untracked, and don't run other agents; discussing, reading and editing `plans/` are fine (it is gitignored). `agent-guard` compares snapshots taken at the reviewer's start and stop, and any change in between counts as the reviewer's.
2. When it completes, record its agent id under `## Reviewer` in `plans/<topic>.md`. Later rounds take the id from there.
3. Compare its intent bullets with `plans/<topic>.md`: scope creep, missing rules, unintended behaviour changes. Treat any mismatch as a finding.
4. Rules the test plan marks as not automatically testable (in its traceability table): check each against the diff yourself and note the result under `## Reviewer`. A rule not followed is a must-fix finding for the developer. Skip if there are none.
5. Route must-fix findings:
   - test gaps → the same `qa`. Verify the new tests fail for the right reason, commit them, append the hash to `## QA commits`, **before** the developer round.
   - implementation issues → the same `developer`.
6. Later rounds: SendMessage to the id under `## Reviewer` ("fixed, please re-review"). Don't paraphrase the plan to it. If resuming fails, stop and tell the user; don't start a new reviewer on your own.
7. Repeat until APPROVE. Collect the reviewer's follow-ups for the TODO update.

For a true blind review (a fresh reviewer every round, never resumed), see `~/.claude/docs/blind-review.md`; use it only when the user asks.

## 5. Wrap up

Follow "Wrap up" in `SKILL.md`. Optionally run `/code-review` medium as a last pass before writing the PR description.

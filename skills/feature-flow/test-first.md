# Test-first (logic, wiring)

Logic: state machines, calculations, data validation. Wiring: UI or scenes reacting to events, real components together.

Wiring: every entry point a user runs (a CLI, `python -m <module>`, a script) gets at least one test that runs it in a subprocess exactly as the README or PR description says, from the repo root, on a temporary data directory, and checks the exit code and the output files. Tests that import and call `main()` don't catch a broken command line. Name these cases in the test plan.

## 1. Test plan

1. First check that `plans/<topic>.md` cites a source for every external fact it relies on (data source semantics, units, limits, API behaviour). If one is missing or marked "check first", don't plan around it: read the official docs and quote them in the plan when the answer matches the plan; otherwise, or if the docs don't answer it, stop with `needs-decision` (worker) or ask the user. Then spawn `qa-planner`: "Write `plans/tests/<topic>.md` for `plans/<topic>.md`". The planner writes the file itself: `~/.claude/settings.json` allows `Write(plans/tests/**)` and `Edit(plans/tests/**)` (relative to the session's working directory, so every repo's `plans/tests/`), so auto mode doesn't send those writes to its classifier, and plan-guard keeps qa-planner and qa inside `plans/tests/`. Its report names the file and the case count. In revision rounds the planner edits the file itself.
2. Spawn `spec-reviewer` on both files.
3. Revision rounds: edit the test plan yourself only when agents.md "Doing an agent's step yourself" allows it (local findings, `direct ≤ 0.5 × dispatch`); otherwise spawn a **new** `qa-planner` (don't resume the old one: a resumed agent re-reads its whole context on every request and runs out of budget in 2–3 requests). Give it an explicit edit list (case ids and what changes) plus every finding of the spec-reviewer's report **verbatim** (id, rule, text, suggested change), never a summary; the verdict line, praise and restated context may be left out. Then resume the same `spec-reviewer` until it APPROVEs.
4. Ask the user only questions that change what they see or do, in product terms. Decide internal contracts yourself and tell the user what you decided. Pass the answers on.

## 2. Tests

1. Spawn `qa` pointing at the reviewed test plan.
2. Run the tests yourself (project-flow). New tests must fail only on missing implementation, with no parse errors; existing tests must pass.
3. Commit the test files with explicit paths: "Add tests for <feature>".
4. Append the commit hash to `## QA commits` in `plans/<topic>.md`. Later steps always take the hash from there, never from `git log`. In a headless worker, make this Edit and the phase's `## Cost` / `## Friction` Edit (step 5) in the same response: one request instead of two.
5. **Headless worker only** (`CLAUDE_WORKER_MINUTES` is set): end your turn here. With the phase's `## Cost` and `## Friction` rows written, send a last message whose first line is `PHASE: tests-done`, followed by what is done and the next step (the worker-stop hook refuses it while an agent still runs). The wrapper compacts the conversation and resumes you: re-read `plans/<topic>.md` and continue with step 3.

## 3. Implementation

1. Spawn `developer` with the plan paths. The working tree should be clean (tests just committed); `agent-guard` adds a warning if it isn't. Fix rounds use a fresh `developer` with the findings verbatim (agents.md, "Fresh agent or resume").
2. If it reports a test as wrong, judge it against the plan: either the test goes back to `qa`, or the developer complies.
3. Verify yourself:
   - `agent-guard` checks every developer and qa round on its own (refs, and the paths in the project's `.claude/guard-paths.json`). An ALERT denies the call it arrives on, then every agent dispatch and `git commit` / `git merge` until the user answers: tell the user and stop. In a headless worker run, end the run with `STATUS: blocked` and quote the ALERT in full with its cause (`wt resume` lifts the hold). A reminder (e.g. a shared file changed) arrives as context: mention it to the user and have the code reviewer look at that diff
   - without `.claude/guard-paths.json`, only refs are checked; then also run `bash ~/.claude/skills/feature-flow/verify-untouched.sh <latest qa-commit> <protected paths...>`, which must exit 0
   - the suite passes, plus any project "done" checks in project-flow
4. Commit the implementation with explicit paths: "Implement <feature>".
5. **Headless worker only:** as in step 2.5 (the `## Cost` / `## Friction` Edit in the same response as the commit), with the first line `PHASE: dev-done`; continue with step 4.

## 4. Review

1. Spawn `code-reviewer` with the Agent tool, in the background, with a prompt holding only the `Budget:` line: the dispatch-guard hook replaces the prompt with the fixed plan-blind review prompt.

   While a reviewer runs (every round), don't change files git tracks or reports as untracked, and don't run other agents; discussing, reading and editing `plans/` are fine (it is gitignored). `agent-guard` compares snapshots taken at the reviewer's start and stop, and any change in between counts as the reviewer's.
2. When it completes, record its agent id under `## Reviewer` in `plans/<topic>.md`. Later rounds take the id from there.
3. Compare its intent bullets with `plans/<topic>.md`: scope creep, missing rules, unintended behaviour changes. Treat any mismatch as a finding.
4. Rules the test plan marks as not automatically testable (in its traceability table): check each against the diff yourself and note the result under `## Reviewer`. A rule not followed is a must-fix finding for the developer. Skip if there are none.
5. Route must-fix findings:
   - test gaps → a fresh `qa` with the findings verbatim. Verify the new tests fail for the right reason, commit them, append the hash to `## QA commits`, **before** the developer round.
   - implementation issues → a fresh `developer` with the findings verbatim.
6. Later rounds: SendMessage to the id under `## Reviewer` ("fixed, please re-review"). Don't paraphrase the plan to it. If resuming fails, stop and tell the user; don't start a new reviewer on your own.
7. Repeat until APPROVE. Collect the reviewer's follow-ups for the TODO update.

For a true blind review (a fresh reviewer every round, never resumed), see `~/.claude/docs/blind-review.md`; use it only when the user asks.

## 5. Wrap up

Follow "Wrap up" in `SKILL.md`. Optionally run `/code-review` medium as a last pass before writing the PR description.

---
name: clean-code
description: Code-quality rules that need judgement (names, one job per function, duplication, dead code, comments, error handling, change scope). The developer writes to them and the code reviewer checks against them, citing the rule id. Numeric limits and formatting are enforced by crap-gate and the project's formatter, not here. Preloaded by developer and code-reviewer.
user-invocable: false
---

# Clean code

Complexity numbers (CCN, CRAP, unexecuted lines) belong to crap-gate and formatting to the project's formatter and linter: don't restate or second-guess them. These rules cover what a tool can't judge. Project `CLAUDE.md` 「慣例」 and the plan win over this file.

| Id | Rule |
|---|---|
| C1 | **Names say what, in the domain's words.** A reader knows what a value holds or a function returns without reading its body. No abbreviations except the domain's own (e.g. `eps`, `roe`); booleans read as predicates (`is_listed`, `has_report`). |
| C2 | **One function, one job.** If the honest name needs "and", split it. Keep one level of abstraction per function: orchestration calls helpers, helpers do the detail. |
| C3 | **One rule, one place.** The same business rule or formula in two places becomes one function or constant. Don't abstract on speculation: a single use stays inline. |
| C4 | **No dead code.** No unused imports, parameters, variables or branches, no commented-out code, no "just in case" options nobody passes. |
| C5 | **Comments say why, not what.** Explain a non-obvious reason, a source (doc link, regulation) or a trade-off. No comment that restates the code; no `TODO` without a matching line in the project's TODO. |
| C6 | **Errors are loud.** No bare or broad catch that hides a failure, no silent default that turns missing data into a plausible number (a wrong number someone trusts is worse than a crash). Validate at the boundary where data enters; the core trusts its inputs. |
| C7 | **Pure core, effects at the edge.** Calculations take values and return values; I/O, clock, randomness and config loading sit at the edge and are passed in (global `CLAUDE.md` 「原則」). |
| C8 | **Explicit boundaries.** Public functions and module interfaces have type annotations and say their units (shares vs. thousands, TWD vs. thousand TWD). |
| C9 | **Change only what the task needs.** Follow the file's existing patterns; no unrelated renames, reformatting or refactors in the diff. A refactor that the task really needs is mentioned in the report. |

## Developer

Apply the rules while writing; they never justify changing a test or skipping a plan rule. If a rule conflicts with the plan or the existing code's pattern, follow the plan or pattern and mention it in the report in one line.

## Code reviewer

A quality finding cites the rule (`C3`) with `file:line` and a concrete change. C6 findings that can produce a wrong number count as correctness, not style. Everything else is a quality finding; the orchestrator decides must-fix vs TODO (feature-flow 「Must-fix vs TODO」). Don't report what crap-gate or the linter already reports.

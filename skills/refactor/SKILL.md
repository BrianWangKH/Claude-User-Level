---
name: refactor
description: How to change the structure of working code without losing behaviour - lock today's behaviour with characterization tests first, compare old and new over generated inputs, keep a removed-behaviour table for the commit message, record (not fix) the bugs found on the way, small steps with tests after each. For the home and for repos. Preloaded by home-drafter; the repo developer does not preload it and loads it on demand (Skill tool) when a task is a refactor.
---

# Refactor

A refactor changes how code is organised, never what it does for its callers. Every step below exists to prove that, or to name each deliberate exception. The industry names for the techniques: characterization tests (Michael Feathers, *Working Effectively with Legacy Code*) and the refactoring catalogue (Martin Fowler, *Refactoring*). Use the names; this file holds the rules.

The project's own rules win where they are stricter (its `CLAUDE.md`, the plan, the home's `hooks/RULES.md` for home code, not included in the public version).

## 1. Lock today's behaviour first

Before changing a line, make sure a test fails when the behaviour you are about to move changes.

- List the behaviours the code you will touch has: each output, each side effect (files, state, events, exit codes, messages a person or program reads), each error path.
- For each, find the test that fails when it changes. None: write a characterization test against the **current** code and see it pass there, before the change. It asserts what the code does today, right or wrong; a wrong behaviour you find goes to section 5, not into the test as the "fixed" value.
- Output other programs parse (command lines, JSON, report lines): keep a golden file of the current bytes for a fixed set of inputs and compare byte for byte. Example: `hooks/tests/test_wt_cli.py` with `hooks/tests/wt_cli.golden.json` (stdout, stderr and exit code of each command per flow mode, the scratch path normalised; only states that don't depend on the wall clock).
- Prove each new locking test bites: break the behaviour by hand (one mutation), see the test fail, put it back, see it pass. A test that never failed proves nothing.

## 2. Old vs new over generated inputs

When the change replaces a decision (which mode, which branch, which registration, which rows), compare the old rule and the new one over every combination of inputs, not a few examples.

- Keep the old rule as test data: a module in the tests that transcribes how each old caller decided, as pure functions of the inputs. It is never imported by a program. Example: `hooks/tests/flow_mode_baseline.py`.
- Check the transcription against the real old code first, on a sample of the matrix, before trusting it as the baseline.
- Build the matrix from every input dimension and its edge values (absent, empty, garbage, each valid value, conflicting sources) and run old and new on every cell. Example: `test_old_vs_new_every_change_is_a_named_one_and_valid_setups_keep_their_answer` in `hooks/tests/test_flow_mode_source.py` (records x variable x run record x caller).
- Classify every cell: same answer, or a change with a named reason. A change no reason explains fails the test. Print the counts per reason so the report can quote them.
- A registration or table that replaces an older one: keep the old one as a snapshot and assert every old entry is replaced or listed as removed with a reason, and every replacement names a real old entry. Example: `hooks/tests/old-registrations.json` with `test_every_old_registration_is_replaced_or_listed_as_removed` in `hooks/tests/test_hook_entries.py`.
- Reading code incrementally instead of whole: include every boundary in the cut points (mid-line, after a newline, a last line without a newline).
- Equivalence proves the migration, not that each behaviour is protected; section 1 does that.

## 3. Removed-behaviour table

Every behaviour the change removes or changes gets a row; the table goes into the commit message.

| Behaviour | Why removed or changed | Who owns it now | Locking test |
|---|---|---|---|
| what the old code did, one behaviour per row | the reason | `path:function`, or `dropped on purpose` | the test that fails if the new owner stops doing it; for a dropped one, the test that shows it is gone, or `none` with why |

- Rules, docs and markdown count: a rule removed from a skill or playbook is a behaviour of the flow.
- A row without an owner and without a reason is a blocker: the reviewer sends the change back.
- The old files themselves stay in version control history; no archive copies.

## 4. Small steps

- One move per step (extract, rename, inline, move a file, swap one caller to the new function); run the narrowest tests after each, the full suite at the end.
- Keep the old and new side by side while callers move over, then remove the old one in its own step with its row in the table.
- After a move or rename, update every reference (code, docs, tests); a path checker such as the home's `config-inventory.py` `missing_ref` (not included in the public version) catches the ones you missed.
- Never change a check's threshold, a snapshot or a value to make a number pass; a check that fails tells you what the change broke.

## 5. Bugs found on the way

Record, don't fix: file and line, what happens, which direction it errs (fails open, wrong number, data loss) and how you saw it. Fix it only when the change already rewrites that code; then it gets its own row in the table and its own test. A bug in the dangerous direction (a guard that lets through, lost data) is reported at the top of your report.

## 6. Report

- the removed-behaviour table
- the locking tests you added, each with its mutation check (broken: failed, restored: passed)
- the old-vs-new counts per reason
- bugs found, recorded not fixed

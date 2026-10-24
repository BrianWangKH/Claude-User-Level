# Small fix

A single bug fix. The plan is short: how to reproduce, expected behaviour, scope.

1. `qa` writes a regression test.
2. Run it **before the fix**: it must fail for the reported bug, not a parse or setup error. If it passes, send it back.
3. Commit the test and record the hash under `## QA commits`.
4. `developer` fixes the bug. Verify as in step 3 of `test-first.md`.
5. Run `/code-review` low.
6. Follow "Wrap up" in `SKILL.md`.

Tell the user you took the short path.

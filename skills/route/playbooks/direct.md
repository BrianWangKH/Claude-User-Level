# Playbook: direct change (explicitly asked, outside the worktree flow)

Use only when the user explicitly asks you to make the change here; the default for repo work is a topic through `/repo-plan`.

1. **Confirm scope in one line**: files you will touch, what you won't. Anything that grows past a small change → suggest a topic instead.
2. **Branch rules still hold**: on the default branch, create a `<type>/<scope>/<topic>` branch before committing (global `CLAUDE.md` 「Git」). Never touch a worktree whose worker is running.
3. **Test first when it is logic**: a failing test, then the change, then the project's test command (project `CLAUDE.md`).
   Load skill `clean-code` before writing code and follow C1–C9; the project's formatter and linter on the changed paths only.
4. **Commit only when asked**, with explicit paths. Never push; give the user the push command.
5. Report what changed, the test output, and anything left for TODO.

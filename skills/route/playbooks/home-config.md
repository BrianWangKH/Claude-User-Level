# Playbook: change ~/.claude

You can read most of `~/.claude` with Bash `cat` but can't write it (sandbox), except `~/.claude/docs`. `settings.json` you can't even read.

1. **Size it.** Raised in the middle of project work → give a short assessment, add a row to the repo's `plans/config-update/home-settings-plan.md` (待辦 table: 項目 | 狀況 | 下一步), and go back to the project. Draft now only when the user asks for it.
2. **Search before inventing.** A flow step blocked or misbehaving → `grep -rn` the skills, hooks and `CLAUDE.md` for the rule that already covers it. Diagnose from raw evidence (`ops/evidence.md`), never from a plan's or an earlier write-up's stated cause.
3. **Fix at the source.** A guard that blocked something is fixed where the wrong input came from (rule text, generator, flow), never by widening the hook. A rule that must always hold becomes a hook or script; text only explains. Skills hold rules, not incident stories.
4. **Check the docs.** Any Claude Code behaviour the change relies on is checked in the official docs and cited (`ops/cite.md`).
5. **Draft full files** with `ops/home-draft.md`. Any change to the flow or architecture also updates `~/.claude/README.md`; a new skill or role also updates the index table in the global `CLAUDE.md`.
6. **Test what can be tested** in the draft tree (hook tests with the hooks dir pointed at the draft, `bash -n`, a dry run of a script).
7. Give the apply steps (from `ops/home-draft.md`), then update the row in `home-settings-plan.md`: drafted / applied, and what is still untested.

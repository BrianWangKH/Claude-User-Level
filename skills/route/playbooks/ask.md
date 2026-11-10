# Playbook: understand the code (no changes)

1. Read-only: no Edit, Write or state-changing commands.
2. Find before reading: `grep -rn` / `find` to locate, then Read only the relevant ranges. A sweep across many directories → an Explore agent, keep only its conclusion.
3. Answer with `path:line` references for every claim about the code; say what you didn't check.
4. The answer reveals a bug or a missing piece → mention it in one line and offer to route it (`playbooks/debug.md`, or a topic via `/repo-plan`); don't fix it.
5. Retro: `ops/retro.md`.

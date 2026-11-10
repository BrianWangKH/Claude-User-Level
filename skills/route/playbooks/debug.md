# Playbook: debugging ("it used to work", "why did X happen")

1. **The user's facts are constraints.** What they saw, when it last worked, what changed: take it as given and say back the short version before guessing.
2. **Reproduce or find the record first.** Run the failing command once, or locate the raw record (`ops/evidence.md`). No reproduction and no record → say so; don't continue on a guess.
3. **Compare working vs broken** on concrete evidence: versions (a session transcript's `"version"` follows the session start time), config diffs, `git log` between the two, the failing input.
4. **Name the cause only with evidence** that ties it to the symptom. A correlation (e.g. a version changed in between) is labelled a hypothesis and you say what would confirm it.
5. **Fix by class:** inside the repo and small → a fix topic through `/repo-plan` (or `playbooks/direct.md` if the user asks to fix it here); in `~/.claude` → `playbooks/home-config.md`; can't be fixed now → record it with the evidence.
6. Report: symptom, evidence, cause (confirmed or hypothesis), fix or where it was recorded.
7. Retro: `ops/retro.md`.

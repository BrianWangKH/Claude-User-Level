# Playbook: run it / manual verification

1. **What exactly is being checked?** Take the steps from the source: a worktree's `PR_DESCRIPTION.md` "manual verification", a plan's done criteria, or the user's words. List them back as a numbered checklist.
2. **Split by kind.**
   - Non-UI (CLI, scripts, API calls, data checks): run them here, in front of the user, one per step, and show the real output against the expected result.
   - UI (browser, Streamlit, anything needing eyes): don't run; append the steps to the main checkout's `plans/manual-verify.md` (topic, steps, expected) for the user to do later, unless the user wants to do it now with you.
3. **Use real data where the project keeps it** (project `CLAUDE.md` says where); never re-fetch from a paid or quota-limited source just to verify. After a passed verification that produced new real data, move it to that place.
4. **Pass/fail per step**, with output. A failure → `playbooks/debug.md`; don't patch it silently.
5. Long-running server for a check → start it in the background and stop it when the check is done.
6. Retro: `ops/retro.md`.

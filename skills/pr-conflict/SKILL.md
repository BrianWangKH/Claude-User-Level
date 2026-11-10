---
name: pr-conflict
description: Check a topic's open PR for conflicts with origin's default branch after a fresh fetch, and hand the merge to the topic's worker, resumed in merge mode in its worktree. Main checkout only.
when_to_use: When the user says a topic's PR shows conflicts, or asks whether it still merges cleanly.
argument-hint: "<topic>"
disable-model-invocation: true
---

A hook fetches origin when this skill is invoked. Run exactly this command, with nothing added:

```bash
bash ~/.claude/skills/pr-conflict/pr-conflict.sh <topic>
```

- `FAIL`: report the reason to the user and stop.
- `MERGED` / `OK`: tell the user there is nothing to resolve. No merge commit is made just because the default branch moved.
- `CONFLICT:`: list the files for the user, then **resume the topic's worker in merge mode**: the session that built the branch continues with feature-flow `merge.md` (its last run ended with a compaction, so it re-reads only the summary, and it already knows the branch). Follow `~/.claude/skills/repo-plan/handoff.md` "Worker" (estimate, `plans/worker/<topic>.json`, start, wait) with these differences:
  - **Estimate** the merge's own work (reading both sides, resolving, the full suite, a merge review when hunks are resolved by hand): `python3 ~/.claude/skills/repo-plan/cost-history.py estimate branch <type>/<scope> <class> <usd> <minutes> --merge --dev-rounds <n>` (`<n>` developer rounds for implementation conflicts, 0 if none; never `--reviews`: a merge review runs no scan). The estimate model comes from earlier merge runs only, and the json it prints carries `"mode": "merge"`.
  - **Start** with `wt resume <topic>`, never `wt launch`: typed, the pane's button or auto mode all submit it, and worker-control.py sends the merge prompt because of the json's `mode`. It keeps the mode for every later resume of the topic.
  - A topic built without a worker has no session to resume (`wt resume` says so): give the user `/feature-flow <topic> merge` to send in a session opened in the worktree.

  The worktree's `plans/<topic>.md` doesn't need a new section: `merge.md` carries its own worker rules.
- When the run ends, do what `run-result.py`'s `Next:` line says (its `PURPOSE: merge` line marks the merge run). `needs-decision` / `blocked`: answers into the worktree plan, re-estimate with `--merge`, `wt resume <topic>`. `done` → the push command; the PR updates itself.

The worktree session never fetches; the merge uses the `origin/<default>` this fetch brought in (worktrees share the repo's refs).

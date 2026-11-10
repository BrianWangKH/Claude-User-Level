# Operation: gather raw evidence

Look at the records yourself; a plan's or an earlier write-up's explanation is only a hypothesis.

| Question | Where to look |
|---|---|
| what a worker run did, how it ended | `python3 ~/.claude/skills/repo-plan/run-result.py <topic> [<run>]` first: its KEY lines (`PHASE:`, `BUSY:`, `TODO:`) and the `evidence:` and `detail:` lines name each file and field it read; then those files in `<worktree>/.hook-only/runs/` (`<n>.json` the run record, `est-<n>.json`, `start-<n>`, `exit-<n>`, `result-<n>.json`) |
| what a subagent cost, why it stopped | `<worktree>/.hook-only/cost/<id>.jsonl` (not `.start.json`), `.hook-only/budget/events.jsonl` |
| why a hook blocked | the hook's own log under `.hook-only/` (`agent-guard/guard.log`, `quality/`), then the hook source in `~/.claude/hooks/` when it is readable |
| what changed in the repo | `git log --oneline <a>..<b>`, `git diff`, `git status --short` (character-device entries are sandbox placeholders, not changes) |
| which Claude Code version ran | the session transcript's `"version"` field (the user runs transcript scripts outside the sandbox: `~/.claude/worker-cost-scripts`) |

Rules:
- An unparsed or missing field is "unknown", not zero.
- Quote the line you rely on (path + the line), so the user can check it.
- Never poll a running worker's PID (the sandbox can't see it); wait only with `run-result.py <topic> --wait` (it reads the run's status and returns on ended, orphan or unknown).

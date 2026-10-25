# Starting a worker by hand

Used by `handoff.md`, "Worker", only when `~/.claude/hooks/worker-control.py` is missing or broken. The planning session `cat`s this file and fills in the command.

Without the worker-control hook (no phase compaction: that is the hook's wrapper `run-worker.sh`), give the user exactly this, with `<topic>`, the run number `<n>` (1, then 2, … for each resume) and absolute paths filled in:

```bash
claude auth status --text && cd <worktree> && mkdir -p .hook-only/runs && date -Is > .hook-only/runs/start-<n> && echo '{"minutes": <min>, "usd": <est_usd>, "cap": <usd>}' > .hook-only/runs/est-<n>.json && CLAUDE_WORKER_MINUTES=<min> claude -p "/feature-flow <topic>" --permission-mode auto --max-budget-usd <usd> --output-format json > .hook-only/runs/result-<n>.json; echo "$? $(date -Is)" > .hook-only/runs/exit-<n>
```

- `est-<n>.json` holds this run's estimate (`<usd>` = est_usd × 1.2) for `run-result.py` and `/finish-worktree`; the hook writes it when there is one.
- `--output-format json` writes one result line when the run ends; no transcript is kept, so nothing large is ever read here.
- `exit-<n>` is always written (`;`), with the exit code and end time, also when claude crashes or is killed and writes no JSON.
- The run files live in the worktree's `.hook-only/` (agents can't write there, so the worker can't alter them) and are removed with the worktree; `/finish-worktree` copies the numbers into the cost history first.
- No `--permission-prompts none`: in `-p` a step that needs approval is denied at once (no hang), and the worker reports it as `STATUS: blocked`.
- Resume after the user answers: the same command with `--resume <session_id>` (from the previous result file) right after `-p`, and the next `<n>`.

Then wait and judge the run exactly as with the hook: `run-result.py <topic> --wait` (skill repo-plan).

#!/usr/bin/env bash
# One headless worker run, started by worker-control.py in its own process group.
# Usage: run-worker.sh <topic> <worktree> <run> <new|resume> <session-id> <minutes> <usd> <agent|""> <prompt>
# Always writes .hook-only/runs/exit-<run> ("<code> <time>"); 90 = not logged in.
set -u
topic=$1 worktree=$2 run=$3 mode=$4 session_id=$5 minutes=$6 usd=$7 agent=$8 prompt=$9
export PATH="$HOME/.local/bin:/usr/local/go/bin:$PATH"
runs="$worktree/.hook-only/runs"
cd "$worktree" || exit 1
date -Is > "$runs/start-$run"

args=(-p "$prompt" --permission-mode auto --max-budget-usd "$usd" --output-format json)
if [ "$mode" = new ]; then args+=(--session-id "$session_id"); else args+=(--resume "$session_id"); fi
if [ -n "$agent" ]; then args+=(--agent "$agent"); fi

if claude auth status --text > "$runs/auth-$run.log" 2>&1; then
  CLAUDE_WORKER_MINUTES=$minutes claude "${args[@]}" > "$runs/result-$run.json" 2> "$runs/stderr-$run.log"
  code=$?
else
  code=90
fi
echo "$code $(date -Is)" > "$runs/exit-$run"

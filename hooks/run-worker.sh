#!/usr/bin/env bash
# One headless worker run, started by worker-control.py in its own process group.
# Usage: run-worker.sh <topic> <worktree> <run> <new|resume> <session-id> <minutes> <usd> <agent|""> <prompt>
# Always writes .hook-only/runs/exit-<run> ("<code> <time>"); 90 = not logged in.
#
# Phases: at a phase boundary the orchestrator ends its turn with a first line `PHASE: <name>`
# (feature-flow test-first.md). The wrapper then compacts the conversation while the cache is still
# warm and resumes it with continue_prompt, at most worker_max_phases times (budget-defaults.json).
# After the run's real end (a `STATUS:` line) it compacts once more, so the next `wt resume`
# re-writes only the summary. Never after `wt stop` or an error.
# Files per run in .hook-only/runs/: result-<run>.json (the last turn, with the STATUS line),
# phase-<run>-<k>.json (turns that ended with PHASE), compact-<run>-<k>.json (compactions).
# total_cost_usd in these files is cumulative for the session, and --max-budget-usd counts one
# invocation only, so each invocation gets what is left of <usd> for this run.
set -u
topic=$1 worktree=$2 run=$3 mode=$4 session_id=$5 minutes=$6 usd=$7 agent=$8 prompt=$9
export PATH="$HOME/.local/bin:/usr/local/go/bin:$PATH"
runs="$worktree/.hook-only/runs"
defaults="$HOME/.claude/hooks/budget-defaults.json"
cd "$worktree" || exit 1
date -Is > "$runs/start-$run"
stopped=0
trap 'stopped=1' INT TERM

compact_prompt="/compact Keep: the topic and plans/$topic.md, the branch, the phase just finished and the next step, commit hashes, agent ids and open decisions. Drop subagent reports, tool output and file contents: the plan, git and the files hold them."
continue_prompt="The conversation was compacted at a phase boundary. Re-read plans/$topic.md, load the class step files again (feature-flow, Mode: one cat) and continue with the next phase."

json() {  # $1 result file, $2 first_line | cost
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
if sys.argv[2] == "first_line":
    print((str(d.get("result") or "").strip().splitlines() or [""])[0])
else:
    print(d.get("total_cost_usd") or 0)
PY
}

last_file_of() {  # run $1's newest file: its highest-numbered compact file, else its result file
  local r=$1 best="" k=0 f n
  for f in "$runs"/compact-"$r"-*.json; do
    [ -s "$f" ] || continue
    n=${f##*-}; n=${n%.json}
    if [ "$n" -gt "$k" ]; then k=$n; best=$f; fi
  done
  if [ -n "$best" ]; then echo "$best"; elif [ -s "$runs/result-$r.json" ]; then echo "$runs/result-$r.json"; fi
}

base=0  # session cost before this run
if [ "$mode" = resume ]; then
  for ((r = run - 1; r >= 1; r--)); do
    f=$(last_file_of "$r")
    if [ -n "$f" ]; then base=$(json "$f" cost); break; fi
  done
fi
last=""  # this run's newest result file

remaining() {
  python3 - "$usd" "$base" "${last:+$(json "$last" cost)}" <<'PY'
import sys
usd, base = float(sys.argv[1]), float(sys.argv[2])
now = float(sys.argv[3]) if sys.argv[3] else base
print(f"{max(usd - (now - base), 0):.2f}")
PY
}

next_mode=$mode
invoke() {  # $1 output file, $2 prompt, $3 "compact" for the wrapper's own /compact (worker-stop.py skips it)
  local budget code
  budget=$(remaining)
  if ! python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= 0.05 else 1)' "$budget"; then
    echo "run-worker: run $run budget used up before: ${2:0:40}" >> "$runs/stderr-$run.log"
    return 99
  fi
  local -a args=(-p "$2" --permission-mode auto --max-budget-usd "$budget" --output-format json)
  if [ "$next_mode" = new ]; then args+=(--session-id "$session_id"); else args+=(--resume "$session_id"); fi
  if [ -n "$agent" ]; then args+=(--agent "$agent"); fi
  CLAUDE_WORKER_COMPACT=${3:+1} CLAUDE_WORKER_MINUTES=$minutes claude "${args[@]}" > "$1" 2>> "$runs/stderr-$run.log"
  code=$?
  next_mode=resume
  if [ -s "$1" ]; then last=$1; fi
  return $code
}

if ! claude auth status --text > "$runs/auth-$run.log" 2>&1; then
  echo "90 $(date -Is)" > "$runs/exit-$run"
  exit 0
fi
max_phases=$(python3 -c 'import json, sys; print(int(json.load(open(sys.argv[1])).get("worker_max_phases", 0)))' "$defaults" 2>/dev/null || echo 0)

result="$runs/result-$run.json"
invoke "$result" "$prompt"
code=$?
k=0
while [ "$stopped" = 0 ] && [ "$code" = 0 ] && [ "$k" -lt "$max_phases" ] \
    && [[ "$(json "$result" first_line)" == PHASE:* ]]; do
  k=$((k + 1))
  mv "$result" "$runs/phase-$run-$k.json"
  last="$runs/phase-$run-$k.json"
  invoke "$runs/compact-$run-$k.json" "$compact_prompt" compact || break
  [ "$stopped" = 0 ] || break
  invoke "$result" "$continue_prompt"
  code=$?
done
# A run that stopped right after a phase turn keeps that turn as its result.
if [ ! -s "$result" ] && [ "$k" -gt 0 ]; then cp "$runs/phase-$run-$k.json" "$result"; fi
if [ "$stopped" = 0 ] && [ "$code" = 0 ] && [[ "$(json "$result" first_line)" == STATUS:* ]]; then
  invoke "$runs/compact-$run-$((k + 1)).json" "$compact_prompt" compact
fi
echo "$code $(date -Is)" > "$runs/exit-$run"

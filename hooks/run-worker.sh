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
# CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS = the run's minutes: claude -p otherwise stops background work
# after 10 minutes of idle waiting (headless docs, "Background tasks at exit"), and a background
# code-reviewer held at hand-back by the mutation scan (~10 min) was killed with its report
# (seen in a real run). The run's own limits (worker-budget, --max-budget-usd) apply.
# Automatic resume: when dispatch-guard.py left auto-resume-<run>.json (the run stopped only because the
# planned dispatch didn't fit), worker-control.py auto decides, before exit-<run> is written, whether to
# start one bounded resume (its decision goes to auto-<run>.json; WT_MAIN_DIR comes from worker-control).
# Local mode (FLOW_INTEGRATION=local): main-<run> records where the main checkout's HEAD was at the start,
# and a run that ends `STATUS: done` goes to auto-merge.py, which merges the branch into the local main
# when every recorded condition holds (decision in merge-<run>.json), before exit-<run> is written.
# A real-data failure (verify.py or verifier.py) makes auto-merge.py ask worker-control.py auto-fix for a
# fix run, which resumes the session with the findings (auto-<run>.json "fix").
set -u
topic=$1 worktree=$2 run=$3 mode=$4 session_id=$5 minutes=$6 usd=$7 agent=$8 prompt=$9
export PATH="$HOME/.local/bin:/usr/local/go/bin:$PATH"
runs="$worktree/.hook-only/runs"
defaults="$HOME/.claude/hooks/budget-defaults.json"
cd "$worktree" || exit 1
# The topic's own flow mode (worker-control.py records it at the first launch): every hook of this run,
# auto-merge, verify and auto-finish follow it, whatever `wt local` says now. No record (older topics):
# the inherited FLOW_INTEGRATION.
if [ -f "$worktree/.hook-only/flow-mode" ]; then
  if [ "$(tr -d '[:space:]' < "$worktree/.hook-only/flow-mode")" = "local" ]; then
    export FLOW_INTEGRATION=local
  else
    unset FLOW_INTEGRATION
  fi
fi
date -Is > "$runs/start-$run"
if [ "${FLOW_INTEGRATION:-}" = "local" ] && [ -n "${WT_MAIN_DIR:-}" ]; then
  git -C "$WT_MAIN_DIR" rev-parse HEAD > "$runs/main-$run" 2>/dev/null || rm -f "$runs/main-$run"
fi
stopped=0
trap 'stopped=1' INT TERM

compact_prompt="/compact Keep: the topic and plans/$topic.md, the branch, the phase just finished and the next step, commit hashes, agent ids and open decisions. Drop subagent reports, tool output and file contents: the plan, git and the files hold them."
continue_prompt="The conversation was compacted at a phase boundary. Re-read plans/$topic.md, load the class step files again (feature-flow, Mode: one cat) and continue with the next phase."
if [ -f "$worktree/.hook-only/flow/scripted" ]; then
  # The scripted flow (flow-next.py): the next step is in next.md, the rules in scripted.md.
  continue_prompt="The conversation was compacted at a phase boundary. Load your rules with one Bash call, \`cat ~/.claude/skills/feature-flow/scripted.md\`, then Read .hook-only/flow/next.md and do what it says."
fi

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

last_file_of() {  # run $1's newest file: the one with the highest total_cost_usd (cumulative)
  # A run without a STATUS line has no final compact, so its result file is newer than its highest
  # compact file (seen in a real run: compact-1-2 $4.34, result-1 $5.36).
  local r=$1 best="" top=-1 f c
  for f in "$runs"/result-"$r".json "$runs"/compact-"$r"-*.json "$runs"/phase-"$r"-*.json; do
    [ -s "$f" ] || continue
    c=$(json "$f" cost)
    if python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)' "$c" "$top"; then
      top=$c; best=$f
    fi
  done
  if [ -n "$best" ]; then echo "$best"; fi
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
  CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=$((minutes * 60000)) CLAUDE_WORKER_COMPACT=${3:+1} CLAUDE_WORKER_MINUTES=$minutes \
    claude "${args[@]}" > "$1" 2>> "$runs/stderr-$run.log"
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
# The run's end as an event (wt_events.py): the person's next prompt and the pane's toast carry it. Before
# verify and auto-merge, so their events follow it.
if [ -n "${WT_MAIN_DIR:-}" ]; then
  if [ "$stopped" = 1 ]; then ended="stopped (wt stop)"; elif [ "$code" != 0 ]; then ended="exit $code"; else ended="$(json "$result" first_line)"; fi
  python3 "$HOME/.claude/hooks/wt_events.py" emit "$WT_MAIN_DIR" run-ended "$topic" "run $run: ${ended:-no STATUS line}" >> "$runs/stderr-$run.log" 2>&1 || true
fi
# Real-data checks (verify.py): only when the planning session wrote <main>/plans/verify/<topic>.json;
# before auto-merge, which merges only on a passed result when the spec exists. Then the second layer
# (verifier.py: a verifier agent judges the approved plan's rules on the real data), which runs only when
# the spec has a "verifier" section and verify.py passed; auto-merge's condition 9 reads its result, and a
# failure of either layer goes back to the worker as a fix run (auto-merge.py, worker-control.py auto-fix).
if [ "$stopped" = 0 ] && [ "$code" = 0 ] && [ -n "${WT_MAIN_DIR:-}" ] && [ -f "$WT_MAIN_DIR/plans/verify/$topic.json" ] \
    && [[ "$(json "$result" first_line)" == "STATUS: done"* ]]; then
  python3 "$HOME/.claude/hooks/verify.py" "$worktree" "$topic" "$run" >> "$runs/stderr-$run.log" 2>&1
  python3 "$HOME/.claude/hooks/verifier.py" "$worktree" "$topic" "$run" >> "$runs/stderr-$run.log" 2>&1
fi
if [ "$stopped" = 0 ] && [ "$code" = 0 ] && [ "${FLOW_INTEGRATION:-}" = "local" ] \
    && [[ "$(json "$result" first_line)" == "STATUS: done"* ]]; then
  python3 "$HOME/.claude/hooks/auto-merge.py" "$worktree" "$topic" "$run" >> "$runs/stderr-$run.log" 2>&1
fi
if [ "$stopped" = 0 ] && [ "$code" = 0 ] && [ -s "$runs/auto-resume-$run.json" ] \
    && [[ "$(json "$result" first_line)" == "STATUS: blocked"* ]]; then
  python3 "$HOME/.claude/hooks/worker-control.py" auto "$worktree" "$topic" "$run" >> "$runs/stderr-$run.log" 2>&1
fi
echo "$code $(date -Is)" > "$runs/exit-$run"
# The topic queue (wt_queue.py, local mode): with this run's exit file written its topic no longer counts
# as busy (an automatic resume or merge run it decided still does), and a merge auto-merge.py just made
# may free a dependent: start whatever may start now.
if [ -n "${WT_MAIN_DIR:-}" ]; then
  python3 "$HOME/.claude/hooks/wt_queue.py" tick "$WT_MAIN_DIR" >> "$runs/stderr-$run.log" 2>&1 || true
fi

"""The run wrapper as it was before migration step 6 of the run record (hooks/run-worker.sh, 281 lines, its wrap-up
in bash): the baseline test_run_steps.py drives next to today's run-worker.sh over the same scenarios, to show the
move of the wrap-up into run-steps.py changes no program call and no file of today's layout. Unused since migration
step 6 except as that baseline. Test data, never run as a home script: write SCRIPT to a file and run it with bash.
"""

SCRIPT = r"""#!/usr/bin/env bash
# One headless worker run, started by worker-control.py in its own process group.
# Usage: run-worker.sh <topic> <worktree> <run> <new|resume> <session-id> <minutes> <usd> <agent|""> <prompt>
# Always writes .hook-only/runs/exit-<run> ("<code> <time>"); 90 = not logged in.
#
# Phases: at a phase boundary the orchestrator ends its turn with a first line `PHASE: <name>`
# (feature-flow test-first.md). The wrapper then compacts the conversation while the cache is still
# warm and resumes it with continue_prompt, at most worker_phases_max times (home budget-defaults, read through ../config/homeconfig.py relative to this script).
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
# Automatic resume: when dispatch_guard.py left auto-resume-<run>.json (the run stopped only because the
# planned dispatch didn't fit), worker-control.py auto decides, before exit-<run> is written, whether to
# start one bounded resume (its decision goes to auto-<run>.json; WT_MAIN_DIR comes from worker-control).
# Local mode (FLOW_INTEGRATION=local): main-<run> records where the main checkout's HEAD was at the start,
# and a run that ends `STATUS: done` goes to auto-merge.py, which merges the branch into the local main
# when every recorded condition holds (decision in merge-<run>.json), before exit-<run> is written.
# A real-data failure (verify.py or verifier.py) makes auto-merge.py ask worker-control.py auto-fix for a
# fix run, which resumes the session with the findings (auto-<run>.json "fix").
# The run record (hooks/runstate.py, migration step 2: dual write): next to today's files the wrapper hands
# each fact to run-steps.py (its start, the main head, every claude invocation, claude's end); a background
# loop writes the heartbeat every heartbeat_interval_seconds (budget-defaults run_state) while the wrapper
# lives; the run's exit (reason and code) goes into the record when exit-<run> is written (run-steps ended), and
# the EXIT trap stops that loop, writes the wrapper's own exit (and the run's, on an early way out the shell
# sees), then compares the record's story with today's files (status-diverged, once per difference). A record
# write that fails is reported once and never changes the run.
# claude runs as `( trap - INT; exec claude ... ) &` and the wrapper waits for it, so an INT or TERM reaches
# the wrapper's trap at once: the trap forwards the signal to claude, and the wrapper keeps waiting until
# claude has really exited and takes claude's exit code (a background child would otherwise ignore INT, and
# the wrapper would go on while claude still runs).
# The whole body is the function main, called on the last line, so overwriting this file while a wrapper runs
# (applying a home change) can't feed the running shell new lines: bash has read all of it before main runs.
set -u

on_signal() {  # $1 INT or TERM: the run was stopped; claude gets the signal too (also when only this pid got it)
  stopped=1
  signal_seen=$1
  if [ -n "${child:-}" ]; then kill -s "$1" "$child" 2>/dev/null || true; fi
}

wait_claude() {  # wait for claude ($child) until it has really exited; its exit code in claude_code
  # A trapped signal ends `wait` early (> 128) while claude may still run: wait until it is gone.
  wait "$child"
  while kill -0 "$child" 2>/dev/null; do wait "$child"; done
  # bash keeps an exited child's status, so this returns claude's own code, never the interrupted wait's.
  wait "$child"
  claude_code=$?
}

heartbeat_loop() {  # background: one beat every $interval seconds while the wrapper ($wrapper_pid) lives
  local pause=""
  trap '' INT TERM  # a stop signal ends the run, not the beats: the wrapper still wraps up
  trap 'if [ -n "$pause" ]; then kill -KILL "$pause" 2>/dev/null; fi; exit 0' USR1
  while kill -0 "$wrapper_pid" 2>/dev/null; do
    python3 "$steps" beat "$worktree" "$run" >> "$runs/stderr-$run.log" 2>&1 || true
    sleep "$interval" &
    pause=$!
    wait "$pause"
  done
}

stop_heartbeat() {
  if [ -n "${beat_pid:-}" ]; then
    kill -USR1 "$beat_pid" 2>/dev/null || true
    wait "$beat_pid" 2>/dev/null || true
    beat_pid=""
  fi
}

on_exit() {  # every way out the shell sees: the record's exit, then the comparison with today's files
  local status=$?
  stop_heartbeat
  if [ -n "${steps:-}" ] && [ -d "${runs:-}" ]; then
    local -a extra=()
    if [ -n "$exit_reason" ]; then extra+=(--reason "$exit_reason" --code "$exit_code"); fi
    if [ -n "$signal_seen" ]; then extra+=(--signal "$signal_seen"); fi
    python3 "$steps" wrapper-exit "$worktree" "$run" "$status" "${extra[@]}" >> "$runs/stderr-$run.log" 2>&1 || true
  fi
}

main() {
# The body keeps its original indentation (none): only the frame around it is new.
topic=$1 worktree=$2 run=$3 mode=$4 session_id=$5 minutes=$6 usd=$7 agent=$8 prompt=$9
export PATH="$HOME/.local/bin:/usr/local/go/bin:$PATH"
runs="$worktree/.hook-only/runs"
hooks_dir=$(dirname "$(readlink -f "$0")")
steps="$hooks_dir/run-steps.py"
wrapper_pid=$$
exit_reason="" exit_code="" signal_seen="" child="" beat_pid="" stopped=0
trap on_exit EXIT
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
python3 "$steps" started "$worktree" "$run" "$wrapper_pid" >> "$runs/stderr-$run.log" 2>&1 || true
if interval=$(python3 "$hooks_dir/../config/homeconfig.py" get budget run_state.heartbeat_interval_seconds \
    2>> "$runs/stderr-$run.log"); then
  heartbeat_loop &
  beat_pid=$!
fi
# The flow mode through the home loader (default origin; any value but origin/local is an error, in the
# stderr log): a bad value ends the run before it starts.
if ! flow_mode=$(python3 "$hooks_dir/../config/homeconfig.py" env FLOW_INTEGRATION 2>> "$runs/stderr-$run.log"); then
  echo "1 $(date -Is)" > "$runs/exit-$run"
  exit_reason=config-error exit_code=1
  exit 0
fi
if [ "$flow_mode" = "local" ] && [ -n "${WT_MAIN_DIR:-}" ]; then
  git -C "$WT_MAIN_DIR" rev-parse HEAD > "$runs/main-$run" 2>/dev/null || rm -f "$runs/main-$run"
  python3 "$steps" main-head "$worktree" "$run" >> "$runs/stderr-$run.log" 2>&1 || true
fi
stopped=0
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

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
invoke() {  # $1 output file, $2 prompt, $3 "compact" for the wrapper's own /compact (worker-stop.py skips it), $4 the invocation's kind for the run record
  local budget code started
  budget=$(remaining)
  if ! python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= 0.05 else 1)' "$budget"; then
    echo "run-worker: run $run budget used up before: ${2:0:40}" >> "$runs/stderr-$run.log"
    return 99
  fi
  local -a args=(-p "$2" --permission-mode auto --max-budget-usd "$budget" --output-format json)
  if [ "$next_mode" = new ]; then args+=(--session-id "$session_id"); else args+=(--resume "$session_id"); fi
  if [ -n "$agent" ]; then args+=(--agent "$agent"); fi
  started=$(date -Is)
  (
    trap - INT
    export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=$((minutes * 60000)) CLAUDE_WORKER_COMPACT=${3:+1} CLAUDE_WORKER_MINUTES=$minutes
    exec claude "${args[@]}"
  ) > "$1" 2>> "$runs/stderr-$run.log" &
  child=$!
  wait_claude
  code=$claude_code
  child=""
  python3 "$steps" invocation "$worktree" "$run" "$4" "$(basename "$1")" "$started" "$code" "$base" \
    >> "$runs/stderr-$run.log" 2>&1 || true
  next_mode=resume
  if [ -s "$1" ]; then last=$1; fi
  return $code
}

if ! claude auth status --text > "$runs/auth-$run.log" 2>&1; then
  echo "90 $(date -Is)" > "$runs/exit-$run"
  exit_reason=not-logged-in exit_code=90
  exit 0
fi
# A broken home config (homeconfig.py's error is in the stderr log) ends the run before it starts.
if ! max_phases=$(python3 "$hooks_dir/../config/homeconfig.py" get budget worker_phases_max 2>> "$runs/stderr-$run.log"); then
  echo "1 $(date -Is)" > "$runs/exit-$run"
  exit_reason=config-error exit_code=1
  exit 0
fi

result="$runs/result-$run.json"
invoke "$result" "$prompt" "" main
code=$?
k=0
while [ "$stopped" = 0 ] && [ "$code" = 0 ] && [ "$k" -lt "$max_phases" ] \
    && [[ "$(json "$result" first_line)" == PHASE:* ]]; do
  k=$((k + 1))
  mv "$result" "$runs/phase-$run-$k.json"
  last="$runs/phase-$run-$k.json"
  invoke "$runs/compact-$run-$k.json" "$compact_prompt" compact compact || break
  [ "$stopped" = 0 ] || break
  invoke "$result" "$continue_prompt" "" continue
  code=$?
done
# A run that stopped right after a phase turn keeps that turn as its result.
if [ ! -s "$result" ] && [ "$k" -gt 0 ]; then cp "$runs/phase-$run-$k.json" "$result"; fi
if [ "$stopped" = 0 ] && [ "$code" = 0 ] && [[ "$(json "$result" first_line)" == STATUS:* ]]; then
  invoke "$runs/compact-$run-$((k + 1)).json" "$compact_prompt" compact final-compact
fi
python3 "$steps" claude-ended "$worktree" "$run" "$code" "$stopped" >> "$runs/stderr-$run.log" 2>&1 || true
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
if [ "$stopped" = 0 ] && [ "$code" = 0 ] && [ "$flow_mode" = "local" ] \
    && [[ "$(json "$result" first_line)" == "STATUS: done"* ]]; then
  python3 "$HOME/.claude/hooks/auto-merge.py" "$worktree" "$topic" "$run" >> "$runs/stderr-$run.log" 2>&1
fi
if [ "$stopped" = 0 ] && [ "$code" = 0 ] && [ -s "$runs/auto-resume-$run.json" ] \
    && [[ "$(json "$result" first_line)" == "STATUS: blocked"* ]]; then
  python3 "$HOME/.claude/hooks/worker-control.py" auto "$worktree" "$topic" "$run" >> "$runs/stderr-$run.log" 2>&1
fi
echo "$code $(date -Is)" > "$runs/exit-$run"
if [ "$stopped" = 1 ]; then exit_reason=stopped; else exit_reason=completed; fi
exit_code=$code
# The record's exit in the same moment (run-steps ended): the queue tick below and auto-finish read status().
python3 "$steps" ended "$worktree" "$run" --reason "$exit_reason" --code "$exit_code" >> "$runs/stderr-$run.log" 2>&1 || true
# The topic queue (wt_queue.py, local mode): with this run's exit written (file and record) its topic no longer
# counts as busy (an automatic resume or merge run it decided still does), and a merge auto-merge.py just made
# may free a dependent: start whatever may start now.
if [ -n "${WT_MAIN_DIR:-}" ]; then
  python3 "$HOME/.claude/hooks/wt_queue.py" tick "$WT_MAIN_DIR" >> "$runs/stderr-$run.log" 2>&1 || true
fi
}

main "$@"; exit $?
"""

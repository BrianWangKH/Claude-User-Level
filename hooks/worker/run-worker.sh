#!/usr/bin/env bash
# One headless worker run, started by worker-control.py in its own process group.
# Usage: run-worker.sh <topic> <worktree> <run> <new|resume> <session-id> <minutes> <usd> <agent|""> <prompt>
# Every decision about the run is hooks/worker/run-steps.py's (migration step 6 of the run record, hooks/lib/runstate.py); this
# script starts claude as told, keeps the heartbeat and traps its own exit:
# - run-steps.py next-invocation names each claude call (the first prompt; after a turn whose first line is
#   `PHASE: <name>` a /compact and a continue prompt, at most worker_phases_max times, home.json budget read
#   through ../../config/homeconfig.py relative to this script; after the run's real end, a STATUS line, one final
#   /compact so the next `wt resume` re-writes only the summary; never after `wt stop` or an error) with what is
#   left of <usd> for it, until it says done. Files per run in .hook-only/runs/: result-<run>.json (the last turn,
#   with the STATUS line), phase-<run>-<k>.json (turns that ended with PHASE), compact-<run>-<k>.json (compactions).
# - run-steps.py wrapup then runs the wrap-up steps, each recorded in the run record and safe to run again: verify.py
#   and verifier.py (real-data checks), auto-merge.py (local mode: merge into the local main when every recorded
#   condition holds), the next run (worker-control.py auto, auto-merge or auto-fix), auto-finish.py after a merge,
#   then exit-<run> ("<code> <time>") with the record's exit, then the queue tick (wt_queue.py).
# - Early ends write exit-<run> here: 90 = not logged in, 1 = a home config error (before claude starts).
# CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS = the run's minutes: claude -p otherwise stops background work
# after 10 minutes of idle waiting (headless docs, "Background tasks at exit"), and a background
# code-reviewer held at hand-back by the mutation scan (~10 min) was killed with its report
# (seen in a real run). The run's own limits (worker-budget, --max-budget-usd) apply.
# That ceiling bounds every idle wait, so the wait after the run's final turn (its STATUS line) has its own
# bound: each worker call (not the wrapper's /compact) gets a watcher, `run-steps.py watch`, which ends claude
# once worker-stop.py's record of its STATUS turn is ending_idle_max_seconds old (budget run_state; a pause or a
# PHASE turn never counts); run-steps.py invocation then keeps that turn as the call's result
# (hooks/worker/run_ending.py).
# The run record (hooks/lib/runstate.py): the wrapper hands each fact to run-steps.py (its start, the main head, every
# claude invocation, claude's end); a background loop writes the heartbeat every heartbeat_interval_seconds
# (budget run_state) while the wrapper lives, and every reconcile_heartbeat_beats_count beats starts the reconciler
# for the repository's other worktrees (run-steps.py reconcile --all --exclude <this worktree>, in its own session
# with default signal handling, its output in the run's stderr log only: nothing it does reaches the beats or the
# worker; reconcile_others); the EXIT trap stops that loop, writes the wrapper's own exit
# (and the run's, on an early way out the shell sees), then compares the record's story with today's files
# (status-diverged, once per difference). A record write that fails is reported once and never changes the run.
# A wrap-up that crashes ends the run wrapup-failed (run-steps.py); one killed before it could write that leaves the
# run's exit unwritten: the run then reads as an orphan (its wrapper exited before the wrap-up ended). Either way
# `wt wrapup` (run-steps.py wrapup <worktree> <run>) continues it.
# claude and the wrap-up run as `( trap - INT; exec ... ) &` and the wrapper waits for them, so an INT or TERM reaches
# the wrapper's trap at once: the trap forwards the signal, and the wrapper keeps waiting until the child has really
# exited and takes its exit code (a background child would otherwise ignore INT, and the wrapper would go on while
# claude still runs). A stop during the wrap-up is a stop of the run (run-steps.py wrapup).
# The INT/TERM and EXIT traps are the first thing main does, before run-steps.py started records process.started_at:
# `wt stop` signals a wrapper only once that is recorded (worker-control.py stopRun), and a stop it recorded before
# reaches the wrapper as `started`'s answer instead. A stop at any moment of the start-up (prepare) skips what is
# left of it, and a start-up step the stop's signal ended is never read as not logged in or a config error: the run
# goes to next-invocation, which starts no claude after a stop, and ends stopped through the wrap-up.
# The whole body is the function main, called on the last line, so overwriting this file while a wrapper runs
# (applying a home change) can't feed the running shell new lines: bash has read all of it before main runs.
set -u

on_signal() {  # $1 INT or TERM: the run was stopped; the child gets the signal too (also when only this pid got it)
  stopped=1
  signal_seen=$1
  if [ -n "${child:-}" ]; then kill -s "$1" "$child" 2>/dev/null || true; fi
}

wait_child() {  # wait for $child until it has really exited; its exit code in child_code
  # A trapped signal ends `wait` early (> 128) while the child may still run: wait until it is gone.
  wait "$child"
  while kill -0 "$child" 2>/dev/null; do wait "$child"; done
  # bash keeps an exited child's status, so this returns the child's own code, never the interrupted wait's.
  wait "$child"
  child_code=$?
}

reconcile_others() {  # detached: its own session (wt stop's signal never reaches it), signals back to default
  ( trap - INT TERM USR1; exec setsid python3 "$steps" reconcile --all --exclude "$worktree" ) \
    < /dev/null >> "$runs/stderr-$run.log" 2>&1 &
}

heartbeat_loop() {  # background: one beat every $interval seconds while the wrapper ($wrapper_pid) lives
  local pause="" beats=0
  trap '' INT TERM  # a stop signal ends the run, not the beats: the wrapper still wraps up
  trap 'if [ -n "$pause" ]; then kill -KILL "$pause" 2>/dev/null; fi; exit 0' USR1
  while kill -0 "$wrapper_pid" 2>/dev/null; do
    python3 "$steps" beat "$worktree" "$run" >> "$runs/stderr-$run.log" 2>&1 || true
    beats=$((beats + 1))
    if [ "${reconcile_every:-0}" -gt 0 ] && [ $((beats % reconcile_every)) -eq 0 ]; then reconcile_others; fi
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

invoke() {  # one claude call as run-steps.py named it: $1 output file, $2 "1" for the wrapper's own /compact
  # (worker-stop.py skips it), $3 the session's cost before this run, $4... claude's arguments
  local out=$1 compact=$2 base=$3 started watcher=""
  shift 3
  started=$(date -Is)
  (
    trap - INT
    export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=$((minutes * 60000)) CLAUDE_WORKER_COMPACT=$compact CLAUDE_WORKER_MINUTES=$minutes
    exec claude "$@"
  ) > "$out" 2>> "$runs/stderr-$run.log" &
  child=$!
  if [ -z "$compact" ]; then
    # claude's identity for the watcher: its start time (/proc stat field 22), read before the wrapper waits, so the
    # pid is still this child's (a zombie keeps it); unreadable = empty, and the watcher then never signals.
    local stat="" ticks=""
    local -a fields=()
    if read -r stat < "/proc/$child/stat" 2>/dev/null; then
      read -r -a fields <<< "${stat##*) }"
      ticks=${fields[19]:-}
    fi
    python3 "$steps" watch "$worktree" "$run" "$child" "$ticks" "$(basename "$out")" "$started" >> "$runs/stderr-$run.log" 2>&1 &
    watcher=$!
  fi
  wait_child
  if [ -n "$watcher" ]; then  # the cut record, if any, was written before claude got its signal
    kill "$watcher" 2>/dev/null || true
    wait "$watcher" 2>/dev/null || true
  fi
  claude_code=$child_code
  child=""
  python3 "$steps" invocation "$worktree" "$run" "$after" "$(basename "$out")" "$started" "$claude_code" "$base" \
    >> "$runs/stderr-$run.log" 2>&1 || true
}

early_end() {  # $1 code, $2 reason: an end before claude starts; none after a stop (its signal may have ended the step)
  [ "$stopped" = 0 ] || return 1
  echo "$1 $(date -Is)" > "$runs/exit-$run"
  exit_reason=$2 exit_code=$1
}

prepare() {  # the start-up before claude's first call; 1 after an early end, 0 otherwise (also after a stop)
  local said=""
  cd "$worktree" || exit 1
  date -Is > "$runs/start-$run"
  said=$(python3 "$steps" started "$worktree" "$run" "$wrapper_pid" 2>> "$runs/stderr-$run.log") || true
  if [ "$said" = stopped ]; then stopped=1; fi  # wt stop came before the start was recorded: it sent no signal
  [ "$stopped" = 0 ] || return 0
  # A count that can't be read (the error is in the stderr log) only turns the heartbeat's reconcile off.
  reconcile_every=$(python3 "$hooks_dir/../config/homeconfig.py" get budget run_state.reconcile_heartbeat_beats_count \
    2>> "$runs/stderr-$run.log") || reconcile_every=0
  case "$reconcile_every" in ''|*[!0-9]*) reconcile_every=0 ;; esac
  if interval=$(python3 "$hooks_dir/../config/homeconfig.py" get budget run_state.heartbeat_interval_seconds \
      2>> "$runs/stderr-$run.log"); then
    heartbeat_loop &
    beat_pid=$!
  fi
  [ "$stopped" = 0 ] || return 0
  # The topic's flow mode (homeconfig.py flow-mode: its record, written at the first launch, else the main
  # checkout's); every hook of this run reads the same itself, whatever `wt local` says now. One that can't be
  # read (an invalid record; the error is in the stderr log) ends the run before it starts.
  if ! flow_mode=$(python3 "$hooks_dir/../config/homeconfig.py" flow-mode "$worktree" 2>> "$runs/stderr-$run.log"); then
    early_end 1 config-error && return 1
    return 0
  fi
  if [ "$flow_mode" = "local" ] && [ -n "${WT_MAIN_DIR:-}" ]; then
    git -C "$WT_MAIN_DIR" rev-parse HEAD > "$runs/main-$run" 2>/dev/null || rm -f "$runs/main-$run"
    python3 "$steps" main-head "$worktree" "$run" >> "$runs/stderr-$run.log" 2>&1 || true
  fi
  [ "$stopped" = 0 ] || return 0
  if ! claude auth status --text > "$runs/auth-$run.log" 2>&1; then
    early_end 90 not-logged-in && return 1
    return 0
  fi
  [ "$stopped" = 0 ] || return 0
  # A broken home config (homeconfig.py's error is in the stderr log) ends the run before it starts. A read the stop
  # ended keeps max_phases at 0: next-invocation starts nothing after a stop anyway.
  local phases=""
  if ! phases=$(python3 "$hooks_dir/../config/homeconfig.py" get budget worker_phases_max 2>> "$runs/stderr-$run.log"); then
    early_end 1 config-error && return 1
    return 0
  fi
  max_phases=$phases
  return 0
}

main() {
topic=$1 worktree=$2 run=$3 mode=$4 session_id=$5 minutes=$6 usd=$7 agent=$8 prompt=$9
exit_reason="" exit_code="" signal_seen="" child="" beat_pid="" stopped=0 max_phases=0
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap on_exit EXIT
export PATH="$HOME/.local/bin:/usr/local/go/bin:$PATH"
runs="$worktree/.hook-only/runs"
hooks_dir=$(dirname "$(dirname "$(readlink -f "$0")")")
steps="$hooks_dir/worker/run-steps.py"
wrapper_pid=$$
prepare || exit 0

# The claude calls, one at a time as run-steps.py names them: `invoke <run code> <kind> <file> <compact> <base>
# <args...>` or `done <run code>`; it decides from the files and the last call's kind and exit code.
code=0 after="" last=0
while :; do
  if ! out=$(python3 "$steps" next-invocation "$worktree" "$run" --topic "$topic" --mode "$mode" --session "$session_id" \
      --usd "$usd" --agent "$agent" --prompt "$prompt" --max-phases "$max_phases" --after "$after" --code "$code" \
      --last "$last" --stopped "$stopped" 2>> "$runs/stderr-$run.log"); then
    # A decision that can't be made is an error of the run (exit 1), never a quiet success.
    echo "run-worker: run $run: run-steps.py next-invocation failed; no further claude call, exit 1" >> "$runs/stderr-$run.log"
    code=1
    break
  fi
  eval "spec=($out)"
  code=${spec[1]}
  [ "${spec[0]}" = invoke ] || break
  [ "$stopped" = 0 ] || continue  # a stop came while it decided: ask again, now as stopped
  after=${spec[2]}
  invoke "$runs/${spec[3]}" "${spec[4]}" "${spec[5]}" "${spec[@]:6}"
  last=$claude_code
done
python3 "$steps" claude-ended "$worktree" "$run" "$code" "$stopped" --topic "$topic" >> "$runs/stderr-$run.log" 2>&1 || true
# The wrap-up (run-steps.py: verify, verifier, merge, next run, finish, exit-<run>, queue tick), waited for as claude
# is, so a stop reaches it.
(
  trap - INT
  exec python3 "$steps" wrapup "$worktree" "$run" --topic "$topic" --code "$code" --stopped "$stopped"
) >> "$runs/stderr-$run.log" 2>&1 &
child=$!
wait_child
child=""
if [ "$child_code" = 0 ]; then
  if [ "$stopped" = 1 ]; then exit_reason=stopped; else exit_reason=completed; fi
  exit_code=$code
fi
}

main "$@"; exit $?

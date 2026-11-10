// What the pane draws, rebuilt from the workers' state files on every poll.

export type AgentRow = {
  id: string
  type: string
  // The round's start as the budget hook counts it: pushed forward by each hook wait already over.
  startedMs: number
  // A hook holding the agent now (budget_state.markWait): since when, what, and the wait's limit.
  waitSinceMs?: number
  waitWhat?: string
  waitMaxMinutes?: number
  budgetMinutes?: number
  budgetUsd?: number
  usdRun?: number
  // This round's spend, as the budget hook counts it: spend now less spend when the round began.
  usdRound?: number
  tool?: string
  what?: string
  lastMs?: number
}

export type WorkerRow = {
  topic: string
  run: number
  startedMs: number
  estMinutes?: number // the run's estimate (est-<n>.json minutes, usd)
  estUsd?: number
  capMinutes?: number // the run's limits, budget_headroom_factor above the estimate (cap_minutes, cap)
  capUsd?: number
  compacts: number
  exitCode?: number
  exitMs?: number
  resultUsd?: number
  usdRun: number
  unpriced: number
  // Finished subagent rounds of every run of the topic (from its first run's start), in order;
  // consecutive rounds of one type merged.
  done: { type: string; count: number }[]
  agents: AgentRow[]
  orchestrator?: { tool: string; what: string; lastMs: number }
  // The run's phase from wt_controls.py (hooks/lib/runstate.py, model.withPhases): the pane counts and draws a
  // worker as running only in launching, running or finishing; undefined until the controls were read.
  phase?: Phase
}

// What a pane button or auto mode can send for a topic: always the prompt `wt <action> <topic>`. wrapup (a run
// whose wrapper ended before its wrap-up) is a button only: wt_controls.py never lets auto send it.
export type Action = 'launch' | 'resume' | 'stop' | 'wrapup'

// A run's phase as hooks/lib/runstate.py derives it from the facts (wt_controls.py "phase"); orphan: the run may have
// died (suspected) or did (confirmed), which the line says; unknown: its state can't be read.
export type Phase = 'none' | 'launching' | 'running' | 'finishing' | 'ended' | 'orphan' | 'unknown'

// What the person can do about the run (runstate.human's todo): text to show, the `wt` command it means.
export type RunTodo = { text?: string; command?: string }

export type Control = {
  topic: string
  action?: Action // absent: nothing to send (why says what is missing)
  why?: string
  run?: number
  isHeld: boolean // an agent-guard ALERT or HOLD the person has not decided (wt_controls.py "held")
  auto?: { ok: boolean; why?: string } // whether auto mode may submit the action (wt_controls.py)
  merge?: { reason: string; files: string[] } // an automatic merge refused for the person to approve
  approving?: { run: number; since: string } // a `wt merge` of that run in progress (since HH:MM): no merge button
  // The run's state (hooks/lib/runstate.py through wt_controls.py, which decides the row's action from it too):
  phase?: Phase // the run's phase (wt_controls.py "phase"); the pane counts running workers by it
  human?: { line: string; todo?: RunTodo } // the person's line and to-do (runstate.human, in status_language)
  statusError?: string // why the run state couldn't be read (wt_controls.py "status_error")
}

// One item of the topic queue (wt_queue.py status, through wt_controls.py): waiting (why), started
// (its run), merged, or an error that stopped it.
export type QueueRow = {
  topic: string
  state?: 'waiting' | 'started' | 'merged'
  why?: string
  run?: number
  error?: string
}

// A topic the planning session wrote to plans/queue/ and nobody queued yet (wt_queue.py planned):
// the pane's `queue` button sends `wt queue <topic>`. A spec that doesn't check out comes with its error.
export type PlannedRow = {
  topic: string
  order?: number
  after?: string[]
  estUsd?: number
  error?: string
}

// Where an effective queue limit comes from (wt_queue.limits): home.json, the person's local.json, the repo's
// .claude/flow.json when it is lower than the person's, or home.json because local.json's value is invalid.
export type LimitSource = 'home' | 'local' | 'repo' | 'home (local invalid)'

// The queue's effective limits (wt_controls.py "queue_limits"): busy workers and summed estimates in USD.
export type QueueLimits = { workers?: number; usd?: number; source: { workers?: LimitSource; usd?: LimitSource } }

// A pressed Start/Stop button waiting for its second press, until untilMs; topic '' for none.
export type Armed = { topic: string; action: string; untilMs: number }

// A button whose `wt` prompt was sent and whose outcome hasn't shown yet: the topic's buttons read busy and
// ignore presses until an event for the topic, a change of its row (mark), or untilMs. topic '' is `queue all`.
// mark: the row's markOf at the press; absent when the controls were unreadable then (set at the next readable poll).
export type Pending = { topic: string; action: string; sinceMs: number; untilMs: number; mark?: string }

// One event (or a refused press) kept for the Notices list; needs: the person must act.
export type Notice = { t: string; kind: string; topic: string; text: string; needs: boolean }

// The Notices list: newest first; unread counts what arrived while it was closed.
export type Notices = { items: Notice[]; unread: number; open: boolean }

// The flow mode wt_controls.py reports (homeconfig.flowMode).
export type FlowMode = 'origin' | 'local'

// The home's config mode wt_controls.py reports (homeconfig.configMode: config/mode, no file = DEV).
export type ConfigMode = 'DEV' | 'TEST'

export type View = {
  workers: WorkerRow[]
  controls: Control[]
  controlsError?: string // wt_controls.py failed or printed another schema: shown above the controls
  flowMode?: FlowMode // the local switch's state; undefined until wt_controls.py answered
  flowModeError?: string // wt_controls.py couldn't read the flow mode: shown instead of the switch state
  configMode?: ConfigMode // one status word in the Controls line; undefined until wt_controls.py answered
  configModeError?: string // wt_controls.py couldn't load the config mode's settings: shown in red
  queue?: QueueRow[]
  planned?: PlannedRow[]
  queueLimits?: QueueLimits // shown under the queue with each limit's source
  error?: string
  nowMs: number
  tick: number
}

declare module 'claude-code' {
  interface PluginState {
    'wt-pane': {
      view: Shaped<View>
      tab: Shaped<string>
      auto: Shaped<boolean>
      armed: Shaped<Armed>
      pending: Shaped<Pending[]>
      notices: Shaped<Notices>
    }
  }
}

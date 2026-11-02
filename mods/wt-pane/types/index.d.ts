// What the pane draws, rebuilt from the workers' state files on every poll.

export type AgentRow = {
  id: string
  type: string
  // The round's start as the budget hook counts it: pushed forward by each hook wait already over.
  startedMs: number
  // A hook holding the agent now (qg_common.markWait): since when, what, and the wait's limit.
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
  capMinutes?: number // the run's limits, budget_headroom above the estimate (cap_minutes, cap)
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
}

// What a pane button or auto mode can send for a topic: always the prompt `wt <action> <topic>`.
export type Action = 'launch' | 'resume' | 'stop'

export type Control = {
  topic: string
  action?: Action // absent: nothing to send (why says what is missing)
  why?: string
  run?: number
  isHeld: boolean // <worktree>/.hook-only/agent-guard/HOLD: an ALERT the person has not decided
  auto?: { ok: boolean; why?: string } // whether auto mode may submit the action (wt_controls.py)
  merge?: { reason: string; files: string[] } // an automatic merge refused for the person to approve
  approving?: { run: number; since: string } // a `wt merge` of that run in progress (since HH:MM): no merge button
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

export type View = {
  workers: WorkerRow[]
  controls: Control[]
  controlsError?: string // wt_controls.py failed or printed another schema: shown above the controls
  queue?: QueueRow[]
  planned?: PlannedRow[]
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

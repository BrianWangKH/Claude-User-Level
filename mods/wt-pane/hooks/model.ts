// Builds the pane's rows from the files hooks write; no drawing and no `$` here, so tests feed it
// plain file maps. File layout (see ~/.claude/README.md "東西在哪"):
//   <main>/.hook-only/workers/<topic>.json                    wt: run, worktree, started
//   <worktree>/.hook-only/runs/start-<n> exit-<n> est-<n>.json result-<n>.json compact-<n>-<k>.json
//   <worktree>/.hook-only/budget/events.jsonl, <agent_id>.json  subagent-budget.py
//   <worktree>/.hook-only/hook-read-only/progress/<key>.json    worker-progress.py

import type { AgentRow, WorkerRow } from '../types'
import type { Desk, Person, Pose, Seated } from './office'
import { hex } from './sprite'

export type Files = {
  list: (dir: string) => Promise<string[]>
  read: (path: string) => Promise<string | undefined>
}

type Progress = {
  t: number
  agent_id: string
  agent_type: string
  tool: string
  what: string
  run: number
  usd: number
  usd_run: number
  unpriced: number
}

function parse<T>(text: string | undefined): T | undefined {
  if (text === undefined) return undefined
  try {
    return JSON.parse(text) as T
  } catch {
    return undefined
  }
}

// `date -Is` ("2026-10-30T18:13:34+08:00"); events.jsonl's local "2026-10-30T18:13:34" parses as local time.
function timeMs(text: string | undefined): number | undefined {
  const ms = text === undefined ? NaN : Date.parse(text.trim())
  return Number.isNaN(ms) ? undefined : ms
}

type Round = { id: string; type: string; startedMs: number; endedMs?: number }

// Subagent rounds from events.jsonl that began at or after `sinceMs`. A start logs two lines (the
// event and the claimed budget), so the first one opens the round; a start after a stop (a resume)
// opens a new one. So does a start after a run boundary (`boundariesMs`, later runs' starts): the
// round a stopped run left open never ended and is not this one.
export function rounds(eventsText: string | undefined, sinceMs: number, boundariesMs: number[] = []): Round[] {
  const open = new Map<string, Round>()
  const all: Round[] = []
  for (const line of (eventsText ?? '').split('\n')) {
    const event = parse<{ t: string; event: string; agent_id?: string; agent_type?: string }>(line)
    const at = timeMs(event?.t)
    if (!event || !event.agent_id || at === undefined || at < sinceMs) continue
    const stale = open.get(event.agent_id)
    if (stale && boundariesMs.some(boundary => stale.startedMs < boundary && boundary <= at)) open.delete(event.agent_id)
    if (event.event === 'SubagentStart' && !open.has(event.agent_id)) {
      const round = { id: event.agent_id, type: event.agent_type ?? '', startedMs: at }
      open.set(event.agent_id, round)
      all.push(round)
    } else if (event.event === 'SubagentStop') {
      const round = open.get(event.agent_id)
      if (round) {
        round.endedMs = at
        open.delete(event.agent_id)
      }
    }
  }
  return all
}

// The start times of the topic's runs from their start-<n> files, first run first; unreadable ones skipped.
async function runStartsMs(files: Files, runs: string, runFiles: string[]): Promise<number[]> {
  const numbers = runFiles
    .map(name => /^start-(\d+)$/.exec(name))
    .filter((match): match is RegExpExecArray => match !== null)
    .map(match => Number(match[1]))
    .sort((a, b) => a - b)
  const out: number[] = []
  for (const n of numbers) {
    const ms = timeMs(await files.read(`${runs}/start-${n}`))
    if (ms !== undefined) out.push(ms)
  }
  return out
}

function merged(types: string[]): { type: string; count: number }[] {
  const out: { type: string; count: number }[] = []
  for (const type of types) {
    const last = out[out.length - 1]
    if (last && last.type === type) last.count += 1
    else out.push({ type, count: 1 })
  }
  return out
}

export async function worker(
  files: Files,
  topic: string,
  stateText: string | undefined,
  nowMs: number,
  lingerMs: number,
): Promise<WorkerRow | undefined> {
  const state = parse<{ run?: number; worktree?: string; started?: number }>(stateText)
  if (!state?.worktree) return undefined
  const run = state.run ?? 1
  const hookOnly = `${state.worktree}/.hook-only`
  const runs = `${hookOnly}/runs`
  const runFiles = await files.list(runs)
  if (runFiles.length === 0) return undefined // worktree removed, or never started

  const exitText = runFiles.includes(`exit-${run}`) ? await files.read(`${runs}/exit-${run}`) : undefined
  const [codeText, exitAt] = (exitText ?? '').trim().split(' ')
  const exitMs = timeMs(exitAt)
  if (exitMs !== undefined && nowMs - exitMs > lingerMs) return undefined

  const startedMs =
    timeMs(await files.read(`${runs}/start-${run}`)) ?? (state.started ?? 0) * 1000
  // est-<n>.json (worker-control.py): the estimate (minutes, usd) and the run's limits, budget_headroom
  // above it (cap_minutes, cap). Files from before the headroom change have no cap_minutes.
  const est = parse<{ minutes?: number; usd?: number | null; cap?: number; cap_minutes?: number }>(
    await files.read(`${runs}/est-${run}.json`),
  )
  const result = exitMs === undefined
    ? undefined
    : parse<{ total_cost_usd?: number }>(await files.read(`${runs}/result-${run}.json`))

  const progressDir = `${hookOnly}/hook-read-only/progress`
  const progress = new Map<string, Progress>()
  for (const name of await files.list(progressDir)) {
    if (name.startsWith('.') || !name.endsWith('.json')) continue
    const one = parse<Progress>(await files.read(`${progressDir}/${name}`))
    if (one) progress.set(name.slice(0, -5), one)
  }
  const thisRun = [...progress.values()].filter(one => one.run === run)

  const eventsText = await files.read(`${hookOnly}/budget/events.jsonl`)
  // Running subagents are this run's: a round left open by an earlier run that stopped is not at work.
  const thisRunRounds = rounds(eventsText, startedMs - 1000)
  // Finished rounds count over every run of the topic, from its first run's start, so a resumed
  // topic keeps the desks done in earlier runs.
  const starts = await runStartsMs(files, runs, runFiles)
  const topicStartMs = Math.min(startedMs, starts[0] ?? startedMs)
  const finished = rounds(eventsText, topicStartMs - 1000, starts.slice(1).map(ms => ms - 1000))
    .filter(one => one.endedMs !== undefined)
  const typeOf = (round: Round) => round.type || progress.get(round.id)?.agent_type || round.id
  const agents: AgentRow[] = []
  for (const round of thisRunRounds.filter(one => one.endedMs === undefined)) {
    const budget = parse<{
      minutes?: number
      usd?: number | null
      usd_at_start?: number
      started?: number
      hook_wait?: { since?: number; what?: string; max_seconds?: number }
    }>(await files.read(`${hookOnly}/budget/${round.id}.json`))
    const last = progress.get(round.id)
    const wait = budget?.hook_wait
    agents.push({
      id: round.id,
      type: typeOf(round),
      // subagent-budget.py's started moves forward by every finished hook wait (qg_common.excludeWait).
      startedMs: budget?.started ? budget.started * 1000 : round.startedMs,
      waitSinceMs: wait?.since ? wait.since * 1000 : undefined,
      waitWhat: wait?.since ? wait.what : undefined,
      waitMaxMinutes: wait?.since && wait.max_seconds ? wait.max_seconds / 60 : undefined,
      budgetMinutes: budget?.minutes,
      budgetUsd: budget?.usd ?? undefined,
      usdRun: last?.run === run ? last.usd_run : undefined,
      // A resumed agent keeps its id: what it spent in earlier rounds is not this round's.
      usdRound: last && budget?.usd_at_start !== undefined ? last.usd - budget.usd_at_start : undefined,
      tool: last?.tool,
      what: last?.what,
      lastMs: last ? last.t * 1000 : undefined,
    })
  }
  const main = progress.get('main')

  return {
    topic,
    run,
    startedMs,
    estMinutes: est?.minutes,
    estUsd: est?.usd ?? undefined,
    capMinutes: est?.cap_minutes,
    capUsd: est?.cap,
    compacts: runFiles.filter(name => name.startsWith(`compact-${run}-`)).length,
    exitCode: codeText ? Number(codeText) : undefined,
    exitMs,
    resultUsd: result?.total_cost_usd,
    usdRun: thisRun.reduce((sum, one) => sum + (one.usd_run || 0), 0),
    unpriced: thisRun.reduce((sum, one) => sum + (one.unpriced || 0), 0),
    done: merged(finished.map(typeOf)),
    agents,
    orchestrator: main && main.run === run ? { tool: main.tool, what: main.what, lastMs: main.t * 1000 } : undefined,
  }
}

// The agent's own time this round: a hook wait under way is the hook's, not the agent's, as the
// budget hook counts it once the wait is over.
export function workedMs(agent: AgentRow, nowMs: number): number {
  return Math.max(0, Math.min(nowMs, agent.waitSinceMs ?? nowMs) - agent.startedMs)
}

// Every worker wt knows of in this repo, running ones first.
export async function snapshot(files: Files, mainRoot: string, nowMs: number, lingerMs: number): Promise<WorkerRow[]> {
  const dir = `${mainRoot}/.hook-only/workers`
  const rows: WorkerRow[] = []
  for (const name of await files.list(dir)) {
    if (!name.endsWith('.json')) continue
    const row = await worker(files, name.slice(0, -5), await files.read(`${dir}/${name}`), nowMs, lingerMs)
    if (row) rows.push(row)
  }
  return rows.sort((a, b) =>
    Number(a.exitMs !== undefined) - Number(b.exitMs !== undefined) || a.startedMs - b.startedMs)
}

// The share of the estimate used, time or spend, whichever is more: past 1 the run is in the
// headroom (budget_headroom) before its limits. Older files with only a cap: spend against the cap.
export function used(row: WorkerRow, nowMs: number): number | undefined {
  const parts: number[] = []
  if (row.estMinutes) parts.push((nowMs - row.startedMs) / 60000 / row.estMinutes)
  const usdBase = row.estUsd ?? row.capUsd
  if (usdBase) parts.push(row.usdRun / usdBase)
  return parts.length ? Math.max(...parts) : undefined
}

export function bar(ratio: number, width: number): string {
  const filled = Math.max(0, Math.min(width, Math.round(ratio * width)))
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

export function minutes(ms: number): string {
  return `${Math.max(0, Math.floor(ms / 60000))}m`
}

export function ago(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return seconds < 60 ? `${seconds}s ago` : `${Math.floor(seconds / 60)}m ago`
}

export function usd(value: number): string {
  return `$${value.toFixed(2)}`
}

export function exitLabel(code: number): string {
  if (code === 0) return 'exit 0'
  if (code === 90) return 'exit 90 (not logged in)'
  return `exit ${code}`
}

export type TrackOptions = {
  stations: string[] // agent types in flow order: one desk each
  colors: Record<string, string>
  labels: Record<string, string>
  verbs: Record<string, string> // tool -> what a bubble says
  idleVerb: string
  waitVerb: string // the orchestrator's while a subagent works
  holdMs: number
  // A figure warns (its bubble flashes) at this share of its budget, the estimate: 1 = past the
  // estimate, into the headroom the budget hooks allow before they stop it.
  warnRatio: number
}

export type Boss = Person & { desk: number }

// The office of a running worker. A desk is done once a round of its agent type ended in any run
// of the topic (`row.done` counts them from the first run's start), active while one runs. Each
// running subagent sits at its desk, typing while its last tool call is under `holdMs` old; the orchestrator heads for the desk it deals
// with (the active one, else the next pending one). A bubble says the last tool call's verb for
// `holdMs`, then the idle verb. At `warnRatio` of its budget (time or USD) a figure's bubble flashes.
export function track(
  row: WorkerRow,
  nowMs: number,
  options: TrackOptions,
): { desks: Desk[]; seated: (Seated & { key: string })[]; boss: Boss } {
  const colorOf = (type: string) => hex(options.colors[type] ?? options.colors.other, 0x9aa0a6)
  const running = new Set(row.agents.map(agent => agent.type))
  const rounds = new Map<string, number>()
  for (const step of row.done) rounds.set(step.type, (rounds.get(step.type) ?? 0) + step.count)
  // The topic's rounds in the order they began: the finished ones (all runs), then the running ones.
  const order = [...row.done.flatMap(step => Array<string>(step.count).fill(step.type)), ...row.agents.map(agent => agent.type)]
  const place = (type: string) => options.stations.indexOf(type)
  // Sent back: after a desk's last finished round the flow went to a desk before it (a reviewer's
  // findings sent the work back), and has not come past it since. It is done again once a round of
  // its own follows, or once the flow reaches a desk after it: rev sends work to qa, qa goes straight
  // back to rev, and dev, skipped, is done again as rev starts.
  const returned = (type: string) => {
    const last = row.done.flatMap(step => Array<string>(step.count).fill(step.type)).lastIndexOf(type)
    if (last === -1) return false
    let isBack = false
    for (const next of order.slice(last + 1)) {
      if (place(next) === -1) continue
      if (place(next) < place(type)) isBack = true
      else if (place(next) > place(type)) isBack = false
    }
    return isBack
  }
  const desks: Desk[] = options.stations.map(type => ({
    label: options.labels[type] ?? type,
    color: colorOf(type),
    state: running.has(type) ? 'active' : !rounds.has(type) ? 'pending' : returned(type) ? 'returned' : 'done',
    rounds: rounds.get(type) ?? 0,
  }))
  const active = desks.findIndex(desk => desk.state === 'active')
  const pending = desks.findIndex(desk => desk.state === 'pending' || desk.state === 'returned')
  const next = pending === -1 ? Math.max(0, desks.length - 1) : pending

  const say = (tool: string | undefined, lastMs: number | undefined) =>
    tool && lastMs !== undefined && nowMs - lastMs <= options.holdMs ? (options.verbs[tool] ?? options.idleVerb) : options.idleVerb

  const seated = row.agents
    .map(agent => {
      const shares: number[] = []
      if (agent.budgetMinutes) shares.push(workedMs(agent, nowMs) / 60000 / agent.budgetMinutes)
      const spent = agent.usdRound ?? agent.usdRun
      if (agent.budgetUsd && spent !== undefined) shares.push(spent / agent.budgetUsd)
      const verb = say(agent.tool, agent.lastMs)
      return {
        key: agent.id,
        desk: options.stations.indexOf(agent.type),
        color: colorOf(agent.type),
        pose: (verb === options.idleVerb ? 'idle' : 'type') as Pose,
        say: verb,
        isWarned: shares.length > 0 && Math.max(...shares) >= options.warnRatio,
      }
    })
    // An agent type with no desk works off the floor plan: the orchestrator's caption names it.
    .filter(person => person.desk !== -1)

  const offPlan = row.agents.filter(agent => !options.stations.includes(agent.type)).map(agent => agent.type)
  const doing = row.agents.length
    ? offPlan.length
      ? `${options.waitVerb} ${offPlan.join(', ')}`
      : options.waitVerb
    : say(row.orchestrator?.tool, row.orchestrator?.lastMs)
  const boss: Boss = {
    desk: active === -1 ? next : active,
    color: colorOf('orchestrator'),
    pose: 'idle',
    // Named, so the figure in the aisle reads as the orchestrator.
    say: `${options.labels.orchestrator ?? 'orch'}: ${doing}`,
    isWarned: (used(row, nowMs) ?? 0) >= options.warnRatio,
  }
  return { desks, seated, boss }
}

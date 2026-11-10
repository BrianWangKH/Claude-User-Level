// What a person (pane buttons) or auto mode may start or stop for each topic. The decisions are not
// made here: ~/.claude/hooks/worker/wt_controls.py makes them (worker-control.py imports the same functions,
// so a typed `wt resume` and a button agree), and the pane runs it and draws what it prints. This file
// reads that output (schema 1) and words the notes. Every action still ends as the prompt
// `wt launch|resume|stop|wrapup <topic>`, so worker-control.py (UserPromptSubmit) stays the one place that
// starts and stops workers, and typing `wt ...` by hand keeps working unchanged.

import type { Action, ConfigMode, Control, FlowMode, LimitSource, Notice, Notices, Pending, Phase, PlannedRow, QueueLimits, QueueRow, RunTodo, View } from '../types'

// worker-control.py's topic rule.
export const TOPIC = /^[a-z0-9][a-z0-9-]{0,63}$/
// The output version this pane reads; wt_controls.py raises it when a field is removed or renamed.
export const SCHEMA = 1
const ACTIONS = new Set<string>(['launch', 'resume', 'stop', 'wrapup'])
// hooks/lib/runstate.py's phases (wt_controls.py "phase"); another value is shown as no phase.
const PHASES = new Set<string>(['none', 'launching', 'running', 'finishing', 'ended', 'orphan', 'unknown'])

// flowMode: the main checkout's flow mode as wt_controls.py read it (homeconfig.flowMode); flowModeError when
// it couldn't (an invalid flow-mode record, a broken config/registry/env.json): the pane shows that, never a guessed mode.
export type Controls = {
  controls: Control[]
  queue?: QueueRow[]
  planned?: PlannedRow[]
  queueLimits?: QueueLimits
  localBlocker?: string
  flowMode?: FlowMode
  flowModeError?: string
  configMode?: ConfigMode
  configModeError?: string
  error?: string
}

function plannedRowOf(value: unknown): PlannedRow | undefined {
  if (!isObject(value) || typeof value.topic !== 'string' || !TOPIC.test(value.topic)) return undefined
  const after = Array.isArray(value.after) ? value.after.filter((t): t is string => typeof t === 'string' && TOPIC.test(t)) : undefined
  return {
    topic: value.topic,
    order: typeof value.order === 'number' ? value.order : undefined,
    after,
    estUsd: typeof value.est_usd === 'number' ? value.est_usd : undefined,
    error: typeof value.error === 'string' ? value.error : undefined,
  }
}

// The prompt the pane's `queue all` sends: every planned topic without an error, in the order wt_queue.py
// listed them (its "order", then name); undefined when there is none.
export function queueAllText(planned: PlannedRow[]): string | undefined {
  const topics = planned.filter(row => !row.error).map(row => row.topic)
  return topics.length > 0 ? `wt queue ${topics.join(' ')}` : undefined
}

const QUEUE_STATES = new Set<string>(['waiting', 'started', 'merged'])
const LIMIT_SOURCES = new Set<string>(['home', 'local', 'repo', 'home (local invalid)'])

// wt_controls.py "queue_limits" (wt_queue.limits); undefined when absent or not an object.
function queueLimitsOf(value: unknown): QueueLimits | undefined {
  if (!isObject(value)) return undefined
  const given = isObject(value.source) ? value.source : {}
  const sourceOf = (limit: string): LimitSource | undefined =>
    typeof given[limit] === 'string' && LIMIT_SOURCES.has(given[limit] as string) ? (given[limit] as LimitSource) : undefined
  return {
    workers: typeof value.workers === 'number' ? value.workers : undefined,
    usd: typeof value.usd === 'number' ? value.usd : undefined,
    source: { workers: sourceOf('workers'), usd: sourceOf('usd') },
  }
}

// The line under the queue: each effective limit and where it comes from ("repo" = the repo's flow.json is
// lower than the person's); undefined without limits.
export function queueLimitsText(limits: QueueLimits | undefined): string | undefined {
  if (!limits || (limits.workers === undefined && limits.usd === undefined)) return undefined
  const part = (text: string, source: LimitSource | undefined) => (source ? `${text} (${source})` : text)
  const parts = [
    ...(limits.workers !== undefined ? [part(`${limits.workers} worker(s)`, limits.source.workers)] : []),
    ...(limits.usd !== undefined ? [part(`$${limits.usd}`, limits.source.usd)] : []),
  ]
  return `limits: ${parts.join(' · ')}`
}

function queueRowOf(value: unknown): QueueRow | undefined {
  if (!isObject(value) || typeof value.topic !== 'string' || !TOPIC.test(value.topic)) return undefined
  return {
    topic: value.topic,
    state: typeof value.state === 'string' && QUEUE_STATES.has(value.state) ? (value.state as QueueRow['state']) : undefined,
    why: typeof value.why === 'string' ? value.why : undefined,
    run: typeof value.run === 'number' ? value.run : undefined,
    error: typeof value.error === 'string' ? value.error : undefined,
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function todoOf(value: unknown): RunTodo | undefined {
  if (!isObject(value)) return undefined
  return {
    text: typeof value.text === 'string' ? value.text : undefined,
    command: typeof value.command === 'string' ? value.command : undefined,
  }
}

// The run's display fields (migration step 3 of the run record): "phase" and "human" (runstate.human: the
// person's line and to-do, in the status_language setting), or "status_error". Rows from hooks without them
// parse as before.
function displayOf(value: Record<string, unknown>): Pick<Control, 'phase' | 'human' | 'statusError'> {
  const human = isObject(value.human) && typeof value.human.line === 'string'
    ? { line: value.human.line, todo: todoOf(value.human.todo) }
    : undefined
  return {
    ...(typeof value.phase === 'string' && PHASES.has(value.phase) ? { phase: value.phase as Phase } : {}),
    ...(human ? { human } : {}),
    ...(typeof value.status_error === 'string' ? { statusError: value.status_error } : {}),
  }
}

// The line under a control row: what the run is doing (the person's line and its to-do text, when there is
// one), or why the run state couldn't be read. Yellow for an orphan (the run may have died or did), red for an
// unknown state or an error; undefined when the hooks printed neither. Display only: the buttons still follow
// the row's action.
export function runLine(row: Control): { text: string; color?: 'yellow' | 'red' } | undefined {
  if (row.statusError !== undefined) return { text: row.statusError, color: 'red' }
  if (!row.human) return undefined
  const todo = row.human.todo?.text
  const text = todo ? `${row.human.line} → ${todo}` : row.human.line
  return { text, color: row.phase === 'orphan' ? 'yellow' : row.phase === 'unknown' ? 'red' : undefined }
}

function rowOf(value: unknown): Control | undefined {
  if (!isObject(value) || typeof value.topic !== 'string' || !TOPIC.test(value.topic)) return undefined
  const auto = isObject(value.auto) ? value.auto : {}
  const merge = isObject(value.merge) && typeof value.merge.reason === 'string'
    ? { reason: value.merge.reason, files: Array.isArray(value.merge.files) ? value.merge.files.filter((f): f is string => typeof f === 'string') : [] }
    : undefined
  const approving =
    isObject(value.approving) && typeof value.approving.run === 'number'
      ? { run: value.approving.run, since: typeof value.approving.since === 'string' ? value.approving.since : '' }
      : undefined
  return {
    ...(merge ? { merge } : {}),
    ...(approving ? { approving } : {}),
    ...displayOf(value),
    topic: value.topic,
    action: typeof value.action === 'string' && ACTIONS.has(value.action) ? (value.action as Action) : undefined,
    why: typeof value.why === 'string' ? value.why : undefined,
    run: typeof value.run === 'number' ? value.run : undefined,
    isHeld: value.held === true,
    auto: { ok: auto.ok === true, why: typeof auto.why === 'string' ? auto.why : undefined },
  }
}

// wt_controls.py's stdout. Anything unexpected is an error the pane shows, never an empty list.
export function parseControls(stdout: string): Controls {
  let data: unknown
  try {
    data = JSON.parse(stdout)
  } catch {
    return { controls: [], error: `wt_controls.py printed no JSON: ${stdout.trim().slice(0, 120)}` }
  }
  if (!isObject(data) || data.schema !== SCHEMA || !Array.isArray(data.controls)) {
    return { controls: [], error: `wt_controls.py output is not schema ${SCHEMA} (update the pane or the hooks)` }
  }
  const controls = data.controls.map(rowOf).filter((row): row is Control => row !== undefined)
  const localBlocker = typeof data.local_blocker === 'string' ? data.local_blocker : undefined
  const flowMode = data.flow_mode === 'origin' || data.flow_mode === 'local' ? (data.flow_mode as FlowMode) : undefined
  const flowModeError =
    typeof data.flow_mode_error === 'string'
      ? data.flow_mode_error
      : flowMode === undefined
        ? `wt_controls.py gave no flow mode (${JSON.stringify(data.flow_mode)})`
        : undefined
  // The config mode (homeconfig.configMode, DEV or TEST): its error, or no mode, is an error, never DEV.
  const configMode = data.config_mode === 'DEV' || data.config_mode === 'TEST' ? (data.config_mode as ConfigMode) : undefined
  const configModeError =
    typeof data.config_mode_error === 'string'
      ? data.config_mode_error
      : configMode === undefined
        ? `wt_controls.py gave no config mode (${JSON.stringify(data.config_mode)})`
        : undefined
  const queue = Array.isArray(data.queue)
    ? data.queue.map(queueRowOf).filter((row): row is QueueRow => row !== undefined)
    : []
  const planned = Array.isArray(data.planned)
    ? data.planned.map(plannedRowOf).filter((row): row is PlannedRow => row !== undefined)
    : []
  // Every row would read "no estimate yet": say why instead (Claude Code's sandbox hides plans/).
  const error =
    data.plans_visible === false && (controls.length > 0 || queue.length > 0)
      ? "wt_controls.py can't see the main checkout's plans/ (run sandboxed?): estimates unreadable"
      : typeof data.queue_error === 'string'
        ? data.queue_error
        : undefined
  const queueLimits = queueLimitsOf(data.queue_limits)
  return { controls, queue, planned, queueLimits, localBlocker, flowMode, flowModeError, configMode, configModeError, error }
}

// The topic of a write to <main>/plans/worker/<topic>.json, else undefined.
export function workerFileTopic(path: string, mainRoot: string): string | undefined {
  const prefix = `${mainRoot}/plans/worker/`
  if (!path.startsWith(prefix) || !path.endsWith('.json')) return undefined
  const topic = path.slice(prefix.length, -5)
  return TOPIC.test(topic) ? topic : undefined
}

// Auto mode, after the planning session wrote plans/worker/<topic>.json: the prompt to submit (none
// when it must stay by hand, as wt_controls.py's "auto" says) and the note the model reads with the
// Write result.
export function autoStep(topic: string, row: Control | undefined): { submit?: string; note: string } {
  const by = (why: string) =>
    `wt-pane auto: nothing started for ${topic}: ${why}. Tell the user to type \`wt <launch|resume> ${topic}\` or use the pane button.`
  if (!row) return { note: by('no worktree at ../worktrees/' + topic) }
  if (!row.auto?.ok || !row.action) return { note: by(row.auto?.why ?? row.why ?? 'nothing to start') }
  const text = `wt ${row.action} ${topic}`
  return {
    submit: text,
    note:
      `wt-pane auto: \`${text}\` is queued and is submitted by itself once this turn ends; the hook's ` +
      `"wt: ..." context comes with it, so start the wait then. Do not ask the user to type it; end the turn.`,
  }
}

// One line of <main>/.hook-only/events.jsonl (wt_events.py), and the toast it becomes: the ones the person
// must act on (a refusal, a failed start, step, wrap-up or finish, a run that stopped short of done) say so first.
export type WtEvent = { t?: string; kind?: string; topic?: string; text?: string }

// wt_events.py ATTENTION (hooks/tests/test_wt_events.py checks the two lists agree).
const ATTENTION = new Set(['refused', 'queue-failed', 'finish-failed', 'step-failed', 'wrapup-failed', 'heartbeat-stalled',
  'step-at-limit', 'merge-slow', 'limit-unreadable'])

export function needsYou(event: WtEvent): boolean {
  if (ATTENTION.has(event.kind ?? '')) return true
  const status = (event.text ?? '').split(': ').slice(1).join(': ')
  return event.kind === 'run-ended' && !status.startsWith('STATUS: done')
}

export function toastOf(event: WtEvent): string {
  return `${needsYou(event) ? 'needs you · ' : ''}${event.topic ?? '?'} ${event.kind ?? ''}: ${event.text ?? ''}`
}

// The events appended after `seen` characters of the log's text; a shorter text (replaced) starts over.
// A last line without its newline waits for the next read.
export function newEvents(text: string, seen: number): { events: WtEvent[]; seen: number } {
  const from = text.length < seen ? 0 : seen
  const end = text.lastIndexOf('\n') + 1
  if (end <= from) return { events: [], seen: from }
  const events: WtEvent[] = []
  for (const line of text.slice(from, end).split('\n')) {
    if (!line.trim()) continue
    try {
      const value = JSON.parse(line)
      if (isObject(value)) events.push(value as WtEvent)
    } catch {
      // a broken line is skipped
    }
  }
  return { events, seen: end }
}

export type AutoCommand = 'on' | 'off' | 'show'

// `wt auto`, `wt auto on`, `wt auto off`; worker-control.py's regexes take only launch|resume|stop, merge and wrapup.
export function autoCommand(text: string): AutoCommand | undefined {
  const match = /^\s*wt\s+auto(?:\s+(on|off))?\s*$/.exec(text)
  if (!match) return undefined
  return (match[1] as AutoCommand | undefined) ?? 'show'
}

// `wt local`, `wt local on`, `wt local off`: the flow mode (local main vs origin's PRs).
export function localCommand(text: string): AutoCommand | undefined {
  const match = /^\s*wt\s+local(?:\s+(on|off))?\s*$/.exec(text)
  if (!match) return undefined
  return (match[1] as AutoCommand | undefined) ?? 'show'
}

// Only the person turns auto on or switches the mode: their Enter at the terminal or through Remote
// Control, never a plugin, a peer session or a notification. The one check that stays in the mod: a
// hook's UserPromptSubmit input carries no origin.
export function isPersonal(origin: { kind: string } | undefined): boolean {
  return origin?.kind === 'composer' || origin?.kind === 'bridge'
}

export function label(action: Action, isArmed: boolean): string {
  return isArmed ? `confirm ${action}?` : action
}

// ---- Sent buttons and notices (so a pressed `merge` never shows nothing for minutes, nor a toast cut
// short). A pressed button's prompt is Pending until its outcome shows; events and refusals become Notices.

// The state of a topic's row the pane can see, compared before and after a press: a change (the run
// started, the queue took it, the merge decision moved) means the press took effect. '' is `queue all`.
export function markOf(topic: string, controls: Control[], queue: QueueRow[], planned: PlannedRow[]): string {
  if (topic === '') return planned.map(row => row.topic).join(' ')
  const control = controls.find(row => row.topic === topic)
  const item = queue.find(row => row.topic === topic)
  return JSON.stringify([
    control?.action ?? null,
    control?.run ?? null,
    control?.merge ? 1 : 0,
    item?.state ?? null,
    item ? 1 : 0,
    planned.some(row => row.topic === topic) ? 1 : 0,
  ])
}

// The marks of the pane's current view, or undefined for every topic while wt_controls.py's output was
// unreadable (failed, timed out, another schema, plans/ or the queue hidden): its rows are then empty or
// partial, and a mark read from them is no outcome. Seen: a fetch that failed while the approval's
// tests loaded the machine emptied the rows, the mark changed, the pending `merge` was released, and the
// next good fetch drew the merge button again for a second press.
export function marksOf(view: Pick<View, 'controls' | 'queue' | 'planned' | 'controlsError'>): (topic: string) => string | undefined {
  if (view.controlsError !== undefined) return () => undefined
  return topic => markOf(topic, view.controls, view.queue ?? [], view.planned ?? [])
}

// Whether worker-control.py took a pressed prompt. It answers `wt` prompts as a block (the prompt never
// enters the session) for refusals and also for some acceptances, so the wording decides; these patterns
// follow worker-control.py's done(block=...) texts (merge_command, queue_command) and change with them.
// launch, resume and stop are accepted without a block.
// "already being approved" counts as taken: one approval of that run is running, and its outcome is what
// the press waits for. Matched after a line start or a space, not only at the text's start, in case the
// engine puts words before the hook's reason.
const ACCEPTED: Record<string, RegExp> = {
  merge: /(?:^|\s)wt: merge: (approving |\S+ run \d+ is already being approved )/,
  queue: /(?:^|\s)wt: (queued |started now: )/,
  'queue all': /(?:^|\s)wt: (queued |started now: )/,
  drop: /(?:^|\s)wt: queue: \S+ dropped/,
}

export function sentOutcome(action: string, drop: string | undefined): 'accepted' | 'refused' {
  if (drop === undefined) return 'accepted'
  const accepted = ACCEPTED[action]
  return accepted && accepted.test(drop) ? 'accepted' : 'refused'
}

// The pending presses still waiting for their outcome: one goes when its topic has an event at or after
// the press, when its row's mark changed, or at its time limit. An unknown mark now (marksOf: unreadable
// controls) changes nothing; a press made while the mark was unknown takes the first known one as its own.
export function settle(
  pending: Pending[],
  markNow: (topic: string) => string | undefined,
  events: WtEvent[],
  nowMs: number,
): Pending[] {
  const kept: Pending[] = []
  for (const item of pending) {
    if (nowMs >= item.untilMs) continue
    const answered = events.some(event => {
      const at = eventMs(event)
      return (event.topic === item.topic || item.topic === '') && at !== undefined && at >= item.sinceMs - 1000
    })
    if (answered) continue
    const mark = markNow(item.topic)
    if (mark === undefined || mark === item.mark) kept.push(item)
    else if (item.mark === undefined) kept.push({ ...item, mark })
  }
  return kept
}

// events.jsonl writes local time without an offset, which Date.parse reads as local time.
export function eventMs(event: WtEvent): number | undefined {
  const ms = event.t ? Date.parse(event.t) : NaN
  return Number.isNaN(ms) ? undefined : ms
}

// How long a toast stays: long enough to read (chars_per_second), within [min, max]; one that needs the
// person stays the longest.
export function toastMs(text: string, needs: boolean, limits: { min: number; max: number; charsPerSecond: number }): number {
  if (needs) return limits.max * 1000
  return Math.round(Math.min(limits.max, Math.max(limits.min, text.length / limits.charsPerSecond)) * 1000)
}

export function noticeOf(event: WtEvent): Notice {
  return { t: event.t ?? '', kind: event.kind ?? '', topic: event.topic ?? '', text: event.text ?? '', needs: needsYou(event) }
}

// One line of the Notices list: time (HH:MM), a mark for the ones that need the person, the whole text.
export function noticeLine(notice: Notice): string {
  const time = /T(\d\d:\d\d)/.exec(notice.t)?.[1] ?? notice.t
  return `${time} ${notice.needs ? '! ' : ''}${notice.topic} ${notice.kind}: ${notice.text}`
}

// The list after new notices: newest first, at most max; unread counts what arrived since it was opened.
export function addNotices(notices: Notices, added: Notice[], max: number): Notices {
  if (added.length === 0) return notices
  return {
    items: [...[...added].reverse(), ...notices.items].slice(0, max),
    unread: notices.open ? 0 : notices.unread + added.length,
    open: notices.open,
  }
}

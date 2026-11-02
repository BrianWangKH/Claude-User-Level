// Typing `wt pane`, or pressing the `wt pane` button in the band above the prompt (shown by default,
// config band_toggle), toggles a pane of this repo's worktree workers. Everything comes from files hooks
// write (see model.ts); the mod never calls the model, and the toggle drops the prompt before it
// enters the session (a prompt.submit hook answering without next enters nothing; the drop text is
// shown to the person only), so opening, closing and every redraw cost no tokens. No slash command
// is registered, so nothing is added to any listing the model reads. Polling runs only while the
// pane is open. worker-control.py's `wt` regex takes only launch|resume|stop, so `wt pane` is ours.
//
// Three ways to start and stop workers, all ending in the same prompt `wt launch|resume|stop <topic>`
// that worker-control.py (UserPromptSubmit) handles, so that hook stays the only thing that starts
// a worker. What each topic offers is decided by ~/.claude/hooks/wt_controls.py, which the pane runs
// (the mod draws, it doesn't decide; control.ts reads the output):
//   by hand  type `wt ...` as before; nothing here changes it.
//   buttons  the pane's Controls rows: press once to arm, again within confirm_seconds to submit.
//   auto     `wt auto on` (typed by the person; this session only, off by default): when the
//            planning session writes <main>/plans/worker/<topic>.json, `wt launch|resume <topic>` is
//            submitted once the turn ends. Never `wt stop`, never across an agent-guard HOLD.
// The pane's `local` button (press twice) or `wt local on|off` typed by the person switches the flow mode:
// <main>/.hook-only/flow-mode ("local" or "origin"; gitignored, so no commit; only hooks and this mod
// write there, never an agent) for later sessions, which session.start reads into FLOW_INTEGRATION, and
// this process's environment for the hooks and workers it starts from now on. Not settings.local.json:
// Claude Code manages that file and left it read-only (2026-10-31), so a write there failed.
// Refused when wt_controls.py --local says so (a worker runs, a topic is planned but not launched, or,
// for on, the local main lags origin/main). A topic keeps the mode it was launched in (worker-control.py
// records .hook-only/flow-mode in its worktree).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, ToolCallResult } from 'claude-code'

import type { Action, AgentRow, Armed, Control, Notices, Pending, PlannedRow, QueueRow, View, WorkerRow } from '../types'
import {
  addNotices,
  autoCommand,
  autoStep,
  isPersonal,
  label,
  localCommand,
  marksOf,
  needsYou,
  newEvents,
  noticeLine,
  noticeOf,
  parseControls,
  queueAllText,
  sentOutcome,
  settle,
  toastMs,
  toastOf,
  workerFileTopic,
  type Controls,
  type WtEvent,
} from './control'
import { ago, bar, exitLabel, minutes, snapshot, track, usd, used, workedMs, type Files } from './model'
import { deskCentre, fits, MIN_SLOT, office, slotFor } from './office'
import { encode, walk } from './sprite'

const PANE = 'wt-pane'
const TOGGLE = /^\s*wt\s+pane\s*$/
const SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
const DEFAULTS = {
  poll_seconds: 2,
  spinner_ms: 250,
  linger_minutes: 10,
  bar_width: 12,
  // stage: the office of the chosen worker (terminal only; other surfaces draw the list);
  // list: text rows; both: the office over the text rows.
  view: 'stage' as 'list' | 'stage' | 'both',
  pose_hold_seconds: 20,
  walk_step: 2, // pixels the orchestrator walks per tick
  stations: ['qa-planner', 'spec-reviewer', 'qa', 'developer', 'code-reviewer'],
  colors: {} as Record<string, string>,
  labels: {} as Record<string, string>,
  verbs: {} as Record<string, string>,
  idle_verb: 'thinking',
  wait_verb: 'waiting',
  confirm_seconds: 5, // a Start/Stop button submits only on a second press within this
  band_toggle: true, // the `wt pane` button in the band above the prompt, so the pane is one press away
  events_poll_seconds: 15, // how often a planning session reads .hook-only/events.jsonl for toasts, pane open or not
  controls_seconds: 5, // how often the pane asks wt_controls.py again (the worker rows poll every poll_seconds)
  // Yellow header and flashing bubbles at this share of the estimate: 1 = past it, into the
  // budget_headroom the hooks allow before they stop the run or the subagent.
  warn_ratio: 1,
  // A sent button stays busy at most this long when no outcome shows (a merge runs the whole test suite first).
  pending_minutes: 15,
  notices_max: 30, // events kept in the Notices list
  // How long a toast stays: its length at chars_per_second, within [min, max]; one that needs the person, max.
  toast_min_seconds: 6,
  toast_max_seconds: 20,
  toast_chars_per_second: 20,
}

// Where each worker's orchestrator stood when last drawn, by topic, so it walks to the next desk.
const positions = new Map<string, { x: number; tick: number }>()

// Change the tag when View's fields change: it declines what an earlier version stored.
const view = atom({ plugin: 'wt-pane', key: 'view' } as const, { workers: [], controls: [], nowMs: 0, tick: 0 } as View, {
  shape: 'view-3',
})
// The worker whose office the pane shows: a topic, or '' for the first running one.
const tab = atom({ plugin: 'wt-pane', key: 'tab' } as const, '', { shape: 'tab-1' })
// Auto mode, for this session only ($.state, not $.store): a new session starts by hand.
const auto = atom({ plugin: 'wt-pane', key: 'auto' } as const, false, { shape: 'auto-1' })
const NONE: Armed = { topic: '', action: '', untilMs: 0 }
const armed = atom({ plugin: 'wt-pane', key: 'armed' } as const, NONE, { shape: 'armed-1' })
// Buttons sent and waiting for their outcome (control.settle), and the Notices list.
const sent = atom({ plugin: 'wt-pane', key: 'pending' } as const, [] as Pending[], { shape: 'pending-1' })
const NO_NOTICES: Notices = { items: [], unread: 0, open: false }
const noticesAtom = atom({ plugin: 'wt-pane', key: 'notices' } as const, NO_NOTICES, { shape: 'notices-1' })

// Auto prompts submitted and not yet seen by our prompt.submit hook, by topic: one per write burst.
const queued = new Map<string, number>()
// Auto prompts waiting for the main loop's turn to end, by topic (afterWrite fills, turn.complete sends).
const pending = new Map<string, string>()
const QUEUE_MS = 60000

let timers: { cancel: () => void }[] = []
let config = DEFAULTS
// Whether the last poll found a running worker: the spinner redraws only then.
let anyRunning = false

// The events watcher (toasts), independent of the pane: its timer and how much of the log it has read.
let eventsTimer: { cancel: () => void } | undefined
let eventsSeen = 0

// A toast that stays long enough to read (config toast_*).
function toast($: EngineInterface, text: string, needs = false): void {
  const timeoutMs = toastMs(text, needs, {
    min: config.toast_min_seconds,
    max: config.toast_max_seconds,
    charsPerSecond: config.toast_chars_per_second,
  })
  void Promise.resolve()
    .then(() => $.ui.toast(text, { timeoutMs }))
    .catch(() => undefined)
}

// A planning session (not a worker: WT_MAIN_DIR set) toasts each new event from <main>/.hook-only/events.jsonl
// (wt_events.py) and keeps it in the Notices list; it starts at the log's end, so only what happens from now
// on is toasted, but an empty list is filled with the last notices_max events first. An event also settles
// the sent buttons of its topic.
async function watchEvents($: EngineInterface): Promise<void> {
  eventsTimer?.cancel()
  eventsTimer = undefined
  if ((await $.env.get('WT_MAIN_DIR')) !== undefined) return
  const mainRoot = await mainRootOf($)
  if (!mainRoot) return
  const path = `${mainRoot}/.hook-only/events.jsonl`
  const files = filesOf($)
  const text = (await files.read(path)) ?? ''
  eventsSeen = text.length
  if ((await read($, noticesAtom)).items.length === 0) {
    const earlier = newEvents(text, 0).events.slice(-config.notices_max).map(noticeOf)
    await update($, noticesAtom, old => ({ ...addNotices(old, earlier, config.notices_max), unread: 0 }))
  }
  eventsTimer = $.clock.every(config.events_poll_seconds * 1000, () => {
    void files.read(path).then(async read2 => {
      const { events, seen } = newEvents(read2 ?? '', eventsSeen)
      eventsSeen = seen
      if (events.length === 0) return
      for (const event of events) toast($, toastOf(event), needsYou(event))
      await update($, noticesAtom, old => addNotices(old, events.map(noticeOf), config.notices_max))
      await settleSent($, events)
      redrawBand($)
    })
  })
}

// Drop the sent buttons whose outcome showed: an event of their topic, a changed row, or their time limit.
async function settleSent($: EngineInterface, events: WtEvent[] = []): Promise<void> {
  const nowMs = await $.clock.now()
  const markNow = marksOf(await read($, view))
  await update($, sent, old => settle(old, markNow, events, nowMs))
}

// A pressed button's prompt: the topic's buttons turn busy at once, then worker-control.py's answer says
// whether it took it (control.sentOutcome). A refusal frees the buttons and stays in the Notices list.
async function sendButton($: EngineInterface, topic: string, action: string, text: string): Promise<void> {
  const at = await $.clock.now()
  const mark = marksOf(await read($, view))(topic)
  await update($, sent, old => [
    ...old.filter(item => item.topic !== topic),
    { topic, action, sinceMs: at, untilMs: at + config.pending_minutes * 60000, mark },
  ])
  const note = async (line: string, kind: string, needs: boolean) => {
    toast($, line, needs)
    const stamp = new Date(await $.clock.now()).toISOString().slice(0, 19)
    await update($, noticesAtom, old => addNotices(old, [{ t: stamp, kind, topic, text: line, needs }], config.notices_max))
  }
  void $.prompt.submit({ text }).then(
    async result => {
      if (sentOutcome(action, result.drop) === 'refused') {
        await update($, sent, old => old.filter(item => item.topic !== topic))
        await note(`${text}: ${result.drop ?? 'not taken'}`, 'not-done', true)
      } else {
        await note(result.drop ? `${text}: ${result.drop}` : `${text}: sent`, 'sent', false)
      }
    },
    async (error: unknown) => {
      await update($, sent, old => old.filter(item => item.topic !== topic))
      await note(`${text}: not sent (${String(error)})`, 'not-done', true)
    },
  )
}

// Bumped by every stop: a refresh still reading files when the pane stopped draws nothing.
let generation = 0

function stop() {
  generation += 1
  for (const timer of timers) timer.cancel()
  timers = []
}

// Most topics in workers/ belong to worktrees already removed, and many files are optional:
// ask first, so a missing path is an answer and not a failed call logged on every poll.
function filesOf($: EngineInterface): Files {
  return {
    list: async dir => {
      try {
        return (await $.fs.exists(dir)) ? (await $.fs.list(dir)).map(entry => entry.name) : []
      } catch {
        return []
      }
    },
    read: async path => {
      try {
        return (await $.fs.exists(path)) ? ((await $.fs.read(path)) as string) : undefined
      } catch {
        return undefined
      }
    },
  }
}

// The main checkout holds .hook-only/workers and plans/worker, also when this session runs in a
// worktree. One short git call, killed by the engine past the timeout.
async function mainRootOf($: EngineInterface): Promise<string | undefined> {
  const git = await $.process.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], {
    timeoutMs: 5000,
  }).catch(() => ({ exitCode: 1, stdout: '' }))
  const commonDir = git.stdout.trim()
  return git.exitCode === 0 && commonDir.endsWith('/.git') ? commonDir.slice(0, -5) : undefined
}

// The decisions: wt_controls.py beside worker-control.py, found from this mod's own folder
// (<home>/mods/wt-pane -> <home>/hooks), not a hard-coded ~/.claude. A failure is an error the pane
// shows, never an empty Controls list.
async function fetchControls($: EngineInterface, mainRoot: string, local?: 'on' | 'off'): Promise<Controls> {
  const script = `${$.plugin.root}/../../hooks/wt_controls.py`
  const ran = await $.process
    .run(['python3', script, mainRoot, ...(local ? ['--local', local] : [])], { timeoutMs: 10000 })
    .catch((error: unknown) => ({ exitCode: -1, stdout: '', stderr: String(error) }))
  if (ran.exitCode !== 0) {
    return { controls: [], error: `wt_controls.py failed (exit ${ran.exitCode}): ${(ran.stderr ?? '').trim().slice(-160)}` }
  }
  return parseControls(ran.stdout)
}

async function loadConfig($: EngineInterface): Promise<void> {
  try {
    const file = JSON.parse((await filesOf($).read(`${$.plugin.root}/config.json`)) ?? '{}')
    config = {
      ...DEFAULTS,
      ...file,
      colors: { ...DEFAULTS.colors, ...file.colors },
      labels: { ...DEFAULTS.labels, ...file.labels },
      verbs: { ...DEFAULTS.verbs, ...file.verbs },
    }
  } catch {
    config = DEFAULTS
  }
}

// Ask the band to draw again (its answer is cached); a surface that can't is no reason to fail a toggle.
function redrawBand($: EngineInterface): void {
  try {
    $.ui.invalidate('ui.render')
  } catch {
    // no render surface
  }
}

async function isPaneOpen($: EngineInterface): Promise<boolean> {
  try {
    return (await $.ui.panes()).some(pane => pane.id === PANE)
  } catch {
    return false
  }
}

// `wt pane` typed or the band's button: close an open pane, else open it and start polling. The band's
// label follows at once (its answer is cached until asked to draw again).
async function togglePane($: EngineInterface): Promise<string> {
  let text: string
  if (await isPaneOpen($)) {
    await $.ui.close({ id: PANE })
    text = 'wt pane: closed'
  } else {
    const opened = await $.ui.open({ id: PANE, title: 'Workers' })
    await start($)
    text = opened.isPlaced ? 'wt pane: opened' : `wt pane: waiting for room (${opened.reason})`
  }
  redrawBand($)
  return text
}

async function start($: EngineInterface) {
  stop()
  const mine = generation
  const files = filesOf($)
  await loadConfig($)
  const mainRoot = await mainRootOf($)
  if (mine !== generation) return

  // The controls are asked for every controls_seconds, not every poll: one python start each time.
  let controlsAt = -Infinity
  let lastControls: Controls = { controls: [] }
  const refresh = async () => {
    const nowMs = await $.clock.now()
    if (mine !== generation) return
    if (!mainRoot) {
      await update($, view, old => ({ ...old, workers: [], controls: [], nowMs, error: 'Not in a git checkout.' }))
      return
    }
    const workers = await snapshot(files, mainRoot, nowMs, config.linger_minutes * 60000)
    if (nowMs - controlsAt >= config.controls_seconds * 1000) {
      controlsAt = nowMs
      lastControls = await fetchControls($, mainRoot)
    }
    if (mine !== generation) return
    anyRunning = workers.some(row => row.exitMs === undefined)
    const { controls, queue, planned, error: controlsError } = lastControls
    await update($, view, old => ({ ...old, workers, controls, queue, planned, controlsError, nowMs, error: undefined }))
    await settleSent($)
  }
  await refresh()
  timers.push($.clock.every(config.poll_seconds * 1000, () => void refresh()))
  timers.push(
    $.clock.every(config.spinner_ms, () => {
      if (anyRunning) void update($, view, old => ({ ...old, tick: old.tick + 1 }))
    }),
  )
}

// Auto mode: the planning session's estimate file is the signal (handoff.md writes it right
// before every launch and resume). The Write itself is untouched; its result carries a note. The
// tool has run once next(e) resolved: whatever goes wrong here, its result stands.
async function afterWrite($: EngineInterface, path: string, ran: ToolCallResult): Promise<ToolCallResult> {
  try {
    if (ran.deny !== undefined || ran.isError || !(await read($, auto))) return ran
    const mainRoot = await mainRootOf($)
    const topic = mainRoot ? workerFileTopic(path, mainRoot) : undefined
    if (!mainRoot || !topic) return ran
    const fetched = await fetchControls($, mainRoot)
    const step = fetched.error
      ? { note: `wt-pane auto: nothing started for ${topic}: ${fetched.error}. Tell the user to type \`wt <launch|resume> ${topic}\`.` }
      : autoStep(topic, fetched.controls.find(row => row.topic === topic))
    const nowMs = await $.clock.now()
    if (step.submit && nowMs - (queued.get(topic) ?? 0) > QUEUE_MS) {
      queued.set(topic, nowMs)
      // Sent at the main loop's turn.complete, not from inside the running turn: submitted from here
      // the prompt never arrived (2026-11-01, one and three topics alike), and the result was ignored.
      pending.set(topic, step.submit)
      void $.ui.toast(`wt auto: ${step.submit} after this turn`)
    }
    return { ...ran, context: [...(ran.context ?? []), step.note] }
  } catch {
    return ran
  }
}

// prompt.submit resolves `{ drop }` when the prompt did not enter (a hook's refusal, or the engine's
// own, which is only logged), and that is no rejection: read the result and say why, never swallow it.
function sendAuto($: EngineInterface, topic: string, text: string): void {
  const report = (why: string) => {
    queued.delete(topic)
    void $.ui.toast(`wt auto: \`${text}\` not sent (${why}); type it or press the pane button`)
    void $.ui.log(`wt-pane auto: ${text} not sent: ${why}`)
  }
  void $.prompt.submit({ text }).then(
    result => {
      if (result.drop !== undefined) report(result.drop || 'dropped, no reason given')
    },
    (error: unknown) => report(String(error)),
  )
}

// The repo's switch file, relative to the main checkout.
const FLOW_MODE = '.hook-only/flow-mode'

// At a planning session's start: the switch file, when present, sets FLOW_INTEGRATION for this process
// and everything it starts. A worker (WT_MAIN_DIR set by worker-control.py) keeps the mode run-worker.sh
// exported from its topic's record.
async function loadFlowMode($: EngineInterface): Promise<void> {
  if ((await $.env.get('WT_MAIN_DIR')) !== undefined) return
  const mainRoot = await mainRootOf($)
  if (!mainRoot || !(await $.fs.exists(`${mainRoot}/${FLOW_MODE}`))) return
  const mode = String(await $.fs.read(`${mainRoot}/${FLOW_MODE}`)).trim()
  await $.env.set('FLOW_INTEGRATION', mode === 'local' ? 'local' : undefined)
}

// Whether this process runs in local mode. A failed read counts as off, so the pane always draws.
async function localModeOn($: EngineInterface): Promise<boolean> {
  try {
    return (await $.env.get('FLOW_INTEGRATION')) === 'local'
  } catch {
    return false
  }
}

// `wt local [on|off]`: the drop text the person sees; never enters the session.
async function switchLocal($: EngineInterface, command: 'on' | 'off' | 'show', personal: boolean): Promise<string> {
  const isOn = await localModeOn($)
  const shown = isOn ? 'on (local main is the source of truth, automatic merge)' : 'off (origin, PRs)'
  if (command === 'show') return `wt local: ${shown} (wt local on|off)`
  if (!personal) return 'wt local: only the person typing at the prompt can switch it'
  const want = command === 'on'
  if (want === isOn) return `wt local: already ${shown}`
  const mainRoot = await mainRootOf($)
  if (!mainRoot) return 'wt local: not in a git repo'
  const checked = await fetchControls($, mainRoot, want ? 'on' : 'off')
  if (checked.error) return `wt local: not switched: can't check (${checked.error})`
  if (checked.localBlocker) return `wt local: not switched: ${checked.localBlocker}`
  const path = `${mainRoot}/${FLOW_MODE}`
  try {
    await $.fs.write(path, want ? 'local\n' : 'origin\n')
  } catch (error) {
    return `wt local: not switched: can't write ${path} (${String(error)})`
  }
  await $.env.set('FLOW_INTEGRATION', want ? 'local' : undefined)
  return want
    ? 'wt local: on — new topics start from and merge into the local main; running topics keep their mode'
    : 'wt local: off — new topics use origin and PRs; running topics keep their mode'
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await loadConfig($)
    try {
      await watchEvents($)
    } catch {
      // no toasts: the pane and the prompt hook still carry the events
    }
    // A reload keeps the pane up but drops the timers: start them again. A headless worker
    // (`claude -p`) loads the mod too and has no panes; nothing to do there.
    try {
      await loadFlowMode($)
    } catch {
      // no switch file read: the mode stays as the environment has it
    }
    try {
      if ((await $.ui.panes()).some(pane => pane.id === PANE)) await start($)
    } catch {
      // no surface
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    const command = autoCommand(e.text)
    if (command) {
      if (command === 'show') return { drop: `wt auto: ${(await read($, auto)) ? 'on' : 'off'} (wt auto on|off)` }
      if (!isPersonal(e.origin)) return { drop: 'wt auto: only the person typing at the prompt can switch it' }
      await update($, auto, () => command === 'on')
      return {
        drop:
          command === 'on'
            ? 'wt auto: on for this session — a new plans/worker/<topic>.json starts or resumes its worker when the turn ends (not across an agent-guard HOLD; stop stays by hand)'
            : 'wt auto: off — type wt launch|resume, or use the pane buttons',
      }
    }
    const local = localCommand(e.text)
    if (local) return { drop: await switchLocal($, local, isPersonal(e.origin)) }
    const started = /^\s*wt\s+(?:launch|resume)\s+(\S+)\s*$/.exec(e.text)
    if (started) queued.delete(started[1])
    if (!TOGGLE.test(e.text)) return next(e)
    return { drop: await togglePane($) }
  })

  // The main loop's turn has ended: send what auto queued during it. An interrupted or failed turn
  // keeps them for the next answer. Not awaited: the prompt waits for the session to go idle, which
  // this hook would hold up.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined && e.reason === 'answer' && pending.size > 0) {
      const ready = [...pending.entries()]
      pending.clear()
      for (const [topic, text] of ready) sendAuto($, topic, text)
    }
    return result
  })

  on('tool.call', { tool: 'Write' }, async ($, e, next) => afterWrite($, e.file_path, await next(e)))
  on('tool.call', { tool: 'Edit' }, async ($, e, next) => afterWrite($, e.file_path, await next(e)))

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      stop()
      redrawBand($) // the band's button reads "wt pane" again
    }
    return next(e)
  })

  // The band above the prompt: one `wt pane` button (and how many workers run while the pane is open), so
  // the pane is a press away without typing; it yields to a survey. The pane itself stays closed until
  // pressed. Terminal and desktop raise the band; elsewhere nothing changes.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !config.band_toggle) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const isOpen = await isPaneOpen($)
    const running = isOpen ? (await read($, view)).workers.filter(row => row.exitMs === undefined).length : 0
    const unread = (await read($, noticesAtom)).unread
    return (
      <Box flexDirection="row">
        <Button key="pane-toggle" onPress={() => void togglePane($).then(text => $.ui.toast(text))}>
          <Text color="cyan">{isOpen ? 'close wt pane' : 'wt pane'}</Text>
        </Button>
        {running > 0 && <Text dimColor> ▶ {running} running</Text>}
        {unread > 0 && <Text color="yellow"> · {unread} new notice{unread > 1 ? 's' : ''}</Text>}
      </Box>
    )
  })

  // A session that ends (exit, logout, deleted) stops polling and closes the pane; the module's
  // unload would drop the timers anyway, this does it at once. /clear ends the conversation with
  // no session.start after it while the pane stays up in the same process: keep polling there.
  on('session.end', async ($, e, next) => {
    if (e.reason !== 'clear') {
      stop()
      eventsTimer?.cancel()
      eventsTimer = undefined
      await $.ui.close({ id: PANE }).catch(() => undefined)
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Box, Button, Text } = ui
    // Only the terminal draws a Raster: elsewhere the office gives way to the text rows. Asked of
    // the surface, not the table: another surface's table answers `'Raster' in ui` as well.
    const Raster = e.surface === 'terminal' && 'Raster' in ui ? ui.Raster : undefined
    const state = await read($, view)
    const spin = SPINNER[state.tick % SPINNER.length]
    const now = state.nowMs

    const head = (row: WorkerRow) => {
      if (row.exitMs !== undefined) {
        const cost = row.resultUsd !== undefined ? ` · ${usd(row.resultUsd)}` : ''
        return (
          <Text color={row.exitCode === 0 ? 'green' : 'red'}>
            {row.exitCode === 0 ? '✓' : '✗'} {row.topic} · run {row.run} · {exitLabel(row.exitCode ?? -1)}
            {cost} · {ago(now - row.exitMs)}
          </Text>
        )
      }
      const ratio = used(row, now)
      // Against the estimate, with the limit where the hooks stop the run (estimate x headroom).
      const time = `${minutes(now - row.startedMs)}${row.estMinutes ? `/${row.estMinutes}m` : ''}${row.capMinutes ? ` (stop ${row.capMinutes}m)` : ''}`
      const usdBase = row.estUsd ?? row.capUsd
      const stopUsd = row.estUsd !== undefined && row.capUsd ? ` (stop ${usd(row.capUsd)})` : ''
      const cost = `${usd(row.usdRun)}${usdBase ? `/${usd(usdBase)}` : ''}${stopUsd}${row.unpriced ? ` +${row.unpriced} unpriced` : ''}`
      const meter = ratio === undefined ? '' : ` ${bar(ratio, config.bar_width)} ${Math.round(ratio * 100)}%`
      const compacts = row.compacts ? ` · compact ${row.compacts}` : ''
      return (
        <Text bold color={ratio !== undefined && ratio >= config.warn_ratio ? 'yellow' : undefined}>
          {spin} {row.topic} · run {row.run} · {time} · {cost}
          {meter}
          {compacts}
        </Text>
      )
    }

    // A hook holding the agent (the mutation scan before its hand-back): how long, against the wait's limit.
    const heldFor = (agent: AgentRow) =>
      `waiting for ${agent.waitWhat ?? 'a hook'} ${minutes(now - (agent.waitSinceMs ?? now))}${agent.waitMaxMinutes ? `/${Math.round(agent.waitMaxMinutes)}m` : ''}`

    const steps = (row: WorkerRow) => {
      const parts = [
        ...row.done.map(step => `${step.type}${step.count > 1 ? `×${step.count}` : ''} ✓`),
        ...row.agents.map(agent => `${agent.type} ▶`),
      ]
      return parts.length ? <Text dimColor>  {parts.join(' → ')}</Text> : undefined
    }

    const doing = (row: WorkerRow) => [
      ...row.agents.map(agent => {
        const time = `${minutes(workedMs(agent, now))}${agent.budgetMinutes ? `/${agent.budgetMinutes}m` : ''}`
        const spent = agent.usdRound ?? agent.usdRun
        const cost = spent !== undefined ? ` ${usd(spent)}${agent.budgetUsd ? `/${usd(agent.budgetUsd)}` : ''}` : ''
        const last = agent.waitSinceMs !== undefined
          ? ` · ${heldFor(agent)}`
          : agent.tool ? ` · ${agent.tool} ${agent.what ?? ''} (${ago(now - (agent.lastMs ?? now))})` : ''
        return (
          <Text wrap="truncate-end">
            {'  '}▶ {agent.type} {time}
            {cost}
            {last}
          </Text>
        )
      }),
      ...(row.orchestrator
        ? [
            <Text wrap="truncate-end" dimColor={row.agents.length > 0}>
              {'  '}◆ orchestrator · {row.orchestrator.tool} {row.orchestrator.what} (
              {ago(now - row.orchestrator.lastMs)})
            </Text>,
          ]
        : []),
    ]

    const options = {
      stations: config.stations,
      colors: config.colors,
      labels: config.labels,
      verbs: config.verbs,
      idleVerb: config.idle_verb,
      waitVerb: config.wait_verb,
      holdMs: config.pose_hold_seconds * 1000,
      warnRatio: config.warn_ratio,
    }
    const slot = slotFor(e.props.bodyColumns, config.stations.length)
    // Too narrow for every desk: draw the text rows instead of cutting desks off at the edge.
    const isRoomy = fits(e.props.bodyColumns, config.stations.length)
    const officeOf = (row: WorkerRow) => {
      if (!Raster || !isRoomy) return undefined
      const { desks, seated, boss } = track(row, now, options)
      const target = deskCentre(boss.desk, slot)
      const last = positions.get(row.topic)
      const x = last ? walk(last.x, target, state.tick - last.tick, config.walk_step) : target
      positions.set(row.topic, { x, tick: state.tick })
      const walker = { ...boss, x, pose: x === target ? boss.pose : ('walk' as const) }
      const drawn = office(desks, seated, walker, slot, state.tick)
      return <Raster key={`office-${row.topic}`} columns={drawn.columns} rows={drawn.rows} cells={encode(drawn.cells)} />
    }

    // One tab per worker (1-9 press it): ▶ running, ✓ done, ✗ failed. The chosen one is drawn.
    const chosen = await read($, tab)
    const shown = state.workers.find(row => row.topic === chosen) ?? state.workers[0]
    const tabs = state.workers.map((row, index) => {
      const mark = row.exitMs === undefined ? '▶' : row.exitCode === 0 ? '✓' : '✗'
      const color = row.exitMs === undefined ? 'cyan' : row.exitCode === 0 ? 'green' : 'red'
      return (
        <Button
          key={`tab-${row.topic}`}
          hotkey={index < 9 ? String(index + 1) : undefined}
          onPress={() => void update($, tab, () => row.topic)}
        >
          <Text color={color}>{mark}</Text> <Text bold={row === shown}>{row.topic}</Text>
        </Button>
      )
    })

    // Under the office, one line per working subagent: its time and this round's spend against
    // its budget, in its color, so a flashing bubble shows which one ran over.
    const budgets = (row: WorkerRow) =>
      row.agents.map(agent => {
        const label = config.labels[agent.type] ?? agent.type
        const time = `${minutes(workedMs(agent, now))}${agent.budgetMinutes ? `/${agent.budgetMinutes}m` : ''}`
        const spent = agent.usdRound ?? agent.usdRun
        const cost = spent !== undefined ? `  ${usd(spent)}${agent.budgetUsd ? `/${usd(agent.budgetUsd)}` : ''}` : ''
        return (
          <Text color={config.colors[agent.type] ?? config.colors.other}>
            ▶ {label}  {time}
            {cost}
            {agent.waitSinceMs !== undefined ? `  ${heldFor(agent)}` : ''}
          </Text>
        )
      })

    const body = (row: WorkerRow) => {
      if (row.exitMs !== undefined) return [head(row)]
      const drawn = config.view !== 'list' ? officeOf(row) : undefined
      const text = config.view !== 'stage' || drawn === undefined
      const narrow =
        Raster && !isRoomy && config.view !== 'list' ? (
          <Text dimColor>
            office needs {config.stations.length * MIN_SLOT} columns (pane has {e.props.bodyColumns}): widen it to see it
          </Text>
        ) : undefined
      return [head(row), drawn, narrow, ...(drawn ? budgets(row) : []), text ? steps(row) : undefined, ...(text ? doing(row) : [])]
    }

    // Controls: one row per topic with a worktree. A press arms the button; a second press within
    // confirm_seconds submits `wt <action> <topic>`, the same prompt typed by hand.
    const isAuto = await read($, auto)
    const held = await read($, armed)
    // A sent button of the topic ('' for `queue all`): its buttons read busy and ignore presses.
    const busy = await read($, sent)
    const busyOf = (topic: string) => busy.find(item => item.topic === topic && now < item.untilMs)
    const busyText = (item: Pending) => <Text dimColor>{item.action}… (sent {ago(now - item.sinceMs)})</Text>
    const isArmed = (row: Control, action: Action) =>
      held.topic === row.topic && held.action === action && now < held.untilMs
    const press = (row: Control, action: Action) => async () => {
      const at = await $.clock.now()
      if ((await read($, sent)).some(item => item.topic === row.topic && at < item.untilMs)) return
      const current = await read($, armed)
      if (current.topic === row.topic && current.action === action && at < current.untilMs) {
        await update($, armed, () => NONE)
        await sendButton($, row.topic, action, `wt ${action} ${row.topic}`)
        return
      }
      await update($, armed, () => ({ topic: row.topic, action, untilMs: at + config.confirm_seconds * 1000 }))
      // Draw the armed label at once, not at the next poll.
      await update($, view, old => ({ ...old, nowMs: at }))
    }
    const controlRow = (row: Control) => (
      <Box key={`ctl-${row.topic}`} flexDirection="row" flexWrap="wrap">
        <Text>{row.topic} </Text>
        {busyOf(row.topic) ? (
          busyText(busyOf(row.topic) as Pending)
        ) : row.action ? (
          <Button key={`ctl-${row.topic}-${row.action}`} onPress={press(row, row.action)}>
            <Text color={isArmed(row, row.action) ? 'yellow' : row.action === 'stop' ? 'red' : 'cyan'}>
              {label(row.action, isArmed(row, row.action))}
            </Text>
          </Button>
        ) : (
          <Text dimColor>{row.why}</Text>
        )}
        {row.isHeld && <Text color="yellow"> HOLD (decide the ALERT first)</Text>}
      </Box>
    )
    // Queue buttons, armed like the controls (two presses within confirm_seconds), each sending a typed
    // `wt queue ...` prompt to worker-control.py: `queue` per planned topic, `queue all` in the planned
    // order, `drop` per waiting item. Only the person presses a pane button.
    const isArmedQueue = (topic: string, action: string) =>
      held.topic === topic && held.action === action && now < held.untilMs
    const pressQueue = (topic: string, action: string, text: string) => async () => {
      const at = await $.clock.now()
      if ((await read($, sent)).some(item => item.topic === topic && at < item.untilMs)) return
      const current = await read($, armed)
      if (current.topic === topic && current.action === action && at < current.untilMs) {
        await update($, armed, () => NONE)
        await sendButton($, topic, action, text)
        return
      }
      await update($, armed, () => ({ topic, action, untilMs: at + config.confirm_seconds * 1000 }))
      await update($, view, old => ({ ...old, nowMs: at }))
    }
    const queueButton = (topic: string, action: string, text: string, color: string) =>
      busyOf(topic) ? (
        <Box key={`${action}-${topic || 'all'}`}>{busyText(busyOf(topic) as Pending)}</Box>
      ) : (
      <Button key={`${action}-${topic || 'all'}`} onPress={pressQueue(topic, action, text)}>
        <Text color={isArmedQueue(topic, action) ? 'yellow' : color}>
          {isArmedQueue(topic, action) ? `confirm ${action}?` : action}
        </Text>
      </Button>
      )
    // The topic queue, in order: what each item waits for (with `drop`), its run once started, merged, or
    // the error that stopped it.
    const queueRow = (row: QueueRow, index: number) => (
      <Box key={`qrow-${row.topic}`} flexDirection="row" flexWrap="wrap">
        <Text wrap="truncate-end" color={row.error ? 'red' : row.state === 'merged' ? 'green' : undefined}>
          {index + 1}. {row.topic} ·{' '}
          {row.error
            ? `error: ${row.error}`
            : row.state === 'waiting'
              ? `waiting: ${row.why ?? ''} `
              : `${row.state ?? '?'}${row.run ? ` (run ${row.run})` : ''}`}
        </Text>
        {row.state === 'waiting' && queueButton(row.topic, 'drop', `wt queue drop ${row.topic}`, 'red')}
      </Box>
    )
    // Planned, not queued: one `queue` button each; a broken spec shows its error instead.
    const plannedRow = (row: PlannedRow) => (
      <Box key={`prow-${row.topic}`} flexDirection="row" flexWrap="wrap">
        <Text>
          {row.topic}
          {row.estUsd !== undefined ? ` · ${usd(row.estUsd)}` : ''}
          {row.after && row.after.length > 0 ? ` · after ${row.after.join(', ')}` : ''}{' '}
        </Text>
        {row.error ? <Text color="red">{row.error}</Text> : queueButton(row.topic, 'queue', `wt queue ${row.topic}`, 'cyan')}
      </Box>
    )
    const allText = queueAllText(state.planned ?? [])
    // Refused automatic merges the person can approve: why, what changed, and `merge` (two presses), which
    // sends `wt merge <topic>`: the hook merges in the background and the finish follows by itself. While
    // that approval runs (wt_controls.py "approving") the row says so and has no button.
    const waiting = state.controls.filter(row => row.merge || row.approving)
    const mergeRow = (row: Control) => (
      <Box key={`mrow-${row.topic}`} flexDirection="column">
        <Box flexDirection="row" flexWrap="wrap">
          {row.approving ? (
            <Text dimColor>
              {row.topic}: approving run {row.approving.run} (since {row.approving.since}): tests, then the merge…
            </Text>
          ) : (
            <Box flexDirection="row" flexWrap="wrap">
              <Text color="yellow">{row.topic}: {row.merge?.reason} </Text>
              {queueButton(row.topic, 'merge', `wt merge ${row.topic}`, 'green')}
            </Box>
          )}
        </Box>
        {(row.merge?.files ?? []).length > 0 && <Text dimColor wrap="truncate-end">  {(row.merge?.files ?? []).join(', ')}</Text>}
      </Box>
    )
    // The flow mode switch: two presses within confirm_seconds, like the controls; only the person can
    // press a pane button, so no origin check is needed here (the typed `wt local` checks it).
    const isLocal = await localModeOn($)
    const localArmed = held.topic === '' && held.action === 'local' && now < held.untilMs
    const pressLocal = async () => {
      const at = await $.clock.now()
      const current = await read($, armed)
      if (current.topic === '' && current.action === 'local' && at < current.untilMs) {
        await update($, armed, () => NONE)
        const wasOn = await localModeOn($)
        void $.ui.toast(await switchLocal($, wasOn ? 'off' : 'on', true))
      } else {
        await update($, armed, () => ({ topic: '', action: 'local', untilMs: at + config.confirm_seconds * 1000 }))
      }
      await update($, view, old => ({ ...old, nowMs: at }))
    }
    // The Notices list: every event and refused press, newest first, whole text; opening it clears unread.
    const notices = await read($, noticesAtom)
    const toggleNotices = async () => {
      await update($, noticesAtom, old => ({ ...old, open: !old.open, unread: 0 }))
      redrawBand($)
    }
    const toggleAuto = async () => {
      const next = !(await read($, auto))
      await update($, auto, () => next)
      void $.ui.toast(next ? 'wt auto: on for this session' : 'wt auto: off')
    }

    return (
      <Box flexDirection="column">
        {state.error && <Text color="red">{state.error}</Text>}
        {!state.error && state.workers.length === 0 && <Text dimColor>No worker running.</Text>}
        {state.workers.length > 1 && (
          <Box flexDirection="row" flexWrap="wrap">
            {tabs}
          </Box>
        )}
        {shown && <Box flexDirection="column">{body(shown)}</Box>}
        {!state.error && (
          <Box flexDirection="column">
            {state.controlsError && <Text color="red">{state.controlsError}</Text>}
            <Box flexDirection="row" flexWrap="wrap">
              <Text dimColor>Controls · </Text>
              <Button key="auto" onPress={() => void toggleAuto()}>
                <Text color={isAuto ? 'green' : undefined}>auto: {isAuto ? 'on' : 'off'}</Text>
              </Button>
              <Text dimColor> · </Text>
              <Button key="local" onPress={() => void pressLocal()}>
                <Text color={localArmed ? 'yellow' : isLocal ? 'green' : undefined}>
                  {localArmed ? `confirm local ${isLocal ? 'off' : 'on'}?` : `local: ${isLocal ? 'on' : 'off'}`}
                </Text>
              </Button>
            </Box>
            {state.controls.map(controlRow)}
            {waiting.length > 0 && <Text dimColor>Waiting for you (wt merge)</Text>}
            {waiting.map(mergeRow)}
            {(state.queue ?? []).length > 0 && <Text dimColor>Queue (wt queue)</Text>}
            {(state.queue ?? []).map(queueRow)}
            {(state.planned ?? []).length > 0 && (
              <Box flexDirection="row" flexWrap="wrap">
                <Text dimColor>Planned (plans/queue) </Text>
                {allText && queueButton('', 'queue all', allText, 'cyan')}
              </Box>
            )}
            {(state.planned ?? []).map(plannedRow)}
          </Box>
        )}
        <Box flexDirection="row">
          <Button key="notices" onPress={() => void toggleNotices()}>
            <Text color={notices.unread > 0 ? 'yellow' : 'cyan'}>
              {notices.open ? 'hide notices' : `notices${notices.unread > 0 ? ` (${notices.unread} new)` : ''}`}
            </Text>
          </Button>
        </Box>
        {notices.open && notices.items.length === 0 && <Text dimColor>No notices yet.</Text>}
        {notices.open &&
          notices.items.map((item, index) => (
            <Text key={`notice-${index}`} color={item.needs ? 'yellow' : undefined}>
              {noticeLine(item)}
            </Text>
          ))}
      </Box>
    )
  })
}

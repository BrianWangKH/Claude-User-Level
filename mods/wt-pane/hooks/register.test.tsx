import { expect, test } from 'claude-code/testing'

import type { View } from '../types'

const PANE = {
  plugin: 'wt-pane',
  component: 'Pane',
  requestId: 'wt-pane',
  props: {
    title: 'Workers',
    isFocused: false,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 20 },
    view: {},
  },
} as const

const NOW = Date.parse('2026-10-30T10:12:00+08:00')

const VIEW: View = {
  nowMs: NOW,
  tick: 0,
  controls: [],
  workers: [
    {
      topic: 'clean',
      run: 2,
      phase: 'running',
      startedMs: NOW - 12 * 60000,
      estMinutes: 40,
      capUsd: 4,
      compacts: 1,
      usdRun: 0.75,
      unpriced: 0,
      done: [{ type: 'qa-planner', count: 1 }],
      agents: [{ id: 'q1', type: 'qa', startedMs: NOW - 60000, budgetMinutes: 20, usdRun: 0.25, tool: 'Edit', what: 'test_x.py', lastMs: NOW - 5000 }],
      orchestrator: { tool: 'Agent', what: 'qa: Write tests', lastMs: NOW - 60000 },
    },
  ],
}

// Stands for the engine beneath the mod: every `$` call it makes is answered `{ value }`.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function engine(on: any, panes: { id: string }[]) {
  const calls = { opened: 0, closed: 0 }
  on('ui.panes', () => ({ value: panes }))
  on('ui.open', () => {
    calls.opened += 1
    return { value: { isPlaced: true } }
  })
  on('ui.close', () => {
    calls.closed += 1
    return { value: undefined }
  })
  on('process.run', () => ({ value: { exitCode: 0, stdout: '/m/.git\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  on('fs.exists', () => ({ value: false }))
  on('fs.list', () => ({ value: [] }))
  on('fs.read', () => ({ value: '{}' }))
  on('clock.now', () => ({ value: NOW }))
  on('clock.every', () => ({ value: { cancel: () => {} } }))
  on('env.get', () => ({ value: undefined }))
  on('ui.invalidate', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  return calls
}

test('`wt pane` opens the pane and the prompt never enters the session', async ($, on) => {
  const calls = engine(on, [])
  let entered = 0
  on('prompt.submit', (_, e) => {
    entered += 1
    return { text: e.text }
  })

  const result = await $.prompt.submit({ text: 'wt pane' })

  expect(calls.opened).toBe(1)
  expect(entered).toBe(0)
  expect(result.drop).toBe('wt pane: opened')
})

test('`wt pane` closes an open pane', async ($, on) => {
  const calls = engine(on, [{ id: 'wt-pane' }])

  const result = await $.prompt.submit({ text: '  wt   pane ' })

  expect(calls.closed).toBe(1)
  expect(result.drop).toBe('wt pane: closed')
})

test('other prompts, `wt launch` included, pass through untouched', async ($, on) => {
  engine(on, [])
  const seen: string[] = []
  on('prompt.submit', (_, e) => {
    seen.push(e.text)
    return { text: e.text }
  })

  await $.prompt.submit({ text: 'wt launch clean' })
  await $.prompt.submit({ text: 'show me wt pane please' })

  expect(seen).toEqual(['wt launch clean', 'show me wt pane please'])
})

// Every Text the drawing shows, one per line: a failed match prints what was drawn.
async function texts(ui: { findAll: (query: { type: string }) => Promise<{ text: string }[]> }): Promise<string> {
  return (await ui.findAll({ type: 'Text' })).map(found => found.text).join('\n')
}

test('without a Raster (desktop) the pane draws each worker, its steps and what it does as text', async ($, on) => {
  on('state.get', () => ({ value: { value: { shape: 'view-3', value: VIEW }, version: 1 } }))

  for (const surface of ['desktop', 'vscode'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const drawn = await texts(ui)
    expect(drawn).toMatch(/clean · run 2 · 12m\/40m · \$0\.75\/\$4\.00/)
    expect(drawn).toMatch(/qa-planner ✓ → qa ▶/)
    expect(drawn).toMatch(/▶ qa 1m\/20m \$0\.25 · Edit test_x\.py \(5s ago\)/)
    expect(drawn).toMatch(/◆ orchestrator · Agent qa: Write tests/)
    await ui.unmount()
  }
})

test('a subagent held by a hook shows its own time and how long it has waited, on every surface', async ($, on) => {
  // The reviewer worked 15 min, then the mutation scan held its hand-back for 17 min (limit 55).
  const held = {
    id: 'c1', type: 'code-reviewer', startedMs: NOW - 32 * 60000, budgetMinutes: 11, usdRun: 0.69,
    waitSinceMs: NOW - 17 * 60000, waitWhat: 'mutation scan', waitMaxMinutes: 55, tool: 'Bash', what: 'pytest', lastMs: NOW - 18 * 60000,
  }
  const view = { ...VIEW, workers: [{ ...VIEW.workers[0], agents: [held] }] }
  on('state.get', () => ({ value: { value: { shape: 'view-3', value: view }, version: 1 } }))

  const desktop = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(desktop)).toMatch(/▶ code-reviewer 15m\/11m \$0\.69 · waiting for mutation scan 17m\/55m/)
  await desktop.unmount()
  const terminal = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await texts(terminal)).toMatch(/15m\/11m {2}\$0\.69 {2}waiting for mutation scan 17m\/55m/)
  await terminal.unmount()
})

test('a worker without an end that is not running by its phase is drawn as such, without the office', async ($, on) => {
  const dead = { ...VIEW.workers[0], phase: 'orphan' as const }
  on('state.get', () => ({ value: { value: { shape: 'view-3', value: { ...VIEW, workers: [dead] } }, version: 1 } }))

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.findAll({ type: 'Raster' })).length).toBe(0)
  const drawn = await texts(ui)
  expect(drawn).toMatch(/\? clean · run 2 · orphan/)
  expect(drawn).not.toMatch(/◆ orchestrator/)
  await ui.unmount()
})

test('with no worker the pane says so', async ($, on) => {
  on('state.get', () => ({ value: { value: { shape: 'view-3', value: { workers: [], controls: [], nowMs: NOW, tick: 0 } }, version: 1 } }))

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text' }))?.text).toBe('No worker running.')
  await ui.unmount()
})

test('the terminal draws the header and the office, no text rows; desktop draws text only', async ($, on) => {
  on('state.get', () => ({ value: { value: { shape: 'view-3', value: VIEW }, version: 1 } }))

  const terminal = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await terminal.findAll({ type: 'Raster' })).length).toBe(1)
  const onTerminal = await texts(terminal)
  expect(onTerminal).toMatch(/clean · run 2/)
  // Under the office: each working subagent's time and spend against its budget.
  expect(onTerminal).toMatch(/▶ qa  1m\/20m  \$0\.25/)
  expect(onTerminal).not.toMatch(/◆ orchestrator/)
  await terminal.unmount()

  const desktop = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await desktop.findAll({ type: 'Raster' })).length).toBe(0)
  expect(await texts(desktop)).toMatch(/◆ orchestrator/)
  await desktop.unmount()
})

// Stands for the host's session values: what the mod sets is what it reads back.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function values(on: any, initial: Record<string, unknown>) {
  const held = new Map<string, unknown>(Object.entries(initial))
  let version = 1
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  on('state.get', (_: unknown, e: any) =>
    ({ value: held.has(e.key) ? { value: held.get(e.key), version } : { value: undefined, version: 0 } }))
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  on('state.set', (_: unknown, e: any) => {
    held.set(e.key, e.value)
    version += 1
    return { value: { isSet: true, version } }
  })
  // The flow mode the pane's local switch shows: unset (off) unless a test sets it.
  on('env.get', () => ({ value: undefined }))
  return held
}

// A plugin's own prompt is sent without awaiting it: let it reach the engine below.
async function settle() {
  for (let i = 0; i < 50; i += 1) await Promise.resolve()
}

const CONTROLLED: View = {
  workers: [],
  nowMs: NOW,
  tick: 0,
  controls: [
    { topic: 'fresh', action: 'launch', isHeld: false },
    { topic: 'held', action: 'resume', run: 1, isHeld: true },
    { topic: 'old', why: 'no estimate yet (plans/worker/old.json)', isHeld: false },
  ],
  flowMode: 'origin',
}

test('the pane draws a control row per topic, with the auto switch, on every surface', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: CONTROLLED } })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const drawn = await texts(ui)
    expect(drawn).toMatch(/auto: off/)
    expect(drawn).toMatch(/local: off/)
    expect(drawn).toMatch(/^launch$/m)
    expect(drawn).toMatch(/^resume$/m)
    expect(drawn).toMatch(/HOLD/)
    expect(drawn).toMatch(/no estimate yet \(plans\/worker\/old\.json\)/)
    expect((await ui.findAll({ type: 'Button' })).length).toBe(5) // auto, local, launch, resume, notices
    await ui.unmount()
  }
})

test('under each control row the run line shows, an orphan with its to-do, without another button', async ($, on) => {
  const controls = [
    {
      topic: 'dead-t', action: 'stop' as const, run: 1, isHeld: false, phase: 'orphan' as const,
      human: { line: 'dead-t · run 1 may have died: no sign of life since 05:41', todo: { text: 'check its process with wt status dead-t', command: 'wt status dead-t' } },
    },
    { topic: 'odd-t', why: 'no estimate yet (plans/worker/odd-t.json)', isHeld: false, statusError: 'run status unreadable: boom' },
    { topic: 'old-t', action: 'launch' as const, isHeld: false },
  ]
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, controls } } })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const drawn = await texts(ui)
    expect(drawn).toMatch(/dead-t · run 1 may have died: no sign of life since 05:41 → check its process with wt status dead-t/)
    expect(drawn).toMatch(/run status unreadable: boom/)
    expect(drawn).toMatch(/^stop$/m) // the row's own action, as wt_controls.py printed it
    expect((await ui.findAll({ type: 'Button' })).length).toBe(5) // auto, local, stop, launch, notices
    await ui.unmount()
  }
})

test('a control submits `wt <action> <topic>` only on the second press', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: CONTROLLED } })
  on('clock.now', () => ({ value: NOW }))
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })

  const first = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await first.press({ key: 'ctl-fresh-launch' })
  await settle()
  expect(submitted).toEqual([])
  await first.unmount()

  // values() stands in for the host's state, so a set does not redraw a mounted drawing:
  // draw again to read what the first press stored.
  const armedUi = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(armedUi)).toMatch(/^confirm launch\?$/m)
  await armedUi.press({ key: 'ctl-fresh-launch' })
  await settle()
  expect(submitted).toEqual(['wt launch fresh'])
  await armedUi.unmount()
})

test('an armed control expires: a late second press only arms it again', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: CONTROLLED } })
  let now = NOW
  on('clock.now', () => ({ value: now }))
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'ctl-fresh-launch' })
  now += 6000 // past confirm_seconds (5)
  await ui.press({ key: 'ctl-fresh-launch' })
  await settle()
  expect(submitted).toEqual([])
  await ui.unmount()

  // The late press armed it again, from its own time.
  const again = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(again)).toMatch(/^confirm launch\?$/m)
  await again.unmount()
})

test('`wt auto` shows the switch and never enters the session', async ($, on) => {
  values(on, {})
  let entered = 0
  on('prompt.submit', (_, e) => {
    entered += 1
    return { text: e.text }
  })

  const result = await $.prompt.submit({ text: 'wt auto' })

  expect(result.drop).toBe('wt auto: off (wt auto on|off)')
  expect(entered).toBe(0)
})

test('`wt auto on` from anything but the person at the prompt is refused', async ($, on) => {
  const held = values(on, {})
  on('prompt.submit', (_, e) => ({ text: e.text }))

  // A test's submission is a plugin's, not the person's Enter.
  const result = await $.prompt.submit({ text: 'wt auto on' })

  expect(result.drop).toMatch(/only the person/)
  expect(held.has('auto')).toBe(false)
})

test('a pane too narrow for every desk draws the text rows and says how wide the office needs', async ($, on) => {
  on('state.get', () => ({ value: { value: { shape: 'view-3', value: VIEW }, version: 1 } }))

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns: 40 } })
  expect((await ui.findAll({ type: 'Raster' })).length).toBe(0)
  const drawn = await texts(ui)
  expect(drawn).toMatch(/office needs 60 columns \(pane has 40\)/)
  expect(drawn).toMatch(/◆ orchestrator/)
  await ui.unmount()
})

test('the header shows the estimate and where the run stops', async ($, on) => {
  const row = { ...VIEW.workers[0], estUsd: 2, capUsd: 2.5, capMinutes: 50 }
  on('state.get', () => ({ value: { value: { shape: 'view-3', value: { ...VIEW, workers: [row] } }, version: 1 } }))

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(ui)).toMatch(/12m\/40m \(stop 50m\) · \$0\.75\/\$2\.00 \(stop \$2\.50\)/)
  await ui.unmount()
})

test('the local switch shows the flow mode wt_controls.py reported', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, flowMode: 'local' } } })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(ui)).toMatch(/local: on/)
  await ui.unmount()
})

test('the Controls line shows config mode DEV as wt_controls.py reported it', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, configMode: 'DEV' } } })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(ui)).toMatch(/config: DEV/)
  await ui.unmount()
})

test('the Controls line shows config mode TEST as wt_controls.py reported it', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, configMode: 'TEST' } } })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(ui)).toMatch(/config: TEST/)
  await ui.unmount()
})

test('an unreadable config mode shows its error, never a guessed DEV', async ($, on) => {
  const configModeError = "config mode unreadable: <home>/config/local.test.json: missing"
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, configModeError } } })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(ui)
  expect(drawn).toMatch(/config: error/)
  expect(drawn).toMatch(/local\.test\.json: missing/)
  expect(drawn).not.toMatch(/config: DEV/)
  await ui.unmount()
})

test('an unreadable flow mode shows its error, never local: off', async ($, on) => {
  const flowModeError = "flow mode unreadable: environment variable FLOW_INTEGRATION='Local' is not allowed"
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, flowMode: undefined, flowModeError } } })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(ui)
  expect(drawn).toMatch(/local: error/)
  expect(drawn).toMatch(/FLOW_INTEGRATION='Local' is not allowed/)
  expect(drawn).not.toMatch(/local: off/)
  await ui.unmount()
})

test('when wt_controls.py fails the pane says so above the controls instead of showing none', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, controls: [], controlsError: 'wt_controls.py failed (exit 1): boom' } } })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(ui)).toMatch(/wt_controls\.py failed \(exit 1\): boom/)
  await ui.unmount()
})


test('the queue is drawn under the controls, in order, with what each waits for', async ($, on) => {
  const queue = [
    { topic: 'first', state: 'started', run: 1 },
    { topic: 'second', state: 'waiting', why: 'waits for first to be merged into the local main' },
    { topic: 'third', error: 'new-worktree.sh: FAIL' },
  ]
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, queue } } })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(ui)
  expect(drawn).toMatch(/Queue \(wt queue\)/)
  expect(drawn).toMatch(/1\. first · started \(run 1\)/)
  expect(drawn).toMatch(/2\. second · waiting: waits for first/)
  expect(drawn).toMatch(/3\. third · error: new-worktree\.sh: FAIL/)
  await ui.unmount()
})

const PLANNED = [
  { topic: 'zeta-t', order: 1, estUsd: 1.5 },
  { topic: 'beta-t', order: 2, after: ['zeta-t'], estUsd: 2 },
  { topic: 'bad-t', error: 'no plans/queue/bad-t.md' },
]

test('planned topics get a queue button each and one for all, a broken spec its error', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, planned: PLANNED } } })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(ui)
  expect(drawn).toMatch(/Planned \(plans\/queue\)/)
  expect(drawn).toMatch(/beta-t · \$2\.00 · after zeta-t/)
  expect(drawn).toMatch(/no plans\/queue\/bad-t\.md/)
  expect(drawn).toMatch(/^queue all$/m)
  await ui.unmount()
})

test('queue all sends the planned order on the second press only', async ($, on) => {
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, planned: PLANNED } } })
  on('clock.now', () => ({ value: NOW }))
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })

  const first = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await first.press({ key: 'queue all-all' })
  await settle()
  expect(submitted).toEqual([])
  await first.unmount()
  const second = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await second.press({ key: 'queue all-all' })
  await settle()
  expect(submitted).toEqual(['wt queue zeta-t beta-t'])
  await second.unmount()
})

test('a waiting queue item has a drop button that sends wt queue drop', async ($, on) => {
  const queue = [{ topic: 'beta-t', state: 'waiting', why: 'waits for zeta-t to be merged into the local main' }]
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, queue } } })
  on('clock.now', () => ({ value: NOW }))
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })

  const first = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await first.press({ key: 'drop-beta-t' })
  await first.unmount()
  const second = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await second.press({ key: 'drop-beta-t' })
  await settle()
  expect(submitted).toEqual(['wt queue drop beta-t'])
  await second.unmount()
})

const BAND = { plugin: 'wt-pane', component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 80, scroll: { offset: 0, bodyRows: 10 } } } as const

// The band's engine: values() answers state and env; this adds the rest once (engine() would register
// env.get a second time, which the test harness refuses).
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function bandEngine(on: any, panes: { id: string }[], initial: Record<string, unknown>) {
  values(on, initial)
  const calls = { opened: 0, closed: 0 }
  on('ui.panes', () => ({ value: panes }))
  on('ui.open', () => {
    calls.opened += 1
    return { value: { isPlaced: true } }
  })
  on('ui.close', () => {
    calls.closed += 1
    return { value: undefined }
  })
  on('ui.invalidate', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '/m/.git\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  on('fs.exists', () => ({ value: false }))
  on('fs.list', () => ({ value: [] }))
  on('fs.read', () => ({ value: '{}' }))
  on('clock.now', () => ({ value: NOW }))
  on('clock.every', () => ({ value: { cancel: () => {} } }))
  return calls
}

test('the band above the prompt shows a wt pane button that opens the pane', async ($, on) => {
  const calls = bandEngine(on, [], {})
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await texts(ui)).toMatch(/^wt pane$/m)
  await ui.press({ key: 'pane-toggle' })
  await settle()
  expect(calls.opened).toBe(1)
  await ui.unmount()
})

test('with the pane open the band offers to close it', async ($, on) => {
  const calls = bandEngine(on, [{ id: 'wt-pane' }], { view: { shape: 'view-3', value: { workers: [], controls: [], nowMs: NOW, tick: 0 } } })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await texts(ui)).toMatch(/^close wt pane$/m)
  await ui.press({ key: 'pane-toggle' })
  await settle()
  expect(calls.closed).toBe(1)
  await ui.unmount()
})

test('the band counts running workers by their phase, not by a missing exit file', async ($, on) => {
  // Neither row has an exit file; only the one whose phase (wt_controls.py, hooks/lib/runstate.py) is running counts.
  const dead = { ...VIEW.workers[0], topic: 'dead', phase: 'orphan' as const }
  bandEngine(on, [{ id: 'wt-pane' }], { view: { shape: 'view-3', value: { ...VIEW, workers: [VIEW.workers[0], dead] } } })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await texts(ui)).toMatch(/▶ 1 running/)
  await ui.unmount()
})

test('the band yields to a survey', async ($, on) => {
  bandEngine(on, [], {})
  // Nothing draws beneath the plugin in a test: this hook stands for the engine (the survey's drawing).
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>survey</Text>
  })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, hasSurvey: true } })
  const drawn = await texts(ui)
  expect(drawn).toMatch(/^survey$/m)
  expect(drawn).not.toMatch(/wt pane/)
  await ui.unmount()
})

test('a refused merge waits for the person with its reason, files and a merge button', async ($, on) => {
  const controls = [{ topic: 'held-t', why: 'done', isHeld: false, merge: { reason: 'a person must review these paths: .claude/x', files: ['.claude/x'] } }]
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, controls } } })
  on('clock.now', () => ({ value: NOW }))
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })

  const first = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(first)
  expect(drawn).toMatch(/Waiting for you/)
  expect(drawn).toMatch(/held-t: a person must review these paths: \.claude\/x/)
  await first.press({ key: 'merge-held-t' })
  await first.unmount()
  const second = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await second.press({ key: 'merge-held-t' })
  await settle()
  expect(submitted).toEqual(['wt merge held-t'])
  await second.unmount()
})


test('a sent merge turns its topic busy and ignores presses until the outcome shows', async ($, on) => {
  const controls = [{ topic: 'held-t', why: 'done', isHeld: false, merge: { reason: 'a person must review these paths: .claude/x', files: [] } }]
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, controls } } })
  on('clock.now', () => ({ value: NOW }))
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { drop: 'wt: merge: approving held-t run 1 (refused: review); the tests run first' }
  })
  for (let press = 0; press < 2; press += 1) {
    const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
    await ui.press({ key: 'merge-held-t' })
    await settle()
    await ui.unmount()
  }
  expect(submitted).toEqual(['wt merge held-t'])
  const busy = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(busy)
  expect(drawn).toMatch(/merge… \(sent 0s ago\)/)
  expect(drawn).toMatch(/notices \(1 new\)/)
  // No merge button while it waits: a press has nothing to hit.
  expect((await busy.findAll({ type: 'Button' })).some(found => found.key === 'merge-held-t')).toBe(false)
  await busy.unmount()
})

test('a refused press frees its buttons and stays in the Notices list with the reason', async ($, on) => {
  const controls = [{ topic: 'held-t', why: 'done', isHeld: false, merge: { reason: 'review', files: [] } }]
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, controls } } })
  on('clock.now', () => ({ value: NOW }))
  on('prompt.submit', () => ({ drop: 'wt: merge: held-t run 1 is still going' }))
  for (let press = 0; press < 2; press += 1) {
    const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
    await ui.press({ key: 'merge-held-t' })
    await settle()
    await ui.unmount()
  }
  const after = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(after)).toMatch(/^merge$/m)
  await after.press({ key: 'notices' })
  await settle()
  await after.unmount()
  const opened = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(opened)
  expect(drawn).toMatch(/hide notices/)
  expect(drawn).toMatch(/! held-t not-done: wt merge held-t: wt: merge: held-t run 1 is still going/)
  await opened.unmount()
})

test('the Notices list shows each event whole, newest first, and opening it clears the count', async ($, on) => {
  const long = 'check: merge-<run>.json doesn\'t say merged; nothing done (finish.json) ' + 'x'.repeat(150)
  values(on, {
    view: { shape: 'view-3', value: CONTROLLED },
    notices: {
      shape: 'notices-1',
      value: {
        items: [
          { t: '2026-11-02T02:14:43', kind: 'refused', topic: 'b', text: 'no automatic merge run', needs: true },
          { t: '2026-11-02T02:13:26', kind: 'finish-failed', topic: 'a', text: long, needs: true },
        ],
        unread: 2,
        open: false,
      },
    },
  })
  const closed = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(closed)).toMatch(/notices \(2 new\)/)
  await closed.press({ key: 'notices' })
  await settle()
  await closed.unmount()
  const open = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const drawn = await texts(open)
  expect(drawn.indexOf('02:14 ! b refused')).toBeLessThan(drawn.indexOf('02:13 ! a finish-failed'))
  expect(drawn).toMatch(new RegExp('x{150}'))
  expect(drawn).not.toMatch(/new\)/)
  await open.unmount()
})

test('an approval in progress shows its run and time and no merge button, so nothing can be pressed twice', async ($, on) => {
  const controls = [{ topic: 'held-t', why: 'done', isHeld: false, approving: { run: 2, since: '07:45' } }]
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, controls } } })
  on('clock.now', () => ({ value: NOW }))
  const submitted: string[] = []
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await texts(ui)).toMatch(/held-t: approving run 2 \(since 07:45\): tests, then the merge…/)
  expect((await ui.findAll({ type: 'Button' })).some(found => found.key === 'merge-held-t')).toBe(false)
  await ui.unmount()
  expect(submitted).toEqual([])
})

// The local switch (switchLocal): wt_controls.py's answers stand for the hooks (the mode it reports; with
// --local, the check), and every write and toast is kept. The main checkout is /m (git's common dir /m/.git).
type LocalSeen = { runs: string[][]; writes: [string, string][]; toasts: string[] }
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function localEngine(on: any, mode: 'origin' | 'local', check: { exitCode?: number; blocker?: string } = {}): LocalSeen {
  values(on, { view: { shape: 'view-3', value: { ...CONTROLLED, flowMode: mode } } })
  on('clock.now', () => ({ value: NOW }))
  const seen: LocalSeen = { runs: [], writes: [], toasts: [] }
  const answer = (stdout: string, exitCode = 0) =>
    ({ value: { exitCode, stdout, stderr: exitCode === 0 ? '' : 'boom', isStdoutTruncated: false, isStderrTruncated: false } })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  on('process.run', (_: unknown, e: any) => {
    const argv = [...e.argv] as string[]
    seen.runs.push(argv)
    if (argv[0] === 'git') return answer('/m/.git\n')
    const checking = argv.includes('--local')
    if (checking && check.exitCode) return answer('', check.exitCode)
    const blocker = checking && check.blocker ? { local_blocker: check.blocker } : {}
    return answer(JSON.stringify({ schema: 1, controls: [], flow_mode: mode, ...blocker }))
  })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  on('fs.write', (_: unknown, e: any) => {
    seen.writes.push([e.path, e.text])
    return { value: undefined }
  })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  on('ui.toast', (_: unknown, e: any) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  return seen
}

// The pane's `local` button pressed twice (a press is the person's own act): the first arms it, the second
// switches; the switch is not awaited by the press, so wait for its toast.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function pressLocalTwice($: any, seen: LocalSeen): Promise<void> {
  for (let press = 0; press < 2; press += 1) {
    const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
    await ui.press({ key: 'local' })
    await settle()
    if (press === 0) expect(seen.writes).toEqual([])
    for (let wait = 0; press === 1 && wait < 40 && seen.toasts.length === 0; wait += 1) await settle()
    await ui.unmount()
  }
}

test('the local button writes local to the main checkout record on the second press only', async ($, on) => {
  const seen = localEngine(on, 'origin')
  await pressLocalTwice($, seen)
  expect(seen.writes).toEqual([['/m/.hook-only/flow-mode', 'local\n']])
  expect(seen.runs.some(argv => argv[0] === 'python3' && argv.slice(-3).join(' ') === '/m --local on')).toBe(true)
  expect(seen.toasts).toEqual(['wt local: on — new topics start from and merge into the local main; running topics keep their mode'])
})

test('switching off writes origin to the main checkout record', async ($, on) => {
  const seen = localEngine(on, 'local')
  await pressLocalTwice($, seen)
  expect(seen.writes).toEqual([['/m/.hook-only/flow-mode', 'origin\n']])
  expect(seen.runs.some(argv => argv[0] === 'python3' && argv.slice(-3).join(' ') === '/m --local off')).toBe(true)
  expect(seen.toasts[0]).toMatch(/^wt local: off/)
})

test('a blocker from wt_controls.py --local leaves the record unwritten and says why', async ($, on) => {
  const seen = localEngine(on, 'origin', { blocker: 'the local main is 2 commits behind origin/main' })
  await pressLocalTwice($, seen)
  expect(seen.writes).toEqual([])
  expect(seen.toasts).toEqual(['wt local: not switched: the local main is 2 commits behind origin/main'])
})

test('a check that fails leaves the record unwritten', async ($, on) => {
  const seen = localEngine(on, 'origin', { exitCode: 1 })
  await pressLocalTwice($, seen)
  expect(seen.writes).toEqual([])
  expect(seen.toasts[0]).toMatch(/^wt local: not switched: can't check \(wt_controls\.py failed \(exit 1\)/)
})

test('`wt local on` typed by anything but the person is refused before any check or write', async ($, on) => {
  const seen = localEngine(on, 'origin')
  let entered = 0
  on('prompt.submit', (_, e) => {
    entered += 1
    return { text: e.text }
  })

  // A test's submission is a plugin's, not the person's Enter.
  const result = await $.prompt.submit({ text: 'wt local on' })

  expect(result.drop).toBe('wt local: only the person typing at the prompt can switch it')
  expect(entered).toBe(0)
  expect(seen.runs).toEqual([])
  expect(seen.writes).toEqual([])
})

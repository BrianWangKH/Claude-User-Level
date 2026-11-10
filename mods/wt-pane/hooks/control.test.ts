import { expect, test } from 'claude-code/testing'

import {
  addNotices,
  autoCommand,
  autoStep,
  isPersonal,
  localCommand,
  markOf,
  marksOf,
  needsYou,
  newEvents,
  noticeLine,
  noticeOf,
  parseControls,
  queueAllText,
  queueLimitsText,
  runLine,
  sentOutcome,
  settle,
  toastMs,
  toastOf,
  workerFileTopic,
} from './control'

const OUTPUT = JSON.stringify({
  schema: 1,
  controls: [
    { topic: 'fresh', action: 'launch', held: false, auto: { ok: true } },
    { topic: 'held', action: 'resume', run: 1, held: true, auto: { ok: false, why: 'HOLD' } },
    { topic: 'done', why: 'done (STATUS done); nothing to resume', run: 3, held: false, auto: { ok: false, why: 'done' } },
    { topic: 'Bad Name', action: 'launch', held: false },
    { topic: 'odd', action: 'explode', held: false },
  ],
  local_blocker: 'topic(s) planned but not launched (x) started from the other mode\'s base; launch or remove them first',
  flow_mode: 'local',
})

test('the rows wt_controls.py prints become controls; bad names and unknown actions are dropped', () => {
  const parsed = parseControls(OUTPUT)
  expect(parsed.error).toBe(undefined)
  expect(parsed.controls.map(row => row.topic)).toEqual(['fresh', 'held', 'done', 'odd'])
  expect(parsed.controls[0]).toEqual({ topic: 'fresh', action: 'launch', why: undefined, run: undefined, isHeld: false, auto: { ok: true, why: undefined } })
  expect(parsed.controls[1].isHeld).toBe(true)
  expect(parsed.controls[2].action).toBe(undefined)
  expect(parsed.controls[3].action).toBe(undefined) // 'explode' is no action
  expect(parsed.localBlocker).toMatch(/^topic\(s\) planned but not launched \(x\)/)
  expect(parsed.flowMode).toBe('local')
  expect(parsed.flowModeError).toBe(undefined)
})

test('a kept wrap-up row parses as the wrapup action with its line and to-do; auto never sends it', () => {
  const kept = {
    topic: 'kept-t', action: 'wrapup', run: 2, held: false,
    auto: { ok: false, why: "run 2's wrap-up is kept for the person: wt wrapup" }, phase: 'orphan',
    human: { schema: 1, topic: 'kept-t', run: 2, phase: 'orphan',
      line: 'kept-t · run 2 finished its work (STATUS done), but its wrapper ended before the wrap-up; kept for later: merge, next, finish, queue_tick',
      todo: { text: 'type wt wrapup kept-t (or press the button) to finish the wrap-up', command: 'wt wrapup kept-t', button: 'wrapup' } },
  }
  const [row] = parseControls(JSON.stringify({ schema: 1, controls: [kept], flow_mode: 'local' })).controls
  expect(row.action).toBe('wrapup')
  expect(row.auto).toEqual({ ok: false, why: kept.auto.why })
  expect(runLine(row)).toEqual({ text: `${kept.human.line} → ${kept.human.todo.text}`, color: 'yellow' })
})

test('the run display fields are read when present and an older output parses exactly as before', () => {
  const orphan = {
    topic: 'dead-t', action: 'stop', run: 1, held: false, auto: { ok: false, why: 'already running (run 1)' },
    phase: 'orphan',
    human: { schema: 1, topic: 'dead-t', run: 1, phase: 'orphan', line: 'dead-t · run 1 may have died: no sign of life since 05:41',
      todo: { text: 'check its process with wt status dead-t, then ask in the planning session what to do', command: 'wt status dead-t', button: null } },
  }
  const odd = { topic: 'odd-t', held: false, phase: 'sleeping', human: { line: 7 }, status_error: 'run status unreadable: boom' }
  const [dead, broken] = parseControls(JSON.stringify({ schema: 1, controls: [orphan, odd], flow_mode: 'local' })).controls
  expect(dead.action).toBe('stop') // the decision is still the row's own
  expect(dead.phase).toBe('orphan')
  expect(dead.human).toEqual({ line: orphan.human.line, todo: { text: orphan.human.todo.text, command: 'wt status dead-t' } })
  expect(broken.phase).toBe(undefined)
  expect(broken.human).toBe(undefined)
  expect(broken.statusError).toBe('run status unreadable: boom')
  // schema 1 without the display fields (hooks before migration step 3): the same row as before
  expect(parseControls(OUTPUT).controls[0]).toEqual({ topic: 'fresh', action: 'launch', why: undefined, run: undefined, isHeld: false, auto: { ok: true, why: undefined } })
  expect('phase' in parseControls(OUTPUT).controls[0]).toBe(false)
})

test('the line under a row: the run line and its to-do, yellow for an orphan, red for unknown or an error', () => {
  const base = { topic: 't', isHeld: false }
  expect(runLine(base)).toBe(undefined)
  expect(runLine({ ...base, phase: 'running', human: { line: 't · run 1 running', todo: { command: 'wt stop t' } } }))
    .toEqual({ text: 't · run 1 running', color: undefined })
  expect(runLine({ ...base, phase: 'orphan', human: { line: 't · run 1 may have died', todo: { text: 'wt resume checks first', command: 'wt resume t' } } }))
    .toEqual({ text: 't · run 1 may have died → wt resume checks first', color: 'yellow' })
  expect(runLine({ ...base, phase: 'unknown', human: { line: 't · run 2 state unreadable: bad' } }))
    .toEqual({ text: 't · run 2 state unreadable: bad', color: 'red' })
  expect(runLine({ ...base, statusError: 'run status unreadable: boom' })).toEqual({ text: 'run status unreadable: boom', color: 'red' })
})

test('the flow mode comes only from wt_controls.py; its error, or no mode, is an error, never off', () => {
  const failed = parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: null, flow_mode_error: 'flow mode unreadable: bad' }))
  expect(failed.flowMode).toBe(undefined)
  expect(failed.flowModeError).toBe('flow mode unreadable: bad')
  expect(parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'Local' })).flowModeError).toMatch(/gave no flow mode/)
  expect(parseControls(JSON.stringify({ schema: 1, controls: [] })).flowModeError).toMatch(/gave no flow mode/)
})

test('the config mode comes only from wt_controls.py; its error, or no mode, is an error, never DEV', () => {
  expect(parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local', config_mode: 'TEST' })).configMode).toBe('TEST')
  const failed = parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local', config_mode: null, config_mode_error: 'config mode unreadable: x' }))
  expect(failed.configMode).toBe(undefined)
  expect(failed.configModeError).toBe('config mode unreadable: x')
  expect(parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local', config_mode: 'dev' })).configModeError).toMatch(/gave no config mode/)
  expect(parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local' })).configModeError).toMatch(/gave no config mode/)
})

test('output that is not JSON or not schema 1 is an error, never an empty list', () => {
  expect(parseControls('Traceback (most recent call last)').error).toMatch(/printed no JSON: Traceback/)
  expect(parseControls(JSON.stringify({ schema: 2, controls: [] })).error).toMatch(/not schema 1/)
  expect(parseControls(JSON.stringify({ controls: [] })).error).toMatch(/not schema 1/)
})

test('a run that can not see plans/ is an error while topics exist', () => {
  const hidden = JSON.stringify({ schema: 1, plans_visible: false, controls: [{ topic: 'a', why: 'no estimate yet', held: false }] })
  expect(parseControls(hidden).error).toMatch(/can't see the main checkout's plans/)
  expect(parseControls(JSON.stringify({ schema: 1, plans_visible: false, controls: [] })).error).toBe(undefined)
})

test('auto submits only what wt_controls.py allows, and says why otherwise', () => {
  const [fresh, held, done] = parseControls(OUTPUT).controls
  expect(autoStep('fresh', fresh).submit).toBe('wt launch fresh')
  expect(autoStep('held', held).submit).toBe(undefined)
  expect(autoStep('held', held).note).toMatch(/nothing started for held: HOLD/)
  expect(autoStep('done', done).note).toMatch(/nothing started for done: done/)
  expect(autoStep('gone', undefined).note).toMatch(/no worktree/)
})

test('workerFileTopic reads the topic of an estimate file under the main checkout only', () => {
  expect(workerFileTopic('/r/main/plans/worker/fresh.json', '/r/main')).toBe('fresh')
  expect(workerFileTopic('/r/main/plans/fresh.md', '/r/main')).toBe(undefined)
  expect(workerFileTopic('/r/main/plans/worker/Bad.json', '/r/main')).toBe(undefined)
})

test('wt auto and wt local commands', () => {
  expect(autoCommand('wt auto')).toBe('show')
  expect(autoCommand(' wt auto on ')).toBe('on')
  expect(autoCommand('wt auto maybe')).toBe(undefined)
  expect(localCommand('wt local off')).toBe('off')
  expect(localCommand('wt launch x')).toBe(undefined)
})

test('only the person at the prompt is personal', () => {
  expect(isPersonal({ kind: 'composer' })).toBe(true)
  expect(isPersonal({ kind: 'bridge' })).toBe(true)
  expect(isPersonal({ kind: 'plugin' })).toBe(false)
  expect(isPersonal(undefined)).toBe(false)
})

test('the queue rows come with the controls; unknown states are dropped to none', () => {
  const parsed = parseControls(JSON.stringify({
    schema: 1,
    controls: [],
    queue: [
      { topic: 'a', state: 'started', run: 1 },
      { topic: 'b', state: 'waiting', why: 'waits for a to be merged into the local main' },
      { topic: 'c', error: 'new-worktree.sh: FAIL' },
      { topic: 'd', state: 'exploded' },
    ],
  }))
  expect(parsed.queue?.map(row => [row.topic, row.state])).toEqual([['a', 'started'], ['b', 'waiting'], ['c', undefined], ['d', undefined]])
  expect(parsed.queue?.[1].why).toMatch(/waits for a/)
  expect(parseControls(JSON.stringify({ schema: 1, controls: [], queue: [], queue_error: 'wt_queue.py status failed: x' })).error).toMatch(/status failed/)
})

test('planned topics come with the controls, and queue all keeps their order and skips broken specs', () => {
  const parsed = parseControls(JSON.stringify({
    schema: 1,
    controls: [],
    planned: [
      { topic: 'zeta-t', order: 1, after: [], est_usd: 1.5 },
      { topic: 'beta-t', order: 2, after: ['zeta-t', 'Bad Name'], est_usd: 2 },
      { topic: 'bad-t', error: 'no plans/queue/bad-t.md' },
      { topic: 'Not A Topic' },
    ],
  }))
  expect(parsed.planned?.map(row => row.topic)).toEqual(['zeta-t', 'beta-t', 'bad-t'])
  expect(parsed.planned?.[1].after).toEqual(['zeta-t'])
  expect(parsed.planned?.[0].estUsd).toBe(1.5)
  expect(queueAllText(parsed.planned ?? [])).toBe('wt queue zeta-t beta-t')
  expect(queueAllText([{ topic: 'bad-t', error: 'x' }])).toBe(undefined)
})

test('a refused merge the person can approve comes with its reason and files', () => {
  const parsed = parseControls(JSON.stringify({ schema: 1, controls: [
    { topic: 'held-t', why: 'done', held: false, merge: { reason: 'a person must review these paths: .claude/x', files: ['.claude/x', 7] } },
    { topic: 'plain-t', action: 'launch', held: false },
  ] }))
  expect(parsed.controls[0].merge).toEqual({ reason: 'a person must review these paths: .claude/x', files: ['.claude/x'] })
  expect(parsed.controls[1].merge).toBe(undefined)
})

test('events become toasts, the ones to act on first; each read once, a partial line waits', () => {
  const log = [
    JSON.stringify({ kind: 'merged', topic: 'a-t', text: 'into main as abc' }),
    JSON.stringify({ kind: 'run-ended', topic: 'b-t', text: 'run 2: STATUS: needs-decision' }),
    '{"kind": "ref',
  ].join('\n')
  const first = newEvents(log, 0)
  expect(first.events.map(toastOf)).toEqual(['a-t merged: into main as abc', 'needs you · b-t run-ended: run 2: STATUS: needs-decision'])
  expect(newEvents(log, first.seen).events).toEqual([])
  expect(newEvents('', first.seen)).toEqual({ events: [], seen: 0 })
  expect(needsYou({ kind: 'run-ended', text: 'run 1: STATUS: done' })).toBe(false)
  expect(needsYou({ kind: 'refused', text: 'x' })).toBe(true)
  expect(needsYou({ kind: 'step-failed', text: 'run 1: finish failed' })).toBe(true)
  expect(needsYou({ kind: 'wrapup-failed', text: 'run 1: OSError' })).toBe(true)
  expect(needsYou({ kind: 'heartbeat-stalled', text: 'run 1: its wrapper runs but its heartbeat is 900 s old' })).toBe(true)
  expect(needsYou({ kind: 'merged', text: 'x' })).toBe(false)
})


test('a pressed prompt counts as taken by worker-control.py wording: launch without a block, merge and queue by their texts', () => {
  expect(sentOutcome('launch', undefined)).toBe('accepted')
  expect(sentOutcome('resume', 'wt: x has no running worker')).toBe('refused')
  expect(sentOutcome('merge', 'wt: merge: approving x run 1 (refused: tests); the tests run first')).toBe('accepted')
  expect(sentOutcome('merge', 'wt: merge: x run 1 is still going')).toBe('refused')
  expect(sentOutcome('queue', 'wt: queued x. waiting: busy')).toBe('accepted')
  expect(sentOutcome('queue all', 'wt: started now: x. ')).toBe('accepted')
  expect(sentOutcome('queue', 'wt: queue: no plans/queue/x.json')).toBe('refused')
  expect(sentOutcome('drop', 'wt: queue: x dropped')).toBe('accepted')
})

test('a sent button settles on an event of its topic after the press, a changed row, or its time limit', () => {
  const at = Date.parse('2026-11-02T02:10:00')
  const item = { topic: 'x', action: 'merge', sinceMs: at, untilMs: at + 900000, mark: 'm' }
  const same = () => 'm'
  // An older event of the topic, or a new one of another topic, leaves it waiting.
  expect(settle([item], same, [{ t: '2026-11-02T02:05:00', topic: 'x', kind: 'refused' }], at + 1000)).toEqual([item])
  expect(settle([item], same, [{ t: '2026-11-02T02:11:00', topic: 'y', kind: 'merged' }], at + 1000)).toEqual([item])
  expect(settle([item], same, [{ t: '2026-11-02T02:11:00', topic: 'x', kind: 'merged' }], at + 1000)).toEqual([])
  expect(settle([item], () => 'changed', [], at + 1000)).toEqual([])
  expect(settle([item], same, [], at + 900000)).toEqual([])
})

test('the mark of a topic changes when its run, action, merge decision, queue state or planned entry does', () => {
  const base = markOf('x', [{ topic: 'x', action: 'resume', run: 1, isHeld: false }], [], [{ topic: 'x' }])
  expect(markOf('x', [{ topic: 'x', action: 'stop', run: 2, isHeld: false }], [], [{ topic: 'x' }])).not.toBe(base)
  expect(markOf('x', [{ topic: 'x', action: 'resume', run: 1, isHeld: false }], [{ topic: 'x', state: 'waiting' }], [])).not.toBe(base)
  expect(markOf('', [], [], [{ topic: 'a' }, { topic: 'b' }])).toBe('a b')
})

test('a toast stays for its length within the limits; one that needs the person stays the longest', () => {
  const limits = { min: 6, max: 20, charsPerSecond: 20 }
  expect(toastMs('short', false, limits)).toBe(6000)
  expect(toastMs('x'.repeat(200), false, limits)).toBe(10000)
  expect(toastMs('x'.repeat(2000), false, limits)).toBe(20000)
  expect(toastMs('short', true, limits)).toBe(20000)
})

test('notices keep the whole text, newest first, at most max; unread counts only while closed', () => {
  const event = { t: '2026-11-02T02:13:26', kind: 'finish-failed', topic: 'x', text: 'check: merge-<run>.json doesn\'t say merged; nothing done (finish.json)' }
  const notice = noticeOf(event)
  expect(notice.needs).toBe(true)
  expect(noticeLine(notice)).toBe("02:13 ! x finish-failed: check: merge-<run>.json doesn't say merged; nothing done (finish.json)")
  const first = addNotices({ items: [], unread: 0, open: false }, [notice, { ...notice, topic: 'y' }], 2)
  expect(first.items.map(item => item.topic)).toEqual(['y', 'x'])
  expect(first.unread).toBe(2)
  const more = addNotices(first, [{ ...notice, topic: 'z' }], 2)
  expect(more.items.map(item => item.topic)).toEqual(['z', 'y'])
  expect(addNotices({ ...first, open: true }, [notice], 5).unread).toBe(0)
})

test('an approval in progress comes as "approving" with its run and time, in place of the merge offer', () => {
  const parsed = parseControls(JSON.stringify({ schema: 1, controls: [
    { topic: 'held-t', why: 'done', held: false, approving: { run: 2, since: '07:45' } },
    { topic: 'bad-t', why: 'done', held: false, approving: { run: 'x' } },
  ] }))
  expect(parsed.controls[0].approving).toEqual({ run: 2, since: '07:45' })
  expect(parsed.controls[0].merge).toBe(undefined)
  expect(parsed.controls[1].approving).toBe(undefined)
})

test('"already being approved" and a reason with words before it count as a taken merge', () => {
  expect(sentOutcome('merge', 'wt: merge: x run 2 is already being approved (since 07:45); the result comes as an event')).toBe('accepted')
  expect(sentOutcome('merge', 'Operation blocked by hook:\nwt: merge: approving x run 1 (refused: tests)')).toBe('accepted')
  expect(sentOutcome('merge', 'wt: merge: x run 1 was merged, nothing to approve')).toBe('refused')
})

// Seen: wt_controls.py failed (or timed out) while the approval's tests ran, the pane's rows went empty,
// the mark changed and the pending merge was released; the next good fetch drew the button for a second press.
test('unreadable controls never release a pending press; the next readable view decides', () => {
  const controls = [{ topic: 'x', why: 'done', isHeld: false, merge: { reason: 'review', files: [] } }]
  const good = { controls, queue: [], planned: [] }
  const failed = { controls: [], queue: [], planned: [], controlsError: 'wt_controls.py failed (exit -1): timed out' }
  const at = Date.parse('2026-11-02T07:45:00')
  const item = { topic: 'x', action: 'merge', sinceMs: at, untilMs: at + 900000, mark: marksOf(good)('x') }
  expect(marksOf(failed)('x')).toBe(undefined)
  expect(settle([item], marksOf(failed), [], at + 5000)).toEqual([item])
  expect(settle([item], marksOf(good), [], at + 10000)).toEqual([item])
  // The approval's own outcome still settles it: the row turns "approving" (no merge offer).
  const approving = { ...good, controls: [{ topic: 'x', why: 'done', isHeld: false, approving: { run: 1, since: '07:45' } }] }
  expect(settle([item], marksOf(approving), [], at + 15000)).toEqual([])
})

test('a press made while the controls were unreadable takes the first readable mark, then settles on a change', () => {
  const at = Date.parse('2026-11-02T07:45:00')
  const item = { topic: 'x', action: 'merge', sinceMs: at, untilMs: at + 900000 }
  const [baselined] = settle([item], () => 'm', [], at + 1000)
  expect(baselined).toEqual({ ...item, mark: 'm' })
  expect(settle([baselined], () => 'm', [], at + 2000)).toEqual([baselined])
  expect(settle([baselined], () => 'changed', [], at + 3000)).toEqual([])
})

test('queue_limits: each effective limit with its source; a repo limit says repo', () => {
  const parsed = parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local',
    queue_limits: { workers: 1, usd: 20, source: { workers: 'repo', usd: 'local' } } }))
  expect(parsed.queueLimits).toEqual({ workers: 1, usd: 20, source: { workers: 'repo', usd: 'local' } })
  expect(queueLimitsText(parsed.queueLimits)).toBe('limits: 1 worker(s) (repo) · $20 (local)')
})

test('queue_limits: absent or malformed gives no line; an unknown source is left out', () => {
  expect(parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local' })).queueLimits).toBe(undefined)
  expect(queueLimitsText(undefined)).toBe(undefined)
  const odd = parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local',
    queue_limits: { workers: 2, usd: 'x', source: { workers: 'mine' } } })).queueLimits
  expect(odd).toEqual({ workers: 2, usd: undefined, source: { workers: undefined, usd: undefined } })
  expect(queueLimitsText(odd)).toBe('limits: 2 worker(s)')
  const invalid = parseControls(JSON.stringify({ schema: 1, controls: [], flow_mode: 'local',
    queue_limits: { workers: 2, usd: 20, source: { workers: 'home (local invalid)', usd: 'home' } } })).queueLimits
  expect(queueLimitsText(invalid)).toBe('limits: 2 worker(s) (home (local invalid)) · $20 (home)')
})

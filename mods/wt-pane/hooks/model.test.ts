import { expect, test } from 'claude-code/testing'

import { bar, isRunning, snapshot, track, used, withPhases, workedMs, type Files } from './model'

const START = Date.parse('2026-10-30T10:00:00+08:00')

// events.jsonl writes local time without an offset.
function local(ms: number): string {
  const d = new Date(ms)
  const two = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}T${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`
}

function event(kind: string, id: string, type: string, minute: number): string {
  return JSON.stringify({ t: local(START + minute * 60000), event: kind, agent_id: id, agent_type: type })
}

function filesOf(map: Record<string, string>): Files {
  return {
    list: async dir => {
      const names = new Set<string>()
      for (const path of Object.keys(map)) {
        if (path.startsWith(dir + '/')) names.add(path.slice(dir.length + 1).split('/')[0])
      }
      return [...names]
    },
    read: async path => map[path],
  }
}

function running(): Record<string, string> {
  return {
    '/m/.hook-only/workers/clean.json': JSON.stringify({ run: 2, worktree: '/w', started: START / 1000 }),
    '/w/.hook-only/runs/start-1': '2026-10-30T08:00:00+08:00\n',
    '/w/.hook-only/runs/exit-1': '0 2026-10-30T09:00:00+08:00\n',
    '/w/.hook-only/runs/start-2': '2026-10-30T10:00:00+08:00\n',
    '/w/.hook-only/runs/est-2.json': JSON.stringify({ minutes: 40, usd: 2, cap: 4 }),
    '/w/.hook-only/runs/compact-2-1.json': '{}',
    '/w/.hook-only/budget/events.jsonl': [
      event('SubagentStart', 'old', 'qa', -90), // an earlier run
      event('SubagentStart', 'p1', 'qa-planner', 1),
      event('SubagentStart', 'p1', 'qa-planner', 1), // the claimed-budget line
      event('SubagentStop', 'p1', 'qa-planner', 5),
      event('SubagentStart', 'r1', 'spec-reviewer', 6),
      event('SubagentStop', 'r1', 'spec-reviewer', 8),
      event('SubagentStart', 'r1', 'spec-reviewer', 9), // resumed
      event('SubagentStop', 'r1', 'spec-reviewer', 10),
      event('SubagentStart', 'q1', '', 11),
    ].join('\n'),
    '/w/.hook-only/budget/q1.json': JSON.stringify({ minutes: 20, usd: 0.8, usd_at_start: 0 }),
    '/w/.hook-only/hook-read-only/progress/main.json': JSON.stringify({
      t: (START + 11 * 60000) / 1000, agent_id: '', agent_type: 'orchestrator', tool: 'Agent', what: 'qa: Write tests', run: 2, usd_run: 0.5, unpriced: 0,
    }),
    '/w/.hook-only/hook-read-only/progress/q1.json': JSON.stringify({
      t: (START + 12 * 60000) / 1000, agent_id: 'q1', agent_type: 'qa', tool: 'Edit', what: 'transform/tests/test_x.py', run: 2, usd: 0.25, usd_run: 0.25, unpriced: 1,
    }),
    '/w/.hook-only/hook-read-only/progress/old.json': JSON.stringify({
      t: 0, agent_id: 'old', agent_type: 'qa', tool: 'Bash', what: '', run: 1, usd_run: 9, unpriced: 0,
    }),
    '/w/.hook-only/hook-read-only/progress/.offsets/main.json': '{}',
  }
}

test('a running worker: steps, the current subagent, what it does and this run spend', async () => {
  const [row] = await snapshot(filesOf(running()), '/m', START + 12 * 60000, 600000)

  expect(row.topic).toBe('clean')
  expect(row.run).toBe(2)
  expect(row.exitMs).toBe(undefined)
  expect(row.compacts).toBe(1)
  expect(row.done).toEqual([
    { type: 'qa-planner', count: 1 },
    { type: 'spec-reviewer', count: 2 },
  ])
  expect(row.agents.length).toBe(1)
  // events.jsonl had no agent_type: the progress file names it.
  expect(row.agents[0].type).toBe('qa')
  expect(row.agents[0].budgetMinutes).toBe(20)
  expect(row.agents[0].budgetUsd).toBe(0.8)
  expect(row.agents[0].usdRun).toBe(0.25)
  expect(row.agents[0].what).toBe('transform/tests/test_x.py')
  expect(row.orchestrator?.what).toBe('qa: Write tests')
  // The earlier run's 9 dollars are not this run's.
  expect(row.usdRun).toBe(0.75)
  expect(row.unpriced).toBe(1)
  // 12 of 40 minutes is 0.3, 0.75 of the $2 estimate (not the $4 cap) is 0.375: the larger counts.
  expect(used(row, START + 12 * 60000)).toBe(0.375)
})

test('a finished worker shows its exit for a while, then leaves', async () => {
  const files = running()
  files['/w/.hook-only/runs/exit-2'] = '1 2026-10-30T10:30:00+08:00\n'
  files['/w/.hook-only/runs/result-2.json'] = JSON.stringify({ total_cost_usd: 1.5 })
  const end = Date.parse('2026-10-30T10:30:00+08:00')

  const [row] = await snapshot(filesOf(files), '/m', end + 60000, 600000)
  expect(row.exitCode).toBe(1)
  expect(row.resultUsd).toBe(1.5)

  expect((await snapshot(filesOf(files), '/m', end + 600001, 600000)).length).toBe(0)
})

test('a removed worktree and an unreadable state file are skipped', async () => {
  const files = {
    '/m/.hook-only/workers/gone.json': JSON.stringify({ run: 1, worktree: '/nowhere' }),
    '/m/.hook-only/workers/bad.json': 'not json',
    '/m/.hook-only/workers/notes.txt': 'x',
  }
  expect((await snapshot(filesOf(files), '/m', START, 600000)).length).toBe(0)
})

test('running workers come before finished ones', async () => {
  const files = running()
  files['/m/.hook-only/workers/done.json'] = JSON.stringify({ run: 1, worktree: '/d' })
  files['/d/.hook-only/runs/start-1'] = '2026-10-30T07:00:00+08:00\n'
  files['/d/.hook-only/runs/exit-1'] = '0 2026-10-30T10:05:00+08:00\n'

  const rows = await snapshot(filesOf(files), '/m', START + 12 * 60000, 600000)
  expect(rows.map(row => row.topic)).toEqual(['clean', 'done'])
})

test('running is the phase wt_controls.py reports, never a missing exit file; running ones come first', async () => {
  const files = running()
  files['/m/.hook-only/workers/dead.json'] = JSON.stringify({ run: 1, worktree: '/d' })
  files['/d/.hook-only/runs/start-1'] = '2026-10-30T07:00:00+08:00\n' // no exit file: its wrapper died
  const rows = await snapshot(filesOf(files), '/m', START + 12 * 60000, 600000)
  expect(rows.every(row => row.exitMs === undefined)).toBe(true)

  const controls = [
    { topic: 'dead', isHeld: false, phase: 'orphan' as const },
    { topic: 'clean', isHeld: false, phase: 'finishing' as const },
  ]
  const phased = withPhases(rows, controls)
  expect(phased.map(row => [row.topic, row.phase, isRunning(row)])).toEqual([
    ['clean', 'finishing', true],
    ['dead', 'orphan', false],
  ])
  // Before the controls are read (or when they can't be), nothing counts as running.
  expect(withPhases(rows, []).some(isRunning)).toBe(false)
})

test('the bar is clamped to its width', () => {
  expect(bar(0.5, 4)).toBe('██░░')
  expect(bar(2, 4)).toBe('████')
  expect(bar(-1, 4)).toBe('░░░░')
})

const OPTIONS = {
  stations: ['qa-planner', 'spec-reviewer', 'qa', 'developer', 'code-reviewer'],
  colors: { orchestrator: '#e0e0e0', qa: '#33cc66', 'qa-planner': '#33cccc', other: '#9aa0a6' },
  labels: { 'qa-planner': 'plan', 'spec-reviewer': 'spec', qa: 'qa', developer: 'dev', 'code-reviewer': 'rev' },
  verbs: { Edit: 'writing', Agent: 'assigning' },
  idleVerb: 'thinking',
  waitVerb: 'waiting',
  holdMs: 20000,
  warnRatio: 1,
}

test('the office: done, active and pending desks, the subagent typing at its desk, the orchestrator waiting', async () => {
  const now = START + 12 * 60000
  const [row] = await snapshot(filesOf(running()), '/m', now, 600000)
  const { desks, seated, boss } = track(row, now, OPTIONS)

  expect(desks.map(desk => `${desk.label}:${desk.state}:${desk.rounds}`)).toEqual([
    'plan:done:1', 'spec:done:2', 'qa:active:0', 'dev:pending:0', 'rev:pending:0',
  ])
  expect(desks[0].color).toBe(0x33cccc)
  expect(seated).toEqual([{ key: 'q1', desk: 2, color: 0x33cc66, pose: 'type', say: 'writing', isWarned: false }])
  expect(boss.desk).toBe(2)
  expect(boss.say).toBe('orch: waiting')
  expect(boss.color).toBe(0xe0e0e0)
})

test('a reviewer whose findings sent the work back shows a cross until its own next round', async () => {
  const now = START + 12 * 60000
  const [row] = await snapshot(filesOf(running()), '/m', now, 600000)
  const states = (done: { type: string; count: number }[], agents: string[]) =>
    track({ ...row, done, agents: agents.map(type => ({ ...row.agents[0], type })) }, now, OPTIONS).desks.map(desk => desk.state)

  // plan, spec, then plan again: spec sent it back.
  expect(states([{ type: 'qa-planner', count: 1 }, { type: 'spec-reviewer', count: 1 }], ['qa-planner'])).toEqual([
    'active', 'returned', 'pending', 'pending', 'pending',
  ])
  // spec reviewed again and passed it on: done again.
  expect(
    states([{ type: 'qa-planner', count: 1 }, { type: 'spec-reviewer', count: 1 }, { type: 'qa-planner', count: 1 }, { type: 'spec-reviewer', count: 1 }], ['qa']),
  ).toEqual(['done', 'done', 'active', 'pending', 'pending'])
  // rev's findings sent dev back; plan, spec and qa stay done.
  expect(
    states([{ type: 'qa-planner', count: 1 }, { type: 'spec-reviewer', count: 1 }, { type: 'qa', count: 1 }, { type: 'developer', count: 1 }, { type: 'code-reviewer', count: 1 }], ['developer']),
  ).toEqual(['done', 'done', 'done', 'active', 'returned'])
  // rev sent it to qa: dev and rev both crossed while qa works.
  const toQa = [{ type: 'qa-planner', count: 1 }, { type: 'spec-reviewer', count: 1 }, { type: 'qa', count: 1 }, { type: 'developer', count: 1 }, { type: 'code-reviewer', count: 1 }]
  expect(states(toQa, ['qa'])).toEqual(['done', 'done', 'active', 'returned', 'returned'])
  // qa went straight back to rev, skipping dev: dev is done again as soon as rev starts.
  expect(states([...toQa, { type: 'qa', count: 1 }], ['code-reviewer'])).toEqual(['done', 'done', 'done', 'done', 'active'])
  // and stays done after rev's round, with nobody at work.
  expect(states([...toQa, { type: 'qa', count: 1 }, { type: 'code-reviewer', count: 1 }], [])).toEqual(['done', 'done', 'done', 'done', 'done'])
  // A back-step after passing it again crosses it again: rev -> qa -> rev -> dev.
  expect(states([...toQa, { type: 'qa', count: 1 }, { type: 'code-reviewer', count: 1 }], ['developer'])).toEqual(['done', 'done', 'done', 'active', 'returned'])
  // With nobody at work, the orchestrator heads for the first desk to do again.
  const between = { ...row, agents: [], done: [{ type: 'qa-planner', count: 1 }, { type: 'spec-reviewer', count: 1 }, { type: 'qa-planner', count: 1 }] }
  expect(track(between, now, OPTIONS).boss.desk).toBe(1)
})

test('a verb lasts holdMs, an unlisted tool is thinking, and only past its budget a figure warns', async () => {
  const now = START + 12 * 60000
  const [row] = await snapshot(filesOf(running()), '/m', now, 600000)

  const later = track(row, now + 30000, OPTIONS).seated[0]
  expect(later.say).toBe('thinking')
  expect(later.pose).toBe('idle')

  const bash = { ...row, agents: [{ ...row.agents[0], tool: 'Bash' }] }
  expect(track(bash, now, OPTIONS).seated[0].say).toBe('thinking')

  // Budget $0.8: 0.7 is 88%, inside the estimate, no flash (the old 80% checkpoint is gone);
  // 0.85 is past it, into the headroom the budget hook allows before it stops the subagent.
  const within = { ...row, agents: [{ ...row.agents[0], usdRound: 0.7 }] }
  expect(track(within, now, OPTIONS).seated[0].isWarned).toBe(false)
  const over = { ...row, agents: [{ ...row.agents[0], usdRound: 0.85 }] }
  expect(track(over, now, OPTIONS).seated[0].isWarned).toBe(true)
})

test('a resumed agent is judged on this round only, as the budget hook judges it', async () => {
  const files = running()
  // The same agent id, resumed: 0.6 spent in its first round, 0.25 since this round began.
  files['/w/.hook-only/budget/q1.json'] = JSON.stringify({ minutes: 20, usd: 0.4, usd_at_start: 0.6 })
  files['/w/.hook-only/hook-read-only/progress/q1.json'] = JSON.stringify({
    t: (START + 12 * 60000) / 1000, agent_id: 'q1', agent_type: 'qa', tool: 'Edit', what: '', run: 2, usd: 0.85, usd_run: 0.85, unpriced: 0,
  })
  const now = START + 12 * 60000
  const [row] = await snapshot(filesOf(files), '/m', now, 600000)

  // 0.85 - 0.6 in floating point: compared to the cent.
  expect(Math.round((row.agents[0].usdRound ?? 0) * 100) / 100).toBe(0.25)
  // 0.25 of 0.4 is 62%: no flash, though the whole run's 0.85 is past the round's budget.
  expect(track(row, now, OPTIONS).seated[0].isWarned).toBe(false)
})

test('a hook wait stops the agent clock: its time and its flash count only its own work', async () => {
  const files = running()
  // Started at minute 11 with a 20 min budget; the mutation scan has held it since minute 21.
  files['/w/.hook-only/budget/q1.json'] = JSON.stringify({
    minutes: 20, usd: 0.8, usd_at_start: 0, started: (START + 11 * 60000) / 1000,
    hook_wait: { since: (START + 21 * 60000) / 1000, what: 'mutation scan', max_seconds: 3300 },
  })
  const now = START + 51 * 60000
  const [row] = await snapshot(filesOf(files), '/m', now, 600000)

  const agent = row.agents[0]
  expect(agent.waitWhat).toBe('mutation scan')
  expect(agent.waitMaxMinutes).toBe(55)
  expect(workedMs(agent, now)).toBe(10 * 60000)
  // 10 of 20 min worked, 30 min held: no flash.
  expect(track(row, now, OPTIONS).seated[0].isWarned).toBe(false)

  // The wait over: excludeWait moved started forward by it and dropped the mark.
  files['/w/.hook-only/budget/q1.json'] = JSON.stringify({
    minutes: 20, usd: 0.8, usd_at_start: 0, started: (START + 41 * 60000) / 1000,
  })
  const [after] = await snapshot(filesOf(files), '/m', now, 600000)
  expect(after.agents[0].waitSinceMs).toBe(undefined)
  expect(workedMs(after.agents[0], now)).toBe(10 * 60000)
})

test('with no subagent the orchestrator heads for the next pending desk; an off-plan agent is named', async () => {
  const now = START + 12 * 60000
  const [row] = await snapshot(filesOf(running()), '/m', now, 600000)

  const alone = { ...row, agents: [], orchestrator: { tool: 'Agent', what: '', lastMs: now } }
  const quiet = track(alone, now, OPTIONS)
  expect(quiet.desks[2].state).toBe('pending')
  expect(quiet.seated).toEqual([])
  expect(quiet.boss.desk).toBe(2)
  expect(quiet.boss.say).toBe('orch: assigning')

  const stranger = { ...row, agents: [{ ...row.agents[0], type: 'helper' }] }
  const off = track(stranger, now, OPTIONS)
  expect(off.seated).toEqual([])
  expect(off.boss.say).toBe('orch: waiting helper')
})

test('the run is measured against its estimate; the limits are kept for the header', async () => {
  const files = running()
  files['/w/.hook-only/runs/est-2.json'] = JSON.stringify({ minutes: 40, usd: 2, cap: 2.5, cap_minutes: 50 })
  const now = START + 12 * 60000
  const [row] = await snapshot(filesOf(files), '/m', now, 600000)
  expect([row.estMinutes, row.estUsd, row.capMinutes, row.capUsd]).toEqual([40, 2, 50, 2.5])
  // 12 of 40 minutes is 0.3; spend against the $2 estimate, not the $2.50 limit.
  expect(used({ ...row, usdRun: 1.5 }, now)).toBe(0.75)
  // A file from before the headroom change has no usd: spend against its cap.
  expect(used({ ...row, estUsd: undefined, capUsd: 3, usdRun: 1.5 }, now)).toBe(0.5)
})

// A topic on its third run: plan and spec finished in run 1, qa in run 2 (which also left a round
// open as it stopped), dev in run 3, where rev now works.
function thirdRun(): Record<string, string> {
  return {
    '/m/.hook-only/workers/multi.json': JSON.stringify({ run: 3, worktree: '/x', started: START / 1000 }),
    '/x/.hook-only/runs/start-1': '2026-10-30T07:00:00+08:00\n',
    '/x/.hook-only/runs/exit-1': '0 2026-10-30T07:40:00+08:00\n',
    '/x/.hook-only/runs/start-2': '2026-10-30T08:00:00+08:00\n',
    '/x/.hook-only/runs/exit-2': '0 2026-10-30T09:00:00+08:00\n',
    '/x/.hook-only/runs/start-3': '2026-10-30T10:00:00+08:00\n',
    '/x/.hook-only/runs/compact-2-1.json': '{}',
    '/x/.hook-only/budget/events.jsonl': [
      event('SubagentStart', 'old', 'qa', -200), // before the topic's first run
      event('SubagentStop', 'old', 'qa', -195),
      event('SubagentStart', 'p1', 'qa-planner', -170),
      event('SubagentStop', 'p1', 'qa-planner', -160),
      event('SubagentStart', 'r1', 'spec-reviewer', -150),
      event('SubagentStop', 'r1', 'spec-reviewer', -140),
      event('SubagentStart', 'q1', 'qa', -110),
      event('SubagentStop', 'q1', 'qa', -100),
      event('SubagentStart', 'z1', 'qa', -70), // run 2 stopped with it at work
      event('SubagentStart', 'd1', 'developer', 1),
      event('SubagentStop', 'd1', 'developer', 5),
      event('SubagentStart', 'c1', 'code-reviewer', 6),
    ].join('\n'),
    '/x/.hook-only/hook-read-only/progress/q1.json': JSON.stringify({
      t: (START - 100 * 60000) / 1000, agent_id: 'q1', agent_type: 'qa', tool: 'Edit', what: '', run: 2, usd: 5, usd_run: 5, unpriced: 2,
    }),
    '/x/.hook-only/hook-read-only/progress/c1.json': JSON.stringify({
      t: (START + 7 * 60000) / 1000, agent_id: 'c1', agent_type: 'code-reviewer', tool: 'Bash', what: '', run: 3, usd: 0.3, usd_run: 0.3, unpriced: 0,
    }),
  }
}

test('a resumed topic keeps the desks finished in earlier runs; running agents and spend stay per run', async () => {
  const now = START + 8 * 60000
  const [row] = await snapshot(filesOf(thirdRun()), '/m', now, 600000)

  expect(row.run).toBe(3)
  expect(row.startedMs).toBe(START)
  // Counted from run 1's start: the round before it and the round run 2 left open are not done.
  expect(row.done).toEqual([
    { type: 'qa-planner', count: 1 },
    { type: 'spec-reviewer', count: 1 },
    { type: 'qa', count: 1 },
    { type: 'developer', count: 1 },
  ])
  // Only this run's open round is at work, not the one run 2 left open.
  expect(row.agents.map(agent => agent.id)).toEqual(['c1'])
  // Run 2's $5 and its unpriced calls are not this run's; nor are its compactions.
  expect(row.usdRun).toBe(0.3)
  expect(row.unpriced).toBe(0)
  expect(row.compacts).toBe(0)

  expect(track(row, now, OPTIONS).desks.map(desk => `${desk.label}:${desk.state}:${desk.rounds}`)).toEqual([
    'plan:done:1', 'spec:done:1', 'qa:done:1', 'dev:done:1', 'rev:active:0',
  ])
})

test('a desk sent back by a reviewer round of an earlier run is still returned', async () => {
  const files = thirdRun()
  // Run 2 ended on a rev round after dev; run 3 sends the work back to dev.
  files['/x/.hook-only/budget/events.jsonl'] = [
    event('SubagentStart', 'p1', 'qa-planner', -170),
    event('SubagentStop', 'p1', 'qa-planner', -160),
    event('SubagentStart', 'q1', 'qa', -110),
    event('SubagentStop', 'q1', 'qa', -100),
    event('SubagentStart', 'd1', 'developer', -90),
    event('SubagentStop', 'd1', 'developer', -80),
    event('SubagentStart', 'c1', 'code-reviewer', -75),
    event('SubagentStop', 'c1', 'code-reviewer', -65),
    event('SubagentStart', 'd1', 'developer', 2), // resumed in run 3 for rev's findings
  ].join('\n')
  const now = START + 8 * 60000
  const [row] = await snapshot(filesOf(files), '/m', now, 600000)

  expect(row.agents.map(agent => agent.type)).toEqual(['developer'])
  expect(track(row, now, OPTIONS).desks.map(desk => desk.state)).toEqual([
    'done', 'pending', 'done', 'active', 'returned',
  ])
})

test('an agent id that a stopped run left open starts a new round when a later run starts it again', async () => {
  const files = thirdRun()
  files['/x/.hook-only/budget/events.jsonl'] = [
    event('SubagentStart', 'p1', 'qa-planner', -170),
    event('SubagentStop', 'p1', 'qa-planner', -160),
    event('SubagentStart', 'z1', 'qa', -70), // run 2 stopped with it at work
    event('SubagentStart', 'd1', 'developer', 1),
    event('SubagentStop', 'd1', 'developer', 3),
    event('SubagentStart', 'z1', 'qa', 4), // the same id, started again in run 3
    event('SubagentStop', 'z1', 'qa', 6),
  ].join('\n')
  const [row] = await snapshot(filesOf(files), '/m', START + 8 * 60000, 600000)

  // qa's round is run 3's, after dev, not run 2's open one before it.
  expect(row.done).toEqual([
    { type: 'qa-planner', count: 1 },
    { type: 'developer', count: 1 },
    { type: 'qa', count: 1 },
  ])
  expect(row.agents).toEqual([])
})

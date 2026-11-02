import { expect, test } from 'claude-code/testing'

import { deskCentre, fits, frameOf, office, ROWS, slotFor, type Desk, type Seated, type Walker } from './office'
import type { Cell } from './sprite'

const SLOT = 16
const GREEN = 0x33cc66
const HAIR = 0x5a3a22
const TICK_MARK = 0x4ade80
const CHAIR = 0x2b2f3a
const DESKS: Desk[] = [
  { label: 'plan', color: 0x33cccc, state: 'done', rounds: 1 },
  { label: 'spec', color: 0x4a7dff, state: 'done', rounds: 2 },
  { label: 'qa', color: GREEN, state: 'active', rounds: 0 },
  { label: 'dev', color: 0xddbb22, state: 'pending', rounds: 0 },
  { label: 'rev', color: 0xbb55dd, state: 'pending', rounds: 0 },
]
const QA: Seated = { desk: 2, color: GREEN, pose: 'type', say: 'writing', isWarned: false }
const BOSS: Walker = { x: deskCentre(2, SLOT), color: 0xe0e0e0, pose: 'idle', say: 'waiting', isWarned: false }

function text(cells: Cell[], columns: number, row: number): string {
  return cells.slice(row * columns, (row + 1) * columns).map(cell => String.fromCharCode(cell[0])).join('')
}

// Every pixel color drawn in the cell rows `top`..`bottom` of a desk's slot.
function colorsIn(cells: Cell[], columns: number, desk: number, top: number, bottom: number): Set<number> {
  const found = new Set<number>()
  for (let row = top; row <= bottom; row++) {
    for (let col = desk * SLOT; col < (desk + 1) * SLOT; col++) {
      const cell = cells[row * columns + col]
      if (cell[0] === 0x2580) {
        found.add(cell[1])
        found.add(cell[2])
      }
    }
  }
  return found
}

test('the office spans one slot per desk and twenty cell rows', () => {
  const { columns, rows, cells } = office(DESKS, [QA], BOSS, SLOT, 0)
  expect(columns).toBe(5 * SLOT)
  expect(rows).toBe(ROWS)
  expect(rows).toBe(20)
  expect(cells.length).toBe(columns * rows)
})

test('the wall names every desk: done ones with a ✓ and their rounds, pending ones grey', () => {
  const { columns, cells } = office(DESKS, [QA], BOSS, SLOT, 0)
  const names = text(cells, columns, 1)
  expect(names.includes('plan ✓')).toBe(true)
  expect(names.includes('specx2 ✓')).toBe(true)
  expect(names.includes('qa')).toBe(true)
  const dev = names.indexOf('dev')
  expect(cells[columns + dev][1]).toBe(0x8a93a3)
  expect(cells[columns + names.indexOf('plan')][1]).toBe(0x33cccc)
})

test('a desk at work again shows its round; a done one its count', () => {
  const again = DESKS.map((desk, index) => (index === 0 ? { ...desk, state: 'active' as const, rounds: 1 } : desk))
  const { columns, cells } = office(again, [], undefined, SLOT, 0)
  const names = text(cells, columns, 1)
  expect(names.includes('plan #2')).toBe(true)
  expect(names.includes('specx2 ✓')).toBe(true)
})

test('a desk that sent the work back: a red cross over it and its name', () => {
  const back = DESKS.map((desk, index) => (index === 1 ? { ...desk, state: 'returned' as const } : desk))
  const { columns, cells } = office(back, [], undefined, SLOT, 0)
  const names = text(cells, columns, 1)
  expect(names.includes('specx2 ✗')).toBe(true)
  expect(cells[columns + names.indexOf('spec')][1]).toBe(0xe05050)
  expect(colorsIn(cells, columns, 1, 6, 9).has(0xe05050)).toBe(true)
  expect(colorsIn(cells, columns, 1, 6, 9).has(TICK_MARK)).toBe(false)
})

test('the worker at its desk: hair over the laptop, its bubble on its own color', () => {
  const { columns, cells } = office(DESKS, [QA], BOSS, SLOT, 0)
  expect(colorsIn(cells, columns, 2, 5, 6).has(HAIR)).toBe(true)
  const bubble = text(cells, columns, 3)
  const at = bubble.indexOf(' writing ')
  expect(at).toBeGreaterThan(2 * SLOT - 1)
  expect(cells[3 * columns + at + 1]).toEqual([0x77, 0x111111, GREEN])
})

test('a done desk shows the tick and an empty chair; a pending one only the chair', () => {
  const { columns, cells } = office(DESKS, [QA], BOSS, SLOT, 0)
  const done = colorsIn(cells, columns, 0, 7, 10)
  expect(done.has(TICK_MARK)).toBe(true)
  expect(done.has(CHAIR)).toBe(true)
  const pending = colorsIn(cells, columns, 3, 7, 10)
  expect(pending.has(TICK_MARK)).toBe(false)
  expect(pending.has(CHAIR)).toBe(true)
  expect(colorsIn(cells, columns, 3, 5, 6).has(HAIR)).toBe(false)
})

test('the orchestrator walks the aisle with its caption under its feet', () => {
  const { columns, cells } = office(DESKS, [QA], BOSS, SLOT, 0)
  expect(text(cells, columns, ROWS - 1).includes(' waiting ')).toBe(true)
  expect(colorsIn(cells, columns, 2, 12, 13).has(HAIR)).toBe(true)
  const alone = office(DESKS, [QA], undefined, SLOT, 0)
  expect(text(alone.cells, alone.columns, ROWS - 1).includes('waiting')).toBe(false)
})

test('a warned bubble flashes red on odd ticks', () => {
  const warned = { ...QA, isWarned: true }
  const even = office(DESKS, [warned], undefined, SLOT, 0)
  const odd = office(DESKS, [warned], undefined, SLOT, 1)
  const at = text(even.cells, even.columns, 3).indexOf(' writing ')
  expect(even.cells[3 * even.columns + at][2]).toBe(GREEN)
  expect(odd.cells[3 * odd.columns + at][2]).toBe(0xe05050)
})

test('long names are cut to the slot, other characters shown as ?', () => {
  // A 16-column slot keeps 12 letters of a name.
  const desks = [{ ...DESKS[3], label: 'developer-reviewer' }, { ...DESKS[3], label: 'é' }]
  const { columns, cells } = office(desks, [], undefined, SLOT, 0)
  const names = text(cells, columns, 1)
  expect(names.includes('developer-rev')).toBe(false)
  expect(names.includes('developer-re')).toBe(true)
  expect(names.includes('?')).toBe(true)
})

test('walking alternates two steps; idle blinks now and then; slots stay between 12 and 18', () => {
  expect(frameOf('walk', 0)).not.toEqual(frameOf('walk', 2))
  expect(frameOf('walk', 0)).toEqual(frameOf('walk', 4))
  expect(frameOf('idle', 0)[4]).toBe('.KKEKKEKK.')
  expect(frameOf('idle', 10)[4]).toBe('.KKKKKKKK.')
  expect(slotFor(40, 5)).toBe(12)
  expect(slotFor(80, 5)).toBe(16)
  expect(slotFor(200, 5)).toBe(18)
  expect(deskCentre(2, 16)).toBe(40)
})

test('five desks need 60 columns: a narrower pane does not fit them', () => {
  expect(fits(60, 5)).toBe(true)
  expect(fits(59, 5)).toBe(false)
  expect(fits(40, 5)).toBe(false)
})

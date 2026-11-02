// The office of one worker, hand drawn: a wall with each station's name, a row of desks (plan, spec,
// qa, dev, rev), the subagent at work sitting at its desk behind a laptop, and the orchestrator
// walking the aisle to the desk it deals with. Painted into a pixel buffer, then turned into a
// Raster of half blocks (two pixels a cell: the top one the foreground of `▀`, the bottom one its
// background), with the words (names, bubbles, the caption) laid over as text cells. Pure: no `$`.

import { shade, type Cell } from './sprite'

// returned: done, then the flow went back to an earlier desk (its work was sent back to be redone).
export type Desk = { label: string; color: number; state: 'done' | 'returned' | 'active' | 'pending'; rounds: number }
export type Pose = 'idle' | 'type' | 'walk'
export type Person = { color: number; pose: Pose; say: string; isWarned: boolean }
export type Seated = Person & { desk: number }
export type Walker = Person & { x: number } // the walker's centre, in pixels

export const HEIGHT = 40 // pixels: 20 cell rows
export const ROWS = HEIGHT / 2
export const MIN_SLOT = 12
export const MAX_SLOT = 18

const WALL = 0x4c566a
const MOLDING = 0x3b4252
const BASEBOARD = 0x2e3440
const FLOOR = 0xc69c6d
const PLANK = 0xb48a5c
const DESK_TOP = 0xb8834e
const DESK = 0xa0703f
const DESK_FRONT = 0x7a5230
const DESK_LEG = 0x5e3d22
const LID = 0xc0c6cf
const LID_EDGE = 0x5c6370
const CLOSED = 0x9aa0a6
const CHECK = 0x4ade80
const CROSS = 0xe05050
const PENDING_TEXT = 0x8a93a3
const BUBBLE_TEXT = 0x111111
const CAPTION = 0xf2f2f2
const WARN = 0xe05050
const CHAIR = 0x2b2f3a
const CHAIR_TOP = 0x434a5a

// Rows of the room, in pixels.
const LABEL_ROW = 1 // cell row of the station names
const BUBBLE_ROW = 3 // cell row of a seated worker's bubble
const SEAT_TOP = 10 // a seated worker's head
const LID_TOP = 17
const DESK_Y = 22
const FLOOR_Y = 22
const WALKER_TOP = 24
const CAPTION_ROW = ROWS - 1

// 10 x 14. H hair, K skin, E eyes, C cheeks, M mouth, B shirt, S shirt shade, P trousers, D shoes.
const HEAD = ['...HHHH...', '..HHHHHH..', '.HHHHHHHH.', '.HKKKKKKH.']
const EYES = '.KKEKKEKK.'
const SHUT = '.KKKKKKKK.'
const FACE = ['.KKKKKKKK.', '..KCMMCK..', '...KKKK...', '..BBBBBB..', '.BBSBBSBB.']
const STAND = ['KBBBBBBBBK', '.BBBBBBBB.', '..PP..PP..', '..DD..DD..']
const STEP_A = ['KBBBBBBBB.', '.BBBBBBBB.', '.PP....PP.', '.DD....DD.']
const STEP_B = ['.BBBBBBBBK', '.BBBBBBBB.', '...PPPP...', '...DD.DD..']
const FIGURE = {
  open: [...HEAD, EYES, ...FACE, ...STAND],
  blink: [...HEAD, SHUT, ...FACE, ...STAND],
  stepA: [...HEAD, EYES, ...FACE, ...STEP_A],
  stepB: [...HEAD, EYES, ...FACE, ...STEP_B],
}
export const FIGURE_WIDTH = 10
export const FIGURE_HEIGHT = 14

const SKIN: Record<string, number> = { H: 0x5a3a22, K: 0xf2c49b, E: 0x202020, C: 0xf08a8a, M: 0xb5524a, P: 0x3b4a6b, D: 0x222222 }

// 5 x 4: the mark over a desk whose station is done; 5 x 5 over one that sent its work back.
const TICK = ['....G', '...G.', 'G.G..', '.G...']
const CROSS_MARK = ['X...X', '.X.X.', '..X..', '.X.X.', 'X...X']

type Canvas = { width: number; pixels: number[] }

function paint(canvas: Canvas, x: number, y: number, color: number) {
  if (x >= 0 && x < canvas.width && y >= 0 && y < HEIGHT) canvas.pixels[y * canvas.width + x] = color
}

function fill(canvas: Canvas, left: number, top: number, width: number, height: number, color: number) {
  for (let y = top; y < top + height; y++) for (let x = left; x < left + width; x++) paint(canvas, x, y, color)
}

function sprite(canvas: Canvas, rows: string[], left: number, top: number, colors: Record<string, number>, maxY = HEIGHT) {
  rows.forEach((row, dy) => {
    if (top + dy >= maxY) return
    ;[...row].forEach((ch, dx) => {
      const color = colors[ch]
      if (color !== undefined) paint(canvas, left + dx, top + dy, color)
    })
  })
}

// Which frame of the figure a pose shows at `tick`: poses change every two ticks.
export function frameOf(pose: Pose, tick: number): string[] {
  const odd = Math.floor(tick / 2) % 2 === 1
  if (pose === 'walk') return odd ? FIGURE.stepB : FIGURE.stepA
  // Idle blinks now and then; typing keeps the eyes on the screen.
  if (pose === 'idle' && Math.floor(tick / 2) % 6 === 5) return FIGURE.blink
  return FIGURE.open
}

function room(canvas: Canvas) {
  fill(canvas, 0, 0, canvas.width, 1, MOLDING)
  fill(canvas, 0, 1, canvas.width, 19, WALL)
  fill(canvas, 0, 20, canvas.width, 2, BASEBOARD)
  fill(canvas, 0, FLOOR_Y, canvas.width, HEIGHT - FLOOR_Y, FLOOR)
  // Planks: a seam every fourth row, the joints staggered.
  for (let y = FLOOR_Y + 3; y < HEIGHT; y += 4) {
    fill(canvas, 0, y, canvas.width, 1, PLANK)
    const offset = ((y - FLOOR_Y) / 4) % 2 === 0 ? 0 : 6
    for (let x = offset; x < canvas.width; x += 12) fill(canvas, x, y - 3, 1, 3, PLANK)
  }
}

function desk(canvas: Canvas, left: number, slot: number, state: Desk['state'], color: number, tick: number, typing: boolean) {
  const centre = left + Math.floor(slot / 2)
  const lidLeft = centre - 4
  if (state === 'active') {
    // The open lid, its back to us; the logo pulses in the worker's color.
    fill(canvas, lidLeft, LID_TOP, 8, 5, LID_EDGE)
    fill(canvas, lidLeft + 1, LID_TOP + 1, 6, 4, LID)
    fill(canvas, centre - 1, LID_TOP + 2, 2, 2, tick % 2 === 0 ? color : shade(color, 0.6))
    if (typing) {
      // Hands on the keys at either side of the lid, taking turns.
      const up = tick % 2 === 0
      paint(canvas, lidLeft - 1, up ? 20 : 21, SKIN.K)
      paint(canvas, lidLeft + 8, up ? 21 : 20, SKIN.K)
    }
  } else {
    fill(canvas, lidLeft, DESK_Y - 1, 8, 1, CLOSED)
  }
  fill(canvas, left + 1, DESK_Y, slot - 2, 1, DESK_TOP)
  fill(canvas, left + 1, DESK_Y + 1, slot - 2, 1, DESK)
  fill(canvas, left + 1, DESK_Y + 2, slot - 2, 1, DESK_FRONT)
  fill(canvas, left + 2, DESK_Y + 3, 2, 4, DESK_LEG)
  fill(canvas, left + slot - 4, DESK_Y + 3, 2, 4, DESK_LEG)
  if (state === 'done') sprite(canvas, TICK, centre - 2, 14, { G: CHECK })
  if (state === 'returned') sprite(canvas, CROSS_MARK, centre - 2, 13, { X: CROSS })
}

function colorsOf(person: Person): Record<string, number> {
  return { ...SKIN, B: person.color, S: shade(person.color, 0.75) }
}

type Words = { row: number; col: number; text: string; fg: number; bg?: number }

// Text over the picture: printable ASCII and ✓ (a Raster takes width-1 characters), others as '?'.
function lay(cells: Cell[], columns: number, words: Words) {
  const glyphs = [...words.text]
  const first = Math.min(Math.max(0, words.col), Math.max(0, columns - glyphs.length))
  glyphs.forEach((ch, i) => {
    const col = first + i
    if (col >= columns) return
    const code = ch === '✓' ? 0x2713 : ch === '✗' ? 0x2717 : ch.charCodeAt(0) >= 0x20 && ch.charCodeAt(0) <= 0x7e ? ch.charCodeAt(0) : 0x3f
    const cell = cells[words.row * columns + col]
    // Without a background of its own the word sits on whatever the picture has there.
    cells[words.row * columns + col] = [code, words.fg, words.bg ?? cell[1]]
  })
}

// Whether every desk fits the pane's width: a desk is MIN_SLOT columns at the least (a figure and its
// desk legs need them), so a narrower pane would cut off the desks past its edge.
export function fits(bodyColumns: number, desks: number): boolean {
  return bodyColumns >= Math.max(1, desks) * MIN_SLOT
}

export function slotFor(bodyColumns: number, desks: number): number {
  return Math.min(MAX_SLOT, Math.max(MIN_SLOT, Math.floor(bodyColumns / Math.max(1, desks))))
}

export function deskCentre(index: number, slot: number): number {
  return index * slot + Math.floor(slot / 2)
}

export function office(
  desks: Desk[],
  seated: Seated[],
  walker: Walker | undefined,
  slot: number,
  tick: number,
): { columns: number; rows: number; cells: Cell[] } {
  const columns = Math.max(1, desks.length) * slot
  const canvas: Canvas = { width: columns, pixels: new Array(columns * HEIGHT).fill(WALL) }
  room(canvas)

  // Chairs behind the desks show an empty seat; a seated worker hides its chair, and its desk and
  // laptop cover its lower half.
  desks.forEach((_, index) => {
    const centre = deskCentre(index, slot)
    fill(canvas, centre - 3, 15, 6, 1, CHAIR_TOP)
    fill(canvas, centre - 3, 16, 6, 6, CHAIR)
  })
  for (const person of seated) {
    const left = deskCentre(person.desk, slot) - FIGURE_WIDTH / 2
    sprite(canvas, frameOf(person.pose === 'type' ? 'type' : 'idle', tick), left, SEAT_TOP, colorsOf(person), DESK_Y)
  }
  desks.forEach((one, index) => {
    const typing = seated.some(person => person.desk === index && person.pose === 'type')
    desk(canvas, index * slot, slot, one.state, one.color, tick, typing)
  })
  if (walker) sprite(canvas, frameOf(walker.pose, tick), walker.x - FIGURE_WIDTH / 2, WALKER_TOP, colorsOf(walker))

  const cells: Cell[] = []
  for (let row = 0; row < ROWS; row++) {
    for (let x = 0; x < columns; x++) {
      cells.push([0x2580, canvas.pixels[row * 2 * columns + x], canvas.pixels[(row * 2 + 1) * columns + x]])
    }
  }

  desks.forEach((one, index) => {
    // A done desk counts its rounds (x2); a desk at work again shows which round it is on (#2).
    const rounds = one.state === 'active' ? (one.rounds > 0 ? ` #${one.rounds + 1}` : '') : one.rounds > 1 ? `x${one.rounds}` : ''
    const mark = one.state === 'done' ? ' ✓' : one.state === 'returned' ? ' ✗' : ''
    const text = `${one.label.slice(0, slot - 4)}${rounds}${mark}`
    const fg = one.state === 'pending' ? PENDING_TEXT : one.state === 'returned' ? CROSS : one.color
    lay(cells, columns, { row: LABEL_ROW, col: deskCentre(index, slot) - Math.floor([...text].length / 2), text, fg, bg: WALL })
  })
  const flash = (person: Person, bg: number) => (person.isWarned && tick % 2 === 1 ? WARN : bg)
  for (const person of seated) {
    const text = ` ${person.say} `
    lay(cells, columns, { row: BUBBLE_ROW, col: deskCentre(person.desk, slot) - Math.floor(text.length / 2), text, fg: BUBBLE_TEXT, bg: flash(person, person.color) })
  }
  if (walker) {
    const text = ` ${walker.say} `
    lay(cells, columns, { row: CAPTION_ROW, col: walker.x - Math.floor(text.length / 2), text, fg: BUBBLE_TEXT, bg: flash(walker, CAPTION) })
  }
  return { columns, rows: ROWS, cells }
}

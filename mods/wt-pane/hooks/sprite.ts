// Shared helpers for drawing on a Raster: colors, the cell encoding, walking. Pure: no `$`.

// One Raster cell: [codePoint, foreground, background].
export type Cell = [number, number, number]

export const DEFAULT_COLOR = 0x01000000 // the terminal's own color

// '#rrggbb' -> 0x00RRGGBB; anything else -> fallback.
export function hex(text: string | undefined, fallback: number): number {
  return text && /^#[0-9a-fA-F]{6}$/.test(text) ? parseInt(text.slice(1), 16) : fallback
}

// The color with each channel scaled by `factor` (0..1 darkens).
export function shade(color: number, factor: number): number {
  const channel = (shift: number) => Math.min(255, Math.round(((color >> shift) & 255) * factor))
  return (channel(16) << 16) | (channel(8) << 8) | channel(0)
}

// `step` pixels toward the target per tick: a figure walks to where it is going.
export function walk(from: number, to: number, ticks: number, step = 1): number {
  if (ticks <= 0 || from === to) return from
  const by = ticks * step
  return from < to ? Math.min(to, from + by) : Math.max(to, from - by)
}

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

export function base64(bytes: Uint8Array): string {
  let out = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0)
    out += ALPHABET[(n >> 18) & 63] + ALPHABET[(n >> 12) & 63]
    out += i + 1 < bytes.length ? ALPHABET[(n >> 6) & 63] : '='
    out += i + 2 < bytes.length ? ALPHABET[n & 63] : '='
  }
  return out
}

// RasterProps.cells: little-endian u32 triplets [codePoint, foreground, background], base64.
export function encode(cells: Cell[]): string {
  const view = new DataView(new ArrayBuffer(cells.length * 12))
  cells.forEach((cell, index) => cell.forEach((word, k) => view.setUint32(index * 12 + k * 4, word, true)))
  return base64(new Uint8Array(view.buffer))
}

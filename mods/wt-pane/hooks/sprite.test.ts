import { expect, test } from 'claude-code/testing'

import { base64, DEFAULT_COLOR, encode, hex, shade, walk } from './sprite'

test('cells encode as base64 of little-endian u32 triplets', () => {
  // The example in RasterProps: one orange full block on the default background.
  expect(encode([[0x2588, 0xff8800, DEFAULT_COLOR]])).toBe('iCUAAACI/wAAAAAB')
  expect(base64(new Uint8Array([1, 2, 3, 4]))).toBe('AQIDBA==')
  expect(base64(new Uint8Array([1, 2]))).toBe('AQI=')
})

test('hex colors parse, anything else falls back; shade scales each channel', () => {
  expect(hex('#33cc66', 0)).toBe(0x33cc66)
  expect(hex('green', 7)).toBe(7)
  expect(hex(undefined, 7)).toBe(7)
  expect(shade(0x804020, 0.5)).toBe(0x402010)
  expect(shade(0xffffff, 2)).toBe(0xffffff)
})

test('a walker moves `step` pixels a tick toward its target and stops there', () => {
  expect(walk(1, 11, 3)).toBe(4)
  expect(walk(1, 11, 3, 2)).toBe(7)
  expect(walk(1, 11, 30, 2)).toBe(11)
  expect(walk(21, 11, 4)).toBe(17)
  expect(walk(5, 5, 9)).toBe(5)
  expect(walk(5, 9, 0)).toBe(5)
})

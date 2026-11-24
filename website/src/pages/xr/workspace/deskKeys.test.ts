import { describe, expect, it } from 'vitest'
import { KEYS_LINE, cutToFit, keysLine, KEYS, boardSize, deskKeysV, fitsRest, keyRect, keysOf, layerAfter } from './deskKeys'

describe('the keyboard of the room', () => {
  it('has every letter, a space, a backspace and an Enter on its first layer', () => {
    const keys = keysOf('abc').map(k => k.key)
    for (const c of 'abcdefghijklmnopqrstuvwxyz') expect(keys).toContain(c)
    for (const name of [' ', 'Backspace', 'Enter', 'Shift', 'Layer', ',', '.', '?']) expect(keys).toContain(name)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('types capitals after Shift, and every digit on the signs', () => {
    expect(keysOf('ABC').map(k => k.key)).toContain('Q')
    const signs = keysOf('123').map(k => k.key)
    for (const c of '0123456789') expect(signs).toContain(c)
    expect(signs).not.toContain('Shift')
    expect(new Set(signs).size).toBe(signs.length)
  })

  it('lays no key over another, and none past the board, on any layer', () => {
    for (const layer of ['abc', 'ABC', '123'] as const) {
      const cells = new Set<string>()
      for (const key of keysOf(layer)) {
        expect(key.col + key.span).toBeLessThanOrEqual(KEYS.columns)
        expect(key.row).toBeLessThan(KEYS.rows)
        for (let i = 0; i < key.span; i += 1) {
          const cell = `${key.row}:${key.col + i}`
          expect(cells.has(cell), `${layer} ${cell}`).toBe(false)
          cells.add(cell)
        }
        const [w, h] = boardSize(KEYS.deskStepM)
        const r = keyRect(key, KEYS.deskStepM)
        expect(Math.abs(r.x) + r.w / 2).toBeLessThanOrEqual(w / 2)
        expect(Math.abs(r.y) + r.h / 2).toBeLessThanOrEqual(h / 2)
      }
    }
  })

  it('has its far row away from the person and its Enter at the near right', () => {
    const q = keyRect(keysOf('abc').find(k => k.key === 'q')!, 0.04)
    const enter = keyRect(keysOf('abc').find(k => k.key === 'Enter')!, 0.04)
    expect(q.y).toBeGreaterThan(enter.y)
    expect(enter.x).toBeGreaterThan(q.x)
  })

  it('goes back to small letters after one capital, and to and from the signs', () => {
    expect(layerAfter('abc', 'Shift')).toBe('ABC')
    expect(layerAfter('ABC', 'A')).toBe('abc')
    expect(layerAfter('ABC', 'Shift')).toBe('abc')
    expect(layerAfter('abc', 'Layer')).toBe('123')
    expect(layerAfter('123', '1')).toBe('123')
    expect(layerAfter('123', 'Layer')).toBe('abc')
  })

  it('lies whole inside the hands’ rest on the desk', () => {
    expect(fitsRest()).toBe(true)
    expect(deskKeysV()).toBeCloseTo(KEYS.deskNearM + (KEYS.rows * KEYS.deskStepM) / 2)
  })
})

describe('the line over the keys', () => {
  it('shows a short text whole and a long one by its last signs with "…" in front', () => {
    expect(keysLine('')).toBe('')
    expect(keysLine('ops')).toBe('ops')
    expect(keysLine('x'.repeat(KEYS_LINE.signs))).toBe('x'.repeat(KEYS_LINE.signs))
    expect(keysLine(`abc${'y'.repeat(KEYS_LINE.signs)}`)).toBe(`…${'y'.repeat(KEYS_LINE.signs)}`)
  })
  it('cuts more in front while the line has no room, and keeps one sign at least', () => {
    expect(keysLine('abcdefgh', s => s.length <= 4)).toBe('…fgh')
    expect(keysLine('abc', s => s.length <= 3)).toBe('abc')
    expect(keysLine('abcdefgh', () => false)).toBe('…h')
  })
  it('cuts what is said at its end by whole signs, with "…", and leaves a text that fits whole (M68)', () => {
    expect(cutToFit('Another room is named Chat', s => s.length <= 40)).toBe('Another room is named Chat')
    expect(cutToFit('Another room is named Chat', s => s.length <= 13)).toBe('Another room…')
    const flag = '\u{1F1EF}\u{1F1F5}'
    expect(cutToFit(`ab${flag}${flag}cd`, s => s.length <= 8)).toBe(`ab${flag}…`)
  })
  it('never splits a flag or a joined sign', () => {
    const flag = '\u{1F1EF}\u{1F1F5}'
    const shown = keysLine(`${'a'.repeat(KEYS_LINE.signs)}${flag}`)
    expect(shown.endsWith(flag)).toBe(true)
    expect(shown).toBe(`…${'a'.repeat(KEYS_LINE.signs - 1)}${flag}`)
  })
})

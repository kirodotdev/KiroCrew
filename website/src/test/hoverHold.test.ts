import { describe, it, expect } from 'vitest'
import { heldSeat, type HoverPin } from '../pages/chat/hoverHold'

/**
 * Direct tests for the hover-hold seat arithmetic. The component suite exercises
 * this through jsdom, where every height reads 0 and only the ordinal fallback
 * runs — so the pixel path, which is what ships in a browser, is only reachable
 * with the numbers supplied here.
 */

const pin = (over: Partial<HoverPin> = {}): HoverPin => ({
  key: 'b',
  scope: 'list',
  container: 'tree:root',
  seenOrder: ['a', 'b', 'c'],
  heights: { a: 40, b: 40, c: 40 },
  headerPxAbove: 0,
  headerH: 0,
  staleSide: false,
  ...over,
})

const list = (...keys: string[]) => keys.map(key => ({ key }))

describe('heldSeat – pixel anchoring', () => {
  it('re-seats the held row at the offset it was captured at', () => {
    expect(heldSeat(pin(), list('c', 'a', 'b'))).toBe(1)
  })

  it('does not let a taller row sorting in above push the held row down a slot', () => {
    const p = pin({ heights: { a: 40, b: 40, c: 200 } })
    expect(heldSeat(p, list('c', 'a', 'b'))).toBe(0)
  })

  it('keeps the captured offset when a row above the held one closes', () => {
    expect(heldSeat(pin(), list('b', 'c'))).toBe(1)
  })

  it('raises the anchor by the captured header pixels, seating the row lower', () => {
    const base = { key: 'b', seenOrder: ['b', 'c'], heights: { b: 40, c: 40 } }
    expect(heldSeat(pin({ ...base, headerPxAbove: 0 }), list('c', 'b'))).toBe(0)
    expect(heldSeat(pin({ ...base, headerPxAbove: 24 }), list('c', 'b'))).toBe(1)
  })

  it('charges a segment header to each later bucket, seating the row above it', () => {
    const base = { key: 'b', seenOrder: ['c', 'b'], heights: { c: 40, b: 40 } }
    const segmentOf = (s: { key: string }) => (s.key === 'c' ? 'older' : 'today')
    expect(heldSeat(pin({ ...base, headerH: 0 }), list('c', 'b'), segmentOf)).toBe(1)
    expect(heldSeat(pin({ ...base, headerH: 100 }), list('c', 'b'), segmentOf)).toBe(0)
  })
})

describe('heldSeat – degenerate and absent frames', () => {
  it('counts rows instead of pixels when there is no layout to measure', () => {
    const p = pin({ heights: {} })
    expect(heldSeat(p, list('c', 'a', 'b'))).toBe(1)
  })

  it('returns null for a row the captured frame never saw, leaving its live slot', () => {
    expect(heldSeat(pin({ seenOrder: ['a', 'c'] }), list('c', 'a', 'b'))).toBeNull()
  })
})

describe('heldSeat – the frame must cover only the row own container', () => {
  const CONFINED = pin({ key: 'b1', seenOrder: ['b1', 'b2'], heights: { b1: 40, b2: 40 } })

  it('seats the first row of a later container at the top of that container', () => {
    expect(heldSeat(CONFINED, list('b2', 'b1'))).toBe(0)
  })

  it('dumps that row to the bottom once the frame also spans a preceding container', () => {
    const spanning = pin({
      key: 'b1',
      seenOrder: ['a1', 'a2', 'b1', 'b2'],
      heights: { a1: 40, a2: 40, b1: 40, b2: 40 },
    })
    expect(heldSeat(spanning, list('b2', 'b1'))).toBe(1)
  })
})

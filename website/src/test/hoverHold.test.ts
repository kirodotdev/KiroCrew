import { describe, it, expect } from 'vitest'
import { heldSeat, type HoverPin } from '../pages/chat/hoverHold'

/**
 * Direct tests for the hover-hold seat arithmetic. The component suite exercises
 * this through jsdom, where every height reads 0 and only the ordinal fallback
 * runs — so the pixel path, which is what ships in a browser, is only reachable
 * with the numbers supplied here.
 */

/** The raw-key identity space, passed EXPLICITLY at every call.
 *
 * `heldSeat` takes no default for its identity accessor: the sidebar keys rows on
 * `sessionRowIdentity` (origin-qualified for peer rows), so a silent raw-key
 * default would hand a lane whose rows can collide across gateways the wrong
 * space. These tests are the only place the raw-key space is exercised, and they
 * now say so at each call rather than inheriting it.
 */
const rawKey = (s: { key: string }) => s.key

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
    expect(heldSeat(pin(), list('c', 'a', 'b'), rawKey)).toBe(1)
  })

  it('does not let a taller row sorting in above push the held row down a slot', () => {
    const p = pin({ heights: { a: 40, b: 40, c: 200 } })
    expect(heldSeat(p, list('c', 'a', 'b'), rawKey)).toBe(0)
  })

  it('keeps the captured offset when a row above the held one closes', () => {
    expect(heldSeat(pin(), list('b', 'c'), rawKey)).toBe(1)
  })

  it('raises the anchor by the captured header pixels, seating the row lower', () => {
    const base = { key: 'b', seenOrder: ['b', 'c'], heights: { b: 40, c: 40 } }
    expect(heldSeat(pin({ ...base, headerPxAbove: 0 }), list('c', 'b'), rawKey)).toBe(0)
    expect(heldSeat(pin({ ...base, headerPxAbove: 24 }), list('c', 'b'), rawKey)).toBe(1)
  })

  it('charges a segment header to each later bucket, seating the row above it', () => {
    const base = { key: 'b', seenOrder: ['c', 'b'], heights: { c: 40, b: 40 } }
    const segmentOf = (s: { key: string }) => (s.key === 'c' ? 'older' : 'today')
    expect(heldSeat(pin({ ...base, headerH: 0 }), list('c', 'b'), rawKey, segmentOf)).toBe(1)
    expect(heldSeat(pin({ ...base, headerH: 100 }), list('c', 'b'), rawKey, segmentOf)).toBe(0)
  })
})

describe('heldSeat – degenerate and absent frames', () => {
  it('counts rows instead of pixels when there is no layout to measure', () => {
    const p = pin({ heights: {} })
    expect(heldSeat(p, list('c', 'a', 'b'), rawKey)).toBe(1)
  })

  it('returns null for a row the captured frame never saw, leaving its live slot', () => {
    expect(heldSeat(pin({ seenOrder: ['a', 'c'] }), list('c', 'a', 'b'), rawKey)).toBeNull()
  })
})

describe('heldSeat – origin-qualified identity across a colliding key', () => {
  // The pin is captured from `data-session-row`, which is origin-qualified, so
  // `pin.key` and `seenOrder` carry `peer:key` for a peer row. A local row and a
  // peer row can share the same raw key; the `idOf` accessor is what keeps the
  // two apart. Without it (the raw-key default) the peer row and the local row
  // both match, the held row is dropped twice, and the wrong row is reseated.
  const idOf = (s: { key: string; peer_id?: string }) =>
    s.peer_id ? `${s.peer_id}:${s.key}` : s.key
  const collidingList = () => [
    { key: 'x' }, // local row, raw key 'x'
    { key: 'x', peer_id: 'astro' }, // peer row, same raw key
    { key: 'y' },
  ]

  // Degenerate frame (no measured heights) so the seat is a pure ordinal count
  // of rows ranked before the held one — the path where a raw-key collision
  // diverges cleanly. The held row is the PEER `astro:x` at captured index 1.
  const degPin = () => pin({
    key: 'astro:x',
    seenOrder: ['x', 'astro:x', 'y'],
    heights: {},
  })

  it('counts only the local `x` before the held peer row when addressed by qualified id', () => {
    // idOf('x')=0 < mine(1) counts; idOf(peer)='astro:x'=1 not <1; y not <1. Seat 1.
    expect(heldSeat(degPin(), collidingList(), idOf)).toBe(1)
  })

  it('miscounts the colliding peer row as also-before under the raw-key default', () => {
    // Raw default: BOTH the local `x` and the peer row (raw key `x`) resolve to
    // rank 0 < 1, so the held row is counted before itself — seat 2, one slot
    // too low. This is the wrong-row displacement the qualified id prevents.
    expect(heldSeat(degPin(), collidingList(), rawKey)).toBe(2)
  })
})

describe('heldSeat – the frame must cover only the row own container', () => {
  const CONFINED = pin({ key: 'b1', seenOrder: ['b1', 'b2'], heights: { b1: 40, b2: 40 } })

  it('seats the first row of a later container at the top of that container', () => {
    expect(heldSeat(CONFINED, list('b2', 'b1'), rawKey)).toBe(0)
  })

  it('dumps that row to the bottom once the frame also spans a preceding container', () => {
    const spanning = pin({
      key: 'b1',
      seenOrder: ['a1', 'a2', 'b1', 'b2'],
      heights: { a1: 40, a2: 40, b1: 40, b2: 40 },
    })
    expect(heldSeat(spanning, list('b2', 'b1'), rawKey)).toBe(1)
  })
})

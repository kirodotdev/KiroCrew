import { describe, expect, it } from 'vitest'
import { DOM_AIM, distanceTo, pressFate, samePlace, snapOf, type Lit } from './domAim'

describe('domAim', () => {
  it('snaps to the nearest control only when it is near and the second is at least twice as far', () => {
    expect(snapOf([{ it: 'a', d: 10 }, { it: 'b', d: 25 }])).toBe('a')
    expect(snapOf([{ it: 'a', d: 10 }, { it: 'b', d: 15 }])).toBeNull()
    expect(snapOf([{ it: 'a', d: DOM_AIM.snapPx + 1 }])).toBeNull()
    expect(snapOf([{ it: 'b', d: 30 }, { it: 'a', d: 8 }])).toBe('a')
    expect(snapOf([])).toBeNull()
  })

  it('measures a point against a rectangle, and the same place within 4 px', () => {
    expect(distanceTo([10, 10, 20, 20], 15, 15)).toBe(0)
    expect(distanceTo([10, 10, 20, 20], 0, 10)).toBe(10)
    expect(samePlace([0, 0, 10, 10], [4, -4, 10, 10])).toBe(true)
    expect(samePlace([0, 0, 10, 10], [5, 0, 10, 10])).toBe(false)
  })

  it('a pinch presses only the lit control, still for 300 ms, at the same place, and calm for a second', () => {
    const el = {} as Element
    const other = {} as Element
    const lit: Lit = { el, rect: [0, 0, 10, 10], snapped: false, since: 1000, movedAt: -Infinity }
    expect(pressFate(null, null, 2000, null)).toBe('nothing-lit')
    expect(pressFate(lit, { el: other, rect: [0, 0, 10, 10] }, 2000, null)).toBe('not-the-lit')
    expect(pressFate(lit, { el, rect: [6, 0, 10, 10] }, 2000, null)).toBe('moved')
    expect(pressFate(lit, { el, rect: [0, 0, 10, 10] }, 1200, null)).toBe('too-soon')
    expect(pressFate(lit, { el, rect: [0, 0, 10, 10] }, 2000, 1500)).toBe('came')
    expect(pressFate({ ...lit, movedAt: 1500 }, { el, rect: [0, 0, 10, 10] }, 2000, null)).toBe('moved')
    expect(pressFate(lit, { el, rect: [2, 0, 10, 10] }, 1000 + DOM_AIM.stillMs, null)).toBeNull()
  })
})

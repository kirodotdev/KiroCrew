import { describe, expect, it } from 'vitest'
import { PAGES } from './layout'
import { TWO, twoBeforeOf, twoOf, twoPair, twoWay, widthDeg, type TwoWay } from './twoUp'

describe('two in view', () => {
  it('the ruled way with no word or any other word; way a only with its word', () => {
    expect(twoWay('')).toBe('d')
    expect(twoWay('?two=b')).toBe('d')
    expect(twoWay('?two=c')).toBe('d')
    expect(twoWay('?view=pages&two=a&fps=1')).toBe('a')
  })

  it.each(['a', 'd'] as TwoWay[])('way %s: both pages are inside the lens, side by side, with the gap between', way => {
    const [left, right] = twoPair(way)
    const l = left.at.yawDeg - widthDeg(left.css[0], left.at.distanceM) / 2
    const r = right.at.yawDeg + widthDeg(right.css[0], right.at.distanceM) / 2
    expect(l).toBeGreaterThanOrEqual(-TWO.lensDeg / 2)
    expect(r).toBeLessThanOrEqual(TWO.lensDeg / 2)
    const between = right.at.yawDeg - widthDeg(right.css[0], right.at.distanceM) / 2 - (left.at.yawDeg + widthDeg(left.css[0], left.at.distanceM) / 2)
    expect(between).toBeCloseTo(TWO.gapDeg, 5)
    expect(left.at.distanceM).toBe(right.at.distanceM)
    expect(left.at.pitchDeg).toBe(TWO.pitchDeg)
  })

  it('the ruled way keeps the desktop width and stands farther; way a keeps the distance', () => {
    const [d] = twoPair('d')
    expect(d.css[0]).toBe(800)
    expect(d.at.distanceM).toBeGreaterThan(PAGES.distanceM)
    const [a] = twoPair('a')
    expect(a.css[0]).toBe(600)
    expect(a.at.distanceM).toBe(PAGES.distanceM)
  })

  it('the pair is the page ahead of him and the page he opened last, the left one first', () => {
    const pages = [{ id: 'page', yawDeg: 0 }, { id: 'open-1', yawDeg: -60 }, { id: 'open-2', yawDeg: 60 }]
    expect(twoOf(pages, 0)).toEqual(['page', 'open-2'])
    // He looks at the page on his left: that one and the last one.
    expect(twoOf(pages, -55)).toEqual(['open-1', 'open-2'])
    // He looks at the last one: it and the one before it.
    expect(twoOf(pages, 58)).toEqual(['open-1', 'open-2'])
    expect(twoOf(pages.slice(0, 2), 0)).toEqual(['open-1', 'page'])
    expect(twoOf(pages.slice(0, 1), 0)).toBeNull()
    expect(twoOf([], 0)).toBeNull()
  })

  it('reads what the record kept of a pair with care', () => {
    expect(twoBeforeOf({ at: { yawDeg: 60, pitchDeg: 0, distanceM: 1.75 }, css: [1024, 720], byHand: true })).toEqual({ at: { yawDeg: 60, pitchDeg: 0, distanceM: 1.75 }, css: [1024, 720], byHand: true })
    expect(twoBeforeOf({ at: { yawDeg: 60, pitchDeg: 0, distanceM: 1.75 }, css: [1024, 720] })?.byHand).toBe(false)
    expect(twoBeforeOf({ at: { yawDeg: 999, pitchDeg: 0, distanceM: 1.75 }, css: [1024, 720] })).toBeUndefined()
    expect(twoBeforeOf({ at: { yawDeg: 0, pitchDeg: 0, distanceM: 1.75 }, css: [10, 720] })).toBeUndefined()
    expect(twoBeforeOf(null)).toBeUndefined()
    expect(twoBeforeOf('pair')).toBeUndefined()
  })
})

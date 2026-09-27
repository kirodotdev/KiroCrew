// Unit coverage for the pure rules the virtualizer's owners share: the window
// rules in WindowCalculator, the retirement plan in HeightIndex, the reader-row
// geometry in anchorGeometry, and the collapsed-box predicate in
// FollowController. The hook suites exercise each of them through the hook;
// these pin the rules directly, including the edges a transcript rarely reaches.

import { describe, it, expect } from 'vitest'
import { initialWindow, tailWindow, jumpIsNear, mergeWindowRange } from '../hooks/virtualizer/WindowCalculator'
import { planHeightRetirement } from '../hooks/virtualizer/HeightIndex'
import { captureTopAnchorFrom, rowTopFrom, captureAnchorCandsFrom } from '../hooks/virtualizer/anchorGeometry'
import { scrollerCollapsed } from '../hooks/virtualizer/FollowController'

describe('initialWindow / tailWindow', () => {
  it('opens a chat at its tail and a list at its head', () => {
    expect(initialWindow(100, 5, 'bottom')).toEqual({ start: 94, end: 100 })
    expect(initialWindow(100, 5, 'top')).toEqual({ start: 0, end: 6 })
  })

  it('clamps a list shorter than the window', () => {
    expect(initialWindow(3, 5, 'bottom')).toEqual({ start: 0, end: 3 })
    expect(initialWindow(3, 5, 'top')).toEqual({ start: 0, end: 3 })
    expect(initialWindow(0, 5, 'bottom')).toEqual({ start: 0, end: 0 })
  })

  it('agrees with the tail window an explicit bottom placement remounts', () => {
    for (const n of [0, 1, 5, 6, 7, 250]) {
      expect(tailWindow(n, 5)).toEqual(initialWindow(n, 5, 'bottom'))
    }
  })
})

describe('jumpIsNear', () => {
  const range = { start: 100, end: 110 }
  it('is near within four overscan windows of either edge, far beyond', () => {
    // overscan 5 -> a 20-row band on each side.
    expect(jumpIsNear({ start: 125, end: 135 }, range, 5)).toBe(true)
    expect(jumpIsNear({ start: 130, end: 140 }, range, 5)).toBe(true)
    expect(jumpIsNear({ start: 131, end: 141 }, range, 5)).toBe(false)
    expect(jumpIsNear({ start: 70, end: 80 }, range, 5)).toBe(true)
    expect(jumpIsNear({ start: 69, end: 79 }, range, 5)).toBe(false)
  })
})

describe('mergeWindowRange', () => {
  const prev = { start: 20, end: 40 }

  it('returns the SAME object when nothing moved, so a state update bails out', () => {
    expect(mergeWindowRange(prev, { start: 20, end: 40 }, false)).toBe(prev)
    expect(mergeWindowRange(prev, { start: 22, end: 38 }, false)).toBe(prev)
    expect(mergeWindowRange(prev, { start: 25, end: 35 }, true)).toBe(prev)
  })

  it('mounts eagerly and unmounts only past the hysteresis band', () => {
    expect(mergeWindowRange(prev, { start: 19, end: 41 }, false)).toEqual({ start: 19, end: 41 })
    // Four rows of drift keep the edge; the fifth adopts it.
    expect(mergeWindowRange(prev, { start: 24, end: 36 }, false)).toBe(prev)
    expect(mergeWindowRange(prev, { start: 25, end: 35 }, false)).toEqual({ start: 25, end: 35 })
  })

  it('only ever grows under expandOnly', () => {
    expect(mergeWindowRange(prev, { start: 30, end: 50 }, true)).toEqual({ start: 20, end: 50 })
    expect(mergeWindowRange(prev, { start: 10, end: 30 }, true)).toEqual({ start: 10, end: 40 })
  })
})

describe('planHeightRetirement', () => {
  type Row = { key: string; stable: string }
  const k = (r: Row) => r.key
  const s = (r: Row) => r.stable
  const rows = (...spec: [string, string][]) => spec.map(([key, stable]) => ({ key, stable }))
  const plan = (
    prev: Row[], next: Row[], opts: { stable?: boolean; paging?: boolean } = {},
  ) => planHeightRetirement({
    prevItems: prev,
    prevGetKey: k,
    items: next,
    getKey: k,
    survivingKeys: new Set(next.map(k)),
    getStableId: opts.stable ? s : undefined,
    pagingConsumer: opts.paging ?? false,
    countFell: next.length < prev.length,
  })

  it('retires a row that left, and keeps survivors', () => {
    const prev = rows(['a', 'A'], ['b', 'B'], ['c', 'C'])
    expect(plan(prev, rows(['a', 'A'], ['c', 'C']))).toEqual({ renamed: [], retired: ['b'] })
  })

  it('renames a row that survived under a new display key instead of retiring it', () => {
    const prev = rows(['a', 'A'], ['b', 'B'])
    expect(plan(prev, rows(['a', 'A'], ['b2', 'B']), { stable: true })).toEqual({ renamed: [['b', 'b2']], retired: [] })
    // Without a stable id there is nothing to say it survived.
    expect(plan(prev, rows(['a', 'A'], ['b2', 'B']))).toEqual({ renamed: [], retired: ['b'] })
  })

  it('exempts a head page-out in a paging consumer, and only that', () => {
    const prev = rows(['a', 'A'], ['b', 'B'], ['c', 'C'])
    const pagedOut = rows(['c', 'C'])
    expect(plan(prev, pagedOut, { paging: true })).toEqual({ renamed: [], retired: [] })
    // The same shape retires in a consumer that never pages (a filtered list).
    expect(plan(prev, pagedOut)).toEqual({ renamed: [], retired: ['a', 'b'] })
    // A full clear leaves no survivor, so its rows are not coming back.
    expect(plan(prev, [], { paging: true })).toEqual({ renamed: [], retired: ['a', 'b', 'c'] })
    // An interior removal is not a prefix.
    expect(plan(prev, rows(['a', 'A'], ['c', 'C']), { paging: true })).toEqual({ renamed: [], retired: ['b'] })
  })
})

describe('anchorGeometry', () => {
  /** A node whose rect sits at `top` with `height`, relative to a scroller at 100. */
  const node = (top: number, height: number) => {
    const el = document.createElement('div')
    el.getBoundingClientRect = () => ({ top: 100 + top, bottom: 100 + top + height, height } as DOMRect)
    return el
  }
  const scroller = () => {
    const el = document.createElement('div')
    el.getBoundingClientRect = () => ({ top: 100, bottom: 500, height: 400 } as DOMRect)
    return el
  }

  it('captures the topmost visible row, skipping rows above the viewport and unkeyed rows', () => {
    const entries: [Element, number][] = [[node(-80, 50), 0], [node(-30, 60), 1], [node(30, 60), 2]]
    expect(captureTopAnchorFrom(scroller(), entries, (i) => `k${i}`)).toEqual({ key: 'k1', top: -30, index: 1 })
    expect(captureTopAnchorFrom(scroller(), entries, (i) => (i === 1 ? null : `k${i}`))).toEqual({ key: 'k2', top: 30, index: 2 })
  })

  it('locates a mounted row by key, or reports it unmounted', () => {
    const entries: [Element, number][] = [[node(10, 50), 4], [node(60, 50), 5]]
    expect(rowTopFrom(scroller(), entries, (i) => `k${i}`, 'k5')).toBe(60)
    expect(rowTopFrom(scroller(), entries, (i) => `k${i}`, 'k9')).toBeNull()
  })

  it('collects up to three visible, laid-out candidates in screen order', () => {
    const entries: [Element, number][] = [
      [node(200, 40), 3], [node(0, 40), 1], [node(-100, 40), 0], [node(100, 0), 2], [node(300, 40), 4], [node(250, 40), 5],
    ]
    const items = ['a', 'b', 'c', 'd', 'e', undefined]
    const cands = captureAnchorCandsFrom(scroller(), entries, (i) => items[i], (it, i) => `${it}${i}`)
    // Row 0 is above the viewport, row 2 has no height, row 5 has no item.
    expect(cands).toEqual([{ key: 'b1', top: 0 }, { key: 'd3', top: 200 }, { key: 'e4', top: 300 }])
  })

  it('answers nothing in an environment without layout', () => {
    const bare = { } as unknown as HTMLDivElement
    expect(captureTopAnchorFrom(bare, [], () => 'k')).toBeNull()
    expect(rowTopFrom(bare, [], () => 'k', 'k')).toBeNull()
    expect(captureAnchorCandsFrom(bare as HTMLElement, [], () => 'x', () => 'k')).toEqual([])
  })
})

describe('scrollerCollapsed', () => {
  it('is true only for a zero-height box', () => {
    expect(scrollerCollapsed({ clientHeight: 0 })).toBe(true)
    expect(scrollerCollapsed({ clientHeight: 1 })).toBe(false)
  })
})

/**
 * Session ordering — the comparator shared by the sidebar and the collapsed
 * sidebar's hover flyout.
 *
 * Locks the contract:
 *  (1) `date-desc` ranks by last activity, using the modified → last_ts →
 *      created fallback ladder, mixing epoch-seconds and ISO sources.
 *  (2) A session with no usable timestamp sorts last, never first.
 *  (3) Pin priority wraps the sort, so a pinned row cannot change position
 *      between the two surfaces that both apply it.
 *  (4) `created-*` uses byte order (ISO is chronological), so ordering does
 *      not shift with the app language.
 */
import { describe, it, expect } from 'vitest'
import { compareBySort, comparePinnedThenSort, lastActivityEpoch } from '../pages/chat/sessionOrder'
import type { Sortable } from '../pages/chat/sessionOrder'

const order = (items: Sortable[], key: Parameters<typeof compareBySort>[2] = 'date-desc') =>
  [...items].sort((a, b) => compareBySort(a, b, key)).map(s => s.key)

const pinnedOrder = (items: Sortable[], pinned: string[]) =>
  [...items].sort((a, b) => comparePinnedThenSort(a, b, 'date-desc', new Set(pinned))).map(s => s.key)

describe('lastActivityEpoch', () => {
  it('prefers modified, then last_ts, then created', () => {
    expect(lastActivityEpoch({ key: 'a', modified: 500, last_ts: '2026-01-01T00:00:00Z', created: '2020-01-01T00:00:00Z' })).toBe(500)
    expect(lastActivityEpoch({ key: 'b', last_ts: '2026-01-01T00:00:00Z', created: '2020-01-01T00:00:00Z' }))
      .toBe(Date.parse('2026-01-01T00:00:00Z') / 1000)
    expect(lastActivityEpoch({ key: 'c', created: '2020-01-01T00:00:00Z' }))
      .toBe(Date.parse('2020-01-01T00:00:00Z') / 1000)
  })

  it('returns 0 for a session with no timestamp at all', () => {
    expect(lastActivityEpoch({ key: 'z' })).toBe(0)
  })
})

describe('compareBySort date-desc', () => {
  it('ranks newest first across mixed epoch and ISO sources', () => {
    // Deliberately mixed: history items carry epoch seconds, active slots carry
    // ISO. Both surfaces feed this comparator, so it must rank them together.
    const items: Sortable[] = [
      { key: 'iso-old', last_ts: '2026-08-01T10:00:00Z' },
      { key: 'epoch-new', modified: Date.parse('2026-08-05T10:00:00Z') / 1000 },
      { key: 'iso-new', last_ts: '2026-08-04T10:00:00Z' },
      { key: 'created-only', created: '2026-07-01T10:00:00Z' },
    ]
    expect(order(items)).toEqual(['epoch-new', 'iso-new', 'iso-old', 'created-only'])
  })

  it('sorts a timestampless session last, not first', () => {
    // The 0 fallback must not read as "epoch 1970 is oldest, therefore first"
    // under desc — that would park a broken row at the top of a recents list.
    const items: Sortable[] = [
      { key: 'no-ts' },
      { key: 'has-ts', last_ts: '2026-08-05T10:00:00Z' },
    ]
    expect(order(items)).toEqual(['has-ts', 'no-ts'])
  })

  it('date-asc is the exact reverse', () => {
    const items: Sortable[] = [
      { key: 'a', last_ts: '2026-08-01T00:00:00Z' },
      { key: 'b', last_ts: '2026-08-03T00:00:00Z' },
      { key: 'c', last_ts: '2026-08-02T00:00:00Z' },
    ]
    expect(order(items, 'date-asc')).toEqual([...order(items, 'date-desc')].reverse())
  })
})

describe('comparePinnedThenSort', () => {
  it('puts pinned sessions first even when they are the least recent', () => {
    const items: Sortable[] = [
      { key: 'newest', last_ts: '2026-08-05T10:00:00Z' },
      { key: 'ancient', last_ts: '2020-01-01T10:00:00Z' },
      { key: 'middle', last_ts: '2026-08-03T10:00:00Z' },
    ]
    expect(pinnedOrder(items, ['ancient'])).toEqual(['ancient', 'newest', 'middle'])
  })

  it('still ranks by recency within the pinned group and within the rest', () => {
    const items: Sortable[] = [
      { key: 'pin-old', last_ts: '2026-08-01T00:00:00Z' },
      { key: 'free-old', last_ts: '2026-08-02T00:00:00Z' },
      { key: 'pin-new', last_ts: '2026-08-04T00:00:00Z' },
      { key: 'free-new', last_ts: '2026-08-05T00:00:00Z' },
    ]
    expect(pinnedOrder(items, ['pin-old', 'pin-new']))
      .toEqual(['pin-new', 'pin-old', 'free-new', 'free-old'])
  })

  it('is a no-op wrapper when nothing is pinned', () => {
    const items: Sortable[] = [
      { key: 'a', last_ts: '2026-08-01T00:00:00Z' },
      { key: 'b', last_ts: '2026-08-05T00:00:00Z' },
    ]
    expect(pinnedOrder(items, [])).toEqual(order(items))
  })
})

describe('compareBySort created-*', () => {
  it('orders ISO created strings by byte order, newest first under desc', () => {
    const items: Sortable[] = [
      { key: 'mid', created: '2026-08-03T00:00:00Z' },
      { key: 'new', created: '2026-08-05T00:00:00Z' },
      { key: 'old', created: '2026-08-01T00:00:00Z' },
    ]
    expect(order(items, 'created-desc')).toEqual(['new', 'mid', 'old'])
    expect(order(items, 'created-asc')).toEqual(['old', 'mid', 'new'])
  })

  it('does not consult last_ts — created sorts are about creation only', () => {
    const items: Sortable[] = [
      { key: 'created-first-active-last', created: '2026-08-05T00:00:00Z', last_ts: '2026-08-01T00:00:00Z' },
      { key: 'created-last-active-first', created: '2026-08-01T00:00:00Z', last_ts: '2026-08-09T00:00:00Z' },
    ]
    expect(order(items, 'created-desc')).toEqual(['created-first-active-last', 'created-last-active-first'])
    expect(order(items, 'date-desc')).toEqual(['created-last-active-first', 'created-first-active-last'])
  })
})

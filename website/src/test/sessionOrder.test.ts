/**
 * Session ordering — the comparator shared by the sidebar and the collapsed
 * sidebar's hover flyout.
 *
 * Locks the contract:
 *  (1) `date-desc` ranks by last activity, using the modified → last_turn_ts →
 *      last_ts → created fallback ladder, mixing epoch-seconds and ISO sources.
 *  (2) A session with no usable timestamp sorts last, never first.
 *  (3) Pin priority wraps the sort, so a pinned row cannot change position
 *      between the two surfaces that both apply it.
 *  (4) `created-*` uses byte order (ISO is chronological), so ordering does
 *      not shift with the app language.
 *  (5) A running session ranks by `last_turn_ts` (its prompt), so mid-turn rows
 *      moving `last_ts` cannot reshuffle the list.
 *  (6) `fmtRelativeTime` classifies a row by the difference in LOCAL CALENDAR
 *      DAYS between it and now, holding no state between calls, so the label
 *      follows the active timezone immediately and cannot be served from a day
 *      that has stopped being true.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { compareBySort, comparePinnedThenSort, fmtRelativeTime, lastActivityEpoch, slotActivityTs } from '../pages/chat/sessionOrder'
import type { Sortable } from '../pages/chat/sessionOrder'

const order = (items: Sortable[], key: Parameters<typeof compareBySort>[2] = 'date-desc') =>
  [...items].sort((a, b) => compareBySort(a, b, key)).map(s => s.key)

const pinnedOrder = (items: Sortable[], pinned: string[]) =>
  [...items].sort((a, b) => comparePinnedThenSort(a, b, 'date-desc', new Set(pinned))).map(s => s.key)

describe('lastActivityEpoch', () => {
  it('prefers modified, then last_turn_ts, then last_ts, then created', () => {
    expect(lastActivityEpoch({ key: 'a', modified: 500, last_ts: '2026-01-01T00:00:00Z', created: '2020-01-01T00:00:00Z' })).toBe(500)
    expect(lastActivityEpoch({ key: 'b', last_ts: '2026-01-01T00:00:00Z', created: '2020-01-01T00:00:00Z' }))
      .toBe(Date.parse('2026-01-01T00:00:00Z') / 1000)
    expect(lastActivityEpoch({ key: 'c', created: '2020-01-01T00:00:00Z' }))
      .toBe(Date.parse('2020-01-01T00:00:00Z') / 1000)
    // The settled instant WINS over the newest row: a running session's last_ts
    // is a mid-turn tool call, and ranking by it is the churn this ladder exists
    // to avoid.
    expect(lastActivityEpoch({ key: 'd', last_turn_ts: '2026-01-01T00:00:00Z', last_ts: '2026-01-01T09:00:00Z' }))
      .toBe(Date.parse('2026-01-01T00:00:00Z') / 1000)
  })

  it('returns 0 for a session with no timestamp at all', () => {
    expect(lastActivityEpoch({ key: 'z' })).toBe(0)
  })

  it('returns 0 for an unparseable timestamp instead of NaN', () => {
    // NaN makes every comparison false, which leaves the WHOLE list in an
    // arbitrary order rather than misplacing just the one broken row.
    expect(lastActivityEpoch({ key: 'bad', last_turn_ts: 'not a date' })).toBe(0)
  })
})

describe('slotActivityTs', () => {
  it('is the settled instant, so a row is labelled with what it was sorted by', () => {
    expect(slotActivityTs({ last_turn_ts: 'A', last_ts: 'B', created: 'C' })).toBe('A')
    expect(slotActivityTs({ last_ts: 'B', created: 'C' })).toBe('B')
    expect(slotActivityTs({ created: 'C' })).toBe('C')
    expect(slotActivityTs({})).toBeUndefined()
  })
})

describe('a running session does not reshuffle mid-turn', () => {
  it('keeps its position while last_ts advances past a newer session', () => {
    // The reported bug: two agents working at once swap places in the sidebar on
    // every streamed tool call. Ranked by the settled instant, the order is the
    // order the two turns were REQUESTED in and stays put.
    const running = { key: 'running', last_turn_ts: '2026-08-05T10:00:00Z', last_ts: '2026-08-05T10:00:00Z' }
    const idle = { key: 'idle', last_turn_ts: '2026-08-05T11:00:00Z', last_ts: '2026-08-05T11:00:00Z' }
    expect(order([running, idle])).toEqual(['idle', 'running'])
    // …a tool call lands in `running`, moving only last_ts.
    expect(order([{ ...running, last_ts: '2026-08-05T12:00:00Z' }, idle])).toEqual(['idle', 'running'])
  })

  it('re-ranks once the turn completes and the settled instant moves', () => {
    const done = { key: 'running', last_turn_ts: '2026-08-05T12:00:00Z', last_ts: '2026-08-05T12:00:00Z' }
    const idle = { key: 'idle', last_turn_ts: '2026-08-05T11:00:00Z', last_ts: '2026-08-05T11:00:00Z' }
    expect(order([done, idle])).toEqual(['running', 'idle'])
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

/**
 * Count `new Date(...)` constructions performed inside `fn`.
 *
 * Per-call cost is not visible in the string `fmtRelativeTime` returns, so the
 * allocation count is the direct evidence of it. The subclass is restored in
 * `finally` so a failed assertion cannot leak it into a later test. `Date.now()`
 * and `Date.UTC()` are statics and are inherited, so reading the clock and
 * projecting a calendar day are deliberately not counted — only allocation is.
 */
function countDateConstructions(fn: () => void): number {
  const Real = globalThis.Date
  let made = 0
  class Counting extends Real {
    constructor(...args: ConstructorParameters<typeof Date>) {
      // @ts-expect-error variadic forwarding into the Date constructor overloads
      super(...args)
      made++
    }
  }
  globalThis.Date = Counting as unknown as DateConstructor
  try {
    fn()
  } finally {
    globalThis.Date = Real
  }
  return made
}

/** Run `fn` with the process timezone set to `tz`, restoring it afterwards.
 *  A real zone, not a stubbed offset: only a real zone moves what the `Date`
 *  constructor itself produces, which is what the label is derived from. */
function inZone<T>(tz: string, fn: () => T): T {
  const prev = process.env.TZ
  process.env.TZ = tz
  try {
    return fn()
  } finally {
    process.env.TZ = prev
  }
}

/** The catalog's rendering of the yesterday prefix, read out of a row that is
 *  unambiguously yesterday in `tz`, so assertions do not hardcode a locale. */
const yesterdayPrefixIn = (tz: string, ts: string) =>
  inZone(tz, () => fmtRelativeTime(ts)).split(' ')[0]

describe('fmtRelativeTime day boundaries', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('costs a flat two Date allocations per row, with no warm-up pass', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-17T12:00:00Z'))
    const rows = Array.from({ length: 20 }, (_, i) => `2026-08-17T0${i % 9}:30:00Z`)

    const first = countDateConstructions(() => {
      for (const ts of rows) fmtRelativeTime(ts)
    })
    const second = countDateConstructions(() => {
      for (const ts of rows) fmtRelativeTime(ts)
    })

    // One Date for the row, one for the clock; `Date.UTC` allocates nothing. Flat
    // cost means no retained instant exists that a later call could find stale.
    expect(first).toEqual(rows.length * 2)
    expect(second).toEqual(first)
  })

  it('reclassifies a timestamp once the local day rolls over', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-17T12:00:00Z'))
    const ts = '2026-08-17T10:00:00Z'
    const asToday = fmtRelativeTime(ts)

    vi.setSystemTime(new Date('2026-08-18T00:30:00Z'))
    const asYesterday = fmtRelativeTime(ts)

    expect(asYesterday).not.toEqual(asToday)
    // The yesterday branch appends the same clock time, so this holds whatever
    // the catalog renders for the label and whatever locale is active.
    expect(asYesterday).toContain(asToday)
  })

  it('does not reuse a future day when the clock moves backwards', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-17T12:00:00Z'))
    const asToday = fmtRelativeTime('2026-08-17T10:00:00Z')

    vi.setSystemTime(new Date('2026-08-20T12:00:00Z'))
    fmtRelativeTime('2026-08-20T10:00:00Z')

    vi.setSystemTime(new Date('2026-08-17T12:00:00Z'))
    // Held at the Aug 20 day, Aug 17 would fall in the within-6-days branch and
    // gain a weekday prefix.
    expect(fmtRelativeTime('2026-08-17T10:00:00Z')).toEqual(asToday)
  })

  it('classifies by the local day of the active zone, an hour apart all year', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-06T12:00:00Z'))
    // 18:30Z is 23:30 on Sep 5 in Almaty (UTC+5) but 00:30 on Sep 6 in Bishkek
    // (UTC+6), so the same instant is yesterday in one zone and today in the other.
    const ts = '2026-09-05T18:30:00Z'
    const yesterday = yesterdayPrefixIn('Asia/Almaty', '2026-09-05T06:00:00Z')

    expect(inZone('Asia/Almaty', () => fmtRelativeTime(ts)).startsWith(yesterday)).toBe(true)
    expect(inZone('Asia/Bishkek', () => fmtRelativeTime(ts)).startsWith(yesterday)).toBe(false)
  })

  it('follows a zone whose midnight offset differs from its offset at noon', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-02-15T12:00:00Z'))
    // Casablanca is +00:00 by noon on this date but +01:00 at local midnight, so
    // 23:30Z is Feb 14 in UTC and Feb 15 there while both agree on the time of day.
    const ts = '2026-02-14T23:30:00Z'
    const yesterday = yesterdayPrefixIn('UTC', '2026-02-14T06:00:00Z')

    expect(inZone('UTC', () => fmtRelativeTime(ts)).startsWith(yesterday)).toBe(true)
    expect(inZone('Africa/Casablanca', () => fmtRelativeTime(ts)).startsWith(yesterday)).toBe(false)
  })

  it('follows a zone that agrees on today but not on when yesterday began', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-06T12:00:00Z'))
    // The zones agree at this instant and at today's midnight, diverging only
    // further back: 05:30Z is Sep 5 in Winnipeg but still Sep 4 on Easter.
    const ts = '2026-09-05T05:30:00Z'
    const yesterday = yesterdayPrefixIn('America/Winnipeg', '2026-09-05T12:00:00Z')

    const winnipeg = inZone('America/Winnipeg', () => fmtRelativeTime(ts))
    const easter = inZone('Pacific/Easter', () => fmtRelativeTime(ts))

    expect(winnipeg.startsWith(yesterday)).toBe(true)
    expect(easter.startsWith(yesterday)).toBe(false)
    expect(easter).not.toEqual(winnipeg)
  })

  it('still returns empty for a missing or unparseable timestamp', () => {
    expect(fmtRelativeTime(undefined)).toEqual('')
    expect(fmtRelativeTime('not-a-date')).toEqual('')
  })
})

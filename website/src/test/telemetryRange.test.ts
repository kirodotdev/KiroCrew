import { describe, it, expect } from 'vitest'

import {
  DEFAULT_RANGE,
  RANGE_CHOICES,
  customDays,
  earliestDay,
  inclusiveEnd,
  localDay,
  measuredQuery,
  parseLocalDay,
  rangeQuery,
  rangeSearch,
} from '../pages/telemetryRange'

const NOW = new Date(2026, 9, 1, 15, 30) // 1 Oct 2026, 15:30 local

describe('telemetryRange', () => {
  it('offers Default, the four brief presets, the 90-day ceiling, and a custom range', () => {
    expect(RANGE_CHOICES).toEqual(['default', '24h', '7d', '14d', '30d', '90d', 'custom'])
    expect(DEFAULT_RANGE).toBe('default')
  })

  it('asks for no window at all on Default', () => {
    expect(rangeQuery('default', '2026-01-01', '2026-01-02', NOW)).toBeNull()
    expect(rangeSearch(null)).toBe('')
  })

  it('asks for a rolling length for every preset', () => {
    expect(rangeQuery('24h', '', '', NOW)).toEqual({ days: 1 })
    expect(rangeQuery('30d', '2026-01-01', '2026-01-02', NOW)).toEqual({ days: 30 })
    expect(rangeSearch({ days: 14 })).toBe('days=14')
  })

  it('sends a custom range as whole local days, end day included', () => {
    const q = rangeQuery('custom', '2026-09-01', '2026-09-03', NOW)
    expect(q).toEqual({
      since: new Date(2026, 8, 1).getTime() / 1000,
      until: new Date(2026, 8, 4).getTime() / 1000,
    })
    expect(rangeSearch(q)).toBe(`since=${new Date(2026, 8, 1).getTime() / 1000}&until=${new Date(2026, 8, 4).getTime() / 1000}`)
  })

  it('fills a blank custom range with the week ending today', () => {
    expect(customDays('', '', NOW)).toEqual({ since: '2026-09-25', until: '2026-10-01' })
    expect(customDays('', '2026-09-10', NOW)).toEqual({ since: '2026-09-04', until: '2026-09-10' })
  })

  it('rejects a date that does not exist rather than rolling it over', () => {
    expect(parseLocalDay('2026-02-31')).toBeNull()
    expect(parseLocalDay('soon')).toBeNull()
    expect(localDay(parseLocalDay('2026-02-28') as Date)).toBe('2026-02-28')
  })

  it('labels a window ending at midnight with the day before it', () => {
    const end = new Date(2026, 8, 4).toISOString()
    expect(localDay(inclusiveEnd(end))).toBe('2026-09-03')
  })

  it('offers an earliest custom day that keeps the range to 90 inclusive dates', () => {
    const earliest = earliestDay(90, NOW)
    expect(localDay(earliest)).toBe('2026-07-04')
    // Earliest through today, both ends included: exactly the 90-day ceiling.
    const q = rangeQuery('custom', localDay(earliest), localDay(NOW), NOW) as { since: number; until: number }
    expect(Math.round((q.until - q.since) / 86400)).toBe(90)
    // And the whole first day lies after the gateway's `now - 90 days` floor.
    expect(q.since * 1000).toBeGreaterThan(NOW.getTime() - 90 * 86_400_000)
  })

  it('keeps the earliest day inside the 90x24h floor across a DST fall-back', () => {
    // New York falls back on 1 Nov 2026, so the 89 calendar days before 23:30
    // that night hold one extra hour: the earliest day's local midnight would
    // sit 90 days and 30 minutes back, before the gateway's `now - 90 days`
    // floor, and the gateway would cut the first half hour off a day the label
    // still names whole. That day moves up instead.
    const prev = process.env.TZ
    process.env.TZ = 'America/New_York'
    try {
      const now = new Date(2026, 10, 1, 23, 30)
      const floor = now.getTime() - 90 * 86_400_000
      const earliest = earliestDay(90, now)
      expect(earliest.getTime()).toBeGreaterThanOrEqual(floor)
      expect(localDay(earliest)).toBe('2026-08-05')
      // A typed start before it moves up to it, so the query never crosses the floor.
      const q = rangeQuery('custom', '2026-08-04', localDay(now), now) as { since: number; until: number }
      expect(q.since * 1000).toBeGreaterThanOrEqual(floor)
      // Outside the DST edge the earliest day is still `maxDays - 1` days back.
      expect(localDay(earliestDay(90, new Date(2026, 10, 1, 0, 30)))).toBe('2026-08-04')
    } finally {
      process.env.TZ = prev
    }
  })

  it('moves a typed start before the ceiling up to the earliest day', () => {
    expect(customDays('2026-01-01', '2026-10-01', NOW)).toEqual({ since: '2026-07-04', until: '2026-10-01' })
    // An end that falls before the moved start moves with it.
    expect(customDays('2026-01-01', '2026-02-01', NOW)).toEqual({ since: '2026-07-04', until: '2026-07-04' })
  })

  it('reads the measured window back out of a payload', () => {
    expect(measuredQuery({ window_days: 30, window_rolling: true })).toEqual({ days: 30 })
    expect(
      measuredQuery({
        window_rolling: false,
        window_start: '2026-09-01T00:00:00Z',
        window_end: '2026-09-02T15:31:00.000000Z',
      }),
    ).toEqual({ since: Date.UTC(2026, 8, 1) / 1000, until: Date.UTC(2026, 8, 2, 15, 31) / 1000 })
    expect(measuredQuery({ window_rolling: false, window_start: 'x', window_end: 'y' })).toBeNull()
    expect(measuredQuery({})).toBeNull()
  })
})

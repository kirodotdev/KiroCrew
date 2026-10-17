import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { parseCronSlots } from '../components/WeekGrid'
import { convertCronTime, tzOffsetMinutes, hourFractionInTz, formatTzOffset } from '../utils/tz'
import type { CronJob } from '../types'

/** Smallest CronJob that satisfies the type, with sensible defaults. */
const baseJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'j1',
  name: 'test',
  message: 'msg',
  enabled: true,
  schedule: '*/5 9-16 * * 1-5',
  last_status: 'ok',
  cron_expr: '*/5 9-16 * * 1-5',
  ...overrides,
})

describe('tz.convertCronTime', () => {
  // May 12 2026 is a Tuesday; America/New_York is on EDT (UTC-4).
  const refNow = new Date('2026-05-12T17:00:00Z')

  it('is a no-op when source and target are the same', () => {
    const out = convertCronTime(2 /* Tue */, 9, 0, 'America/New_York', 'America/New_York', refNow)
    expect(out).toEqual({ dow: 2, hour: 9, minute: 0 })
  })

  it('converts America/New_York 9:00 Tue → UTC 13:00 Tue on EDT', () => {
    const out = convertCronTime(2 /* Tue */, 9, 0, 'America/New_York', 'UTC', refNow)
    expect(out).toEqual({ dow: 2, hour: 13, minute: 0 })
  })

  it('converts UTC 09:00 Mon → America/New_York 05:00 Mon (EDT)', () => {
    const out = convertCronTime(1 /* Mon */, 9, 0, 'UTC', 'America/New_York', refNow)
    expect(out).toEqual({ dow: 1, hour: 5, minute: 0 })
  })

  it('handles day-boundary wrap: UTC 02:00 Mon → America/Los_Angeles 19:00 Sun (PDT, UTC-7)', () => {
    const out = convertCronTime(1 /* Mon */, 2, 0, 'UTC', 'America/Los_Angeles', refNow)
    expect(out).toEqual({ dow: 0 /* Sun */, hour: 19, minute: 0 })
  })

  it('handles half-hour offsets: UTC 09:00 Wed → Asia/Kolkata 14:30 Wed (IST, UTC+5:30)', () => {
    const out = convertCronTime(3 /* Wed */, 9, 0, 'UTC', 'Asia/Kolkata', refNow)
    expect(out).toEqual({ dow: 3 /* Wed */, hour: 14, minute: 30 })
  })
})

describe('tz.tzOffsetMinutes', () => {
  const summer = new Date('2026-07-01T12:00:00Z') // July → DST in US zones
  const winter = new Date('2026-12-15T12:00:00Z') // December → standard time

  it('returns 0 for UTC', () => {
    expect(tzOffsetMinutes('UTC', summer)).toBe(0)
  })

  it('returns -240 for America/New_York in EDT', () => {
    expect(tzOffsetMinutes('America/New_York', summer)).toBe(-240)
  })

  it('returns -300 for America/New_York in EST', () => {
    expect(tzOffsetMinutes('America/New_York', winter)).toBe(-300)
  })

  it('returns +330 for Asia/Kolkata (no DST)', () => {
    expect(tzOffsetMinutes('Asia/Kolkata', summer)).toBe(330)
  })
})

describe('tz.formatTzOffset', () => {
  const summer = new Date('2026-07-01T12:00:00Z')
  it('formats integer-hour negative offsets', () => {
    expect(formatTzOffset('America/New_York', summer)).toBe('UTC−4')
  })
  it('formats half-hour positive offsets', () => {
    expect(formatTzOffset('Asia/Kolkata', summer)).toBe('UTC+05:30')
  })
  it('formats UTC as the bare string', () => {
    expect(formatTzOffset('UTC', summer)).toBe('UTC')
  })
})

describe('tz.hourFractionInTz', () => {
  it('reads the correct hour in the target TZ', () => {
    const at = new Date('2026-05-12T17:30:00Z') // 13:30 EDT, 10:30 PDT
    expect(hourFractionInTz('America/New_York', at)).toBeCloseTo(13.5, 3)
    expect(hourFractionInTz('America/Los_Angeles', at)).toBeCloseTo(10.5, 3)
    expect(hourFractionInTz('UTC', at)).toBeCloseTo(17.5, 3)
  })
})

describe('WeekGrid.parseCronSlots — timezone-aware', () => {
  // Pin to May 12 2026 so tests don't drift across DST boundaries.
  // `parseCronSlots` calls `convertCronTime` without an explicit `now`, so
  // it reads `new Date()` internally — the UTC offset differs between
  // summer (EDT, UTC−4) and winter (EST, UTC−5). Pinning to May 2026
  // guarantees EDT for all America/New_York assertions below.
  beforeEach(() => {
    vi.useFakeTimers({ now: new Date('2026-05-12T17:00:00Z') })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('places America/New_York 9-16 Mon-Fri correctly when rendered in America/New_York', () => {
    const job = baseJob({
      cron_expr: '*/5 9-16 * * 1-5',
      timezone: 'America/New_York',
    })
    const slots = parseCronSlots(job, 'America/New_York')
    // No conversion needed — hours should match the cron expression.
    const hoursSeen = [...new Set(slots.map(s => s.hour))].sort((a, b) => a - b)
    expect(hoursSeen).toEqual([9, 10, 11, 12, 13, 14, 15, 16])
    const daysSeen = [...new Set(slots.map(s => s.day))].sort((a, b) => a - b)
    expect(daysSeen).toEqual([0, 1, 2, 3, 4]) // Mon-Fri in grid (0=Mon..6=Sun)
  })

  it('shifts America/New_York 9-16 Mon-Fri → 13-20 Mon-Fri when rendered in UTC', () => {
    const job = baseJob({
      cron_expr: '0 9-16 * * 1-5',
      timezone: 'America/New_York',
    })
    const slots = parseCronSlots(job, 'UTC')
    const hoursSeen = [...new Set(slots.map(s => s.hour))].sort((a, b) => a - b)
    expect(hoursSeen).toEqual([13, 14, 15, 16, 17, 18, 19, 20])
  })

  it('treats legacy jobs with no timezone as UTC', () => {
    const job = baseJob({
      cron_expr: '0 9 * * 1', // Mon 9:00 (assumed UTC)
      timezone: null,
    })
    const slots = parseCronSlots(job, 'America/New_York')
    // UTC 09:00 Mon = 05:00 EDT Mon (May → EDT = UTC-4).
    // In May 2026 both are Monday.
    expect(slots).toHaveLength(1)
    expect(slots[0].hour).toBe(5)
    expect(slots[0].day).toBe(0) // Monday
  })

  it('handles the reported bug case: stored as NY, browser is NY, rendered as NY → no shift', () => {
    // Correct case — no shift: the cron `9-16` renders at 9-16 in ET,
    // not at 05:00-12:00.
    const job = baseJob({
      cron_expr: '*/5 9-16 * * 1-5',
      timezone: 'America/New_York',
    })
    const slots = parseCronSlots(job, 'America/New_York')
    const minHour = Math.min(...slots.map(s => s.hour))
    const maxHour = Math.max(...slots.map(s => s.hour))
    expect(minHour).toBe(9)
    expect(maxHour).toBe(16)
  })

  it('interval jobs return 7 slots across all days', () => {
    const job = baseJob({
      cron_expr: null,
      every_secs: 86400, // once per day
    })
    const slots = parseCronSlots(job, 'America/New_York')
    expect(slots).toHaveLength(7)
    expect(new Set(slots.map(s => s.day)).size).toBe(7)
  })
})

import { describe, expect, it } from 'vitest'
import {
  DEFAULT_THEME_SCHEDULE,
  dueScheduledMode,
  parseThemeSchedule,
  scheduledModeAt,
  sunTimes,
  type ThemeSchedule,
} from './themeSchedule'

// vitest pins TZ=UTC, so the device's local day is the UTC day here.
const MINUTE = 60_000

function expectNear(actual: Date, expectedIso: string, toleranceMin = 4) {
  expect(Math.abs(actual.getTime() - Date.parse(expectedIso))).toBeLessThanOrEqual(toleranceMin * MINUTE)
}

function schedule(patch: Partial<ThemeSchedule>): ThemeSchedule {
  return { ...DEFAULT_THEME_SCHEDULE, enabled: true, ...patch }
}

describe('sunTimes', () => {
  it('matches published sunrise and sunset for Seattle at the June solstice', () => {
    const sun = sunTimes(new Date('2026-06-21T12:00:00Z'), 47.6, -122.3)
    expect(sun.kind).toBe('normal')
    if (sun.kind !== 'normal') return
    expectNear(sun.sunrise, '2026-06-21T12:11:00Z')
    expectNear(sun.sunset, '2026-06-22T04:11:00Z')
  })

  it('matches published sunrise and sunset for London at the December solstice', () => {
    const sun = sunTimes(new Date('2026-12-21T12:00:00Z'), 51.5, -0.13)
    expect(sun.kind).toBe('normal')
    if (sun.kind !== 'normal') return
    expectNear(sun.sunrise, '2026-12-21T08:04:00Z')
    expectNear(sun.sunset, '2026-12-21T15:54:00Z')
  })

  it('reports polar day and polar night above the Arctic Circle', () => {
    expect(sunTimes(new Date('2026-06-21T12:00:00Z'), 69.65, 18.96).kind).toBe('polar-day')
    expect(sunTimes(new Date('2026-12-21T12:00:00Z'), 69.65, 18.96).kind).toBe('polar-night')
  })
})

describe('scheduledModeAt', () => {
  const fixed = schedule({ method: 'fixed', lightAt: '07:30', darkAt: '20:00' })

  it('uses the fixed light and dark times on the device clock', () => {
    expect(scheduledModeAt(new Date('2026-03-10T07:29:00Z'), fixed)?.mode).toBe('dark')
    expect(scheduledModeAt(new Date('2026-03-10T07:30:00Z'), fixed)?.mode).toBe('light')
    expect(scheduledModeAt(new Date('2026-03-10T19:59:00Z'), fixed)?.mode).toBe('light')
    expect(scheduledModeAt(new Date('2026-03-10T20:00:00Z'), fixed)?.mode).toBe('dark')
  })

  it('names the next boundary', () => {
    expect(scheduledModeAt(new Date('2026-03-10T12:00:00Z'), fixed)?.next).toBe(Date.parse('2026-03-10T20:00:00Z'))
    expect(scheduledModeAt(new Date('2026-03-10T21:00:00Z'), fixed)?.next).toBe(Date.parse('2026-03-11T07:30:00Z'))
  })

  it('handles a dark period that starts before the light one (night shift)', () => {
    const inverted = schedule({ method: 'fixed', lightAt: '22:00', darkAt: '06:00' })
    expect(scheduledModeAt(new Date('2026-03-10T23:00:00Z'), inverted)?.mode).toBe('light')
    expect(scheduledModeAt(new Date('2026-03-10T12:00:00Z'), inverted)?.mode).toBe('dark')
  })

  it('follows sunrise and sunset when a location is set', () => {
    const sun = schedule({ method: 'sun', latitude: 51.5, longitude: -0.13 })
    expect(scheduledModeAt(new Date('2026-12-21T07:30:00Z'), sun)?.mode).toBe('dark')
    expect(scheduledModeAt(new Date('2026-12-21T12:00:00Z'), sun)?.mode).toBe('light')
    expect(scheduledModeAt(new Date('2026-12-21T17:00:00Z'), sun)?.mode).toBe('dark')
  })

  it('falls back to the fixed times when sunrise/sunset has no location', () => {
    const sun = schedule({ method: 'sun', latitude: null, longitude: null, lightAt: '09:00', darkAt: '17:00' })
    expect(scheduledModeAt(new Date('2026-12-21T08:00:00Z'), sun)?.mode).toBe('dark')
    expect(scheduledModeAt(new Date('2026-12-21T10:00:00Z'), sun)?.mode).toBe('light')
  })

  it('keeps one mode all day at the poles', () => {
    const arctic = schedule({ method: 'sun', latitude: 69.65, longitude: 18.96 })
    expect(scheduledModeAt(new Date('2026-06-21T23:30:00Z'), arctic)?.mode).toBe('light')
    expect(scheduledModeAt(new Date('2026-12-21T12:00:00Z'), arctic)?.mode).toBe('dark')
  })

  // West of its time zone's meridian (here 18.96 W on UTC) local midnight comes
  // before solar midnight, so the last sunset before polar day (17 May, 00:38 on
  // 18 May) falls after the first polar day's local midnight.
  const lateSunset = schedule({ method: 'sun', latitude: 69.65, longitude: -18.96 })

  it('starts polar day light even when the previous sunset falls after local midnight', () => {
    expect(sunTimes(new Date('2026-05-18T12:00:00Z'), 69.65, -18.96).kind).toBe('polar-day')
    expect(scheduledModeAt(new Date('2026-05-18T10:00:00Z'), lateSunset)?.mode).toBe('light')
  })

  it('treats a polar day as one period, so it does not re-switch at each midnight', () => {
    const first = scheduledModeAt(new Date('2026-05-19T10:00:00Z'), lateSunset)
    const later = scheduledModeAt(new Date('2026-05-25T10:00:00Z'), lateSunset)
    expect(first?.mode).toBe('light')
    expect(later?.since).toBe(first?.since)
    const picked = { ...lateSunset, pickedAt: first!.since + 1 }
    expect(dueScheduledMode(new Date('2026-05-25T10:00:00Z'), picked)).toBeNull()
  })

  it('has no schedule when light and dark start at the same time', () => {
    expect(scheduledModeAt(new Date('2026-03-10T12:00:00Z'), schedule({ method: 'fixed', lightAt: '08:00', darkAt: '08:00' }))).toBeNull()
  })
})

describe('dueScheduledMode', () => {
  const fixed = schedule({ method: 'fixed', lightAt: '07:00', darkAt: '19:00' })

  it('applies nothing while the schedule is off', () => {
    expect(dueScheduledMode(new Date('2026-03-10T12:00:00Z'), { ...fixed, enabled: false })).toBeNull()
  })

  it('names the current period until a mode is picked by hand, then holds that pick until the next boundary', () => {
    const noon = new Date('2026-03-10T12:00:00Z')
    const due = dueScheduledMode(noon, fixed)
    expect(due).toEqual({ mode: 'light', boundary: Date.parse('2026-03-10T07:00:00Z') })
    // Still due later in the same period: nothing recorded means nothing to hold.
    expect(dueScheduledMode(new Date('2026-03-10T18:59:00Z'), fixed)).toEqual(due)
    const picked = { ...fixed, pickedAt: noon.getTime() }
    // Same period: a mode picked by hand holds.
    expect(dueScheduledMode(new Date('2026-03-10T18:59:00Z'), picked)).toBeNull()
    // The next boundary passes: the schedule takes over again.
    expect(dueScheduledMode(new Date('2026-03-10T19:00:00Z'), picked)).toEqual({
      mode: 'dark',
      boundary: Date.parse('2026-03-10T19:00:00Z'),
    })
  })
})

describe('parseThemeSchedule', () => {
  it('defaults a missing or corrupt value', () => {
    expect(parseThemeSchedule(null)).toEqual(DEFAULT_THEME_SCHEDULE)
    expect(parseThemeSchedule('{not json')).toEqual(DEFAULT_THEME_SCHEDULE)
  })

  it('keeps well-formed fields and drops malformed ones', () => {
    const parsed = parseThemeSchedule(JSON.stringify({ enabled: true, method: 'fixed', lightAt: '6:00', darkAt: '21:15', latitude: 'x', longitude: 2.35 }))
    expect(parsed).toEqual({ ...DEFAULT_THEME_SCHEDULE, enabled: true, method: 'fixed', darkAt: '21:15', longitude: 2.35 })
  })
})

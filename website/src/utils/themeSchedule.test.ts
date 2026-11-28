// Scheduled mode by the sun. Vitest pins TZ to UTC, so the device's local day is the UTC day.
import { afterEach, describe, expect, it } from 'vitest'
import { THEME_SCHEDULE_STORAGE_KEY, readStoredThemeSchedule, scheduledMode, sunTimes } from './themeSchedule'

const FIXED = { light: '07:00', dark: '19:00' }
const at = (iso: string) => new Date(iso)
const sun = (latitude: number | null, longitude: number | null) => ({ ...FIXED, method: 'sun' as const, latitude, longitude })

describe('scheduledMode by sunrise and sunset', () => {
  const london = sun(51.5074, -0.1278)

  it('is light at local noon and dark after local midnight', () => {
    expect(scheduledMode(london, at('2026-06-21T12:00:00Z'))).toBe('light')
    expect(scheduledMode(london, at('2026-06-21T00:30:00Z'))).toBe('dark')
  })

  it('switches to light at the computed sunrise', () => {
    const day = sunTimes(at('2026-06-21T12:00:00Z'), 51.5074, -0.1278)
    if (day.kind !== 'normal') throw new Error(`expected a normal day, got ${day.kind}`)
    // London's sunrise on the June solstice is 03:43 UTC.
    expect(day.sunrise.getTime()).toBeGreaterThan(at('2026-06-21T03:40:00Z').getTime())
    expect(day.sunrise.getTime()).toBeLessThan(at('2026-06-21T03:47:00Z').getTime())
    expect(scheduledMode(london, at('2026-06-21T03:36:00Z'))).toBe('dark')
    expect(scheduledMode(london, at('2026-06-21T03:51:00Z'))).toBe('light')
  })

  it("is light past local midnight while the previous day's sunset has not come", () => {
    // Seattle on the device's UTC clock: the 21st's sunset is about 04:11 on the 22nd.
    const now = at('2026-06-22T02:00:00Z')
    expect(scheduledMode(sun(47.61, -122.33), now)).toBe('light')
    expect(scheduledMode(FIXED, now)).toBe('dark')
  })

  it('is light through a polar day and dark through a polar night', () => {
    const tromso = sun(69.65, 18.96)
    expect(scheduledMode(tromso, at('2026-06-21T00:00:00Z'))).toBe('light')
    expect(scheduledMode(tromso, at('2026-12-21T12:00:00Z'))).toBe('dark')
  })

  it('uses the fixed times without a valid location', () => {
    const night = at('2026-06-21T22:00:00Z')
    expect(scheduledMode({ ...FIXED, method: 'sun' }, night)).toBe('dark')
    expect(scheduledMode(sun(51.5, null), night)).toBe('dark')
    expect(scheduledMode(sun(91, -0.13), night)).toBe('dark')
    expect(scheduledMode(sun(51.5, 181), night)).toBe('dark')
    expect(scheduledMode({ light: '21:00', dark: '23:00', method: 'sun', latitude: 95, longitude: 0 }, night)).toBe('light')
  })
})

describe('readStoredThemeSchedule', () => {
  afterEach(() => localStorage.clear())

  it('returns the stored method and location', () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '06:30', dark: '20:00', method: 'sun', latitude: 47.61, longitude: -122.33 }))
    expect(readStoredThemeSchedule()).toEqual({ light: '06:30', dark: '20:00', method: 'sun', latitude: 47.61, longitude: -122.33 })
  })

  it('drops an unknown method and an invalid coordinate', () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '06:30', dark: '20:00', method: 'moon', latitude: 95, longitude: '-122' }))
    expect(readStoredThemeSchedule()).toStrictEqual({ light: '06:30', dark: '20:00' })
  })
})

/**
 * Scheduled mode: the light appearance from one local time, the dark one from
 * another, or from local sunrise to sunset at a typed location. Import-free, so
 * every window that paints the dashboard theme from localStorage (`useTheme`,
 * the crew companion, Mochi) resolves `mc-theme` = `schedule` the same way.
 */

/** Two 24-hour `HH:MM` times, read in the device's own time zone. */
export interface ThemeSchedule {
  light: string
  dark: string
  /** What Scheduled follows; absent = the two times, never chosen. */
  source?: ThemeScheduleSource
  /** `latitude, longitude` in decimal degrees, for `source` = `sun`. */
  location?: string
}

/** `times`: `light` / `dark`. `sun`: light from local sunrise to sunset at `location`. */
export type ThemeScheduleSource = 'times' | 'sun'

const DEFAULT_THEME_SCHEDULE: ThemeSchedule = { light: '07:00', dark: '19:00' }

/** The browser copy of the schedule, kept so the first paint is already right. */
export const THEME_SCHEDULE_STORAGE_KEY = 'mc-theme-schedule'

function isClockTime(value: unknown): value is string {
  return typeof value === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(value)
}

/**
 * `latitude, longitude` in decimal degrees ("51.51, -0.13"), or null. The same
 * shape `/api/config/theme` accepts.
 */
export function parseLocation(value: unknown): { latitude: number; longitude: number } | null {
  if (typeof value !== 'string' || value.length > 64) return null
  const match = /^ *([+-]?\d{1,3}(?:\.\d+)?) *, *([+-]?\d{1,3}(?:\.\d+)?) *$/.exec(value)
  if (!match) return null
  const latitude = Number(match[1])
  const longitude = Number(match[2])
  if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return null
  return { latitude, longitude }
}

/**
 * Each time as given when it is a 24-hour `HH:MM`, else its default. `source`
 * and `location` are kept only when valid, so a schedule that never chose one
 * stays the plain pair.
 */
export function normalizeThemeSchedule(
  light: unknown,
  dark: unknown,
  source?: unknown,
  location?: unknown,
): ThemeSchedule {
  const schedule: ThemeSchedule = {
    light: isClockTime(light) ? light : DEFAULT_THEME_SCHEDULE.light,
    dark: isClockTime(dark) ? dark : DEFAULT_THEME_SCHEDULE.dark,
  }
  if (source === 'times' || source === 'sun') schedule.source = source
  if (typeof location === 'string' && parseLocation(location)) schedule.location = location.trim()
  return schedule
}

export function readStoredThemeSchedule(): ThemeSchedule {
  try {
    const stored = JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY) ?? '{}')
    return normalizeThemeSchedule(stored?.light, stored?.dark, stored?.source, stored?.location)
  } catch {
    return { ...DEFAULT_THEME_SCHEDULE }
  }
}

function minutesOf(time: string): number {
  return Number(time.slice(0, 2)) * 60 + Number(time.slice(3))
}

const RAD = Math.PI / 180

/**
 * The sun's height above the horizon, in degrees, at `latitude` / `longitude`
 * and the instant `now`. The low-precision solar position from the
 * Astronomical Almanac: within about two minutes of published sunrise and
 * sunset times. Works from the instant, so the device time zone cannot skew it.
 */
export function solarElevation(latitude: number, longitude: number, now: Date): number {
  const d = now.getTime() / 86_400_000 - 10_957.5 // days since J2000.0 (2000-01-01 12:00 UTC)
  const g = (357.529 + 0.98560028 * d) * RAD
  const q = 280.459 + 0.98564736 * d
  const eclipticLongitude = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * RAD
  const obliquity = (23.439 - 0.00000036 * d) * RAD
  const rightAscension = Math.atan2(Math.cos(obliquity) * Math.sin(eclipticLongitude), Math.cos(eclipticLongitude)) / RAD
  const declination = Math.asin(Math.sin(obliquity) * Math.sin(eclipticLongitude))
  const siderealDegrees = 280.46061837 + 360.98564736629 * d
  const hourAngle = (siderealDegrees + longitude - rightAscension) * RAD
  const sinElevation = Math.sin(latitude * RAD) * Math.sin(declination)
    + Math.cos(latitude * RAD) * Math.cos(declination) * Math.cos(hourAngle)
  return Math.asin(sinElevation) / RAD
}

/** The sun's top edge on the horizon, after refraction: the standard sunrise and sunset. */
const SUNRISE_ELEVATION = -0.833

/**
 * Light from `light` until `dark` on the device clock, wrapping past midnight.
 * With `source` = `sun` and a valid `location`, light while the sun is up there
 * instead: all day in a polar day, never in a polar night. A sun schedule
 * without a valid location keeps to the two times.
 */
export function scheduledMode(schedule: ThemeSchedule, now: Date = new Date()): 'light' | 'dark' {
  const place = schedule.source === 'sun' ? parseLocation(schedule.location) : null
  if (place) return solarElevation(place.latitude, place.longitude, now) > SUNRISE_ELEVATION ? 'light' : 'dark'
  const light = minutesOf(schedule.light)
  const dark = minutesOf(schedule.dark)
  const t = now.getHours() * 60 + now.getMinutes()
  const isLight = light <= dark ? t >= light && t < dark : t >= light || t < dark
  return isLight ? 'light' : 'dark'
}

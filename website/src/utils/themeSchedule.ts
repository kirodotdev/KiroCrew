/**
 * Scheduled light/dark switching: the pure half (no React, no DOM timers).
 *
 * A schedule names WHEN the dashboard should turn light and when dark, either at
 * the local sunrise and sunset or at two fixed clock times. Every time is read in
 * the device's own time zone, because the calendar days and clock times below are
 * built with the local `Date` constructor.
 *
 * Sunrise and sunset are computed here, from a coarse latitude/longitude the user
 * typed. Nothing is looked up online and the browser's geolocation API is never
 * called: a location the user did not give is a location this feature does not
 * have, and the schedule falls back to the fixed times instead.
 *
 * The schedule never pins the mode. It names boundaries, and the driver shows
 * each period's mode unless the user picked a mode by hand on this device since
 * that period began, so a hand pick holds until the next switch (`dueScheduledMode`).
 */

import type { ResolvedMode } from '../hooks/useTheme'

export type ThemeScheduleMethod = 'sun' | 'fixed'

export interface ThemeSchedule {
  enabled: boolean
  method: ThemeScheduleMethod
  /** Local clock time the light theme starts, `HH:MM` (24-hour). */
  lightAt: string
  /** Local clock time the dark theme starts, `HH:MM` (24-hour). */
  darkAt: string
  /** Coarse location for sunrise/sunset, in decimal degrees (north / east positive). */
  latitude: number | null
  longitude: number | null
  /** Epoch ms of the last mode picked by hand on this device while the schedule ran; null = none since it last changed. */
  pickedAt: number | null
}

export const THEME_SCHEDULE_STORAGE_KEY = 'mc-theme-schedule'

export const DEFAULT_THEME_SCHEDULE: ThemeSchedule = {
  enabled: false,
  method: 'sun',
  lightAt: '07:00',
  darkAt: '19:00',
  latitude: null,
  longitude: null,
  pickedAt: null,
}

const HHMM_RE = /^([01]\d|2[0-3]):([0-5]\d)$/

export function isValidClockTime(value: string): boolean {
  return HHMM_RE.test(value)
}

export function isValidLatitude(value: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= -90 && value <= 90
}

export function isValidLongitude(value: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= -180 && value <= 180
}

function finiteOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Read a stored schedule, keeping each field that is well-formed and defaulting the rest. */
export function parseThemeSchedule(raw: string | null): ThemeSchedule {
  if (!raw) return { ...DEFAULT_THEME_SCHEDULE }
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return { ...DEFAULT_THEME_SCHEDULE }
  }
  if (!data || typeof data !== 'object') return { ...DEFAULT_THEME_SCHEDULE }
  const d = data as Record<string, unknown>
  return {
    enabled: d.enabled === true,
    method: d.method === 'fixed' ? 'fixed' : 'sun',
    lightAt: typeof d.lightAt === 'string' && isValidClockTime(d.lightAt) ? d.lightAt : DEFAULT_THEME_SCHEDULE.lightAt,
    darkAt: typeof d.darkAt === 'string' && isValidClockTime(d.darkAt) ? d.darkAt : DEFAULT_THEME_SCHEDULE.darkAt,
    latitude: finiteOrNull(d.latitude),
    longitude: finiteOrNull(d.longitude),
    pickedAt: finiteOrNull(d.pickedAt),
  }
}

/** True when the sunrise/sunset method has a usable location to compute from. */
export function hasSunLocation(schedule: ThemeSchedule): boolean {
  return isValidLatitude(schedule.latitude) && isValidLongitude(schedule.longitude)
}

const DEG = Math.PI / 180
const MS_PER_DAY = 86_400_000
const JULIAN_UNIX_EPOCH = 2440587.5
const J2000 = 2451545

/** Sun events for one local calendar day: instants, or a whole-day state at the poles. */
export type SunDay =
  | { kind: 'normal'; sunrise: Date; sunset: Date }
  | { kind: 'polar-day' }
  | { kind: 'polar-night' }

/**
 * Sunrise and sunset for the local calendar day containing `day`, by the standard
 * sunrise equation (solar disc's upper limb at the horizon, with refraction:
 * -0.833 degrees). Accurate to a minute or two away from the poles, which is far
 * finer than a coarse location needs.
 */
export function sunTimes(day: Date, latitude: number, longitude: number): SunDay {
  // The Julian day number of this local date's noon UTC: the equation's day index.
  const noonUtc = Date.UTC(day.getFullYear(), day.getMonth(), day.getDate(), 12)
  const n = Math.round(noonUtc / MS_PER_DAY + JULIAN_UNIX_EPOCH - J2000)
  const meanSolarTime = n - longitude / 360
  const anomaly = (357.5291 + 0.98560028 * meanSolarTime) % 360
  const m = anomaly * DEG
  const center = 1.9148 * Math.sin(m) + 0.02 * Math.sin(2 * m) + 0.0003 * Math.sin(3 * m)
  const eclipticLongitude = ((anomaly + center + 180 + 102.9372) % 360) * DEG
  const transit = J2000 + meanSolarTime + 0.0053 * Math.sin(m) - 0.0069 * Math.sin(2 * eclipticLongitude)
  const sinDecl = Math.sin(eclipticLongitude) * Math.sin(23.4397 * DEG)
  const cosDecl = Math.cos(Math.asin(sinDecl))
  const phi = latitude * DEG
  const cosHourAngle = (Math.sin(-0.833 * DEG) - Math.sin(phi) * sinDecl) / (Math.cos(phi) * cosDecl)
  if (cosHourAngle < -1) return { kind: 'polar-day' }
  if (cosHourAngle > 1) return { kind: 'polar-night' }
  const halfDay = Math.acos(cosHourAngle) / (2 * Math.PI)
  const toDate = (julian: number) => new Date(Math.round((julian - JULIAN_UNIX_EPOCH) * MS_PER_DAY))
  return { kind: 'normal', sunrise: toDate(transit - halfDay), sunset: toDate(transit + halfDay) }
}

interface Boundary {
  at: number
  mode: ResolvedMode
}

function clockOn(base: Date, offsetDays: number, hhmm: string): Date {
  const [h, m] = hhmm.split(':').map(Number)
  return new Date(base.getFullYear(), base.getMonth(), base.getDate() + offsetDays, h, m)
}

/** How far back to look for the day a polar day or night began (longer than any polar period). */
const MAX_POLAR_LOOKBACK_DAYS = 200

/**
 * Every scheduled switch from two days before `now` to two days after, oldest
 * first, with consecutive same-mode boundaries merged into the earliest one.
 *
 * Merging is what keeps a polar day or night from re-switching at every local
 * midnight: the whole run is one boundary, so a mode picked by hand during it
 * holds. For the same reason the sun window reaches back past `now - 2 days` to
 * the last day that had a sunrise or sunset, so a run's start (and with it
 * `scheduledModeAt().since`) stays the same from one day to the next.
 */
function boundariesAround(now: Date, schedule: ThemeSchedule): Boundary[] {
  const out: Boundary[] = []
  const useSun = schedule.method === 'sun' && hasSunLocation(schedule)
  const lat = schedule.latitude as number
  const lon = schedule.longitude as number
  const noonOn = (offset: number) => new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, 12)
  let first = -2
  if (useSun) {
    while (first > -MAX_POLAR_LOOKBACK_DAYS && sunTimes(noonOn(first), lat, lon).kind !== 'normal') first--
  }
  // The latest instant emitted for an earlier day. A day's sunset can fall after
  // the next local midnight (local midnight before solar midnight), so a whole-day
  // polar boundary is placed after it rather than at midnight: otherwise the
  // previous evening's sunset would sort after it and win the whole polar day.
  let latest = -Infinity
  for (let offset = first; offset <= 2; offset++) {
    if (useSun) {
      const sun = sunTimes(noonOn(offset), lat, lon)
      if (sun.kind === 'normal') {
        out.push({ at: sun.sunrise.getTime(), mode: 'light' })
        out.push({ at: sun.sunset.getTime(), mode: 'dark' })
        latest = Math.max(latest, sun.sunrise.getTime(), sun.sunset.getTime())
      } else {
        // No sunrise or sunset that day: the whole day takes one mode, from midnight.
        const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset).getTime()
        const at = Math.max(midnight, latest + 1)
        out.push({ at, mode: sun.kind === 'polar-day' ? 'light' : 'dark' })
        latest = Math.max(latest, at)
      }
    } else if (schedule.lightAt !== schedule.darkAt) {
      out.push({ at: clockOn(now, offset, schedule.lightAt).getTime(), mode: 'light' })
      out.push({ at: clockOn(now, offset, schedule.darkAt).getTime(), mode: 'dark' })
    }
  }
  out.sort((a, b) => a.at - b.at)
  const merged: Boundary[] = []
  for (const b of out) {
    if (merged.length === 0 || merged[merged.length - 1].mode !== b.mode) merged.push(b)
  }
  return merged
}

/** The mode the schedule calls for at `now`, and when that period started and ends. */
export function scheduledModeAt(
  now: Date,
  schedule: ThemeSchedule,
): { mode: ResolvedMode; since: number; next: number | null } | null {
  const boundaries = boundariesAround(now, schedule)
  const t = now.getTime()
  let current: Boundary | null = null
  let next: number | null = null
  for (const b of boundaries) {
    if (b.at <= t) current = b
    else if (next === null) next = b.at
  }
  if (!current) return null
  return { mode: current.mode, since: current.at, next }
}

/**
 * The mode this device should show now, or null when the schedule leaves the
 * mode alone: it is off, it names no switch, or a mode was picked by hand
 * (`pickedAt`) since the current period began, which holds until the next one.
 */
export function dueScheduledMode(
  now: Date,
  schedule: ThemeSchedule,
): { mode: ResolvedMode; boundary: number } | null {
  if (!schedule.enabled) return null
  const current = scheduledModeAt(now, schedule)
  if (!current) return null
  if (schedule.pickedAt !== null && schedule.pickedAt >= current.since) return null
  return { mode: current.mode, boundary: current.since }
}

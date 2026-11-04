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
 * The schedule never pins the mode. It names boundaries, and the driver applies
 * the scheduled mode once per boundary, so a mode the user picks by hand holds
 * until the next scheduled switch (`dueScheduledMode`).
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
  /** Epoch ms of the last boundary the driver applied; null = none yet. */
  appliedAt: number | null
}

export const THEME_SCHEDULE_STORAGE_KEY = 'mc-theme-schedule'

export const DEFAULT_THEME_SCHEDULE: ThemeSchedule = {
  enabled: false,
  method: 'sun',
  lightAt: '07:00',
  darkAt: '19:00',
  latitude: null,
  longitude: null,
  appliedAt: null,
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
    appliedAt: finiteOrNull(d.appliedAt),
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

/** Every scheduled switch from two days before `now` to two days after, oldest first. */
function boundariesAround(now: Date, schedule: ThemeSchedule): Boundary[] {
  const out: Boundary[] = []
  const useSun = schedule.method === 'sun' && hasSunLocation(schedule)
  for (let offset = -2; offset <= 2; offset++) {
    if (useSun) {
      const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, 12)
      const sun = sunTimes(day, schedule.latitude as number, schedule.longitude as number)
      if (sun.kind === 'normal') {
        out.push({ at: sun.sunrise.getTime(), mode: 'light' })
        out.push({ at: sun.sunset.getTime(), mode: 'dark' })
      } else {
        // No sunrise or sunset that day: the whole day takes one mode, from midnight.
        const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset).getTime()
        out.push({ at: midnight, mode: sun.kind === 'polar-day' ? 'light' : 'dark' })
      }
    } else if (schedule.lightAt !== schedule.darkAt) {
      out.push({ at: clockOn(now, offset, schedule.lightAt).getTime(), mode: 'light' })
      out.push({ at: clockOn(now, offset, schedule.darkAt).getTime(), mode: 'dark' })
    }
  }
  return out.sort((a, b) => a.at - b.at)
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
 * The mode to apply now, or null when nothing is due. A boundary is applied once:
 * after it is recorded in `appliedAt`, a manual mode change holds until the next
 * boundary passes.
 */
export function dueScheduledMode(
  now: Date,
  schedule: ThemeSchedule,
): { mode: ResolvedMode; boundary: number } | null {
  if (!schedule.enabled) return null
  const current = scheduledModeAt(now, schedule)
  if (!current) return null
  if (schedule.appliedAt !== null && schedule.appliedAt >= current.since) return null
  return { mode: current.mode, boundary: current.since }
}

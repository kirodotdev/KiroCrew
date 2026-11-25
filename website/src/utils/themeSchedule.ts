/**
 * Scheduled mode: the light appearance from one local time, the dark one from
 * another, or from local sunrise until sunset at a typed location. Import-free,
 * so every window that paints the dashboard theme from localStorage
 * (`useTheme`, the crew companion, Mochi) resolves `mc-theme` = `schedule` the
 * same way.
 */

/**
 * Two 24-hour `HH:MM` times, read in the device's own time zone. `method` `'sun'`
 * with a valid location follows sunrise and sunset there instead; the method and
 * the location are kept in this browser only, never in the workspace.
 */
export interface ThemeSchedule {
  light: string
  dark: string
  method?: 'sun'
  /** Decimal degrees, north and east positive. */
  latitude?: number | null
  longitude?: number | null
}

const DEFAULT_THEME_SCHEDULE: ThemeSchedule = { light: '07:00', dark: '19:00' }

/** The browser copy of the schedule, kept so the first paint is already right. */
export const THEME_SCHEDULE_STORAGE_KEY = 'mc-theme-schedule'

function isClockTime(value: unknown): value is string {
  return typeof value === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(value)
}

/** Each time as given when it is a 24-hour `HH:MM`, else its default. */
export function normalizeThemeSchedule(light: unknown, dark: unknown): ThemeSchedule {
  return {
    light: isClockTime(light) ? light : DEFAULT_THEME_SCHEDULE.light,
    dark: isClockTime(dark) ? dark : DEFAULT_THEME_SCHEDULE.dark,
  }
}

export function isLatitude(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= -90 && value <= 90
}

export function isLongitude(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= -180 && value <= 180
}

export function readStoredThemeSchedule(): ThemeSchedule {
  try {
    const stored = JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY) ?? '{}')
    const schedule = normalizeThemeSchedule(stored?.light, stored?.dark)
    if (stored?.method === 'sun') schedule.method = 'sun'
    if (isLatitude(stored?.latitude)) schedule.latitude = stored.latitude
    if (isLongitude(stored?.longitude)) schedule.longitude = stored.longitude
    return schedule
  } catch {
    return { ...DEFAULT_THEME_SCHEDULE }
  }
}

function minutesOf(time: string): number {
  return Number(time.slice(0, 2)) * 60 + Number(time.slice(3))
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

/**
 * Light between a sunrise and its sunset. The previous and next local days are
 * checked too: far west in a time zone, a summer sunset falls after local midnight.
 */
function sunMode(now: Date, latitude: number, longitude: number): 'light' | 'dark' {
  for (const offset of [-1, 0, 1]) {
    const day = sunTimes(new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset), latitude, longitude)
    if (day.kind === 'normal' && now >= day.sunrise && now < day.sunset) return 'light'
  }
  return sunTimes(now, latitude, longitude).kind === 'polar-day' ? 'light' : 'dark'
}

/**
 * Light from `light` until `dark` on the device clock, wrapping past midnight;
 * by the sun instead when `method` is `'sun'` and the location is valid.
 */
export function scheduledMode(schedule: ThemeSchedule, now: Date = new Date()): 'light' | 'dark' {
  const { method, latitude, longitude } = schedule
  if (method === 'sun' && isLatitude(latitude) && isLongitude(longitude)) return sunMode(now, latitude, longitude)
  const light = minutesOf(schedule.light)
  const dark = minutesOf(schedule.dark)
  const t = now.getHours() * 60 + now.getMinutes()
  const isLight = light <= dark ? t >= light && t < dark : t >= light || t < dark
  return isLight ? 'light' : 'dark'
}

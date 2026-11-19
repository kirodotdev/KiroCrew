/**
 * Scheduled mode: the light appearance from one local time, the dark one from
 * another. Import-free, so every window that paints the dashboard theme from
 * localStorage (`useTheme`, the crew companion, Mochi) resolves `mc-theme` =
 * `schedule` the same way.
 */

/** Two 24-hour `HH:MM` times, read in the device's own time zone. */
export interface ThemeSchedule {
  light: string
  dark: string
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

export function readStoredThemeSchedule(): ThemeSchedule {
  try {
    const stored = JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY) ?? '{}')
    return normalizeThemeSchedule(stored?.light, stored?.dark)
  } catch {
    return { ...DEFAULT_THEME_SCHEDULE }
  }
}

function minutesOf(time: string): number {
  return Number(time.slice(0, 2)) * 60 + Number(time.slice(3))
}

/** Light from `light` until `dark` on the device clock, wrapping past midnight. */
export function scheduledMode(schedule: ThemeSchedule, now: Date = new Date()): 'light' | 'dark' {
  const light = minutesOf(schedule.light)
  const dark = minutesOf(schedule.dark)
  const t = now.getHours() * 60 + now.getMinutes()
  const isLight = light <= dark ? t >= light && t < dark : t >= light || t < dark
  return isLight ? 'light' : 'dark'
}

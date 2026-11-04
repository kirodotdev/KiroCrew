/**
 * Applies the theme schedule. Renders nothing.
 *
 * At each scheduled boundary (sunrise/sunset or the fixed times) it sets the
 * mode through the same `setTheme` the Mode picker uses, then records the
 * boundary so it is applied once. A mode the user picks by hand in between is
 * left alone until the next boundary.
 *
 * It re-checks when the next boundary is due, at least hourly (a sleeping laptop
 * or a clock or time-zone change can move "now" past a boundary without a timer
 * firing on time), and whenever the tab becomes visible again.
 */

import { useEffect } from 'react'
import { useTheme } from '../hooks/useTheme'
import { readThemeSchedule, useThemeSchedule, writeThemeSchedule } from '../hooks/useThemeSchedule'
import { dueScheduledMode, scheduledModeAt } from '../utils/themeSchedule'

const MAX_WAIT_MS = 60 * 60 * 1000

export default function ThemeScheduleDriver() {
  const { preference, setTheme } = useTheme()
  const [schedule] = useThemeSchedule()

  useEffect(() => {
    if (!schedule.enabled) return
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = () => {
      const current = readThemeSchedule()
      const now = new Date()
      const due = dueScheduledMode(now, current)
      if (due) {
        if (preference !== due.mode) setTheme(due.mode)
        writeThemeSchedule({ ...current, appliedAt: due.boundary })
      }
      const next = scheduledModeAt(now, current)?.next ?? null
      const wait = next === null ? MAX_WAIT_MS : Math.min(MAX_WAIT_MS, Math.max(1000, next - now.getTime() + 1000))
      clearTimeout(timer)
      timer = setTimeout(tick, wait)
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') tick()
    }

    tick()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [schedule, preference, setTheme])

  return null
}

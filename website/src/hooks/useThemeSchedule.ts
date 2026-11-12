/**
 * Scheduled mode's state for `useThemeState` (`useTheme.tsx`): the two switch
 * times, their browser copy, and the clock that re-resolves the mode while
 * Scheduled is on.
 */
import { useCallback, useEffect, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import {
  THEME_SCHEDULE_STORAGE_KEY,
  readStoredThemeSchedule,
  scheduledMode,
  type ThemeSchedule,
} from '../utils/themeSchedule'

export function useThemeSchedule(
  mode: string,
  setResolved: (resolved: 'light' | 'dark') => void,
  persist: (
    body: { schedule_light: string; schedule_dark: string },
    options?: { onError?: () => void },
  ) => void,
) {
  const [schedule, adoptSchedule] = useState<ThemeSchedule>(readStoredThemeSchedule)
  const [saveFailed, setSaveFailed] = useState(false)

  useEffect(() => {
    safeSetItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify(schedule))
  }, [schedule])

  // A minute tick rather than a timer to the next switch time: it also lands
  // the right appearance within a minute of the device waking from sleep.
  useEffect(() => {
    if (mode !== 'schedule') return
    const tick = () => setResolved(scheduledMode(schedule))
    tick()
    const id = window.setInterval(tick, 60_000)
    return () => window.clearInterval(id)
  }, [mode, schedule, setResolved])

  const setSchedule = useCallback((next: ThemeSchedule) => {
    adoptSchedule(next)
    setSaveFailed(false)
    persist({ schedule_light: next.light, schedule_dark: next.dark }, { onError: () => setSaveFailed(true) })
  }, [persist])

  return { schedule, setSchedule, adoptSchedule, saveFailed }
}

/**
 * Scheduled mode's state for `useThemeState` (`useTheme.tsx`): the two switch
 * times, the browser-only sunrise and sunset method, their browser copy, and
 * the clock that re-resolves the mode while Scheduled is on.
 */
import { useCallback, useEffect, useState } from 'react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
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

  // Unchanged copies are not rewritten, so adopting another tab's write below
  // never echoes back to it as a fresh `storage` event.
  useEffect(() => {
    const serialized = JSON.stringify(schedule)
    if (safeGetItem(THEME_SCHEDULE_STORAGE_KEY) !== serialized) safeSetItem(THEME_SCHEDULE_STORAGE_KEY, serialized)
  }, [schedule])

  // Every writer here (a time save, a method or location pick, the boot merge)
  // starts from this tab's copy, so it follows another tab's writes: a tab
  // opened earlier would otherwise write its older copy back whole and drop
  // the method and location saved there. `null` is a storage clear.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_SCHEDULE_STORAGE_KEY || e.key === null) adoptSchedule(readStoredThemeSchedule())
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

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

  // The method and the location stay in this browser: no workspace write.
  const setSunSchedule = useCallback((next: Pick<ThemeSchedule, 'method' | 'latitude' | 'longitude'>) => {
    adoptSchedule(current => ({ ...current, ...next }))
  }, [])

  return { schedule, setSchedule, setSunSchedule, adoptSchedule, saveFailed }
}

/**
 * Scheduled mode's state for `useThemeState` (`useTheme.tsx`): the two switch
 * times, their browser copy, and the clock that re-resolves the mode while
 * Scheduled is on.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import {
  THEME_SCHEDULE_STORAGE_KEY,
  readStoredThemeSchedule,
  scheduledMode,
  type ThemeSchedule,
} from '../utils/themeSchedule'

/** The `/api/config/theme` fields a schedule save writes. */
export interface ThemeScheduleBody {
  schedule_light: string
  schedule_dark: string
  schedule_source?: string
  schedule_location?: string
}

/**
 * The save for `next`. Its source goes once one was chosen, so a times-only
 * schedule writes the same two keys it always has. Its location goes only while
 * `sendLocation`: a browser that never loaded the workspace's location (boot
 * leaves it out) must not write '' over it with a time or source edit.
 */
function scheduleBody(next: ThemeSchedule, sendLocation: boolean): ThemeScheduleBody {
  const body: ThemeScheduleBody = { schedule_light: next.light, schedule_dark: next.dark }
  if (next.source) body.schedule_source = next.source
  if (sendLocation) body.schedule_location = next.location ?? ''
  return body
}

export function useThemeSchedule(
  mode: string,
  setResolved: (resolved: 'light' | 'dark') => void,
  persist: (
    body: ThemeScheduleBody,
    options?: { onError?: () => void; onSuccess?: () => void },
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

  // A location edited here stays unsaved until a save carrying it succeeds, so a
  // retry after a refused save sends it again.
  const locationUnsavedRef = useRef(false)
  const setSchedule = useCallback((next: ThemeSchedule) => {
    if (next.location !== schedule.location) locationUnsavedRef.current = true
    const sendLocation = locationUnsavedRef.current
    adoptSchedule(next)
    setSaveFailed(false)
    persist(scheduleBody(next, sendLocation), {
      onError: () => setSaveFailed(true),
      onSuccess: () => { if (sendLocation) locationUnsavedRef.current = false },
    })
  }, [persist, schedule])

  return { schedule, setSchedule, adoptSchedule, saveFailed }
}

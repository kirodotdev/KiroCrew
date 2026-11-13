/**
 * Applies the theme schedule. Renders nothing.
 *
 * The schedule is device-local, so its switches are too: the driver shows each
 * period's mode through `applyLocalTheme`, which changes this tab and the
 * `mc-theme` render cache but never the workspace mode (`PUT /api/config/theme`).
 * Saving it there would make one device's schedule the mode every other device
 * boots into, schedule or not.
 *
 * A mode the user picks by hand (the Mode picker, the palette, the cycle key)
 * still goes through `setTheme` and the workspace. The driver notices the pick
 * and records its time in the schedule (`pickedAt`), and the schedule then
 * leaves the mode alone until the next boundary (`dueScheduledMode`).
 *
 * Because nothing it shows is saved, the driver re-asserts the period's mode
 * whenever it starts watching: after the theme boot read (`themeBootReady`),
 * which adopts the workspace mode when it lands, and in every tab on its own.
 * So a reload, a new tab, or a boundary another tab reached first all end on
 * the scheduled mode, and a pick by hand in any tab of this device holds in all
 * of them until the next switch.
 *
 * It re-checks when the next boundary is due, at least hourly (a sleeping laptop
 * or a clock or time-zone change can move "now" past a boundary without a timer
 * firing on time), and whenever the tab becomes visible again.
 */

import { useEffect, useRef } from 'react'
import { useTheme, type ModePreference } from '../hooks/useTheme'
import { readThemeSchedule, useThemeSchedule, writeThemeSchedule } from '../hooks/useThemeSchedule'
import { dueScheduledMode, scheduledModeAt } from '../utils/themeSchedule'

const MAX_WAIT_MS = 60 * 60 * 1000

export default function ThemeScheduleDriver() {
  const { preference, applyLocalTheme, themeBootReady } = useTheme()
  const [schedule] = useThemeSchedule()
  // The mode this tab last showed or saw while watching; null while not watching.
  // A different preference after that is a pick by hand.
  const shownMode = useRef<ModePreference | null>(null)
  // A pick by hand that storage refused to record, so this tab still honours it.
  const unsavedPickAt = useRef<number | null>(null)

  useEffect(() => {
    if (!schedule.enabled || !themeBootReady) {
      shownMode.current = null
      return
    }
    if (shownMode.current !== null && preference !== shownMode.current) {
      const at = Date.now()
      unsavedPickAt.current = writeThemeSchedule({ ...readThemeSchedule(), pickedAt: at }) ? null : at
    }
    shownMode.current = preference
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = () => {
      const current = readThemeSchedule()
      const now = new Date()
      const pickedAt = Math.max(current.pickedAt ?? -Infinity, unsavedPickAt.current ?? -Infinity)
      const due = dueScheduledMode(now, { ...current, pickedAt: Number.isFinite(pickedAt) ? pickedAt : null })
      if (due && due.mode !== shownMode.current) {
        shownMode.current = due.mode
        applyLocalTheme(due.mode)
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
  }, [schedule, preference, applyLocalTheme, themeBootReady])

  return null
}

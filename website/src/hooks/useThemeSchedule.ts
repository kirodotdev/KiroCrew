/**
 * The device-local theme schedule store.
 *
 * The schedule lives in this browser's localStorage, not in the workspace
 * config: it is read against this device's clock and time zone, and its
 * location is one the user typed on this device. One in-memory snapshot is
 * shared by every subscriber so the Settings row and the driver never disagree,
 * and a write from another tab arrives through the `storage` event.
 */

import { useCallback, useSyncExternalStore } from 'react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { THEME_SCHEDULE_STORAGE_KEY, parseThemeSchedule, type ThemeSchedule } from '../utils/themeSchedule'

const listeners = new Set<() => void>()
let snapshot: ThemeSchedule | null = null
let snapshotRaw: string | null = null

function read(): ThemeSchedule {
  const raw = safeGetItem(THEME_SCHEDULE_STORAGE_KEY)
  if (snapshot === null || raw !== snapshotRaw) {
    snapshotRaw = raw
    snapshot = parseThemeSchedule(raw)
  }
  return snapshot
}

function notify() {
  for (const listener of listeners) listener()
}

function onStorage(e: StorageEvent) {
  if (e.key === THEME_SCHEDULE_STORAGE_KEY || e.key === null) notify()
}

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0) window.addEventListener('storage', onStorage)
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) window.removeEventListener('storage', onStorage)
  }
}

/**
 * Write the schedule.
 *
 * Returns false, and leaves the snapshot untouched, when the browser refuses the
 * write (storage full or blocked). Publishing a snapshot that storage does not
 * hold would make the next `read()` reparse the old value into a new object,
 * re-running every subscriber's effect, which would retry the same write forever.
 */
export function writeThemeSchedule(next: ThemeSchedule): boolean {
  const raw = JSON.stringify(next)
  if (!safeSetItem(THEME_SCHEDULE_STORAGE_KEY, raw)) return false
  snapshotRaw = raw
  snapshot = next
  notify()
  return true
}

export function readThemeSchedule(): ThemeSchedule {
  return read()
}

/** Fields that decide when the schedule switches; editing one clears a hand pick, so it applies now. */
const TIMING_FIELDS: ReadonlyArray<keyof ThemeSchedule> = ['enabled', 'method', 'lightAt', 'darkAt', 'latitude', 'longitude']

/** Apply `patch` to the stored schedule; false when the browser refused to store it. */
export function useThemeSchedule(): [ThemeSchedule, (patch: Partial<ThemeSchedule>) => boolean] {
  const schedule = useSyncExternalStore(subscribe, read, read)
  const update = useCallback((patch: Partial<ThemeSchedule>) => {
    const current = read()
    const timingChanged = TIMING_FIELDS.some(k => k in patch && patch[k] !== current[k])
    return writeThemeSchedule({ ...current, ...patch, ...(timingChanged ? { pickedAt: null } : {}) })
  }, [])
  return [schedule, update]
}

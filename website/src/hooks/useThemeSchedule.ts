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

/** Write the schedule. A change to WHEN it switches clears `appliedAt`, so it applies at once. */
export function writeThemeSchedule(next: ThemeSchedule): void {
  const raw = JSON.stringify(next)
  safeSetItem(THEME_SCHEDULE_STORAGE_KEY, raw)
  snapshotRaw = raw
  snapshot = next
  notify()
}

export function readThemeSchedule(): ThemeSchedule {
  return read()
}

/** Fields that decide when the schedule switches; editing one re-applies it now. */
const TIMING_FIELDS: ReadonlyArray<keyof ThemeSchedule> = ['enabled', 'method', 'lightAt', 'darkAt', 'latitude', 'longitude']

export function useThemeSchedule(): [ThemeSchedule, (patch: Partial<ThemeSchedule>) => void] {
  const schedule = useSyncExternalStore(subscribe, read, read)
  const update = useCallback((patch: Partial<ThemeSchedule>) => {
    const current = read()
    const timingChanged = TIMING_FIELDS.some(k => k in patch && patch[k] !== current[k])
    writeThemeSchedule({ ...current, ...patch, ...(timingChanged ? { appliedAt: null } : {}) })
  }, [])
  return [schedule, update]
}

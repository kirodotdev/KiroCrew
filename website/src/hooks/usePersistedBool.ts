import { useState, useEffect, useRef } from 'react'
import { safeSetItem } from '../utils/safeStorage'

/**
 * Same-tab broadcast channel for `usePersistedBool` writes. The DOM `storage`
 * event only fires in OTHER documents, so without this a toggle in one mounted
 * instance would not reach same-key siblings in the same tab until they
 * remounted (the `mc-diff-split` key is shared across several chat diff
 * surfaces). One shared event name carrying `{ key, value }` in `detail`;
 * listeners filter by key.
 */
const SYNC_EVENT = 'mc:persisted-bool'

type SyncDetail = { key: string; value: boolean }

/**
 * Boolean state persisted to localStorage — used for view preferences that
 * should survive tab switches and reloads (word wrap, line numbers, diff
 * split/unified, …). Reads once on mount; writes (via safeSetItem, quota-
 * defensive) whenever the value changes. Mounted instances sharing a key
 * live-sync: a setter write broadcasts to same-document siblings via
 * `SYNC_EVENT`, and cross-tab updates arrive through the DOM `storage` event,
 * parsed with the same read semantics as the mount read.
 *
 * Assumes a stable `key` (every call site passes a literal): the hook has never
 * re-read on key change, and with live sync a key swap would persist the old
 * key's value under the new key without broadcasting it.
 */
export function usePersistedBool(key: string, defaultValue: boolean) {
  const [value, setValue] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(key)
      return v === null ? defaultValue : v === '1'
    } catch {
      return defaultValue
    }
  })

  // The last value that arrived via broadcast/storage, so the write effect can
  // tell an incoming sync (must NOT be re-broadcast — the origin instance
  // already notified everyone) from a genuine local setter write (must be).
  const receivedRef = useRef<boolean | null>(null)
  // The last value this effect persisted. null until the mount write runs, so
  // the mount write persists the initial value exactly as before without
  // broadcasting it; comparing values (instead of a "mounted" boolean) also
  // keeps StrictMode's dev-only effect re-run from broadcasting on mount.
  const lastWrittenRef = useRef<boolean | null>(null)

  useEffect(() => {
    const prevWritten = lastWrittenRef.current
    lastWrittenRef.current = value
    const received = receivedRef.current
    receivedRef.current = null
    // A sync-received value is already persisted by the instance/tab that
    // originated it — re-persisting here would fire a fresh `storage` event at
    // the origin tab, whose re-persist fires one back: two queued alternating
    // events would ping-pong forever. Skipping the write also lets a cross-tab
    // key REMOVAL stick (we adopt the default without resurrecting the key).
    // The mount write (prevWritten === null) is never skipped.
    if (received === value && prevWritten !== null) return
    safeSetItem(key, value ? '1' : '0')
    if (prevWritten === null || prevWritten === value) return // mount write / no change
    if (typeof window === 'undefined') return
    try {
      window.dispatchEvent(
        new CustomEvent<SyncDetail>(SYNC_EVENT, { detail: { key, value } })
      )
    } catch {
      /* best-effort: a failed broadcast degrades to the old next-mount pickup */
    }
  }, [key, value])

  useEffect(() => {
    if (typeof window === 'undefined') return
    // Cross-tab path: `storage` fires in every OTHER document whose origin
    // shares the store. Parse newValue with the same semantics as the mount
    // read (null -> default, '1' -> true, anything else -> false). The write
    // effect deliberately does NOT re-persist sync-received values, so a
    // cross-tab removal of the key sticks (we adopt the default in memory).
    // A cross-tab localStorage.clear() fires with e.key === null and is
    // ignored (matches pre-live-sync behavior).
    const onStorage = (e: StorageEvent) => {
      try {
        // Ignore events from other storage areas (a sessionStorage write in a
        // same-origin iframe also fires 'storage'); if the comparison itself
        // throws (locked-down storage), fall through — no event would fire
        // from a store we cannot access anyway.
        if (e.storageArea && e.storageArea !== localStorage) return
      } catch {
        /* proceed on the key filter alone */
      }
      if (e.key !== key) return
      const next = e.newValue === null ? defaultValue : e.newValue === '1'
      receivedRef.current = next
      setValue(next)
    }
    // Same-tab path: broadcast from a sibling instance's setter write.
    const onSync = (e: Event) => {
      const detail = (e as CustomEvent<SyncDetail>).detail
      if (!detail || detail.key !== key || typeof detail.value !== 'boolean') return
      receivedRef.current = detail.value
      setValue(detail.value)
    }
    window.addEventListener('storage', onStorage)
    window.addEventListener(SYNC_EVENT, onSync)
    return () => {
      window.removeEventListener('storage', onStorage)
      window.removeEventListener(SYNC_EVENT, onSync)
    }
  }, [key, defaultValue])

  return [value, setValue] as const
}

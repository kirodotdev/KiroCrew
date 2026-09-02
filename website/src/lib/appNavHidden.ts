/**
 * Pin-persistence contract for the sidebar Apps group (Library launchpad).
 *
 * The Library page's launchpad grid lets the user choose which installed
 * apps appear ("pinned") in the sidebar. Persistence stores the HIDDEN set
 * — a JSON string array of app nav ids under `mc-app-nav-hidden` — not the
 * visible set, so a newly installed app defaults to pinned with no
 * migration: an id absent from the list is visible.
 *
 * Both writers/readers (LibraryPage tiles and the App.tsx sidebar filter)
 * MUST go through this module so the contract lives in one place. Writes
 * dispatch `mc:app-nav-hidden-changed` on window because same-tab
 * localStorage writes do not fire the `storage` event — the sidebar
 * subscribes to that event to re-render immediately when a tile's pin
 * badge is toggled.
 */
import { useEffect, useState } from 'react'

import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/** localStorage key holding the JSON string array of HIDDEN app nav ids. */
export const APP_NAV_HIDDEN_KEY = 'mc-app-nav-hidden'

/** Window event dispatched after every persisted change to the hidden set. */
export const APP_NAV_HIDDEN_CHANGED_EVENT = 'mc:app-nav-hidden-changed'

/**
 * Read the hidden set. Malformed JSON, a non-array value, storage denial,
 * or an absent key all degrade to the empty set (everything visible) —
 * never throw. Non-string entries in a tampered array are dropped.
 */
export function readAppNavHidden(): Set<string> {
  const raw = safeGetItem(APP_NAV_HIDDEN_KEY)
  if (raw === null) return new Set()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((id): id is string => typeof id === 'string'))
  } catch {
    return new Set()
  }
}

/**
 * Flip one app id's hidden state. Returns the NEW hidden value so callers
 * can update local UI state without a second read.
 */
export function toggleAppNavHidden(id: string): boolean {
  const next = readAppNavHidden()
  const nowHidden = !next.has(id)
  if (nowHidden) next.add(id)
  else next.delete(id)
  writeAppNavHidden(next)
  return nowHidden
}

/**
 * Subscribe to same-tab hidden-set changes. Returns an unsubscribe
 * function suitable for a useEffect cleanup. Listeners should re-read via
 * `readAppNavHidden()` — the event carries no payload by design (the
 * stored set is the single source of truth).
 */
export function subscribeAppNavHidden(listener: () => void): () => void {
  window.addEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
  return () => window.removeEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
}

/**
 * React view of the hidden set, live under both propagation paths: the
 * module's own change event (same-tab writes; localStorage writes never
 * fire `storage` in their own tab) and the native `storage` event (writes
 * made in ANOTHER tab), mirroring `usePreviewFlag`'s two-listener shape.
 * One implementation so every consumer stays on the same sync contract.
 */
export function useAppNavHidden(): ReadonlySet<string> {
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => readAppNavHidden())
  useEffect(() => {
    const reread = () => setHidden(readAppNavHidden())
    const unsubscribe = subscribeAppNavHidden(reread)
    const onStorage = (e: StorageEvent) => {
      if (e.key === APP_NAV_HIDDEN_KEY) reread()
    }
    window.addEventListener('storage', onStorage)
    return () => {
      unsubscribe()
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return hidden
}

/** localStorage key holding the persisted sidebar Apps-group order. */
export const APP_NAV_ORDER_KEY = 'mc-app-nav-order'

/**
 * Read the CURRENT persisted sidebar order straight from storage.
 *
 * Order writers must merge into this fresh value, never into a React copy:
 * a tab's in-memory order goes stale the moment ANOTHER tab reorders (there
 * is no cross-tab propagation for the order key), and a merge seeded with
 * the stale copy would silently overwrite the other tab's write. Malformed
 * or absent data degrades to the empty array (natural order).
 */
export function readAppNavOrder(): string[] {
  try {
    const parsed = JSON.parse(safeGetItem(APP_NAV_ORDER_KEY) || '[]')
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

/**
 * Build the baseline order a drag-reorder merge runs against: every id the
 * persisted order remembers, IN its persisted positions, plus every current
 * nav id the persisted order has never listed, appended in their effective
 * (natural) relative order.
 *
 * The persisted array is authoritative and must survive wholesale: it can
 * hold ids with NO current nav row at all — a hidden app that is currently
 * disabled, or one mid-uninstall — and a baseline built only from current
 * rows would silently drop their saved slots on the next unrelated drag.
 * The current-ids tail covers the opposite gap: apps never reordered (or a
 * fresh install) whose position exists only implicitly.
 */
export function buildReorderBaseline(
  persistedOrder: readonly string[],
  effectiveAllIds: readonly string[],
): string[] {
  const persisted = new Set(persistedOrder)
  return [...persistedOrder, ...effectiveAllIds.filter(id => !persisted.has(id))]
}

/**
 * Merge a drag-reorder of the VISIBLE app rows back into the full persisted
 * order without erasing hidden rows' slots.
 *
 * `fullOrder` must be the EFFECTIVE full order — every app id, hidden ones
 * included, in the order the rail would show them if all were pinned (the
 * caller derives it from the persisted order with natural order as the
 * fallback for unlisted ids). Passing only the raw persisted array would
 * re-open the hole this function closes: with an empty or incomplete
 * persisted order a hidden app's slot is implicit, and a merge that never
 * saw it persists a visible-only array — re-pinning the app would then dump
 * it at the end instead of where the user left it.
 *
 * Walk `fullOrder` keeping hidden ids in place, fill each visible slot with
 * the next id of the new visible order, and append any visible ids the full
 * order had never seen (their relative dragged order is preserved).
 */
export function mergeVisibleReorder(
  fullOrder: readonly string[],
  visibleIds: readonly string[],
  movedVisible: readonly string[],
): string[] {
  const visible = new Set(visibleIds)
  const queue = [...movedVisible]
  const next: string[] = []
  for (const id of fullOrder) {
    if (visible.has(id)) {
      const filled = queue.shift()
      if (filled) next.push(filled)
    } else {
      next.push(id)
    }
  }
  next.push(...queue)
  return next
}

function writeAppNavHidden(ids: Set<string>): void {
  safeSetItem(APP_NAV_HIDDEN_KEY, JSON.stringify([...ids].sort()))
  // Same-tab sync: localStorage writes only fire `storage` in OTHER tabs,
  // and mc-app-nav-order has no live propagation to piggyback on (Step 1
  // finding), so dispatch our own event after every write.
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(APP_NAV_HIDDEN_CHANGED_EVENT))
  }
}

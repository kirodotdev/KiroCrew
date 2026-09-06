/**
 * Pin-persistence contract for PROMOTED SUB-ITEMS on the left nav rail.
 *
 * A `pinnable` surface (see `Surface.pinnable`) is a destination that lives
 * inside another surface's secondary panel — Agent Capabilities' Steering
 * files, Skills, Hooks and so on. It is registered like any other surface, so
 * it is routable and covered by the registry-wide invariants, but it occupies
 * a rail row only while the user has promoted it here.
 *
 * WHY THIS STORES THE PINNED SET WHILE `appNavHidden` STORES THE HIDDEN ONE.
 * Both keys exist so that "an id absent from storage" means "leave this row
 * exactly as it shipped", which is what lets a fresh install and an upgrade
 * skip migration entirely. For apps that default VISIBLE, so the absent-means-
 * default set is the HIDDEN one. A promoted sub-item defaults to NOT on the
 * rail, so here the absent-means-default set is the PINNED one. Storing the
 * hidden set for these would invert it: every pinnable sub-item would have to
 * be seeded as hidden on first run, and any sub-item added later would appear
 * on every existing user's rail unasked — the exact migration
 * `appNavHidden`'s comment says the shape is chosen to avoid.
 *
 * Both writers/readers MUST go through this module so the contract lives in
 * one place. Writes dispatch `mc:nav-pinned-changed` on window because
 * same-tab localStorage writes do not fire the `storage` event — the rail
 * subscribes to that event to re-render immediately when the pin control is
 * toggled from a page header.
 */
import { useEffect, useState } from 'react'

import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/** localStorage key holding the JSON string array of PINNED surface navIds. */
export const NAV_PINNED_KEY = 'mc-nav-pinned'

/** Window event dispatched after every persisted change to the pinned set. */
export const NAV_PINNED_CHANGED_EVENT = 'mc:nav-pinned-changed'

/**
 * How many sub-items may be promoted at once.
 *
 * The rail's Main group lives in a `shrink-0` block that does not scroll, so
 * rows added there take height from the one flex region that does (the app
 * list) rather than becoming scrollable themselves. A cap is what keeps that
 * bounded, and it is the same defence `APPS_NAV_LIMIT` already provides for
 * the app list. Reaching the cap refuses the new pin rather than evicting an
 * existing one: silently dropping a row the user pinned earlier is a worse
 * outcome than a refused click, because nothing would tell them it happened.
 */
export const NAV_PINNED_LIMIT = 5

/**
 * Read the pinned set. Malformed JSON, a non-array value, storage denial, or
 * an absent key all degrade to the empty set (nothing promoted — the rail
 * exactly as it shipped) and never throw. Non-string entries in a tampered
 * array are dropped.
 *
 * The cap is applied on READ as well as on write, so a set that grew past it
 * by a hand-edit or by a future lower cap cannot render an unbounded rail.
 */
export function readNavPinned(): Set<string> {
  const raw = safeGetItem(NAV_PINNED_KEY)
  if (raw === null) return new Set()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(
      parsed.filter((id): id is string => typeof id === 'string').slice(0, NAV_PINNED_LIMIT),
    )
  } catch {
    return new Set()
  }
}

/**
 * Flip one surface's pinned state. Returns the NEW pinned value so callers can
 * update local UI state without a second read.
 *
 * A pin that would exceed `NAV_PINNED_LIMIT` is refused: the set is left
 * untouched and `false` is returned, so the caller's control stays in its
 * unpinned state rather than reporting a promotion that did not happen.
 * Unpinning is never refused.
 */
export function toggleNavPinned(id: string): boolean {
  const next = readNavPinned()
  if (next.has(id)) {
    next.delete(id)
    writeNavPinned(next)
    return false
  }
  if (next.size >= NAV_PINNED_LIMIT) return false
  next.add(id)
  writeNavPinned(next)
  return true
}

/**
 * Subscribe to same-tab pinned-set changes. Returns an unsubscribe function
 * suitable for a useEffect cleanup. Listeners should re-read via
 * `readNavPinned()` — the event carries no payload by design (the stored set
 * is the single source of truth).
 */
export function subscribeNavPinned(listener: () => void): () => void {
  window.addEventListener(NAV_PINNED_CHANGED_EVENT, listener)
  return () => window.removeEventListener(NAV_PINNED_CHANGED_EVENT, listener)
}

/**
 * React view of the pinned set, live under both propagation paths: the
 * module's own change event (same-tab writes; localStorage writes never fire
 * `storage` in their own tab) and the native `storage` event (writes made in
 * ANOTHER tab), matching `useAppNavHidden`'s two-listener shape so every
 * sidebar consumer stays on one sync contract.
 */
export function useNavPinned(): ReadonlySet<string> {
  const [pinned, setPinned] = useState<ReadonlySet<string>>(() => readNavPinned())
  useEffect(() => {
    const reread = () => setPinned(readNavPinned())
    const unsubscribe = subscribeNavPinned(reread)
    const onStorage = (e: StorageEvent) => {
      if (e.key === NAV_PINNED_KEY) reread()
    }
    window.addEventListener('storage', onStorage)
    return () => {
      unsubscribe()
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return pinned
}

function writeNavPinned(ids: Set<string>): void {
  safeSetItem(NAV_PINNED_KEY, JSON.stringify([...ids].sort()))
  // Same-tab sync: localStorage writes only fire `storage` in OTHER tabs, so
  // dispatch our own event after every write (same reason `appNavHidden` does).
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(NAV_PINNED_CHANGED_EVENT))
  }
}

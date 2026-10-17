import { safeGetItem, safeSetItem } from './safeStorage'

/** Browser-local order of pinned session keys, shared by expanded sidebar views. */
export const PINNED_SESSION_ORDER_KEY = 'mc-pinned-session-order'

/** Invalid or unavailable storage degrades to the natural pinned order. */
export function readPinnedSessionOrder(): string[] {
  try {
    const parsed: unknown = JSON.parse(safeGetItem(PINNED_SESSION_ORDER_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter((key): key is string => typeof key === 'string')
  } catch {
    return []
  }
}

/**
 * Keep stored keys that are still pinned, discard duplicates/stale keys, then
 * append newly pinned sessions in the caller's natural sort order.
 */
export function reconcilePinnedSessionOrder(
  stored: readonly string[],
  natural: readonly string[],
): string[] {
  const valid = new Set(natural)
  const seen = new Set<string>()
  const out: string[] = []
  for (const key of stored) {
    if (valid.has(key) && !seen.has(key)) {
      seen.add(key)
      out.push(key)
    }
  }
  for (const key of natural) {
    if (!seen.has(key)) {
      seen.add(key)
      out.push(key)
    }
  }
  return out
}

/** Move one pinned key to another key's position. Unknown/equal keys are inert. */
export function movePinnedSession(
  order: readonly string[],
  activeKey: string,
  overKey: string,
): string[] {
  const from = order.indexOf(activeKey)
  const to = order.indexOf(overKey)
  if (from < 0 || to < 0 || from === to) return [...order]
  const next = [...order]
  const [moved] = next.splice(from, 1)
  next.splice(to, 0, moved)
  return next
}

export function persistPinnedSessionOrder(order: readonly string[]): void {
  safeSetItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(order))
}

/** Same-tab signal emitted after a pin mutation is authoritatively accepted. */
export const PINNED_SESSION_ORDER_CHANGED_EVENT = 'mc-pinned-session-order-changed'

/** Persist rank against authoritative pinned membership from a fresh slots snapshot. */
export function commitPinnedSessionSnapshot(
  pinnedKeys: readonly string[],
  baseline: readonly string[] = [],
  newlyPinnedKeys: readonly string[] = [],
  expectedStoredBaseline?: readonly string[],
): string[] {
  const newlyPinned = new Set(newlyPinnedKeys)
  const natural = reconcilePinnedSessionOrder(baseline, pinnedKeys)
  const stored = readPinnedSessionOrder()
  const storageUnchanged = expectedStoredBaseline === undefined
    || (stored.length === expectedStoredBaseline.length
      && stored.every((key, index) => key === expectedStoredBaseline[index]))
  // Filter stale occurrences only while storage still matches the mutation's
  // capture. A concurrent manual reorder is newer rank authority and wins.
  const ranked = storageUnchanged ? stored.filter(key => !newlyPinned.has(key)) : stored
  const next = reconcilePinnedSessionOrder(ranked, natural)
  persistPinnedSessionOrder(next)
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(PINNED_SESSION_ORDER_CHANGED_EVENT))
  return next
}

export interface PinnedSessionMembershipOperation {
  key: string
  pinned: boolean
}

/** Commit one or more successful membership changes against one authoritative baseline. */
export function commitPinnedSessionOperations(
  operations: readonly PinnedSessionMembershipOperation[],
  baseline: readonly string[] = [],
  expectedStoredBaseline?: readonly string[],
): string[] {
  const stored = readPinnedSessionOrder()
  const storageUnchanged = expectedStoredBaseline === undefined
    || (stored.length === expectedStoredBaseline.length
      && stored.every((key, index) => key === expectedStoredBaseline[index]))
  let next = baseline.length > 0 && storageUnchanged
    ? reconcilePinnedSessionOrder(stored, baseline)
    : stored
  for (const { key, pinned } of operations) {
    next = pinned
      ? (next.includes(key) ? next : [...next, key])
      : next.filter(candidate => candidate !== key)
  }
  if (operations.length > 0) {
    persistPinnedSessionOrder(next)
    if (typeof window !== 'undefined') window.dispatchEvent(new Event(PINNED_SESSION_ORDER_CHANGED_EVENT))
  }
  return next
}

/**
 * Commit membership only after the pin API succeeds. Optimistic Redux updates
 * must not prune or append storage: a rejected mutation rolls back, and keeping
 * storage untouched preserves the session's previous rank exactly.
 */
export function commitPinnedSessionMembership(
  key: string,
  pinned: boolean,
  baseline: readonly string[] = [],
): void {
  commitPinnedSessionOperations([{ key, pinned }], baseline)
}

import { safeGetItem, safeRemoveItem } from './safeStorage'

/**
 * The order of pinned sessions lives on the gateway (`chat_pinned_order.json`,
 * written by `POST /api/chat/pinned-order`). Each slot row carries its place as
 * `pin_rank`: an index into one global list, or `null` when the session has no
 * stored place. Every surface ranks pinned rows from that field, so the sidebar,
 * the flyout, the Sessions page and every browser the person opens agree.
 *
 * Pinned rows without a rank follow the ranked ones in the surface's own sort.
 * No reorder is needed for rows to gain a rank: the sidebar's first load seeds
 * the order it is showing (this browser's old local order, then the natural
 * order), so later pins append instead of re-sorting. A board view seeds only
 * when this browser kept an old local order. Until a seed or a reorder is
 * saved, pinned rows stay unranked and keep the plain sort: on a board view
 * with no old local order, on an empty roster, before the seed's request
 * succeeds, and after a failed seed until the next page load. When the gateway
 * refuses the seed because an order is already stored, the rows take that
 * stored order from the next slots frame.
 */

/**
 * Where this browser kept the order before the gateway did. Read once, handed
 * to the gateway, then removed (see ChatSidebar's migration effect).
 */
export const LEGACY_PINNED_SESSION_ORDER_KEY = 'mc-pinned-session-order'

/** The legacy browser-local order. Invalid or unavailable storage reads as none. */
export function readLegacyPinnedSessionOrder(): string[] {
  try {
    const parsed: unknown = JSON.parse(safeGetItem(LEGACY_PINNED_SESSION_ORDER_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter((key): key is string => typeof key === 'string')
  } catch {
    return []
  }
}

export function clearLegacyPinnedSessionOrder(): void {
  safeRemoveItem(LEGACY_PINNED_SESSION_ORDER_KEY)
}

interface RankedRow {
  key: string
  pinned?: boolean
  pin_rank?: number | null
}

/** Keys of pinned rows that have a stored place, in that order. */
export function rankedPinnedKeys(rows: readonly RankedRow[]): string[] {
  return rows
    .filter((row): row is RankedRow & { pin_rank: number } => !!row.pinned && typeof row.pin_rank === 'number')
    .sort((a, b) => a.pin_rank - b.pin_rank)
    .map(row => row.key)
}

/**
 * Keep stored keys that are still pinned, discard duplicates/stale keys, then
 * append the remaining pinned sessions in the caller's natural sort order.
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

import { safeGetItem, safeRemoveItem, safeSetItem } from './safeStorage'
import { pinMutationsAreInFlight } from './pinMutationsInFlight'

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

export function persistPinnedSessionOrder(order: readonly string[]): boolean {
  return safeSetItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(order))
}

/** Same-tab signal emitted after a pin mutation is authoritatively accepted. */
export const PINNED_SESSION_ORDER_CHANGED_EVENT = 'mc-pinned-session-order-changed'

/**
 * Set once the user has explicitly reordered the pinned section. Cleared by an explicit
 * "Follow sort order", and removed outright once nothing is pinned.
 *
 * The stored ORDER cannot stand in for that intent. Membership bookkeeping writes it
 * from the first pin onwards, so it is non-empty long before anyone drags a row --
 * and consuming rank on that basis froze the pinned section at whatever order a pin
 * toggle happened to capture, leaving the sidebar's chosen sort with no effect on any
 * pinned row. Rank is a user preference, so it applies only once the user has stated
 * one; until then the pinned section follows the active sort key like every other row.
 */
export const PINNED_SESSION_ORDER_MANUAL_KEY = 'mc-pinned-session-order-manual'

export function readPinnedSessionOrderIsManual(): boolean {
  return safeGetItem(PINNED_SESSION_ORDER_MANUAL_KEY) === '1'
}

/**
 * The one rule for "authoritative membership says the arrangement is over".
 *
 * Two channels reach it, because they genuinely differ: slot frames arrive as redux actions, while
 * a pin reconciliation runs through React Query and dispatches none of them — so with the socket
 * down, an accepted last-pin unpin reaches only the second. Both call this, so the decision has
 * one implementation and neither channel can drift from the other.
 *
 * Both freshness tests live HERE rather than in the callers. Holding them in the redux arms alone
 * left the reconcile channel settling on a read taken before the arrangement existed, so a report
 * of "nothing pinned" that was already stale when it arrived erased a newer arrangement.
 */
export function settlePinnedArrangementAgainstMembership(pinnedKeys: readonly string[]): void {
  if (pinnedKeys.length !== 0) return
  // This tab's own unanswered pin is the ONLY staleness guard: a reply issued before those pins
  // existed is accepted once they are reconciled, and the cost of that is one re-drag.
  if (pinMutationsAreInFlight()) return
  forgetPinnedSessionOrderManual()
}

/**
 * Records that the upgrade adoption below has already been considered, so it happens once per
 * browser. The guard is load-bearing rather than tidiness: ending an arrangement REMOVES the marker,
 * so "no marker" cannot tell a user who never arranged from one who just chose to follow the sort —
 * without this, every reload would re-adopt and silently undo that choice.
 */
export const PINNED_SESSION_ORDER_ADOPTED_KEY = 'mc-pinned-session-order-adopted'

/**
 * One-shot upgrade path: treat an order stored before this release as a stated arrangement.
 *
 * Rank became opt-in here, so a user who had dragged their pinned rows holds the order but no marker,
 * and would see the arrangement they curated replaced by the active sort with nothing offering it
 * back. Adopting it once keeps what they built.
 *
 * The cost is recorded rather than buried: a stored order is ALSO what plain pin bookkeeping leaves
 * behind, and nothing distinguishes the two from before the marker existed. So a user who only ever
 * pinned is adopted too, and keeps the pre-release behaviour until they use the pinned-row menu once.
 */
export function adoptStoredPinnedOrderAsManual(): boolean {
  if (safeGetItem(PINNED_SESSION_ORDER_ADOPTED_KEY) === '1') return false
  const alreadyStated = readPinnedSessionOrderIsManual()
  const hasStoredOrder = readPinnedSessionOrder().length > 0
  if (alreadyStated || !hasStoredOrder) {
    // Nothing to adopt, so the question is settled: record it and never ask again.
    safeSetItem(PINNED_SESSION_ORDER_ADOPTED_KEY, '1')
    return false
  }
  // Recorded only once the latch has actually taken. Writing it first consumed the one chance to
  // adopt even when the latch failed at quota, so the arrangement was lost with no later retry.
  const adopted = markPinnedSessionOrderManual()
  if (!adopted) return false
  if (!safeSetItem(PINNED_SESSION_ORDER_ADOPTED_KEY, '1')) {
    // The pair has to land together. A marker with no receipt is re-adopted on the next load, so a
    // user who then chose "Follow sort order" would silently get the arrangement back.
    forgetPinnedSessionOrderManual()
    return false
  }
  return true
}

/** Monotonic across tabs; a tab that states an arrangement also advances its OWN high-water mark. */
export const PINNED_SESSION_ORDER_REVISION_KEY = 'mc-pinned-session-order-revision'

// Seeded from storage, not 0: the shared revision outlives a reload, so starting at 0 would read every
// persisted revision as an unseen sibling update and refuse every settlement for the life of the page.
let revisionSeenHere = readSharedArrangementRevision()

export function readSharedArrangementRevision(): number {
  const raw = safeGetItem(PINNED_SESSION_ORDER_REVISION_KEY)
  const parsed = raw === null ? 0 : Number.parseInt(raw, 10)
  return Number.isFinite(parsed) ? parsed : 0
}

/** True when some OTHER tab has stated an arrangement this tab has not accounted for yet. */
export function arrangementStatedElsewhere(): boolean {
  return readSharedArrangementRevision() > revisionSeenHere
}

export function noteArrangementRevisionSeen(): void {
  revisionSeenHere = readSharedArrangementRevision()
}

/** Record the intent, then signal so every surface re-reads it in the same tab. */
export function markPinnedSessionOrderManual(): boolean {
  // Revision FIRST: a marker written against a revision that failed to persist disarms the sibling
  // staleness guard, so the whole statement aborts rather than landing half of itself.
  const next = readSharedArrangementRevision() + 1
  if (!safeSetItem(PINNED_SESSION_ORDER_REVISION_KEY, String(next))) return false
  revisionSeenHere = next
  if (!safeSetItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')) return false
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(PINNED_SESSION_ORDER_CHANGED_EVENT))
  return true
}

/**
 * End the arrangement: the marker is removed, so the pinned section follows the active sort
 * again until the user states a new arrangement by reordering.
 *
 * One function serves both callers, because there is exactly one thing to say. The user asking
 * for it explicitly ("Follow sort order (all pinned)") and authoritative membership going empty
 * are different REASONS to stop honouring an arrangement, not different outcomes: in both cases
 * the arrangement no longer describes anything. Keeping it in the second case would attribute
 * intent to a LATER pin set the user never arranged, since membership bookkeeping rebuilds an
 * order in pin-toggle sequence and a stale marker then re-freezes the section -- the exact
 * symptom this gate exists to remove, resurfacing under its own principle.
 *
 * The stored ORDER is deliberately left alone. It is bookkeeping the pin paths own, and removing
 * it here would destroy membership state on a preference change.
 */
export function forgetPinnedSessionOrderManual(): void {
  if (safeGetItem(PINNED_SESSION_ORDER_MANUAL_KEY) === null) return
  safeRemoveItem(PINNED_SESSION_ORDER_MANUAL_KEY)
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(PINNED_SESSION_ORDER_CHANGED_EVENT))
}

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
  // This snapshot IS authoritative membership, taken from a fresh slots read. With the socket
  // down it is the only channel an accepted last-pin unpin reaches.
  settlePinnedArrangementAgainstMembership(next)
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
    // Reached when the reconciliation GET failed, so this is the only membership statement that
    // arrives before the user re-pins; without it a stale marker refreezes their next pin set.
    settlePinnedArrangementAgainstMembership(next)
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

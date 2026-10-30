import type { QueryClient } from '@tanstack/react-query'

/**
 * Cross-consumer ledger for in-flight reaction toggles, shared by
 * `useChatReactions` (the mutations), the `reactions_changed` WebSocket frame
 * router, and the reconnect catch-up — none of which can hold the others'
 * React state. A toggle holds an optimistic entry in the
 * `['chat-reactions', slot]` cache until its request settles; refetching that
 * cache while the entry is pending drops it, and a queued off-toggle then reads
 * the identity as absent and sends POST instead of DELETE, persisting the
 * reaction the user ended OFF. So every refetch of a reaction query — the
 * hook's own settled refetch, a cross-tab `reactions_changed` frame, and the
 * reconnect reconcile — is routed through here and held until the slot's
 * pending count drains. Keyed by `slot_key` so a toggle on one slot never
 * defers another slot's refresh.
 *
 * This lives in its own module (no React imports) so the hook, the socket leaf
 * and the reconnect path can all share ONE singleton without a hook importing a
 * leaf or a leaf importing a hook.
 */

export const reactionQueryKey = (slotKey: string | undefined) =>
  ['chat-reactions', slotKey] as const

// Pending toggles and held-back refreshes, kept per QueryClient. An embedded
// app chat runs on its own client (`app-sdk/appQueryClient.ts`), holding its
// own copy of a slot's reactions cache and its own optimistic rows. A toggle on
// one client must defer refreshes of THAT client's cache only, and its drain
// must flush that client: keyed by slot alone, a toggle in the app chat would
// hold back the dashboard's refresh and then drop it on drain, leaving the
// dashboard stale.
interface ClientLedger {
  pending: Map<string, number>
  // Slots whose refetch was held back while a toggle was pending — a
  // cross-tab `reactions_changed` frame or a reconnect reconcile that must
  // still land. Cleared when the slot's pending count hits zero, because the
  // drain's own refetch covers it.
  refetchWhenDrained: Set<string>
}
let ledgerByClient = new WeakMap<QueryClient, ClientLedger>()

function ledgerFor(qc: QueryClient): ClientLedger {
  let ledger = ledgerByClient.get(qc)
  if (!ledger) {
    ledger = { pending: new Map(), refetchWhenDrained: new Set() }
    ledgerByClient.set(qc, ledger)
  }
  return ledger
}

/** Register that a toggle has started for `slotKey` on `qc`. */
export function beginToggle(qc: QueryClient, slotKey: string): void {
  const { pending } = ledgerFor(qc)
  pending.set(slotKey, (pending.get(slotKey) ?? 0) + 1)
}

/** Release a settled toggle for `slotKey` on `qc`. When its last toggle
 *  drains, fire the single settled refetch — the server truth the hook always
 *  wants once its own toggles drain, and the flush of any refresh held back
 *  meanwhile. */
export function endToggle(qc: QueryClient, slotKey: string): void {
  const { pending, refetchWhenDrained } = ledgerFor(qc)
  const next = Math.max(0, (pending.get(slotKey) ?? 0) - 1)
  if (next > 0) {
    pending.set(slotKey, next)
    return
  }
  pending.delete(slotKey)
  refetchWhenDrained.delete(slotKey)
  qc.invalidateQueries({ queryKey: reactionQueryKey(slotKey) })
}

/** True when a reaction toggle is still in flight for `slotKey` on `qc`. A
 *  refetch checks this so it never stomps an optimistic entry a queued
 *  off-toggle still needs to read. */
export function reactionTogglePending(qc: QueryClient, slotKey: string): boolean {
  return (ledgerFor(qc).pending.get(slotKey) ?? 0) > 0
}

/** Record that a refresh for `slotKey` on `qc` was held back because a toggle
 *  was in flight; it is flushed when the last toggle settles. */
export function deferReactionInvalidation(qc: QueryClient, slotKey: string): void {
  ledgerFor(qc).refetchWhenDrained.add(slotKey)
}

/** Invalidate (or defer) the reaction query for ONE slot on ONE client,
 *  respecting that client's pending toggles. */
export function invalidateReactionsForSlot(qc: QueryClient, slotKey: string): void {
  if (reactionTogglePending(qc, slotKey)) {
    deferReactionInvalidation(qc, slotKey)
  } else {
    qc.invalidateQueries({ queryKey: reactionQueryKey(slotKey) })
  }
}

/** `invalidateReactionsForSlot` on every client in `clients`, each once. The
 *  `reactions_changed` frame passes the dashboard client plus every registered
 *  app client, so an embedded app chat showing the same slot refreshes too. */
export function invalidateReactionsForSlotOn(clients: Iterable<QueryClient>, slotKey: string): void {
  for (const qc of new Set(clients)) invalidateReactionsForSlot(qc, slotKey)
}

/** After a reconnect, reconcile every cached reaction query: a
 *  `reactions_changed` frame pushed while the socket was down was never
 *  delivered, and the query carries the app-wide `staleTime: Infinity`, so the
 *  view would show stale markers until a reload or a local toggle. Invalidate
 *  each observed slot's query on every client passed (the dashboard's and each
 *  registered app client's), deferring any slot with a pending toggle on that
 *  client so the refetch cannot drop a still-pending optimistic entry. */
export function reconcileReactionsOnReconnect(clients: Iterable<QueryClient>): void {
  for (const qc of new Set(clients)) {
    const queries = qc.getQueryCache().findAll({ queryKey: ['chat-reactions'] })
    for (const q of queries) {
      const slotKey = q.queryKey[1]
      if (typeof slotKey === 'string' && slotKey) invalidateReactionsForSlot(qc, slotKey)
    }
  }
}

/** One identity's toggle chain: `tail` serializes its writes, `want` is the
 *  user's LATEST intended state for it. */
export interface ReactionToggleChain {
  tail: Promise<void>
  want: boolean
}

// The per-identity toggle chains, one map per QueryClient. They live here, not
// in a `useRef` inside the hook, because the hook remounts (the embedded
// companion chat closes and reopens) while the QueryClient — and the optimistic
// row a pending POST left in its cache — survives. A per-mount map would start
// empty on the new mount, so a remove there would not wait for the add the old
// mount still has in flight: its DELETE 404s first (counted as success), the
// late POST then persists the reaction, and the marker the user removed comes
// back. Keying by QueryClient matches the cache the chain is coordinating with,
// and a WeakMap lets a discarded client (a test's) take its chains with it.
let toggleChainsByClient = new WeakMap<QueryClient, Map<string, ReactionToggleChain>>()

/** The toggle chains shared by every hook mount on `qc`. */
export function reactionToggleChains(qc: QueryClient): Map<string, ReactionToggleChain> {
  let chains = toggleChainsByClient.get(qc)
  if (!chains) {
    chains = new Map()
    toggleChainsByClient.set(qc, chains)
  }
  return chains
}

/** A failed reaction, scoped to the slot AND message it happened on. Both are
 *  needed: `fork_slot` copies a message's `mid` into the new fork, so matching
 *  on `mid` alone would surface a slot A quota error on the identical message
 *  in fork B — a notice in a conversation that never failed. The row's bar
 *  shows the notice only when its own slot and mid both match. */
export interface ReactionError {
  slot: string
  mid: string
  /** The emoji the failed toggle was for; the add- and remove-failure notices
   *  name it, the limit notice does not. */
  emoji: string
  kind: 'react' | 'unreact' | 'limit'
  /** On a `limit` failure, the per-chat cap the server reported, so the
   *  notice can state it. Absent when the response did not carry one. */
  limit?: number
}

/** Read the per-chat cap from a 409 body (`{"code": "reaction_limit_reached",
 *  "limit": N}`); undefined when the body is missing or malformed. */
export function reactionLimitFromError(err: unknown): number | undefined {
  const body = typeof err === 'object' && err !== null ? (err as { body?: unknown }).body : undefined
  if (typeof body !== 'string' || !body) return undefined
  try {
    const limit = (JSON.parse(body) as { limit?: unknown }).limit
    return typeof limit === 'number' && Number.isInteger(limit) && limit > 0 ? limit : undefined
  } catch {
    return undefined
  }
}

/** The latest reaction failure per slot, readable by every hook mount on one
 *  client. */
export interface ReactionErrorStore {
  get: (slotKey: string | undefined) => ReactionError | null
  /** Record `error` for its own slot. */
  set: (error: ReactionError) => void
  /** Clear the failure recorded for `slotKey`, if any. */
  clear: (slotKey: string) => void
  subscribe: (listener: () => void) => () => void
}

// Failures per QueryClient, for the same reason the toggle chains are: a
// mutation started on a mount that has since unmounted still settles, and its
// failure must reach the mount now on screen. Held in the hook's own state it
// would land on the dead mount, so the rollback would put the reaction back
// with no notice saying the remove failed.
//
// Keyed by slot within the client, because one client serves several mounts
// at once (the main chat and the companion panel share the dashboard client).
// A single shared slot would let a toggle or a failure in one conversation
// clear or overwrite the notice another is showing.
let errorStoreByClient = new WeakMap<QueryClient, ReactionErrorStore>()

/** The reaction-failure store shared by every hook mount on `qc`. */
export function reactionErrorStore(qc: QueryClient): ReactionErrorStore {
  let store = errorStoreByClient.get(qc)
  if (!store) {
    const bySlot = new Map<string, ReactionError>()
    const listeners = new Set<() => void>()
    const notify = () => { for (const listener of listeners) listener() }
    store = {
      get: (slotKey) => (slotKey ? bySlot.get(slotKey) ?? null : null),
      set: (error) => {
        if (bySlot.get(error.slot) === error) return
        bySlot.set(error.slot, error)
        notify()
      },
      clear: (slotKey) => {
        if (bySlot.delete(slotKey)) notify()
      },
      subscribe: (listener) => {
        listeners.add(listener)
        return () => { listeners.delete(listener) }
      },
    }
    errorStoreByClient.set(qc, store)
  }
  return store
}

/** Test seam: drop all cross-consumer toggle state between cases. */
export function __resetReactionToggleLedgerForTests(): void {
  ledgerByClient = new WeakMap()
  toggleChainsByClient = new WeakMap()
  errorStoreByClient = new WeakMap()
}

/** Test seam: pin a slot's pending-toggle count so a frame/reconnect test can
 *  drive the deferral branch without standing up real mutations. */
export function __setReactionTogglePendingForTests(qc: QueryClient, slotKey: string, count: number): void {
  const { pending } = ledgerFor(qc)
  if (count <= 0) pending.delete(slotKey)
  else pending.set(slotKey, count)
}

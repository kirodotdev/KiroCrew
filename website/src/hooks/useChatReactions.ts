import { useCallback, useMemo, useRef, useSyncExternalStore } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { reactionsApi, type ChatReaction, type ReactMessageBody } from '../api/reactions'
import { secureRandomId } from '../utils/secureId'
import { reactionQueryKey, beginToggle, endToggle, reactionToggleChains, reactionErrorStore, reactionLimitFromError } from './reactionToggleLedger'
import { refetchWhileFailed } from '../api/queryClient'

// The cross-consumer in-flight ledger lives in `./reactionToggleLedger` (no
// React imports) so the hook, the `reactions_changed` frame router and the
// reconnect reconcile can share ONE singleton. Re-exported here so existing
// importers keep their path.
export {
  reactionTogglePending,
  deferReactionInvalidation,
  invalidateReactionsForSlot,
  reconcileReactionsOnReconnect,
  __resetReactionToggleLedgerForTests,
  __setReactionTogglePendingForTests,
} from './reactionToggleLedger'
export type { ReactionError } from './reactionToggleLedger'

const NO_REACTIONS: ReadonlySet<string> = new Set()

/**
 * Hook to manage message reactions for a given slot.
 *
 * A reaction toggle is idempotent on `(slot_key, mid, emoji)` with no ordering,
 * no preview text and no jump-navigation. The one coordination it DOES need is
 * per-identity serialization: a quick on-then-off double click would otherwise
 * fire an add and a remove concurrently, and because the bodyless DELETE can
 * 404 before the POST takes the backend lock, the server could persist the
 * reaction the user ended on OFF. So toggles for a given `(mid, emoji)` are
 * chained here — the latest requested state is applied after the one in flight
 * settles — rather than racing.
 */
export function useChatReactions(slotKey: string | undefined) {
  const qc = useQueryClient()
  const queryKey = useMemo(() => reactionQueryKey(slotKey), [slotKey])
  // The failure lives in a store shared by every mount on this QueryClient,
  // not in this mount's state: a toggle can settle after the mount that started
  // it unmounted, and its notice must show on the mount that replaced it.
  // Keyed by slot, so this mount reads only its own conversation's failure.
  const errorStore = useMemo(() => reactionErrorStore(qc), [qc])
  const readError = useCallback(() => errorStore.get(slotKey), [errorStore, slotKey])
  const error = useSyncExternalStore(errorStore.subscribe, readError, readError)
  const setError = errorStore.set
  const clearError = useCallback(() => { if (slotKey) errorStore.clear(slotKey) }, [errorStore, slotKey])

  const { data: reactions = [], isLoading: loading, isError: loadFailed } = useQuery<ChatReaction[]>({
    queryKey,
    queryFn: async () => {
      const res = await reactionsApi.list(slotKey!)
      return res.reactions
    },
    enabled: !!slotKey,
    // The app-wide default is staleTime:Infinity, and with the socket still up
    // nothing re-reads this query, so after retries give up on a transient
    // failure the "Could not load reactions… Retrying automatically" notice
    // would stay up forever. refetchWhileFailed re-reads every 30s WHILE the
    // query is errored (never while it holds an answer), making that promise
    // true without polling a healthy query.
    refetchInterval: refetchWhileFailed,
  })

  // One Set per message, rebuilt only when the reactions list changes, and
  // reused for a message whose emoji did not change. The rows are memoized and
  // a reply streams a render per chunk, so a fresh Set per call would re-render
  // every reacted row on every chunk.
  const setsRef = useRef(new Map<string, ReadonlySet<string>>())
  const setsByMid = useMemo(() => {
    const grouped = new Map<string, string[]>()
    for (const r of reactions) {
      const list = grouped.get(r.mid)
      if (list) list.push(r.emoji)
      else grouped.set(r.mid, [r.emoji])
    }
    const next = new Map<string, ReadonlySet<string>>()
    for (const [mid, emoji] of grouped) {
      const prev = setsRef.current.get(mid)
      const same = prev && prev.size === new Set(emoji).size && emoji.every(e => prev.has(e))
      next.set(mid, same ? prev : new Set(emoji))
    }
    setsRef.current = next
    return next
  }, [reactions])

  /** Every emoji the CURRENT caller has placed on `mid`. The same Set comes
   *  back until that message's reactions change. */
  const reactionsFor = useCallback(
    (mid: string): ReadonlySet<string> => setsByMid.get(mid) ?? NO_REACTIONS,
    [setsByMid],
  )

  // Every mutation carries the slot it STARTED on through its variables, and
  // every callback derives its cache key and ledger slot from that — never from
  // the hook's render-captured `slotKey`. The hook is reused across slots
  // (ChatPage keeps one instance and swaps the active slot), so a reaction
  // started in slot A can settle after the user has switched to B. Reading the
  // render slot in onSettled would then release B's pending count and refetch
  // B's cache while A's count stays stuck above zero — stranding A's deferred
  // `reactions_changed` refreshes for the rest of the session. Keying off the
  // originating slot keeps each mutation's bookkeeping on its own slot.
  const { mutateAsync: addReactionAsync } = useMutation({
    mutationFn: (body: ReactMessageBody) => reactionsApi.create(body),
    onMutate: async (body: ReactMessageBody) => {
      const sk = body.slot_key
      const qk = reactionQueryKey(sk)
      beginToggle(qc, sk)
      await qc.cancelQueries({ queryKey: qk })
      const optimisticId = `temp-${secureRandomId()}`
      const optimistic: ChatReaction = {
        id: optimisticId,
        slot_key: sk,
        mid: body.mid,
        emoji: body.emoji,
        reacted_at: new Date().toISOString(),
      }
      qc.setQueryData<ChatReaction[]>(qk, old => [...(old ?? []), optimistic])
      return { optimisticId, sk, qk }
    },
    onError: (err: unknown, body, ctx) => {
      // Drop THIS optimistic entry by id rather than restoring a captured
      // snapshot: when the initial GET failed there is no prior snapshot to
      // restore, and a snapshot would also clobber other identities' pending
      // entries. Removing by id leaves the failed reaction off; the chained
      // toggle's own per-identity intent (not this cache) decides the next
      // queued write, so a queued off-click still resolves to "leave it off".
      if (ctx?.optimisticId) {
        qc.setQueryData<ChatReaction[]>(ctx.qk, old =>
          (old ?? []).filter(r => r.id !== ctx.optimisticId),
        )
      }
      const atLimit = typeof err === 'object' && err !== null && (err as { status?: number }).status === 409
      setError(atLimit
        ? { slot: body.slot_key, mid: body.mid, emoji: body.emoji, kind: 'limit', limit: reactionLimitFromError(err) }
        : { slot: body.slot_key, mid: body.mid, emoji: body.emoji, kind: 'react' })
    },
    onSuccess: (real, _body, ctx) => {
      if (!ctx) return
      // Replace the optimistic row with the server record. The pending-toggle
      // ledger holds every refetch of this slot (the settled refetch, the
      // `reactions_changed` frame, the reconnect reconcile) until the toggle
      // drains, so the optimistic row is still present here — a plain map.
      qc.setQueryData<ChatReaction[]>(ctx.qk, old =>
        (old ?? []).map(r => r.id === ctx.optimisticId ? real : r),
      )
    },
    onSettled: (_data, _err, _body, ctx) => { if (ctx) endToggle(qc, ctx.sk) },
  })

  const { mutateAsync: removeReactionAsync } = useMutation({
    mutationFn: async ({ slot_key, mid, emoji }: { slot_key: string; mid: string; emoji: string }) => {
      try {
        return await reactionsApi.remove(slot_key, mid, emoji)
      } catch (err) {
        // A 404 means the reaction is already gone — which is exactly the end
        // state a remove wants, so treat it as success rather than surfacing an
        // "unreact failed" notice. This makes DELETE safe to send even when the
        // cache shows the reaction absent (see toggleReaction: a lost POST
        // response can leave the server holding a reaction the cache dropped).
        if (typeof err === 'object' && err !== null && (err as { status?: number }).status === 404) {
          return { ok: true }
        }
        throw err
      }
    },
    onMutate: async ({ slot_key, mid, emoji }: { slot_key: string; mid: string; emoji: string }) => {
      const qk = reactionQueryKey(slot_key)
      beginToggle(qc, slot_key)
      await qc.cancelQueries({ queryKey: qk })
      // Snapshot just the removed records so a failed delete restores exactly
      // them by id, without touching other identities' pending entries.
      const removed = (qc.getQueryData<ChatReaction[]>(qk) ?? []).filter(
        r => r.mid === mid && r.emoji === emoji,
      )
      qc.setQueryData<ChatReaction[]>(qk, old =>
        (old ?? []).filter(r => !(r.mid === mid && r.emoji === emoji)),
      )
      return { removed, sk: slot_key, qk }
    },
    onError: (_err, { slot_key, mid, emoji }, ctx) => {
      if (ctx?.removed?.length) {
        qc.setQueryData<ChatReaction[]>(ctx.qk, old => {
          const have = new Set((old ?? []).map(r => r.id))
          return [...(old ?? []), ...ctx.removed.filter(r => !have.has(r.id))]
        })
      }
      setError({ slot: slot_key, mid, emoji, kind: 'unreact' })
    },
    onSettled: (_data, _err, _vars, ctx) => { if (ctx) endToggle(qc, ctx.sk) },
  })

  // Per-identity toggle state, keyed by `(slot, mid, emoji)`. `tail` serializes the
  // writes so a new click chains onto the one in flight rather than racing it
  // (a bodyless DELETE that 404s before a concurrent POST takes the backend
  // lock could otherwise leave the wrong final state). `want` is the user's
  // LATEST intended state for that identity — flipped on every click at request
  // time — and is what the chained callback acts on. Reading intent from `want`
  // rather than the live cache is what fixes the queued-off-click-after-failure
  // race: a failed create rolls its optimistic entry out of the cache, but the
  // queued off-click still knows the user wants it OFF and sends nothing (it is
  // already absent) instead of re-POSTing it ON.
  //
  // The map is shared by every mount on this QueryClient (see
  // `reactionToggleChains`), not held per mount: a remount keeps the cache and
  // its optimistic row, so it must also keep the chain that row belongs to, or
  // a remove on the new mount would not wait for the old mount's pending add.
  const toggles = useMemo(() => reactionToggleChains(qc), [qc])

  /** Toggle `emoji` on `mid`: flips the caller's reaction on this message.
   *  Rejects on failure so the caller can await it; the hook's own `error`
   *  state drives the UI notice. */
  const toggleReaction = useCallback(
    (mid: string, emoji: string): Promise<void> => {
      if (!slotKey) return Promise.resolve()
      const sk = slotKey
      errorStore.clear(sk)
      // Chain key is the (mid, emoji) pair joined by a NUL, which cannot occur
      // in either value so the boundary is unambiguous. Built with join +
      // fromCharCode rather than a template literal: this is an internal Map
      // key, never user-facing, and a literal separator string on this line
      // would trip the i18n added-lines gate (no-literal-string) as if it were
      // untranslated copy.
      const key = [sk, mid, emoji].join(String.fromCharCode(0))
      const entry = toggles.get(key)
      // The new desired state is the opposite of the last INTENDED state, not
      // the last committed one: three fast clicks mean on, off, on regardless
      // of what has committed yet. Seed the first click's intent from the live
      // cache (whether the reaction is currently placed).
      const currentlyPlaced = (qc.getQueryData<ChatReaction[]>(reactionQueryKey(sk)) ?? [])
        .some(r => r.mid === mid && r.emoji === emoji)
      const want = entry ? !entry.want : !currentlyPlaced
      // Wait for the previous toggle on this identity to settle (success OR
      // failure) before issuing the next, so the writes never race.
      const prior = (entry?.tail ?? Promise.resolve()).catch(() => {})
      const next = prior.then(() => {
        // Act on the recorded intent, re-read at run time so a later click that
        // landed while this one waited wins. Always SEND the idempotent request
        // for the desired state rather than skipping when the cache already
        // matches: a POST whose response was lost persists the reaction server
        // -side while onError rolls the optimistic row out of the cache, so a
        // queued OFF that trusted the cache would skip its DELETE and leave the
        // reaction the user turned OFF persisted ON (and the reverse for a lost
        // DELETE). create is idempotent (returns the existing record), and a
        // DELETE of an already-absent reaction is the desired end state, which
        // removeReactionAsync treats as success. The redundant request when the
        // cache was right is cheap; trusting a possibly-stale cache is not.
        const desired = toggles.get(key)?.want ?? want
        return desired
          ? addReactionAsync({ slot_key: sk, mid, emoji }).then(() => {})
          : removeReactionAsync({ slot_key: sk, mid, emoji }).then(() => {})
      })
      // The stored tail swallows rejections (so it never becomes an unhandled
      // rejection and a later toggle can still await it); drop the entry once
      // it is the settled tail and nothing newer replaced it.
      const tail = next.catch(() => {})
      toggles.set(key, { tail, want })
      void tail.then(() => {
        if (toggles.get(key)?.tail === tail) toggles.delete(key)
      })
      return next
    },
    [slotKey, qc, toggles, errorStore, addReactionAsync, removeReactionAsync],
  )

  return {
    reactions,
    loading,
    // True when the reactions GET failed after retries. Surfaced so the caller
    // can SHOW the failure instead of letting `data: reactions = []` quietly
    // read as "no reactions" and make a message's existing markers vanish with
    // no notice (errors-use-error-notice).
    loadFailed,
    error,
    clearError,
    reactionsFor,
    toggleReaction,
  }
}

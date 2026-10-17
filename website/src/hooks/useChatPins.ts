import { useCallback, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { PIN_PREVIEW_INPUT_MAX_CHARS, pinsApi, type ChatPin, type PinMessageBody } from '../api/pins'
import { ApiError } from '../api/apiError'
import { parseErrorCode } from '../utils/errorReport'
import { secureRandomId } from '../utils/secureId'

/**
 * The backend `code` from a pins API failure, or undefined for anything else.
 *
 * pinsApi now routes through the shared transport, so a failure is an
 * `ApiError` carrying the raw backend body — the machine-readable `code` is
 * read from that body with the same `parseErrorCode` the transport journals
 * with, rather than off a bespoke `.code` property. The structural
 * `'code' in err` fallback is kept so any error object that still carries a
 * direct `code` (older callers, hand-built test errors) resolves too.
 */
export function pinErrorCode(err: unknown): string | undefined {
  if (err instanceof ApiError) {
    return parseErrorCode(err.body)
  }
  if (err instanceof Error && 'code' in err) {
    const code = (err as { code?: unknown }).code
    return typeof code === 'string' ? code : undefined
  }
  return undefined
}

const pinQueryKey = (slotKey: string | undefined) => ['chat-pins', slotKey] as const

type UnpinMutation = { id: string; slotKey: string }

/**
 * Unpin-race coordination state for a single QueryClient.
 *
 *  - `inFlightCreates`: in-flight create promises keyed by `${slot_key}::${mid}`.
 *    A temp-id unpin awaits this promise to learn the real server id (or that
 *    the create failed).
 *  - `pinGenerations`: monotonic pin-intent generation per `${slot_key}::${mid}`.
 *    A temp-id unpin snapshots the generation it is cancelling and, after
 *    awaiting the create, only deletes the server pin if no newer pin intent
 *    arrived meanwhile.
 */
type PinCoordStore = {
  inFlightCreates: Map<string, Promise<ChatPin>>
  pinGenerations: Map<string, number>
}

/**
 * Coordination state lives on the QueryClient, not in per-instance `useRef`
 * maps, so it is shared by every `useChatPins` instance bound to the same
 * client and survives a remount. The pin DATA it guards already lives in that
 * client's React Query cache; keeping the coordination beside it means a
 * remount mid-create, or a second consumer of the hook for the same slot, sees
 * the same in-flight promise instead of the "no tracked promise" branch (which
 * would silently drop an unpin — the pin resurfaces on the settled refetch).
 *
 * A WeakMap keys the store off the client identity so it is reclaimed with the
 * client and never leaks across the QueryClients that tests create per case.
 */
const coordStores = new WeakMap<QueryClient, PinCoordStore>()

function coordStoreFor(qc: QueryClient): PinCoordStore {
  let store = coordStores.get(qc)
  if (!store) {
    store = { inFlightCreates: new Map(), pinGenerations: new Map() }
    coordStores.set(qc, store)
  }
  return store
}

/**
 * Hook to manage chat message pins for a given slot.
 * Uses React Query with optimistic updates – eliminates stale-closure race
 * conditions when the user switches slots quickly (each slot has its own
 * query key, so a late response for slot A never overwrites slot B's cache).
 *
 * Pin identity uses `mid` (server-minted message ID from meta.mid) so that
 * messages sharing a timestamp can each be pinned independently.
 *
 * In-flight create tracking: when the Unpin button is clicked while the
 * create request is still in flight (optimistic id starts with "temp-"),
 * we await the in-flight promise before deciding whether to DELETE the server
 * pin or just clear local state.  This prevents a DELETE /api/chat/pins/temp-…
 * → 404 → "unpin failed" toast in that race window.
 */
export function useChatPins(slotKey: string | undefined) {
  const qc = useQueryClient()
  const queryKey = useMemo(() => pinQueryKey(slotKey), [slotKey])
  const [error, setError] = useState<'pin' | 'pin_limit' | 'unpin' | null>(null)
  const clearError = useCallback(() => setError(null), [])

  /**
   * Race-coordination state scoped to this QueryClient (see PinCoordStore).
   * Shared across every hook instance on the same client and stable across
   * remounts — the maps are NOT recreated per render or per mount.
   */
  const { inFlightCreates, pinGenerations } = coordStoreFor(qc)

  const { data: pins = [], isLoading: loading } = useQuery<ChatPin[]>({
    queryKey,
    queryFn: async () => {
      const res = await pinsApi.list(slotKey!)
      return res.pins
    },
    enabled: !!slotKey,
  })

  const { mutateAsync: pinMessageAsync } = useMutation({
    mutationFn: (body: PinMessageBody) => pinsApi.create(body),
    onMutate: async (body: PinMessageBody) => {
      setError(null)
      const mutationQueryKey = pinQueryKey(body.slot_key)
      await qc.cancelQueries({ queryKey: mutationQueryKey })
      const prev = qc.getQueryData<ChatPin[]>(mutationQueryKey)
      const optimistic: ChatPin = {
        id: `temp-${secureRandomId()}`,
        slot_key: body.slot_key,
        mid: body.mid,
        message_ts: body.message_ts,
        role: body.role,
        preview: body.preview,
        pinned_at: new Date().toISOString(),
      }
      qc.setQueryData<ChatPin[]>(mutationQueryKey, old => [...(old ?? []), optimistic])
      return { prev, optimisticId: optimistic.id, queryKey: mutationQueryKey }
    },
    onError: (_err, _body, ctx) => {
      if (ctx?.prev) {
        qc.setQueryData(ctx.queryKey, ctx.prev)
      } else if (ctx?.optimisticId && ctx?.queryKey) {
        // prev was undefined (e.g. no prior cache entry) — remove the ghost
        // optimistic pin rather than leaving it stranded in the query cache.
        qc.setQueryData<ChatPin[]>(ctx.queryKey, old =>
          (old ?? []).filter(p => p.id !== ctx.optimisticId),
        )
      }
      const code = pinErrorCode(_err)
      setError(code === 'pin_limit_reached' ? 'pin_limit' : 'pin')
    },
    onSuccess: (real, _body, ctx) => {
      if (!ctx) return
      // Replace the temp entry with the server-confirmed pin in its originating slot.
      qc.setQueryData<ChatPin[]>(ctx.queryKey, old =>
        (old ?? []).map(p => p.id === ctx.optimisticId ? real : p),
      )
    },
    onSettled: (_data, _error, body) => {
      qc.invalidateQueries({ queryKey: pinQueryKey(body.slot_key) })
    },
  })

  const { mutateAsync: unpinMessageAsync } = useMutation({
    mutationFn: ({ id }: UnpinMutation) => pinsApi.remove(id),
    onMutate: async ({ id, slotKey }: UnpinMutation) => {
      setError(null)
      const mutationQueryKey = pinQueryKey(slotKey)
      await qc.cancelQueries({ queryKey: mutationQueryKey })
      const prev = qc.getQueryData<ChatPin[]>(mutationQueryKey)
      qc.setQueryData<ChatPin[]>(mutationQueryKey, old =>
        (old ?? []).filter(p => p.id !== id),
      )
      return { prev, queryKey: mutationQueryKey }
    },
    onError: (_err, _mutation, ctx) => {
      if (ctx?.prev) qc.setQueryData(ctx.queryKey, ctx.prev)
      setError('unpin')
    },
    onSettled: (_data, _error, mutation) => {
      qc.invalidateQueries({ queryKey: pinQueryKey(mutation.slotKey) })
    },
  })

  /**
   * Core unpin logic shared by both unpinMessage and unpinById.
   *
   * If the pin's id is a temporary optimistic id (starts with "temp-"):
   *  1. Optimistically remove it from the cache right away so the UI reacts immediately.
   *  2. Await the in-flight create promise for that mid.
   *     - If the create succeeded, DELETE the real server id via the normal mutation.
   *     - If the create failed/rejected, there is nothing on the server — just swallow.
   * If the id is already a real server id, delegate straight to the unpin mutation.
   */
  const _doUnpin = useCallback(
    async (pin: ChatPin) => {
      const resolvedSlotKey = pin.slot_key ?? slotKey ?? ''

      if (pin.id.startsWith('temp-')) {
        // Optimistically remove from cache immediately (no network call yet)
        const mutationQueryKey = pinQueryKey(resolvedSlotKey)
        qc.setQueryData<ChatPin[]>(mutationQueryKey, old =>
          (old ?? []).filter(p => p.id !== pin.id),
        )
        qc.invalidateQueries({ queryKey: mutationQueryKey })

        const inFlight = inFlightCreates.get(`${resolvedSlotKey}::${pin.mid}`)
        if (!inFlight) {
          // No tracked promise. This is NOT always "nothing on the server":
          // the create may have already succeeded and been cleaned from the
          // map while the caller held a stale temp-id reference (onSuccess
          // swaps temp -> real in the cache, but a snapshot taken before the
          // swap still carries the temp id). Check the cache for the settled
          // pin with this mid and delete that if present.
          const settled = qc
            .getQueryData<ChatPin[]>(mutationQueryKey)
            ?.find(p => p.mid === pin.mid && !p.id.startsWith('temp-'))
          if (settled) {
            await unpinMessageAsync({ id: settled.id, slotKey: settled.slot_key })
          }
          return
        }

        let realPin: ChatPin
        const inFlightKey = `${resolvedSlotKey}::${pin.mid}`
        const generationAtUnpin = pinGenerations.get(inFlightKey) ?? 0
        try {
          realPin = await inFlight
        } catch {
          // Create failed — no server pin exists, nothing to DELETE.
          return
        }

        // If the user pinned this message AGAIN while we awaited the create,
        // that newer intent wins: the idempotent create returns the same
        // record, so deleting it now would destroy the re-created pin.
        if ((pinGenerations.get(inFlightKey) ?? 0) !== generationAtUnpin) {
          return
        }

        // Create succeeded and no newer pin intent — delete the server pin.
        await unpinMessageAsync({ id: realPin.id, slotKey: realPin.slot_key })
        return
      }

      await unpinMessageAsync({ id: pin.id, slotKey: resolvedSlotKey })
    },
    [slotKey, qc, unpinMessageAsync, inFlightCreates, pinGenerations],
  )

  const isPinned = useCallback(
    (mid: string) => pins.some(p => p.mid === mid),
    [pins],
  )

  const pinMessage = useCallback(
    async (body: Omit<PinMessageBody, 'slot_key'>) => {
      if (!slotKey) return
      const fullBody: PinMessageBody = {
        ...body,
        slot_key: slotKey,
        preview: body.preview.slice(0, PIN_PREVIEW_INPUT_MAX_CHARS),
      }
      // Register the in-flight promise so concurrent unpins can await it.
      // Keyed by (slot_key, mid): forked sessions can carry the same mid in
      // different slots, and a mid-only key would let one slot's create
      // overwrite the other's entry (unpinning one slot could then delete the
      // other slot's pin). The .finally cleanup only removes the entry when it
      // still holds THIS promise, so a concurrent create for the same key can
      // never have its registration deleted by an older settling promise.
      const inFlightKey = `${fullBody.slot_key}::${fullBody.mid}`
      // A new pin intent supersedes any unpin still awaiting the previous
      // create for this message (see pinGenerations).
      pinGenerations.set(inFlightKey, (pinGenerations.get(inFlightKey) ?? 0) + 1)
      const promise = pinMessageAsync(fullBody).finally(() => {
        if (inFlightCreates.get(inFlightKey) === promise) {
          inFlightCreates.delete(inFlightKey)
        }
      })
      inFlightCreates.set(inFlightKey, promise)
      await promise
    },
    [slotKey, pinMessageAsync, inFlightCreates, pinGenerations],
  )

  const unpinMessage = useCallback(
    async (mid: string) => {
      if (!slotKey) return
      const pin = pins.find(p => p.mid === mid)
      if (!pin) return
      await _doUnpin(pin)
    },
    [slotKey, pins, _doUnpin],
  )

  const unpinById = useCallback(
    async (id: string) => {
      if (!slotKey) return
      const pin = pins.find(candidate => candidate.id === id)
      if (pin) {
        // Pin is in local cache: use the full _doUnpin path (handles temp- ids).
        await _doUnpin(pin)
        return
      }
      // Pin not in local cache (e.g. not yet loaded): fall back to direct
      // unpin by id — but never with a temp id. A second click on the same
      // still-saving pin lands here after the first click already removed the
      // temp entry from the cache; the first click's flow owns the server
      // deletion, so this one is a no-op rather than a guaranteed-404 DELETE.
      if (id.startsWith('temp-')) return
      await unpinMessageAsync({ id, slotKey })
    },
    [slotKey, pins, _doUnpin, unpinMessageAsync],
  )

  const refresh = useCallback(() => {
    qc.invalidateQueries({ queryKey })
  }, [qc, queryKey])

  return {
    pins,
    loading,
    error,
    clearError,
    isPinned,
    pinMessage,
    unpinMessage,
    unpinById,
    refresh,
  }
}

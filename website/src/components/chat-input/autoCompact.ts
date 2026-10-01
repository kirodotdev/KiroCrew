import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, type QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { useAppDispatch } from '../../store'
import { setAgentSwitchNotice } from '../../store/chatSlice'
import { agentSwitchFailureMessage } from '../../utils/agentSwitchFeedback'

/* The per-session auto-compact threshold behind the context popover's slider:
   read lazily on open, written through one per-slot chain so writes commit in
   issue order, debounced per slot and flushed (never dropped) on a slot
   switch or unmount. */
export function useAutoCompactThreshold({ activeSlot, ctxPopoverOpen, queryClient, dispatch }: {
  activeSlot: string | null
  ctxPopoverOpen: boolean
  queryClient: QueryClient
  dispatch: ReturnType<typeof useAppDispatch>
}) {
  // Per-session auto-compact threshold (slider in the context popover). The
  // debounce timer collapses a slider drag into one POST; the fetch itself is
  // the React Query below, so the value lives in the standard cache rather
  // than hand-rolled state.
  const autoCompactTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const autoCompactPending = useRef<{ slot: string; pct: number | null } | null>(null)
  // Per-session auto-compact threshold: fetched lazily on popover open (the
  // slots frame stays untouched), cached under the standard query layer. The
  // slider writes optimistically into the cache per step and the debounced
  // mutation collapses a drag into one POST; the response re-syncs the cache.
  const autoCompactQuery = useQuery({
    queryKey: ['slot-autocompact', activeSlot ?? null],
    queryFn: () => api.chatSlotAutocompact(activeSlot as string),
    enabled: ctxPopoverOpen && !!activeSlot,
    staleTime: 30_000,
  })
  const autoCompact = autoCompactQuery.data ?? null
  // In-popover report of an auto-compact threshold write that did not persist.
  const [autoCompactError, setAutoCompactError] = useState('')
  // INVARIANT: every threshold POST is chained onto the previous
  // write for that slot, so writes commit in issue order — a delayed earlier
  // POST can never land after (and overwrite) a newer value on the server.
  // ALL dispatch sites (the debounced mutation, the cross-slot flush, the
  // unmount flush) MUST go through enqueueAutoCompactWrite; never call
  // api.setChatSlotAutocompact directly: the composer's controls write only
  // through `pushAutoCompact`.
  const autoCompactChain = useRef<Map<string, Promise<unknown>>>(new Map())
  const enqueueAutoCompactWrite = useCallback((slot: string, pct: number | null) => {
    const prev = autoCompactChain.current.get(slot) ?? Promise.resolve()
    // Chain through settle (not just success): a failed write must not block
    // — or reorder — the writes queued behind it.
    const next = prev.then(
      () => api.setChatSlotAutocompact(slot, pct),
      () => api.setChatSlotAutocompact(slot, pct),
    )
    autoCompactChain.current.set(slot, next.then(() => undefined, () => undefined))
    return next
  }, [])
  const autoCompactMutation = useMutation({
    mutationFn: ({ slot, pct }: { slot: string; pct: number | null }) => enqueueAutoCompactWrite(slot, pct),
    onSuccess: (r, vars) => {
      setAutoCompactError('')
      queryClient.setQueryData(
        ['slot-autocompact', vars.slot],
        (prev: { pct: number | null; global_pct: number; min: number; max: number } | undefined) =>
          prev ? { ...prev, pct: r.pct, global_pct: r.global_pct } : prev,
      )
    },
    onError: (err, vars) => {
      // A rejected write must not leave the optimistic value cached: refetch
      // the server truth so the slider snaps back to the applied threshold.
      queryClient.invalidateQueries({ queryKey: ['slot-autocompact', vars.slot] })
      // Surface the failure the way the sibling per-slot settings do (model,
      // reasoning effort): a silent snap-back leaves the user's compaction
      // intent unapplied with no explanation — and with the popover closed,
      // no visible change at all. The toast is transient feedback only; the
      // in-popover ErrorNotice (autoCompactError) is the error surface.
      const msg = agentSwitchFailureMessage(err)
      dispatch(setAgentSwitchNotice(msg))
      setAutoCompactError(msg)
    },
  })
  const pushAutoCompact = useCallback((pct: number | null) => {
    if (!activeSlot) return
    const slot = activeSlot
    queryClient.setQueryData(
      ['slot-autocompact', slot],
      (prev: { pct: number | null; global_pct: number; min: number; max: number } | undefined) =>
        prev ? { ...prev, pct } : prev,
    )
    if (autoCompactTimer.current) clearTimeout(autoCompactTimer.current)
    // Debouncing only ever supersedes a write for the SAME slot. A pending
    // write for another slot (drag on A, switch, drag on B within the window)
    // is a different session's change: flush it now instead of discarding it,
    // or A would silently keep its old threshold on the server.
    const pending = autoCompactPending.current
    if (pending && pending.slot !== slot) {
      autoCompactPending.current = null
      autoCompactMutation.mutate({ slot: pending.slot, pct: pending.pct })
    }
    autoCompactPending.current = { slot, pct }
    autoCompactTimer.current = setTimeout(() => {
      autoCompactPending.current = null
      autoCompactMutation.mutate({ slot, pct })
    }, 400)
  }, [activeSlot, queryClient, autoCompactMutation])
  // Flush (not discard) a pending debounced write on unmount: cancelling the
  // sole POST would leave the server on the old threshold while the user saw
  // their change accepted. Fire the API call directly -- the component is
  // gone, so the mutation's cache re-sync has nothing left to update.
  useEffect(() => () => {
    if (autoCompactTimer.current) clearTimeout(autoCompactTimer.current)
    const pending = autoCompactPending.current
    if (pending) {
      autoCompactPending.current = null
      // On rejection, drop the optimistic value from the cache so a return to
      // this slot refetches server truth instead of showing a threshold the
      // session never applied (mirrors the mutation's onError). Routed through
      // the per-slot chain so the flush cannot overtake an in-flight write.
      void enqueueAutoCompactWrite(pending.slot, pending.pct).catch((err) => {
        queryClient.invalidateQueries({ queryKey: ['slot-autocompact', pending.slot] })
        // The component is gone but the store is not: surface the failure
        // like the sibling settings do, or the user's last change before
        // navigating away silently never applies.
        dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(err)))
      })
    }
  }, [queryClient, enqueueAutoCompactWrite, dispatch])

  return { autoCompactQuery, autoCompact, autoCompactError, setAutoCompactError, pushAutoCompact }
}

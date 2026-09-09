import { createListenerMiddleware } from '@reduxjs/toolkit'
import { fetchSlots, sseSlots } from './dashboardSlice'
import { settlePinnedArrangementAgainstMembership } from '../utils/pinnedSessionOrder'
import type { ChatSlot } from '../types'

/**
 * One owner for "authoritative membership says the arrangement is over".
 *
 * The rule lives once, in `settlePinnedArrangementAgainstMembership`. This file only decides which
 * frames are authoritative enough to hand it. Two arms, because the channels genuinely differ: slot
 * frames arrive as redux actions, while a pin reconciliation runs through React Query and dispatches
 * none — with the socket down, an accepted last-pin unpin reaches the rule through the commit seam.
 *
 * There is deliberately no cross-tab liveness protocol behind these arms. Two attempts existed and
 * were removed: a deferral that replayed a zero-pin frame on a timer, and a storage-backed revision
 * counter, each layer added to keep the previous one honest and each producing a defect of its own.
 * The guard is this tab's own in-flight record, and the sibling-tab residual costs one re-drag or one
 * "Follow sort order (all pinned)" click.
 */
export const pinnedMarkerListener = createListenerMiddleware()

interface SlotsState {
  dashboard?: { slotsLoaded?: boolean, slotsGeneration?: number }
}

/** null when the payload is not a slot list, so a malformed frame is ignored rather than settled. */
function pinnedKeysOf(payload: unknown): string[] | null {
  if (!Array.isArray(payload)) return null
  return (payload as ChatSlot[]).filter(slot => slot?.pinned).map(slot => slot.key)
}

/** A pin this tab has not had answered yet is absent from any membership the server reports. */
function settleIfAuthoritative(payload: unknown, slotsLoaded: boolean): void {
  const pinned = pinnedKeysOf(payload)
  if (pinned === null) return
  // A reconnect delivers an empty frame before the first real snapshot, which the reducer refuses
  // too; an empty frame after that is authoritative — deleting the only pinned session leaves none.
  if ((payload as ChatSlot[]).length === 0 && !slotsLoaded) return
  settlePinnedArrangementAgainstMembership(pinned)
}

pinnedMarkerListener.startListening({
  actionCreator: sseSlots,
  effect: async (action, listenerApi) => {
    settleIfAuthoritative(action.payload,
      (listenerApi.getState() as SlotsState).dashboard?.slotsLoaded === true)
  },
})

/** A refetch reply is a distinct authoritative writer, and the only membership channel a reconnect
 * has before its first live frame. */
const generationWhenRequested = new Map<string, number>()

pinnedMarkerListener.startListening({
  actionCreator: fetchSlots.pending,
  effect: async (action, listenerApi) => {
    generationWhenRequested.set(action.meta.requestId,
      (listenerApi.getState() as SlotsState).dashboard?.slotsGeneration ?? 0)
  },
})

pinnedMarkerListener.startListening({
  actionCreator: fetchSlots.rejected,
  effect: async action => {
    generationWhenRequested.delete(action.meta.requestId)
  },
})

pinnedMarkerListener.startListening({
  actionCreator: fetchSlots.fulfilled,
  effect: async (action, listenerApi) => {
    const requested = generationWhenRequested.get(action.meta.requestId)
    generationWhenRequested.delete(action.meta.requestId)
    const state = (listenerApi.getState() as SlotsState).dashboard
    // A reply a newer accepted snapshot has overtaken cannot speak for present membership, and an
    // undateable one cannot be shown to post-date the arrangement it would discard.
    if (requested === undefined || requested !== (state?.slotsGeneration ?? 0)) return
    settleIfAuthoritative(action.payload, state?.slotsLoaded === true)
  },
})

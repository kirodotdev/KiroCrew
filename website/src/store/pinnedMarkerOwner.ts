import { createListenerMiddleware } from '@reduxjs/toolkit'
import { fetchSlots, sseSlots } from './dashboardSlice'
import { arrangementStatedElsewhere, noteArrangementRevisionSeen, readSharedArrangementRevision, settlePinnedArrangementAgainstMembership } from '../utils/pinnedSessionOrder'
import { pinMutationsAreInFlight } from '../utils/pinMutationsInFlight'
import type { ChatSlot } from '../types'

/**
 * One owner for "authoritative membership says the arrangement is over".
 *
 * The rule lives once, in `settlePinnedArrangementAgainstMembership`. This file only decides which
 * frames are authoritative enough to hand it. Two arms, because the channels genuinely differ: slot
 * frames arrive as redux actions, while a pin reconciliation runs through React Query and dispatches
 * none — with the socket down, an accepted last-pin unpin reaches the rule through the commit seam.
 *
 * There is deliberately no cross-tab liveness protocol behind these arms. An earlier revision
 * deferred a zero-pin frame, replayed it on a settlement callback, re-armed it on a timer and
 * compared storage-backed intent revisions; each layer existed to keep the previous one honest and
 * each produced a defect of its own. A fetch reply is instead checked against the arrangement that
 * stood when it was asked for, which is read from shared storage and so sees every tab.
 */
export const pinnedMarkerListener = createListenerMiddleware()

interface SlotsState {
  dashboard?: { slotsLoaded?: boolean }
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
  if (pinned.length === 0 && pinMutationsAreInFlight()) return
  // The shared revision outruns this tab's high-water mark exactly when ANOTHER tab just stated an
  // arrangement — the one case this tab's own in-flight record structurally cannot see.
  const racesASiblingArrangement = pinned.length === 0 && arrangementStatedElsewhere()
  // Noted even when suppressing, so ONE frame is refused per unseen sibling revision. Returning first
  // left the mark behind forever, and a sibling that closes emits no later frame to catch it up.
  noteArrangementRevisionSeen()
  if (racesASiblingArrangement) return
  settlePinnedArrangementAgainstMembership(pinned)
}

pinnedMarkerListener.startListening({
  actionCreator: sseSlots,
  effect: async (action, listenerApi) => {
    settleIfAuthoritative(action.payload,
      (listenerApi.getState() as SlotsState).dashboard?.slotsLoaded === true)
  },
})

// The arrangement REVISION as it stood when a fetch left, keyed by that fetch's own request id. A
// serialized order cannot see a reorder away and back to the same sequence; a counter can.
const arrangementWhenRequested = new Map<string, number>()

pinnedMarkerListener.startListening({
  actionCreator: fetchSlots.pending,
  effect: async action => {
    arrangementWhenRequested.set(action.meta.requestId, readSharedArrangementRevision())
  },
})

pinnedMarkerListener.startListening({
  actionCreator: fetchSlots.rejected,
  effect: async action => {
    arrangementWhenRequested.delete(action.meta.requestId)
  },
})

pinnedMarkerListener.startListening({
  actionCreator: fetchSlots.fulfilled,
  effect: async (action, listenerApi) => {
    const requested = arrangementWhenRequested.get(action.meta.requestId)
    arrangementWhenRequested.delete(action.meta.requestId)
    // A reply cannot speak for an arrangement stated after it was asked for, in this tab or any
    // other. An unrecorded request is UNDATEABLE, so it loses rather than settling unguarded.
    if (requested === undefined || requested !== readSharedArrangementRevision()) return
    settleIfAuthoritative(action.payload,
      (listenerApi.getState() as SlotsState).dashboard?.slotsLoaded === true)
  },
})

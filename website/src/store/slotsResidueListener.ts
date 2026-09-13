import { createListenerMiddleware } from '@reduxjs/toolkit'
import { fetchSlots } from './dashboardSlice'
import { slotsSnapshotApplied } from './chatSlice'

type WithSlotsVerdict = {
  dashboard: { lastSlotsRead: { readId?: string; applied: boolean } | null }
}

/** Carry the dashboard reducer's FINAL slots verdict to the chat slice.
 *
 *  A listener rather than a reducer case because the answer does not exist yet while the
 *  reducers for `fetchSlots.fulfilled` run: `chatSlice` cannot read `dashboard` state, and the
 *  thunk's `appliedProvisional` is a pre-reduction guess that a `closeSeq` bump in the microtask
 *  gap can invalidate. A listener effect runs AFTER the action is reduced, so `lastSlotsRead` is
 *  the decision itself. Keyed to THIS read's `requestId`, so a sibling read cannot answer for it.
 *
 *  Placed here, not at the call sites: eviction must be withheld on every refused read, and
 *  `fetchSlots` is dispatched bare from eight places. Gating each one would leave the next
 *  caller to remember, which is how the provisional read became load-bearing in the first place. */
export const slotsResidueListener = createListenerMiddleware()

slotsResidueListener.startListening({
  actionCreator: fetchSlots.fulfilled,
  effect: (action, api) => {
    const last = (api.getState() as WithSlotsVerdict).dashboard.lastSlotsRead
    if (last === null || last.readId !== action.meta.requestId || !last.applied) return
    api.dispatch(slotsSnapshotApplied(action.payload))
  },
})

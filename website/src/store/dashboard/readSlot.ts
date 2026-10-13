import { api } from '../../api/client'
import { emitSlotRead, relaySlotReadWatermark, slotReadWatermark } from '../../lib/slotReadRelay'
import { withDeadline } from '../../lib/withDeadline'
import { markSlotRead, retireSlotUnread, type DashboardState } from '../dashboardSlice'
import { slotLastTsOf } from '../slotLastTs'
import { clearUnreadArrival, holdUnreadArrival, readUnreadArrival } from '../unreadArrivalMarker'
import { UNREAD_PROOF_TIMEOUT_MS, orphanedWatermark, watermarkDisprovedBy } from './unread'

/** A read gesture that shows no transcript (Mark as read): clear this window's
 *  dot, then relay the read at the click-time last_ts. A watermark that
 *  last_ts cannot cover needs the gateway's word first: a one-row page fetched
 *  after `held` was captured includes every row a saved watermark can name, so
 *  an orphan tombstone is recorded before the relay and its echo cannot light
 *  the dot again. The relay carries the watermark captured at the click, so a
 *  row that arrives while the proof is in flight stays unread. A proof that
 *  outlives UNREAD_PROOF_TIMEOUT_MS counts as no proof, so the relay still goes out. */
export const readSlot = (key: string) => (
  dispatch: (action: ReturnType<typeof markSlotRead> | ReturnType<typeof retireSlotUnread>) => unknown,
  getState: () => unknown,
): Promise<void> | void => {
  const dashboard = (getState() as { dashboard?: DashboardState } | undefined)?.dashboard
  const held = dashboard?.unreadSince?.[key]
  const readTs = slotLastTsOf(dashboard, key)
  const orphanSuspected = orphanedWatermark(held, readTs) !== undefined
  const heldArrival = orphanSuspected ? holdUnreadArrival(key) : undefined
  if (dashboard?.unreadSlots?.includes(key)) dispatch(markSlotRead(key))
  if (!orphanSuspected) {
    emitSlotRead(key, readTs)
    return
  }
  const relayWatermark = slotReadWatermark(key, readTs)
  return withDeadline(UNREAD_PROOF_TIMEOUT_MS, undefined, (signal) => api.chatSlotDetail(key, 1, undefined, signal))
    .then((page) => {
      const orphan = watermarkDisprovedBy(held, page.messages)
      if (!orphan || heldArrival === undefined) return
      dispatch(retireSlotUnread({ slot: key, watermark: orphan, arrival: heldArrival }))
    })
    // No proof, so the saved dot keeps its guard; the read still relays.
    .catch(() => undefined)
    .finally(() => {
      if (heldArrival !== undefined && readUnreadArrival(key) === heldArrival) clearUnreadArrival(key)
    })
    .then(() => { relaySlotReadWatermark(key, relayWatermark) })
}

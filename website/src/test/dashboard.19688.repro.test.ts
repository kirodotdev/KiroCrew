import { it, expect } from 'vitest'
import { createTestStore } from './helpers'
import dashboardReducer, { fetchSlots, sseConnected, sseDisconnected, sseSlots, markSlotRead, remoteSlotRead, restoreUnreadSince, restoreUnreadBadges } from '../store/dashboardSlice'
import type { RootState } from '../store'

it('a read of an idle session keeps its dot cleared', () => {
  // The record a build before #15014 left: the watermark is the turn's
  // unsaved `done` row, a few ms after the transcript tail.
  localStorage.setItem('mc-unread-shared', JSON.stringify({ s1: '2026-10-09T22:33:35.611204+00:00' }))
  const since = restoreUnreadSince()
  const store = createTestStore({ dashboard: {
    ...dashboardReducer(undefined, { type: '@@test/init' }),
    unreadSince: since, unreadSlots: restoreUnreadBadges(since),
  } as RootState['dashboard'] })
  store.dispatch(sseConnected())
  store.dispatch(sseSlots([{ key: 's1', messages: 166, running: false, last_ts: '2026-10-09T22:33:35.542839+00:00' }]))
  // What opening the session or Mark as read does: the local read, then the
  // gateway's rebroadcast of the relayed read reaching this window too.
  store.dispatch(markSlotRead('s1'))
  store.dispatch(remoteSlotRead({ slot: 's1', readTs: '2026-10-09T22:33:35.542839+00:00' }))
  expect(store.getState().dashboard.unreadSlots).not.toContain('s1')
})

it('the idle read retires the orphan watermark, so no reload can bring the dot back', () => {
  localStorage.setItem('mc-unread-shared', JSON.stringify({ s1: '2026-10-09T22:33:35.611204+00:00' }))
  const since = restoreUnreadSince()
  const store = createTestStore({ dashboard: {
    ...dashboardReducer(undefined, { type: '@@test/init' }),
    unreadSince: since, unreadSlots: restoreUnreadBadges(since),
  } as RootState['dashboard'] })
  store.dispatch(sseConnected())
  store.dispatch(sseSlots([{ key: 's1', messages: 166, running: false, last_ts: '2026-10-09T22:33:35.542839+00:00' }]))
  store.dispatch(markSlotRead('s1'))
  // Retirement is the whole point: the record a pre-#15014 build left must
  // leave localStorage, or restoreUnreadBadges revives the dot on reload.
  expect(Object.keys(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}'))).not.toContain('s1')
  expect(restoreUnreadBadges(restoreUnreadSince())).not.toContain('s1')
})

it('a read of an in-flight session still never retires a watermark it cannot cover', () => {
  // The lag-window protection is load-bearing for live sessions: mid-turn a
  // slot's saved `last_ts` trails the turn's final rows, so an uncovered
  // watermark may describe rows this window has simply not been pushed yet.
  // Only an IDLE slot's read may retire.
  localStorage.setItem('mc-unread-shared', JSON.stringify({ s1: '2026-10-09T22:33:35.611204+00:00' }))
  const since = restoreUnreadSince()
  const store = createTestStore({ dashboard: {
    ...dashboardReducer(undefined, { type: '@@test/init' }),
    unreadSince: since, unreadSlots: restoreUnreadBadges(since),
  } as RootState['dashboard'] })
  store.dispatch(sseConnected())
  store.dispatch(sseSlots([{ key: 's1', messages: 166, running: true, last_ts: '2026-10-09T22:33:35.542839+00:00' }]))
  store.dispatch(markSlotRead('s1'))
  expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual({ s1: '2026-10-09T22:33:35.611204+00:00' })
})

it('an idle read from a stale view never retires a watermark it cannot cover', () => {
  // A window whose socket dropped (or reconnected but has no fresh snapshot
  // yet) still shows the slot idle with an old last_ts, while a connected
  // sibling may have watermarked a newer, real unread row. That read must
  // leave the shared record alone.
  const seed = { s1: '2026-10-09T22:33:35.611204+00:00' }
  const fresh = () => {
    localStorage.setItem('mc-unread-shared', JSON.stringify(seed))
    const since = restoreUnreadSince()
    const store = createTestStore({ dashboard: {
      ...dashboardReducer(undefined, { type: '@@test/init' }),
      unreadSince: since, unreadSlots: restoreUnreadBadges(since),
    } as RootState['dashboard'] })
    store.dispatch(sseConnected())
    store.dispatch(sseSlots([{ key: 's1', messages: 166, running: false, last_ts: '2026-10-09T22:33:35.542839+00:00' }]))
    return store
  }
  // Disconnected: the idle snapshot is stale.
  const a = fresh()
  a.dispatch(sseDisconnected())
  a.dispatch(markSlotRead('s1'))
  expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual(seed)
  // Reconnected, but the fresh snapshot has not arrived yet.
  const b = fresh()
  b.dispatch(sseDisconnected())
  b.dispatch(sseConnected())
  b.dispatch(markSlotRead('s1'))
  expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual(seed)
  // Reconnected, and an HTTP slot reply requested before the drop lands
  // before the live snapshot: it marks the list loaded but is not proof.
  const c = fresh()
  c.dispatch(sseDisconnected())
  c.dispatch(sseConnected())
  c.dispatch({ type: fetchSlots.fulfilled.type, payload: [{ key: 's1', messages: 166, running: false, last_ts: '2026-10-09T22:33:35.542839+00:00' }], meta: { requestId: 'stale', requestStatus: 'fulfilled' } })
  expect(c.getState().dashboard.slotsLoaded).toBe(true)
  c.dispatch(markSlotRead('s1'))
  expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual(seed)  // Reconnected, the live snapshot arrived, THEN a pre-drop HTTP reply
  // overwrote it with an older idle row: the view is stale again.
  const d = fresh()
  d.dispatch(sseDisconnected())
  d.dispatch(sseConnected())
  d.dispatch(sseSlots([{ key: 's1', messages: 166, running: false, last_ts: '2026-10-09T22:33:35.542839+00:00' }]))
  d.dispatch({ type: fetchSlots.fulfilled.type, payload: [{ key: 's1', messages: 166, running: false, last_ts: '2026-10-09T22:33:35.542839+00:00' }], meta: { requestId: 'stale', requestStatus: 'fulfilled' } })
  d.dispatch(markSlotRead('s1'))
  expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual(seed)
})

import { it, expect } from 'vitest'
import { createTestStore } from './helpers'
import dashboardReducer, { sseConnected, sseSlots, markSlotRead, remoteSlotRead, restoreUnreadSince, restoreUnreadBadges } from '../store/dashboardSlice'
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

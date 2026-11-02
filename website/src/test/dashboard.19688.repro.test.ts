import { it, expect, vi, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import dashboardReducer, { sseConnected, sseSlots, markSlotRead, remoteSlotRead, markSlotUnread, confirmOrphanUnread, restoreUnreadSince, restoreUnreadBadges } from '../store/dashboardSlice'
import { api } from '../api/client'
import { switchSlot } from '../store/chatSlice'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

// The record a build before #15014 left: the watermark is the turn's unsaved
// `done` row, a few ms after the transcript tail.
const ORPHAN = '2026-10-09T22:33:35.611204+00:00'
const TAIL = '2026-10-09T22:33:35.542839+00:00'
const NEWER = '2026-10-09T22:40:00.000000+00:00'
const idle = (last_ts: string): ChatSlot => ({ key: 's1', messages: 166, running: false, last_ts } as ChatSlot)
const record = () => JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>

const boot = (shared: Record<string, string>, view: ChatSlot) => {
  localStorage.setItem('mc-unread-shared', JSON.stringify(shared))
  const since = restoreUnreadSince()
  const store = createTestStore({ dashboard: {
    ...dashboardReducer(undefined, { type: '@@test/init' }),
    unreadSince: since, unreadSlots: restoreUnreadBadges(since),
  } as RootState['dashboard'] })
  store.dispatch(sseConnected())
  store.dispatch(sseSlots([view]))
  return store
}

/** What opening a session or Mark as read does: the local read, the orphan
 *  check, and the gateway's echo of the relayed read reaching this window. */
const read = async (store: ReturnType<typeof boot>, echoTs: string) => {
  store.dispatch(markSlotRead('s1'))
  const check = store.dispatch(confirmOrphanUnread('s1'))
  store.dispatch(remoteSlotRead({ slot: 's1', readTs: echoTs }))
  await check
}

afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

it('a read of an idle session keeps its dot cleared', async () => {
  vi.spyOn(api, 'chatSlots').mockResolvedValue([idle(TAIL)])
  const store = boot({ s1: ORPHAN }, idle(TAIL))
  await read(store, TAIL)
  expect(store.getState().dashboard.unreadSlots).not.toContain('s1')
})

it('the read retires the orphan record, so no reload can bring the dot back', async () => {
  vi.spyOn(api, 'chatSlots').mockResolvedValue([idle(TAIL)])
  const store = boot({ s1: ORPHAN }, idle(TAIL))
  await read(store, TAIL)
  expect(Object.keys(record())).not.toContain('s1')
  expect(restoreUnreadBadges(restoreUnreadSince())).not.toContain('s1')
})

it('a lagging window keeps a real watermark the gateway already holds a row for', async () => {
  // GPT's case on #19782: this window still shows the old idle row, while a
  // sibling already received a newer reply and watermarked it. The gateway
  // has that row, so its fresh last_ts covers the watermark.
  const chatSlots = vi.spyOn(api, 'chatSlots').mockResolvedValue([idle(NEWER)])
  const store = boot({ s1: NEWER }, idle(TAIL))
  await read(store, TAIL)
  expect(chatSlots).toHaveBeenCalledTimes(1)
  expect(record()).toEqual({ s1: NEWER })
  expect(store.getState().dashboard.unreadSlots).toContain('s1')
})

it('a session the gateway reports mid-turn keeps the watermark', async () => {
  vi.spyOn(api, 'chatSlots').mockResolvedValue([{ ...idle(TAIL), running: true }])
  const store = boot({ s1: ORPHAN }, idle(TAIL))
  await read(store, TAIL)
  expect(record()).toEqual({ s1: ORPHAN })
})

it('a newer watermark written while the check is in flight survives', async () => {
  let reply: (rows: ChatSlot[]) => void = () => {}
  vi.spyOn(api, 'chatSlots').mockReturnValue(new Promise(r => { reply = r }))
  const store = boot({ s1: ORPHAN }, idle(TAIL))
  store.dispatch(markSlotRead('s1'))
  const check = store.dispatch(confirmOrphanUnread('s1'))
  // A sibling's arrival lands between the gateway's answer and the removal.
  localStorage.setItem('mc-unread-shared', JSON.stringify({ s1: NEWER }))
  reply([idle(TAIL)])
  await check
  expect(record()).toEqual({ s1: NEWER })
})

it('a failed check, or a session missing from the reply, keeps the watermark', async () => {
  vi.spyOn(api, 'chatSlots').mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([])
  const a = boot({ s1: ORPHAN }, idle(TAIL))
  await read(a, TAIL)
  expect(record()).toEqual({ s1: ORPHAN })
  const b = boot({ s1: ORPHAN }, idle(TAIL))
  await read(b, TAIL)
  expect(record()).toEqual({ s1: ORPHAN })
})

it('a read that covers the record asks the gateway nothing', async () => {
  const chatSlots = vi.spyOn(api, 'chatSlots').mockResolvedValue([idle(NEWER)])
  const store = boot({ s1: TAIL }, idle(NEWER))
  await read(store, NEWER)
  expect(chatSlots).not.toHaveBeenCalled()
  expect(record()).toEqual({})
})

it('retiring the orphan leaves a badge lit by a different watermark', async () => {
  let reply: (rows: ChatSlot[]) => void = () => {}
  vi.spyOn(api, 'chatSlots').mockReturnValue(new Promise(r => { reply = r }))
  const store = boot({ s1: ORPHAN }, idle(TAIL))
  store.dispatch(markSlotRead('s1'))
  const check = store.dispatch(confirmOrphanUnread('s1'))
  // A manual mark-as-unread here: its own reminder, not the orphan.
  store.dispatch(markSlotUnread('s1'))
  reply([idle(TAIL)])
  await check
  expect(store.getState().dashboard.unreadSlots).toContain('s1')
})

it('opening the session runs the check, so the issue\'s dot clears for good', async () => {
  vi.spyOn(api, 'chatSlots').mockResolvedValue([idle(TAIL)])
  vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] } as never)
  const store = boot({ s1: ORPHAN }, idle(TAIL))
  await store.dispatch(switchSlot('s1') as never)
  await vi.waitFor(() => expect(record()).toEqual({}))
  expect(store.getState().dashboard.unreadSlots).not.toContain('s1')
})

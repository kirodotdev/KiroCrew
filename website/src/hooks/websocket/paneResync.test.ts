/** A pane on screen whose live stream lost the frames that answered the user:
 *  the reply, the turn end, or both. The pane must catch up from the gateway
 *  over HTTP, its live status must stop saying "Thinking…", and Resume must
 *  not be offered for a turn that finished. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import type { ChatSlot } from '../../types'
import { store } from '../../store'
import { api } from '../../api/client'
import { sseSlots } from '../../store/dashboardSlice'
import {
  clearSlotState,
  selectResumeInterrupted,
  selectSlotMessages,
  selectTurnInterrupted,
  setActiveSlot,
  setSlotStatusDetail,
  startLocalTurn,
} from '../../store/chatSlice'
import { slotMessagesQueryKey } from '../../api/slotMessagesQuery'
import { registerPendingChunkDrain } from '../../lib/pendingChunkDrain'
import type { StreamBuffers } from './streamBuffers'
import { useSlotListSync, type SlotListSync } from './slotList'
import { attachReturnResync, clearBehindReads, RETURN_RESYNC_MIN_HIDDEN_MS } from './paneResync'

vi.mock('../../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn(),
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),
  },
}))

const ACTIVE = 'slot-active'
const MEMBER = 'member-bot'
const GEN = 'gen-a'
const ASKED = { role: 'user', content: 'Run the Linux cases', cls: 'msg msg-u', ts: '2026-11-17T18:18:34Z', meta: { mid: 'u-2' } }
const EARLIER = [
  { role: 'user', content: 'first ask', cls: 'msg msg-u', ts: '2026-11-17T18:11:22Z', meta: { mid: 'u-1' } },
  { role: 'assistant', content: 'first reply', cls: 'msg msg-a', ts: '2026-11-17T18:11:45Z', meta: { mid: 'a-1' } },
]
const REPLY = { role: 'assistant', content: 'Started the test in a new work session.', cls: 'msg msg-a', ts: '2026-11-17T18:19:26Z', meta: { mid: 'a-2' } }
/** The gateway's page for the member thread: before the reply was written,
 *  and after. Both name the same ended turn. */
const pageWithoutReply = { messages: [...EARLIER, ASKED], running: false, has_more: false, total: 3, queue: [], turn: 2, turn_gen: GEN }
const pageWithReply = { messages: [...EARLIER, ASKED, REPLY], running: false, has_more: false, total: 4, queue: [], turn: 2, turn_gen: GEN }

const activeRow = { key: ACTIVE, title: 'A', agent: 'kirocrew', running: false, messages: 2, last_message: 'x' } as ChatSlot
const memberRow = (over: Partial<ChatSlot> = {}) =>
  ({ key: MEMBER, title: 'Bot', agent: 'kirocrew', running: false, messages: 3, last_message: 'Run the Linux cases', turn: 2, turn_gen: GEN, ...over }) as ChatSlot
const buffers = { hasPendingChunks: () => false, flushChunks: () => {} } as unknown as StreamBuffers
const detailCalls = (slot: string) => vi.mocked(api.chatSlotDetail).mock.calls.filter(c => c[0] === slot).length

describe('a pane on screen that missed the reply to its last message', () => {
  let queryClient: QueryClient
  let stopObserving: () => void

  beforeEach(() => {
    vi.mocked(api.chatSlotDetail).mockReset()
    // The re-read records are module state, as they are for one page's life.
    clearBehindReads()
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(ACTIVE))
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // The Bot thread is mounted (a Crew Members pane) but is not the active slot.
    stopObserving = new QueryObserver(queryClient, {
      queryKey: slotMessagesQueryKey(MEMBER, undefined),
      queryFn: () => new Promise(() => {}),
      staleTime: Infinity,
    }).subscribe(() => {})
  })

  afterEach(() => {
    stopObserving()
    queryClient.clear()
    // The next test starts from a list without this test's member row.
    store.dispatch(sseSlots([activeRow]))
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(null))
  })

  const deliver = (sync: SlotListSync, rows: ChatSlot[]) => {
    const msg = { type: 'slots', data: rows }
    act(() => { sync.onSlots(msg, rows, JSON.stringify(msg)) })
  }
  const mountSync = () => renderHook(() => useSlotListSync(store.dispatch, queryClient, buffers)).result.current
  const memberShowsReply = () => selectSlotMessages(store.getState(), MEMBER).some(m => m.content === REPLY.content)

  it('re-reads the pane when the idle row says the turn was answered but the pane ends on the user', async () => {
    vi.mocked(api.chatSlotDetail).mockImplementation(async (slot: string) =>
      (slot === MEMBER ? pageWithoutReply : { messages: [], running: false, has_more: false, total: 0, queue: [] }) as never)
    const sync = mountSync()
    // The first list warms the pane; its page was read before the reply landed,
    // and it records turn 2 as ended.
    deliver(sync, [activeRow, memberRow({ interrupted: true } as Partial<ChatSlot>)])
    await vi.waitFor(() => expect(detailCalls(MEMBER)).toBe(1))
    await vi.waitFor(() => expect(selectTurnInterrupted(store.getState(), MEMBER)).toBe(true))
    expect(memberShowsReply()).toBe(false)

    // The reply is written. The row for the SAME ended turn now says it was
    // answered. Nothing else the tab tracks moved, so only the readings'
    // disagreement says the pane is behind.
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithReply as never)
    deliver(sync, [activeRow, memberRow({ messages: 4, last_message: REPLY.content, interrupted: false } as Partial<ChatSlot>)])
    await vi.waitFor(() => expect(detailCalls(MEMBER)).toBe(2))
    await vi.waitFor(() => expect(memberShowsReply()).toBe(true))
    expect(selectResumeInterrupted(store.getState(), MEMBER)).toBe(false)

    // The same row again reads nothing more.
    deliver(sync, [activeRow, memberRow({ messages: 4, last_message: REPLY.content, interrupted: false, title: 'Bot 2' } as Partial<ChatSlot>)])
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(detailCalls(MEMBER)).toBe(2)
  })

  it('tries the same row again when the re-read it sent failed', async () => {
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithoutReply as never)
    const sync = mountSync()
    deliver(sync, [activeRow, memberRow({ interrupted: true } as Partial<ChatSlot>)])
    await vi.waitFor(() => expect(selectTurnInterrupted(store.getState(), MEMBER)).toBe(true))
    const answered = memberRow({ messages: 4, last_message: REPLY.content, interrupted: false } as Partial<ChatSlot>)
    vi.mocked(api.chatSlotDetail).mockRejectedValueOnce(new Error('gateway busy'))
    deliver(sync, [activeRow, answered])
    await vi.waitFor(() => expect(detailCalls(MEMBER)).toBeGreaterThanOrEqual(2))
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(memberShowsReply()).toBe(false)
    const readsBefore = detailCalls(MEMBER)
    // Another session's row moves; the member row is the same one as before.
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithReply as never)
    deliver(sync, [{ ...activeRow, title: 'A2' }, answered])
    await vi.waitFor(() => expect(detailCalls(MEMBER)).toBe(readsBefore + 1))
    await vi.waitFor(() => expect(memberShowsReply()).toBe(true))
  })

  it('retries a failed re-read on a byte-identical repeat frame and lands the reply', async () => {
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithoutReply as never)
    const sync = mountSync()
    deliver(sync, [activeRow, memberRow({ interrupted: true } as Partial<ChatSlot>)])
    await vi.waitFor(() => expect(selectTurnInterrupted(store.getState(), MEMBER)).toBe(true))
    const rows = [activeRow, memberRow({ messages: 4, last_message: REPLY.content, interrupted: false } as Partial<ChatSlot>)]
    const msg = { type: 'slots', data: rows }
    const raw = JSON.stringify(msg)
    vi.mocked(api.chatSlotDetail).mockRejectedValueOnce(new Error('gateway busy'))
    // Await the failed read and its record cleanup before repeating the frame.
    await act(async () => { sync.onSlots(msg, rows, raw) })
    expect(detailCalls(MEMBER)).toBe(2)
    expect(memberShowsReply()).toBe(false)

    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithReply as never)
    await act(async () => { sync.onSlots(msg, rows, raw) })
    expect(detailCalls(MEMBER)).toBe(3)
    expect(memberShowsReply()).toBe(true)
    expect(selectResumeInterrupted(store.getState(), MEMBER)).toBe(false)
  })

  it('does not offer Resume while the pane is behind an idle row that says the turn was answered', async () => {
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithoutReply as never)
    const sync = mountSync()
    deliver(sync, [activeRow, memberRow({ interrupted: true } as Partial<ChatSlot>)])
    await vi.waitFor(() => expect(selectTurnInterrupted(store.getState(), MEMBER)).toBe(true))
    // The gateway's own reading agrees: Resume is right.
    expect(selectResumeInterrupted(store.getState(), MEMBER)).toBe(true)
    // An idle row that says the turn was answered withholds it, before any re-read lands.
    act(() => { store.dispatch(sseSlots([activeRow, memberRow({ messages: 4, interrupted: false } as Partial<ChatSlot>)])) })
    expect(selectTurnInterrupted(store.getState(), MEMBER)).toBe(true)
    expect(selectResumeInterrupted(store.getState(), MEMBER)).toBe(false)
    // A running row, or a row from a gateway without the field, leaves the transcript's reading.
    act(() => { store.dispatch(sseSlots([activeRow, memberRow({ running: true, interrupted: false } as Partial<ChatSlot>)])) })
    expect(selectResumeInterrupted(store.getState(), MEMBER)).toBe(true)
    act(() => { store.dispatch(sseSlots([activeRow, memberRow()])) })
    expect(selectResumeInterrupted(store.getState(), MEMBER)).toBe(true)
  })

  it('settles the live status of a turn whose _done never arrived', async () => {
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithReply as never)
    const sync = mountSync()
    deliver(sync, [activeRow, memberRow({ running: true, turn: 1 } as Partial<ChatSlot>)])
    // The turn's first frame set "Thinking…"; then its frames and `_done` were lost.
    act(() => { store.dispatch(setSlotStatusDetail({ slot: MEMBER, kind: 'thinking' })) })
    deliver(sync, [activeRow, memberRow({ messages: 4, interrupted: false } as Partial<ChatSlot>)])
    expect(store.getState().chat.slotStatusDetail[MEMBER]?.kind).toBe('idle')
  })

  it('leaves the live status alone while the tab has its own send waiting for its turn', () => {
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithReply as never)
    const sync = mountSync()
    deliver(sync, [activeRow, memberRow({ messages: 4, interrupted: false } as Partial<ChatSlot>)])
    // The pane sent a message; the user row echoed and set "Thinking…", but a
    // row built before the gateway started the turn still says idle.
    act(() => { store.dispatch(startLocalTurn(MEMBER)) })
    act(() => { store.dispatch(setSlotStatusDetail({ slot: MEMBER, kind: 'thinking' })) })
    deliver(sync, [activeRow, memberRow({ messages: 5, interrupted: false } as Partial<ChatSlot>)])
    expect(store.getState().chat.slotStatusDetail[MEMBER]?.kind).toBe('thinking')
  })

  it('leaves the live status alone while the row says the turn runs', () => {
    vi.mocked(api.chatSlotDetail).mockResolvedValue(pageWithReply as never)
    const sync = mountSync()
    deliver(sync, [activeRow, memberRow({ running: true, turn: 3 } as Partial<ChatSlot>)])
    act(() => { store.dispatch(setSlotStatusDetail({ slot: MEMBER, kind: 'thinking' })) })
    deliver(sync, [activeRow, memberRow({ running: true, turn: 3, title: 'Bot 2' } as Partial<ChatSlot>)])
    expect(store.getState().chat.slotStatusDetail[MEMBER]?.kind).toBe('thinking')
  })
})

describe('the page comes back after being hidden', () => {
  let queryClient: QueryClient
  let stopObserving: () => void
  let hidden = false
  let clock = 0
  let detach: () => void

  const setHidden = (value: boolean) => {
    hidden = value
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  }

  beforeEach(() => {
    vi.mocked(api.chatSlotDetail).mockReset()
    vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] } as never)
    hidden = false
    clock = 1_000_000
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(ACTIVE))
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    stopObserving = new QueryObserver(queryClient, {
      queryKey: slotMessagesQueryKey(MEMBER, undefined),
      queryFn: () => new Promise(() => {}),
      staleTime: Infinity,
    }).subscribe(() => {})
    detach = attachReturnResync(store.dispatch, queryClient, () => clock)
  })

  afterEach(() => {
    detach()
    stopObserving()
    queryClient.clear()
    // Drop the own-property override so the prototype getter is back.
    delete (document as unknown as { hidden?: boolean }).hidden
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(null))
  })

  it('re-reads the active slot and every mounted pane after a long hide', async () => {
    setHidden(true)
    clock += RETURN_RESYNC_MIN_HIDDEN_MS
    setHidden(false)
    await vi.waitFor(() => expect(detailCalls(ACTIVE)).toBe(1))
    await vi.waitFor(() => expect(detailCalls(MEMBER)).toBe(1))
  })

  it('drains buffered chunks before the first re-read after a long hide', async () => {
    const order: string[] = []
    const drain = vi.fn(() => { order.push('drain') })
    const unregister = registerPendingChunkDrain(drain)
    vi.mocked(api.chatSlotDetail).mockImplementation(async () => {
      order.push('read')
      return { messages: [], running: false, has_more: false, total: 0, queue: [] } as never
    })
    try {
      setHidden(true)
      clock += RETURN_RESYNC_MIN_HIDDEN_MS
      setHidden(false)
      await vi.waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
      expect(drain).toHaveBeenCalled()
      expect(order.slice(0, 2)).toEqual(['drain', 'read'])
    } finally {
      unregister()
    }
  })

  it('reads nothing after a short hide', async () => {
    setHidden(true)
    clock += RETURN_RESYNC_MIN_HIDDEN_MS - 1
    setHidden(false)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
  })

  it('re-reads on a restore from the back-forward cache, and not on a first load', async () => {
    act(() => { window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: false })) })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
    act(() => { window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true })) })
    await vi.waitFor(() => expect(detailCalls(MEMBER)).toBe(1))
    expect(detailCalls(ACTIVE)).toBe(1)
  })
})

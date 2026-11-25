import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { store } from '../../store'
import { setActiveSlot } from '../../store/chatSlice'
import { sseSlots, markSlotRead } from '../../store/dashboardSlice'
import { __resetSharedUnreadQueueForTests } from '../../store/dashboard/unread'
import { bindSlotReadSender, shownHere, _resetSlotReadRelayForTest } from '../../lib/slotReadRelay'
import { queueRead, queuedReads } from '../../lib/readOutbox'
import { api } from '../../api/client'
import type { ChatSlot } from '../../types'
import { applyServerReads } from './serverReads'
import { recordShown } from './attention'
import { runReconnectCatchUp, type CatchUpContext } from './reconnectCatchUp'

vi.mock('../../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn(),
  },
}))

const SHOWN = '2026-11-13T12:00:00+00:00'
const OLD_READ = '2026-11-13T10:00:00+00:00'
const EPOCH = '2026-11-13T08:00:00+00:00'
const reply = { role: 'assistant', content: 'while you were away', ts: SHOWN, meta: { mid: 'm1' } }
const sent: Array<[string, string | undefined]> = []
const queryClient = new QueryClient()
let shownCalls = vi.fn(recordShown)
const slotRow = (key: string): ChatSlot => ({
  key, title: key, messages: 1, running: false, last_read_ts: OLD_READ, read_epoch: EPOCH,
  unread_ts: SHOWN, last_ts: SHOWN,
} as ChatSlot)

function catchUp(): void {
  const ctx = {
    dispatch: store.dispatch,
    queryClient,
    socket: { logCbRef: { current: null }, awaitsFirstFrame: () => false },
    buffers: { dropForReconnect: () => {} },
    seedAutomations: () => {},
    syncPendingApprovals: () => Promise.resolve(),
    syncPendingQuestions: () => Promise.resolve(),
    syncWorkflowRuns: () => Promise.resolve(),
    focusedSlot: () => 'chat-a',
    recordShown: (shownCalls = vi.fn(recordShown)),
    reconnectSnapshot: { arm: () => {}, onSlots: () => {}, disarm: () => {} },
  } as unknown as CatchUpContext
  runReconnectCatchUp({ send: vi.fn() } as unknown as WebSocket, ctx)
}

function badgedAfterSwitchingAway(): boolean {
  store.dispatch(setActiveSlot('chat-b'))
  store.dispatch(sseSlots([slotRow('chat-a'), slotRow('chat-b')]))
  applyServerReads(store.getState().dashboard.slots, { dispatch: store.dispatch, queryClient })
  return store.getState().dashboard.unreadSlots.includes('chat-a')
}

describe('reconnect catch-up hydrating a reply into the slot on screen', () => {
  let hidden: ReturnType<typeof vi.spyOn>
  let focus: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __resetSharedUnreadQueueForTests()
    _resetSlotReadRelayForTest()
    sent.length = 0
    bindSlotReadSender((slot, ts) => { queueRead(slot, ts); sent.push([slot, ts]) })
    vi.mocked(api.chatSlotDetail).mockResolvedValue(
      { messages: [reply], running: false, has_more: false, total: 1, queue: [] } as never,
    )
    window.history.pushState({}, '', '/chat')
    hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    focus = vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    store.dispatch(sseSlots([slotRow('chat-a'), slotRow('chat-b')]))
    store.dispatch(setActiveSlot('chat-a'))
    for (const key of [...store.getState().dashboard.unreadSlots]) store.dispatch(markSlotRead(key))
  })

  afterEach(() => {
    hidden.mockRestore()
    focus.mockRestore()
    store.dispatch(setActiveSlot(null))
    store.dispatch(sseSlots([]))
    _resetSlotReadRelayForTest()
    window.history.pushState({}, '', '/')
  })

  it('relays the row as read, and a later slot list does not badge it after switching away', async () => {
    catchUp()
    await vi.waitFor(() => expect(sent).toEqual([['chat-a', SHOWN]]))
    expect(queuedReads()).toEqual({ 'chat-a': SHOWN })
    expect(badgedAfterSwitchingAway()).toBe(false)
  })

  it('an unfocused window sends no read and keeps the row as shown here', async () => {
    focus.mockReturnValue(false)
    catchUp()
    await vi.waitFor(() => expect(shownHere('chat-a')).toBe(SHOWN))
    expect(sent).toEqual([])
    expect(queuedReads()).toEqual({})
    expect(badgedAfterSwitchingAway()).toBe(false)
  })

  it('on a non-chat route the retained active slot keeps nothing as shown, so it badges', async () => {
    window.history.pushState({}, '', '/settings')
    catchUp()
    await vi.waitFor(() => expect(shownCalls).toHaveBeenCalledWith('chat-a', SHOWN))
    expect(badgedAfterSwitchingAway()).toBe(true)
  })
})

/** A live slots frame that reports a slot not running usually arrives before
 * `chat_done`, and can also heal a `_done` the tab never applied. Active and
 * background settlement must take chat_done's ordering rule: text already
 * received for that slot lands before settlement, or the buffered tail is
 * dispatched afterwards and opens a second streaming row that nothing ends.
 * Frames are hand-driven and never run here, reproducing the hidden-tab
 * condition where the buffer holds text.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { api } from '../api/client'
import {
  setActiveSlot,
  clearSlotState,
  selectComposerBusy,
  sseChatMessage,
  startLocalTurn,
  switchSlot,
} from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),  // never resolves
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const SLOT = 'slot-1'
const GEN = 'gateway-a'
const slotsFrame = (rows: Array<{ key: string; running: boolean; turn?: number; turn_gen?: string }>) => ({
  type: 'slots',
  data: rows.map(r => ({ turn: 1, turn_gen: GEN, ...r, messages: 1, stopping: false, mode: '' })),
})
const chunk = (content: string, seq: number) => ({ type: 'chat_chunk', data: { slot: SLOT, content, seq } })
const replies = () =>
  globalStore.getState().chat.messages.filter(m => m.role === 'streaming' || m.role === 'assistant')

describe('useWebSocket: a slots frame ending the active turn flushes its buffered text first', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
    queryClient.clear()
  })

  function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, {
        store: globalStore,
        children: createElement(QueryClientProvider, { client: queryClient }, children),
      })
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(SLOT))
    return { hook, ws }
  }

  const drainFrames = () => {
    const frames = rafQueue.splice(0)
    act(() => { frames.forEach(frame => frame(0)) })
  }

  it('lands the buffered tail, then settles the turn into one finished reply', () => {
    const { ws, hook } = mount()
    act(() => { ws.simulateMessage(slotsFrame([{ key: SLOT, running: true }])) })
    act(() => {
      ws.simulateMessage(chunk('Hello ', 1))
      ws.simulateMessage(chunk('world', 2))
    })
    // Still buffered: no frame has run.
    expect(replies()).toEqual([])

    // The turn's `_done` never arrives; the live frame reports it ended.
    act(() => { ws.simulateMessage(slotsFrame([{ key: SLOT, running: false }])) })
    drainFrames()

    expect(replies().map(m => [m.role, m.content])).toEqual([['assistant', 'Hello world']])
    expect(globalStore.getState().chat.slotState).toBe('idle')
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(false)
    hook.unmount()
  })

  it('flushes a buffered background tail before its idle slots row settles it', () => {
    const dispatchSpy = vi.spyOn(globalStore, 'dispatch')
    const { ws, hook } = mount()
    const background = 'slot-2'

    act(() => {
      ws.simulateMessage(chunk('active answer', 1))
      ws.simulateMessage({
        type: 'chat_chunk',
        data: { slot: background, content: 'background head', seq: 1 },
      })
    })
    drainFrames()
    expect(globalStore.getState().chat).toMatchObject({
      activeSlot: SLOT,
      slotState: 'streaming',
    })
    expect(globalStore.getState().chat.slotRun[background]?.state).toBe('streaming')

    act(() => {
      ws.simulateMessage({
        type: 'chat_chunk',
        data: { slot: background, content: ' and tail', seq: 2 },
      })
    })
    dispatchSpy.mockClear()
    act(() => {
      ws.simulateMessage(slotsFrame([
        { key: SLOT, running: true, turn: 7 },
        { key: background, running: false, turn: 41 },
      ]))
    })

    const actionTypes = dispatchSpy.mock.calls.map(([action]) =>
      typeof action === 'object' && action !== null && 'type' in action ? action.type : null)
    const chunkAt = actionTypes.indexOf(sseChatMessage.type)
    expect(chunkAt).toBeGreaterThanOrEqual(0)
    expect(actionTypes.indexOf(sseSlots.type)).toBeGreaterThan(chunkAt)

    const settled = globalStore.getState().chat
    expect(globalStore.getState().dashboard.slots.find(s => s.key === SLOT)?.running).toBe(true)
    expect(settled.slotState).toBe('streaming')
    expect(settled.slotRun[background]?.state).toBe('idle')
    expect(settled.endedTurn[background]).toEqual({ gen: GEN, turn: 41 })
    expect(settled.slotMessages[background]
      .filter(m => m.role === 'streaming' || m.role === 'assistant')
      .map(m => [m.role, m.content]))
      .toEqual([['assistant', 'background head and tail']])

    const settledMessages = settled.slotMessages[background]
    act(() => {
      ws.simulateMessage({
        type: 'chat_done',
        data: { slot: background, turn: 41, turn_gen: GEN },
      })
    })
    drainFrames()
    const afterDone = globalStore.getState().chat
    expect(afterDone.slotMessages[background]).toBe(settledMessages)
    expect(afterDone.slotMessages[background]
      .filter(m => m.role === 'streaming' || m.role === 'assistant')
      .map(m => [m.role, m.content]))
      .toEqual([['assistant', 'background head and tail']])
    expect(afterDone.slotRun[background]?.state).toBe('idle')
    hook.unmount()
  })

  it('keeps buffering while the live frame reports the slot running', () => {
    const { ws, hook } = mount()
    act(() => { ws.simulateMessage(chunk('Hello ', 1)) })
    act(() => { ws.simulateMessage(slotsFrame([{ key: SLOT, running: true }])) })
    expect(replies()).toEqual([])
    drainFrames()
    expect(replies().map(m => [m.role, m.content])).toEqual([['streaming', 'Hello ']])
    hook.unmount()
  })

  it('does not flush for another slot reporting not running', () => {
    const { ws, hook } = mount()
    act(() => { ws.simulateMessage(chunk('Hello ', 1)) })
    act(() => {
      ws.simulateMessage(slotsFrame([{ key: SLOT, running: true }, { key: 'slot-2', running: false }]))
    })
    expect(replies()).toEqual([])
    hook.unmount()
  })

  it('dedupes an identical idle frame after stale history is fenced by turn identity', () => {
    const dispatchSpy = vi.spyOn(globalStore, 'dispatch')
    const { ws, hook } = mount()
    const idleFrame = slotsFrame([{ key: SLOT, running: false, turn: 7 }])
    act(() => {
      globalStore.dispatch(sseChatMessage({
        slot: SLOT,
        role: 'chunk',
        content: 'live answer',
        seq: 1,
      }))
      globalStore.dispatch(switchSlot.pending('stale-switch', SLOT))
    })
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(true)

    act(() => { ws.simulateMessage(idleFrame) })
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(false)

    act(() => {
      globalStore.dispatch(switchSlot.fulfilled({
        key: SLOT,
        running: true,
        turn: 7,
        turn_gen: GEN,
        hasMore: false,
        total: 1,
        queue: [],
        stopping: false,
        messages: [{ role: 'assistant', content: 'stale answer', cls: '' }],
      } as never, 'stale-switch', SLOT))
    })
    expect(globalStore.getState().chat.slotState).toBe('idle')
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(false)

    dispatchSpy.mockClear()
    act(() => { ws.simulateMessage(idleFrame) })
    const slotsDispatches = dispatchSpy.mock.calls.filter(([action]) =>
      typeof action === 'object' && action !== null && 'type' in action
        && action.type === sseSlots.type,
    )
    expect(slotsDispatches).toHaveLength(0)
    hook.unmount()
  })

  it('reapplies an identical idle frame after identity-less stale history restores busy', () => {
    const { ws, hook } = mount()
    const idleFrame = slotsFrame([{
      key: SLOT,
      running: false,
      turn: undefined,
      turn_gen: undefined,
    }])
    act(() => {
      globalStore.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'answer', seq: 1 }))
      globalStore.dispatch(switchSlot.pending('legacy-switch', SLOT))
      ws.simulateMessage(idleFrame)
    })
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(false)

    act(() => {
      globalStore.dispatch(switchSlot.fulfilled({
        key: SLOT,
        running: true,
        hasMore: false,
        total: 1,
        queue: [],
        stopping: false,
        messages: [{ role: 'assistant', content: 'stale answer', cls: '' }],
      } as never, 'legacy-switch', SLOT))
    })
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(true)

    act(() => { ws.simulateMessage(idleFrame) })
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(false)
    hook.unmount()
  })

  it('dedupes an identical idle frame when the active chat is already idle', () => {
    const dispatchSpy = vi.spyOn(globalStore, 'dispatch')
    const { ws, hook } = mount()
    dispatchSpy.mockClear()
    const idleFrame = slotsFrame([{ key: SLOT, running: false }])

    act(() => { ws.simulateMessage(idleFrame) })
    const slotsAfterFirstFrame = globalStore.getState().dashboard.slots
    act(() => { ws.simulateMessage(idleFrame) })

    const slotsDispatches = dispatchSpy.mock.calls.filter(([action]) =>
      typeof action === 'object' && action !== null && 'type' in action
        && action.type === sseSlots.type,
    )
    expect(slotsDispatches).toHaveLength(1)
    expect(globalStore.getState().dashboard.slots).toBe(slotsAfterFirstFrame)
    hook.unmount()
  })

  it('does not settle an identical idle frame over a pending local send', () => {
    const { ws, hook } = mount()
    const idleFrame = slotsFrame([{ key: SLOT, running: false }])
    act(() => { ws.simulateMessage(idleFrame) })
    const slotsAfterFirstFrame = globalStore.getState().dashboard.slots

    act(() => { globalStore.dispatch(startLocalTurn(SLOT)) })
    expect(globalStore.getState().chat.pendingTurnSlot).toBe(SLOT)
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(true)

    act(() => { ws.simulateMessage(idleFrame) })
    expect(globalStore.getState().dashboard.slots).toBe(slotsAfterFirstFrame)
    expect(globalStore.getState().chat.pendingTurnSlot).toBe(SLOT)
    expect(globalStore.getState().chat.slotRunning).toBe(true)
    expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(true)
    hook.unmount()
  })

  describe('the completion refresh: one per turn end, with or without _done', () => {
    /** Slot-detail fetches for the active slot, i.e. `refreshSlot` dispatches
     *  that reached the network (the background warm never fetches the active
     *  slot). Each case uses its own gateway generation so a turn another case
     *  recorded as ended cannot fence this one's `_done`. */
    const refreshes = () => vi.mocked(api.chatSlotDetail).mock.calls.filter(([key]) => key === SLOT).length
    const turnRow = (running: boolean, turn: number, gen: string) =>
      slotsFrame([{ key: SLOT, running, turn, turn_gen: gen }])
    const chatDone = (turn: number, gen: string) =>
      ({ type: 'chat_done', data: { slot: SLOT, turn, turn_gen: gen } })
    /** The turn streams on screen: its running row, then one landed chunk. */
    function streamTurn(ws: MockWebSocket, turn: number, gen: string, seq: number) {
      act(() => { ws.simulateMessage(turnRow(true, turn, gen)) })
      act(() => { ws.simulateMessage(chunk(`answer ${turn}`, seq)) })
      drainFrames()
      expect(globalStore.getState().chat.slotState).toBe('streaming')
    }

    it('an idle row that ends the turn with no _done re-hydrates the pane once', async () => {
      const { ws, hook } = mount()
      const gen = 'gateway-no-done'
      streamTurn(ws, 3, gen, 1)
      vi.mocked(api.chatSlotDetail).mockResolvedValueOnce({
        messages: [
          { role: 'user', content: 'question', cls: '', meta: { mid: 'u-3' } },
          { role: 'assistant', content: 'answer 3, in full', cls: 'msg msg-a', meta: { mid: 'a-3' } },
        ],
        running: false, turn: 3, turn_gen: gen, has_more: false, total: 2, queue: [],
      })
      const before = refreshes()

      act(() => { ws.simulateMessage(turnRow(false, 3, gen)) })

      expect(refreshes() - before).toBe(1)
      expect(selectComposerBusy(globalStore.getState(), SLOT)).toBe(false)
      await vi.waitFor(() => {
        expect(replies().map(m => [m.role, m.content])).toEqual([['assistant', 'answer 3, in full']])
      })
      hook.unmount()
    })

    it('an idle row whose flush lands the only text of the turn still refreshes once', () => {
      const { ws, hook } = mount()
      const gen = 'gateway-buffered'
      act(() => { ws.simulateMessage(turnRow(true, 3, gen)) })
      // Hidden tab: the text is still buffered when the idle row arrives.
      act(() => { ws.simulateMessage(chunk('buffered answer', 1)) })
      const before = refreshes()

      act(() => { ws.simulateMessage(turnRow(false, 3, gen)) })

      expect(refreshes() - before).toBe(1)
      hook.unmount()
    })

    it('an idle row that ends a pending send refreshes once', () => {
      const { ws, hook } = mount()
      const gen = 'gateway-pending'
      act(() => {
        globalStore.dispatch(startLocalTurn(SLOT))
        ws.simulateMessage(chunk('instant answer', 1))
      })
      drainFrames()
      const before = refreshes()

      act(() => { ws.simulateMessage(turnRow(false, 1, gen)) })

      expect(globalStore.getState().chat.pendingTurnSlot).toBeNull()
      expect(refreshes() - before).toBe(1)
      hook.unmount()
    })

    it('an idle row then the _done naming the same turn refreshes once', () => {
      const { ws, hook } = mount()
      const gen = 'gateway-row-first'
      streamTurn(ws, 4, gen, 1)
      const before = refreshes()

      act(() => { ws.simulateMessage(turnRow(false, 4, gen)) })
      act(() => { ws.simulateMessage(chatDone(4, gen)) })

      expect(refreshes() - before).toBe(1)
      hook.unmount()
    })

    it('a _done then the idle row for the same turn refreshes once, from the _done', () => {
      const { ws, hook } = mount()
      const gen = 'gateway-done-first'
      streamTurn(ws, 5, gen, 1)
      const before = refreshes()

      act(() => { ws.simulateMessage(chatDone(5, gen)) })
      expect(refreshes() - before).toBe(1)
      act(() => { ws.simulateMessage(turnRow(false, 5, gen)) })

      expect(refreshes() - before).toBe(1)
      hook.unmount()
    })

    it("a lost _done's record does not swallow the next turn's _done refresh", () => {
      const { ws, hook } = mount()
      const gen = 'gateway-lost-done'
      streamTurn(ws, 6, gen, 1)
      // Turn 6 ends on its idle row; its `_done` never arrives.
      act(() => { ws.simulateMessage(turnRow(false, 6, gen)) })
      // Turn 7 streams, and its `_done` precedes its idle row.
      streamTurn(ws, 7, gen, 2)
      const before = refreshes()

      act(() => { ws.simulateMessage(chatDone(7, gen)) })
      act(() => { ws.simulateMessage(turnRow(false, 7, gen)) })

      expect(refreshes() - before).toBe(1)
      hook.unmount()
    })
  })
})

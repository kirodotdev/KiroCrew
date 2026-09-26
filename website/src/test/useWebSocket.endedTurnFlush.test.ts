/**
 * A live slots frame that reports the active slot not running stands in for a
 * `_done` the tab never applied (chatSlice settleEndedActiveTurn). It must
 * take chat_done's ordering rule: text already received for that slot lands
 * before the settlement, or the buffered tail is dispatched afterwards and
 * opens a second streaming row that nothing ends. Frames are hand-driven and
 * never run here — the hidden-tab condition where the buffer holds text.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, selectComposerBusy } from '../store/chatSlice'

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
const slotsFrame = (rows: Array<{ key: string; running: boolean }>) => ({
  type: 'slots',
  data: rows.map(r => ({ ...r, messages: 1, stopping: false, mode: '' })),
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
})

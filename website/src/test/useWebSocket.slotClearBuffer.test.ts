/**
 * `slot_clear` must drop the slot's buffered stream text along with the
 * transcript. The chunk buffer flushes once per animation frame, so a chunk
 * (or chat_thinking text — same entry) buffered just before a /clear lands
 * would otherwise flush on the NEXT frame and resurrect discarded text into
 * the just-cleared pane. The delete is keyed: clearing one slot must not
 * touch another slot's in-flight buffer.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState } from '../store/chatSlice'

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

/** Deliberately the SINGLETON store: useWebSocket dispatches via useAppDispatch()
 *  but reads state off the imported singleton, so a separate Provider store would
 *  let reads and writes diverge. */
function seedStore() {
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
  return globalStore
}

const streamingText = () =>
  globalStore.getState().chat.messages.find(m => m.role === 'streaming')?.content ?? ''

const thinkingText = () =>
  globalStore.getState().chat.messages.find(m => m.role === 'thinking' && m.content)?.content ?? ''

const chunk = (slot: string, content: string) => ({
  type: 'chat_chunk',
  data: { slot, content },
})

const thinking = (slot: string, content: string) => ({
  type: 'chat_thinking',
  data: { slot, content },
})

const slotClear = (slot: string) => ({
  type: 'slot_clear',
  data: { slot },
})

describe('useWebSocket slot_clear drops buffered chunks', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Hand-driven frames: nothing flushes until runFrames() is called
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

  function runFrames() {
    const pending = rafQueue
    rafQueue = []
    act(() => { pending.forEach(cb => cb(performance.now())) })
  }

  function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  it('a chunk buffered before slot_clear does not flush back into the cleared transcript', () => {
    const { ws } = mount()
    seedStore()

    // Chunk arrives, buffered — the flush frame has NOT run yet (that gap is
    // exactly the bug window: /clear lands between buffer-in and flush-out).
    act(() => { ws.simulateMessage(chunk('slot-1', 'discarded tail')) })
    expect(streamingText()).toBe('')

    act(() => { ws.simulateMessage(slotClear('slot-1')) })

    // The frame that would have flushed the pre-clear chunk now runs.
    runFrames()

    expect(streamingText()).toBe('')
    expect(globalStore.getState().chat.messages.some(m => m.content?.includes('discarded tail'))).toBe(false)
  })

  it('buffered thinking text is discarded by slot_clear too (shared buffer entry)', () => {
    const { ws } = mount()
    seedStore()

    act(() => { ws.simulateMessage(thinking('slot-1', 'discarded reasoning')) })
    expect(thinkingText()).toBe('')

    act(() => { ws.simulateMessage(slotClear('slot-1')) })
    runFrames()

    expect(thinkingText()).toBe('')
  })

  it('clearing one slot leaves another slot\'s in-flight buffer intact', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(chunk('slot-1', 'survivor'))
      ws.simulateMessage(slotClear('slot-2'))
    })
    runFrames()

    // slot-1 is the active slot; its buffered chunk must still land.
    expect(streamingText()).toBe('survivor')
  })

  it('streaming resumes normally after a slot_clear', () => {
    const { ws } = mount()
    seedStore()

    act(() => { ws.simulateMessage(chunk('slot-1', 'old ')) })
    act(() => { ws.simulateMessage(slotClear('slot-1')) })
    act(() => { ws.simulateMessage(chunk('slot-1', 'fresh start')) })
    runFrames()

    expect(streamingText()).toBe('fresh start')
  })
})

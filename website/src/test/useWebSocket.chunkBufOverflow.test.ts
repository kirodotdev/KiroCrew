/**
 * The chunk buffer drains once per animation frame, and a hidden window never
 * runs that frame. Past CHUNK_BUF_FLUSH_CHARS the buffer must drain
 * synchronously so a backgrounded renderer streaming a long turn holds at
 * most one threshold's worth of text outside the store — the same guard the
 * subagent buffer applies. Frames are hand-driven here and never run, which
 * is exactly the hidden-window condition.
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

/** The SINGLETON store on purpose: useWebSocket dispatches via useAppDispatch()
 *  but reads state off the imported singleton. */
function seedStore() {
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
}

const streamingText = () =>
  globalStore.getState().chat.messages.find(m => m.role === 'streaming')?.content ?? ''

const thinkingText = () =>
  globalStore.getState().chat.messages.find(m => m.role === 'thinking' && m.content)?.content ?? ''

const chunk = (slot: string, content: string, seq: number) => ({
  type: 'chat_chunk',
  data: { slot, content, seq },
})

const thinking = (slot: string, content: string) => ({
  type: 'chat_thinking',
  data: { slot, content },
})

// Mirrors CHUNK_BUF_FLUSH_CHARS in useWebSocket.ts; a pinned literal so a
// silent change to the threshold fails here rather than shifting the test.
const THRESHOLD = 50_000
const PIECE = 'p'.repeat(10_000)

describe('useWebSocket chunk buffer overflow flush (hidden window)', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Frames are queued and NEVER run: the hidden-window condition.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

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

  it('content below the threshold stays buffered until a frame runs', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 1; i <= 5; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))   // exactly THRESHOLD
    })
    expect(streamingText()).toBe('')
  })

  it('content past the threshold lands in the store with no frame', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 1; i <= 6; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))   // THRESHOLD + one piece
    })
    expect(streamingText().length).toBe(THRESHOLD + PIECE.length)
  })

  it('thinking past the threshold lands in the store with no frame', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 0; i < 6; i++) ws.simulateMessage(thinking('slot-1', PIECE))
    })
    expect(thinkingText().length).toBe(THRESHOLD + PIECE.length)
  })

  it('the counter restarts after an overflow flush, so the next burst buffers again', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 1; i <= 6; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))
    })
    const landed = streamingText().length
    expect(landed).toBe(THRESHOLD + PIECE.length)
    // One more piece: below the threshold again, so it waits for a frame.
    act(() => { ws.simulateMessage(chunk('slot-1', PIECE, 7)) })
    expect(streamingText().length).toBe(landed)
  })

  it('content and thinking share one budget per slot', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 0; i < 3; i++) ws.simulateMessage(thinking('slot-1', PIECE))
      for (let i = 1; i <= 2; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))   // total = THRESHOLD
    })
    expect(streamingText()).toBe('')
    expect(thinkingText()).toBe('')
    act(() => { ws.simulateMessage(chunk('slot-1', PIECE, 3)) })                     // tips over
    expect(thinkingText().length).toBe(3 * PIECE.length)
    expect(streamingText().length).toBe(3 * PIECE.length)
  })
})

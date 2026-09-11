/**
 * A rapid steer must not merge pre- and post-steer output (#9977).
 *
 * Chat chunks are buffered in useWebSocket and flushed once per animation
 * frame. A steer landing inside that window (chunk buffered, flush frame not
 * yet run) used to find no streaming row in Redux, so finalize-on-steer froze
 * nothing: the pre-steer text flushed BELOW the steer card and post-steer
 * chunks appended to the same streaming row. These tests hand-drive the
 * animation frames so the chunk is still PENDING IN THE BUFFER -- not already
 * in Redux, which the existing finalize-on-steer reducer tests cover -- when
 * the steer arrives, for both insertion sites: the `steer_push` WS handler
 * (observing tab) and the optimistic dispatch via the drain seam (initiating
 * tab, ChatPage/ChatPane).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, appendMessage } from '../store/chatSlice'
import { drainPendingChunks } from '../lib/pendingChunkDrain'

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

const chunk = (slot: string, content: string) => ({
  type: 'chat_chunk',
  data: { slot, content },
})

const steerPush = (slot: string, content: string) => ({
  type: 'steer_push',
  data: { slot, content },
})

/** The transcript reduced to (role, content) rows for order assertions. */
const rows = () =>
  globalStore.getState().chat.messages
    .filter(m => m.role === 'assistant' || m.role === 'streaming' || (m.role === 'user' && m.meta?.steer))
    .map(m => ({ role: m.role, content: m.content }))

describe('useWebSocket: steer with a chunk still pending in the frame buffer (#9977)', () => {
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

  it('steer_push finalizes the pending pre-steer chunk ABOVE the card; post-steer opens a new row', () => {
    const { ws } = mount()
    seedStore()

    // Pre-steer chunk arrives and is buffered -- the flush frame has NOT run.
    act(() => { ws.simulateMessage(chunk('slot-1', 'pre-steer text')) })
    expect(rows()).toEqual([])  // still in the buffer, not in Redux

    // The steer echo lands inside the buffer window (observing-tab path).
    act(() => { ws.simulateMessage(steerPush('slot-1', 'steer message')) })

    // Post-steer output resumes; then the deferred frame finally runs.
    act(() => { ws.simulateMessage(chunk('slot-1', 'post-steer text')) })
    runFrames()

    expect(rows()).toEqual([
      { role: 'assistant', content: 'pre-steer text' },   // frozen above the card
      { role: 'user', content: 'steer message' },
      { role: 'streaming', content: 'post-steer text' },  // NEW row below the card
    ])
  })

  it('the optimistic steer dispatch (initiating tab) drains the buffer via the seam first', () => {
    const { ws } = mount()
    seedStore()

    act(() => { ws.simulateMessage(chunk('slot-1', 'pre-steer text')) })
    expect(rows()).toEqual([])

    // What ChatPage.steer() / ChatPane's steer path do: drain, then dispatch
    // the optimistic card.
    act(() => {
      drainPendingChunks()
      globalStore.dispatch(appendMessage({ role: 'user', content: 'steer message', cls: 'msg msg-u', ts: 't1', meta: { steer: true, optimistic: true, sendId: 'sid-1' } }))
    })

    act(() => { ws.simulateMessage(chunk('slot-1', 'post-steer text')) })
    runFrames()

    expect(rows()).toEqual([
      { role: 'assistant', content: 'pre-steer text' },
      { role: 'user', content: 'steer message' },
      { role: 'streaming', content: 'post-steer text' },
    ])
  })

  it('steer_push with an empty buffer still inserts normally', () => {
    const { ws } = mount()
    seedStore()

    act(() => { ws.simulateMessage(steerPush('slot-1', 'steer message')) })
    act(() => { ws.simulateMessage(chunk('slot-1', 'after')) })
    runFrames()

    expect(rows()).toEqual([
      { role: 'user', content: 'steer message' },
      { role: 'streaming', content: 'after' },
    ])
  })
})

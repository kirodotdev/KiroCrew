/**
 * `chat_thinking` must buffer into the shared chunk buffer and flush once per
 * frame, not dispatch per token. Reasoning streams run for hundreds of tokens,
 * so a per-token dispatch recomputes the O(N) displayItems on every one.
 * Mirrors the chat_chunk / subagent_chunk coalescing pattern.
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

const thinkingRows = () =>
  globalStore.getState().chat.messages.filter(m => m.role === 'thinking' && m.content)

const streamingText = () =>
  globalStore.getState().chat.messages.find(m => m.role === 'streaming')?.content ?? ''

const thinking = (slot: string, content: string) => ({
  type: 'chat_thinking',
  data: { slot, content },
})

const chunk = (slot: string, content: string, seq?: number) => ({
  type: 'chat_chunk',
  data: { slot, content, ...(seq !== undefined ? { seq } : {}) },
})

describe('useWebSocket chat_thinking coalescing', () => {
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

  it('does not dispatch synchronously on each thinking event', () => {
    const { ws } = mount()
    seedStore()

    act(() => { ws.simulateMessage(thinking('slot-1', 'hmm ')) })

    // Unbuffered code would insert a thinking row here; buffered code has not flushed.
    expect(thinkingRows()).toHaveLength(0)

    runFrames()
    expect(thinkingRows()).toHaveLength(1)
    expect(thinkingRows()[0].content).toBe('hmm ')
  })

  it('collapses a burst into one thinking row with all text concatenated', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      for (let i = 0; i < 40; i++) ws.simulateMessage(thinking('slot-1', `t${i} `))
    })

    expect(thinkingRows()).toHaveLength(0)

    runFrames()

    const expected = Array.from({ length: 40 }, (_, i) => `t${i} `).join('')
    expect(thinkingRows()).toHaveLength(1)
    expect(thinkingRows()[0].content).toBe(expected)
  })

  it('lands thinking before answer chunks when one frame holds both', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(thinking('slot-1', 'reasoning '))
      ws.simulateMessage(chunk('slot-1', 'answer'))
      ws.simulateMessage(thinking('slot-1', 'more'))
    })
    runFrames()

    // One thinking row carrying the full reasoning text, positioned before the
    // streaming answer row.
    const msgs = globalStore.getState().chat.messages
    const thinkIdx = msgs.findIndex(m => m.role === 'thinking' && m.content)
    const streamIdx = msgs.findIndex(m => m.role === 'streaming')
    expect(thinkIdx).toBeGreaterThanOrEqual(0)
    expect(streamIdx).toBeGreaterThan(thinkIdx)
    expect(msgs[thinkIdx].content).toBe('reasoning more')
    expect(streamingText()).toBe('answer')
  })

  it('flushes buffered thinking synchronously before a chat_message lands', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(thinking('slot-1', 'buffered thought'))
      ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-1', role: 'assistant', content: 'final' } })
    })

    // No frame ran, yet the thinking row exists and precedes the final message:
    // chat_message's synchronous flushChunks() must drain the thinking buffer.
    const msgs = globalStore.getState().chat.messages
    const thinkIdx = msgs.findIndex(m => m.role === 'thinking' && m.content === 'buffered thought')
    const finalIdx = msgs.findIndex(m => m.role === 'assistant' && m.content === 'final')
    expect(thinkIdx).toBeGreaterThanOrEqual(0)
    expect(finalIdx).toBeGreaterThan(thinkIdx)
  })

  it('keeps per-slot thinking independent and ignores inactive-slot rows in the transcript', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(thinking('slot-1', 'active '))
      ws.simulateMessage(thinking('slot-2', 'other '))
    })
    runFrames()

    // The reducer only renders the active slot's transcript; slot-2's text must
    // not bleed into it.
    expect(thinkingRows()).toHaveLength(1)
    expect(thinkingRows()[0].content).toBe('active ')
  })

  it('keeps the messages array reference stable across a burst until the frame lands', () => {
    const { ws } = mount()
    const store = seedStore()
    const before = store.getState().chat.messages

    act(() => {
      for (let i = 0; i < 12; i++) ws.simulateMessage(thinking('slot-1', 'x'))
    })
    expect(store.getState().chat.messages).toBe(before)

    runFrames()
    expect(store.getState().chat.messages).not.toBe(before)
  })

  it('salvages buffered thinking on reconnect instead of dropping it', () => {
    // Fake ONLY setTimeout (the reconnect backoff): default fake timers also
    // fake requestAnimationFrame, and advanceTimersByTime would then run the
    // scheduled flush — emptying the buffer before the reconnect this test is
    // about. The hand-driven rafQueue stub must stay in control so the buffer
    // is genuinely unflushed when the socket reopens (as in a hidden tab).
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const { ws } = mount()
      seedStore()

      // Reasoning arrives but the frame never runs (a hidden tab suspends rAF).
      act(() => { ws.simulateMessage(thinking('slot-1', 'unflushed reasoning')) })
      expect(thinkingRows()).toHaveLength(0)

      // Disconnect -> reconnect. Reasoning is client-only (never persisted
      // server-side), so unlike buffered content the reconnect refresh cannot
      // recover it: the reconnect branch must land it in the store before
      // clearing the buffer.
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(2000) })
      const ws2 = WS_INSTANCES[1]
      act(() => { ws2.simulateOpen() })

      expect(thinkingRows()).toHaveLength(1)
      expect(thinkingRows()[0].content).toBe('unflushed reasoning')
    } finally {
      vi.useRealTimers()
    }
  })

  it('salvages buffered thinking on unmount', () => {
    const { hook, ws } = mount()
    seedStore()

    act(() => { ws.simulateMessage(thinking('slot-1', 'tail reasoning')) })
    expect(thinkingRows()).toHaveLength(0)

    // The store outlives the hook; buffered reasoning must not die with it.
    hook.unmount()

    expect(thinkingRows()).toHaveLength(1)
    expect(thinkingRows()[0].content).toBe('tail reasoning')
  })
})

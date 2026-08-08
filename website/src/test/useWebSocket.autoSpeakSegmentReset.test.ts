/**
 * Auto-speak's turn-completion pass exists to speak only the tail the
 * sentence-boundary streamer never reached. `chat_segment` resets the
 * spoken-length counter to 0, so after a segment the completion pass would
 * slice the finished reply from 0 and speak EVERYTHING a second time.
 *
 * These tests pin both sides: a segment-reset turn is not re-spoken, and a
 * reply that never streamed through the sentence path is still spoken in full.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store } from '../store'
import { setActiveSlot, clearMessages } from '../store/chatSlice'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: true }),
    voiceSynthesize: vi.fn().mockResolvedValue({}),
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

  constructor() { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const SENTENCE = 'This is the first spoken sentence. '

describe('useWebSocket auto-speak after a segment reset', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // The hook reads streamed messages and the active slot off the singleton
    // store, so the Provider must hand it that same store.
    store.dispatch(setActiveSlot('slot-1'))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    store.dispatch(clearMessages())
    store.dispatch(setActiveSlot(null))
  })

  async function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    // Let the onopen voiceConfig() promise resolve so autoSpeak is cached.
    await act(async () => {})
    return { hook, ws }
  }

  it('does not re-speak the whole reply when chat_segment reset the counter', async () => {
    const { hook, ws } = await mount()

    // Stream a full sentence, then a segment boundary: the flush speaks the
    // sentence, the segment finalizes it and resets the spoken counter.
    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'slot-1', content: SENTENCE, seq: 1 } })
      ws.simulateMessage({ type: 'chat_segment', data: { slot: 'slot-1' } })
    })
    await act(async () => {})
    expect(api.voiceSynthesize).toHaveBeenCalledTimes(1)
    expect(api.voiceSynthesize).toHaveBeenCalledWith('slot-1', SENTENCE.trim())

    // Turn completion: everything streamed was already spoken, so nothing may
    // be synthesized again — slicing from the reset counter would repeat the
    // entire reply.
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-1' } }) })
    await act(async () => {})
    expect(api.voiceSynthesize).toHaveBeenCalledTimes(1)

    hook.unmount()
  })

  it('still speaks a reply that never went through the streaming path', async () => {
    const { hook, ws } = await mount()

    // A short or non-streamed reply arrives as a plain assistant message: the
    // completion pass is its only chance to be spoken, and the counter is 0
    // because nothing streamed — that must not be mistaken for a segment reset.
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: 'slot-1', role: 'assistant', content: 'A reply that never streamed at all.', ts: '10.0' } })
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'slot-1' } })
    })
    await act(async () => {})

    expect(api.voiceSynthesize).toHaveBeenCalledTimes(1)
    expect(api.voiceSynthesize).toHaveBeenCalledWith('slot-1', 'A reply that never streamed at all.')

    hook.unmount()
  })
})

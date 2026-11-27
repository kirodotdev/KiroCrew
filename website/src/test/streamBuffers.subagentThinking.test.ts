/**
 * `subagent_thinking` frames buffer beside the same card's `subagent_chunk`
 * text and land with it as ONE streamFlush item per frame, the reasoning kept
 * apart from the output. A done frame lands the buffered reasoning first, and a
 * snapshot or a reconnect, which drop the buffered text, keep the reasoning.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UnknownAction } from '@reduxjs/toolkit'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, sseSubagentSpawn } from '../store/chatSlice'
import { streamFlush } from '../store/streamFlush'

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
const CARD = 'a1b2c3d4'

const card = () => globalStore.getState().chat.subagents[CARD]
const thinking = (text: unknown, id: unknown = CARD) => ({ type: 'subagent_thinking', data: { slot: SLOT, id, text } })
const chunk = (text: string) => ({ type: 'subagent_chunk', data: { slot: SLOT, id: CARD, text } })

describe('subagent_thinking buffering', () => {
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
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

  function runFrames() {
    const pending = rafQueue
    rafQueue = []
    act(() => { pending.forEach(cb => cb(performance.now())) })
  }

  /** Mount the socket, then seed the card (the socket's open clears subagents). */
  function mount() {
    const dispatched: UnknownAction[] = []
    const real = globalStore.dispatch
    vi.spyOn(globalStore, 'dispatch').mockImplementation(((action: UnknownAction) => {
      dispatched.push(action)
      return real(action)
    }) as typeof globalStore.dispatch)
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(SLOT))
    globalStore.dispatch(sseSubagentSpawn({ slot: SLOT, id: CARD, task: 'solve the puzzle', agent: 'general-purpose' }))
    return { ws, dispatched }
  }

  it('lands a card\'s reasoning and text as one flush item, kept apart', () => {
    const { ws, dispatched } = mount()
    act(() => {
      ws.simulateMessage(thinking('Dave is in seat 1. '))
      ws.simulateMessage(chunk('Alice'))
      ws.simulateMessage(thinking('Eve sits in seat 2.'))
    })
    expect(card().thinking).toBeUndefined()
    runFrames()
    const flushes = dispatched.filter(a => streamFlush.match(a) && a.payload.subagents.length > 0)
    expect(flushes).toHaveLength(1)
    expect(streamFlush.match(flushes[0]) && flushes[0].payload.subagents).toEqual([
      { id: CARD, slot: SLOT, text: 'Alice', thinking: 'Dave is in seat 1. Eve sits in seat 2.' },
    ])
    expect(card().thinking).toBe('Dave is in seat 1. Eve sits in seat 2.')
    expect(card().streaming).toBe('Alice')
  })

  it('lands the buffered reasoning before a done frame', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage(thinking('last thought'))
      ws.simulateMessage({ type: 'subagent_done', data: { slot: SLOT, id: CARD, elapsed: 1, outcome: 'completed' } })
    })
    expect(card().thinking).toBe('last thought')
    expect(card().status).toBe('done')
  })

  it('keeps the buffered reasoning across a snapshot of the card', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage(thinking('kept'))
      ws.simulateMessage(chunk('dropped, the snapshot holds it'))
      ws.simulateMessage({
        type: 'subagent_snapshot',
        data: { slot: SLOT, id: CARD, task: 'solve the puzzle', agent: 'general-purpose', streaming: 'snap', last_tool: '', started: 1 },
      })
    })
    runFrames()
    expect(card().thinking).toBe('kept')
    expect(card().streaming).toBe('snap')
  })

  it('lands the buffered reasoning on reconnect and drops the text', () => {
    // Fake only the reconnect backoff: the hand-driven rafQueue keeps the
    // buffer unflushed, as a hidden tab's suspended frame does.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const { ws } = mount()
      act(() => {
        ws.simulateMessage(thinking('unflushed reasoning'))
        ws.simulateMessage(chunk('dropped, the refetch recovers it'))
      })
      act(() => { ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(2000) })
      act(() => { WS_INSTANCES[1].simulateOpen() })
      runFrames()
      expect(card().thinking).toBe('unflushed reasoning')
      expect(card().streaming).toBe('')
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores a frame with no text or no id', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage(thinking(''))
      ws.simulateMessage(thinking(undefined))
      ws.simulateMessage(thinking('orphan', ''))
    })
    runFrames()
    expect(card().thinking).toBeUndefined()
    expect(Object.keys(globalStore.getState().chat.subagents)).toEqual([CARD])
  })
})

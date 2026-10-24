/**
 * Stream traffic for a session that is not open defers its store notification
 * to one per frame (lib/streamBatch.ts): the reducer runs at
 * dispatch, so state is current at once, but subscribers hear about it on the
 * next frame. The open session's traffic still notifies at dispatch, so its
 * transcript renders on the frame its text arrives.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SHOULD_AUTOBATCH } from '@reduxjs/toolkit'
import { useWebSocket } from '../hooks/useWebSocket'
import { autoBatched, batchedUnlessOpen } from '../lib/streamBatch'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, setSlotStatusDetail } from '../store/chatSlice'

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

const OPEN = 'slot-open'
const BACKGROUND = 'slot-background'

describe('autoBatched / batchedUnlessOpen', () => {
  afterEach(() => { globalStore.dispatch(setActiveSlot(null)) })

  it('tags the action and keeps its payload and existing meta', () => {
    const tagged = autoBatched({ type: 'x/y', payload: { slot: 'a' }, meta: { keep: 1 } })
    expect(tagged).toEqual({ type: 'x/y', payload: { slot: 'a' }, meta: { keep: 1, [SHOULD_AUTOBATCH]: true } })
  })

  it('leaves the open slot and a slotless action untagged', () => {
    globalStore.dispatch(setActiveSlot(OPEN))
    const action = setSlotStatusDetail({ slot: OPEN, kind: 'streaming', ts: 1 })
    expect(batchedUnlessOpen(OPEN, action)).toBe(action)
    expect(batchedUnlessOpen(undefined, action)).toBe(action)
    expect(batchedUnlessOpen(BACKGROUND, action).meta).toEqual({ [SHOULD_AUTOBATCH]: true })
  })
})

describe('useWebSocket defers notifications for background stream traffic', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]
  let notifications: number
  let unsubscribe: () => void

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // The stream buffers' frames are hand-run by runFrames(). The store's own
    // deferred notification captured the real scheduler when it was created,
    // so it is not held in this queue.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    unsubscribe?.()
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, {
        store: globalStore,
        children: createElement(QueryClientProvider, { client: queryClient }, children),
      })
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(OPEN))
    notifications = 0
    unsubscribe = globalStore.subscribe(() => { notifications++ })
    return ws
  }

  const runFrames = () => {
    act(() => {
      while (rafQueue.length) rafQueue.splice(0).forEach(cb => cb(performance.now()))
    })
  }

  const chunk = (slot: string, content: string, seq: number) => ({ type: 'chat_chunk', data: { slot, content, seq } })
  const statusKind = (slot: string) => globalStore.getState().chat.slotStatusDetail[slot]?.kind

  it('a background chunk updates state at once and notifies later', async () => {
    const ws = mount()
    act(() => {
      ws.simulateMessage(chunk(BACKGROUND, 'one ', 1))
      ws.simulateMessage(chunk(BACKGROUND, 'two', 2))
    })
    runFrames()
    expect(statusKind(BACKGROUND)).toBe('streaming')
    expect(globalStore.getState().chat.slotMessages[BACKGROUND]?.some(m => m.content === 'one two')).toBe(true)
    expect(notifications).toBe(0)
    // The deferred notification arrives on its own. The mounted hook's own
    // startup requests may dispatch too, so this counts at least one.
    await vi.waitFor(() => expect(notifications).toBeGreaterThanOrEqual(1), { timeout: 2000, interval: 5 })
  })

  it('an open-slot chunk notifies once, at the flush, with its status', () => {
    const ws = mount()
    act(() => { ws.simulateMessage(chunk(OPEN, 'hello', 1)) })
    expect(notifications).toBe(0)
    runFrames()
    // The text and the streaming status transition land in one notification.
    expect(notifications).toBe(1)
    expect(statusKind(OPEN)).toBe('streaming')
    expect(globalStore.getState().chat.messages.find(m => m.role === 'streaming')?.content).toBe('hello')
  })

  it('one frame lands every streaming slot in one notification', () => {
    const ws = mount()
    act(() => {
      ws.simulateMessage(chunk(BACKGROUND, 'bg', 1))
      ws.simulateMessage(chunk(OPEN, 'fg', 1))
    })
    runFrames()
    expect(notifications).toBe(1)
    expect(statusKind(BACKGROUND)).toBe('streaming')
    expect(statusKind(OPEN)).toBe('streaming')
  })

  it('an open-slot flush delivers held background changes with it', () => {
    const ws = mount()
    act(() => { ws.simulateMessage(chunk(BACKGROUND, 'bg', 1)) })
    runFrames()
    expect(notifications).toBe(0)
    let seen: string | undefined
    const unsub = globalStore.subscribe(() => { seen ??= statusKind(BACKGROUND) })
    act(() => { ws.simulateMessage(chunk(OPEN, 'fg', 1)) })
    runFrames()
    unsub()
    expect(notifications).toBe(1)
    expect(seen).toBe('streaming')
  })
})

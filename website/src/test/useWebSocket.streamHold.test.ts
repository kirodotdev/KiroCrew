/**
 * Streaming flushes sit out the drawer slide.
 *
 * The drawer slide runs on the compositor; this hold covers the main-thread work
 * that remains during it — the panel's own mount, tool events, subagent status
 * pushes — and the main-thread drag path. While a hold is open the per-frame
 * flush pipelines in useWebSocket sit out, and the burst lands as one flush when
 * it lapses. Nothing is dropped — the pipelines already buffer between flushes.
 *
 * Pinned here:
 *  - a chat_chunk arriving under a hold does NOT reach the store within the
 *    frame, and DOES land (whole buffer, one flush) once the hold lapses;
 *  - animateDrawer opens a hold sized to its own duration and releases it on
 *    completion;
 *  - a locked drag keeps a rolling hold alive;
 *  - a hold is a deadline, never a lock (caps at HOLD_MAX, releasable early).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { motionValue } from 'framer-motion'
import { useWebSocket } from '../hooks/useWebSocket'
import { store } from '../store'
import { setActiveSlot, clearMessages, clearSlotState } from '../store/chatSlice'
import { holdStreamingFlushes, releaseStreamingFlushes, streamingFlushHoldMs } from '../lib/streamHold'
import { animateDrawer, useDrawerSwipe } from '../hooks/useDrawerSwipe'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
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
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

const streamedText = () =>
  store.getState().chat.messages.filter(m => m.role === 'streaming').map(m => m.content).join('')

function renderWs() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    createElement(Provider, { store }, createElement(QueryClientProvider, { client: queryClient }, children))
  const hook = renderHook(() => useWebSocket(), { wrapper })
  const ws = WS_INSTANCES[WS_INSTANCES.length - 1]
  act(() => ws.simulateOpen())
  return { hook, ws }
}

describe('streaming flushes vs the drawer slide', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    releaseStreamingFlushes()
    vi.stubGlobal('WebSocket', MockWebSocket)
    // rAF must run the flush for the un-held baseline half of the test.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(performance.now()), 16) as unknown as number)
    vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id))
    store.dispatch(clearSlotState())
    store.dispatch(clearMessages())
    store.dispatch(setActiveSlot('slot-1'))
  })
  afterEach(() => {
    releaseStreamingFlushes()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('defers a chunk flush while held, then lands the whole burst as one flush', async () => {
    vi.useFakeTimers()
    const { hook, ws } = renderWs()

    holdStreamingFlushes(400)
    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'slot-1', content: 'hello ' } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'slot-1', content: 'world' } })
    })
    // A frame passes; the flush is deferred, so nothing has reached the store.
    await act(async () => { vi.advanceTimersByTime(50) })
    expect(streamedText()).toBe('')

    // The hold lapses; the deferred timer fires and the buffer lands whole.
    await act(async () => { vi.advanceTimersByTime(450) })
    expect(streamedText()).toBe('hello world')
    hook.unmount()
  })

  it('flushes on the next frame when nothing holds', async () => {
    vi.useFakeTimers()
    const { hook, ws } = renderWs()
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'slot-1', content: 'now' } }) })
    await act(async () => { vi.advanceTimersByTime(40) })
    expect(streamedText()).toBe('now')
    hook.unmount()
  })

  it('animateDrawer holds for its own duration and releases on arrival', async () => {
    expect(streamingFlushHoldMs()).toBe(0)
    const done = vi.fn()
    animateDrawer(motionValue(-390), -390, done) // zero distance: arrives immediately
    // The hold opens with the animation. Not asserted against a fixed number
    // here: a settle's duration scales with the distance it has to cover, and
    // this one has none, so it takes the floor — what matters is that the hold
    // is open at all and that arrival closes it.
    expect(streamingFlushHoldMs()).toBeGreaterThan(0)
    // …and completion releases it, so a finished slide leaves no latency behind.
    await vi.waitFor(() => expect(done).toHaveBeenCalled())
    expect(streamingFlushHoldMs()).toBe(0)

    // A real distance gets a real duration, and the hold covers it.
    const stop = animateDrawer(motionValue(-390), 0)
    expect(streamingFlushHoldMs()).toBeGreaterThan(300)
    stop()
    // A stopped tween never completes: the deadline is the backstop.
    expect(streamingFlushHoldMs()).toBeGreaterThan(0)
    expect(streamingFlushHoldMs()).toBeLessThanOrEqual(1_000)
  })

  it('a locked drag keeps a rolling hold alive', () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const x = motionValue(0)
    const touch = (type: string, clientX: number) => {
      const t = { clientX, clientY: 0 } as Touch
      const init: TouchEventInit = { bubbles: true }
      if (type === 'touchstart' || type === 'touchmove') init.touches = [t]
      if (type === 'touchend') init.changedTouches = [t]
      return new TouchEvent(type, init)
    }
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 400 })
    // A STABLE ref object. Inlining `{ current: el }` in the render callback
    // hands the effect a new dep every render — and the lock's own setDragging
    // re-render then re-binds the listeners, whose cleanup resets the gesture.
    const ref = { current: el }
    const hook = renderHook(() => useDrawerSwipe(ref, {
      enabled: true, open: true, x, onGestureOpen: () => {}, onSettle: () => {},
    }))
    act(() => { el.dispatchEvent(touch('touchstart', 200)) })
    act(() => { el.dispatchEvent(touch('touchmove', 150)) }) // locks (leftward close)
    releaseStreamingFlushes() // isolate: the NEXT sample must re-arm it
    act(() => { el.dispatchEvent(touch('touchmove', 140)) })
    expect(streamingFlushHoldMs()).toBeGreaterThan(0)
    expect(streamingFlushHoldMs()).toBeLessThanOrEqual(250)
    hook.unmount()
    el.remove()
  })

  it('holds are deadlines: capped, extend-only, releasable early', () => {
    holdStreamingFlushes(99_999)
    expect(streamingFlushHoldMs()).toBeLessThanOrEqual(1_000)
    const before = streamingFlushHoldMs()
    holdStreamingFlushes(10) // shorter hold must not SHORTEN the active one
    expect(streamingFlushHoldMs()).toBeGreaterThanOrEqual(before - 5)
    releaseStreamingFlushes()
    expect(streamingFlushHoldMs()).toBe(0)
  })
})

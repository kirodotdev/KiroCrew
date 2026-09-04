/**
 * `chat_message` must not dispatch `touchSlotActivity` once per event.
 *
 * The reducer writes `slot.last_ts` in place, which changes the `slots` array
 * identity every dispatch, so every whole-array subscriber (ChatSidebar) re-renders
 * once per event. A streaming burst therefore costs N sidebar renders to arrive at
 * one final timestamp. These tests pin that the bumps coalesce to one dispatch per
 * frame per slot, that the coalesced value matches what the un-buffered path would
 * have left behind, and that nothing is lost or fires late on teardown.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
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
  static CLOSED = 3
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

  /** A real socket is CLOSED by the time onclose runs; the gate under test reads readyState. */
  simulateClose() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close'))
  }
}

const slotA: ChatSlot = { key: 'slot-1', title: 'A', messages: 0, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }
const slotB: ChatSlot = { key: 'slot-2', title: 'B', messages: 0, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }

/** Deliberately the SINGLETON store: useWebSocket dispatches via useAppDispatch()
 *  but reads state off the imported singleton, so a separate Provider store would
 *  let reads and writes diverge. */
function seedStore() {
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
  globalStore.dispatch(sseSlots([{ ...slotA }, { ...slotB }]))
  return globalStore
}

const lastTs = (key: string) =>
  globalStore.getState().dashboard.slots.find(s => s.key === key)?.last_ts

/** The key the sidebar ORDERS by — moves only on a settled event. */
const lastTurnTs = (key: string) =>
  globalStore.getState().dashboard.slots.find(s => s.key === key)?.last_turn_ts

const msg = (slot: string, ts?: string) => ({
  type: 'chat_message',
  data: { slot, role: 'assistant', content: 'x', ts },
})

/** An inbound prompt: the one kind of row that settles a session's rank. */
const prompt = (slot: string, ts?: string) => ({
  type: 'chat_message',
  data: { slot, role: 'user', content: 'go', ts },
})

describe('useWebSocket slot-activity coalescing', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    // Leave the on-open slots refetch pending forever: fetchSlots.fulfilled assigns
    // state.slots wholesale, which would replace the seeded fixtures mid-test.
    vi.mocked(api.chatSlots).mockReturnValue(new Promise<ChatSlot[]>(() => {}))
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Hand-driven frames: nothing flushes until runFrames() is called, which is
    // what lets a burst be observed mid-flight rather than after the fact.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
    globalStore.dispatch(sseSlots([]))
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

  it('does not bump last_ts synchronously on each event', () => {
    seedStore()
    const { ws } = mount()

    act(() => { ws.simulateMessage(msg('slot-1', '2026-08-10T10:00:00Z')) })

    // Un-coalesced code writes last_ts here; buffered code has not flushed yet.
    expect(lastTs('slot-1')).toBeUndefined()

    runFrames()
    expect(lastTs('slot-1')).toBe('2026-08-10T10:00:00Z')
  })

  it('collapses a burst into one dispatch per slot and keeps the last ts seen', () => {
    const store = seedStore()
    const dispatchSpy = vi.spyOn(store, 'dispatch')
    const { ws } = mount()
    dispatchSpy.mockClear()

    act(() => {
      for (let i = 0; i < 30; i++) ws.simulateMessage(msg('slot-1', `2026-08-10T10:00:${String(i).padStart(2, '0')}Z`))
    })
    runFrames()

    const touches = dispatchSpy.mock.calls.filter(
      ([a]) => (a as { type?: string })?.type === 'dashboard/touchSlotActivity')
    expect(touches).toHaveLength(1)
    // Last-seen wins, matching the un-buffered path's terminal state.
    expect(lastTs('slot-1')).toBe('2026-08-10T10:00:29Z')
    dispatchSpy.mockRestore()
  })

  it('keeps the slots array reference stable across a burst until the frame lands', () => {
    const store = seedStore()
    const { ws } = mount()
    const before = store.getState().dashboard.slots

    act(() => {
      for (let i = 0; i < 12; i++) ws.simulateMessage(msg('slot-1', '2026-08-10T10:00:00Z'))
    })
    expect(store.getState().dashboard.slots).toBe(before)

    runFrames()
    expect(store.getState().dashboard.slots).not.toBe(before)
  })

  it('keeps per-slot bumps independent within one frame', () => {
    seedStore()
    const { ws } = mount()

    act(() => {
      ws.simulateMessage(msg('slot-1', '2026-08-10T10:00:01Z'))
      ws.simulateMessage(msg('slot-2', '2026-08-10T10:00:02Z'))
      ws.simulateMessage(msg('slot-1', '2026-08-10T10:00:03Z'))
    })
    runFrames()

    expect(lastTs('slot-1')).toBe('2026-08-10T10:00:03Z')
    expect(lastTs('slot-2')).toBe('2026-08-10T10:00:02Z')
  })

  it('falls back to arrival time, not frame time, when the event carries no ts', async () => {
    seedStore()
    const { ws } = mount()

    const beforeArrival = new Date().toISOString()
    act(() => { ws.simulateMessage(msg('slot-1')) })
    const afterArrival = new Date().toISOString()
    // Real delay so frame time is strictly later than arrival: a fallback computed
    // at flush time would land after afterArrival and fail the upper bound.
    await new Promise(r => globalThis.setTimeout(r, 25))
    runFrames()

    const ts = lastTs('slot-1')
    expect(ts).toBeDefined()
    expect(ts! >= beforeArrival).toBe(true)
    expect(ts! <= afterArrival).toBe(true)
  })

  it('flushes a pending bump on unmount instead of dropping it', () => {
    seedStore()
    const { ws, hook } = mount()

    act(() => { ws.simulateMessage(msg('slot-1', '2026-08-10T11:00:00Z')) })
    expect(lastTs('slot-1')).toBeUndefined()

    act(() => { hook.unmount() })
    expect(lastTs('slot-1')).toBe('2026-08-10T11:00:00Z')
  })

  it('does not dispatch from a frame that fires after unmount', () => {
    const store = seedStore()
    const { ws, hook } = mount()

    act(() => { ws.simulateMessage(msg('slot-1', '2026-08-10T12:00:00Z')) })
    act(() => { hook.unmount() })

    const dispatchSpy = vi.spyOn(store, 'dispatch')
    runFrames()  // the frame the unmount flush should have cancelled
    const touches = dispatchSpy.mock.calls.filter(
      ([a]) => (a as { type?: string })?.type === 'dashboard/touchSlotActivity')
    expect(touches).toHaveLength(0)
    dispatchSpy.mockRestore()
  })

  it('drops pending bumps on reconnect, since the slots refetch is authoritative', () => {
    // Only the reconnect backoff timer — faking rAF too would clobber the
    // hand-driven frame stub and flush the buffer before the reconnect clear.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      seedStore()
      const { ws } = mount()

      act(() => { ws.simulateMessage(msg('slot-1', '2026-08-10T13:00:00Z')) })
      act(() => { ws.simulateClose() })
      act(() => { vi.advanceTimersByTime(5000) })  // reconnect backoff
      const reconnected = WS_INSTANCES[WS_INSTANCES.length - 1]
      expect(reconnected).not.toBe(ws)
      act(() => { reconnected.simulateOpen() })
      runFrames()

      expect(lastTs('slot-1')).toBeUndefined()
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops a frame that fires during reconnect backoff, before the socket reopens', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      seedStore()
      const { ws } = mount()

      act(() => { ws.simulateMessage(msg('slot-1', '2026-08-10T15:00:00Z')) })
      // Socket is down and the backoff timer has NOT fired, so the on-open clear has
      // not run yet. A frame landing in this window must drop rather than dispatch.
      act(() => { ws.simulateClose() })
      runFrames()

      expect(lastTs('slot-1')).toBeUndefined()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not overwrite a newer authoritative snapshot that lands before the flush', () => {
    seedStore()
    const { ws } = mount()

    // No ts, so the bump falls back to arrival time...
    act(() => { ws.simulateMessage(msg('slot-1')) })
    // ...then an authoritative slots push lands a strictly newer last_ts, still
    // before the frame runs. The deferred flush must not walk it backwards.
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [{ ...slotA, last_ts: '2099-01-01T00:00:00Z' }, { ...slotB }],
      })
    })
    expect(lastTs('slot-1')).toBe('2099-01-01T00:00:00Z')

    runFrames()

    expect(lastTs('slot-1')).toBe('2099-01-01T00:00:00Z')
  })

  it('still applies a bump that is newer than the snapshot it lands after', () => {
    seedStore()
    const { ws } = mount()

    act(() => { ws.simulateMessage(msg('slot-1')) })
    // Snapshot carries an OLD last_ts, so the buffered arrival time is genuinely
    // newer and must win — the guard suppresses regressions, not every write.
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [{ ...slotA, last_ts: '2000-01-01T00:00:00Z' }, { ...slotB }],
      })
    })
    runFrames()

    const ts = lastTs('slot-1')
    expect(ts).toBeDefined()
    expect(ts).not.toBe('2000-01-01T00:00:00Z')
    expect(Date.parse(ts!)).toBeGreaterThan(Date.parse('2000-01-01T00:00:00Z'))
  })

  it('leaves the ordering key untouched for a burst of agent output', () => {
    // The churn fix: a streaming turn moves last_ts many times and must not
    // re-rank the sidebar even once.
    seedStore()
    const { ws } = mount()

    act(() => {
      for (let i = 0; i < 5; i++) ws.simulateMessage(msg('slot-1', `2026-08-10T16:00:0${i}Z`))
    })
    runFrames()

    expect(lastTs('slot-1')).toBe('2026-08-10T16:00:04Z')
    expect(lastTurnTs('slot-1')).toBeUndefined()
  })

  it('settles the ordering key when the burst contains an inbound prompt', () => {
    // A prompt anywhere in the burst settles the coalesced flush — the buffer
    // keeps one entry per slot, so the settling role must survive being followed
    // by the agent output it triggered.
    seedStore()
    const { ws } = mount()

    act(() => {
      ws.simulateMessage(prompt('slot-1', '2026-08-10T17:00:00Z'))
      ws.simulateMessage(msg('slot-1', '2026-08-10T17:00:01Z'))
    })
    runFrames()

    expect(lastTs('slot-1')).toBe('2026-08-10T17:00:01Z')
    expect(lastTurnTs('slot-1')).toBe('2026-08-10T17:00:01Z')
  })

  it('settles the ordering key for a send that queues behind a busy turn', () => {
    // A queued send emits queue_push, not chat_message: without this the session
    // the user just typed into would stay where it was until the queue popped.
    seedStore()
    const { ws } = mount()

    act(() => {
      ws.simulateMessage({
        type: 'queue_push',
        data: { slot: 'slot-1', content: 'later', ts: '2026-08-10T18:00:00Z', queue_id: 'q1' },
      })
    })
    runFrames()

    expect(lastTurnTs('slot-1')).toBe('2026-08-10T18:00:00Z')
  })

  it('settles the ordering key for a mid-turn steer, like a queued send', () => {
    // Steering is the other way to type into a busy session; without this the
    // re-rank waits for the next slots push while a queued send re-ranks at once.
    seedStore()
    const { ws } = mount()

    act(() => {
      ws.simulateMessage({
        type: 'steer_push',
        data: { slot: 'slot-1', content: 'actually do X', ts: '2026-08-10T20:00:00Z' },
      })
    })
    runFrames()

    expect(lastTurnTs('slot-1')).toBe('2026-08-10T20:00:00Z')
  })

  it('keeps the newest ts when a settling event arrives before older output', () => {
    // The buffer holds one entry per slot, so an out-of-order pair must not walk
    // the timestamp backwards while still settling the rank.
    seedStore()
    const { ws } = mount()

    act(() => {
      ws.simulateMessage(prompt('slot-1', '2026-08-10T19:00:05Z'))
      ws.simulateMessage(msg('slot-1', '2026-08-10T19:00:01Z'))
    })
    runFrames()

    expect(lastTs('slot-1')).toBe('2026-08-10T19:00:05Z')
    expect(lastTurnTs('slot-1')).toBe('2026-08-10T19:00:05Z')
  })
})

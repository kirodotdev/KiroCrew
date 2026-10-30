/**
 * A `slots` frame notifies store subscribers once.
 *
 * Every store notification re-runs every mounted `useAppSelector`, and the
 * frame arrives up to several times a second while sessions work. Its side
 * dispatches (queued-depth reconcile, list holds, partial flag, yolo, channel
 * trust) ride the one notification `sseSlots` sends; a frame that repeats the
 * last one, which stops before `sseSlots`, sends one deferred notification.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import { store as globalStore } from '../store'
import { useWebSocket } from '../hooks/useWebSocket'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
    credentialRedaction: vi.fn().mockResolvedValue({ enabled: true, changed_at: '' }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

const TS = '2026-09-01T00:00:00.000Z'
const SLOTS = [
  { key: 'slot-a', title: 'Active', last_ts: TS, running: false, subagents_queued: 0 },
  { key: 'slot-b', title: 'Background', last_ts: TS, running: false, subagents_queued: 2 },
] as unknown as ChatSlot[]

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static instances: MockWebSocket[] = []
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()
  constructor(public url: string) { MockWebSocket.instances.push(this) }
  open() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  frame(frame: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(frame) })) }
}

let rafQueue: FrameRequestCallback[] = []
function drainFrames() {
  const pending = rafQueue
  rafQueue = []
  for (const cb of pending) cb(0)
}

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
    middleware: getDefault => getDefault({ serializableCheck: false, immutableCheck: false }),
    // The deferred notification must wait on the stubbed frame source, which
    // the enhancer captures when the store is made.
    enhancers: getDefault => getDefault({ autoBatch: { type: 'callback', queueNotification: cb => { rafQueue.push(() => cb()) } } }),
  })
}

let testStore: ReturnType<typeof makeStore>

beforeEach(() => {
  MockWebSocket.instances.length = 0
  rafQueue = []
  sessionStorage.clear()
  localStorage.clear()
  testStore = makeStore()
  vi.stubGlobal('WebSocket', MockWebSocket)
  // The hook reads the singleton store; production passes the singleton as the
  // Provider store, so point its reads at the store under test.
  vi.spyOn(globalStore, 'getState').mockImplementation(() => testStore.getState())
  vi.spyOn(globalStore, 'subscribe').mockImplementation(listener => testStore.subscribe(listener))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function mountOpen() {
  testStore.dispatch(sseSlots(SLOTS))
  testStore.dispatch(setActiveSlot('slot-a'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  renderHook(() => useWebSocket(), {
    wrapper: ({ children }) => createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)),
  })
  const ws = MockWebSocket.instances[0]
  await act(async () => { ws.open() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  act(() => { drainFrames() })
  return ws
}

function countNotifications(): { read(): number } {
  let n = 0
  testStore.subscribe(() => { n += 1 })
  return { read: () => n }
}

describe('slots frame store notifications', () => {
  it('a changed frame notifies once and applies every side channel', async () => {
    const ws = await mountOpen()
    const notified = countNotifications()
    act(() => {
      ws.frame({
        type: 'slots',
        data: SLOTS.map(s => s.key === 'slot-b' ? { ...s, title: 'Renamed', subagents_queued: 3 } : s),
        yolo: true,
        channelTrusted: true,
        slotsHeldKeys: ['slot-c'],
        slotsHoldChannels: true,
      })
    })
    expect(notified.read()).toBe(1)
    act(() => { drainFrames() })
    expect(notified.read()).toBe(1)
    const state = testStore.getState()
    expect(state.dashboard.slots.find(s => s.key === 'slot-b')?.title).toBe('Renamed')
    expect(state.dashboard.approvalMode).toBe('yolo')
    expect(state.dashboard.channelTrusted).toBe(true)
    expect(state.dashboard.slotsHeldKeys).toEqual(['slot-c'])
    expect(state.dashboard.slotsHoldChannels).toBe(true)
    expect(state.chat.subagentQueued['slot-b']).toBe(3)
  })

  it('a repeated frame notifies once, on the next animation frame', async () => {
    const ws = await mountOpen()
    const frame = { type: 'slots', data: SLOTS, yolo: false, channelTrusted: false }
    act(() => { ws.frame(frame) })
    act(() => { drainFrames() })
    // A count moved between the two pushes: the repeat must still correct it.
    act(() => { ws.frame({ type: 'subagent_queued', data: { slot: 'slot-b', queued: 5 } }) })
    act(() => { drainFrames() })
    const notified = countNotifications()
    act(() => { ws.frame(frame) })
    expect(testStore.getState().chat.subagentQueued['slot-b']).toBe(2)
    expect(notified.read()).toBe(0)
    act(() => { drainFrames() })
    expect(notified.read()).toBe(1)
  })
})

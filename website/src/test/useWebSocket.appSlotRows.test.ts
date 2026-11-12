/**
 * The slot list reaches the app embed's change signal.
 *
 * A message can enter a slot with no slot-scoped frame, so an embed that
 * waited only for those frames kept its stale read until the next backstop
 * poll. The `slots` frame that follows the send restates the slot's message
 * count and running flag, and the frame router hands each list it applies to
 * `noteAppSlotRows` (utils/appSlotPush.ts), which signals a watched slot whose
 * row moved. A `slot_patch` row is handed over too; it states only metadata.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import { store as globalStore } from '../store'
import { useWebSocket } from '../hooks/useWebSocket'
import { createAppSlotPush, resetAppSlotPushForTests } from '../utils/appSlotPush'
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

const APP = 'spec-builder'
const TS = '2026-09-01T00:00:00.000Z'
const IDLE = [
  { key: 'app-slot', title: 'Embed', last_ts: TS, app: APP, messages: 0, running: false, queue_depth: 0 },
  { key: 'other', title: 'Other', last_ts: TS, messages: 4, running: false, queue_depth: 0 },
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

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
    middleware: getDefault => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

let testStore: ReturnType<typeof makeStore>

beforeEach(() => {
  MockWebSocket.instances.length = 0
  sessionStorage.clear()
  localStorage.clear()
  testStore = makeStore()
  vi.stubGlobal('WebSocket', MockWebSocket)
  // The hook and the push read the singleton store; production passes the
  // singleton as the Provider store, so point its reads at the store under test.
  vi.spyOn(globalStore, 'getState').mockImplementation(() => testStore.getState())
  vi.spyOn(globalStore, 'subscribe').mockImplementation(listener => testStore.subscribe(listener))
  resetAppSlotPushForTests()
})

afterEach(() => {
  resetAppSlotPushForTests()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function mountOpen() {
  testStore.dispatch(sseSlots(IDLE))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  renderHook(() => useWebSocket(), {
    wrapper: ({ children }) => createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)),
  })
  const ws = MockWebSocket.instances[0]
  await act(async () => { ws.open() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return ws
}

describe('slot list rows signal app embeds', () => {
  it('a slots frame that adds a row and starts a turn signals the watching embed once', async () => {
    const ws = await mountOpen()
    const onChange = vi.fn()
    createAppSlotPush(APP).watch('app-slot', { onChange, onConnection: vi.fn() })
    const busy = IDLE.map(s => s.key === 'app-slot' ? { ...s, messages: 1, running: true } : s)
    act(() => { ws.frame({ type: 'slots', data: busy }) })
    expect(onChange).toHaveBeenCalledTimes(1)
    // Another slot moving, or the same list again, is not a change to this slot.
    const otherMoved = busy.map(s => s.key === 'other' ? { ...s, messages: 5 } : s)
    act(() => { ws.frame({ type: 'slots', data: otherMoved }) })
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('a slot_patch row reaches the signal, which moves only on a watched field', async () => {
    const ws = await mountOpen()
    const onChange = vi.fn()
    createAppSlotPush(APP).watch('app-slot', { onChange, onConnection: vi.fn() })
    act(() => { ws.frame({ type: 'slot_patch', data: { slots: [{ key: 'app-slot', pinned: true }] } }) })
    expect(onChange).not.toHaveBeenCalled()
    act(() => { ws.frame({ type: 'slot_patch', data: { slots: [{ key: 'app-slot', running: true }] } }) })
    expect(onChange).toHaveBeenCalledTimes(1)
  })
})

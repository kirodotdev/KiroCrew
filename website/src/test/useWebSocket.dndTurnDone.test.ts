/**
 * The gateway's Do Not Disturb silences a routine finished turn's chime and
 * background-finished toast on every session, read from the same cached query
 * the Settings select and the bell badge read. A turn that waits on the user or
 * ended in an error still sounds, and the unread badge still marks. Driven over
 * the dashboard socket so the chat_done gate reads it the way the app wires it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { postNativeNotification } = vi.hoisted(() => ({ postNativeNotification: vi.fn() }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    notificationDnd: vi.fn().mockResolvedValue({ until: null }),
    notificationChannels: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))
// The opt-in and away checks are not under test: let every eligible
// completion reach the toast so the spy records the attention gate alone.
vi.mock('../hooks/chatCompleteNotify', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/chatCompleteNotify')>()),
  shouldNotifyOnChatComplete: (opts: { slot?: string | null; reconnecting: boolean }) => !!opts.slot && !opts.reconnecting,
}))
vi.mock('../lib/nativeNotify', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/nativeNotify')>()),
  postNativeNotification,
}))

import { api } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearMessages } from '../store/chatSlice'
import { markSlotRead, sseSlots } from '../store/dashboardSlice'
import { _resetSlotReadRelayForTest } from '../lib/slotReadRelay'
import { MC_NOTIFICATION_EVENT, TURN_DONE_KIND } from '../hooks/notificationEvent'
import { _resetTurnErrorsForTest } from '../hooks/turnError'
import { NOTIFICATION_DND_KEY } from '../hooks/useNotificationDnd'

// A fixed clock: only Date is faked, so the socket hooks keep real timers.
const NOW = new Date(2026, 10, 6, 21, 0).getTime()
const SLOT = 'slot-own'
const ON_SCREEN = 'slot-on-screen'
const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })
  constructor(public url: string) { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: unknown) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

describe('Do Not Disturb over the dashboard socket', () => {
  let chimes: string[] = []
  const onChime = (e: Event) => { chimes.push((e as CustomEvent<{ kind: string }>).detail.kind) }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    _resetSlotReadRelayForTest()
    _resetTurnErrorsForTest()
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    chimes = []
    window.addEventListener(MC_NOTIFICATION_EVENT, onChime)
    globalStore.dispatch(setActiveSlot(ON_SCREEN))
  })

  afterEach(() => {
    window.removeEventListener(MC_NOTIFICATION_EVENT, onChime)
    _resetSlotReadRelayForTest()
    _resetTurnErrorsForTest()
    vi.unstubAllGlobals()
    vi.useRealTimers()
    globalStore.dispatch(markSlotRead(SLOT))
    globalStore.dispatch(sseSlots([]))
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  /** Omit the end time to mount without any cached Do Not Disturb state. */
  function mount(untilSecs: number | null | undefined, qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })) {
    if (untilSecs !== undefined) qc.setQueryData(NOTIFICATION_DND_KEY, { until: untilSecs })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(Provider, { store: globalStore }, createElement(QueryClientProvider, { client: qc }, children))
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(sseSlots([
      { key: ON_SCREEN, messages: 1, running: false },
      { key: SLOT, messages: 1, running: true },
    ]))
    return ws
  }
  const turn = (ws: MockWebSocket, done: Record<string, unknown> = {}) => act(() => {
    ws.simulateMessage({ type: 'chat_message', data: { slot: SLOT, role: 'assistant', content: 'x', ts: '2026-11-05T00:00:01Z' } })
    ws.simulateMessage({ type: 'chat_done', data: { slot: SLOT, ts: '2026-11-05T00:00:05Z', continuing: false, ...done } })
  })
  const turnChimes = () => chimes.filter(k => k === TURN_DONE_KIND).length
  const IN_AN_HOUR = NOW / 1000 + 3600

  it('loads Do Not Disturb without dashboard chrome before a routine finish', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    expect(qc.getQueryData(NOTIFICATION_DND_KEY)).toBeUndefined()
    const response = Promise.resolve({ until: IN_AN_HOUR })
    vi.mocked(api.notificationDnd).mockReturnValueOnce(response)
    const ws = mount(undefined, qc)
    await act(async () => { await response })
    turn(ws)
    expect(turnChimes()).toBe(0)
    expect(postNativeNotification).not.toHaveBeenCalled()
    expect(qc.getQueryData(NOTIFICATION_DND_KEY)).toEqual({ until: IN_AN_HOUR })
    expect(api.notificationDnd).toHaveBeenCalledTimes(1)
    expect(globalStore.getState().dashboard.unreadSlots).toContain(SLOT)
  })

  it('a routine finish raises no turn-done chime while it runs, but still marks unread', () => {
    const ws = mount(IN_AN_HOUR)
    turn(ws)
    expect(turnChimes()).toBe(0)
    expect(globalStore.getState().dashboard.unreadSlots).toContain(SLOT)
  })

  it('a routine finish raises no background-finished toast while it runs', () => {
    const ws = mount(IN_AN_HOUR)
    turn(ws)
    expect(postNativeNotification).not.toHaveBeenCalled()
  })

  it('a turn that waits on the user still chimes and toasts', () => {
    const ws = mount(IN_AN_HOUR)
    turn(ws, { needs_input: true })
    expect(turnChimes()).toBe(1)
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
  })

  it('a finish chimes and toasts again once the end time has passed', () => {
    const ws = mount(NOW / 1000 - 1)
    turn(ws)
    expect(turnChimes()).toBe(1)
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
  })
})

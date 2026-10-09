/**
 * A session muted by its creator's "mute sessions it opens" rule stays quiet
 * when a turn finishes, but NOT when the turn ended in a terminal error row:
 * the worker has stopped, and a silenced failure stalls it unseen. Driven
 * over the dashboard socket so the chatStream record and the chat_done gate
 * are exercised together, the way the app wires them.
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

import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearMessages } from '../store/chatSlice'
import { markSlotRead, sseSlots } from '../store/dashboardSlice'
import { _resetSlotReadRelayForTest } from '../lib/slotReadRelay'
import { MC_NOTIFICATION_EVENT, TURN_DONE_KIND } from '../hooks/notificationEvent'
import { _resetTurnErrorsForTest, isTerminalErrorRow } from '../hooks/turnError'
import { UNREAD_ON_ATTENTION_KEY } from '../hooks/unreadOnAttention'

const CONDUCTOR = 'slot-conductor'
const WORKER = 'slot-worker'
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

describe('isTerminalErrorRow', () => {
  it('reads the role and the structural retry tag, never the text', () => {
    expect(isTerminalErrorRow({ role: 'error' })).toBe(true)
    expect(isTerminalErrorRow({ role: 'error', meta: { kind: 'transient_retry' } })).toBe(false)
    expect(isTerminalErrorRow({ role: 'error', kind: 'transient_retry' })).toBe(false)
    expect(isTerminalErrorRow({ role: 'assistant' })).toBe(false)
    expect(isTerminalErrorRow({ role: undefined })).toBe(false)
  })
})

describe('muted worker turn end over the dashboard socket', () => {
  let chimes: string[] = []
  const onChime = (e: Event) => { chimes.push((e as CustomEvent<{ kind: string }>).detail.kind) }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    _resetSlotReadRelayForTest()
    _resetTurnErrorsForTest()
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    chimes = []
    window.addEventListener(MC_NOTIFICATION_EVENT, onChime)
    globalStore.dispatch(setActiveSlot(CONDUCTOR))
  })

  afterEach(() => {
    window.removeEventListener(MC_NOTIFICATION_EVENT, onChime)
    _resetSlotReadRelayForTest()
    _resetTurnErrorsForTest()
    vi.unstubAllGlobals()
    globalStore.dispatch(markSlotRead(WORKER))
    globalStore.dispatch(sseSlots([]))
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount(muted: boolean) {
    const wrapper = ({ children }: { children: ReactNode }) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: qc }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(sseSlots([
      { key: CONDUCTOR, messages: 1, running: false, mutes_opened: muted },
      { key: WORKER, messages: 1, running: true, created_by: CONDUCTOR },
    ]))
    return ws
  }
  const send = (ws: MockWebSocket, frame: unknown) => act(() => { ws.simulateMessage(frame) })
  const row = (role: string, extra: Record<string, unknown> = {}) =>
    ({ type: 'chat_message', data: { slot: WORKER, role, content: 'x', ts: '2026-10-09T00:00:01Z', ...extra } })
  const done = { type: 'chat_done', data: { slot: WORKER, ts: '2026-10-09T00:00:05Z', continuing: false } }
  const unread = () => globalStore.getState().dashboard.unreadSlots
  const turnChimes = () => chimes.filter(k => k === TURN_DONE_KIND).length

  it('muted + terminal error: chime, toast and unread all fire', () => {
    const ws = mount(true)
    send(ws, row('error'))
    send(ws, done)
    expect(turnChimes()).toBe(1)
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
    expect(unread()).toContain(WORKER)
  })

  it('muted + terminal error: the error row alone badges the session', () => {
    const ws = mount(true)
    send(ws, row('error'))
    expect(unread()).toContain(WORKER)
  })

  it('muted + terminal error, "unread only when they need you" on: the finished turn badges', () => {
    // With the opt-in on, the error row itself badges nothing, so this pins
    // the chat_done half of the exemption on its own.
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount(true)
    send(ws, row('error'))
    expect(unread()).not.toContain(WORKER)
    send(ws, done)
    expect(unread()).toContain(WORKER)
  })

  it('muted + successful turn: no chime, toast or unread', () => {
    const ws = mount(true)
    send(ws, row('assistant'))
    send(ws, done)
    expect(turnChimes()).toBe(0)
    expect(postNativeNotification).not.toHaveBeenCalled()
    expect(unread()).not.toContain(WORKER)
  })

  it('muted + transient-retry row only: stays silent', () => {
    const ws = mount(true)
    send(ws, row('error', { meta: { kind: 'transient_retry', notice: 'transient_retrying' } }))
    send(ws, done)
    expect(turnChimes()).toBe(0)
    expect(postNativeNotification).not.toHaveBeenCalled()
    expect(unread()).not.toContain(WORKER)
  })

  it('muted: a failed turn does not carry into the next, successful one', () => {
    const ws = mount(true)
    send(ws, row('error'))
    send(ws, done)
    globalStore.dispatch(markSlotRead(WORKER))
    chimes = []
    postNativeNotification.mockClear()
    send(ws, row('assistant'))
    send(ws, { type: 'chat_done', data: { slot: WORKER, ts: '2026-10-09T00:00:09Z', continuing: false } })
    expect(turnChimes()).toBe(0)
    expect(postNativeNotification).not.toHaveBeenCalled()
    expect(unread()).not.toContain(WORKER)
  })

  it('unmuted: a successful turn and a failed turn both signal, as before', () => {
    const ws = mount(false)
    send(ws, row('assistant'))
    send(ws, done)
    expect(turnChimes()).toBe(1)
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
    expect(unread()).toContain(WORKER)
    globalStore.dispatch(markSlotRead(WORKER))
    send(ws, row('error'))
    send(ws, { type: 'chat_done', data: { slot: WORKER, ts: '2026-10-09T00:00:09Z', continuing: false } })
    expect(turnChimes()).toBe(2)
    expect(postNativeNotification).toHaveBeenCalledTimes(2)
    expect(unread()).toContain(WORKER)
  })
})

/**
 * A session a Bot dispatched (`mate_root` set) is the Bot's to read when a turn
 * finishes, as a Crew Mode crew member (`crew_root`) is its conductor's: no
 * turn-done chime for the person on a routine finish. A turn that paused on a
 * question or ended in a terminal error still sounds, because the worker has
 * stopped. A turn that stops for the user plays the approval sound, and only a
 * routine finish plays the turn sound. Driven over the dashboard socket, the
 * way the app wires it.
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
// The toast's opt-in and away checks are not under test: let every completion
// that passes the attention gate reach the toast, so the spy records that gate.
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
import { APPROVAL_KIND, MC_NOTIFICATION_EVENT, TURN_DONE_KIND } from '../hooks/notificationEvent'
import { _resetTurnErrorsForTest } from '../hooks/turnError'
import { __setFeaturePreviewLockKnownForTests } from '../utils/editionLocks'

const HUMAN = 'slot-human'
const CONDUCTOR = 'slot-conductor'
const CREW_WORKER = 'slot-crew-worker'
const MATE = 'member-bot.memory-bot'
const MATE_WORKER = 'slot-mate-worker'
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

describe('turn-end sounds for sessions a Bot dispatched', () => {
  let chimes: string[] = []
  const onChime = (e: Event) => { chimes.push((e as CustomEvent<{ kind: string }>).detail.kind) }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    _resetSlotReadRelayForTest()
    _resetTurnErrorsForTest()
    __setFeaturePreviewLockKnownForTests(true)
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    chimes = []
    window.addEventListener(MC_NOTIFICATION_EVENT, onChime)
    globalStore.dispatch(setActiveSlot(HUMAN))
  })

  afterEach(() => {
    window.removeEventListener(MC_NOTIFICATION_EVENT, onChime)
    _resetSlotReadRelayForTest()
    _resetTurnErrorsForTest()
    __setFeaturePreviewLockKnownForTests(false)
    vi.unstubAllGlobals()
    for (const key of [CREW_WORKER, MATE, MATE_WORKER]) globalStore.dispatch(markSlotRead(key))
    globalStore.dispatch(sseSlots([]))
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount() {
    const wrapper = ({ children }: { children: ReactNode }) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: qc }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(sseSlots([
      { key: HUMAN, messages: 1, running: false },
      { key: CONDUCTOR, messages: 1, running: false, crew_mode_root: true },
      { key: CREW_WORKER, messages: 1, running: true, created_by: CONDUCTOR, crew_root: CONDUCTOR },
      { key: MATE, messages: 1, running: false, mode: 'member' },
      { key: MATE_WORKER, messages: 1, running: true, created_by: MATE, mate_root: MATE },
    ]))
    return ws
  }
  const send = (ws: MockWebSocket, frame: unknown) => act(() => { ws.simulateMessage(frame) })
  const done = (slot: string, extra: Record<string, unknown> = {}) =>
    ({ type: 'chat_done', data: { slot, ts: '2026-11-27T00:00:05Z', continuing: false, ...extra } })
  const errorRow = (slot: string) =>
    ({ type: 'chat_message', data: { slot, role: 'error', content: 'x', ts: '2026-11-27T00:00:01Z' } })
  it('control: a Crew Mode crew member finishing a turn makes no sound and no toast', () => {
    const ws = mount()
    send(ws, done(CREW_WORKER))
    expect(chimes).toEqual([])
    expect(postNativeNotification).not.toHaveBeenCalled()
  })

  it('the person\'s own chat: a routine finish plays the turn sound and toasts', () => {
    const ws = mount()
    send(ws, done(HUMAN))
    expect(chimes).toEqual([TURN_DONE_KIND])
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
  })

  it('the person\'s own chat: a turn that stops on a question plays the approval sound', () => {
    const ws = mount()
    send(ws, done(HUMAN, { needs_input: true }))
    expect(chimes).toEqual([APPROVAL_KIND])
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
  })

  it('the person\'s own chat: a turn that ends in a terminal error plays the approval sound', () => {
    const ws = mount()
    send(ws, errorRow(HUMAN))
    send(ws, done(HUMAN))
    expect(chimes).toEqual([APPROVAL_KIND])
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
  })

  it('the Bot\'s own thread finishing a turn still plays the turn sound: it carries no mate_root', () => {
    const ws = mount()
    send(ws, done(MATE))
    expect(chimes).toEqual([TURN_DONE_KIND])
  })

  it('a Bot worker (mate_root set) finishing a routine turn makes no sound and no toast', () => {
    const ws = mount()
    send(ws, done(MATE_WORKER))
    expect(chimes).toEqual([])
    expect(postNativeNotification).not.toHaveBeenCalled()
  })

  it('a Bot worker that paused on a question still sounds, as approval, and toasts', () => {
    const ws = mount()
    send(ws, done(MATE_WORKER, { needs_input: true }))
    expect(chimes).toEqual([APPROVAL_KIND])
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
  })

  it('a Bot worker whose turn ended in a terminal error still sounds, as approval, and toasts', () => {
    const ws = mount()
    send(ws, errorRow(MATE_WORKER))
    send(ws, done(MATE_WORKER))
    expect(chimes).toEqual([APPROVAL_KIND])
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
  })
})

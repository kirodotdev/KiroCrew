/**
 * Muting an app in Settings > Notifications > Sources (`apps.<name>.mute_sessions`)
 * silences the sessions that app opened, from the config the dashboard already
 * caches, while the user's own session keeps its chime. Driven over the dashboard
 * socket so both attention gates (the chat_message unread mark and the chat_done
 * chime and toast) read the setting the way the app wires them.
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
import { _resetTurnErrorsForTest } from '../hooks/turnError'

const APP_SLOT = 'slot-app'
const OWN_SLOT = 'slot-own'
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

describe('a muted app over the dashboard socket', () => {
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
    globalStore.dispatch(setActiveSlot(ON_SCREEN))
  })

  afterEach(() => {
    window.removeEventListener(MC_NOTIFICATION_EVENT, onChime)
    _resetSlotReadRelayForTest()
    _resetTurnErrorsForTest()
    vi.unstubAllGlobals()
    globalStore.dispatch(markSlotRead(APP_SLOT))
    globalStore.dispatch(markSlotRead(OWN_SLOT))
    globalStore.dispatch(sseSlots([]))
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount(muteSessions: boolean) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['kirocrewConfig'], { apps: { 'code-reviewer': { model: '', mute_sessions: muteSessions } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(Provider, { store: globalStore }, createElement(QueryClientProvider, { client: qc }, children))
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    globalStore.dispatch(sseSlots([
      { key: ON_SCREEN, messages: 1, running: false },
      { key: APP_SLOT, messages: 1, running: true, app: 'code-reviewer', origin: 'app' },
      { key: OWN_SLOT, messages: 1, running: true },
    ]))
    return ws
  }
  const turn = (ws: MockWebSocket, slot: string) => act(() => {
    ws.simulateMessage({ type: 'chat_message', data: { slot, role: 'assistant', content: 'x', ts: '2026-11-05T00:00:01Z' } })
    ws.simulateMessage({ type: 'chat_done', data: { slot, ts: '2026-11-05T00:00:05Z', continuing: false } })
  })
  const unread = () => globalStore.getState().dashboard.unreadSlots
  const turnChimes = () => chimes.filter(k => k === TURN_DONE_KIND).length

  it("the muted app's session finishes in silence while the user's own still chimes", () => {
    const ws = mount(true)
    turn(ws, APP_SLOT)
    expect(turnChimes()).toBe(0)
    expect(postNativeNotification).not.toHaveBeenCalled()
    expect(unread()).not.toContain(APP_SLOT)
    turn(ws, OWN_SLOT)
    expect(turnChimes()).toBe(1)
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
    expect(unread()).toContain(OWN_SLOT)
  })

  it('an app that is not muted keeps every signal', () => {
    const ws = mount(false)
    turn(ws, APP_SLOT)
    expect(turnChimes()).toBe(1)
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
    expect(unread()).toContain(APP_SLOT)
  })
})

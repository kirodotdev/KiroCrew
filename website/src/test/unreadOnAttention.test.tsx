/**
 * The "mark sessions unread only when they need you" opt-in.
 *
 * Off (the default) keeps today's rule: every chat_message in a background
 * session badges it. On, routine rows stay quiet and only the hand-off
 * signals badge: the finished turn, a permission row, a question card, and a
 * coordinator approval. The Settings toggle persists through the helper.
 *
 * The hook reads pending question cards off the singleton store, so the
 * socket specs mount the Provider ON the singleton, as the app does.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, render, screen, fireEvent } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearMessages, resolveQuestionCard } from '../store/chatSlice'
import { markSlotRead } from '../store/dashboardSlice'
import { _resetSlotReadRelayForTest } from '../lib/slotReadRelay'
import { UNREAD_ON_ATTENTION_KEY, chatMessageMarksUnread, loadUnreadOnAttention } from '../hooks/unreadOnAttention'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import en from '../i18n/locales/en.manual.json'

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

const ACTIVE = 'slot-active'
const BACKGROUND = 'slot-background'
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

describe('chatMessageMarksUnread', () => {
  beforeEach(() => localStorage.clear())

  it('badges every row while the opt-in is off (default, and a corrupt value)', () => {
    expect(chatMessageMarksUnread('assistant')).toBe(true)
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, 'yes')
    expect(chatMessageMarksUnread('tool_call')).toBe(true)
  })

  it('badges only a permission row while the opt-in is on', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    expect(chatMessageMarksUnread('assistant')).toBe(false)
    expect(chatMessageMarksUnread('tool_call')).toBe(false)
    expect(chatMessageMarksUnread(undefined)).toBe(false)
    expect(chatMessageMarksUnread('permission')).toBe(true)
  })
})

describe('unread badge over the dashboard socket', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    _resetSlotReadRelayForTest()
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    _resetSlotReadRelayForTest()
    vi.unstubAllGlobals()
    for (const id of ['a1', 'a2']) globalStore.dispatch(resolveQuestionCard({ ask_id: id }))
    globalStore.dispatch(markSlotRead(BACKGROUND))
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount() {
    const wrapper = ({ children }: { children: React.ReactNode }) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: qc }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }
  const unread = () => globalStore.getState().dashboard.unreadSlots
  const send = (ws: MockWebSocket, frame: unknown) => act(() => { ws.simulateMessage(frame) })
  const row = (role: string) => ({ type: 'chat_message', data: { slot: BACKGROUND, role, content: 'x', ts: '2026-09-28T00:00:00Z' } })

  it('off: a routine agent row badges a background session, as before', () => {
    const ws = mount()
    send(ws, row('tool_call'))
    expect(unread()).toContain(BACKGROUND)
  })

  it('off: a question card and an approval add no badge of their own', () => {
    const ws = mount()
    send(ws, { type: 'question_card', data: { slot: BACKGROUND, ask_id: 'a1', questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] } })
    send(ws, { type: 'approval', data: { id: 'ap1', slot: BACKGROUND, tool: 'shell' } })
    expect(unread()).not.toContain(BACKGROUND)
  })

  it('on: routine rows stay quiet, the finished turn badges', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount()
    send(ws, row('assistant'))
    send(ws, row('tool_call'))
    send(ws, row('tool_result'))
    expect(unread()).not.toContain(BACKGROUND)
    send(ws, { type: 'chat_done', data: { slot: BACKGROUND, ts: '2026-09-28T00:00:05Z' } })
    expect(unread()).toContain(BACKGROUND)
  })

  it('on: a permission row badges', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount()
    send(ws, row('permission'))
    expect(unread()).toContain(BACKGROUND)
  })

  it('on: a question card badges', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount()
    send(ws, { type: 'question_card', data: { slot: BACKGROUND, ask_id: 'a1', questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] } })
    expect(unread()).toContain(BACKGROUND)
  })

  it('on: a coordinator approval badges its session', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount()
    send(ws, { type: 'approval', data: { id: 'ap1', slot: BACKGROUND, tool: 'shell' } })
    expect(unread()).toContain(BACKGROUND)
  })

  it('on: a question card in the session on screen adds no badge', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount()
    send(ws, { type: 'question_card', data: { slot: ACTIVE, ask_id: 'a2', questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] } })
    expect(unread()).not.toContain(ACTIVE)
  })
})

describe('Settings toggle', () => {
  beforeEach(() => localStorage.clear())

  it('is off by default and persists a flip', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // The toggle lives in the "Desktop alerts" rail item; SettingsSubNav reads the
    // sub param from the router, so mount under one pointed at that item.
    render(createElement(MemoryRouter, { initialEntries: ['/settings?tab=notifications&sub=alerts'] },
      createElement(QueryClientProvider, { client: qc }, createElement(NotificationsPanel))))
    const label = en.pages.settings.notificationsPanel.unread_only_when_done_or_waiting
    const toggle = screen.getByRole('switch', { name: label })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(toggle)
    expect(loadUnreadOnAttention()).toBe(true)
    expect(screen.getByRole('switch', { name: label }).getAttribute('aria-checked')).toBe('true')
  })
})

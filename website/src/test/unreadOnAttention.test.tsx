/**
 * The "mark sessions unread only when they need you" opt-in.
 *
 * Off (the default) keeps today's rule: every chat_message in a background
 * session badges it. On, routine rows stay quiet and only the hand-off
 * signals badge: the finished turn, a permission row, a question card, and a
 * coordinator approval. The Settings toggle persists through the helper.
 *
 * A member DM thread (`slot.mode === 'member'`) is narrower in both modes:
 * only a row the crewmate wrote to the user (`assistant`, `permission`)
 * badges it; its tool calls never do, and its finished turn badges only
 * when the turn delivered such a row or paused for input.
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
import { markSlotRead, remoteSlotRead, sseSlots } from '../store/dashboardSlice'
import { _resetSlotReadRelayForTest, emitSlotRead } from '../lib/slotReadRelay'
import {
  UNREAD_ON_ATTENTION_KEY, _resetMemberThreadTurnsForTest, chatMessageMarksUnread, isMemberThreadSlot,
  loadUnreadOnAttention, memberThreadRowMarksUnread, noteMemberThreadRow, takeMemberThreadSpoke, unreadWatermarkTs,
} from '../hooks/unreadOnAttention'
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
/** A crewmate's DM thread, off screen. The server reserves this key prefix. */
const MEMBER = 'member-ada'
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

describe('memberThreadRowMarksUnread', () => {
  beforeEach(() => localStorage.clear())

  it('off: badges only a row the crewmate wrote to the user', () => {
    expect(memberThreadRowMarksUnread('assistant')).toBe(true)
    expect(memberThreadRowMarksUnread('permission')).toBe(true)
    for (const role of ['tool_call', 'tool_result', 'inject', 'subagent', 'user', 'chunk', 'streaming', undefined]) {
      expect(memberThreadRowMarksUnread(role)).toBe(false)
    }
  })

  it('on: narrows further to the permission row, never wider than the opt-in', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    expect(memberThreadRowMarksUnread('assistant')).toBe(false)
    expect(memberThreadRowMarksUnread('tool_call')).toBe(false)
    expect(memberThreadRowMarksUnread('permission')).toBe(true)
  })
})

describe('member turn record', () => {
  beforeEach(() => _resetMemberThreadTurnsForTest())

  it('remembers a row to the user until the turn takes it, once', () => {
    noteMemberThreadRow('member-ada', 'tool_call')
    expect(takeMemberThreadSpoke('member-ada')).toBe(false)
    noteMemberThreadRow('member-ada', 'assistant')
    expect(takeMemberThreadSpoke('member-ada')).toBe(true)
    expect(takeMemberThreadSpoke('member-ada')).toBe(false)
    noteMemberThreadRow('member-ada', 'permission')
    expect(takeMemberThreadSpoke('member-ada')).toBe(true)
    expect(takeMemberThreadSpoke('member-bob')).toBe(false)
  })
})

describe('isMemberThreadSlot', () => {
  it('reads the slot mode when the list has the slot', () => {
    const slots = [{ key: 'member-ada', mode: 'member' }, { key: 'chat-1', mode: '' }, { key: 'chat-2' }]
    expect(isMemberThreadSlot('member-ada', slots)).toBe(true)
    expect(isMemberThreadSlot('chat-1', slots)).toBe(false)
    expect(isMemberThreadSlot('chat-2', slots)).toBe(false)
  })

  it('falls back to the reserved key prefix for a slot the list has not caught up to', () => {
    expect(isMemberThreadSlot('member-ada', [])).toBe(true)
    expect(isMemberThreadSlot('member-ada', undefined)).toBe(true)
    expect(isMemberThreadSlot('chat-9', undefined)).toBe(false)
  })
})

describe('unread badge over the dashboard socket', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    _resetSlotReadRelayForTest()
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    _resetMemberThreadTurnsForTest()
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    _resetSlotReadRelayForTest()
    _resetMemberThreadTurnsForTest()
    vi.unstubAllGlobals()
    for (const id of ['a1', 'a2']) globalStore.dispatch(resolveQuestionCard({ ask_id: id }))
    globalStore.dispatch(markSlotRead(BACKGROUND))
    globalStore.dispatch(markSlotRead(MEMBER))
    // A spec that seeds the slots list must not hand its rows to the next one:
    // a known slot's last_ts moves on arrival and would change the watermark.
    globalStore.dispatch(sseSlots([]))
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
  const row = (role: string, slot: string = BACKGROUND) => ({ type: 'chat_message', data: { slot, role, content: 'x', ts: '2026-09-28T00:00:00Z' } })

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

  // A crewmate's DM thread: the Crewmates rail dot means "they said something
  // to you". Its tool calls are its own work and never light it.
  it("member thread: a crewmate's tool call and tool result badge nothing", () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    send(ws, row('tool_call', MEMBER))
    send(ws, row('tool_result', MEMBER))
    send(ws, row('inject', MEMBER))
    expect(unread()).not.toContain(MEMBER)
  })

  it("member thread: a crewmate's message badges", () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    send(ws, row('assistant', MEMBER))
    expect(unread()).toContain(MEMBER)
  })

  it('member thread: a permission row badges', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    send(ws, row('permission', MEMBER))
    expect(unread()).toContain(MEMBER)
  })

  it('member thread: the key prefix alone decides before the slots list has the thread', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([]))
    send(ws, row('tool_call', MEMBER))
    expect(unread()).not.toContain(MEMBER)
    send(ws, row('assistant', MEMBER))
    expect(unread()).toContain(MEMBER)
  })

  it('member thread, on: an assistant row stays quiet, the permission row badges', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    send(ws, row('assistant', MEMBER))
    expect(unread()).not.toContain(MEMBER)
    send(ws, row('permission', MEMBER))
    expect(unread()).toContain(MEMBER)
  })

  const memberDone = { type: 'chat_done', data: { slot: MEMBER, ts: '2026-09-28T00:00:05Z' } }

  it('member thread: a tool-only turn ending quietly badges nothing', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    send(ws, row('tool_call', MEMBER))
    send(ws, row('tool_result', MEMBER))
    send(ws, memberDone)
    expect(unread()).not.toContain(MEMBER)
  })

  it('member thread: a turn that spoke badges on its chat_done, and only that turn', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')  // the row itself stays quiet; the done carries it
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    send(ws, row('assistant', MEMBER))
    expect(unread()).not.toContain(MEMBER)
    send(ws, memberDone)
    expect(unread()).toContain(MEMBER)
    globalStore.dispatch(markSlotRead(MEMBER))
    send(ws, row('tool_call', MEMBER))
    send(ws, memberDone)
    expect(unread()).not.toContain(MEMBER)
  })

  it('member thread: a quiet turn that pauses for input badges', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    send(ws, row('tool_call', MEMBER))
    send(ws, { ...memberDone, data: { ...memberDone.data, needs_input: true } })
    expect(unread()).toContain(MEMBER)
  })

  it('member thread: a row the user watched arrive still counts for the chat_done', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: MEMBER, messages: 1, running: true, mode: 'member', agent: 'ada' }]))
    globalStore.dispatch(setActiveSlot(MEMBER))
    send(ws, row('assistant', MEMBER))
    globalStore.dispatch(setActiveSlot(ACTIVE))
    send(ws, memberDone)
    expect(unread()).toContain(MEMBER)
  })

  it('a non-member slot still badges on a quiet chat_done', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: BACKGROUND, messages: 1, running: true, mode: '' }]))
    send(ws, row('tool_call'))
    globalStore.dispatch(markSlotRead(BACKGROUND))
    send(ws, { type: 'chat_done', data: { slot: BACKGROUND, ts: '2026-09-28T00:00:05Z' } })
    expect(unread()).toContain(BACKGROUND)
  })

  it('a non-member slot the list knows keeps the ordinary rule on a tool call', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: BACKGROUND, messages: 1, running: true, mode: '' }]))
    send(ws, row('tool_call'))
    expect(unread()).toContain(BACKGROUND)
  })

  // The gateway never saves a permission row, so after a restart no slot
  // last_ts reaches its ts. A watermark taken from it could never be covered.
  const sharedWatermark = () => (JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)[BACKGROUND]

  it.each([false, true])('a permission row records no watermark of its own (opt-in %s)', (optIn) => {
    if (optIn) localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    const ws = mount()
    send(ws, row('permission'))
    expect(unread()).toContain(BACKGROUND)
    expect(sharedWatermark()).toBe('')
    globalStore.dispatch(markSlotRead(BACKGROUND))
    expect(sharedWatermark()).toBeUndefined()
  })

  it('a read relayed at the saved last_ts cannot clear a newer permission badge', () => {
    // Another window watching the slot relays its read at the saved last_ts
    // (t1), after the permission row (t2) badged this one. The shared record
    // holds t1, but this window keeps t2, so the stale relay leaves the badge.
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: BACKGROUND, messages: 2, running: true, last_ts: '2026-09-27T23:59:59Z' }]))
    send(ws, row('permission'))
    expect(sharedWatermark()).toBe('2026-09-27T23:59:59Z')
    globalStore.dispatch(remoteSlotRead({ slot: BACKGROUND, readTs: '2026-09-27T23:59:59Z' }))
    expect(unread()).toContain(BACKGROUND)
    expect(sharedWatermark()).toBe('2026-09-27T23:59:59Z')
    globalStore.dispatch(remoteSlotRead({ slot: BACKGROUND, readTs: '2026-09-28T00:00:00Z' }))
    expect(unread()).not.toContain(BACKGROUND)
    expect(sharedWatermark()).toBeUndefined()
  })

  it('a read this window relays at the saved last_ts carries the permission row ts it saw', () => {
    const ws = mount()
    globalStore.dispatch(sseSlots([{ key: BACKGROUND, messages: 2, running: true, last_ts: '2026-09-27T23:59:59Z' }]))
    send(ws, row('permission'))
    ws.send.mockClear()
    emitSlotRead(BACKGROUND, '2026-09-27T23:59:59Z')
    const reads = ws.send.mock.calls.map(c => JSON.parse(c[0] as string)).filter(f => f.type === 'slot_read')
    expect(reads).toEqual([{ type: 'slot_read', slot: BACKGROUND, read_ts: '2026-09-28T00:00:00Z' }])
  })

  it('a saved row still records its own ts as the watermark', () => {
    const ws = mount()
    send(ws, row('tool_call'))
    expect(sharedWatermark()).toBe('2026-09-28T00:00:00Z')
  })
})

describe('unreadWatermarkTs', () => {
  it('keeps the ts of a saved row and drops the ts of an unsaved one', () => {
    for (const role of ['assistant', 'tool_call', 'tool_result', 'user', 'inject']) {
      expect(unreadWatermarkTs(role, 't1')).toBe('t1')
    }
    for (const role of ['chunk', 'done', 'streaming', 'queued', 'permission']) {
      expect(unreadWatermarkTs(role, 't1')).toBeUndefined()
    }
    expect(unreadWatermarkTs('assistant', '')).toBeUndefined()
    expect(unreadWatermarkTs(undefined, 't1')).toBe('t1')
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

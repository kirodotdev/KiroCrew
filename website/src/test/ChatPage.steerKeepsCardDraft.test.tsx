/**
 * A pick made in a blocking question card survives a mid-turn Steer.
 *
 * Sequence observed on a live gateway: the card holds a pick (draftActive, so
 * the steer does not resolve the ask itself), the user steers typed text into
 * the running turn, kiro-cli cancels the blocked `ask_question` tool call, the
 * tool withdraws its card (`question_card_resolved`, reason `withdrawn`), the
 * agent answers the steer and the turn ends (`chat_done` -> `refreshSlot`).
 *
 * The pick must land in the composer with a notice that outlives the
 * transcript refresh the finished turn triggers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement, type ReactNode } from 'react'
import { render, renderHook, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { refreshSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { useWebSocket } from '../hooks/useWebSocket'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const sendChat = vi.fn()
const slotRow = () => ({
  key: 'slot-a', messages: 1, running: true, mode: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
  subagents_running: false,
})
vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500; body = '' },
  api: {
    chatSlots: vi.fn().mockImplementation(() => Promise.resolve([slotRow()])),
    chatSlotDetail: vi.fn().mockImplementation(() => Promise.resolve({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: true, has_more: false, total: 1, queue: [] })),
    sendChat: (...a: unknown[]) => sendChat(...a),
    answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
    // The socket's open-time reconcile must still list the card, or it drops it as stale.
    pendingQuestions: vi.fn().mockImplementation(() => Promise.resolve({ pending: [{ ask_id: 'ask-1', slot: 'slot-a', questions: QUESTIONS }], resolved: {} })),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    suggestions: vi.fn().mockResolvedValue({ suggestions: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

const WS_INSTANCES: MockWebSocket[] = []
class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
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

const STEERED_TEXT = 'Actually, what sizes do you support?'
const QUESTIONS = [
  { question: 'Which color?', header: 'COLOR', options: [{ label: 'Teal' }, { label: 'Amber' }] },
  { question: 'Which size?', header: 'SIZE', options: [{ label: 'Small' }, { label: 'Large' }] },
]

function makeStore() {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [slotRow()],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: true, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
        pendingQuestions: { 'slot-a': { slot: 'slot-a', ask_id: 'ask-1', questions: QUESTIONS } },
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
  // Receipt handlers and the socket's card handlers read the singleton.
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  return store
}

/** The page plus the live socket, the way App mounts them. */
async function mountPage() {
  const store = makeStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => createElement(
    QueryClientProvider, { client: qc },
    createElement(Provider, { store }, createElement(ThemeProvider, null, createElement(MemoryRouter, null, children))),
  )
  await act(async () => {
    render(<ChatPage />, { wrapper })
  })
  const socket = renderHook(() => useWebSocket(), { wrapper })
  const ws = WS_INSTANCES[0]
  await act(async () => { ws.simulateOpen() })
  await waitFor(() => expect(store.getState().chat.pendingQuestions['slot-a']?.ask_id).toBe('ask-1'))
  const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
  return { store, qc, ws, socket, input }
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  WS_INSTANCES.length = 0
  vi.stubGlobal('WebSocket', MockWebSocket)
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) })
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('steer while the question card holds a pick', { timeout: 20_000 }, () => {
  it('hands the pick back to the composer with a durable notice once the server withdraws the card', async () => {
    const { store, qc, ws, input } = await mountPage()

    // 1. A pick in the card publishes a draft, so a steer must not resolve the ask.
    fireEvent.click(screen.getByText('Teal'))
    await waitFor(() => expect(store.getState().chat.pendingQuestions['slot-a']?.draftAnswers).toEqual({ 'Which color?': 'Teal' }))

    // 2. Steer typed text into the running turn.
    fireEvent.change(input, { target: { value: STEERED_TEXT } })
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(sendChat.mock.calls[0][5]).toBe(true)
    await waitFor(() => expect(qc.isMutating()).toBe(0))
    expect(input.value).toBe('')
    expect(store.getState().chat.pendingQuestions['slot-a']?.ask_id).toBe('ask-1')

    // 3. kiro-cli cancelled the blocked tool call; the tool withdrew its card.
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-1', slot: 'slot-a', reason: 'withdrawn' } })
    })
    expect(store.getState().chat.pendingQuestions['slot-a']).toBeUndefined()
    await waitFor(() => expect(input.value).toBe('Teal'))

    // 4. The agent answered the steer and the turn ended: the transcript is re-read from the server.
    await act(async () => { await store.dispatch(refreshSlot('slot-a') as never) })

    expect(input.value).toBe('Teal')
    expect(store.getState().chat.restoredQuestionNotices?.['slot-a']).toEqual({
      message: expect.stringContaining('Which color?'),
      kind: 'restored',
    })
    expect(screen.getByTestId('pending-question-notice')).toHaveTextContent(/Which color\?/)
    // Nothing rides the transcript: a local row would not have survived the refresh above.
    expect(store.getState().chat.messages.some(m => m.role === 'notice')).toBe(false)
  })

  it('leaves the composer alone and shows no notice when the withdrawn card held no pick', async () => {
    const { store, ws, input } = await mountPage()
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-1', slot: 'slot-a', reason: 'withdrawn' } })
    })
    expect(store.getState().chat.pendingQuestions['slot-a']).toBeUndefined()
    expect(input.value).toBe('')
    expect(store.getState().chat.restoredQuestionNotices?.['slot-a']).toBeUndefined()
    expect(screen.queryByTestId('pending-question-notice')).toBeNull()
  })
})

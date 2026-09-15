/**
 * The single-chat composer's optimistic bubble is confirmed by the send's OWN
 * HTTP response (#4131).
 *
 * `meta.optimistic` marks a bubble as awaiting confirmation. A `chat_message`
 * user echo can also clear it through `reconcileOptimisticEcho`.
 * Dashboard sends now emit that echo before starting the reply, but the HTTP
 * response still confirms delivery if the echo is missed.
 *
 * Before dashboard sends emitted user echoes, response-driven confirmation
 * was the only way to retire the pending state during a turn. The 30s wall-clock
 * indicator once flagged every dashboard send, including delivered ones,
 * which is why it was removed.
 *
 * These tests pin both directions: an accepted response retires the pending
 * state, a refused one leaves it alone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { sseChatMessage } from '../store/chatSlice'
import * as transport from '../chat-core/transport/sendTurn'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const sendChat = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: (...a: unknown[]) => sendChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    knowledgeSources: vi.fn().mockResolvedValue({ sources: [] }),
    createChatSlot: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: '',
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderPage(store: ReturnType<typeof makeStore>) {
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat']}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

/** Type into the composer and submit, the way the user does. */
async function sendText(text: string) {
  const box = screen.getByLabelText('Message input')
  fireEvent.change(box, { target: { value: text } })
  await act(async () => { fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' }) })
}

const userRow = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.find(m => m.role === 'user')

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
})
afterEach(() => vi.restoreAllMocks())

describe('send() confirms its own optimistic bubble from the response', { timeout: 20_000 }, () => {
  it('keeps a confirmed send when the HTTP connection resets after its user echo', async () => {
    let rejectSend!: (error: unknown) => void
    sendChat.mockReturnValue(new Promise((_resolve, reject) => { rejectSend = reject }))
    const sendTurn = vi.spyOn(transport, 'sendTurn')
    try {
      const store = makeStore()
      await renderPage(store)
      await sendText('already delivered')
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      const [content, slot, , , meta] = sendChat.mock.calls[0]
      await act(async () => {
        store.dispatch(sseChatMessage({ slot, role: 'user', content, ts: '2026-09-10T00:00:00Z', meta: { ...meta, mid: 'm-delivered' } }))
        store.dispatch(sseChatMessage({ slot, role: 'chunk', content: 'reply in progress' }))
        rejectSend(new TypeError('connection reset after acceptance'))
        await sendTurn.mock.results[0].value
      })
      expect(store.getState().chat.messages.filter(m => m.role === 'user')).toHaveLength(1)
      expect(userRow(store)?.meta?.optimistic).toBeUndefined()
      expect(screen.getByLabelText('Message input')).toHaveValue('')
      expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
      expect(store.getState().chat.slotRunning).toBe(true)
    } finally {
      sendTurn.mockRestore()
    }
  })

  it('retires the pending state on an accepted send', async () => {
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('this one was delivered')

    await waitFor(() => expect(userRow(store)).toBeTruthy())
    await waitFor(() => expect(userRow(store)?.meta?.optimistic).toBeUndefined())
    // The correlation id survives so a late echo (channel-linked slot) still
    // updates this row in place instead of pushing a duplicate bubble.
    expect(userRow(store)?.meta?.sendId).toMatch(/^s-/)
  })

  it('does NOT count a queued acceptance as delivery for this bubble', async () => {
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: true }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('queued is not delivered')

    await waitFor(() => expect(userRow(store)).toBeTruthy())
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // The busy branch queues only a non-empty message yet answers ok+queued
    // either way, and when it does queue, its own `queue_push` card owns the
    // message. Either way this bubble is not a delivered row.
    expect(userRow(store)?.meta?.optimistic).toBe(true)
  })

  it('leaves an unreadable 2xx receipt SILENT — pending bubble, no error row (#4217)', async () => {
    // The request was accepted and only its answer is mangled, so the turn may
    // be running. The bubble stays pending, which is exactly what it means, and
    // no error row claims a refusal that nothing proves — telling the user to
    // retry here duplicates a delivered turn, side effects included.
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.reject(new Error('unexpected end of JSON input')) })
    const store = makeStore()
    await renderPage(store)
    await sendText('maybe it landed')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(userRow(store)).toBeTruthy())
    expect(userRow(store)?.meta?.optimistic).toBe(true)
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
    // The payload is NOT handed back: a composer holding it again is the retry
    // invitation this whole branch exists to withhold.
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
  })

  it('leaves the bubble pending when the server rejects the send', async () => {
    sendChat.mockResolvedValue({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('this one was refused')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // A refusal is not a receipt, so the pending flag stays put. What the user
    // is told is not this flag: the refusal path appends its own error row and
    // hands the text back to the composer, immediately and by name.
    expect(userRow(store)?.meta?.optimistic).toBe(true)
  })
})

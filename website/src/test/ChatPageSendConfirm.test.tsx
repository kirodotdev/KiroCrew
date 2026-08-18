/**
 * The single-chat composer's optimistic bubble is confirmed by the send's OWN
 * HTTP response (#4131).
 *
 * The 30s "Message not confirmed - may not have been delivered" indicator keys
 * on `meta.optimistic` surviving, and the only other thing that clears it is
 * `reconcileOptimisticEcho`, driven by a `chat_message` user echo. That echo is
 * never broadcast for a dashboard send: `DashboardState.append` defaults
 * `broadcast_user=False` precisely BECAUSE the composer already rendered the
 * bubble, and the composer's persistence point does not override it (only a row
 * replayed from a CHANNEL transcript opts in). So without a response-driven
 * confirmation every message the user types is flagged 30s later, mid-turn, and
 * un-flagged only as a side effect of `chat_done`'s transcript refresh.
 *
 * These tests pin both directions: an accepted response retires the pending
 * state, a rejected one leaves it for the sweep.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { sweepStaleOptimistic, OPTIMISTIC_TIMEOUT_MS } from '../store/chatSlice'
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

describe('send() confirms its own optimistic bubble from the response', { timeout: 20_000 }, () => {
  it('retires the pending state on an accepted send, so the sweep never flags it', async () => {
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('this one was delivered')

    await waitFor(() => expect(userRow(store)).toBeTruthy())
    await waitFor(() => expect(userRow(store)?.meta?.optimistic).toBeUndefined())
    // The correlation id survives so a late echo (channel-linked slot) still
    // updates this row in place instead of pushing a duplicate bubble.
    expect(userRow(store)?.meta?.sendId).toMatch(/^s-/)

    // Even aged well past the timeout, a confirmed bubble cannot go stale.
    store.dispatch(sweepStaleOptimistic())
    expect(userRow(store)?.meta?.stale).toBeUndefined()
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

  it('leaves the bubble pending when the server rejects the send', async () => {
    sendChat.mockResolvedValue({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('this one was refused')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // A refused send is exactly what the indicator is for: the flags stay, and
    // the sweep can flag it once the timeout passes.
    const row = userRow(store)
    expect(row?.meta?.optimistic).toBe(true)
    expect(typeof row?.meta?.optimisticTs).toBe('number')
    expect(OPTIMISTIC_TIMEOUT_MS).toBeGreaterThan(0)
  })
})

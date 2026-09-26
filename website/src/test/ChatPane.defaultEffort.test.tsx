import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A grid pane's effort chip names the inherited default from the shared
 * ['kirocrewConfig'] entry -- the one the Settings save writes and the server's
 * refresh broadcast invalidates -- and says so in fixed wording when that read
 * fails, instead of leaving the chip silently on Default. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false }),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: {} }),
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
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const MESSAGES = [
  { role: 'user', content: 'hi', ts: '2026-08-25T00:00:00Z' },
  { role: 'assistant', content: 'Ready to proceed.', ts: '2026-08-25T00:00:01Z' },
]

function makeStore(slotKey: string, slotFields: Record<string, unknown> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...slotFields }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(slotKey = 'pane-1', slotFields: Record<string, unknown> = {}) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <Provider store={makeStore(slotKey, slotFields)}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slotKey} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  await waitFor(() => expect(screen.getByText(/Ready to proceed/)).toBeTruthy())
  return qc
}

describe('ChatPane default effort', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('reads the default from the shared kirocrewConfig entry', async () => {
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ agent: { reasoning_effort: 'high' } })
    const qc = await renderPane()
    await waitFor(() => expect(qc.getQueryData(['kirocrewConfig'])).toEqual({ agent: { reasoning_effort: 'high' } }))
    expect(screen.queryByTestId('chat-pane-default-effort-config-error')).toBeNull()
  })

  it('says when the Settings read fails, in fixed wording rather than the server text', async () => {
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('config store unavailable'))
    await renderPane()
    const notice = await screen.findByTestId('chat-pane-default-effort-config-error')
    expect(notice).toHaveTextContent("Couldn't load your Settings, so the effort control next to the model picker shows Default. You can still send messages.")
    expect(screen.queryByText(/config store unavailable/)).toBeNull()
  })

  it('shows no notice for a pane with its own effort, since the failed read changes nothing it shows', async () => {
    // The notice says the control shows Default. A pane pinned to its own level
    // keeps showing that level whatever the Settings read did, so the notice
    // would describe a control that is not there.
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('config store unavailable'))
    const qc = await renderPane('pane-1', { reasoning_effort: 'low' })
    // Wait for the read to actually fail, so the absence below is a verdict on
    // the failed state rather than on a query that has not settled yet.
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    expect(screen.queryByTestId('chat-pane-default-effort-config-error')).toBeNull()
  })
})

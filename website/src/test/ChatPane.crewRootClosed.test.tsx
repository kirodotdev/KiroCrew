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

/* Crew Mode L1: a Split View pane holding a worker whose conductor is closed
 * says 'Main chat is closed. Open it again to message <crew member>.' once, on the gate line, and leaves the
 * composer's placeholder empty, as the main worker view does (R5 V2). */

const engine = {
  recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
  toggle: vi.fn(), start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
  error: null as string | null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
  download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
}
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => engine, voiceInputSupported: true }))
vi.mock('../hooks/usePushToTalk', () => ({ usePushToTalk: () => ({ config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }) }))
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
    crewWorkers: vi.fn(() => Promise.reject(new Error('no worker list'))),
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }),
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
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'chat-1-worker'

async function renderPane() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // No 'root-1' row: the conductor is closed.
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, title: 'Worker B', crew_root: 'root-1', created_by: 'root-1', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
}

beforeEach(() => { localStorage.clear() })

describe('Crew Mode L1: a worker in a Split View pane under a closed conductor', () => {
  it('says the reopen line once and leaves the placeholder empty', async () => {
    await renderPane()
    expect(screen.getByTestId('crew-worker-root-closed')).toHaveTextContent('Main chat is closed')
    expect(screen.getAllByText(/Main chat is closed/)).toHaveLength(1)
    const box = screen.getByRole('textbox')
    expect(box.getAttribute('placeholder') ?? '').toBe('')
  })
})

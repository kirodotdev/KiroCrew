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

/* A split-view pane on a chat with its own backend pick reads that backend's
 * model list. When that list fails to load it falls back to Auto alone and keeps
 * retrying; the pane must say so rather than look like a backend with no models.
 * A pane on a chat with no pick is not blamed for the configured list. */

const engine = {
  recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
  toggle: vi.fn(), start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
  error: null as string | null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
  download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
}
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => engine, voiceInputSupported: true }))
vi.mock('../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
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
import { markModelsDegraded, modelHealthKey } from '../providers/modelListHealth'
import { useProvider } from '../providers'

const SLOT = 'chat-1-models'

function makeStore(slotKey: string, acpBackend: string | null, degraded = false) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, acp_backend: acpBackend, acp_backend_degraded: degraded }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

let providerId = ''
function ProviderId() { providerId = useProvider().id; return null }

async function renderPane(acpBackend: string | null, degraded = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <Provider store={makeStore(SLOT, acpBackend, degraded)}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ProviderId />
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

describe('ChatPane: a picked backend whose model list failed', () => {
  it('says the models could not be loaded', async () => {
    await renderPane('kas')
    act(() => { markModelsDegraded(modelHealthKey(providerId, 'kas'), true) })
    try {
      const notice = await screen.findByTestId('chat-pane-backend-models-error')
      expect(notice.textContent).toContain("Couldn't load the models for this backend")
    } finally {
      act(() => { markModelsDegraded(modelHealthKey(providerId, 'kas'), false) })
    }
  })

  it('reads a degraded pick off Kiro\'s list, which the chat runs on', async () => {
    await renderPane('codex', true)
    act(() => { markModelsDegraded(modelHealthKey(providerId, ''), true) })
    try {
      const notice = await screen.findByTestId('chat-pane-backend-models-error')
      expect(notice.textContent).toContain("Couldn't load the models for this backend")
    } finally {
      act(() => { markModelsDegraded(modelHealthKey(providerId, ''), false) })
    }
    // The unselectable pick's own list failing is not reported: it is not used.
    act(() => { markModelsDegraded(modelHealthKey(providerId, 'codex'), true) })
    try {
      await waitFor(() => expect(screen.queryByTestId('chat-pane-backend-models-error')).toBeNull())
    } finally {
      act(() => { markModelsDegraded(modelHealthKey(providerId, 'codex'), false) })
    }
  })

  it('does not blame a chat with no pick for the configured list', async () => {
    await renderPane(null)
    act(() => { markModelsDegraded(modelHealthKey(providerId), true) })
    try {
      await new Promise(r => setTimeout(r, 0))
      expect(screen.queryByTestId('chat-pane-backend-models-error')).toBeNull()
    } finally {
      act(() => { markModelsDegraded(modelHealthKey(providerId), false) })
    }
  })
})

/**
 * The "Start Dictation with a Key" binding in a ChatPane (#24967).
 *
 * The binding is document-wide, so only the host that shows ONE composer may
 * arm it. ChatPage arms its own; a crewmate DM is a ChatPane, which used to
 * leave it off for every host, so the key did nothing there. The pane now arms
 * it only when its host passes `pushToTalk`; split-view panes stay off.
 *
 * The real ChatPane is rendered; `usePushToTalk` is stubbed to record the
 * `disabled` flag the composer hands it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn(),
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
    dashboardConfig: vi.fn().mockResolvedValue({}),
    getDecisionsConsent: vi.fn().mockResolvedValue({ permits: false }),
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, streaming: true, dictation_panel: true, provider: 'transcribe', available: true }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    recording: false, transcribing: false, sessionOwner: null, streamEnabled: true,
    toggle: vi.fn(), start: vi.fn(), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
    error: null, level: 0, deviceLabel: '', clearError: vi.fn(), partial: '',
    sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
  }),
  voiceInputSupported: true,
}))
const ptt = vi.hoisted(() => ({ disabled: [] as (boolean | undefined)[] }))
vi.mock('../hooks/usePushToTalk', () => ({
  usePushToTalk: (_voice: unknown, opts?: { disabled?: boolean }) => {
    ptt.disabled.push(opts?.disabled)
    return { config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }
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

const SLOT = 'pane-slot'

async function renderPane(pushToTalk?: boolean) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, subagents_running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} {...(pushToTalk === undefined ? {} : { pushToTalk })} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  await screen.findByRole('button', { name: /voice input/i })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPaneDraftsForTests()
  ptt.disabled = []
})

describe('ChatPane push-to-talk ownership', { timeout: 20_000 }, () => {
  it('arms the dictation key when the host owns it (crewmate DM)', async () => {
    await renderPane(true)
    expect(ptt.disabled.length).toBeGreaterThan(0)
    expect(ptt.disabled.at(-1)).toBe(false)
  })

  it('leaves the key off by default (split-view panes)', async () => {
    await renderPane()
    expect(ptt.disabled.length).toBeGreaterThan(0)
    expect(ptt.disabled.every(d => d === true)).toBe(true)
  })
})

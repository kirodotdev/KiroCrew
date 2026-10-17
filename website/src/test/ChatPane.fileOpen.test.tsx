import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* ChatPane must hand its host's file viewer to the transcript (#9487): a
 * non-image attachment card in a split pane or a Crew Members DM opens the
 * file the way it does on the main chat page. Before this the pane never
 * passed `onFileOpen` to ChatMessageList, so the card rendered without an
 * opener and a click habituated on the main chat answered nothing. Both user
 * paths are pinned: the SDK default `user` entry (split panes) and the
 * transcript factory's steer-only `user` entry (member DMs). */

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
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span data-testid="markdown">{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const MESSAGES = [
  {
    role: 'user',
    content: 'summarize this',
    cls: '',
    ts: '2026-09-08T08:00:00Z',
    meta: { files: ['/tmp/report.pdf'] },
  },
]

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(slotKey: string, extraProps: Record<string, unknown> = {}) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slotKey} {...extraProps} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  // Hydration is settled once the attachment card paints.
  await waitFor(() => expect(screen.getByRole('button', { name: /report\.pdf/ })).toBeTruthy())
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane file open (issue #9487)', () => {
  it('a split-pane card opens through the host handler', async () => {
    const onFileOpen = vi.fn()
    await renderPane('pane-file-1', { onFileOpen })
    fireEvent.click(screen.getByRole('button', { name: /report\.pdf/ }))
    expect(onFileOpen).toHaveBeenCalledWith('/tmp/report.pdf')
  })

  it('a member-DM (steer-only) card opens through the host handler', async () => {
    const onFileOpen = vi.fn()
    await renderPane('pane-file-2', { onFileOpen, busyMode: 'steer-only', frameless: true, agentLocked: true })
    fireEvent.click(screen.getByRole('button', { name: /report\.pdf/ }))
    expect(onFileOpen).toHaveBeenCalledWith('/tmp/report.pdf')
  })
})

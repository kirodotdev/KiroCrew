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

/* ChatPane must hand its host's artifact opener to the transcript (#18320):
 * a crewmate's reply that names the artifact it just made as a
 * `/artifacts/<slug>` link opens it in the Members page's side panel, the way
 * the main chat does. Before this the pane never passed `onArtifactOpen`, so
 * the markdown renderer left the link a plain `target="_blank"` anchor and a
 * click opened the standalone artifact view in another browser tab (or nowhere,
 * when the shell swallows the popup), out of the DM. Both assistant
 * paths are pinned: the SDK default `assistant` entry (split panes) and the
 * transcript factory's crewmate bubble (member DMs). The pane's contract is
 * the plumbing -- `onArtifactOpen` reaching `AssistantMessage` -- so the
 * renderer is stubbed to a link that calls what it was handed. */

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
// The real renderer's artifact handling is pinned by its own tests; here it is
// reduced to the one seam under test -- does the host's opener arrive at all.
vi.mock('../components/MarkdownRenderer', () => ({
  // The user row renders through the same component; only the reply that
  // names an artifact becomes the link, so the query below finds one element.
  default: ({ content, onArtifactOpen }: { content: string; onArtifactOpen?: (slug: string) => void }) => !content.includes('/artifacts/') ? <span>{content}</span> : (
    <a
      href="/artifacts/cr-queue"
      data-testid="artifact-link"
      data-wired={onArtifactOpen ? 'yes' : 'no'}
      onClick={(e) => { e.preventDefault(); onArtifactOpen?.('cr-queue') }}
    >
      {content}
    </a>
  ),
}))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const MESSAGES = [
  { role: 'user', content: 'make me a CR queue', cls: '', ts: '2026-10-09T03:00:00Z' },
  { role: 'assistant', content: 'Saved it as /artifacts/cr-queue', cls: '', ts: '2026-10-09T03:00:05Z' },
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
  // Hydration is settled once the reply's link paints.
  await waitFor(() => expect(screen.getByTestId('artifact-link')).toBeTruthy())
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane artifact open (issue #18320)', () => {
  it('a split-pane reply opens the artifact through the host handler', async () => {
    const onArtifactOpen = vi.fn()
    await renderPane('pane-artifact-1', { onArtifactOpen })
    fireEvent.click(screen.getByTestId('artifact-link'))
    expect(onArtifactOpen).toHaveBeenCalledWith('cr-queue')
  })

  it('a crewmate bubble (member DM) opens the artifact through the host handler', async () => {
    const onArtifactOpen = vi.fn()
    await renderPane('pane-artifact-2', {
      onArtifactOpen,
      busyMode: 'steer-only',
      frameless: true,
      agentLocked: true,
      crewmate: { name: 'atlas', label: 'Atlas' },
    })
    fireEvent.click(screen.getByTestId('artifact-link'))
    expect(onArtifactOpen).toHaveBeenCalledWith('cr-queue')
  })

  it('a host that passes nothing leaves the link unwired (capability by omission)', async () => {
    await renderPane('pane-artifact-3')
    expect(screen.getByTestId('artifact-link').getAttribute('data-wired')).toBe('no')
  })
})

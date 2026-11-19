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

/* ChatPane must hand its host's artifact opener to the transcript factory's
 * sub-agent completion, skill load and session message cards, the way it does
 * for a reply: an `/artifacts/<slug>` link in their markdown opens in the host's
 * side panel, not a new browser tab. The pane's contract is the plumbing --
 * `onArtifactOpen` reaching each card's `MarkdownRenderer` -- so the renderer is
 * stubbed to a link that calls what it was handed. */

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
      href="/artifacts/release-checklist"
      data-testid="artifact-link"
      data-wired={onArtifactOpen ? 'yes' : 'no'}
      onClick={(e) => { e.preventDefault(); onArtifactOpen?.('release-checklist') }}
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

const LINK = '[Release checklist](/artifacts/release-checklist)'

// Each card draws its markdown only once open: a failed sub-agent opens
// expanded, a skill card on its toggle, a multi-skill row on its name, and a
// delivered `session_send` sits in the turn's tool group until that opens.
const CARDS = [
  {
    card: 'sub-agent completion card',
    row: {
      role: 'subagent', cls: '', ts: '2026-10-09T03:00:05Z',
      content: `[Subagent completion event]\nAgent \`53e3e5eb\` (kirocrew) failed ❌\nTask: Draft the release checklist\n\nSubagent result: ${LINK}`,
    },
    open: () => {},
  },
  {
    card: 'skill load card',
    row: {
      role: 'system', cls: '', ts: '2026-10-09T03:00:05Z', content: 'Loaded skill: release',
      meta: { kind: 'skill_load', skills: [{ name: 'release', body: `Skill body links ${LINK}` }] },
    },
    open: () => fireEvent.click(screen.getByTestId('skill-load-card-toggle')),
  },
  {
    card: 'skill load card, multi-skill row',
    row: {
      role: 'system', cls: '', ts: '2026-10-09T03:00:05Z', content: 'Loaded skills: alpha, beta',
      meta: { kind: 'skill_load', skills: [{ name: 'alpha', body: `Alpha links ${LINK}` }, { name: 'beta', body: 'Beta body' }] },
    },
    open: () => {
      fireEvent.click(screen.getByTestId('skill-load-card-toggle'))
      fireEvent.click(screen.getByRole('button', { name: 'alpha' }))
    },
  },
  {
    card: 'session message card',
    row: {
      role: 'tool', cls: '', ts: '2026-10-09T03:00:05Z', content: '🔧 Session send',
      meta: {
        tool_name: 'session_send', mcp_server: 'kirocrew-dashboard',
        input: JSON.stringify({ target: 'chat-9', message: `Session message links ${LINK}` }),
        sessionSend: { outcome: 'delivered', target: 'chat-9' },
      },
    },
    open: () => {
      const group = screen.queryByText(/^(Worked through \d+ steps?|\d+ tool calls?)$/)
      if (group) fireEvent.click(group)
    },
  },
]

const USER = { role: 'user', content: 'Where is the checklist?', cls: '', ts: '2026-10-09T03:00:00Z' }

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

async function renderPane(slotKey: string, row: Record<string, unknown>, extraProps: Record<string, unknown>) {
  const messages = [USER, row]
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages, running: false, has_more: false, total: messages.length })
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
  // Hydration is settled once the user's row paints.
  await waitFor(() => expect(screen.getByText(USER.content)).toBeTruthy())
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane card artifact open', () => {
  it.each(CARDS)('the $card opens the artifact through the host handler', async ({ card, row, open }) => {
    const onArtifactOpen = vi.fn()
    await renderPane(`pane-card-${card}`, row, { onArtifactOpen })
    open()
    fireEvent.click(await screen.findByTestId('artifact-link'))
    expect(onArtifactOpen).toHaveBeenCalledWith('release-checklist')
  })
})

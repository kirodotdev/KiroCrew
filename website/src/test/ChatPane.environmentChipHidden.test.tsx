import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { render } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A bot's session opened in the bot page's side panel is a ChatPane told to hide
 * the environment chip (`environmentChipHidden`): the pane hands that to its
 * composer with the slot, so the chip is not drawn while the placement read
 * still gates the local project controls. A pane told nothing keeps the chip. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
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

const seen = vi.hoisted(() => ({ props: null as Record<string, unknown> | null }))
vi.mock('../components/ChatInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ChatInput')>()
  return { ...actual, default: (props: Record<string, unknown>) => { seen.props = props; return null } }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

function renderPane(environmentChipHidden?: boolean, mode = '') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: 'chat-w', messages: 0, running: false, mode, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey="chat-w" environmentChipHidden={environmentChipHidden} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ChatPane environmentChipHidden', () => {
  it('hands the composer the slot with the chip hidden', () => {
    renderPane(true)
    expect(seen.props!.environmentSlot).toBe('chat-w')
    expect(seen.props!.environmentChipHidden).toBe(true)
  })

  it('keeps the chip for a pane told nothing', () => {
    renderPane()
    expect(seen.props!.environmentSlot).toBe('chat-w')
    expect(seen.props!.environmentChipHidden).toBe(false)
  })

  // Split view mounts ChatPane with no prop: a bot's own thread picked there
  // still draws no chip, while its placement is still read.
  it("hides the chip for a bot's own thread whatever the host passes", () => {
    renderPane(undefined, 'member')
    expect(seen.props!.environmentSlot).toBe('chat-w')
    expect(seen.props!.environmentChipHidden).toBe(true)
  })
})

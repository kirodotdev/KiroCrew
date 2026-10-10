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
import { EARLIER_CONVERSATION_ROLE, foldEarlierConversation } from '../components/EarlierConversation'
import type { ChatMessage } from '../types'

/* A fresh start on a crewmate's thread folds the rows written before it under
 * one closed "Earlier conversation" divider. The rows are not removed: the
 * divider opens them again, and nothing is written anywhere. */

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
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const RESET_AT = '2026-10-10T06:00:00+00:00'
const MESSAGES: ChatMessage[] = [
  { role: 'user', content: 'old question', cls: '', ts: '2026-10-10T05:00:00+00:00' },
  { role: 'assistant', content: 'old answer', cls: '', ts: '2026-10-10T05:00:05+00:00' },
  { role: 'user', content: 'new question', cls: '', ts: '2026-10-10T06:01:00+00:00' },
  { role: 'assistant', content: 'new answer', cls: '', ts: '2026-10-10T06:01:05+00:00' },
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
  await act(async () => {
    render(
      <Provider store={makeStore(slotKey)}>
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
  await waitFor(() => expect(screen.getByText('new answer')).toBeTruthy())
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('foldEarlierConversation', () => {
  it('returns the same list when there is no fresh start', () => {
    expect(foldEarlierConversation(MESSAGES, undefined, false)).toBe(MESSAGES)
  })

  it('returns the same list when nothing is older than the fresh start', () => {
    expect(foldEarlierConversation(MESSAGES, '2026-10-10T04:00:00+00:00', false)).toBe(MESSAGES)
  })

  it('closed: one divider row, then only the new rows', () => {
    const out = foldEarlierConversation(MESSAGES, RESET_AT, false)
    expect(out.map(m => m.role)).toEqual([EARLIER_CONVERSATION_ROLE, 'user', 'assistant'])
    expect(out[0].meta).toEqual({ count: 2, at: RESET_AT })
  })

  it('open: the earlier rows come back above the divider', () => {
    const out = foldEarlierConversation(MESSAGES, RESET_AT, true)
    expect(out.map(m => m.content)).toEqual(['old question', 'old answer', '', 'new question', 'new answer'])
  })

  it('a row with no time counts as new', () => {
    const live: ChatMessage = { role: 'assistant', content: 'live', cls: '' }
    const out = foldEarlierConversation([...MESSAGES, live], RESET_AT, false)
    expect(out[out.length - 1]).toBe(live)
  })
})

describe('ChatPane fresh-start fold', () => {
  it('without foldBefore every row is drawn and there is no divider', async () => {
    await renderPane('member-fold-1')
    expect(screen.getByText('old answer')).toBeTruthy()
    expect(screen.queryByTestId('earlier-conversation')).toBeNull()
  })

  it('folds the earlier rows behind a closed divider that opens them again', async () => {
    await renderPane('member-fold-2', { foldBefore: RESET_AT })
    expect(screen.queryByText('old answer')).toBeNull()
    const toggle = screen.getByTestId('earlier-conversation-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.textContent).toContain('Show earlier messages (2)')
    expect(toggle.textContent).toContain('Started fresh')

    fireEvent.click(toggle)
    expect(screen.getByText('old answer')).toBeTruthy()
    expect(screen.getByTestId('earlier-conversation-toggle').getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByTestId('earlier-conversation-toggle').textContent).toContain('Hide earlier messages')

    fireEvent.click(screen.getByTestId('earlier-conversation-toggle'))
    expect(screen.queryByText('old answer')).toBeNull()
  })

  it('a crewmate pane folds the same way', async () => {
    await renderPane('member-fold-3', {
      foldBefore: RESET_AT,
      busyMode: 'steer-only',
      frameless: true,
      agentLocked: true,
      crewmate: { name: 'atlas', label: 'Atlas' },
    })
    expect(screen.queryByText('old answer')).toBeNull()
    expect(screen.getByTestId('earlier-conversation')).toBeTruthy()
  })
})

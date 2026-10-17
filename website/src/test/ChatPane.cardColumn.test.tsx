import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setQuestionCard } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The pending ask_question card sits in the pane's card column, clamped to
 * --mc-content-width with the same px-4 gutter as the messages above and the
 * composer below. Outside that column it spanned the whole pane (measured at
 * 1440px: x=509..1431 while messages and composer sat at ~579..1361).
 *
 * Guide offers are NOT in that column: each is a `card` row of the
 * conversation, drawn in the transcript where it was offered,
 * and a reload (the slot-detail read below) puts it back in the same place. */

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
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'
import { installApiTransport } from '../api/apiTransport'

// `../api/client` is mocked above, so the shared transport it installs at load
// is installed here instead: the card store's read goes to msw like the app's.
const send = (method: string) => (url: string, body?: object) =>
  fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
installApiTransport({
  get: url => fetch(url), post: send('POST'), put: send('PUT'), del: send('DELETE'), patch: send('PATCH'),
  j: r => r.json(), jNullable: async r => (r.status === 204 ? null : r.json()),
})

// Unique per test: the pane parks its composer draft per slot in a module
// store that outlives the render, so a shared key would leak drafts between tests.
let SLOT = 'chat-card-column-0'
let slotSeq = 0

function renderPane() {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  store.dispatch(setQuestionCard({
    slot: SLOT,
    ask_id: 'ask-1',
    questions: [{ question: 'Pick a trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] }],
  }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} frameless />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  SLOT = `chat-card-column-${++slotSeq}`
})

describe('ChatPane card column', () => {
  it('renders the pending question card inside the clamped card column', async () => {
    const view = renderPane()
    const question = await view.findByText('Pick a trust model')
    const column = view.getByTestId('chat-card-column')
    expect(column).toContainElement(question)
    // The column is the card's only gutter and width clamp.
    expect(column).toHaveClass('px-4', 'mx-auto', 'w-full')
    expect(column.style.maxWidth).toBe('var(--mc-content-width, 900px)')
    // Exactly one column: the card did not get a second, nested wrapper that
    // would double the px-4 gutter.
    expect(view.getAllByTestId('chat-card-column')).toHaveLength(1)
    expect(column.querySelector('[data-testid="chat-card-column"]')).toBeNull()
  })
})

describe('a guide offer is part of the conversation', () => {
  it('draws the offer at its row in the transcript, between the messages around it, and not above the composer', async () => {
    server.use(http.get('/api/guide/pending', () => HttpResponse.json({ guides: [] })))
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      messages: [
        { role: 'user', content: 'where is the theme', cls: '', ts: '2026-10-03T10:00:00Z', meta: { mid: 'm1' } },
        { role: 'card', content: 'settings.show', cls: 'msg msg-card', ts: '2026-10-03T10:00:01Z',
          meta: { mid: 'm2', card: { surface: 'guide', id: 'g_inline1', slot: SLOT, kind: 'settings.show', status: 'completed' } } },
        { role: 'assistant', content: 'It is under Settings, Display.', cls: '', ts: '2026-10-03T10:00:02Z', meta: { mid: 'm3' } },
      ],
      running: false, has_more: false, total: 3,
    })
    const view = renderPane()
    const drawn = await view.findByTestId('conversation-card')
    const column = view.getByTestId('chat-card-column')
    expect(column.contains(drawn)).toBe(false)
    // In the transcript, after the user message and before the reply.
    const before = view.getByText('where is the theme')
    const after = view.getByText('It is under Settings, Display.')
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING
    expect(before.compareDocumentPosition(drawn) & FOLLOWING).toBeTruthy()
    expect(drawn.compareDocumentPosition(after) & FOLLOWING).toBeTruthy()
    // And the column above the composer comes after the whole transcript.
    expect(after.compareDocumentPosition(column) & FOLLOWING).toBeTruthy()
  })
})

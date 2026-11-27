import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, waitFor, fireEvent, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A crewmate's chat draws a queued message as the person's own bubble with no
 * controls, because the next drain sends it. A held entry (the server's
 * `meta.held`) is never drained, so it keeps the queue card with send now, edit
 * and cancel. A restored entry the server will still drain is not held. */

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
    cancelQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    editQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    holdQueuedMessageEdit: vi.fn().mockResolvedValue({ ok: true }),
    interruptSlot: vi.fn().mockResolvedValue({ queue_selected: true }),
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
import { takePaneDraft } from '../utils/chatPaneDrafts'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'

const SLOT = 'member-radar'
const HISTORY = [
  { role: 'user', content: 'first words', cls: 'msg msg-u', ts: '2026-09-22T06:00:00Z' },
  { role: 'assistant', content: 'a reply', cls: '', ts: '2026-09-22T06:00:09Z' },
]

function renderPane(slotKey = SLOT) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: HISTORY.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (key: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={key} crewmate={{ name: 'Radar' }} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(tree(slotKey))
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  // Drop any one-shot answer a test left unconsumed.
  for (const fn of [api.cancelQueuedMessage, api.editQueuedMessage]) (fn as ReturnType<typeof vi.fn>).mockReset().mockResolvedValue({ ok: true })
  ;(api.interruptSlot as ReturnType<typeof vi.fn>).mockReset().mockResolvedValue({ queue_selected: true })
  // A cancel puts the entry's words in the composer, and an unmounted pane
  // parks its composer per slot: each test starts with an empty one.
  sessionStorage.clear()
  takePaneDraft(SLOT)
  queuedSendStash.clear()
  ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: HISTORY, running: false, has_more: false, total: HISTORY.length,
    queue: [
      { id: 'held-1', content: 'held words', meta: { restored: true, held: true } },
      { id: 'drain-1', content: 'restored drained words', meta: { restored: true } },
      { id: 'live-1', content: 'live words' },
    ],
  })
})


describe("a held queued message in a mate's chat", () => {
  it('keeps the queue card with send now, edit and cancel', async () => {
    const view = renderPane()
    await view.findByText('held words')
    expect(view.getAllByLabelText('Send now')).toHaveLength(1)
    expect(view.getAllByLabelText('Edit queued message')).toHaveLength(1)
    expect(view.getAllByLabelText('Cancel queued message')).toHaveLength(1)
  })

  it('cancels the restored entry through the queue endpoint', async () => {
    const view = renderPane()
    await view.findByText('held words')
    fireEvent.click(view.getByLabelText('Cancel queued message'))
    await waitFor(() => expect(api.cancelQueuedMessage).toHaveBeenCalledWith(SLOT, 'held-1'))
  })

  it('sends the held entry now through the interrupt endpoint', async () => {
    const view = renderPane()
    // Read inside the card: a cancel in another test puts the same words back
    // in the slot's composer draft.
    const card = await view.findByTestId('queue-card')
    fireEvent.click(within(card).getByLabelText('Send now'))
    await waitFor(() => expect(api.interruptSlot).toHaveBeenCalledWith(SLOT, 'held-1'))
  })

  it('offers only Send now on a held entry whose row is already written', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: HISTORY, running: false, has_more: false, total: HISTORY.length,
      queue: [{ id: 'held-1', content: 'held words', meta: { restored: true, held: true, written: true } }],
    })
    const view = renderPane()
    const card = await view.findByTestId('queue-card')
    expect(within(card).getAllByLabelText('Send now')).toHaveLength(1)
    expect(within(card).queryByLabelText('Edit queued message')).toBeNull()
    expect(within(card).queryByLabelText('Cancel queued message')).toBeNull()
  })


  it('draws an ordinary queued message as a bubble with no controls', async () => {
    const view = renderPane()
    const bubble = await view.findByText('live words')
    expect(bubble.closest('[data-testid="queue-card"]')).toBeNull()
  })

  it('draws a restored entry the server will drain as a bubble with no controls', async () => {
    const view = renderPane()
    const bubble = await view.findByText('restored drained words')
    expect(bubble.closest('[data-testid="queue-card"]')).toBeNull()
    expect(view.getAllByTestId('queue-card')).toHaveLength(1)
  })
})

import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { selectSlotMessages } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Issue #5891, ChatPane half. Two behaviours the single-chat surface had and the
 * split pane did not, because the queue-action callbacks were copy-mirrored per
 * host and the copies drifted:
 *
 *  1. CANCEL DESTROYED THE DRAFT. ChatPage.handleCancelQueued restored the card's
 *     text to the composer; ChatPane.onCancelQueued did not, so cancelling a
 *     queued message from a split pane lost it with no way back.
 *  2. NO IN-FLIGHT LATCH. Neither host threaded QueueStack's `pendingIds`, so the
 *     Zap button — which has no optimistic dispatch and therefore leaves its card
 *     sitting unchanged under the cursor — fired one request per click.
 *
 * Both now come from useQueuedMessageActions, so these pin the pane's wiring of
 * it. Mutation checks: drop `restoreDraft` from the pane's hook options -> test 1
 * RED; drop `pendingIds` from the pane's QueueStack render -> tests 2 and 3 RED.
 */

const deferred = () => {
  let resolve!: (v?: unknown) => void
  const promise = new Promise<unknown>((res) => { resolve = res as typeof resolve })
  return { promise, resolve }
}

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({
      messages: [
        { role: 'queued', content: 'queued draft', cls: 'msg msg-queued', ts: '', meta: { queueId: 'q-77' } },
      ],
      running: true,
      has_more: false,
      total: 1,
    }),
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
    editQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    cancelQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    interruptSlot: vi.fn().mockResolvedValue({ ok: true }),
    reorderQueuedMessages: vi.fn().mockResolvedValue({ ok: true }),
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
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

/** The pane's composer, reduced to the one prop these tests read. Keeping the
 *  real one would make the assertion a query for a textarea the queue's own
 *  EditInput also matches. */
let paneInputValue: string | null = null
vi.mock('../components/ChatInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ChatInput')>()
  const React = await import('react')
  return {
    ...actual,
    default: (props: { value: string; onChange: (v: string) => void }) => {
      paneInputValue = props.value
      return React.createElement('textarea', {
        'aria-label': 'Pane message input',
        value: props.value,
        onChange: (e: { target: { value: string } }) => props.onChange(e.target.value),
      })
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function renderPane(slotKey: string, project?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: true, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...(project ? { project } : {}) }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  return Object.assign(render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  ), { store })
}

beforeEach(() => {
  vi.clearAllMocks()
  __resetPaneDraftsForTests()
  vi.mocked(api.chatSlotDetail).mockReset().mockResolvedValue({
    messages: [{ role: 'queued', content: 'queued draft', cls: 'msg msg-queued', ts: '', meta: { queueId: 'q-77' } }],
    running: true, has_more: false, total: 1,
  } as never)
  vi.mocked(api.cancelQueuedMessage).mockReset().mockResolvedValue({ ok: true } as never)
  vi.mocked(api.interruptSlot).mockReset().mockResolvedValue({ ok: true } as never)
  paneInputValue = null
})

describe('ChatPane queue actions (issue #5891)', () => {
  it('cancelling a queued card hands its text back to this pane composer instead of losing it', async () => {
    const { store } = renderPane('pane-cancel')
    const x = await screen.findByRole('button', { name: 'Cancel queued message' })
    fireEvent.click(x)

    await waitFor(() => expect(paneInputValue).toBe('queued draft'))
    expect(api.cancelQueuedMessage).toHaveBeenCalledWith('pane-cancel', 'q-77')
    // Optimistic removal is unchanged — only the restore is new.
    const msgs = selectSlotMessages(store.getState() as RootState, 'pane-cancel')
    expect(msgs.find((m) => m.meta?.queueId === 'q-77')).toBeUndefined()
  })

  it('keeps the queue card controls disabled through an interrupt, and past its response', async () => {
    const d = deferred()
    vi.mocked(api.interruptSlot).mockReturnValue(d.promise as ReturnType<typeof api.interruptSlot>)
    renderPane('pane-latch')
    const zap = await screen.findByRole('button', { name: 'Send now' })
    fireEvent.click(zap)

    // Interrupt has no optimistic dispatch, so the card stays put; the latch is
    // the only thing standing between a double-click and a duplicate request.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send now' })).toBeDisabled())
    expect(screen.getByRole('button', { name: 'Cancel queued message' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Edit queued message' })).toBeDisabled()

    await act(async () => { d.resolve({ ok: true }) })
    // STILL disabled. The 200 means the entry is being promoted, not that the card
    // is finished: it goes away with the queue_pop frame. Re-enabling here would
    // hand back a button whose next click interrupts the turn this click started.
    expect(screen.getByRole('button', { name: 'Send now' })).toBeDisabled()
  })

  it('swallows a second interrupt click on a latched card', async () => {
    const d = deferred()
    vi.mocked(api.interruptSlot).mockReturnValue(d.promise as ReturnType<typeof api.interruptSlot>)
    renderPane('pane-latch-2')
    const zap = await screen.findByRole('button', { name: 'Send now' })
    fireEvent.click(zap)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send now' })).toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Send now' }))

    expect(api.interruptSlot).toHaveBeenCalledTimes(1)
    await act(async () => { d.resolve({ ok: true }) })
  })

  it('a second cancelled card does not reuse a mention spelling the pane already stages (#14617)', async () => {
    // The pane merges each restore into its composer, so it hands the hook
    // its staged files: the second card's `y/a.txt` must not come back as
    // `@a.txt` beside the first card's `x/a.txt`, or the next send binds
    // both mentions to one file.
    const queuedRow = (queueId: string, content: string, file: string) =>
      ({ role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId, files: [file] } })
    vi.mocked(api.chatSlotDetail).mockResolvedValueOnce({
      messages: [
        queuedRow('q-x', 'compare [attached_file 1] /repo/x/a.txt', '/repo/x/a.txt'),
        queuedRow('q-y', 'against [attached_file 1] /repo/y/a.txt', '/repo/y/a.txt'),
      ],
      running: true,
      has_more: false,
      total: 2,
    } as unknown as Awaited<ReturnType<typeof api.chatSlotDetail>>)
    renderPane('pane-same-name')
    // Two cards stack collapsed; expanding the stack shows each card's actions.
    fireEvent.click(await screen.findByText('compare [attached_file 1] /repo/x/a.txt'))
    const cancelOf = (text: string) =>
      within(screen.getByText(text).parentElement!).getByRole('button', { name: 'Cancel queued message' })
    fireEvent.click(await waitFor(() => cancelOf('compare [attached_file 1] /repo/x/a.txt')))
    await waitFor(() => expect(paneInputValue).toBe('compare @a.txt'))
    fireEvent.click(await waitFor(() => cancelOf('against [attached_file 1] /repo/y/a.txt')))
    await waitFor(() => expect(paneInputValue).toContain('against @y/a.txt'))
    expect(paneInputValue).toContain('compare @a.txt')
  })

  it('spells a restored mention of a file in the pane project relative to that project (#14617)', async () => {
    // The pane hands the hook its slot's project, so a file at src/a.ts in
    // it comes back as the `@src/a.ts` its own picker would insert.
    vi.mocked(api.chatSlotDetail).mockResolvedValueOnce({
      messages: [{
        role: 'queued', content: 'check [attached_file 1] /repo/main/src/a.ts', cls: 'msg msg-queued', ts: '',
        meta: { queueId: 'q-p', files: ['/repo/main/src/a.ts'] },
      }],
      running: true,
      has_more: false,
      total: 1,
    } as unknown as Awaited<ReturnType<typeof api.chatSlotDetail>>)
    renderPane('pane-project', '/repo/main')
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel queued message' }))
    await waitFor(() => expect(paneInputValue).toBe('check @src/a.ts'))
  })
})

// The cold-mount response carries queue separately from persisted messages.
import { afterEach } from 'vitest'
import { appendQueuedMessage, cancelQueuedMessage, removeQueuedMessage } from '../store/chatSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

const COLD_SLOT = 'member-cold-queue'
const COLD_TS = '2026-10-14T08:00:00Z'
const coldQueue = { id: 'cold-q', content: 'accepted before reload', meta: { sendId: 'cold-send' } }
const coldDetail = { messages: [], queue: [coldQueue], running: false, has_more: false, total: 0 }
const coldPush = { slot: COLD_SLOT, queue_id: coldQueue.id, content: coldQueue.content, meta: coldQueue.meta, ts: COLD_TS }

afterEach(() => vi.useRealTimers())

describe('cold-mount queue from slot detail', () => {
  beforeEach(() => __resetPaneDraftsForTests())

  it('draws a crewmate queued entry as one plain bubble, never a queue card', async () => {
    vi.setSystemTime(new Date(COLD_TS))
    vi.mocked(api.chatSlotDetail).mockReset().mockResolvedValue(coldDetail as never)
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
      preloadedState: { dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true } },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Provider store={store}><QueryClientProvider client={qc}><ThemeProvider><MemoryRouter>
      <ChatPane slotKey={COLD_SLOT} crewmate={{ name: 'Radar' }} />
    </MemoryRouter></ThemeProvider></QueryClientProvider></Provider>)
    expect(await screen.findByText(coldQueue.content)).toBeInTheDocument()
    act(() => { store.dispatch(appendQueuedMessage(coldPush)) })
    expect(screen.getAllByText(coldQueue.content)).toHaveLength(1)
    expect(screen.queryByTestId('queue-card')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel queued message' })).toBeNull()
  })

  it.each([false])('restores one cancellable card (crewmate=%s)', async (crewmate) => {
    vi.setSystemTime(new Date(COLD_TS))
    vi.mocked(api.chatSlotDetail).mockReset().mockResolvedValue(coldDetail as never)
    vi.mocked(api.cancelQueuedMessage).mockReset().mockResolvedValue({ ok: true } as never)
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
      preloadedState: { dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true } },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Provider store={store}><QueryClientProvider client={qc}><ThemeProvider><MemoryRouter>
      <ChatPane slotKey={COLD_SLOT} crewmate={crewmate ? { name: 'Radar' } : undefined} />
    </MemoryRouter></ThemeProvider></QueryClientProvider></Provider>)
    const cancel = await screen.findByRole('button', { name: 'Cancel queued message' })
    expect(screen.getAllByText(coldQueue.content)).toHaveLength(1)
    expect(selectSlotMessages(store.getState() as RootState, COLD_SLOT)).toMatchObject([
      { role: 'queued', meta: { queueId: coldQueue.id, sendId: 'cold-send' } },
    ])
    act(() => { store.dispatch(appendQueuedMessage(coldPush)) })
    expect(screen.getAllByText(coldQueue.content)).toHaveLength(1)
    fireEvent.click(cancel)
    await waitFor(() => expect(api.cancelQueuedMessage).toHaveBeenCalledWith(COLD_SLOT, coldQueue.id))
    await waitFor(() => expect(paneInputValue).toBe(coldQueue.content))
    expect(selectSlotMessages(store.getState() as RootState, COLD_SLOT)).toHaveLength(0)
    expect(screen.queryByRole('button', { name: 'Cancel queued message' })).toBeNull()
  })

  it.each(['push', 'pop', 'cancel'] as const)('preserves a %s frame arriving before the cold response', async (event) => {
    vi.setSystemTime(new Date(COLD_TS))
    const d = deferred()
    vi.mocked(api.chatSlotDetail).mockReset().mockReturnValue(d.promise as ReturnType<typeof api.chatSlotDetail>)
    const { store } = renderPane(COLD_SLOT)
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
    act(() => {
      if (event === 'push') store.dispatch(appendQueuedMessage({ ...coldPush, queue_id: 'new-q', content: 'newer request', meta: { sendId: 'new-send' } }))
      if (event === 'pop') store.dispatch(removeQueuedMessage(coldPush))
      if (event === 'cancel') store.dispatch(cancelQueuedMessage({ slot: COLD_SLOT, queue_id: coldQueue.id }))
    })
    await act(async () => { d.resolve(coldDetail) })
    await waitFor(() => expect(store.getState().chat.slotHydrated[COLD_SLOT]).toBe(true))
    const rows = selectSlotMessages(store.getState() as RootState, COLD_SLOT)
    expect(rows.filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(event === 'push' ? ['cold-q', 'new-q'] : [])
    if (event === 'pop') {
      expect(rows).toMatchObject([{ role: 'user', content: coldQueue.content, meta: coldQueue.meta }])
      expect(screen.getAllByText(coldQueue.content)).toHaveLength(1)
    }
    if (event === 'cancel') expect(screen.queryByText(coldQueue.content)).toBeNull()
  })
})
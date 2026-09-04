import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
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

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: true, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
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
})

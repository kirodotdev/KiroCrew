import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { selectSlotMessages } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Regression for issue #2240: ChatPane rendered QueueStack with onCancel and
 * onInterrupt only — never onEdit — so the Pencil button (gated on
 * `onEdit && showActions` inside QueueStack) could not appear in split-pane
 * view even though the store action, api client method, backend PATCH handler,
 * and WS reconciliation were all wired. These tests pin that ChatPane threads
 * onEdit through: the edit affordance must be reachable and must call
 * api.editQueuedMessage with this pane's slot and the card's queue id.
 * Mutation check: removing the `onEdit` prop from ChatPane's QueueStack
 * render makes the Pencil query below fail. */

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

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function makeStore(slotKey: string) {
  return configureStore({
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
}

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
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
})

describe('ChatPane queue inline edit (issue #2240)', () => {
  it('threads onEdit into QueueStack: the Pencil renders and a committed edit calls api.editQueuedMessage with this slot and queue id', async () => {
    const { store } = renderPane('pane-edit')
    // The Pencil is gated on `onEdit && showActions` inside QueueStack — it can
    // only appear when ChatPane passes onEdit, which is the omission #2240 pins.
    // Role-scoped queries: the button and the EditInput share the same
    // accessible name, so match by role rather than bare label text.
    const pencil = await screen.findByRole('button', { name: 'Edit queued message' })
    fireEvent.click(pencil)
    const input = (await screen.findByRole('textbox', { name: 'Edit queued message' })) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'edited from split pane' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.editQueuedMessage).toHaveBeenCalledTimes(1))
    expect(api.editQueuedMessage).toHaveBeenCalledWith('pane-edit', 'q-77', 'edited from split pane')
    // Optimistic store update mirrors ChatPage.handleEditQueued.
    const msgs = selectSlotMessages(store.getState() as RootState, 'pane-edit')
    const card = msgs.find((m) => m.meta?.queueId === 'q-77')
    expect(card?.content).toBe('edited from split pane')
  })

  it('trims the committed content before dispatching (mirrors ChatPage.handleEditQueued)', async () => {
    renderPane('pane-edit-2')
    const pencil = await screen.findByRole('button', { name: 'Edit queued message' })
    fireEvent.click(pencil)
    const input = (await screen.findByRole('textbox', { name: 'Edit queued message' })) as HTMLInputElement
    fireEvent.change(input, { target: { value: '  padded edit  ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.editQueuedMessage).toHaveBeenCalledTimes(1))
    // QueueStack hands the raw value through; ChatPane's callback owns the trim.
    expect(api.editQueuedMessage).toHaveBeenCalledWith('pane-edit-2', 'q-77', 'padded edit')
  })
})

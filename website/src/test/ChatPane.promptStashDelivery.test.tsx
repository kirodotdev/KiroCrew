import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, sseChatMessage } from '../store/chatSlice'
import type { SendReceipt } from '../chat-core/transport/sendTurn'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { addStashEntry, loadPromptStash, makeStashEntry, withPromptStashLock } from '../utils/promptStash'

/* A split-pane draft is pane-local: a failed send hands it back into the
 * composer, but nothing writes it to storage. So a draft restored from the
 * prompt stash must keep its stash entry until the send is CONFIRMED, not
 * when the composer clears optimistically -- or a late refusal followed by a
 * reload loses it. */

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

/** Every send waits for the test to hand it a receipt, so the optimistic
 *  composer clear and the late outcome are separate, ordered steps. */
let settleSend: ((receipt: SendReceipt) => void) | null = null
vi.mock('../chat-core/transport/sendTurn', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../chat-core/transport/sendTurn')>()
  return {
    ...actual,
    sendTurn: () => new Promise<SendReceipt>((resolve) => { settleSend = resolve }),
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'pane-1'

function renderPane(running = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running, subagents_running: false, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  store.dispatch(setActiveSlot('front'))
  // A streamed chunk is what makes the pane's main turn "running", so Enter
  // in a steer-only pane steers.
  if (running) store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'working…', seq: 1 }))
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} {...(running ? { busyMode: 'steer-only' as const } : {})} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

/** A lock granted now runs after every stash mutation queued before it. */
const flushStash = () => act(async () => { await withPromptStashLock(() => undefined) })

async function restoreAndSend(running = false) {
  expect(addStashEntry(SLOT, makeStashEntry('restored draft', []))).toBe(true)
  const r = renderPane(running)
  const box = (await screen.findAllByRole('textbox'))[0]
  fireEvent.keyDown(box, { key: 's', ctrlKey: true })
  await waitFor(() => expect(box).toHaveValue('restored draft'))
  fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
  await waitFor(() => expect(box).toHaveValue(''))
  await waitFor(() => expect(settleSend).not.toBeNull())
  await flushStash()
  return { r, box }
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPaneDraftsForTests()
  settleSend = null
})

describe('ChatPane prompt stash: a restored draft is consumed on confirmed delivery', () => {
  it('keeps the entry in storage while the send is in flight and after a late refusal, across a reload', async () => {
    const { r, box } = await restoreAndSend()
    // In flight: the optimistic clear is not delivery.
    expect(loadPromptStash(SLOT).map(e => e.text)).toEqual(['restored draft'])

    await act(async () => { settleSend!({ status: 'refused', body: {}, reason: 'Service Unavailable' }) })
    await flushStash()
    // The pane hands the text back into its (non-durable) composer...
    await waitFor(() => expect(box).toHaveValue('restored draft'))
    // ...and the stash still holds it, which is what a reload reads.
    r.unmount()
    expect(loadPromptStash(SLOT).map(e => e.text)).toEqual(['restored draft'])
  })

  it('keeps the entry when delivery stays unconfirmed past the deadline', async () => {
    const { r } = await restoreAndSend()
    await act(async () => { settleSend!({ status: 'response-late', body: {} }) })
    await flushStash()
    r.unmount()
    expect(loadPromptStash(SLOT).map(e => e.text)).toEqual(['restored draft'])
  })

  it('removes the entry once the server confirms the send', async () => {
    await restoreAndSend()
    await act(async () => { settleSend!({ status: 'dispatched', body: { ok: true } }) })
    await flushStash()
    await waitFor(() => expect(loadPromptStash(SLOT)).toEqual([]))
  })

  it('keeps the entry through a refused mid-turn steer', async () => {
    const { r, box } = await restoreAndSend(true)
    expect(loadPromptStash(SLOT).map(e => e.text)).toEqual(['restored draft'])
    await act(async () => { settleSend!({ status: 'refused', body: {}, reason: 'Service Unavailable' }) })
    await flushStash()
    await waitFor(() => expect(box).toHaveValue('restored draft'))
    r.unmount()
    expect(loadPromptStash(SLOT).map(e => e.text)).toEqual(['restored draft'])
  })

  it('removes the entry once the server confirms a mid-turn steer', async () => {
    await restoreAndSend(true)
    await act(async () => { settleSend!({ status: 'dispatched', body: { ok: true, steered: true } }) })
    await flushStash()
    await waitFor(() => expect(loadPromptStash(SLOT)).toEqual([]))
  })
})

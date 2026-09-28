import type { ReactNode } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Selecting text in a pane's assistant reply used to offer Copy only: the SDK
 * assistant row draws the selection actions its host hands it, and ChatPage
 * was the only host handing any (#quote/#ask lived in ChatPage). These tests
 * pin the pane's wiring through the shared chat-core seam
 * (chat-core/composer/selectionActions): Quote is always there and lands in
 * THIS pane's composer with the transit flight; Ask appears exactly when the
 * host can bring a Side Chat on screen, opens it for THIS pane's slot, and
 * seeds it with a slot-named event — the main draft untouched. */

type AssistantProps = {
  content: string
  onQuote?: (text: string, rect: DOMRect) => void
  onAsk?: (text: string) => void
}
let assistantProps: AssistantProps | null = null
vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: AssistantProps) => { assistantProps = props; return <div data-testid="assistant-stub">{props.content}</div> },
}))
vi.mock('../components/FlyingQuote', () => ({
  default: ({ text }: { text: string }) => <div data-testid="flying-quote">{text}</div>,
}))
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
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false }),
    planAction: vi.fn().mockResolvedValue({ ok: true }),
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
import { clearSideChatDrafts, readSideChatDraft } from '../chat-core/composer/sideChatDrafts'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

const SLOT = 'pane-slot'
const PANE_MESSAGES = [
  { role: 'user', content: 'hi', ts: '2026-09-06T00:00:00Z' },
  { role: 'assistant', content: 'Selectable reply.', ts: '2026-09-06T00:00:01Z' },
]

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      // No active slot is set: the pane under test is not the active slot,
      // which is the split-view case the slot-named seed exists for.
    } as Partial<RootState>,
  })
}

async function renderPane(props: { openSideChat?: (slot: string) => void } = {}) {
  assistantProps = null
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: PANE_MESSAGES, running: false, has_more: false, total: PANE_MESSAGES.length })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore()
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} {...props} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  await waitFor(() => expect(screen.getByTestId('assistant-stub')).toHaveTextContent('Selectable reply.'))
  await waitFor(() => expect(assistantProps).not.toBeNull())
  return store
}

const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

describe('ChatPane selection actions (Quote / Ask)', () => {
  // Every test mounts the same SLOT; the pane parks its composer on unmount
  // (RTL's auto-cleanup, which runs after this file's afterEach) into a
  // module-level store, so the reset must happen at the START of the next
  // test or one test's Quote text rides into the next test's composer.
  beforeEach(() => { __resetPaneDraftsForTests() })
  afterEach(() => { document.body.replaceChildren(); clearSideChatDrafts() })

  it('hands the assistant row a Quote action even when the host offers no Side Chat', async () => {
    await renderPane()
    expect(typeof assistantProps!.onQuote).toBe('function')
    // No Side Chat surface → no Ask. An Ask that fires into the void would
    // be worse than none (capability by omission, like onOpenFull).
    expect(assistantProps!.onAsk).toBeUndefined()
  })

  it('Quote lands in THIS pane\'s composer as a blockquote and starts the transit flight', async () => {
    await renderPane()
    const rect = { top: 10, left: 20, width: 5, height: 5 } as DOMRect
    act(() => { assistantProps!.onQuote!('first line\nsecond line', rect) })
    await waitFor(() => expect(composer().value).toBe('> first line\n> second line\n\n'))
    expect(screen.getByTestId('flying-quote')).toHaveTextContent('first line')
  })

  it('a second Quote stacks below the first rather than replacing it', async () => {
    await renderPane()
    const rect = { top: 0, left: 0, width: 1, height: 1 } as DOMRect
    act(() => { assistantProps!.onQuote!('alpha', rect) })
    await waitFor(() => expect(composer().value).toContain('> alpha'))
    act(() => { assistantProps!.onQuote!('beta', rect) })
    await waitFor(() => expect(composer().value).toBe('> alpha\n\n> beta\n\n'))
  })

  it('Ask appears with a host Side Chat opener, opens it for THIS pane\'s slot and seeds THIS slot\'s draft — composer untouched', async () => {
    const openSideChat = vi.fn()
    await renderPane({ openSideChat })
    expect(typeof assistantProps!.onAsk).toBe('function')
    act(() => { assistantProps!.onAsk!('why is this slow?') })
    expect(openSideChat).toHaveBeenCalledTimes(1)
    expect(openSideChat).toHaveBeenCalledWith(SLOT)
    // The seed is written under the PANE's slot — not the active slot the
    // store holds — so the Side Chat the host re-binds to this pane reads it.
    expect(readSideChatDraft(SLOT)).toBe('> why is this slow?\n\n')
    expect(composer().value).toBe('')
  })
})

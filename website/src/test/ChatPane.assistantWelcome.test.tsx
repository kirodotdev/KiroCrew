import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { appendSlotMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The built-in Assistant's opening (ChatPane's opt-in `assistantWelcome`):
 * it lives INSIDE the transcript scroller above the messages, the pane's one
 * composer stays mounted below it, the SAME card goes expanded -> compact when
 * a user turn lands, and starters only prefill the composer — never send, never
 * overwrite a draft. Without the prop the pane is unchanged. */

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

// Unique per test: the pane parks its composer draft per slot in a module
// store that outlives the render, so a shared key would leak drafts between tests.
let SLOT = 'member-assistant-0'
let slotSeq = 0
const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>
const sendChat = api.sendChat as ReturnType<typeof vi.fn>

function makeStore() {
  return configureStore({
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
}

function renderPane(opts: { welcome?: boolean; name?: string; onCreate?: () => void; receipt?: ReactNode } = {}) {
  const store = makeStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onCreate = opts.onCreate ?? vi.fn()
  const welcome = opts.welcome === false ? undefined : { onCreate, name: opts.name }
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} frameless assistantWelcome={welcome} crewmateCreated={opts.receipt} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...view, store, onCreate }
}

const composer = (view: ReturnType<typeof render>) => view.container.querySelector('textarea') as HTMLTextAreaElement

beforeEach(() => {
  vi.clearAllMocks()
  SLOT = `member-assistant-${++slotSeq}`
  detail.mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 })
})

describe('Assistant welcome in ChatPane', () => {
  it('is absent without the opt-in: the ordinary empty hint shows instead', async () => {
    const view = renderPane({ welcome: false })
    await view.findByText(/Session ready/)
    expect(view.queryByTestId('assistant-welcome')).toBeNull()
  })

  it('an empty conversation opens expanded, inside the transcript scroller, above the one composer', async () => {
    const view = renderPane({ name: 'Juno' })
    const card = await view.findByTestId('assistant-welcome')
    expect(card).toHaveAttribute('data-state', 'expanded')
    expect(card).toHaveTextContent('Hi, I’m Juno.')
    expect(view.queryByText(/Session ready/)).toBeNull()
    // Transcript content, not a page-level overlay: it scrolls with the messages.
    expect(card.closest('.chat-container')).not.toBeNull()
    const ta = composer(view)
    expect(ta).not.toBeNull()
    expect(ta.closest('.chat-container')).toBeNull()
    expect(view.container.querySelectorAll('textarea')).toHaveLength(1)
    for (const id of ['setup', 'work', 'suggest']) expect(view.getByTestId(`assistant-welcome-starter-${id}`)).toBeInTheDocument()
    expect(view.getByTestId('assistant-welcome-create')).toBeInTheDocument()
    // Said before the first click: a starter fills the composer, it never sends.
    const hint = view.getByTestId('assistant-welcome-starters-hint')
    expect(hint).toHaveTextContent('Starters fill in the message box below. Nothing is sent until you send it.')
    for (const id of ['setup', 'work', 'suggest']) {
      expect(view.getByTestId(`assistant-welcome-starter-${id}`)).toHaveAttribute('aria-describedby', hint.id)
    }
    // Create opens a flow instead of filling the composer, so it is not one of the starters.
    expect(view.getByRole('list', { name: 'Ways to start' })).not.toContainElement(view.getByTestId('assistant-welcome-create'))
  })

  it('falls back to the default name when none is given', async () => {
    const view = renderPane()
    expect(await view.findByTestId('assistant-welcome')).toHaveTextContent('Hi, I’m Assistant.')
  })

  it('a conversation that already has user turns opens compact, with no starters', async () => {
    detail.mockResolvedValue({
      messages: [
        { role: 'user', content: 'Draft my weekly update.', cls: '', ts: '2026-09-25T09:00:00Z' },
        { role: 'assistant', content: 'Here is a draft.', cls: '', ts: '2026-09-25T09:00:05Z' },
      ],
      running: false, has_more: false, total: 2,
    })
    const view = renderPane()
    await view.findByText('Here is a draft.')
    const card = view.getByTestId('assistant-welcome')
    expect(card).toHaveAttribute('data-state', 'compact')
    expect(view.queryByTestId('assistant-welcome-body')).toBeNull()
    expect(view.queryByTestId('assistant-welcome-starter-setup')).toBeNull()
    // Above the messages in document order.
    expect(card.compareDocumentPosition(view.getByText('Here is a draft.')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('a bounded window with older history is not a first visit: compact', async () => {
    detail.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Latest reply.', cls: '', ts: '2026-09-25T09:00:05Z' }],
      running: false, has_more: true, total: 80,
    })
    const view = renderPane()
    await view.findByText('Latest reply.')
    expect(view.getByTestId('assistant-welcome')).toHaveAttribute('data-state', 'compact')
  })

  it('the SAME card compacts when a user turn lands, and the composer is the same mounted node', async () => {
    const view = renderPane()
    const card = await view.findByTestId('assistant-welcome')
    const ta = composer(view)
    expect(card).toHaveAttribute('data-state', 'expanded')
    act(() => { view.store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'user', content: 'Hello there', cls: '' } })) })
    await waitFor(() => expect(card).toHaveAttribute('data-state', 'compact'))
    // One element across the state flip, not a swap of two components.
    expect(view.getByTestId('assistant-welcome')).toBe(card)
    expect(composer(view)).toBe(ta)
    await waitFor(() => expect(view.queryByTestId('assistant-welcome-starter-setup')).toBeNull())
  })

  it('a starter fills the empty composer and sends nothing', async () => {
    const view = renderPane()
    fireEvent.click(await view.findByTestId('assistant-welcome-starter-suggest'))
    await waitFor(() => expect(composer(view).value).toMatch(/tell me what it’s based on/))
    expect(sendChat).not.toHaveBeenCalled()
    // Still the expanded opening: prefilling is not a user turn.
    expect(view.getByTestId('assistant-welcome')).toHaveAttribute('data-state', 'expanded')
  })

  it('a starter never overwrites a draft the user already typed', async () => {
    const view = renderPane()
    await view.findByTestId('assistant-welcome')
    fireEvent.change(composer(view), { target: { value: 'my own words' } })
    await waitFor(() => expect(composer(view).value).toBe('my own words'))
    fireEvent.click(view.getByTestId('assistant-welcome-starter-setup'))
    await waitFor(() => expect(view.getByTestId('assistant-welcome-status')).toHaveTextContent('left as it is'))
    expect(composer(view).value).toBe('my own words')
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('create a crewmate hands off to the host and leaves the composer alone', async () => {
    const onCreate = vi.fn()
    const view = renderPane({ onCreate })
    fireEvent.click(await view.findByTestId('assistant-welcome-create'))
    expect(onCreate).toHaveBeenCalledTimes(1)
    expect(composer(view).value).toBe('')
    expect(sendChat).not.toHaveBeenCalled()
  })
})

it('shows a host creation receipt after a compact opening without sending an AI turn', async () => {
  const view = renderPane({ receipt: <section data-testid="creation-receipt">Scout is ready</section> })
  const welcome = await view.findByTestId('assistant-welcome')
  const receipt = view.getByTestId('creation-receipt')
  expect(welcome).toHaveAttribute('data-state', 'compact')
  expect(welcome.compareDocumentPosition(receipt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(sendChat).not.toHaveBeenCalled()
  expect(view.container.querySelectorAll('textarea')).toHaveLength(1)
})

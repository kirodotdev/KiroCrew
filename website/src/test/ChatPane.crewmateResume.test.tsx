import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* #20692: a crewmate's chat (ChatPane + CrewComposer, as the Members page
 * mounts it) must offer Resume after a turn ends on a transient model error,
 * the way the ordinary chat does: in the composer and on the error row. The
 * crewmate's slot is NOT the store's active slot there, so the pane reads its
 * own slot's rows and run state, not the active mirror. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const continueSlot = vi.hoisted(() => vi.fn())
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
    continueSlot: (...a: unknown[]) => continueSlot(...a),
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
import CrewComposer from '../pages/members/CrewComposer'
import { api } from '../api/client'

// Unique per test: the pane parks its composer draft per slot in a module store
// that outlives the render.
let SLOT = 'member-radar-0'
let slotSeq = 0
const radar = { name: 'Radar' }
const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>

type Msg = { role: string; content: string; ts: string; cls?: string; meta?: Record<string, unknown> }

// The row the runner writes when a turn dies on a throttle: plain prose, no `meta.kind`.
const THROTTLED = 'Bedrock is throttling requests. Try: (1) wait a few seconds and retry, or (2) switch to a different model in the picker (e.g. sonnet).'
const row = (n: number, role: string, content: string, extra: Partial<Msg> = {}): Msg => ({
  role, content, ts: `2026-10-14T09:00:${String(n).padStart(2, '0')}Z`, cls: '', ...extra,
})
const errorRow = (n: number): Msg => row(n, 'error', THROTTLED, { cls: 'msg msg-err' })

function makeStore(running: boolean) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(messages: Msg[], { running = false, crewmate = true }: { running?: boolean; crewmate?: boolean } = {}) {
  detail.mockResolvedValue({ messages, running, has_more: false, total: messages.length })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(running)
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane
              slotKey={SLOT}
              frameless
              agentLocked
              composerInput={crewmate ? CrewComposer : undefined}
              crewmate={crewmate ? radar : undefined}
            />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...view, store }
}

beforeEach(() => {
  vi.clearAllMocks()
  continueSlot.mockReset()
  continueSlot.mockResolvedValue({ ok: true, slot: 'x' })
  SLOT = `member-radar-${++slotSeq}`
})

describe("a mate's chat after a turn ends on a transient model error", () => {
  it('offers Resume in the composer and on the error row, and pressing it continues that slot', async () => {
    const view = renderPane([
      row(0, 'user', 'Check the nightly build'),
      row(1, 'assistant', 'On it.'),
      row(2, 'user', 'And report back when it is green'),
      row(3, 'tool', '🔧 gh run list'),
      errorRow(4),
    ])

    const resume = await view.findByTestId('composer-continue')
    expect(resume).toHaveTextContent('Resume')
    expect(view.getByTestId('error-card-continue')).toBeInTheDocument()
    expect(view.getByTestId('error-card')).toHaveTextContent('Bedrock is throttling requests.')

    fireEvent.click(resume)
    await waitFor(() => expect(continueSlot).toHaveBeenCalledTimes(1))
    expect(continueSlot).toHaveBeenCalledWith(SLOT)
  })

  it('the error row has the same Resume', async () => {
    const view = renderPane([row(0, 'user', 'Check the nightly build'), row(1, 'assistant', 'On it.'), row(2, 'user', 'Status?'), errorRow(3)])

    fireEvent.click(await view.findByTestId('error-card-continue'))
    await waitFor(() => expect(continueSlot).toHaveBeenCalledWith(SLOT))
  })

  it('offers Resume when a patrol wake the chat does not draw is the turn that died', async () => {
    // The wake row and its tool calls are machinery a crewmate's chat filters
    // out; the unanswered wake is still the interrupted turn.
    const view = renderPane([
      row(0, 'user', 'Keep an eye on the queue'),
      row(1, 'assistant', 'Will do.'),
      row(2, 'nudge', '[auto-nudge cycle 2]\nPatrol.'),
      row(3, 'tool', '🔧 gh issue list'),
      errorRow(4),
    ])

    await view.findByTestId('composer-continue')
    expect(view.getByTestId('error-card-continue')).toBeInTheDocument()
    expect(view.queryByText(/auto-nudge/)).toBeNull()
  })

  it('an ordinary split pane gets the same Resume', async () => {
    const view = renderPane([row(0, 'user', 'Check the nightly build'), row(1, 'assistant', 'On it.'), row(2, 'user', 'Status?'), errorRow(3)], { crewmate: false })

    await view.findByTestId('composer-continue')
    expect(view.getByTestId('error-card-continue')).toBeInTheDocument()
  })

  it('offers nothing while the mate is working again', async () => {
    const view = renderPane([row(0, 'user', 'Check the nightly build'), row(1, 'assistant', 'On it.'), row(2, 'nudge', '[auto-nudge cycle 3]\nPatrol.'), errorRow(3)], { running: true })

    await view.findByTestId('error-card')
    expect(view.queryByTestId('composer-continue')).toBeNull()
    expect(view.queryByTestId('error-card-continue')).toBeNull()
  })

  it('offers nothing after a turn that finished cleanly', async () => {
    const view = renderPane([row(0, 'user', 'Check the nightly build'), row(1, 'assistant', 'The build is green.')])

    await view.findByText('The build is green.')
    expect(view.queryByTestId('composer-continue')).toBeNull()
    expect(view.queryByTestId('error-card-continue')).toBeNull()
  })

  it('offers nothing on an error an earlier turn left behind', async () => {
    const view = renderPane([row(0, 'user', 'Check the nightly build'), errorRow(1), row(2, 'user', 'Try again'), row(3, 'assistant', 'The build is green.')])

    await view.findByText('The build is green.')
    expect(view.queryByTestId('composer-continue')).toBeNull()
    expect(view.queryByTestId('error-card-continue')).toBeNull()
  })

  it('says why when the gateway refuses the press, with the reason it gave', async () => {
    continueSlot.mockRejectedValue(new Error('slot is running'))
    const view = renderPane([row(0, 'user', 'Check the nightly build'), row(1, 'assistant', 'On it.'), row(2, 'user', 'Status?'), errorRow(3)])

    fireEvent.click(await view.findByTestId('composer-continue'))

    const notice = await view.findByTestId('chat-pane-continue-error')
    expect(notice).toHaveTextContent("Couldn't continue")
    expect(notice).toHaveTextContent('slot is running')
  })

  it('retires the refusal and both Resume buttons once the slots frame reports the mate running', async () => {
    // A "slot is running" refusal means a turn started that this tab had not
    // heard of. The frame that says so takes the notice down and withholds
    // Resume, so the reason never sits beside a slot that reads idle.
    continueSlot.mockRejectedValue(new Error('slot is running'))
    const view = renderPane([row(0, 'user', 'Check the nightly build'), row(1, 'assistant', 'On it.'), row(2, 'user', 'Status?'), errorRow(3)])
    fireEvent.click(await view.findByTestId('composer-continue'))
    await view.findByTestId('chat-pane-continue-error')

    act(() => { view.store.dispatch(sseSlots([{ key: SLOT, title: 'Radar', messages: 4, running: true, mode: 'member', agent: 'radar' }] as never)) })

    await waitFor(() => expect(view.queryByTestId('chat-pane-continue-error')).toBeNull())
    expect(view.queryByTestId('composer-continue')).toBeNull()
    expect(view.queryByTestId('error-card-continue')).toBeNull()
  })
})

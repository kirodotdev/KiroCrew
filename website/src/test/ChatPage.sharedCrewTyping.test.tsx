/**
 * Shared Crew typing presence: a keystroke in the main composer fires the
 * content-free typing signal the collab strip's presence reporter listens to,
 * and opening a session does not.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: true, has_more: false, total: 1 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    sharedCrewCapabilities: vi.fn(),
    sharedCrewViewer: vi.fn(),
    sharedCrewPresence: vi.fn(),
    sharedCrewDecisions: vi.fn().mockResolvedValue({ schema_version: 1, items: [] }),
    sharedCrewSendState: vi.fn(),
    sharedCrewTypingLease: vi.fn().mockResolvedValue(null),
    sharedCrewTakeOverTypingLease: vi.fn(),
    sharedCrewMode: vi.fn().mockResolvedValue(null),
    sharedCrewPeople: vi.fn().mockResolvedValue(null),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'
import type { PresenceEntry, PublicPrincipal, SharedDecision, TypingLease } from '../api/sharedCrew'
import { SessionCollabStrip } from '../pages/chat/shared-crew/SessionCollabStrip'
import { composerTypingSignal } from '../pages/chat/shared-crew/typingSignal'

type SlotState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'

function makeStore(opts: { slotRunning: boolean; slotStopping: boolean; slotState: SlotState; approvalId?: string }) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 1, running: opts.slotRunning, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }, ...(opts.approvalId ? [{ role: 'permission', content: 'Running: ls', ts: '1', meta: { approval_id: opts.approvalId, tool_call_id: 'tc-1', tool_input: '{}' } }] : [])],
        slotRunning: opts.slotRunning, slotStopping: opts.slotStopping, slotState: opts.slotState,
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderWithState(opts: { slotRunning: boolean; slotStopping: boolean; slotState: SlotState; approvalId?: string }, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const store = makeStore(opts)
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return store
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE })
  vi.mocked(api.sharedCrewViewer).mockResolvedValue(null)
  vi.mocked(api.sharedCrewPresence).mockResolvedValue({ schema_version: 1, entries: [] })
  vi.mocked(api.sharedCrewSendState).mockResolvedValue({ schema_version: 1, session_key: 'slot-a', revision: 0, last: null })
})

describe('ChatPage: Shared Crew typing signal', { timeout: 15_000 }, () => {
  it('signals typing on the user\'s own keystrokes, carrying no text', async () => {
    const heard = vi.fn()
    const off = composerTypingSignal.subscribe((...args: unknown[]) => heard(...args))
    try {
      await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
      heard.mockClear()
      const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.change(input, { target: { value: 'h' } })
      fireEvent.change(input, { target: { value: 'hi' } })
      // Wait for the edit's own follow-up render to land the value before asserting.
      await waitFor(() => expect(input.value).toBe('hi'))
      await waitFor(() => expect(heard).toHaveBeenCalledTimes(2))
      // The only thing a listener hears is whether the draft is now empty.
      for (const call of heard.mock.calls) expect(call).toEqual([false])
    } finally {
      off()
    }
  })

  it('does not signal typing when the page opens a session', async () => {
    const heard = vi.fn()
    const off = composerTypingSignal.subscribe(heard)
    try {
      await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
      expect(heard).not.toHaveBeenCalled()
    } finally {
      off()
    }
  })
})

describe('ChatPage: taking over the typing lease', { timeout: 15_000 }, () => {
  const alice = { id: 'p_alice', kind: 'human', display: { name: 'Alice' } } as PublicPrincipal
  const bob = { id: 'p_bob', kind: 'human', display: { name: 'Bob' } } as PublicPrincipal
  const held = (holder: PublicPrincipal, extra: Partial<TypingLease> = {}): TypingLease => ({
    schema_version: 1, session_key: 'slot-a', holder, holder_ids: [holder.id], expires_at: 1_000, idle_seconds: 10, ended: '', ...extra,
  })

  beforeEach(() => {
    vi.mocked(api.sharedCrewViewer).mockResolvedValue(bob)
    vi.mocked(api.sharedCrewTypingLease).mockResolvedValue(held(alice))
  })
  afterEach(() => {
    vi.mocked(api.sharedCrewTypingLease).mockResolvedValue(null)
    vi.mocked(api.sharedCrewTakeOverTypingLease).mockReset()
  })

  it('offers no Take over without the preview', async () => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available' })
    await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
    expect(await screen.findByTestId('composer-typing-lock')).toHaveTextContent('Alice is typing…')
    expect(screen.queryByTestId('composer-take-over')).toBeNull()
  })

  it('unlocks the taker on the re-read lease with the preview on', async () => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', shared_crew_preview: true })
    const taken = held(bob, { ended: 'taken_over', taken_over_from: alice, taken_over_from_ids: [alice.id] })
    vi.mocked(api.sharedCrewTakeOverTypingLease).mockResolvedValue(taken)
    await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
    const box = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box.readOnly).toBe(true))
    vi.mocked(api.sharedCrewTypingLease).mockResolvedValue(taken)
    await act(async () => { fireEvent.click(screen.getByTestId('composer-take-over')) })
    expect(api.sharedCrewTakeOverTypingLease).toHaveBeenCalledWith('slot-a')
    await waitFor(() => expect(screen.queryByTestId('composer-typing-lock')).toBeNull())
    expect(box.readOnly).toBe(false)
  })
})

describe('Shared Crew collaboration read errors', () => {
  it.each([
    'sharedCrewCapabilities', 'sharedCrewViewer', 'sharedCrewPresence', 'sharedCrewSendState',
  ] as const)('shows %s failures with agent recovery', async which => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available' })
    vi.mocked(api[which]).mockRejectedValue(new Error(`${which} read failed`))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><SessionCollabStrip sessionKey="slot-a" /></QueryClientProvider>)
    expect(await screen.findByTestId('session-collab-error')).toHaveTextContent(`${which} read failed`)
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('renders no collaboration strip for a healthy personal Crew', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { container } = render(<QueryClientProvider client={qc}><SessionCollabStrip sessionKey="slot-a" /></QueryClientProvider>)
    await waitFor(() => {
      expect(qc.getQueryState(['shared-crew', 'capabilities'])?.status).toBe('success')
      expect(qc.getQueryState(['shared-crew', 'send-state', 'slot-a'])?.status).toBe('success')
    })
    expect(container).toBeEmptyDOMElement()
  })
})

/**
 * One session, four moments: each piece of presence and typing information is
 * on screen exactly once, nothing shows for a person alone, and no row names who
 * approved. Each case reads the whole page, so a second copy anywhere (the
 * strip, the placeholder, the lock banner, a transcript row) is a failure.
 */
describe('ChatPage: presence and typing show exactly once', { timeout: 15_000 }, () => {
  // Presence, the lock and the settled line wait on capabilities, then mode and people, then the
  // viewer, presence and decisions: each hop is its own query, so these waits get a longer bound.
  const CHAIN_TIMEOUT_MS = 5000
  const person = (id: string, name: string) => ({ id, kind: 'human', display: { name } }) as PublicPrincipal
  const bob = person('p_bob', 'Bob')
  const alice = person('p_alice', 'Alice')
  const entry = (p: PublicPrincipal, activity: PresenceEntry['activity']): PresenceEntry =>
    ({ principal: p, session_key: 'slot-a', activity, connections: 1, expires_at: 0 })
  const lease = (holder: PublicPrincipal): TypingLease =>
    ({ schema_version: 1, session_key: 'slot-a', holder, holder_ids: [holder.id], expires_at: 1_000, idle_seconds: 10, ended: '' })
  const approved: SharedDecision = {
    decision_id: 'approval:1:slot-a:1', kind: 'approval', session_key: 'slot-a', state: 'resolved', revision: 2,
    initiator: bob, responder: alice, outcome: 'approved', resolved_at: 10,
  }

  /** The team state the surfaces need: preview on, Alice has joined, Bob is the viewer. */
  function teamCrew(entries: PresenceEntry[], held: TypingLease | null) {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available', shared_crew_preview: true })
    vi.mocked(api.sharedCrewMode).mockResolvedValue({ schema_version: 1, mode: 'network', crew_id: 'c1' } as never)
    vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: alice, sources: ['observed'], is_viewer: false }] } as never)
    vi.mocked(api.sharedCrewViewer).mockResolvedValue(bob)
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ schema_version: 1, entries })
    vi.mocked(api.sharedCrewTypingLease).mockResolvedValue(held)
  }
  /** Every place a string can reach the user: text, placeholder, title and aria-label. */
  const mentions = (re: RegExp) => {
    let n = 0
    for (const el of Array.from(document.body.querySelectorAll('*'))) {
      const own = Array.from(el.childNodes).filter(c => c.nodeType === 3).map(c => c.textContent ?? '').join('')
      if (re.test(own)) n++
      for (const a of ['placeholder', 'title', 'aria-label']) if (re.test(el.getAttribute(a) ?? '')) n++
    }
    return n
  }

  afterEach(() => {
    vi.mocked(api.sharedCrewTypingLease).mockResolvedValue(null)
    vi.mocked(api.sharedCrewDecisions).mockClear()
  })

  it('solo: nothing about presence, typing or approvals', async () => {
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available' })
    vi.mocked(api.sharedCrewViewer).mockResolvedValue(bob)
    vi.mocked(api.sharedCrewPresence).mockResolvedValue({ schema_version: 1, entries: [entry(bob, 'typing')] })
    await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
    await waitFor(() => expect(api.sharedCrewPresence).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(api.sharedCrewViewer).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('presence-chips')).toBeNull()
    expect(screen.queryByTestId('composer-typing-lock')).toBeNull()
    expect(screen.queryByTestId('session-collab-strip')).toBeNull()
    expect(mentions(/typing|approved/i)).toBe(0)
  })

  it('two present, nobody typing: one chip for the other person, no typing notice', async () => {
    teamCrew([entry(bob, 'viewing'), entry(alice, 'viewing')], null)
    await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
    const chips = await screen.findByTestId('presence-chips', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(chips.querySelectorAll('li')).toHaveLength(1)
    expect(screen.getAllByTestId('presence-chips')).toHaveLength(1)
    expect(screen.queryByTestId('composer-typing-lock')).toBeNull()
    expect(mentions(/typing/i)).toBe(0)
  })

  it('other typing: "Alice is typing" appears once, in the locked composer', async () => {
    teamCrew([entry(bob, 'viewing'), entry(alice, 'typing')], lease(alice))
    await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
    const banner = await screen.findByTestId('composer-typing-lock', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(banner).toHaveTextContent('Alice is typing…')
    await screen.findByTestId('presence-chips', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(mentions(/Alice is typing/)).toBe(1)
    expect(mentions(/typing/i)).toBe(1)
    expect(mentions(/type again/i)).toBe(0)
    expect(screen.getByLabelText('Message input')).toHaveAttribute('placeholder', '')
    expect(screen.getAllByTestId('presence-chips')).toHaveLength(1)
  })

  it('approval granted elsewhere: one short line, no attribution row', async () => {
    teamCrew([entry(bob, 'viewing'), entry(alice, 'viewing')], null)
    const pending: SharedDecision = { ...approved, state: 'pending', responder: null, outcome: '', resolved_at: null }
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [pending] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle', approvalId: '1' }, qc)
    await screen.findByTestId('presence-chips', {}, { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(qc.getQueryData(sharedCrewKeys.decisions('slot-a'))).toBeDefined(), { timeout: CHAIN_TIMEOUT_MS })
    expect(mentions(/approved/i)).toBe(0)
    await act(async () => { qc.setQueryData(sharedCrewKeys.decisions('slot-a'), { schema_version: 1, items: [approved] }) })
    expect(await screen.findByText('Alice approved')).toBeInTheDocument()
    expect(mentions(/approved/i)).toBe(1)
    expect(screen.queryByTestId('decision-attribution')).toBeNull()
  })

  it('approval granted before the page opened: nothing is replayed', async () => {
    teamCrew([entry(bob, 'viewing'), entry(alice, 'viewing')], null)
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [approved] })
    await renderWithState({ slotRunning: false, slotStopping: false, slotState: 'idle' })
    await screen.findByTestId('presence-chips', {}, { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(api.sharedCrewDecisions).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('decision-attribution')).toBeNull()
    expect(mentions(/approved|rejected|answered|awaits approval/i)).toBe(0)
  })
})

// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import { createTestStore, renderWithProviders } from './helpers'
import SessionDashboardsPage from '../pages/chat/command-center/SessionDashboardsPage'

vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (html: string | null) => ({ url: html ? '/sandbox/card' : null, pending: false, failed: false, retry: vi.fn() }),
}))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))

function store(count = 3) {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: Array.from({ length: count }, (_, i) => ({
    key: `slot-${i}`, title: `Session ${i}`, messages: 2, running: i === 0, needs_input: i === 1,
  })) } })
}

/** The DOM order is the reading order. */
function visualCards() {
  return screen.getAllByTestId('session-dashboard-card')
}

describe('all session dashboards', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { dynamic_dashboard_cards: false } })
    vi.spyOn(api, 'dashboardCard').mockResolvedValue({ card: null, status: 'waiting', published_at: null, content_event_at: null, stale: false })
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([{ slot: 'slot-1', ask_id: 'question-1', questions: [{ question: 'Which release?', options: [{ label: 'Stable' }] }] }])
    vi.spyOn(api, 'approvals').mockResolvedValue([{ id: 'approval-2', instance: 'inst-2', slot: 'slot-2', tool: 'shell', tool_input: 'git status' }])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionSummary').mockImplementation(async slot => ({ enabled: true, stale: false, constraints: [], generated_at: null, user_turns: 2, last_activity: null, intents: [{
      title: `Summary for ${slot}`, initial_intent: 'Ship the release', progress: ['Tests passed'], next_steps: [], ranges: [[1, 2]], status: 'active', verified: null, state: 'in-progress', last_touched_turn: 2, origin_turn: 1,
    }] }))
  })

  it('surfaces exact-session inputs first with saved summaries and no automatic writes', async () => {
    const answer = vi.spyOn(api, 'answerQuestion').mockResolvedValue({})
    const approve = vi.spyOn(api, 'resolveApproval').mockResolvedValue({})
    const generate = vi.spyOn(api, 'generateSessionSummary')
    renderWithProviders(<SessionDashboardsPage />, { store: store() })
    await screen.findByText('Summary for slot-1')
    // The page says what each entry carries, not that every session owns a dashboard
    // page: a dispatched session's entry is a derived CARD, not a package of its own.
    expect(screen.getByText('Saved summaries and status cards for your open sessions, plus the requests that need you.')).toBeVisible()
    const cards = visualCards()
    expect(within(cards[0]).getByRole('link', { name: 'Open session' })).toHaveAttribute('href', '/chat?sid=slot-1')
    expect(cards.map(c => c.getAttribute('data-slot')).slice(0, 2)).toEqual(['slot-1', 'slot-2'])
    const inbox = screen.getByRole('region', { name: 'Needs you' })
    expect(inbox.compareDocumentPosition(cards[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(inbox).getByText('Session 1')).toBeVisible()
    expect(within(inbox).getByText('From session: Session 2')).toBeVisible()
    expect(screen.getAllByRole('button', { name: 'Approve once' })).toHaveLength(1)
    expect(cards.every(card => !within(card).queryByRole('button', { name: /Approve once|Send answer/ }))).toBe(true)
    fireEvent.click(within(inbox).getByText('Stable'))
    fireEvent.click(within(inbox).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(answer).toHaveBeenCalledWith('question-1', { 'Which release?': 'Stable' }))
    expect(approve).not.toHaveBeenCalled()
    expect(generate).not.toHaveBeenCalled()
    fireEvent.click(within(inbox).getByRole('button', { name: 'Approve once' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('approval-2', 'approve', { origin: 'coordinator', slot: 'slot-2', instance: 'inst-2' }))
  })

  it('keeps cards in place on live activity and re-sorts on a filter change', async () => {
    const initial = store().getState()
    const team = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: [
      ...initial.dashboard.slots, { key: 'builder', title: 'Builder', messages: 2, running: false, created_by: 'slot-2' },
    ] } })
    renderWithProviders(<SessionDashboardsPage />, { store: team })
    await waitFor(() => expect(screen.getAllByTestId('session-dashboard-card')).toHaveLength(4))
    const domOrder = () => screen.getAllByTestId('session-dashboard-card').map(card => card.getAttribute('data-slot'))
    await screen.findByRole('button', { name: 'Approve once' })
    await screen.findByText('Stable')
    const before = domOrder()
    act(() => { team.dispatch({ type: 'dashboard/sseSlots', payload: team.getState().dashboard.slots.map(s => s.key === 'slot-2' ? { ...s, running: true, last_turn_ts: '2030-01-01T00:00:00Z' } : s) }) })
    // Live activity does not move cards; changing the filter re-takes the order.
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(domOrder()).toEqual(before)
    fireEvent.change(screen.getByPlaceholderText('Search sessions…'), { target: { value: 'Session' } })
    fireEvent.change(screen.getByPlaceholderText('Search sessions…'), { target: { value: '' } })
    await waitFor(() => expect(domOrder()[0]).toBe('slot-2'))
  })

  it.each([['subagents_running', { subagents_running: true }], ['queue_depth', { queue_depth: 1 }]] as const)(
    'shows Running and sorts ahead of an idle session for a turn-idle slot with %s', async (_flag, delegated) => {
      vi.mocked(api.pendingQuestions).mockResolvedValue([])
      vi.mocked(api.approvals).mockResolvedValue([])
      const initial = createTestStore().getState()
      // Neither slot has a turn in flight; the second one still has work going.
      const fleet = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [
        { key: 'idle', title: 'Idle session', messages: 2, running: false, last_turn_ts: '2030-01-01T00:00:00Z' },
        { key: 'delegated', title: 'Delegating session', messages: 2, running: false, ...delegated },
      ] } })
      renderWithProviders(<SessionDashboardsPage />, { store: fleet })
      await screen.findByText('Summary for delegated')
      const cards = visualCards()
      expect(cards.map(c => c.getAttribute('data-slot'))).toEqual(['delegated', 'idle'])
      expect(within(cards[0]).getByText('Running')).toBeVisible()
      expect(within(cards[1]).getByText('Idle')).toBeVisible()
    })

  it('counts requests, not distinct sessions, consistently in the filter and inbox', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([
      { slot: 'slot-1', ask_id: 'one', questions: [{ question: 'First decision?', options: [{ label: 'Stable' }] }] },
      { slot: 'slot-2', ask_id: 'two', questions: [{ question: 'Second decision?', options: [{ label: 'Continue' }] }] },
    ])
    renderWithProviders(<SessionDashboardsPage />, { store: store() })
    fireEvent.click(await screen.findByText('Stable'))
    fireEvent.click(await screen.findByRole('button', { name: 'Needs you (3)' }))
    expect(within(screen.getByRole('region', { name: 'Needs you' })).getByTestId('panel-section-header')).toHaveTextContent('Needs you3')
    expect(screen.getByText('Summary for slot-0')).not.toBeVisible()
    expect(screen.getAllByRole('button', { name: 'Send answer' })[0]).toBeEnabled()
    fireEvent.change(screen.getByPlaceholderText('Search sessions…'), { target: { value: 'Session 2' } })
    expect(screen.getByRole('button', { name: 'Needs you (2)' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(screen.getByRole('region', { name: 'Needs you' })).getByTestId('panel-section-header')).toHaveTextContent('Needs you2')
    fireEvent.change(screen.getByPlaceholderText('Search sessions…'), { target: { value: '' } })
    expect(screen.getAllByRole('button', { name: 'Send answer' })[0]).toBeEnabled()
  })

  it('preserves answer drafts when filtering sessions out and back in', async () => {
    renderWithProviders(<SessionDashboardsPage />, { store: store() })
    fireEvent.click(await screen.findByText('Stable'))
    const search = screen.getByPlaceholderText('Search sessions…')
    fireEvent.change(search, { target: { value: 'Session 0' } })
    expect(screen.queryByRole('button', { name: 'Send answer' })).not.toBeInTheDocument()
    fireEvent.change(search, { target: { value: '' } })
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
  })

  it.each(['custom', 'option'])('retains a retired stateless %s draft while its session is filtered out', async kind => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'slot-1', card_id: 'card-1', questions: [{ question: 'Which release?', options: [{ label: 'Stable' }] }] }])
    const { queryClient } = renderWithProviders(<SessionDashboardsPage />, { store: store() })
    await screen.findByText('Stable')
    const input = screen.getByPlaceholderText(/type a custom answer/i)
    if (kind === 'custom') fireEvent.change(input, { target: { value: 'Keep my release draft' } })
    else fireEvent.click(screen.getByText('Stable'))
    const search = screen.getByPlaceholderText('Search sessions…')
    fireEvent.change(search, { target: { value: 'Session 0' } })
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    vi.mocked(api.approvals).mockResolvedValue([])
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['command-center'] })
      await queryClient.refetchQueries({ queryKey: ['global-approvals'] })
    })
    fireEvent.change(search, { target: { value: '' } })
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    if (kind === 'custom') expect(input).toHaveValue('Keep my release draft')
    expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
  })

  it('bounds summary reads to the shown cards, and reveals the rest explicitly', async () => {
    renderWithProviders(<SessionDashboardsPage />, { store: store(20) })
    await waitFor(() => expect(api.sessionSummary).toHaveBeenCalledTimes(12))
    expect(api.dashboardCard).toHaveBeenCalledTimes(12)
    expect(screen.getAllByTestId('session-dashboard-card').filter(card => !card.hidden)).toHaveLength(12)
    fireEvent.click(screen.getByRole('button', { name: 'Next sessions' }))
    await waitFor(() => expect(api.sessionSummary).toHaveBeenCalledTimes(20))
    expect(api.dashboardCard).toHaveBeenCalledTimes(20)
    expect(screen.getAllByTestId('session-dashboard-card').filter(card => !card.hidden)).toHaveLength(8)
    fireEvent.click(screen.getByRole('button', { name: 'Previous sessions' }))
    expect(screen.getAllByTestId('session-dashboard-card').filter(card => !card.hidden)).toHaveLength(12)
  })

  it('cards every session whose card the route serves, and only those', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'adopted', ask_id: 'question-adopted', questions: [{ question: 'Rebase onto main?', options: [{ label: 'Rebase' }] }] }])
    vi.mocked(api.approvals).mockResolvedValue([])
    const initial = createTestStore().getState()
    const fleet = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [
      { key: 'root', title: 'Root session', messages: 2, running: false },
      // Dispatched, no session-tree parent: the card route serves this one its derived
      // board, and this page is the only surface that mounts it.
      { key: 'conductor', title: 'Dispatched conductor', messages: 2, running: false, created_by: 'root' },
      // Adopted: a member of someone else's team, so it carries no board of its own.
      { key: 'adopted', title: 'Adopted worker', messages: 2, running: false, parent: { slot: 'root', key: 'root' } },
      { key: 'private', title: 'Incognito session', messages: 2, running: false, memory_mode: 'incognito' },
      { key: 'remote', title: 'Remote session', messages: 2, running: false, executor: 'remote' },
    ] } })
    renderWithProviders(<SessionDashboardsPage />, { store: fleet })
    await screen.findByText('Summary for root')
    await waitFor(() => expect(visualCards()).toHaveLength(2))
    expect(visualCards().map(card => card.getAttribute('data-slot')).sort()).toEqual(['conductor', 'root'])
    // No card means no read, so the page asks for exactly the boards it can show.
    expect(vi.mocked(api.dashboardCard).mock.calls.map(([slot]) => slot).sort()).toEqual(['conductor', 'root'])
    // A session with no card of its own keeps its decision on this page. The inbox has
    // its own source (`['command-center', 'questions']`), which settles independently of
    // the summary awaited above, so it is awaited on its own terms.
    const inbox = screen.getByRole('region', { name: 'Needs you' })
    expect(await within(inbox).findByText('Rebase onto main?')).toBeVisible()
    expect(within(inbox).getByText('Adopted worker')).toBeVisible()
    expect(await screen.findByRole('button', { name: 'Needs you (1)' })).toBeInTheDocument()
    // The three sessions with no card are still said to exist, with their number.
    expect(screen.getByText('3 sessions without a status card are not shown.')).toBeVisible()
  })

  it('says the open sessions have no card rather than that there are no sessions', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    vi.mocked(api.approvals).mockResolvedValue([])
    const initial = createTestStore().getState()
    const hidden = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [
      { key: 'private', title: 'Incognito session', messages: 2, running: false, memory_mode: 'incognito' },
      { key: 'adopted', title: 'Adopted worker', messages: 2, running: false, parent: { slot: 'root', key: 'root' } },
    ] } })
    renderWithProviders(<SessionDashboardsPage />, { store: hidden })
    expect(await screen.findByTestId('empty-state-title')).toHaveTextContent('None of your open sessions has a status card.')
    expect(screen.queryByText('No sessions')).not.toBeInTheDocument()
    expect(screen.getByText('2 sessions without a status card are not shown.')).toBeVisible()
    expect(screen.queryAllByTestId('session-dashboard-card')).toHaveLength(0)
  })

  it('still says there are no sessions when none is open, and counts one in the singular', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    vi.mocked(api.approvals).mockResolvedValue([])
    const initial = createTestStore().getState()
    const none = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [] } })
    renderWithProviders(<SessionDashboardsPage />, { store: none })
    expect(await screen.findByTestId('empty-state-title')).toHaveTextContent('No sessions')
    expect(screen.queryByText(/status card are not shown|status card is not shown/)).not.toBeInTheDocument()
    const one = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [
      { key: 'root', title: 'Root session', messages: 2, running: false },
      { key: 'remote', title: 'Remote session', messages: 2, running: false, executor: 'remote' },
    ] } })
    renderWithProviders(<SessionDashboardsPage />, { store: one })
    expect(await screen.findByText('1 session without a status card is not shown.')).toBeVisible()
  })

  it('keeps pending decisions reachable beyond the summary page limit', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue(Array.from({ length: 14 }, (_, i) => ({
      slot: `slot-${i}`, ask_id: `question-${i}`, questions: [{ question: `Decision ${i}`, options: [{ label: 'Continue' }] }],
    })))
    vi.mocked(api.approvals).mockResolvedValue([])
    renderWithProviders(<SessionDashboardsPage />, { store: store(14) })
    expect(await screen.findByText('Decision 13')).toBeVisible()
    const inbox = screen.getByRole('region', { name: 'Needs you' })
    expect(within(inbox).getAllByRole('button', { name: 'Send answer' })).toHaveLength(14)
    await waitFor(() => expect(api.sessionSummary).toHaveBeenCalledTimes(12))
    const search = screen.getByPlaceholderText('Search sessions…')
    fireEvent.change(search, { target: { value: 'Session 13' } })
    expect(within(inbox).getByText('Decision 13')).toBeVisible()
    expect(within(inbox).getByText('Decision 0')).not.toBeVisible()
  })

  it('bounds active documents to the shown page and keeps native drafts across pages', async () => {
    vi.mocked(api.dashboardCard).mockResolvedValue({ card: { html: '<p>Automatic evidence</p>', data: {} }, status: 'published', published_at: 1, content_event_at: 1, stale: false })
    const { container } = renderWithProviders(<SessionDashboardsPage />, { store: store(20) })
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(12))
    expect(screen.getAllByTestId('session-dashboard-card')).toHaveLength(20)
    expect(api.dashboardCard).toHaveBeenCalledTimes(12)
    fireEvent.click(screen.getByText('Stable'))
    fireEvent.click(screen.getByRole('button', { name: 'Next sessions' }))
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(8))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Previous sessions' }))
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(12))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
  })

  it('shows actual task context alongside the decision without inventing a default', async () => {
    const initial = store().getState()
    const testStore = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(slot => ({ ...slot,
      ...(slot.key === 'slot-2' ? { todo: { tasks: [], current: 'Verify the release in the isolated test workspace' } } : {}),
    })) } })
    renderWithProviders(<SessionDashboardsPage />, { store: testStore })
    const inbox = await screen.findByRole('region', { name: 'Needs you' })
    expect(await within(inbox).findByText('Verify the release in the isolated test workspace')).toBeVisible()
    expect(within(inbox).getByText('Permission mode: Normal', { exact: false })).toBeVisible()
    expect(within(inbox).queryByText(/no production impact|continue automatically/i)).not.toBeInTheDocument()
  })

  it('filters to actionable inputs rather than every running session', async () => {
    renderWithProviders(<SessionDashboardsPage />, { store: store() })
    await screen.findByText('Summary for slot-1')
    fireEvent.click(screen.getByRole('button', { name: 'Needs you (2)' }))
    expect(screen.getByText('Summary for slot-0')).not.toBeVisible()
    expect(screen.getByText('Summary for slot-1')).toBeVisible()
    expect(screen.getByText('Summary for slot-2')).toBeVisible()
  })

  it('distinguishes disabled summaries from a failed read and keeps retry explicit', async () => {
    vi.mocked(api.sessionSummary).mockImplementation(async slot => {
      if (slot === 'slot-1') throw new Error('Offline')
      return { enabled: false, stale: false, constraints: [], generated_at: null, user_turns: null, last_activity: null, intents: [] }
    })
    renderWithProviders(<SessionDashboardsPage />, { store: store(2) })
    expect(await screen.findByText('Session summaries are off')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the summary')
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(vi.mocked(api.sessionSummary).mock.calls.filter(([slot]) => slot === 'slot-1')).toHaveLength(2))
  })

  it.each(['pendingQuestions', 'approvals'] as const)('does not claim an empty inbox when %s fails', async (source) => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    vi.mocked(api.approvals).mockResolvedValue([])
    vi.mocked(api[source]).mockRejectedValue(new Error('Inventory unavailable'))
    renderWithProviders(<SessionDashboardsPage />, { store: store(1) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Some sources are unavailable.')
    await waitFor(() => expect(screen.queryByText('Loading current status…')).not.toBeInTheDocument())
    expect(screen.queryByText('Nothing in this category is waiting for you.')).not.toBeInTheDocument()
  })

  it('marks an out-of-date saved summary instead of presenting it as current', async () => {
    const original = vi.mocked(api.sessionSummary).getMockImplementation()!
    vi.mocked(api.sessionSummary).mockImplementation(async slot => ({ ...await original(slot), stale: true, generated_at: 1_700_000_000 }))
    renderWithProviders(<SessionDashboardsPage />, { store: store(1) })
    expect(await screen.findByText(/behind the conversation/)).toBeInTheDocument()
  })

  it('shows the restored-answer notice when the only pending question expires with a draft', async () => {
    vi.mocked(api.approvals).mockResolvedValue([])
    const initial = createTestStore().getState()
    const idle = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [
      { key: 'slot-1', title: 'Session 1', messages: 2, running: false },
    ] } })
    const { queryClient } = renderWithProviders(<SessionDashboardsPage />, { store: idle })
    const inbox = await screen.findByRole('region', { name: 'Needs you' })
    fireEvent.change(await within(inbox).findByPlaceholderText(/type a custom answer/i), { target: { value: 'eu-west-1' } })
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    expect(await screen.findByText(/The agent in "Session 1" stopped waiting for "Which release\?", so your answers weren't sent\. They're in that session's composer/)).toBeVisible()
    expect(screen.queryByText('Which release?')).not.toBeInTheDocument()
    // The notice is listed but not counted: nothing in it needs an answer.
    expect(screen.getByRole('button', { name: 'Needs you (0)' })).toBeInTheDocument()
    expect(within(inbox).getByTestId('panel-section-header')).toHaveTextContent('Needs you0')
  })

})

// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import { createTestStore, renderWithProviders } from './helpers'
import SessionDashboardsPage from '../pages/chat/command-center/SessionDashboardsPage'
import type { Artifact } from '../types'

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

describe('all session dashboards', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { dynamic_dashboard_cards: false } })
    vi.spyOn(api, 'dashboardCard').mockResolvedValue({ card: null, status: 'waiting', published_at: null, content_event_at: null, stale: false })
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([{ slot: 'slot-1', ask_id: 'question-1', questions: [{ question: 'Which release?', options: [{ label: 'Stable' }] }] }])
    vi.spyOn(api, 'approvals').mockResolvedValue([{ id: 'approval-2', instance: 'inst-2', slot: 'slot-2', tool: 'shell', tool_input: 'git status' }])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'artifacts').mockResolvedValue({ artifacts: [] })
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
    expect(screen.getByText('Dynamic Dashboards for currently open sessions, with saved summaries and requests that need you.')).toBeVisible()
    const cards = screen.getAllByTestId('session-dashboard-card')
    expect(within(cards[0]).getByText('No published view yet. Use Open session, then ask the agent to publish a view for this task.')).toBeVisible()
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
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center'] }) })
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

  it('bounds active documents while retaining published-view selection and native drafts across pages', async () => {
    const artifacts: Artifact[] = Array.from({ length: 20 }, (_, i) => [0, 1].map(view => ({
      slug: `view-${i}-${view}`, session_key: `dashboard:slot-${i}`, name: `View ${i}-${view}`,
      kind: 'html' as const, source: 'chat', description: '', tags: ['task-dashboard'], version: 1,
      created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', content: '<p>Published evidence</p>',
    }))).flat()
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts })
    vi.spyOn(api, 'artifact').mockImplementation(async slug => artifacts.find(a => a.slug === slug)!)
    vi.mocked(api.dashboardCard).mockResolvedValue({ card: { html: '<p>Automatic evidence</p>', data: {} }, status: 'published', published_at: 1, content_event_at: 1, stale: false })
    const { container } = renderWithProviders(<SessionDashboardsPage />, { store: store(20) })
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(24))
    expect(screen.getAllByTestId('session-dashboard-card')).toHaveLength(20)
    expect(api.sessionSummary).toHaveBeenCalledTimes(12)
    expect(api.dashboardCard).toHaveBeenCalledTimes(12)
    expect(api.artifact).toHaveBeenCalledTimes(12)
    const first = screen.getAllByTestId('session-dashboard-card')[0]
    fireEvent.change(within(first).getByRole('combobox', { name: 'Published view' }), { target: { value: 'view-1-1' } })
    await within(first).findByTitle('View 1-1')
    fireEvent.click(screen.getByText('Stable'))
    fireEvent.click(screen.getByRole('button', { name: 'Next sessions' }))
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(16))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    expect(api.sessionSummary).toHaveBeenCalledTimes(20)
    expect(api.dashboardCard).toHaveBeenCalledTimes(20)
    expect(api.artifact).toHaveBeenCalledTimes(21)
    fireEvent.click(screen.getByRole('button', { name: 'Previous sessions' }))
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(24))
    expect(within(first).getByRole('combobox', { name: 'Published view' })).toHaveValue('view-1-1')
    const search = screen.getByPlaceholderText('Search sessions…')
    fireEvent.change(search, { target: { value: 'Session 19' } })
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(2))
    fireEvent.change(search, { target: { value: '' } })
    await waitFor(() => expect(container.querySelectorAll('iframe')).toHaveLength(24))
    expect(within(first).getByRole('combobox', { name: 'Published view' })).toHaveValue('view-1-1')
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    expect(api.sessionSummary).toHaveBeenCalledTimes(20)
    expect(api.dashboardCard).toHaveBeenCalledTimes(20)
    expect(api.artifact).toHaveBeenCalledTimes(21)
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
})

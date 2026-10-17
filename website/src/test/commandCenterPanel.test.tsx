import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api, ApiError } from '../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../api/membersQuery'
import * as transport from '../chat-core/transport/sendTurn'
import { createTestStore, renderWithProviders } from './helpers'
import CommandCenterPanel from '../pages/chat/command-center/CommandCenterPanel'
import PendingQuestionCard from '../components/PendingQuestionCard'
import { setQuestionCard } from '../store/chatSlice'
import { __resetForTests, loadDrafts } from '../utils/chatDrafts'

// Set true to make the lazy Overview subtree throw on render, standing in for a
// failed chunk import — the F1 regression drives the dashboard-only boundary.
const mockState = vi.hoisted(() => ({ chunkLoadFails: false }))

vi.mock('../pages/members/CrewDynamicDashboard', () => ({
  default: ({ target, onAct }: { target: { kind: string; slot?: string; slug?: string; member?: string }; onAct?: (text: string) => void }) => {
    if (mockState.chunkLoadFails) throw new Error('Failed to fetch dynamically imported module')
    return <div data-testid="session-dynamic-dashboard" data-kind={target.kind} data-slot={target.slot} data-slug={target.slug} data-member={target.member}>
      dynamic dashboard
      <button type="button" onClick={() => onAct?.('Re-dispatch it')}>page option</button>
    </div>
  },
}))

/** The Overview mounts `CrewDynamicDashboard` through a lazy import, so its first
 * commit waits on that chunk; the bound says how long the test will wait for it. */
const LAZY_DASHBOARD_WAIT = { timeout: 5000 }

function taskStore() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Conductor', messages: 0, running: true },
    { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: false },
    { key: 'adopted', title: 'Adopted tab', messages: 0, running: false, parent: { slot: 'root', key: 'dashboard:root' } },
    { key: 'mate-dm', title: 'Mate', messages: 0, running: false },
  ] } })
}

describe('task dashboard host controls', () => {
  afterEach(() => vi.unstubAllGlobals())
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    __resetForTests()
    // happy-dom has no layout; establish the panel width that selects tabs.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(480)
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'members').mockResolvedValue({ members: [
      { name: 'Mate', slug: 'mate', slot_key: 'mate-dm', running: false },
    ] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'dispatched', status: 'blocked', summary: 'Needs evidence' },
    ] } })
  })

  it('renders a root session\'s own Dynamic Dashboard as the Overview, keyed by its slot', async () => {
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    const page = await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)
    expect(page).toBeVisible()
    expect(page).toHaveAttribute('data-kind', 'session')
    expect(page).toHaveAttribute('data-slot', 'root')
    expect(screen.getByTestId('command-center-overview')).toContainElement(page)
    // The agent-published page and its request are gone with the HUD.
    expect(screen.queryByRole('button', { name: 'Create published view' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-status-frame')).not.toBeInTheDocument()
    expect(screen.queryByTestId('task-dashboard-frame')).not.toBeInTheDocument()
  })

  it('renders a crewmate DM slot\'s MEMBER page, the one its agent writes', async () => {
    renderWithProviders(<CommandCenterPanel slot="mate-dm" active />, { store: taskStore() })
    const page = await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)
    expect(page).toHaveAttribute('data-kind', 'member')
    expect(page).toHaveAttribute('data-slug', 'mate')
    expect(page).toHaveAttribute('data-member', 'Mate')
  })

  it('says the dashboard could not be loaded when the roster cannot be read, and mounts no page', async () => {
    vi.mocked(api.members).mockRejectedValue(new Error('roster down'))
    renderWithProviders(<CommandCenterPanel slot="mate-dm" active />, { store: taskStore() })
    expect(await screen.findByTestId('command-center-roster-error', {}, LAZY_DASHBOARD_WAIT)).toHaveTextContent('This dashboard could not be loaded.')
    expect(screen.queryByTestId('session-dynamic-dashboard')).not.toBeInTheDocument()
  })

  it('says a refresh failed while keeping the cached DM dashboard mounted', async () => {
    // A good read followed by a failed refresh: react-query serves the cached DM
    // row (so the member page stays) AND reports isError. The notice must still
    // render -- gating it on `!dm` would hide the failed request behind the stale page.
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="mate-dm" active />, { store: taskStore() })
    const page = await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)
    expect(page).toHaveAttribute('data-kind', 'member')
    // The refresh fails while the cached roster row is still in hand.
    vi.mocked(api.members).mockRejectedValue(new Error('refresh down'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }) })
    expect(await screen.findByTestId('command-center-roster-error', {}, LAZY_DASHBOARD_WAIT)).toHaveTextContent('This dashboard could not be loaded.')
    expect(screen.getByTestId('session-dynamic-dashboard')).toHaveAttribute('data-kind', 'member')
  })

  it('contains an Overview chunk-load failure instead of unmounting the panel', async () => {
    // The Overview is a lazy import; a failed chunk load rejects inside its
    // Suspense. Without a boundary of its own that rejection climbs to the host's
    // outer boundary and unmounts this panel, taking the Questions tab's unsent
    // draft. Contained here, the panel and its cards stay mounted.
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] }] }])
    mockState.chunkLoadFails = true
    try {
      renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
      expect(await screen.findByTestId('command-center-overview-error', {}, LAZY_DASHBOARD_WAIT)).toHaveTextContent('This dashboard could not be loaded.')
      // The panel itself is still mounted -- the host's draft-bearing cards survive.
      expect(screen.getByTestId('command-center-panel')).toBeInTheDocument()
    } finally {
      mockState.chunkLoadFails = false
    }
  })

  it.each(['worker', 'adopted'])('gives a non-root (%s) session no dashboard and opens on Questions', async slot => {
    renderWithProviders(<CommandCenterPanel slot={slot} active />, { store: taskStore() })
    expect(await screen.findByRole('radio', { name: /Questions/ })).toBeChecked()
    expect(screen.queryByRole('radio', { name: /Overview/ })).not.toBeInTheDocument()
    expect(screen.queryByTestId('command-center-overview')).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-dynamic-dashboard')).not.toBeInTheDocument()
  })

  it('sends a reply the page offered to the composer the host gave it', async () => {
    const onAct = vi.fn()
    renderWithProviders(<CommandCenterPanel slot="root" active onAct={onAct} />, { store: taskStore() })
    await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'page option' }))
    expect(onAct).toHaveBeenCalledWith('Re-dispatch it')
  })

  it.each(['worker', 'adopted'])('says where a non-root (%s) session\'s work shows', async slot => {
    renderWithProviders(<CommandCenterPanel slot={slot} active />, { store: taskStore() })
    expect(await screen.findByTestId('command-center-no-dashboard')).toHaveTextContent(
      "This session's work shows on the dashboard of the session that started it.",
    )
  })

  it('shows no such line on a root session', async () => {
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)
    expect(screen.queryByTestId('command-center-no-dashboard')).not.toBeInTheDocument()
  })

  it('does not mount the dashboard while the panel is not active', () => {
    renderWithProviders(<CommandCenterPanel slot="root" active={false} />, { store: taskStore() })
    expect(screen.queryByTestId('session-dynamic-dashboard')).not.toBeInTheDocument()
  })

  it('says a part is missing, not that fresh decisions are stale, when an optional source fails', async () => {
    vi.mocked(api.workflowRuns).mockRejectedValue(new Error('workflows not available'))
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByText(/Some sources could not be loaded: workflow runs/)).toBeInTheDocument()
    expect(screen.queryByText(/The last known state may be out of date/)).not.toBeInTheDocument()
  })

  it('keeps the questions and approvals out of the Overview, each in its own tab', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask', questions: [{ question: 'Which contract?', options: [{ label: 'Stable API' }] }] }])
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    const page = await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)
    const overview = screen.getByTestId('command-center-overview')
    expect(within(overview).queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
    expect(within(overview).queryByText('Which contract?')).not.toBeInTheDocument()
    expect(await screen.findByRole('radio', { name: 'Approvals 1' })).toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: 'Questions 1' }))
    expect(screen.getByText('Which contract?')).toBeVisible()
    expect(page).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Approvals 1' }))
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)).toBeVisible()
  })

  it('names approval-only session state Needs input, not Questions', async () => {
    const initial = taskStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, pending_approval: s.key === 'worker' })) } })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store })
    expect(await screen.findByRole('radio', { name: 'Approvals 1' })).toBeVisible()
    expect(screen.getByRole('radio', { name: 'Questions' })).toBeVisible()
  })

  it('shows recorded session context on an approval without inventing a request reason', async () => {
    const initial = taskStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(slot => ({ ...slot, ...(slot.key === 'worker' ? { todo: { tasks: [], current: 'Validate release in isolated workspace' } } : {}) })) } })
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store })
    fireEvent.click(await screen.findByRole('radio', { name: 'Approvals 1' }))
    expect(screen.getAllByText('Approvals')).toHaveLength(1)
    expect(screen.getByRole('radio', { name: /Approvals/ })).toHaveTextContent('1')
    const card = screen.getByRole('button', { name: 'Approve once' }).closest('section')!
    expect(within(card).getByText('Validate release in isolated workspace')).toBeVisible()
    expect(within(card).queryByText(/no production impact|continue automatically/i)).not.toBeInTheDocument()
  })

  it.each(['custom', 'option'])('retains a retired stateless %s draft across polls and section navigation', async kind => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'dashboard:worker', card_id: 'card-1', native: true, questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    const input = screen.getByPlaceholderText(/type a custom answer/i)
    if (kind === 'custom') fireEvent.change(input, { target: { value: 'Keep my contract draft' } })
    else fireEvent.click(screen.getByText('Stable API'))
    fireEvent.click(screen.getByRole('radio', { name: /Approvals/ }))
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    if (kind === 'custom') expect(input).toHaveValue('Keep my contract draft')
    // Clearing the actual draft abandons a retired card, rather than retaining it forever.
    if (kind === 'custom') fireEvent.change(input, { target: { value: '' } })
    else fireEvent.click(screen.getByText('Stable API'))
    await waitFor(() => expect(screen.queryByText('Which contract?')).not.toBeInTheDocument())
  })

  it.each(['failed', 'uncertain', 'accepted-dismiss-failed'])('handles a retired draft send without losing or duplicating it (%s)', async outcome => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', card_id: 'card', questions: [{ question: 'Which contract?', options: [{ label: 'Stable API' }] }] }])
    const send = vi.spyOn(transport, 'sendTurn')
    if (outcome === 'failed') send.mockRejectedValue(new Error('Offline'))
    else if (outcome === 'uncertain') send.mockResolvedValue({ status: 'unknown', body: {} })
    else send.mockResolvedValue({ status: 'dispatched', body: {} })
    const dismiss = vi.spyOn(api, 'dismissQuestionCard').mockRejectedValue(new Error('Retirement failed'))
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    const input = screen.getByPlaceholderText(/type a custom answer/i)
    fireEvent.change(input, { target: { value: 'Drafted response' } })
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    if (outcome === 'accepted-dismiss-failed') {
      await waitFor(() => expect(screen.queryByText('Which contract?')).not.toBeInTheDocument())
      expect(dismiss).toHaveBeenCalledWith('worker', 'card')
      expect(screen.queryByRole('button', { name: 'Send answer' })).not.toBeInTheDocument()
    } else {
      await screen.findByRole('alert')
      expect(input).toHaveValue('Drafted response')
      expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
      expect(dismiss).not.toHaveBeenCalled()
    }
  })

  it('keeps every section accessible with compact labels in a 320px panel', async () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(320)
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { this.callback([{ target, contentRect: { width: 320 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    const approvals = await screen.findByRole('radio', { name: /Approvals/ })
    expect(approvals).toHaveTextContent('Approvals')
    fireEvent.click(approvals)
    expect(approvals).toHaveTextContent('Approvals')
    expect(screen.getByRole('radio', { name: /Overview/ })).toHaveTextContent('Overview')
    expect(screen.getByRole('radio', { name: /Questions/ })).toHaveTextContent('Questions')
  })

  it('keeps a worker answer draft while switching between questions and approvals', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'dashboard:worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(await screen.findByText('Stable API'))
    fireEvent.click(screen.getByRole('radio', { name: /Approvals/ }))
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeVisible()
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Normal')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(await screen.findByTestId('session-dynamic-dashboard', {}, LAZY_DASHBOARD_WAIT)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Send answer', hidden: true })).not.toBeVisible()
  })

  it('restores the Command Center draft when the chat card gets an unanswered 404', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask-cross', questions: [
      { question: 'Command Center choice?', options: [{ label: 'Command Center draft' }] },
    ] }])
    vi.spyOn(api, 'answerQuestion').mockRejectedValue(new ApiError(404, 'gone', JSON.stringify({ reason: 'expired' })))
    const store = taskStore()
    store.dispatch(setQuestionCard({ slot: 'worker', ask_id: 'ask-cross', questions: [
      { question: 'Chat choice?', options: [{ label: 'Chat draft' }] },
    ] }))
    const fallback = vi.fn()
    const { queryClient } = renderWithProviders(<>
      <PendingQuestionCard slotKey="worker" onFallbackSend={fallback} />
      <CommandCenterPanel slot="root" active />
    </>, { store })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(screen.getByText('Command Center draft'))
    fireEvent.click(screen.getByText('Chat draft'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() => expect(fallback).toHaveBeenCalledWith('Chat draft', [{ question: 'Chat choice?', options: [{ label: 'Chat draft' }] }]))
    expect(store.getState().chat.questionsSettled['ask-cross']).toBeUndefined()
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    await waitFor(() => expect(loadDrafts().worker).toBe('Command Center draft'))
  })

  it('restores a blocking draft when the Command Center inventory retires it', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask-1', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    const store = taskStore()
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(screen.getByText('Stable API'))

    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })

    await waitFor(() => expect(loadDrafts().worker).toBe('Stable API'))
    expect(screen.queryByText('Which contract?')).not.toBeInTheDocument()
    const restoredNotice = () => screen.queryAllByRole('status').find(el => /that session's composer/.test(el.textContent ?? ''))
    const notice = restoredNotice()
    expect(notice).toBeDefined()
    // Names the session and the question, so the user knows which composer holds the text.
    expect(notice).toHaveTextContent('The agent in "Review worker" stopped waiting for "Which contract?"')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    expect(restoredNotice()).toBe(notice)
    fireEvent.click(within(notice!).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(restoredNotice()).toBeUndefined())
  })

  it('lists a restored question notice without counting it as input needed', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask-1', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(screen.getByText('Stable API'))

    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })

    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent('The agent in "Review worker" stopped waiting for "Which contract?"')
    expect(screen.getByRole('radio', { name: 'Questions' })).toBeVisible()
  })

  it('clears an accepted blocking answer before the inventory retires it', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask-1', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    const answer = vi.spyOn(api, 'answerQuestion').mockResolvedValue({ ok: true })
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(screen.getByText('Stable API'))
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))

    await waitFor(() => expect(answer).toHaveBeenCalledWith('ask-1', { 'Which contract?': 'Stable API' }))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    expect(loadDrafts().worker).toBeUndefined()
    expect(screen.queryByText(/stopped waiting for/)).not.toBeInTheDocument()
  })

  it('does not restore a blocking draft retired while its answer is pending', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask-1', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    let releaseAnswer!: (value: { ok: boolean }) => void
    const answerPending = new Promise<{ ok: boolean }>(resolve => { releaseAnswer = resolve })
    vi.spyOn(api, 'answerQuestion').mockReturnValue(answerPending)
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(screen.getByText('Stable API'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(api.answerQuestion).toHaveBeenCalledWith('ask-1', { 'Which contract?': 'Stable API' }))

    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    expect(queryClient.getQueryData(['command-center', 'questions'])).toEqual([])
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(loadDrafts().worker).toBeUndefined()
    expect(screen.getByText('Which contract?')).toBeInTheDocument()
    expect(screen.queryByText(/stopped waiting for/)).not.toBeInTheDocument()

    await act(async () => { releaseAnswer({ ok: true }); await answerPending })
    await waitFor(() => expect(screen.queryByText('Which contract?')).not.toBeInTheDocument())
    expect(loadDrafts().worker).toBeUndefined()
  })

  it('does not resurrect an accepted stateless answer after refetch', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', card_id: 'card-1', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'dispatched', body: {} })
    vi.spyOn(api, 'dismissQuestionCard').mockResolvedValue({ ok: true })
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(screen.getByText('Stable API'))
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))

    await waitFor(() => expect(send).toHaveBeenCalledWith({ slot: 'worker', message: 'Which contract?: Stable API' }))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    await waitFor(() => expect(screen.queryByText('Your response was recorded.')).not.toBeInTheDocument())
    expect(screen.queryByText('Which contract?')).not.toBeInTheDocument()
  })

  it('keeps a failed blocking answer draft for retirement recovery', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask-1', questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    vi.spyOn(api, 'answerQuestion').mockRejectedValue(new Error('Offline'))
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('radio', { name: 'Questions 1' }))
    fireEvent.click(screen.getByText('Stable API'))
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Offline')

    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    await waitFor(() => expect(loadDrafts().worker).toBe('Stable API'))
    expect(screen.queryAllByRole('status').some(el => /The agent in "Review worker" stopped waiting for "Which contract\?"/.test(el.textContent ?? ''))).toBe(true)
  })

})

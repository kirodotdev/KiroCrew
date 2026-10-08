import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api, ApiError } from '../api/client'
import * as transport from '../chat-core/transport/sendTurn'
import { createTestStore, renderWithProviders } from './helpers'
import { sseConnected, sseDisconnected, sseSlots } from '../store/dashboardSlice'
import CommandCenterPanel from '../pages/chat/command-center/CommandCenterPanel'
import CommandCenterDock from '../pages/chat/command-center/CommandCenterDock'
import PendingQuestionCard from '../components/PendingQuestionCard'
import { setQuestionCard } from '../store/chatSlice'
import { REQUEST_PUBLISHED_VIEW } from '../pages/chat/command-center/commandCenter.prompt'
import { __resetForTests, loadDrafts } from '../utils/chatDrafts'
import { __resetSettledLatchesForTests } from '../pages/chat/command-center/useCommandCenter'

vi.mock('../pages/chat/command-center/TaskDashboardFrame', () => ({
  default: ({ artifact }: { artifact: { name: string } }) => <div data-testid="published-task-view">{artifact.name}</div>,
}))

function taskStore() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Conductor', messages: 0, running: true },
    { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: false },
  ] } })
}

describe('task dashboard host controls', () => {
  afterEach(() => vi.unstubAllGlobals())
  beforeEach(() => {
    vi.restoreAllMocks()
    __resetSettledLatchesForTests()
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { dynamic_dashboard_cards: false } })
    vi.spyOn(api, 'dashboardCard').mockResolvedValue({ card: null, status: 'waiting', published_at: null, content_event_at: null, stale: false })
    localStorage.clear()
    __resetForTests()
    // happy-dom has no layout; establish the panel width that selects tabs.
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(480)
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'sessionWorkProjection').mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'dispatched', status: 'blocked', summary: 'Needs evidence' },
    ] } })
    vi.spyOn(api, 'artifacts').mockResolvedValue({ artifacts: [] })
  })

  it('says a part is missing, not that fresh decisions are stale, when an optional source fails', async () => {
    vi.mocked(api.workflowRuns).mockRejectedValue(new Error('workflows not available'))
    renderWithProviders(<><CommandCenterPanel slot="root" active /><CommandCenterDock slot="root" onOpen={() => {}} /></>, { store: taskStore() })
    expect(await screen.findAllByText('Some sources could not be loaded: workflow runs under “Progress”. Anything that needs you is still current, and this notice clears once they load.')).toHaveLength(2)
    // The exact text above names only the failed source; the others are not listed.
    expect(screen.queryByText(/The last known state may be out of date/)).not.toBeInTheDocument()
    // The sources that did load still render: the dock's Blocked readout, not a
    // stale placeholder. (The panel draws no tiles of its own any more.)
    expect(screen.getByRole('button', { name: 'Blocked 1' })).toBeInTheDocument()
    expect(screen.getByTestId('command-center-panel')).toBeInTheDocument()
  })

  it('shows the automatic card while no page is published, and requests one only after a click', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'queued', body: {} })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByRole('button', { name: 'Create published view' })).toBeEnabled()
    // No page yet: the gateway's own card stands in. Nothing native beside it —
    // the tiles, the progress bar and the board rows belong to the dock and to
    // the page the agent is asked for.
    expect(screen.getByTestId('session-status-frame')).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByTestId('status-tiles')).not.toBeInTheDocument()
    expect(screen.queryByText('Accepted contract')).not.toBeInTheDocument()
    expect(screen.queryByText('Automatic cards for all sessions')).not.toBeInTheDocument()
    expect(send).not.toHaveBeenCalled()
    expect(screen.getByText('Permission mode: Normal')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create published view' }))
    expect(await screen.findByText('Published view requested — it will appear here when ready.')).toBeInTheDocument()
    expect(send).toHaveBeenCalledWith({ slot: 'root', message: REQUEST_PUBLISHED_VIEW, steer: 'auto' })
    expect(screen.getByRole('button', { name: 'Create published view' })).toBeDisabled()
  })

  it('places a crew published view and task views in one selector without remounting the crew view', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'release', name: 'Release pipeline', kind: 'html', tags: ['task-dashboard'], session_key: 'dashboard:root' }] } as never)
    renderWithProviders(<CommandCenterPanel slot="root" active publishedView={{ title: 'Oncall', content: <input aria-label="Published filter" defaultValue="" /> }} />, { store: taskStore() })
    const view = screen.getByRole('textbox', { name: 'Published filter' })
    fireEvent.change(view, { target: { value: 'release' } })
    expect(screen.queryByRole('button', { name: 'Create published view' })).not.toBeInTheDocument()
    const select = await screen.findByRole('combobox', { name: 'Published view' })
    fireEvent.pointerDown(select, { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('option', { name: 'Release pipeline' }))
    expect(screen.getByTestId('published-task-view')).toBeVisible()
    expect(view).toBeInTheDocument()
    expect(view).not.toBeVisible()
    fireEvent.pointerDown(select, { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('option', { name: 'Oncall' }))
    expect(screen.getByRole('textbox', { name: 'Published filter' })).toBe(view)
    expect(view).toHaveValue('release')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    expect(view).not.toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(view).toBeVisible()
  })

  it('keeps the crew publication readable during thread revalidation without exposing native task controls', () => {
    renderWithProviders(<CommandCenterPanel slot="root" active sessionReady={false} publishedView={{ title: 'Oncall', content: <p>Published pipeline summary</p> }} />, { store: taskStore() })
    expect(screen.getByText('Published pipeline summary')).toBeVisible()
    expect(screen.queryByRole('radio', { name: /Approvals/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create published view' })).not.toBeInTheDocument()
    expect(screen.queryByText('Permission mode: Normal')).not.toBeInTheDocument()
    expect(api.approvals).not.toHaveBeenCalled()
    expect(api.artifacts).not.toHaveBeenCalled()
  })

  it('keeps a refused design request retryable without claiming a dashboard exists', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'refused', reason: 'Session is unavailable', body: {} })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    fireEvent.click(screen.getByRole('button', { name: 'Create published view' }))
    expect(await screen.findByText('Session is unavailable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create published view' })).toBeEnabled()
    expect(send).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Published view requested — it will appear here when ready.')).not.toBeInTheDocument()
  })

  it('shows failed runs as alerts in the dock, without a draft-destroying agent hand-off', async () => {
    vi.mocked(api.workflowRuns).mockResolvedValue({ runs: [{ run_id: 'failed', name: 'Validation', session_key: 'dashboard:root', status: 'failed', error: 'Runner unavailable', last_log: 'Preparing checks' }] })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('button', { name: 'Blocked 2' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Runner unavailable')
    expect(screen.getByText('Preparing checks')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
  })

  it('renders a published view as the whole Overview, with the questions and approvals kept to their own tabs', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'release', name: 'Release pipeline', kind: 'html', tags: ['task-dashboard'], session_key: 'dashboard:root' }] } as never)
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'worker', ask_id: 'ask', questions: [{ question: 'Which contract?', options: [{ label: 'Stable API' }] }] }])
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    const initial = taskStore().getState()
    const running = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, running: true })) } })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: running })
    const frame = await screen.findByTestId('published-task-view')
    expect(frame).toBeVisible()
    const overview = screen.getByTestId('command-center-overview')
    // The page is the Overview: no native tiles, progress bar, settings switch,
    // automatic card, blocked list or board rows beside it.
    expect(overview).toContainElement(frame)
    expect(screen.queryByTestId('status-tiles')).not.toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByText('Automatic cards for all sessions')).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-status-frame')).not.toBeInTheDocument()
    expect(screen.queryByText('Accepted contract')).not.toBeInTheDocument()
    expect(screen.queryByText('Review changes')).not.toBeInTheDocument()
    expect(screen.queryByText('Conductor')).not.toBeInTheDocument()
    expect(screen.queryByText('Create published view')).not.toBeInTheDocument()
    expect(within(overview).queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
    expect(within(overview).queryByText('Which contract?')).not.toBeInTheDocument()
    // The segments still carry the counts, and each tab still lists its cards:
    // answering and approving stay with the host's own controls.
    expect(await screen.findByRole('radio', { name: 'Approvals 1' })).toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: 'Questions 1' }))
    expect(screen.getByText('Which contract?')).toBeVisible()
    expect(frame).not.toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: 'Approvals 1' }))
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeVisible()
    expect(screen.getByText('Which contract?')).not.toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(frame).toBeVisible()
  })

  it('shows no attention card in the Overview even while nothing is published', async () => {
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByRole('radio', { name: 'Approvals 1' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Approve once', hidden: true })).not.toBeVisible()
    expect(screen.getByTestId('session-status-frame')).toBeVisible()
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
    expect(screen.getByRole('button', { name: 'Create published view' })).toBeVisible()
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

  it.each(['disconnected', 'query failure'])('announces a stale dock through the shared error notice (%s)', async (failure) => {
    const store = taskStore()
    const state = store.getState()
    if (failure === 'query failure') vi.mocked(api.approvals).mockRejectedValue(new Error('Offline'))
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, {
      store: failure === 'disconnected' ? createTestStore({ ...state, dashboard: { ...state.dashboard, connected: false } }) : store,
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('Some sources are unavailable.')
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
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

  it('names approval-only session state Needs input, not Questions', async () => {
    const initial = taskStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, pending_approval: s.key === 'worker' })) } })
    renderWithProviders(<><CommandCenterPanel slot="root" active /><CommandCenterDock slot="root" onOpen={vi.fn()} /></>, { store })
    expect(await screen.findByRole('radio', { name: 'Approvals 1' })).toBeVisible()
    expect(screen.getByRole('radio', { name: 'Questions' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Needs you 1' }))
    expect(within(screen.getByRole('region', { name: 'Needs you' })).getByText('Approval')).toBeInTheDocument()
  })

  it('opens one tile at a time, closes it on a second click, hides to a dot that keeps the count, and opens the existing panel', async () => {
    const open = vi.fn()
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterDock slot="root" onOpen={open} />, { store: taskStore() })
    const needsYou = await screen.findByRole('button', { name: 'Needs you 1' })
    expect(screen.getByRole('group', { name: 'Dashboard' })).toContainElement(needsYou)
    expect(screen.getByRole('button', { name: 'Progress 1/2' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: 'Blocked 1' })).toBeVisible()
    // Two actions beside the three disclosures, so the row stays under the button cap.
    expect(screen.getByTestId('status-tiles').querySelectorAll(':scope > button, :scope > div:not([role=group]) button')).toHaveLength(2)
    // No explanatory sentence on the dock: numbers, a disclosure, two controls.
    expect(screen.queryByText(/Running \d/)).not.toBeInTheDocument()
    fireEvent.click(needsYou)
    expect(needsYou).toHaveAttribute('aria-expanded', 'true')
    expect(needsYou).toHaveAttribute('aria-controls', screen.getByRole('region', { name: 'Needs you' }).id)
    // The row titles the command the way the transcript does; the control stays in the panel.
    expect(screen.getByText('Git status')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Blocked 1' }))
    expect(needsYou).toHaveAttribute('aria-expanded', 'false')
    expect(needsYou).not.toHaveAttribute('aria-controls')
    expect(screen.getByText('Needs evidence')).toBeInTheDocument()
    // The row itself is the hand-off, named by its item; no row carries an Open
    // Dashboard of its own, so the dock-level button stays the one labelled hand-off.
    const blockedRow = within(screen.getByRole('region', { name: 'Blocked' })).getByRole('button', { name: 'Review changes' })
    expect(blockedRow).toHaveAccessibleDescription('Needs evidence')
    expect(screen.getAllByRole('button', { name: 'Open Dashboard' })).toHaveLength(1)
    // A blocked item belongs to the Blocked tile alone, never to Progress.
    fireEvent.click(screen.getByRole('button', { name: 'Progress 1/2' }))
    expect(within(screen.getByRole('region', { name: 'Progress' })).queryByText('Review changes')).not.toBeInTheDocument()
    // With a board the number is work done, so the list needs no sentence about runs.
    expect(within(screen.getByRole('region', { name: 'Progress' })).queryByText(/showing what’s running now/)).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Blocked' })).not.toBeInTheDocument()
    // Clicking the open tile closes its list.
    fireEvent.click(screen.getByRole('button', { name: 'Progress 1/2' }))
    expect(screen.getByRole('button', { name: 'Progress 1/2' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Hide status tiles' }))
    expect(screen.queryByRole('button', { name: 'Needs you 1' })).not.toBeInTheDocument()
    // The pill's name carries its visible title and the count, so a screen reader
    // hears what is waiting; its visible text names what was hidden beside the count.
    const dot = screen.getByRole('button', { name: 'Dashboard: Needs you: 1' })
    expect(dot).toHaveTextContent('1')
    expect(dot).toHaveTextContent('Dashboard')
    expect(localStorage.getItem('mc-task-dashboard-hidden')).toBe('1')
    fireEvent.click(dot)
    fireEvent.click(within(screen.getByTestId('status-tiles')).getByRole('button', { name: 'Open Dashboard' }))
    expect(open).toHaveBeenCalledTimes(1)
  })

  it('lists running sessions when an omitted work board cannot supply progress', async () => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'hidden-work', title: 'Omitted board item', state: 'dispatched' },
    ], omitted: 1 } })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('button', { name: 'Progress 1 running' }))
    const progress = screen.getByRole('region', { name: 'Progress' })
    expect(within(progress).getByText('Conductor')).toBeInTheDocument()
    expect(within(progress).queryByText('Omitted board item')).not.toBeInTheDocument()
  })

  it('shows the dock for a lone session as soon as a question waits on the user', async () => {
    const initial = createTestStore().getState()
    const lone = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [{ key: 'root', title: 'Solo', messages: 0, running: true }] } })
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [] } })
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'root', ask_id: 'q1', questions: [{ question: 'Which scope?', options: [{ label: 'A' }] }] }])
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: lone })
    expect(await screen.findByRole('button', { name: 'Needs you 1' })).toBeVisible()
  })

  it('disappears once every run rests and the plan is complete, but not while a plan is open', async () => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'accepted' },
    ] } })
    const initial = taskStore().getState()
    const rest = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, running: false })) } })
    const { unmount } = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: rest })
    // Mounted while the inventory loads: an empty half-read must not count as settled.
    expect(screen.getByTestId('command-center-dock')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    unmount()
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'dispatched' },
    ] } })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: rest })
    expect(await screen.findByRole('button', { name: 'Progress 1/2' })).toBeVisible()
  })

  it('reads the board for a lone slot whose published view keeps the dock on screen, and settles only on what it holds', async () => {
    const initial = createTestStore().getState()
    const lone = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [{ key: 'root', title: 'Solo', messages: 0, running: false }] } })
    const openItem = [{ item_id: 'one', title: 'Review changes', state: 'dispatched' }]
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: openItem } } as never)
    // No view and no team: the whole-log fold stays unread, so the open item
    // costs nothing here and the dock has no reason to show.
    const bare = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: lone })
    await waitFor(() => expect(api.artifacts).toHaveBeenCalled())
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(api.sessionWorkProjection).not.toHaveBeenCalled()
    expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument()
    bare.unmount()
    // A published view for this slot keeps the dock relevant on its own, so the
    // board is read too: its open item is what stops the verdict.
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'release', name: 'Release pipeline', kind: 'html', tags: ['task-dashboard'], session_key: 'dashboard:root' }] } as never)
    const open = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: lone })
    expect(await screen.findByRole('button', { name: 'Progress 0/1' })).toBeVisible()
    expect(api.sessionWorkProjection).toHaveBeenCalledTimes(1)
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(screen.getByTestId('command-center-dock')).toBeInTheDocument()
    open.unmount()
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [{ item_id: 'one', title: 'Review changes', state: 'accepted' }] } })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: lone })
    // The dock is not on screen until the view lands, so wait for the read it
    // triggers rather than for an absence that holds vacuously at mount.
    await waitFor(() => expect(api.sessionWorkProjection).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
  })

  it('opens the Progress list with the sentence its number needs: the plan, the runs, or nothing for a board', async () => {
    const initial = taskStore().getState()
    const running = initial.dashboard.slots.map(s => ({ ...s, running: true }))
    // A plan supplies the number while the rows are the runs, so the lead says so.
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [] } })
    const plan = { tasks: [{ id: '1', text: 'Build', completed: true }, ...['2', '3', '4', '5'].map(id => ({ id, text: `Step ${id}`, completed: false }))], total: 5, completed: 1 }
    const planned = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: running.map(s => s.key === 'root' ? { ...s, todo: plan } : s) } })
    const first = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: planned })
    fireEvent.click(await screen.findByRole('button', { name: 'Progress 1/5' }))
    const planList = screen.getByRole('region', { name: 'Progress' })
    expect(within(planList).getByText('Progress counts the plan; this list shows what’s running now.')).toBeInTheDocument()
    expect(within(planList).queryByText('No task list yet — showing what’s running now.')).not.toBeInTheDocument()
    expect(within(planList).getByText('Conductor')).toBeInTheDocument()
    first.unmount()
    // A board supplies both the number and the rows: no sentence to reconcile.
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'dispatched' },
    ] } })
    const second = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: planned })
    fireEvent.click(await screen.findByRole('button', { name: 'Progress 1/2' }))
    const boardList = screen.getByRole('region', { name: 'Progress' })
    expect(within(boardList).getByText('Review changes')).toBeInTheDocument()
    expect(within(boardList).queryByText(/Progress counts the plan/)).not.toBeInTheDocument()
    expect(within(boardList).queryByText(/No task list yet/)).not.toBeInTheDocument()
    second.unmount()
    // Neither: the number is the running count, and the lead says that.
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [] } })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: running } }) })
    fireEvent.click(await screen.findByRole('button', { name: 'Progress 2 running' }))
    const runList = screen.getByRole('region', { name: 'Progress' })
    expect(within(runList).getByText('No task list yet — showing what’s running now.')).toBeInTheDocument()
    expect(within(runList).queryByText(/Progress counts the plan/)).not.toBeInTheDocument()
  })

  it.each(['pending', 'errored'])('does not settle while an optional source is %s, and settles once it answers', async outcome => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'accepted' },
    ] } })
    let answer: (value: { artifacts: never[] }) => void = () => {}
    if (outcome === 'pending') vi.mocked(api.artifacts).mockReturnValue(new Promise(resolve => { answer = resolve }))
    else vi.mocked(api.artifacts).mockRejectedValue(new Error('views not available'))
    const initial = taskStore().getState()
    const rest = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, running: false })) } })
    const { queryClient } = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: rest })
    // Every run rests and the board is complete, so the model is settled and
    // both decision sources have answered. The verdict still waits on the
    // published views, an optional source.
    expect(await screen.findByRole('button', { name: 'Progress 2/2' })).toBeVisible()
    if (outcome === 'errored') expect(await screen.findByRole('alert')).toHaveTextContent('Some sources could not be loaded')
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(screen.getByTestId('command-center-dock')).toBeInTheDocument()
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
    if (outcome === 'pending') act(() => answer({ artifacts: [] }))
    else await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'artifacts'] }) })
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
  })

  it('keeps a settled dock gone through a connection drop, and brings it back for a new question', async () => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'accepted' },
    ] } })
    const initial = taskStore().getState()
    const rest = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, running: false })) } })
    const { queryClient } = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: rest })
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    // A websocket drop makes the inventory stale, which is not evidence of new
    // work: the task that was over stays over.
    act(() => { rest.dispatch(sseDisconnected()) })
    expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // A question waiting on the user is: the dock returns with its Needs you tile.
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'root', ask_id: 'q1', questions: [{ question: 'Which scope?', options: [{ label: 'A' }] }] }])
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['command-center', 'questions'] }) })
    expect(await screen.findByRole('button', { name: 'Needs you 1' })).toBeVisible()
  })

  it('keeps a settled dock gone after a disconnected remount until a complete read shows new work', async () => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'accepted' },
    ] } })
    const initial = taskStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, running: false })) } })
    const first = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store })
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    first.unmount()

    vi.mocked(api.sessionWorkProjection).mockClear()
    act(() => { store.dispatch(sseDisconnected()) })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store })
    await waitFor(() => expect(api.sessionWorkProjection).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument()

    act(() => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots([
        { key: 'root', title: 'Conductor', messages: 0, running: true },
        { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: true },
      ]))
    })
    expect(await screen.findByTestId('command-center-dock')).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('does not apply one root settled latch to another root', async () => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [
      { item_id: 'one', title: 'Accepted contract', state: 'accepted' },
      { item_id: 'two', title: 'Review changes', state: 'accepted' },
    ] } })
    const initial = taskStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, running: false })) } })
    const first = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store })
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    first.unmount()

    act(() => {
      store.dispatch(sseDisconnected())
      store.dispatch(sseSlots([
        { key: 'root', title: 'Conductor', messages: 0, running: false },
        { key: 'other', title: 'Other conductor', messages: 0, running: true },
        { key: 'other-worker', title: 'Other worker', created_by: 'other', messages: 0, running: true },
      ]))
    })
    renderWithProviders(<CommandCenterDock slot="other" onOpen={vi.fn()} />, { store })
    expect(await screen.findByRole('button', { name: 'Progress 2 running' })).toBeVisible()
    expect(screen.getByRole('alert')).toHaveTextContent('Some sources are unavailable')
  })

  it('keeps a lone session dock settled while the panel beside it still holds an open board item', async () => {
    // The dock of a lone slot with no published view leaves the board unread;
    // the panel always reads it. Their verdicts can legitimately differ, so each
    // latches its own: the panel's open item must not erase the dock's rest.
    vi.mocked(api.workflowRuns).mockResolvedValue({ runs: [{ run_id: 'wf-1', name: 'Nightly audit', status: 'finished', session_key: 'root' }] } as never)
    const initial = createTestStore().getState()
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
      { key: 'root', title: 'Conductor', messages: 0, running: false },
    ] } })
    renderWithProviders(<><CommandCenterDock slot="root" onOpen={vi.fn()} /><CommandCenterPanel slot="root" active /></>, { store })
    // The panel reads the board (its open item keeps the panel's own model
    // unsettled); the dock, which does not, has only a finished run to show and
    // settles away.
    await waitFor(() => expect(api.sessionWorkProjection).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    expect(screen.getByTestId('command-center-panel')).toBeInTheDocument()
    // A connection drop makes every read incomplete. The dock's latch, had the
    // panel released it, would let the drop bring a finished dock back.
    act(() => { store.dispatch(sseDisconnected()) })
    expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument()
    expect(screen.getByTestId('command-center-panel')).toBeInTheDocument()
  })

  it('says what the Progress tile counts without a plan, under the same label', async () => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [] } })
    const initial = taskStore().getState()
    // Two running sessions and no board: the number is runs, not work done.
    const both = createTestStore({ ...initial, dashboard: { ...initial.dashboard, slots: initial.dashboard.slots.map(s => ({ ...s, running: true })) } })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: both })
    const progress = await screen.findByRole('button', { name: 'Progress 2 running' })
    expect(progress).toHaveTextContent('2 running')
    expect(within(progress).getByText('Progress')).toBeInTheDocument()
    // The cell says what it counts; the title says why no done count shows.
    expect(progress).toHaveAttribute('title', 'No task list yet — showing what’s running now.')
    // The disclosed list opens with that same sentence as a row, not a hover.
    fireEvent.click(progress)
    const list = screen.getByRole('region', { name: 'Progress' })
    expect(within(list).getByText('No task list yet — showing what’s running now.')).toBeInTheDocument()
    expect(within(list).getByText('Conductor')).toBeInTheDocument()
    fireEvent.click(progress)
    // Blocked and Needs you both read as "stuck": each title names who acts.
    expect(screen.getByRole('button', { name: 'Blocked 0' })).toHaveAttribute('title', 'Waiting on the agent — runs or items that are stuck')
    expect(screen.getByRole('button', { name: 'Needs you 0' })).toHaveAttribute('title', 'Waiting on you — questions and approvals')
  })

  it('labels the open button beside its icon and keeps the row at two actions', async () => {
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: taskStore() })
    await screen.findByRole('button', { name: 'Progress 1/2' })
    const tiles = screen.getByTestId('status-tiles')
    const open = within(tiles).getByRole('button', { name: 'Open Dashboard' })
    expect(open).toHaveTextContent('Open Dashboard')
    expect(open.querySelector('svg')).not.toBeNull()
    expect(tiles.querySelectorAll(':scope > div:not([role=group]) button')).toHaveLength(2)
  })

  it('shows the dock for work that arrives while disconnected, arming the settled latch only after a real read', async () => {
    vi.mocked(api.sessionWorkProjection).mockResolvedValue({ value: { items: [] } })
    const initial = createTestStore().getState()
    // Mounted before any slot list has landed, with the socket down: nothing is
    // readable yet, so the empty model must not count as a settled task.
    const store = createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: false, slots: [] } })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store })
    expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument()
    // Running work lands while still disconnected: the inventory is stale, and
    // stale is not settled, so the dock shows with its stale notice.
    act(() => { store.dispatch(sseSlots([
      { key: 'root', title: 'Conductor', messages: 0, running: true },
      { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: true },
    ])) })
    expect(await screen.findByRole('button', { name: 'Progress 2 running' })).toBeVisible()
    expect(screen.getByRole('alert')).toHaveTextContent('Some sources are unavailable')
    // Reconnected with everything resting: the first complete read settles it.
    act(() => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots([
        { key: 'root', title: 'Conductor', messages: 0, running: false },
        { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: false },
      ]))
    })
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
  })

  it('opens the panel from a Needs-you row, which is named by its request and mounts no control', async () => {
    const open = vi.fn()
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterDock slot="root" onOpen={open} />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('button', { name: 'Needs you 1' }))
    const row = within(screen.getByRole('region', { name: 'Needs you' })).getByRole('button', { name: 'Git status' })
    // One request, one singular badge; the plural "Approvals" stays a tab name.
    expect(row).toHaveAccessibleDescription('Approval')
    // The trailing open-panel glyph says the click shows the item rather than
    // performing it, and adds nothing to the row's name.
    expect(row.querySelector('svg.lucide-panel-right-open')).toHaveAttribute('aria-hidden', 'true')
    expect(row).toHaveAccessibleName('Git status')
    expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument()
    fireEvent.click(row)
    expect(open).toHaveBeenCalledTimes(1)
  })

  it('mentions the agent-designed page in the panel help only once a published view exists', async () => {
    const first = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    await screen.findByRole('button', { name: 'Create published view' })
    expect(screen.getByRole('button', { name: 'More information' })).toHaveAttribute('title', expect.not.stringContaining('agent-designed page'))
    first.unmount()
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'release', name: 'Release pipeline', kind: 'html', tags: ['task-dashboard'], session_key: 'dashboard:root' }] } as never)
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    await screen.findByTestId('published-task-view')
    expect(screen.getByRole('button', { name: 'More information' })).toHaveAttribute('title', expect.stringContaining('The agent-designed page cannot approve actions.'))
  })

  it('hands focus to the counterpart control when the tiles are hidden or shown from the dock', async () => {
    // A mount that starts hidden takes nothing: only the dock's own toggle moves focus.
    localStorage.setItem('mc-task-dashboard-hidden', '1')
    const first = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: taskStore() })
    expect(await screen.findByRole('button', { name: 'Dashboard: Show status tiles' })).not.toHaveFocus()
    first.unmount()
    localStorage.clear()
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: taskStore() })
    const hide = await screen.findByRole('button', { name: 'Hide status tiles' })
    hide.focus()
    fireEvent.click(hide)
    const dot = screen.getByRole('button', { name: 'Dashboard: Show status tiles' })
    expect(dot).toHaveFocus()
    // Quiet form: no count, but the pill still says what it stands for.
    expect(dot).toHaveTextContent('Dashboard')
    fireEvent.click(dot)
    expect(screen.getByRole('button', { name: 'Hide status tiles' })).toHaveFocus()
  })
})

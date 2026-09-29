import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import * as transport from '../chat-core/transport/sendTurn'
import { createTestStore, renderWithProviders } from './helpers'
import CommandCenterPanel, { PANEL_HEADING_ATTR } from '../pages/chat/command-center/CommandCenterPanel'
import CommandCenterDock from '../pages/chat/command-center/CommandCenterDock'
import { REQUEST_PUBLISHED_VIEW } from '../pages/chat/command-center/commandCenter.prompt'

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

// The DOM test environment has no layout. Model heading boxes independently of
// the focus helper, including CSS-hidden ancestors, and restore the spy per test.
function mockPanelHeadingRects() {
  function isRendered(el: HTMLElement): boolean {
    return !el.hidden && getComputedStyle(el).display !== 'none'
      && (!el.parentElement || isRendered(el.parentElement))
  }
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
    const rects: DOMRect[] = []
    if (this.hasAttribute(PANEL_HEADING_ATTR) && isRendered(this)) {
      rects.push(new DOMRect(0, 0, 100, 20))
    }
    return Object.assign(rects, { item: (index: number) => rects[index] ?? null })
  })
}

describe('task dashboard host controls', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { dynamic_dashboard_cards: false } })
    vi.spyOn(api, 'dashboardCard').mockResolvedValue({ card: null, status: 'waiting', published_at: null, content_event_at: null, stale: false })
    localStorage.clear()
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

  it('shows accepted progress and requests an authored dashboard only after a click', async () => {
    const send = vi.spyOn(transport, 'sendTurn').mockResolvedValue({ status: 'queued', body: {} })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByText('Accepted contract')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '1')
    expect(screen.getByRole('progressbar')).toHaveAttribute('max', '2')
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

  it('shows failed runs as alerts without a draft-destroying agent hand-off', async () => {
    vi.mocked(api.workflowRuns).mockResolvedValue({ runs: [{ run_id: 'failed', name: 'Validation', session_key: 'dashboard:root', status: 'failed', error: 'Runner unavailable', last_log: 'Preparing checks' }] })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    expect(await screen.findByRole('alert')).toHaveTextContent('Runner unavailable')
    expect(screen.getByText('Preparing checks')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
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
    await screen.findByText('Accepted contract')
    const approvals = screen.getByRole('radio', { name: /Approvals/ })
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
    await screen.findByText('Accepted contract')
    fireEvent.click(screen.getByRole('radio', { name: /Approvals/ }))
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
    await screen.findByText('Accepted contract')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    fireEvent.click(await screen.findByText('Stable API'))
    fireEvent.click(screen.getByRole('radio', { name: /Approvals/ }))
    expect(screen.getByRole('button', { name: 'Approve once' })).toBeVisible()
    expect(screen.getByText(/Approval required/)).toHaveTextContent('Normal')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    expect(screen.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    fireEvent.click(screen.getByRole('radio', { name: /Overview/ }))
    expect(screen.getByText('Accepted contract')).toBeVisible()
  })

  it.each(['custom', 'option'])('retains a retired stateless %s draft across polls and section navigation', async kind => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([{ slot: 'dashboard:worker', card_id: 'card-1', native: true, questions: [
      { question: 'Which contract?', options: [{ label: 'Stable API' }] },
    ] }])
    const { queryClient } = renderWithProviders(<CommandCenterPanel slot="root" active />, { store: taskStore() })
    await screen.findByText('Accepted contract')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
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
    await screen.findByText('Accepted contract')
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
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
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store })
    expect(await screen.findByText('Needs input')).toBeVisible()
    expect(screen.getByRole('radio', { name: /Questions/ })).toBeVisible()
  })

  it('names the one-time outcome on the card, has no collapse toggle, and opens the existing panel', async () => {
    const open = vi.fn()
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    renderWithProviders(<CommandCenterDock slot="root" onOpen={open} />, { store: taskStore() })
    await screen.findByText('Needs you: 1')
    // Nothing is collapsible: the card is a hint that goes away once used.
    expect(screen.queryByRole('button', { name: /summary/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { expanded: true })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { expanded: false })).not.toBeInTheDocument()
    expect(screen.getByText('Running 1 · Blocked 1 · Approvals 1')).toBeVisible()
    // The outcome is announced with the button, not just printed beside it.
    const button = screen.getByRole('button', { name: 'Dashboard Needs you: 1' })
    const hint = screen.getByText("Opens the Dashboard panel. This hint won't show again.")
    expect(hint).toBeVisible()
    expect(button).toHaveAttribute('aria-describedby', hint.id)
    expect(button).toHaveAccessibleDescription("Opens the Dashboard panel. This hint won't show again.")
    fireEvent.click(button)
    expect(open).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
  })

  // The chat mounts the panel when its tab opens; the Crew page and a revisited
  // chat tab keep it mounted and merely un-hide it. Focus must land in it either way.
  it.each(['mounts on open', 'is un-hidden on open'])('moves focus from the clicked card into the opened panel when the panel %s', async mode => {
    mockPanelHeadingRects()
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    function Host() {
      const [open, setOpen] = useState(false)
      return <>
        <CommandCenterDock slot="root" onOpen={() => setOpen(true)} />
        {(open || mode === 'is un-hidden on open') && <div hidden={!open}><CommandCenterPanel slot="root" active={open} /></div>}
      </>
    }
    renderWithProviders(<Host />, { store: taskStore() })
    const button = await screen.findByRole('button', { name: 'Dashboard Needs you: 1' })
    // A keyboard user activates the focused button; the card then unmounts under them.
    button.focus()
    expect(document.activeElement).toBe(button)
    fireEvent.click(button)
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { level: 2, name: 'Dashboard' })))
    expect(document.activeElement).not.toBe(document.body)
  })

  it('skips a CSS-hidden heading and retries until the visible panel mounts', async () => {
    mockPanelHeadingRects()
    function Host() {
      const [open, setOpen] = useState(false)
      return <>
        <CommandCenterDock slot="root" onOpen={() => setOpen(true)} />
        <div style={{ display: 'none' }}>
          <h2 tabIndex={-1} {...{ [PANEL_HEADING_ATTR]: '' }}>Hidden dashboard</h2>
        </div>
        {open && <CommandCenterPanel slot="root" active />}
      </>
    }
    renderWithProviders(<Host />, { store: taskStore() })
    const hiddenHeading = screen.getByText('Hidden dashboard')
    const focusHidden = vi.spyOn(hiddenHeading, 'focus')
    expect(hiddenHeading.closest('[hidden]')).toBeNull()
    expect(hiddenHeading.getClientRects()).toHaveLength(0)
    expect(screen.queryByRole('heading', { name: 'Dashboard' })).not.toBeInTheDocument()
    const button = await screen.findByRole('button', { name: 'Dashboard' })
    button.focus()
    fireEvent.click(button)
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Dashboard' })))
    expect(focusHidden).not.toHaveBeenCalled()
  })

  it('does not come back for that session once clicked, but still shows for another session', async () => {
    const open = vi.fn()
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    const store = taskStore()
    const view = renderWithProviders(<CommandCenterDock slot="root" onOpen={open} />, { store })
    fireEvent.click(await screen.findByRole('button', { name: 'Dashboard Needs you: 1' }))
    expect(open).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    view.unmount()
    // A remount (reload, session switch and back) reads the stored dismissal.
    renderWithProviders(<CommandCenterDock slot="root" onOpen={open} />, { store })
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument()
    expect(localStorage.getItem('mc-task-dashboard-dismissed:root')).toBe('1')
    expect(localStorage.getItem('mc-task-dashboard-dismissed:other')).toBeNull()
  })

  it('still hides the clicked card for this mount when the dismissal cannot be persisted', async () => {
    const open = vi.fn()
    vi.mocked(api.approvals).mockResolvedValue([{ id: 'permission', slot: 'worker', tool: 'shell', tool_input: 'git status' }])
    // Storage full or blocked: safeSetItem swallows the throw and returns false.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    renderWithProviders(<CommandCenterDock slot="root" onOpen={open} />, { store: taskStore() })
    fireEvent.click(await screen.findByRole('button', { name: 'Dashboard Needs you: 1' }))
    expect(open).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument())
    expect(localStorage.getItem('mc-task-dashboard-dismissed:root')).toBeNull()
  })

  it('issues no command-center reads for a dismissed session, while an undismissed one still reads', async () => {
    localStorage.setItem('mc-task-dashboard-dismissed:root', '1')
    const store = taskStore()
    const view = renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store })
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('command-center-dock')).not.toBeInTheDocument()
    expect(api.pendingQuestions).not.toHaveBeenCalled()
    expect(api.approvals).not.toHaveBeenCalled()
    expect(api.workflowRuns).not.toHaveBeenCalled()
    expect(api.sessionWorkProjection).not.toHaveBeenCalled()
    expect(api.artifacts).not.toHaveBeenCalled()
    view.unmount()
    // Control: the same spies fire for a session that was never dismissed.
    localStorage.removeItem('mc-task-dashboard-dismissed:root')
    renderWithProviders(<CommandCenterDock slot="root" onOpen={vi.fn()} />, { store: taskStore() })
    await waitFor(() => expect(api.approvals).toHaveBeenCalled())
    expect(api.pendingQuestions).toHaveBeenCalled()
  })
})

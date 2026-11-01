import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), sessionSummary: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
const panel = vi.hoisted(() => ({ openPanelView: vi.fn() }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: panel.openPanelView }))

import { setQuestionCard, switchSlot } from '../../../store/chatSlice'
import { ApiError } from '../../../api/apiError'
import { CrewWorkersTab, reopenCrewSession } from './CrewWorkerList'
import UltraCrewPanel from './UltraCrewPanel'
import { crewModePreviewOn } from '../../../test/crewModePreview'

const W = (key: string, title: string, status: string, group: string) =>
  ({ key, title, agent: 'kirocrew-worker', status, group, last_activity: '', ledger_item: '', created_by: 'root-1' })

const WORKERS = {
  root: 'root-1',
  workers: [
    W('w-done', 'Worker D', 'idle', 'done'),
    { ...W('w-work', 'Worker A', 'working', 'working'), summary: 'Writing a 3-line welcome note' },
    W('w-need', 'Worker B', 'needs_approval', 'approval'),
    W('w-ask', 'Worker F', 'idle', 'with_conductor'),
    W('w-wait', 'Worker C', 'idle', 'waiting'),
    { ...W('w-gone', 'Worker E', 'closed', 'done'), ledger_item: 'it_9' },
  ],
}

const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Plan the launch. Then more.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [], take_over_available: false }

/** The Crew tab opens on Status; most tests here read the List. */
async function toList() {
  fireEvent.click(await screen.findByTestId('ultra-view-list'))
}

function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return store
}

describe('Ultra Mode Crew tab', () => {
  beforeEach(() => {
    api.crewBoard.mockResolvedValue(BOARD)
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('opens on Status: Overview from the ledger goal, Needs your input, Milestones and Recap', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    expect(screen.getByTestId('ultra-view-status')).toHaveAttribute('aria-pressed', 'true')
    expect(await screen.findByText('Plan the launch.')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-needs-you')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-milestones')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-recap')).toBeInTheDocument()
    // Session counts live in List, not Status.
    expect(screen.getByTestId('ultra-status')).not.toHaveTextContent(/sessions ·/)
    expect(screen.queryByText(/Timeline/)).toBeNull()
  })

  it('List: sub-teams sit under their lead, finished members fold away', async () => {
    const D = (key: string, title: string, by: string, group = 'working') => ({ ...W(key, title, 'idle', group), created_by: by, dispatched_by: by })
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('g1', 'Grandchild', 'sub'), D('top', 'Top worker', 'root-1'), D('sub', 'Sub team', 'root-1'), D('old', 'Old one', 'root-1', 'done')] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await screen.findByText('Grandchild')
    const rows = within(screen.getByTestId('ultra-list')).getAllByTestId('ultra-member')
    expect(rows.slice(0, 3).map(r => r.textContent?.split('Working')[0])).toEqual(['Top worker', 'Sub team', 'Grandchild'])
    expect(rows[2]).toHaveAttribute('data-depth', '1')
    const finished = screen.getByTestId('ultra-finished')
    expect(finished).not.toHaveAttribute('open')
    expect(within(finished).getByText('Old one')).toBeInTheDocument()
  })

  it('List: a finished member whose lead is still open is not indented under nothing', async () => {
    const D = (key: string, title: string, by: string, group: string) => ({ ...W(key, title, 'idle', group), created_by: by, dispatched_by: by })
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('sub', 'Sub team', 'root-1', 'working'), D('kid', 'Kid', 'sub', 'done')] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const kid = (await screen.findByText('Kid')).closest('[data-testid="ultra-member"]') as HTMLElement
    expect(kid).toHaveAttribute('data-depth', '0')
  })

  it('List: clicking a member opens it in the main pane, with no drawer step', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    fireEvent.click(await screen.findByRole('button', { name: /Worker A/ }))
    expect(screen.queryByTestId('ultra-member-drawer')).toBeNull()
    expect(panel.openPanelView).toHaveBeenCalledWith('w-work', 'workers', { hold: true })
    expect(store.getState().chat.activeSlot).toBe('w-work')
  })

  it('List: shows the open rows from the slot store when the gateway does not serve the list', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(404, 'not found', ''))
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />, [
      { key: 'w-live', title: 'Live worker', agent: 'kirocrew-worker', messages: 2, running: true, crew_root: 'root-1', created_by: 'root-1' },
    ])
    await toList()
    expect(await screen.findByRole('button', { name: /Live worker/ })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('List: from six members up, a search filters live and finished members and unfolds finished matches', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const box = await screen.findByTestId('crew-search')
    fireEvent.change(box, { target: { value: 'Worker E' } })
    const list = screen.getByTestId('ultra-list')
    await waitFor(() => expect(within(list).getAllByTestId('ultra-member')).toHaveLength(1))
    expect(screen.getByTestId('ultra-finished')).toHaveAttribute('open')
    fireEvent.change(box, { target: { value: 'nobody' } })
    expect(await screen.findByTestId('crew-search-empty')).toBeInTheDocument()
  })

  it('the Crew tab finds the root from a crew member row', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkersTab slot="w-need" active />, [{ key: 'w-need', crew_root: 'root-1', messages: 1 }, { key: 'plain' }])
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledWith('root-1'))
  })

  it('reopenCrewSession says whether the reopened session took the view', async () => {
    // Took the view: the person was still where they asked from.
    api.resumeChatSlot.mockResolvedValueOnce({ ok: true, key: 'w-gone', messages: [] })
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    await expect(reopenCrewSession(store.dispatch, store.getState, 'w-gone', 'Worker E')).resolves.toEqual({ ok: true, activated: true, key: 'w-gone' })
    expect(store.getState().chat.activeSlot).toBe('w-gone')
    // Reopened, but the person picked another chat while it was pending.
    let answer: (v: unknown) => void = () => {}
    api.resumeChatSlot.mockReturnValueOnce(new Promise(resolve => { answer = resolve }))
    const pending = reopenCrewSession(store.dispatch, store.getState, 'w-two', 'Worker F')
    await vi.waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledTimes(2))
    store.dispatch(switchSlot({ key: 'elsewhere' }))
    answer({ ok: true, key: 'w-two', messages: [] })
    await expect(pending).resolves.toEqual({ ok: true, activated: false, key: 'w-two' })
    expect(store.getState().chat.activeSlot).toBe('elsewhere')
    // Could not reopen.
    api.resumeChatSlot.mockRejectedValueOnce(new Error('gone'))
    await expect(reopenCrewSession(store.dispatch, store.getState, 'w-three', 'Worker G')).resolves.toEqual({ ok: false, activated: false, key: 'w-three' })
  })

  it('the Workers tab on a plain chat lists nothing', () => {
    // A plain chat with messages: a brand-new one shows its (empty) panel while
    // its Ultra Mode switch lands.
    renderWith(<CrewWorkersTab slot="plain" active />, [{ key: 'plain', messages: 2 }])
    expect(screen.getByTestId('crew-workers-none')).toBeInTheDocument()
    expect(api.crewWorkers).not.toHaveBeenCalled()
  })

  it('viewing a worker: a Back row returns to the root and keeps the panel open', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<UltraCrewPanel root="root-1" viewing="w-need" active />, [
      { key: 'root-1', title: 'Sample goal', crew_mode_root: true },
      { key: 'w-need', title: 'Worker B', crew_root: 'root-1' },
    ])
    await screen.findByTestId('ultra-crew-panel')
    const back = screen.getByTestId('crew-panel-back')
    expect(back).toHaveTextContent('Back to main chat')
    // One label, no title chip beside it.
    expect(back).not.toHaveTextContent('Sample goal')
    expect(back).toHaveAccessibleName('Back to main chat')
    // A real button, so Tab and Enter reach it.
    expect(back.tagName).toBe('BUTTON')
    fireEvent.click(back)
    expect(store.getState().chat.activeSlot).toBe('root-1')
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(panel.openPanelView).toHaveBeenCalledWith('root-1', 'workers', { hold: true })
  })

  it('viewing the root itself: no Back row', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />, [{ key: 'root-1', title: 'Sample goal', crew_mode_root: true }])
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    expect(screen.queryByTestId('crew-panel-back')).toBeNull()
  })

  it('a worker whose root is closed: the Back row reopens the main chat, never a dead switch', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, root_closed: true, root_title: 'Saved goal' })
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'root-1', messages: [] })
    // No 'root-1' row: the root's tab is closed.
    const store = renderWith(<UltraCrewPanel root="root-1" viewing="w-need" active />, [{ key: 'w-need', title: 'Worker B', crew_root: 'root-1' }])
    await screen.findByTestId('ultra-crew-panel')
    const back = screen.getByTestId('crew-panel-back')
    expect(back).toHaveAttribute('data-root-closed', 'true')
    expect(back).toHaveTextContent('Open main chat')
    // The saved title arrives with the crew list; Back reopens under it.
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    fireEvent.click(back)
    await vi.waitFor(() => expect(store.getState().chat.activeSlot).toBe('root-1'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_root-1', 'Saved goal')
  })

  it('a failed reopen of a closed root says so under the Back row', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, root_closed: true, root_title: 'Saved goal' })
    api.resumeChatSlot.mockResolvedValue({ ok: false })
    renderWith(<UltraCrewPanel root="root-1" viewing="w-need" active />, [{ key: 'w-need', title: 'Worker B', crew_root: 'root-1' }])
    await screen.findByTestId('ultra-crew-panel')
    fireEvent.click(screen.getByTestId('crew-panel-back'))
    expect(await screen.findByText('Could not reopen that session. Try again.')).toBeInTheDocument()
  })

  const ITEM = (over: Record<string, unknown>) => ({
    schema: 1, item_id: 'it_1', title: 'Menu', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
    decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: '2026-10-30T00:00:00Z',
    created_at: '2026-10-30T00:00:00Z', closed_at: null, decided_at: null, orphaned: false, stale: false,
    acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle', events: [], ...over,
  })

  it('Status: a failed board read says so instead of an empty milestone list', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockRejectedValue(new ApiError(500, 'board down', ''))
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByText('board down')).toBeInTheDocument()
    expect(screen.queryByText('No milestones yet.')).toBeNull()
  })

  it('List: a failed crew read says so', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(500, 'boom', ''))
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
  })

  it('Status: the pulse reads the board stale flag on open work', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'a', stale: true })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('ultra-pulse')).toHaveTextContent(/stalled/i)
  })

  it('Status: a blocked milestone opens its crew member, and shows no "moving" pulse', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, workers: [{ ...W('w-sup', 'Supplies', 'idle', 'waiting'), ledger_item: 'b1' }] })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'b1', title: 'Supplies list', status: 'blocked' })] })
    const store = renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const open = await screen.findByTestId('ultra-milestone-open')
    expect(screen.queryByTestId('ultra-pulse')).toBeNull()
    fireEvent.click(open)
    expect(store.getState().chat.activeSlot).toBe('w-sup')
  })

  it('Status: blocked work with no approval says where it is, and its row says what blocks it', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'b2', title: 'Supplies list', status: 'blocked', summary: 'The file save was declined. Retry next.' })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('ultra-milestone')).toHaveTextContent('The file save was declined.')
    expect(screen.getByTestId('ultra-needs-you-none')).toHaveTextContent('Blocked work is under Milestones.')
    expect(screen.getByTestId('ultra-needs-you')).toHaveTextContent('· 0')
  })

  it('Status: an unstarted milestone says Not started, never a made-up order', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'r1', title: 'Menu', alive: 'running' }), ITEM({ item_id: 'n1', title: 'Decor', status: null, last_report_at: null, created_at: '2026-10-30T00:01:00Z' })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const rows = await screen.findAllByTestId('ultra-milestone')
    const next = rows.find(r => r.getAttribute('data-state') === 'next') as HTMLElement
    expect(next).toHaveTextContent('Not started')
    expect(next).not.toHaveTextContent('Starts after')
  })

  it('Status: a finished crew shows no pulse', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'b', state: 'accepted', terminal: true })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await screen.findAllByTestId('ultra-milestone')
    expect(screen.queryByTestId('ultra-pulse')).toBeNull()
  })

  it('Needs your input: the main chat question is one row that points at its card in the chat', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<UltraCrewPanel root="root-1" viewing="w-work" active />)
    act(() => { store.dispatch(switchSlot('w-work')) })
    act(() => { store.dispatch(setQuestionCard({ slot: 'root-1', card_id: 'card-1', questions: [{ question: 'Which tea?', options: [{ label: 'Oolong' }] }] } as never)) })
    const row = await screen.findByTestId('ultra-main-question')
    expect(row).toHaveTextContent('Which tea?')
    expect(screen.queryByText('Oolong')).toBeNull()
    expect(screen.getByTestId('ultra-needs-you')).toHaveTextContent('· 1')
    fireEvent.click(screen.getByTestId('ultra-answer-in-chat'))
    expect(store.getState().chat.activeSlot).toBe('root-1')
  })

  it('Milestones: the open done fold reads "N done" alone; folded it names them', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'd1', title: 'Drinks menu', state: 'accepted', terminal: true })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const fold = await screen.findByTestId('ultra-milestones-done')
    const summary = fold.querySelector('summary') as HTMLElement
    expect(summary).toHaveTextContent('1 done · Drinks menu')
    act(() => { (fold as HTMLDetailsElement).open = true; fireEvent(fold, new Event('toggle')) })
    expect(summary).toHaveTextContent(/^1 done$/)
  })

  it('Status: a failed summary read and a failed crew read say so', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(500, 'boom', ''))
    api.sessionSummary.mockRejectedValue(new ApiError(500, 'summary down', ''))
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByText('summary down')).toBeInTheDocument()
    expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
  })

  it('Status: a worker question waiting on the main chat does not ask the person', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'q', title: 'Menu', status: 'question', outstanding: true })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('ultra-milestone')
    expect(row).toHaveTextContent('Waiting on the main chat')
    expect(screen.getByTestId('ultra-recap')).not.toHaveTextContent(/on you/)
  })
})

// Last, after the file's own hooks that clear localStorage: Ultra Mode is a feature preview.
crewModePreviewOn()

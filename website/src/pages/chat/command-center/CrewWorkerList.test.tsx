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
import UltraCrewPanel, { CREW_BOARD_EXPANDED_KEY } from './UltraCrewPanel'
import { crewModePreviewOn } from '../../../test/crewModePreview'
import { setCrewModeCatalog } from '../../../lib/crewMode'

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

/** A member dispatched by `by` (a sub-conductor's key, or the root). */
const D = (key: string, title: string, by: string, group = 'working') => ({ ...W(key, title, 'idle', group), created_by: by, dispatched_by: by })
const titleOf = (row: HTMLElement) => row.querySelector('[data-testid="ultra-member-title"]')?.textContent
const rowOf = (title: string) => screen.getByText(title).closest('[data-testid="ultra-member"]') as HTMLElement
const findRowOf = async (title: string) => (await screen.findByText(title)).closest('[data-testid="ultra-member"]') as HTMLElement

function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  const view = render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return Object.assign(store, { unmount: view.unmount })
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
    expect(screen.getByTestId('ultra-view-status')).toHaveAttribute('aria-checked', 'true')
    expect(await screen.findByText('Plan the launch.')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-needs-you')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-milestones')).toBeInTheDocument()
    expect(screen.getByTestId('ultra-recap')).toBeInTheDocument()
    // Session counts live in List, not Status.
    expect(screen.getByTestId('ultra-status')).not.toHaveTextContent(/sessions ·/)
    expect(screen.queryByText(/Timeline/)).toBeNull()
  })

  it('List: sub-teams sit under their lead, finished members fold away', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('g1', 'Grandchild', 'sub'), D('top', 'Top worker', 'root-1'), D('sub', 'Sub team', 'root-1'), D('old', 'Old one', 'root-1', 'done')] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await screen.findByText('Grandchild')
    const rows = within(screen.getByTestId('ultra-list')).getAllByTestId('ultra-member')
    expect(rows.slice(0, 3).map(titleOf)).toEqual(['Top worker', 'Sub team', 'Grandchild'])
    expect(rows[2]).toHaveAttribute('data-depth', '1')
    const finished = screen.getByTestId('ultra-finished')
    expect(finished).not.toHaveAttribute('open')
    expect(within(finished).getByText('Old one')).toBeInTheDocument()
  })

  it('List: ONE tree, a finished member stays under its open lead and an open one under its finished lead', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      D('kid', 'Kid', 'sub', 'done'), D('sub', 'Sub team', 'root-1', 'working'),
      D('rel', 'Release notes', 'root-1', 'done'), D('draft', 'Draft notes', 'rel', 'working'),
      D('old', 'Old one', 'root-1', 'done'),
    ] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await screen.findByText('Kid')
    const list = within(screen.getByTestId('ultra-list'))
    const main = list.getAllByTestId('ultra-member').filter(r => !r.closest('[data-testid="ultra-finished"]'))
    expect(main.map(r => `${titleOf(r)}@${r.getAttribute('data-depth')}`)).toEqual(['Sub team@0', 'Kid@1', 'Release notes@0', 'Draft notes@1'])
    // Finished only styles the row.
    expect(main[1]).toHaveAttribute('data-done', 'true')
    expect(main[1].querySelector('[data-testid="ultra-member-title"]')?.className).toContain('text-muted')
    // The Finished fold still holds a tree finished all through.
    const finished = screen.getByTestId('ultra-finished')
    expect(finished).toHaveTextContent('Finished · 1')
    expect(within(finished).getByText('Old one')).toBeInTheDocument()
  })

  it('List: a lead folds and unfolds its crew, and a folded lead shows its counts', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      D('lead', 'Lead', 'root-1', 'waiting'), D('sub', 'Sub team', 'lead', 'working'),
      D('ask', 'Asker', 'sub', 'approval'), D('run', 'Runner', 'sub', 'working'), D('done1', 'Done one', 'sub', 'done'),
    ] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await screen.findByText('Sub team')
    // Top-level open by default, deeper folded: the sub team's crew is out of sight.
    expect(screen.queryByText('Asker')).toBeNull()
    const sub = rowOf('Sub team')
    const chev = within(sub).getByTestId('ultra-member-chevron')
    expect(chev).toHaveAttribute('aria-expanded', 'false')
    expect(within(sub).getByTestId('ultra-member-kids')).toHaveTextContent('3')
    expect(within(sub).getByTestId('ultra-member-needs-you')).toHaveTextContent('1')
    expect(within(sub).getByTestId('ultra-member-running')).toHaveTextContent('1')
    fireEvent.click(chev)
    expect(screen.getByText('Asker')).toBeInTheDocument()
    expect(screen.getByText('Done one')).toBeInTheDocument()
    const opened = rowOf('Sub team')
    expect(within(opened).getByTestId('ultra-member-chevron')).toHaveAttribute('aria-expanded', 'true')
    // Open, the counts make way for the rows themselves; the child count stays.
    expect(within(opened).queryByTestId('ultra-member-needs-you')).toBeNull()
    expect(within(opened).queryByTestId('ultra-member-running')).toBeNull()
    expect(within(opened).getByTestId('ultra-member-kids')).toHaveTextContent('3')
    // The chevron only folds: the session stays where it was.
    expect(panel.openPanelView).not.toHaveBeenCalled()
    // Folding the lead hides its whole tree.
    fireEvent.click(within(rowOf('Lead')).getByTestId('ultra-member-chevron'))
    expect(screen.queryByText('Sub team')).toBeNull()
    expect(within(rowOf('Lead')).getByTestId('ultra-member-running')).toHaveTextContent('2')
  })

  it('List: the fold choices survive a remount under their own key', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('lead', 'Lead', 'root-1', 'working'), D('sub', 'Sub team', 'lead', 'working'), D('deep', 'Deep one', 'sub', 'working')] })
    const first = renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    fireEvent.click(within(await findRowOf('Sub team')).getByTestId('ultra-member-chevron'))
    fireEvent.click(within(rowOf('Lead')).getByTestId('ultra-member-chevron'))
    expect(JSON.parse(localStorage.getItem(CREW_BOARD_EXPANDED_KEY) || '{}')).toEqual({ sub: true, lead: false })
    expect(localStorage.getItem('mc-sidebar-conductor-expanded')).toBeNull()
    first.unmount()
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await findRowOf('Lead')
    expect(screen.queryByText('Sub team')).toBeNull()
    fireEvent.click(within(rowOf('Lead')).getByTestId('ultra-member-chevron'))
    expect(screen.getByText('Deep one')).toBeInTheDocument()
  })

  it('List: a search unfolds every match and the folds come back after', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      D('lead', 'Lead', 'root-1', 'working'), D('sub', 'Sub team', 'lead', 'working'), D('deep', 'Deep one', 'sub', 'working'),
      D('x1', 'Other 1', 'root-1', 'working'), D('x2', 'Other 2', 'root-1', 'working'), D('x3', 'Other 3', 'root-1', 'working'),
    ] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await findRowOf('Lead')
    expect(screen.queryByText('Deep one')).toBeNull()
    const box = screen.getByTestId('crew-search')
    fireEvent.change(box, { target: { value: 'e' } })
    expect(await screen.findByText('Deep one')).toBeInTheDocument()
    // While searching every row is open, so there is nothing to fold.
    expect(screen.queryByTestId('ultra-member-chevron')).toBeNull()
    fireEvent.change(box, { target: { value: '' } })
    await waitFor(() => expect(screen.queryByText('Deep one')).toBeNull())
    expect(within(rowOf('Sub team')).getByTestId('ultra-member-chevron')).toHaveAttribute('aria-expanded', 'false')
  })

  it('List: the member on screen is not folded out of sight, and stays highlighted', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('lead', 'Lead', 'root-1', 'working'), D('sub', 'Sub team', 'lead', 'working'), D('deep', 'Deep one', 'sub', 'working')] })
    renderWith(<UltraCrewPanel root="root-1" viewing="deep" active />)
    await toList()
    const deep = await findRowOf('Deep one')
    expect(within(deep).getByRole('button', { name: /Deep one/ })).toHaveAttribute('aria-current', 'true')
  })

  it('List: past six levels the rows stop stepping right and carry the level', async () => {
    const chain = Array.from({ length: 9 }, (_, i) => D(`n${i}`, `Level ${i}`, i === 0 ? 'root-1' : `n${i - 1}`, 'working'))
    // Every lead opened, so the whole chain is on screen.
    localStorage.setItem(CREW_BOARD_EXPANDED_KEY, JSON.stringify(Object.fromEntries(chain.map(w => [w.key, true]))))
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: chain })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const l6 = await findRowOf('Level 6')
    const l8 = rowOf('Level 8')
    expect(l8).toHaveAttribute('data-depth', '8')
    expect(l8.style.paddingLeft).toBe(l6.style.paddingLeft)
    expect(within(l8).getByTestId('ultra-member-depth')).toHaveTextContent('·8')
    expect(within(l6).queryByTestId('ultra-member-depth')).toBeNull()
  })

  it('List: with no crew under anyone, rows keep their old layout: no chevron and no gutter', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const row = await findRowOf('Worker A')
    expect(screen.queryByTestId('ultra-member-chevron')).toBeNull()
    expect(row.children).toHaveLength(1)
  })

  it('List: clicking a closed member reopens it from history', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'w-gone', messages: [] })
    const store = renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    fireEvent.click(await screen.findByRole('button', { name: /Worker E/ }))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-gone'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_w-gone', 'Worker E')
    // The reopen settles its last update before the test ends.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
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

  it('shows the Ultra panel for a chat with messages that was just switched on, before its root stamp lands', async () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    try {
      api.crewWorkers.mockResolvedValue({ root: 'mid', workers: [] })
      renderWith(<CrewWorkersTab slot="mid" active />, [{ key: 'mid', messages: 3, agent: 'kirocrew-conductor', agent_kind: 'template' }])
      expect(await screen.findByTestId('ultra-crew-panel')).toBeInTheDocument()
      expect(screen.queryByTestId('crew-workers-none')).toBeNull()
    } finally {
      setCrewModeCatalog(undefined, undefined)
    }
  })

  it('names the two views Status and Workers', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />, [{ key: 'root-1', title: 'Sample goal', crew_mode_root: true }])
    expect(await screen.findByTestId('ultra-view-status')).toHaveTextContent(/^Status$/)
    expect(screen.getByTestId('ultra-view-list')).toHaveTextContent(/^Workers$/)
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
    // The visible word is short; its accessible name still says where it goes.
    expect(back).toHaveTextContent(/^Back$/)
    // One label, no title chip beside it.
    expect(back).not.toHaveTextContent('Sample goal')
    // Label in name: the visible word is the accessible name; the long form is the tooltip.
    expect(back).toHaveAccessibleName('Back')
    expect(back).toHaveAttribute('title', 'Back to main chat')
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

  it('Status: the pulse says stuck only when a milestone has no chat left', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'a', alive: 'closed' })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('ultra-pulse')).toHaveTextContent(/stuck/i)
  })

  it('Status: the quiet pulse shows even when nobody has reported yet', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'n1', status: null, last_report_at: null, stale: true })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('ultra-pulse')).toHaveTextContent(/quiet/i)
  })

  it('Status: a stale milestone whose chat is open reads Quiet with its PR, an idle one names its PR or says no reason', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [
      ITEM({ item_id: 'q1', title: 'Supplies list', stale: true, pr: 42, summary: 'Holding for CI.' }),
      ITEM({ item_id: 'w1', title: 'Drinks menu', pr: 7, summary: '' }),
      ITEM({ item_id: 'w2', title: 'Guest list', summary: '' }),
    ] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const rows = await screen.findAllByTestId('ultra-milestone')
    expect(rows.map(r => r.getAttribute('data-state'))).toEqual(['quiet', 'waiting', 'waiting'])
    expect(rows[0]).toHaveTextContent('Quiet · nothing is scheduled to wake it· PR #42Holding for CI.')
    // The worker's words get a line of their own, so a narrow panel never hides them.
    expect(screen.getByTestId('ultra-quiet-said')).toHaveTextContent('Holding for CI.')
    expect(screen.getByTestId('ultra-quiet-said').parentElement?.className).toContain('flex-col')
    // On a narrow panel the reason truncates and the PR number keeps its width.
    expect(screen.getByTestId('ultra-quiet-reason').className).toMatch(/\bmin-w-0\b.*\btruncate\b/)
    expect(screen.getByText('· PR #42').className).toContain('shrink-0')
    expect(rows[1]).toHaveTextContent('Waiting on PR #7')
    expect(rows[2]).toHaveTextContent('Paused, no reason given')
    expect(screen.getByTestId('ultra-pulse')).toHaveTextContent(/quiet/i)
    expect(screen.getByTestId('ultra-recap')).toHaveTextContent('Supplies list has gone quiet')
    expect(screen.getByTestId('ultra-needs-you-none')).toHaveTextContent('Quiet or stuck work is under Milestones.')
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

  it('Status: a worker blocked on the conductor says the main chat is answering, and is not counted as needing you', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'b2', title: 'Supplies list', status: 'blocked', summary: 'The file save was declined. Retry next.' })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('ultra-milestone')
    expect(row.getAttribute('data-state')).toBe('waiting')
    expect(row).toHaveTextContent('Main chat is answering')
    expect(row).not.toHaveTextContent('The file save was declined.')
    expect(screen.getByTestId('ultra-needs-you-none')).toHaveTextContent('Nothing needs you right now.')
    expect(screen.getByTestId('ultra-recap')).not.toHaveTextContent(/wait on you|waits on you/)
  })

  it('Status: a report whose reason names a person needs you, and the Needs your input line points at it', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'h1', title: 'Supplies list', status: 'blocked', reason: 'needs_human', summary: 'Sign in to the shop. Then retry.' })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('ultra-milestone')
    expect(row.getAttribute('data-state')).toBe('needs_you')
    expect(row).toHaveTextContent('Sign in to the shop.')
    // The header counts the row, so the number and the line agree.
    expect(screen.getByTestId('ultra-needs-you')).toHaveTextContent('· 1')
    expect(screen.getByTestId('ultra-needs-you-none')).toHaveTextContent('Supplies list needs you. See Milestones.')
    expect(screen.getByTestId('ultra-needs-you-none')).not.toHaveTextContent('No approval')
    expect(screen.getByTestId('ultra-recap')).toHaveTextContent('Supplies list waits on you')
  })

  it('Status: a worker whose session closed on an open report reads Stuck, not needs you', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 's1', title: 'Supplies list', status: 'progress', alive: 'closed', summary: 'Half the list. More soon.' })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('ultra-milestone')
    expect(row.getAttribute('data-state')).toBe('stuck')
    expect(row).toHaveTextContent('Stuck · its chat closed, ask the main chat to restart it')
    expect(screen.getByTestId('ultra-needs-you-none')).toHaveTextContent('Quiet or stuck work is under Milestones.')
    expect(screen.getByTestId('ultra-recap')).toHaveTextContent('Supplies list is stuck')
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

  it('Status: an idle worker whose last report was progress reads Waiting, with no spinner or "moving" pulse', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'p1', title: 'Menu', summary: 'Drafted the menu. More soon.' })] })
    renderWith(<UltraCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('ultra-milestone')
    expect(row.getAttribute('data-state')).toBe('waiting')
    expect(row).toHaveTextContent('Waiting')
    expect(row).toHaveTextContent('Drafted the menu.')
    expect(row).not.toHaveTextContent('now')
    expect(screen.queryByTestId('ultra-pulse')).toBeNull()
    expect(screen.getByTestId('ultra-recap')).not.toHaveTextContent(/running/)
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
    expect(row).toHaveTextContent('Main chat is answering')
    expect(screen.getByTestId('ultra-recap')).not.toHaveTextContent(/on you/)
  })
})

// Last, after the file's own hooks that clear localStorage: Ultra Mode is a feature preview.
crewModePreviewOn()

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
const panel = vi.hoisted(() => ({ openPanelView: vi.fn() }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: panel.openPanelView }))

import { switchSlot } from '../../../store/chatSlice'
import { ApiError } from '../../../api/apiError'
import CrewWorkerList, { CREW_FINISHED_OPEN_KEY, CREW_WORKER_SEARCH_MIN, CrewWorkersTab, crewWorkerQueryKey, reopenCrewSession } from './CrewWorkerList'
import { searchCrewWorkers } from '../../../lib/crewWorkers'

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

/** Opens the folded Done group (closed by default) once the list has loaded. */
async function openFinished() {
  fireEvent.click(await screen.findByTestId('crew-finished-toggle'))
}

function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return store
}

describe('CrewWorkerList (design B)', () => {
  afterEach(() => { vi.clearAllMocks(); localStorage.clear() })

  it('C: a sub-conductor\'s crew sits one step in under it with a "from" note, at any depth', async () => {
    const D = (key: string, title: string, by: string) => ({ ...W(key, title, 'idle', 'done'), created_by: by, dispatched_by: by })
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('g1', 'Grandchild', 'sub'), D('top', 'Top worker', 'root-1'), D('g2', 'Great-grandchild', 'g1'), D('sub', 'Sub conductor', 'root-1')] })
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await openFinished()
    await screen.findByText('Grandchild')
    const rows = screen.getAllByTestId('crew-worker-row')
    // The sub-conductor's crew right under it; deeper crew under its own dispatcher.
    expect(rows.map(r => r.getAttribute('data-worker'))).toEqual(['top', 'sub', 'g1', 'g2'])
    const byKey = Object.fromEntries(rows.map(r => [r.getAttribute('data-worker'), r]))
    // One indent step only, and the note names the direct dispatcher.
    expect(byKey.top).not.toHaveAttribute('data-indent')
    expect(byKey.sub).not.toHaveAttribute('data-indent')
    expect(byKey.g1).toHaveAttribute('data-indent', '1')
    expect(byKey.g2).toHaveAttribute('data-indent', '1')
    expect(byKey.g1.className).toBe(byKey.g2.className)
    expect(within(byKey.g1).getByTestId('crew-worker-from')).toHaveTextContent('From Sub conductor')
    expect(within(byKey.g2).getByTestId('crew-worker-from')).toHaveTextContent('From Grandchild')
    // The note is in the button's name, so the indent is not the only signal.
    expect(within(byKey.g1).getByRole('button')).toHaveAccessibleName(/From Sub conductor/)
    expect(within(byKey.top).queryByTestId('crew-worker-from')).toBeNull()
  })

  it('groups cards Approval needed, Working, Waiting on conductor, Waiting, Done; never Needs you', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    const groups = screen.getAllByTestId('crew-worker-group')
    expect(groups.map(g => g.getAttribute('data-group'))).toEqual(['approval', 'working', 'with_conductor', 'waiting', 'done'])
    expect(within(groups[0]).getByRole('heading')).toHaveTextContent('Approval needed · 1')
    // A worker's question waits on its conductor, never on the person (owner decision A).
    expect(within(groups[2]).getByRole('heading')).toHaveTextContent('Waiting on conductor · 1')
    expect(screen.queryByText(/Needs you/)).toBeNull()
    expect(within(groups[4]).getByRole('heading')).toHaveTextContent('Done · 2')
    expect(screen.getByTestId('crew-projects-soon')).toHaveTextContent('Projects tab · coming soon')
  })

  it('folds finished workers under one Done toggle, closed by default, with the right count', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    const toggle = screen.getByTestId('crew-finished-toggle')
    expect(toggle.tagName).toBe('BUTTON')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveTextContent('Done · 2')
    // Done is the last group: the live ones stay on top, always shown.
    const groups = screen.getAllByTestId('crew-worker-group')
    expect(groups[groups.length - 1]).toHaveAttribute('data-group', 'done')
    expect(screen.queryByText('Worker D')).toBeNull()
    expect(screen.queryByText('Worker E')).toBeNull()
    for (const live of ['Worker A', 'Worker B', 'Worker C', 'Worker F']) expect(screen.getByText(live)).toBeInTheDocument()
    expect(screen.getAllByTestId('crew-worker-row')).toHaveLength(4)
  })

  it('one click opens and closes the Done group, and the choice is kept', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    const toggle = await screen.findByTestId('crew-finished-toggle')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Worker D')).toBeInTheDocument()
    expect(screen.getByText('Worker E')).toBeInTheDocument()
    expect(localStorage.getItem(CREW_FINISHED_OPEN_KEY)).toBe('1')
    // A fresh mount (reload, another chat) keeps it open.
    cleanup()
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('crew-finished-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByText('Worker D')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('crew-finished-toggle'))
    expect(screen.getByTestId('crew-finished-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Worker D')).toBeNull()
    expect(localStorage.getItem(CREW_FINISHED_OPEN_KEY)).toBe('0')
  })

  it('a folded Done group still shows the finished worker on screen, marked current', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="w-done" active />)
    const card = await screen.findByRole('button', { name: /Worker D/ })
    expect(card).toHaveAttribute('aria-current', 'true')
    expect(screen.getByTestId('crew-finished-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('crew-finished-toggle')).toHaveTextContent('Done · 2')
    expect(screen.queryByText('Worker E')).toBeNull()
  })

  it('a finished worker in the open Done group still opens like today', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await openFinished()
    fireEvent.click(screen.getByRole('button', { name: /Worker D/ }))
    expect(panel.openPanelView).toHaveBeenCalledWith('w-done', 'workers')
    expect(store.getState().chat.activeSlot).toBe('w-done')
  })

  it('a worker that finishes leaves the live list and joins the folded count', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    render(<QueryClientProvider client={qc}><Provider store={store}><CrewWorkerList root="root-1" viewing="root-1" active /></Provider></QueryClientProvider>)
    await screen.findByText('Worker A')
    expect(screen.getByTestId('crew-finished-toggle')).toHaveTextContent('Done · 2')
    const next = { ...WORKERS, workers: WORKERS.workers.map(w => (w.key === 'w-work' ? { ...w, status: 'idle', group: 'done' } : w)) }
    api.crewWorkers.mockResolvedValue(next)
    await qc.refetchQueries({ queryKey: ['crew-workers', 'root-1'] })
    await waitFor(() => expect(screen.getByTestId('crew-finished-toggle')).toHaveTextContent('Done · 3'))
    expect(screen.queryByText('Worker A')).toBeNull()
    expect(document.querySelector('[data-group="working"]')).toBeNull()
  })

  it('opens a worker in the main pane with the Workers tab in front, and marks the one on screen', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<CrewWorkerList root="root-1" viewing="w-need" active />)
    const current = await screen.findByRole('button', { name: /Worker B/ })
    expect(current).toHaveAttribute('aria-current', 'true')
    fireEvent.click(screen.getByRole('button', { name: /Worker A/ }))
    expect(panel.openPanelView).toHaveBeenCalledWith('w-work', 'workers')
    // A plain switch, like a tab click: the main pane now shows that worker.
    expect(store.getState().chat.activeSlot).toBe('w-work')
    // On desktop the Workers panel stays beside it (U2): the target's own,
    // usually closed, panel state is not restored over it.
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('a card says the task in one line under its title', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    const card = (await screen.findByText('Worker A')).closest('[data-testid="crew-worker-row"]') as HTMLElement
    expect(within(card).getByTestId('crew-worker-summary')).toHaveTextContent('Writing a 3-line welcome note')
    const plain = screen.getByText('Worker C').closest('[data-testid="crew-worker-row"]') as HTMLElement
    expect(within(plain).queryByTestId('crew-worker-summary')).toBeNull()
  })

  it('reopens a worker whose tab has closed from its saved transcript (X4)', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'w-gone', messages: [] })
    const store = renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await openFinished()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen Worker E to read it' }))
    await vi.waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-gone'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_w-gone', 'Worker E')
  })

  it('a reopen that answers after the person opened another chat does not take them away', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    let answer: (v: unknown) => void = () => {}
    api.resumeChatSlot.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const store = renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await openFinished()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen Worker E to read it' }))
    await vi.waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalled())
    // The person moves on while the reopen is still in flight.
    store.dispatch(switchSlot({ key: 'elsewhere' }))
    expect(store.getState().chat.activeSlot).toBe('elsewhere')
    answer({ ok: true, key: 'w-gone', messages: [] })
    // The thunk settles: its pending entry is gone.
    await vi.waitFor(() => expect(Object.keys(store.getState().chat.pendingResumes ?? {})).toHaveLength(0))
    // Reopened as a tab, but the late answer leaves the view where the person went.
    expect(store.getState().chat.activeSlot).toBe('elsewhere')
    expect(panel.openPanelView).not.toHaveBeenCalledWith('w-gone', 'workers')
    expect(screen.queryByText('Could not reopen that session. Try again.')).toBeNull()
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

  it('says so when a closed worker cannot be reopened', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.resumeChatSlot.mockRejectedValue(new Error('gone'))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await openFinished()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen Worker E to read it' }))
    expect(await screen.findByText('Could not reopen that session. Try again.')).toBeInTheDocument()
  })

  it('shows a loading line while the first read is in flight (X6)', () => {
    api.crewWorkers.mockReturnValue(new Promise(() => {}))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(screen.getByTestId('crew-workers-loading')).toHaveTextContent('Loading the crew…')
  })

  it('shows no raw ledger id on a card', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await openFinished()
    const card = (await screen.findByText('Worker E')).closest('[data-testid="crew-worker-row"]') as HTMLElement
    expect(card).not.toHaveTextContent('it_9')
  })

  it('lists a root\'s open workers from the slot store when the gateway does not serve the list, says nothing is wrong, and stops asking', async () => {
    // The worker list route lands in a later part. A 404 must not leave a crew
    // member unreachable and is not a failure: the rows the store already stamps
    // `crew_root` are the list, no notice hands the person to the agent, and the
    // route is not polled again.
    api.crewWorkers.mockRejectedValue(new ApiError(404, 'not found', ''))
    const slots = [
      { key: 'root-1', title: 'Root', messages: 1, running: false, crew_mode_root: true },
      { key: 'w-live', title: 'Live worker', agent: 'kirocrew-worker', messages: 2, running: true, crew_root: 'root-1', created_by: 'root-1' },
      { key: 'w-wait', title: 'Waiting worker', messages: 1, running: false, crew_root: 'root-1', pending_approval: true, created_by: 'root-1' },
    ]
    const store = renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />, slots)
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByTestId('crew-workers-loading')).toBeNull())
    expect(screen.queryByRole('alert')).toBeNull()
    const groups = screen.getAllByTestId('crew-worker-group')
    expect(groups.map(g => g.getAttribute('data-group'))).toEqual(['approval', 'working'])
    expect(screen.queryByTestId('crew-workers-loading')).toBeNull()
    expect(screen.queryByText('No crew yet.')).toBeNull()
    // The card opens the live worker like any other.
    fireEvent.click(screen.getByRole('button', { name: /Live worker/ }))
    expect(store.getState().chat.activeSlot).toBe('w-live')
    // The one read is the only one: `refetchInterval` is off once the route
    // answered 404, so nothing is scheduled after the read above.
    expect(api.crewWorkers).toHaveBeenCalledTimes(1)
  })

  it('says there is no crew yet, not that a read failed, when the route is not served and no row is a worker', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(404, 'not found', ''))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(await screen.findByText('No crew yet.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('still says so when the list read fails for a real reason', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(500, 'down', ''))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />, [
      { key: 'w-live', title: 'Live worker', messages: 2, running: true, crew_root: 'root-1', created_by: 'root-1' },
    ])
    expect(await screen.findByText('Could not load the crew list; its open crew members are shown.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Live worker/ })).toBeInTheDocument()
  })

  it('shows the live rows under no loading line while the first read is in flight', () => {
    api.crewWorkers.mockReturnValue(new Promise(() => {}))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />, [
      { key: 'w-live', title: 'Live worker', messages: 2, running: true, crew_root: 'root-1', created_by: 'root-1' },
    ])
    expect(screen.getByRole('button', { name: /Live worker/ })).toBeInTheDocument()
    expect(screen.queryByTestId('crew-workers-loading')).toBeNull()
  })

  it('says when the list cannot be read', async () => {
    api.crewWorkers.mockRejectedValue(new Error('nope'))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
  })

  it('the Workers tab finds the root from a worker row and says so on a plain chat', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkersTab slot="w-need" active />, [{ key: 'w-need', crew_root: 'root-1' }, { key: 'plain' }])
    await screen.findByText('Worker B')
    expect(api.crewWorkers).toHaveBeenCalledWith('root-1')
  })

  it('the Workers tab on a plain chat lists nothing', () => {
    renderWith(<CrewWorkersTab slot="plain" active />, [{ key: 'plain' }])
    expect(screen.getByTestId('crew-workers-none')).toBeInTheDocument()
    expect(api.crewWorkers).not.toHaveBeenCalled()
  })

  it('viewing a worker: a Back row returns to the root and keeps the panel open', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<CrewWorkerList root="root-1" viewing="w-need" active />, [
      { key: 'root-1', title: 'Sample goal', crew_mode_root: true },
      { key: 'w-need', title: 'Worker B', crew_root: 'root-1' },
    ])
    await screen.findByText('Worker A')
    const back = screen.getByTestId('crew-panel-back')
    expect(back).toHaveTextContent('Back to main chat')
    expect(back).toHaveTextContent('Sample goal')
    expect(back).toHaveAccessibleName('Back to main chat · Sample goal')
    // A real button, so Tab and Enter reach it.
    expect(back.tagName).toBe('BUTTON')
    fireEvent.click(back)
    expect(store.getState().chat.activeSlot).toBe('root-1')
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(panel.openPanelView).toHaveBeenCalledWith('root-1', 'workers')
  })

  it('viewing the root itself: no Back row', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />, [{ key: 'root-1', title: 'Sample goal', crew_mode_root: true }])
    await screen.findByText('Worker A')
    expect(screen.queryByTestId('crew-panel-back')).toBeNull()
  })

  it('a worker whose root is closed: the Back row reopens the main chat, never a dead switch', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, root_closed: true, root_title: 'Saved goal' })
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'root-1', messages: [] })
    // No 'root-1' row: the root's tab is closed.
    const store = renderWith(<CrewWorkerList root="root-1" viewing="w-need" active />, [{ key: 'w-need', title: 'Worker B', crew_root: 'root-1' }])
    await screen.findByText('Worker A')
    const back = screen.getByTestId('crew-panel-back')
    expect(back).toHaveAttribute('data-root-closed', 'true')
    expect(back).toHaveTextContent('Open main chat')
    await waitFor(() => expect(back).toHaveTextContent('Saved goal'))
    fireEvent.click(back)
    await vi.waitFor(() => expect(store.getState().chat.activeSlot).toBe('root-1'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_root-1', 'Saved goal')
  })

  it('a failed reopen of a closed root says so under the Back row', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, root_closed: true, root_title: 'Saved goal' })
    api.resumeChatSlot.mockResolvedValue({ ok: false })
    renderWith(<CrewWorkerList root="root-1" viewing="w-need" active />, [{ key: 'w-need', title: 'Worker B', crew_root: 'root-1' }])
    await screen.findByText('Worker A')
    fireEvent.click(screen.getByTestId('crew-panel-back'))
    expect(await screen.findByText('Could not reopen that session. Try again.')).toBeInTheDocument()
  })
})

describe('CrewWorkerList search', () => {
  afterEach(() => { vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear() })

  const SEARCH = { ...WORKERS, workers: [...WORKERS.workers, { ...W('w-zh', '修复登录页面', 'idle', 'done'), summary: '已合并' }] }
  const shown = () => screen.queryAllByTestId('crew-worker-row').map(r => r.getAttribute('data-worker'))
  const type = (text: string) => fireEvent.change(screen.getByRole('textbox', { name: 'Search the crew' }), { target: { value: text } })

  it('filters cards by title as the person types, case-insensitive', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    type('worker a')
    expect(shown()).toEqual(['w-work'])
    expect(screen.getAllByTestId('crew-worker-group').map(g => g.getAttribute('data-group'))).toEqual(['working'])
  })

  it('matches the task line too', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    type('WELCOME note')
    expect(shown()).toEqual(['w-work'])
  })

  it('matches CJK titles, finished ones included', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    type('登录')
    expect(shown()).toEqual(['w-zh'])
    type('已合并')
    expect(shown()).toEqual(['w-zh'])
  })

  it('shows finished matches while the Done group is folded, under a plain heading', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('crew-finished-toggle')).toHaveAttribute('aria-expanded', 'false')
    type('Worker')
    expect(shown()).toEqual(expect.arrayContaining(['w-done', 'w-gone', 'w-work']))
    expect(screen.queryByTestId('crew-finished-toggle')).toBeNull()
    const done = document.querySelector('[data-group="done"]') as HTMLElement
    expect(within(done).getByRole('heading')).toHaveTextContent('Done · 2')
    // The search leaves the person's fold choice alone.
    expect(localStorage.getItem(CREW_FINISHED_OPEN_KEY)).toBe('0')
  })

  it('clearing the search folds Done back as it was', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByTestId('crew-finished-toggle')
    type('Worker D')
    expect(shown()).toEqual(['w-done'])
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(screen.getByRole('textbox', { name: 'Search the crew' })).toHaveValue('')
    expect(screen.getByTestId('crew-finished-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Worker D')).toBeNull()
    // An open Done stays open after a search too.
    fireEvent.click(screen.getByTestId('crew-finished-toggle'))
    type('Worker A')
    type('')
    expect(screen.getByTestId('crew-finished-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Worker D')).toBeInTheDocument()
  })

  it('says so when nothing matches', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    type('  no such task ')
    expect(shown()).toEqual([])
    expect(screen.getByTestId('crew-search-empty')).toHaveTextContent('No crew member matches “no such task”.')
    expect(screen.queryByText('No crew yet.')).toBeNull()
  })

  it('Esc clears the search and restores the list', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    type('Worker C')
    expect(shown()).toEqual(['w-wait'])
    const box = screen.getByRole('textbox', { name: 'Search the crew' })
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(box).toHaveValue('')
    expect(shown()).toHaveLength(4)
    expect(screen.getByTestId('crew-finished-toggle')).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps the query when opening a match remounts the list', async () => {
    api.crewWorkers.mockResolvedValue(SEARCH)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    type('Worker D')
    fireEvent.click(screen.getByRole('button', { name: /Worker D/ }))
    cleanup()
    renderWith(<CrewWorkerList root="root-1" viewing="w-done" active />)
    expect(await screen.findByRole('textbox', { name: 'Search the crew' })).toHaveValue('Worker D')
    await waitFor(() => expect(shown()).toEqual(['w-done']))
    // Clearing forgets it.
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Search the crew' }), { key: 'Escape' })
    expect(sessionStorage.getItem(crewWorkerQueryKey('root-1'))).toBe('')
  })

  it('says nothing matches only once the list is read, not while it is pending or failed', async () => {
    sessionStorage.setItem(crewWorkerQueryKey('root-1'), 'Worker D')
    const live = { key: 'w-live', title: 'Live one', crew_root: 'root-1', running: true }
    const slots = Array.from({ length: CREW_WORKER_SEARCH_MIN }, (_, i) => ({ ...live, key: `w-live-${i}` }))
    api.crewWorkers.mockReturnValue(new Promise(() => {}))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />, slots)
    expect(screen.getByRole('textbox', { name: 'Search the crew' })).toHaveValue('Worker D')
    expect(screen.queryByTestId('crew-search-empty')).toBeNull()
    cleanup()
    api.crewWorkers.mockRejectedValue(new Error('boom'))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />, slots)
    expect(await screen.findByText(/Could not load the crew list/)).toBeInTheDocument()
    expect(screen.queryByTestId('crew-search-empty')).toBeNull()
  })

  it('has no search box under the minimum crew size', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, workers: WORKERS.workers.slice(0, CREW_WORKER_SEARCH_MIN - 1) })
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    expect(screen.queryByTestId('crew-search')).toBeNull()
  })

  it('searchCrewWorkers folds case and full-width forms; a blank query matches all', () => {
    const rows = SEARCH.workers as never as Parameters<typeof searchCrewWorkers>[0]
    expect(searchCrewWorkers(rows, 'ＷＯＲＫＥＲ Ｅ').map(w => w.key)).toEqual(['w-gone'])
    expect(searchCrewWorkers(rows, '   ')).toBe(rows)
  })
})

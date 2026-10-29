import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
const panel = vi.hoisted(() => ({ openPanelView: vi.fn() }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: panel.openPanelView }))

import { switchSlot } from '../../../store/chatSlice'
import { ApiError } from '../../../api/apiError'
import CrewWorkerList, { CrewWorkersTab, reopenCrewSession } from './CrewWorkerList'
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

function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return store
}

describe('CrewWorkerList (design B)', () => {
  afterEach(() => vi.clearAllMocks())

  it('C: a sub-conductor\'s crew sits one step in under it with a "from" note, at any depth', async () => {
    const D = (key: string, title: string, by: string) => ({ ...W(key, title, 'idle', 'done'), created_by: by, dispatched_by: by })
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('g1', 'Grandchild', 'sub'), D('top', 'Top worker', 'root-1'), D('g2', 'Great-grandchild', 'g1'), D('sub', 'Sub conductor', 'root-1')] })
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
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
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen Worker E to read it' }))
    await vi.waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-gone'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_w-gone', 'Worker E')
  })

  it('a reopen that answers after the person opened another chat does not take them away', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    let answer: (v: unknown) => void = () => {}
    api.resumeChatSlot.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const store = renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
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
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()

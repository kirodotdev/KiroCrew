import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'
import { updateSlot } from '../../store/dashboardSlice'
import { ApiError } from '../../api/apiError'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))
const panel = vi.hoisted(() => ({ openPanelView: vi.fn() }))
vi.mock('../../hooks/usePanelTabs', () => ({ openPanelView: panel.openPanelView }))

import { CrewStatusStrip } from './CrewStatusStrip'

const W = (key: string, status: string, group: string) =>
  ({ key, title: key, agent: 'kirocrew-worker', status, group, last_activity: '', ledger_item: 'it_' + key, created_by: 'root-1' })

function renderStrip(slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><CrewStatusStrip root="root-1" /></Provider></QueryClientProvider>)
  return store
}

const chips = () => screen.getAllByTestId('crew-status-chip').map(c => [c.getAttribute('data-group'), within(c).getByTestId('crew-status-count').textContent])

describe('CrewStatusStrip', () => {
  afterEach(() => vi.clearAllMocks())

  it('counts the workers by the state the worker list gives, approval first', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      W('a', 'working', 'working'), W('b', 'idle', 'done'), W('c', 'needs_approval', 'approval'),
      W('d', 'working', 'working'), W('e', 'asking', 'with_conductor'), W('f', 'closed', 'done'), W('g', 'idle', 'waiting'),
    ] })
    renderStrip()
    await screen.findByTestId('crew-status-strip')
    expect(chips()).toEqual([['approval', '1'], ['working', '2'], ['with_conductor', '1'], ['waiting', '1'], ['done', '2']])
    expect(screen.getByTestId('crew-status-strip')).toHaveTextContent('Approval needed1')
  })

  it('puts the approval count first in the warning tone, and says on screen what a press opens', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [W('b', 'working', 'working'), W('a', 'needs_approval', 'approval')] })
    renderStrip()
    await screen.findByTestId('crew-status-strip')
    const [approval, working] = screen.getAllByTestId('crew-status-chip')
    expect(approval).toHaveAttribute('data-group', 'approval')
    expect(approval.className).toContain('crew-tone-warn')
    expect(working.className).not.toContain('crew-tone-warn')
    // The strip only counts: the approval is decided on its own card.
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
    expect(screen.getByTestId('crew-status-open')).toHaveTextContent('Crew')
  })

  it('follows a worker that changes state, from the live slot rows', async () => {
    // The route is not served, so only the store's live rows speak.
    api.crewWorkers.mockRejectedValue(new ApiError(404, 'not found', ''))
    const store = renderStrip([{ key: 'w1', crew_root: 'root-1', running: true, title: 'One' }])
    await screen.findByTestId('crew-status-strip')
    expect(chips()).toEqual([['working', '1']])
    act(() => { store.dispatch(updateSlot({ key: 'w1', running: false, pending_approval: true })) })
    await waitFor(() => expect(chips()).toEqual([['approval', '1']]))
  })

  it('draws nothing while the conductor has no workers', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    renderStrip()
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledWith('root-1'))
    await act(async () => {})
    expect(screen.queryByTestId('crew-status-strip')).toBeNull()
  })

  it('opens the side panel on the Workers tab, by click or keyboard', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [W('a', 'working', 'working')] })
    const store = renderStrip()
    const strip = await screen.findByRole('button', { name: /Crew status\. Open the crew list/ })
    expect(store.getState().chat.activityOpen).toBeFalsy()
    const user = userEvent.setup()
    await user.click(strip)
    expect(panel.openPanelView).toHaveBeenCalledWith('root-1', 'workers')
    expect(store.getState().chat.activityOpen).toBe(true)
    panel.openPanelView.mockClear()
    strip.focus()
    await user.keyboard('{Enter}')
    expect(panel.openPanelView).toHaveBeenCalledWith('root-1', 'workers')
  })

  it('runs beforeOpen first, so a find pane that hides the panel is closed', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [W('a', 'working', 'working')] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    const order: string[] = []
    panel.openPanelView.mockImplementation(() => { order.push('panel') })
    render(<QueryClientProvider client={qc}><Provider store={store}><CrewStatusStrip root="root-1" beforeOpen={() => order.push('close-search')} /></Provider></QueryClientProvider>)
    await userEvent.setup().click(await screen.findByTestId('crew-status-strip'))
    expect(order).toEqual(['close-search', 'panel'])
    panel.openPanelView.mockReset()
  })

  it('says a failed re-read under the counts, and alone when nothing was ever read', async () => {
    api.crewWorkers.mockResolvedValueOnce({ root: 'root-1', workers: [W('a', 'working', 'working')] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    render(<QueryClientProvider client={qc}><Provider store={store}><CrewStatusStrip root="root-1" /></Provider></QueryClientProvider>)
    await screen.findByTestId('crew-status-strip')
    api.crewWorkers.mockRejectedValueOnce(new ApiError(500, 'boom', ''))
    await act(async () => { await qc.refetchQueries({ queryKey: ['crew-workers', 'root-1'] }) })
    expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
    expect(chips()).toEqual([['working', '1']])
  })

  it('shows only the failure notice when the first read fails', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(500, 'boom', ''))
    renderStrip()
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-status-strip')).toBeNull()
  })

  it('names each count for a screen reader and turns transitions off under reduced motion', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [W('a', 'working', 'working')] })
    renderStrip()
    const strip = await screen.findByRole('button', { name: /Crew status\. Open the crew list.*Working.*1/ })
    expect(strip.className).toContain('motion-reduce:transition-none')
  })
})

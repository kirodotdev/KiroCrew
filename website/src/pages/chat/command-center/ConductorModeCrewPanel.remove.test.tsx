/**
 * Conductor mode Board > Status > Milestones: a milestone that was not done
 * (rejected or abandoned) can be hidden from the list. The choice is kept per
 * browser and only hides the row: the Milestones count, the Recap and the
 * Overview still count every milestone, the work ledger is never written, and one
 * click shows the hidden rows again. Focus follows the control that went away, a
 * row another tab hid is kept, and with no usable storage each Hide still adds up.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel, { CONDUCTOR_MILESTONES_REMOVED_KEY, milestoneRemovable, readRemovedMilestones } from './ConductorModeCrewPanel'
import type { WorkBoardItem } from '../../../api/crewBoard'

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_a', title: 'Ship the fix', acceptance: { kind: 'human_approval' }, state: 'accepted', verdict: 'pass',
  decision: '', round: 1, fails: 0, status: 'done', summary: 'Merged.', artifacts: {}, pr: null,
  last_report_at: '2026-10-30T00:05:00Z', created_at: '2026-10-30T00:00:00Z', closed_at: '2026-10-30T00:10:00Z', decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: true, alive: 'closed', events: [],
  reason: null, awaits_person: false, ...over,
} as WorkBoardItem)

const DONE = ITEM({})
const DROPPED = ITEM({ item_id: 'it_c', title: 'Old idea', state: 'abandoned', verdict: null, created_at: '2026-10-30T00:01:00Z' })
const REJECTED = ITEM({ item_id: 'it_b', title: 'Port the gate', state: 'rejected', verdict: 'fail', created_at: '2026-10-30T00:02:00Z' })
const OPEN = ITEM({ item_id: 'it_d', title: 'Write docs', state: 'open', status: 'progress', terminal: false, alive: 'running', closed_at: null, verdict: null, created_at: '2026-10-30T00:03:00Z' })
const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Fix the login bug.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [DONE, DROPPED, REJECTED, OPEN], take_over_available: false }

function renderPanel(root = 'root-1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: [] } } as never)
  return render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root={root} viewing={root} active /></Provider></QueryClientProvider>)
}

const rowTitles = () => screen.getAllByTestId('conductor-mode-milestone').map(r => r.querySelector('.font-medium')?.textContent)

describe('Conductor mode: hide a milestone that was not done', () => {
  beforeEach(() => {
    Object.values(api).forEach(mock => mock.mockReset())
    localStorage.clear()
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
    api.crewBoard.mockResolvedValue(BOARD)
  })
  afterEach(() => { vi.clearAllMocks() })

  it('offers Hide only on rows closed without being accepted', async () => {
    renderPanel()
    await screen.findByText('Old idea')
    const withRemove = screen.getAllByTestId('conductor-mode-milestone').filter(r => within(r).queryByTestId('conductor-mode-milestone-remove'))
    expect(withRemove.map(r => r.getAttribute('data-state'))).toEqual(['not_done', 'not_done'])
    const hide = within(withRemove[0]).getByTestId('conductor-mode-milestone-remove')
    // A visible word that says it only hides, not a bare trash icon read as a delete.
    expect(hide).toHaveTextContent('Hide')
    expect(hide).toHaveAccessibleName('Hide "Old idea" from this list')
    expect([DONE, DROPPED, REJECTED, OPEN].map(milestoneRemovable)).toEqual([false, true, true, false])
  })

  it('hides only the row: the count and the Recap keep it, nothing is written to the ledger, and it survives a reload', async () => {
    const first = renderPanel()
    await screen.findByText('Old idea')
    expect(screen.getByTestId('conductor-mode-milestones')).toHaveTextContent('Milestones · 1 of 4')
    const recap = screen.getByTestId('conductor-mode-recap').textContent
    expect(recap).toContain('1 of 4 done')
    fireEvent.click(within(screen.getByText('Old idea').closest('li')!).getByTestId('conductor-mode-milestone-remove'))
    await waitFor(() => expect(screen.queryByText('Old idea')).toBeNull())
    // Hiding a row makes the run read no better than it went.
    expect(screen.getByTestId('conductor-mode-milestones')).toHaveTextContent('Milestones · 1 of 4')
    expect(screen.getByTestId('conductor-mode-milestones-removed')).toHaveTextContent('Hidden: 1')
    expect(screen.getByTestId('conductor-mode-recap').textContent).toBe(recap)
    expect(api.crewBoardAction).not.toHaveBeenCalled()
    expect(readRemovedMilestones()).toEqual({ 'root-1': ['it_c'] })
    // A fresh mount (a reload) reads the stored choice.
    first.unmount()
    renderPanel()
    await screen.findByText('Port the gate')
    expect(rowTitles()).not.toContain('Old idea')
  })

  it('shows the hidden rows again in one click', async () => {
    localStorage.setItem(CONDUCTOR_MILESTONES_REMOVED_KEY, JSON.stringify({ 'root-1': ['it_c', 'it_b'] }))
    renderPanel()
    await screen.findByText('Write docs')
    // The accepted row stays (inside the folded Done group); both not-done rows are gone.
    expect(rowTitles()).toEqual(['Ship the fix', 'Write docs'])
    expect(screen.getByTestId('conductor-mode-milestones-removed')).toHaveTextContent('Hidden: 2')
    fireEvent.click(screen.getByTestId('conductor-mode-milestones-restore'))
    await screen.findByText('Old idea')
    expect(screen.queryByTestId('conductor-mode-milestones-removed')).toBeNull()
    expect(readRemovedMilestones()).toEqual({})
  })

  it('never hides an open or accepted milestone, and keeps each main chat separate', async () => {
    // A stored id that names an accepted or open item, or another root, hides nothing here.
    localStorage.setItem(CONDUCTOR_MILESTONES_REMOVED_KEY, JSON.stringify({ 'root-1': ['it_a', 'it_d'], 'root-2': ['it_c'] }))
    renderPanel()
    await screen.findByText('Old idea')
    expect(rowTitles()).toEqual(['Ship the fix', 'Old idea', 'Port the gate', 'Write docs'])
    expect(screen.queryByTestId('conductor-mode-milestones-removed')).toBeNull()
  })

  it('moves focus to Show again after a Hide, and to the heading after Show again', async () => {
    renderPanel()
    await screen.findByText('Old idea')
    const remove = within(screen.getByText('Old idea').closest('li')!).getByTestId('conductor-mode-milestone-remove')
    remove.focus()
    fireEvent.click(remove)
    // Never the next row's Hide: a second Enter must not hide another milestone.
    await waitFor(() => expect(screen.getByTestId('conductor-mode-milestones-restore')).toHaveFocus())
    expect(screen.getByTestId('conductor-mode-milestones-removed')).toHaveAttribute('role', 'status')
    fireEvent.click(screen.getByTestId('conductor-mode-milestones-restore'))
    await screen.findByText('Old idea')
    expect(within(screen.getByTestId('conductor-mode-milestones')).getByRole('heading')).toHaveFocus()
  })

  it('still counts every milestone, never "No milestones yet.", when all of them are hidden', async () => {
    // Both workers closed: the crew-count Recap would read them as done ("2 of 2").
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [DROPPED, REJECTED].map((m, n) => ({
      key: `worker-${n}`, title: `Worker ${n}`, agent: 'kirocrew-worker', status: 'closed', group: 'done', last_activity: '', ledger_item: m.item_id, created_by: 'root-1',
    })) })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [DROPPED, REJECTED] })
    localStorage.setItem(CONDUCTOR_MILESTONES_REMOVED_KEY, JSON.stringify({ 'root-1': ['it_c', 'it_b'] }))
    renderPanel()
    expect(await screen.findByTestId('conductor-mode-milestones-removed')).toHaveTextContent('Hidden: 2')
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    const recap = screen.getByTestId('conductor-mode-recap')
    await waitFor(() => expect(recap).toHaveTextContent('0 of 2 done'))
    expect(recap).not.toHaveTextContent('2 of 2')
    expect(screen.queryAllByTestId('conductor-mode-milestone')).toHaveLength(0)
    expect(screen.getByTestId('conductor-mode-milestones')).toHaveTextContent('Milestones · 0 of 2')
    expect(screen.getByTestId('conductor-mode-milestones')).not.toHaveTextContent('No milestones yet.')
  })

  it('keeps a row another tab hid after this panel mounted', async () => {
    renderPanel()
    await screen.findByText('Old idea')
    // Another tab hides the rejected milestone, and one on another main chat.
    localStorage.setItem(CONDUCTOR_MILESTONES_REMOVED_KEY, JSON.stringify({ 'root-1': ['it_b'], 'root-2': ['it_z'] }))
    fireEvent.click(within(screen.getByText('Old idea').closest('li')!).getByTestId('conductor-mode-milestone-remove'))
    await waitFor(() => expect(screen.queryByText('Old idea')).toBeNull())
    expect(readRemovedMilestones()).toEqual({ 'root-1': ['it_b', 'it_c'], 'root-2': ['it_z'] })
    expect(screen.queryByText('Port the gate')).toBeNull()
  })

  it('keeps the run line in the Overview when every milestone is hidden', async () => {
    // Patrol on, nothing settled: the run is working whatever the list shows.
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [DROPPED, REJECTED], run: { settled: '', patrol_on: true, open: 0, accepted: 0, not_done: 2, at_limit: false } })
    localStorage.setItem(CONDUCTOR_MILESTONES_REMOVED_KEY, JSON.stringify({ 'root-1': ['it_c', 'it_b'] }))
    renderPanel()
    expect(await screen.findByTestId('conductor-mode-milestones-removed')).toHaveTextContent('Hidden: 2')
    expect(screen.queryAllByTestId('conductor-mode-milestone')).toHaveLength(0)
    expect(screen.getByTestId('conductor-mode-run')).toHaveAttribute('data-state', 'working')
  })

  it('keeps every Hide when browser storage refuses the write', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Storage is disabled', 'SecurityError') })
    try {
      renderPanel()
      await screen.findByText('Old idea')
      fireEvent.click(within(screen.getByText('Old idea').closest('li')!).getByTestId('conductor-mode-milestone-remove'))
      await waitFor(() => expect(screen.queryByText('Old idea')).toBeNull())
      fireEvent.click(within(screen.getByText('Port the gate').closest('li')!).getByTestId('conductor-mode-milestone-remove'))
      // Hiding the second row must not bring the first one back.
      await waitFor(() => expect(screen.queryByText('Port the gate')).toBeNull())
      expect(screen.queryByText('Old idea')).toBeNull()
      expect(screen.getByTestId('conductor-mode-milestones-removed')).toHaveTextContent('Hidden: 2')
      expect(setItem).toHaveBeenCalled()
      expect(readRemovedMilestones()).toEqual({})
    } finally {
      setItem.mockRestore()
    }
  })

  it('reads an unusable stored value as nothing hidden', () => {
    localStorage.setItem(CONDUCTOR_MILESTONES_REMOVED_KEY, '{not json')
    expect(readRemovedMilestones()).toEqual({})
    localStorage.setItem(CONDUCTOR_MILESTONES_REMOVED_KEY, JSON.stringify({ 'root-1': ['it_c', 7, ''], bad: 'x' }))
    expect(readRemovedMilestones()).toEqual({ 'root-1': ['it_c'] })
  })
})

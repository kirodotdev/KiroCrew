/**
 * Conductor mode Board > Status > Overview: one line says what the run is
 * doing (Working, Waiting for you, Paused, Done or Stopped), from what is live on
 * this screen first and then the board read's `run` verdict. A settled run says
 * one plain sentence with no numbers, and a rejected or abandoned milestone reads
 * Not done, never with the done check.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import type { WorkBoardItem } from '../../../api/crewBoard'
import type { ConductorModeRun } from '../../../types'

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_a', title: 'Ship the fix', acceptance: { kind: 'human_approval' }, state: 'accepted', verdict: 'pass',
  decision: '', round: 1, fails: 0, status: 'done', summary: 'Merged.', artifacts: {}, pr: null,
  last_report_at: '2026-10-30T00:05:00Z', created_at: '2026-10-30T00:00:00Z', closed_at: '2026-10-30T00:10:00Z', decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: true, alive: 'closed', events: [],
  reason: null, awaits_person: false, ...over,
} as WorkBoardItem)

const RUN = (over: Partial<ConductorModeRun>): ConductorModeRun => ({ settled: 'done', open: 0, accepted: 1, not_done: 0, at_limit: false, ...over })
const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Fix the login bug.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [] as WorkBoardItem[], take_over_available: false }

function renderPanel(viewing = 'root-1', slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing={viewing} active /></Provider></QueryClientProvider>)
}

async function runLine() {
  return screen.findByTestId('conductor-mode-run')
}

describe('Conductor mode run line', () => {
  beforeEach(() => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('says Done when every milestone was accepted and the patrol is off', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], run: RUN({}) })
    renderPanel()
    const line = await runLine()
    expect(line).toHaveAttribute('data-state', 'done')
    expect(line.querySelector('svg')?.getAttribute('class')).toContain('crew-tone-ok')
    expect(line).toHaveTextContent('Done')
    expect(within(line).getByTestId('conductor-mode-run-detail')).toHaveTextContent('Every milestone was accepted.')
    // The main chat is on screen: its last message is the report, so no button.
    expect(within(line).queryByRole('button')).toBeNull()
  })

  it('says Done with milestones not done, and marks those rows Not done', async () => {
    const rejected = ITEM({ item_id: 'it_b', title: 'Port the gate', state: 'rejected', verdict: 'fail' })
    const dropped = ITEM({ item_id: 'it_c', title: 'Old idea', state: 'abandoned', verdict: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({}), rejected, dropped], run: RUN({ not_done: 2 }) })
    renderPanel()
    const line = await runLine()
    expect(within(line).getByTestId('conductor-mode-run-detail')).toHaveTextContent('Finished. Some milestones were not done.')
    // The check is muted, never the green of a run where everything landed.
    expect(line.querySelector('svg')?.getAttribute('class')).toContain('text-muted')
    expect(line.querySelector('svg')?.getAttribute('class')).not.toContain('crew-tone-ok')
    const rows = screen.getAllByTestId('conductor-mode-milestone').filter(r => r.getAttribute('data-state') === 'not_done')
    expect(rows.map(r => r.textContent)).toEqual([
      expect.stringContaining('Not done · failed its check'),
      expect.stringContaining('Not done · dropped'),
    ])
    // Only the accepted one counts as done.
    expect(screen.getByTestId('conductor-mode-milestones')).toHaveTextContent('Milestones · 1 of 3')
  })

  it('says Stopped with open milestones, without saying who stopped it', async () => {
    const open = ITEM({ item_id: 'it_d', title: 'Write docs', state: 'open', status: 'progress', terminal: false, alive: 'idle', closed_at: null, verdict: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({}), open], run: RUN({ settled: 'stopped', open: 1 }) })
    renderPanel()
    const line = await runLine()
    expect(line).toHaveAttribute('data-state', 'stopped')
    // A stop sign, not a hollow square that reads as a checkbox.
    expect(line.querySelector('svg')?.getAttribute('class')).toContain('lucide-circle-stop')
    expect(within(line).getByTestId('conductor-mode-run-detail')).toHaveTextContent('Some milestones are still open and nothing is checking on them. Write to the main chat to continue.')
    expect(line.textContent).not.toMatch(/\byou\b.*stopped|stopped by/i)
  })

  it('names a time or budget limit when the patrol ended at one', async () => {
    const open = ITEM({ item_id: 'it_d', state: 'open', status: 'progress', terminal: false, alive: 'idle', closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [open], run: RUN({ settled: 'stopped', open: 1, accepted: 0, at_limit: true }) })
    renderPanel()
    const detail = within(await runLine()).getByTestId('conductor-mode-run-detail')
    expect(detail).toHaveTextContent('It stopped at a time or budget limit with milestones still open.')
    // A stop at the limit sends no final report, so nothing says one exists.
    expect(detail.textContent).not.toMatch(/final report/i)
  })

  it('says Working while the patrol is on, even with every milestone closed', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], run: RUN({ settled: '' }) })
    renderPanel()
    const line = await runLine()
    expect(line).toHaveAttribute('data-state', 'working')
    expect(within(line).queryByTestId('conductor-mode-run-detail')).toBeNull()
  })

  it('says Waiting for you over a settled verdict when an item asks the person', async () => {
    const ask = ITEM({ item_id: 'it_q', state: 'open', status: 'question', reason: 'needs_human', terminal: false, alive: 'idle', awaits_person: true, outstanding: true, closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ask], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) })
    renderPanel()
    expect(await runLine()).toHaveAttribute('data-state', 'waiting')
  })

  it("says Waiting for you while the main chat's own tool approval is open, though its turn runs", async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], run: RUN({ settled: '' }) })
    renderPanel('root-1', [{ key: 'root-1', title: 'Main', running: true, pending_approval: true }])
    expect(await runLine()).toHaveAttribute('data-state', 'waiting')
  })

  it('adds no second way back beside the panel Back row, and claims no report', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], run: RUN({}) })
    renderPanel('worker-1', [{ key: 'root-1', title: 'Main' }])
    const line = await runLine()
    expect(screen.getAllByTestId('crew-panel-back')).toHaveLength(1)
    expect(within(line).queryByRole('button')).toBeNull()
    expect(line.textContent).not.toMatch(/final report/i)
  })

  it("a failed reopen from an ask row is reported once, in that row, not again by the run line", async () => {
    const ask = ITEM({ item_id: 'it_q', state: 'open', status: 'question', reason: 'needs_human', terminal: false, alive: 'idle', awaits_person: true, outstanding: true, closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ask], run: RUN({ settled: '', open: 1, accepted: 0 }) })
    api.resumeChatSlot.mockResolvedValue({ ok: false })
    renderPanel('w-1', [{ key: 'w-1', title: 'Worker', crew_root: 'root-1' }])
    fireEvent.click(await screen.findByTestId('conductor-mode-person-ask-open-main'))
    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalled())
    // The panel's Back row keeps its own instance too; only the ask row reports this click.
    await waitFor(() => expect(screen.getAllByText('Could not reopen that session. Try again.')).toHaveLength(1))
  })

  it('draws no line for a chat with no goal and no milestone', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, conductor: { ...BOARD.conductor, goal: '' }, items: [], run: RUN({ settled: '', accepted: 0 }) })
    renderPanel()
    await screen.findByText('No milestones yet.')
    expect(screen.queryByTestId('conductor-mode-run')).toBeNull()
  })
})

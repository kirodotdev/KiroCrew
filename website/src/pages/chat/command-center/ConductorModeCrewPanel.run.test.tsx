/**
 * Conductor mode Board > Status > Overview: one line says what the run is
 * doing (Working, Waiting for you, Paused, Done or Stopped), from what is live on
 * this screen first and then the board read's `run` verdict. A settled run says
 * one plain sentence with no numbers, and a rejected or abandoned milestone reads
 * Not done, never with the done check.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import { setQuestionCard, sseWorkflowEvent } from '../../../store/chatSlice'
import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import { crewBoardQueryKey, type WorkBoardItem } from '../../../api/crewBoard'
import type { ConductorModeRun, CrewWorker } from '../../../types'
import type { SessionSummary } from '../../../types/sessionSummary'
import { crewWorkersQueryKey } from './CrewWorkerList'

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_a', title: 'Ship the fix', acceptance: { kind: 'human_approval' }, state: 'accepted', verdict: 'pass',
  decision: '', round: 1, fails: 0, status: 'done', summary: 'Merged.', artifacts: {}, pr: null,
  last_report_at: '2026-10-30T00:05:00Z', created_at: '2026-10-30T00:00:00Z', closed_at: '2026-10-30T00:10:00Z', decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: true, alive: 'closed', events: [],
  reason: null, awaits_person: false, ...over,
} as WorkBoardItem)

const RUN = (over: Partial<ConductorModeRun>): ConductorModeRun => ({ settled: 'done', open: 0, accepted: 1, not_done: 0, at_limit: false, patrol_on: over.settled === '', ...over })
const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Fix the login bug.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [] as WorkBoardItem[], take_over_available: false }

function renderPanel(viewing = 'root-1', slots: unknown[] = [], qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const store = createTestStore({ dashboard: { slots } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing={viewing} active /></Provider></QueryClientProvider>)
  return store
}

async function runLine() {
  return screen.findByTestId('conductor-mode-run')
}

describe('Conductor mode run line', () => {
  beforeEach(() => {
    Object.values(api).forEach(mock => mock.mockReset())
    vi.setSystemTime(new Date('2026-10-30T01:00:00Z'))
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

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

  it('says Working while the root has active sub-agents', async () => {
    const open = ITEM({ state: 'open', status: 'progress', terminal: false, alive: 'idle', closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [open], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) })
    renderPanel('root-1', [{ key: 'root-1', title: 'Main', subagents_running: true }])
    expect(await runLine()).toHaveAttribute('data-state', 'working')
  })

  it('says Working while the root owns a workflow', async () => {
    const open = ITEM({ state: 'open', status: 'progress', terminal: false, alive: 'idle', closed_at: null })
    const stopped = { ...BOARD, items: [open], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) }
    api.crewBoard.mockResolvedValueOnce(stopped).mockResolvedValue({ ...stopped, conductor_working: true })
    const store = renderPanel('root-1', [{ key: 'root-1', title: 'Main' }])
    expect(await runLine()).toHaveAttribute('data-state', 'stopped')
    act(() => { store.dispatch(sseWorkflowEvent({ run_id: 'wf-root', session_key: 'root-1', type: 'run_started', data: { name: 'Review' } })) })
    await waitFor(() => expect(screen.getByTestId('conductor-mode-run')).toHaveAttribute('data-state', 'working'))
  })

  it('keeps a stale milestone running while its worker owns a workflow', async () => {
    const open = ITEM({ item_id: 'it_wf', state: 'open', status: 'progress', terminal: false, stale: true, alive: 'idle', closed_at: null })
    const waitingWorker = {
      root: 'root-1',
      workers: [{
        key: 'worker-1', title: 'Worker', agent: 'kirocrew-worker', status: 'idle', group: 'waiting',
        last_activity: '', ledger_item: 'it_wf', created_by: 'root-1',
      }],
    }
    api.crewWorkers.mockResolvedValueOnce(waitingWorker).mockResolvedValue({
      ...waitingWorker,
      workers: [{ ...waitingWorker.workers[0], status: 'working', group: 'working' }],
    })
    const quiet = { ...BOARD, items: [open], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) }
    api.crewBoard.mockResolvedValueOnce(quiet).mockResolvedValue({
      ...quiet,
      items: [{ ...open, alive: 'running', stale: false }],
    })
    const store = renderPanel('root-1', [{ key: 'worker-1', title: 'Worker', crew_root: 'root-1', created_by: 'root-1' }])
    await waitFor(() => expect(screen.getByTestId('conductor-mode-milestone')).toHaveAttribute('data-state', 'quiet'))
    act(() => { store.dispatch(sseWorkflowEvent({ run_id: 'wf-worker', session_key: 'worker-1', type: 'run_started', data: { name: 'Review' } })) })
    await waitFor(() => expect(screen.getByTestId('conductor-mode-milestone')).toHaveAttribute('data-state', 'running'))
    expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent('1 running')
  })

  it('says Waiting for you over a settled verdict when an item asks the person', async () => {
    const ask = ITEM({ item_id: 'it_q', state: 'open', status: 'question', reason: 'needs_human', terminal: false, alive: 'idle', awaits_person: true, outstanding: true, closed_at: null, ask: { what: 'Pick one', why: '', options: [], pick: '', pick_why: '', written_at: '2026-10-30T00:06:00Z' } })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ask], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) })
    renderPanel()
    expect(await runLine()).toHaveAttribute('data-state', 'waiting')
  })

  it.each([
    { status: 'blocked' as const, pendingApproval: false },
    { status: 'question' as const, pendingApproval: false },
    { status: 'blocked' as const, pendingApproval: true },
    { status: 'question' as const, pendingApproval: true },
  ])('keeps a $status report on the person until the board clears it, pendingApproval=$pendingApproval', async ({ status, pendingApproval }) => {
    const ask = ITEM({
      state: 'open', status, reason: 'needs_human', terminal: false, verdict: null,
      alive: 'running', awaits_person: true, outstanding: true, closed_at: null,
      ask: { what: 'Pick a region', why: '', options: [], pick: '', pick_why: '', written_at: '2026-10-30T00:06:00Z' },
    })
    const background: CrewWorker = {
      key: 'worker-1', title: 'Worker', agent: 'kirocrew-worker', status: pendingApproval ? 'needs_approval' : 'working',
      group: pendingApproval ? 'approval' : 'working',
      last_activity: '', ledger_item: ask.item_id, created_by: 'root-1',
    }
    const board = { ...BOARD, items: [ask], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) }
    api.crewBoard.mockResolvedValue(board)
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [background] })
    const slots = pendingApproval ? [{
      key: 'worker-1', title: 'Worker', crew_root: 'root-1', pending_approval: true,
      pending_approval_info: { request_id: 'approve-turn', tool: 'execute_bash', tool_input: 'pwd', tool_kind: 'shell', message: 'Run pwd' },
    }] : []
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPanel('root-1', slots, qc)
    const expectAsking = () => {
      expect(screen.getByTestId('conductor-mode-run')).toHaveAttribute('data-state', 'waiting')
      expect(screen.getByTestId('conductor-mode-milestone')).toHaveAttribute('data-state', 'needs_you')
      expect(within(screen.getByTestId('conductor-mode-needs-you')).getByRole('heading', { name: /^Needs your input/ })).toHaveTextContent('Needs your input · 1')
      expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent('Ship the fix waits on you')
      if (pendingApproval) {
        expect(screen.getByTestId('crew-approval-cards')).toBeInTheDocument()
        expect(screen.queryByTestId('conductor-mode-person-ask')).toBeNull()
      } else {
        expect(screen.getByTestId('conductor-mode-person-ask')).toBeInTheDocument()
        expect(screen.queryByTestId('crew-approval-cards')).toBeNull()
      }
    }
    // Background work alone leaves the person blocked on every surface.
    await waitFor(() => {
      expect(api.crewWorkers).toHaveBeenCalled()
      expectAsking()
    })
    const boardReads = api.crewBoard.mock.calls.length
    // A fresh worker-list read alone, however its worker moved, decides no ask.
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [{ ...background, last_activity: '2026-11-17T00:00:00Z' }] })
    await act(async () => { await qc.refetchQueries({ queryKey: crewWorkersQueryKey('root-1'), exact: true }) })
    await waitFor(() => {
      expect(qc.getQueryData<{ workers: CrewWorker[] }>(crewWorkersQueryKey('root-1'))?.workers[0].last_activity).toBe('2026-11-17T00:00:00Z')
      expectAsking()
    })
    expect(api.crewBoard).toHaveBeenCalledTimes(boardReads)
    // The board read the worker's model turn: only now does the ask end.
    const moved = { ...board, items: [{ ...ask, awaits_person: false }] }
    api.crewBoard.mockResolvedValue(moved)
    await act(async () => { await qc.refetchQueries({ queryKey: crewBoardQueryKey('root-1'), exact: true }) })
    await waitFor(() => {
      expect(qc.getQueryData(crewBoardQueryKey('root-1'))).toEqual(moved)
      expect(screen.queryByTestId('conductor-mode-person-ask')).toBeNull()
      expect(screen.getByTestId('conductor-mode-milestone')).toHaveAttribute('data-state', pendingApproval ? 'needs_you' : 'running')
      expect(screen.getByTestId('conductor-mode-run')).toHaveAttribute('data-state', pendingApproval ? 'waiting' : 'working')
      expect(within(screen.getByTestId('conductor-mode-needs-you')).getByRole('heading', { name: /^Needs your input/ })).toHaveTextContent(`Needs your input · ${pendingApproval ? 1 : 0}`)
      expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent(pendingApproval ? 'Ship the fix waits on you' : '1 running')
      if (pendingApproval) {
        expect(screen.getByTestId('crew-approval-cards')).toBeInTheDocument()
        expect(screen.getByTestId('conductor-mode-milestone')).toHaveTextContent('Waiting on your approval above')
      } else {
        expect(screen.queryByTestId('crew-approval-cards')).toBeNull()
        expect(screen.getByTestId('conductor-mode-needs-you-none')).toBeInTheDocument()
      }
    })
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
    const ask = ITEM({ item_id: 'it_q', state: 'open', status: 'question', reason: 'needs_human', terminal: false, alive: 'idle', awaits_person: true, outstanding: true, closed_at: null, ask: { what: 'Pick one', why: '', options: [], pick: '', pick_why: '', written_at: '2026-10-30T00:06:00Z' } })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ask], run: RUN({ settled: '', open: 1, accepted: 0 }) })
    api.resumeChatSlot.mockResolvedValue({ ok: false })
    renderPanel('w-1', [{ key: 'w-1', title: 'Worker', crew_root: 'root-1' }])
    fireEvent.click(await screen.findByTestId('conductor-mode-person-ask-open-main'))
    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalled())
    // The panel's Back row keeps its own instance too; only the ask row reports this click.
    await waitFor(() => expect(screen.getAllByText('Could not reopen that session. Try again.')).toHaveLength(1))
  })

  const SUMMARY = (line: string): SessionSummary => ({
    enabled: true, stale: false, constraints: [], generated_at: 1, user_turns: 3, last_activity: null,
    intents: [{ title: 'Fix the login bug', initial_intent: '', progress: [line], next_steps: [], ranges: [[1, 3]], status: 'active', verified: null, state: 'in-progress', last_touched_turn: 3, origin_turn: 1 }],
  })

  async function recapReady(qc: QueryClient) {
    // Board, crew and summary reads settle independently.
    await waitFor(() => {
      for (const key of [crewBoardQueryKey('root-1'), crewWorkersQueryKey('root-1'), ['session-summary', 'root-1']]) {
        expect(qc.getQueryState(key)).toMatchObject({ status: 'success', fetchStatus: 'idle' })
      }
    })
  }

  it.each([
    { state: 'running', facts: { alive: 'running' as const }, summary: 'Ship the fix has gone quiet.', recap: '0 of 1 done; 1 running.' },
    { state: 'needs_you', facts: { alive: 'running' as const, status: 'blocked' as const, awaits_person: true, reason: 'needs_human' }, summary: 'Ship the fix is running.', recap: '0 of 1 done; Ship the fix waits on you.' },
    { state: 'stuck', facts: { alive: 'closed' as const }, summary: 'Ship the fix is running.', recap: '0 of 1 done; Ship the fix is stuck.' },
    { state: 'quiet', facts: { stale: true }, summary: 'Ship the fix is running.', recap: '0 of 1 done; Ship the fix has gone quiet.' },
  ])('Recap keeps the Board $state state over an enabled cached summary', async ({ state, facts, summary, recap }) => {
    const open = ITEM({ state: 'open', status: 'progress', terminal: false, verdict: null, alive: 'idle', closed_at: null, ...facts })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [open], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) })
    api.sessionSummary.mockResolvedValue(SUMMARY(summary))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPanel('root-1', [], qc)
    await recapReady(qc)
    await waitFor(() => {
      expect(screen.getByTestId('conductor-mode-milestone')).toHaveAttribute('data-state', state)
      expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent(recap)
    })
  })

  it.each(['bound', 'unbound', 'root'] as const)('Recap keeps a real %s tool approval over an enabled summary', async kind => {
    const open = ITEM({ state: 'open', status: 'progress', terminal: false, verdict: null, alive: 'running', closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [open], run: RUN({ settled: '', open: 1, accepted: 0 }) })
    api.sessionSummary.mockResolvedValue(SUMMARY('Ship the fix is running.'))
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: kind === 'root' ? [] : [{
      key: 'worker-1', title: 'Worker', agent: 'kirocrew-worker', status: 'needs_approval', group: 'approval',
      last_activity: '', ledger_item: kind === 'bound' ? open.item_id : '', created_by: 'root-1',
    }] })
    const slots = [{
      key: kind === 'root' ? 'root-1' : 'worker-1', title: kind === 'root' ? 'Main' : 'Worker',
      crew_root: kind === 'root' ? undefined : 'root-1', running: true, pending_approval: true,
      pending_approval_info: { request_id: `approve-${kind}`, tool: 'execute_bash', tool_input: 'pwd', tool_kind: 'shell', message: 'Run pwd' },
    }]
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPanel('root-1', slots, qc)
    await recapReady(qc)
    await waitFor(() => {
      expect(screen.getByTestId('conductor-mode-run')).toHaveAttribute('data-state', 'waiting')
      const who = kind === 'bound' ? 'Ship the fix' : kind === 'root' ? 'Main' : 'Worker'
      expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent(`${who} waits on you`)
      if (kind === 'bound') expect(screen.getByTestId('conductor-mode-recap')).not.toHaveTextContent('Worker')
    })
  })

  it('Recap keeps the main chat question over an enabled summary', async () => {
    const open = ITEM({ state: 'open', status: 'progress', terminal: false, verdict: null, alive: 'idle', closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [open], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) })
    api.sessionSummary.mockResolvedValue(SUMMARY('Ship the fix has gone quiet.'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = renderPanel('root-1', [], qc)
    await recapReady(qc)
    act(() => { store.dispatch(setQuestionCard({ slot: 'root-1', card_id: 'recap-question', questions: [{ question: 'Which environment?' }] })) })
    expect(screen.getByTestId('conductor-mode-main-question')).toHaveTextContent('Which environment?')
    expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent('a question for you waits on you')
  })

  it('Recap keeps root background work from being described as quiet', async () => {
    const open = ITEM({ state: 'open', status: 'progress', terminal: false, verdict: null, alive: 'idle', closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [open], conductor_working: true, run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) })
    api.sessionSummary.mockResolvedValue(SUMMARY('Ship the fix has gone quiet.'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPanel('root-1', [], qc)
    await recapReady(qc)
    await waitFor(() => {
      expect(screen.getByTestId('conductor-mode-run')).toHaveAttribute('data-state', 'working')
      expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent(/^0 of 1 done\.$/)
    })
  })

  it('Recap preserves a descriptive enabled summary when no live state or ask overrides it', async () => {
    const description = 'Ship the fix covers keyboard focus and narrow screens.'
    const open = ITEM({ state: 'open', status: 'progress', terminal: false, verdict: null, alive: 'idle', closed_at: null })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [open], run: RUN({ settled: 'stopped', open: 1, accepted: 0 }) })
    api.sessionSummary.mockResolvedValue(SUMMARY(description))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPanel('root-1', [], qc)
    await recapReady(qc)
    await waitFor(() => {
      expect(screen.getByTestId('conductor-mode-milestone')).toHaveAttribute('data-state', 'waiting')
      expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent(description)
    })
  })

  it('draws no line for a chat with no goal and no milestone', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, conductor: { ...BOARD.conductor, goal: '' }, items: [], run: RUN({ settled: '', accepted: 0 }) })
    renderPanel()
    await screen.findByText('No milestones yet.')
    expect(screen.queryByTestId('conductor-mode-run')).toBeNull()
  })

  it('draws no Working for a new goal waiting on its first milestone with no patrol on', async () => {
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({})], run: RUN({ settled: '', patrol_on: false }) })
    renderPanel()
    await screen.findAllByText('Ship the fix')
    expect(screen.queryByTestId('conductor-mode-run')).toBeNull()
  })
})

/**
 * Conductor mode Board > Status on a long run: no text is cut. The Overview shows
 * the whole goal and Done when, plus the goal's latest Changed entry with its age;
 * a running milestone shows the time since its report, never "now"; the done fold
 * reads only its count; the Recap counts running milestones instead of naming them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), crewBoard: vi.fn(), sessionSummary: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import type { WorkBoardItem } from '../../../api/crewBoard'

const NOW = Date.parse('2026-11-14T12:00:00Z')
const LONG = 'Speed fix: custom agents at sixty-four servers get faster with the same answer (pure glob memo, one scope read, no rewrite of an identical environment file)'

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_a', title: 'Ship the fix', acceptance: { kind: 'pr_checks' }, state: 'accepted', verdict: 'pass',
  decision: '', round: 1, fails: 0, status: 'done', summary: 'Merged.', artifacts: {}, pr: null,
  last_report_at: '2026-11-14T10:00:00Z', created_at: '2026-11-14T09:00:00Z', closed_at: '2026-11-14T10:00:00Z', decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: true, alive: 'closed', events: [],
  reason: null, awaits_person: false, ...over,
} as WorkBoardItem)

const RUNNING = ITEM({
  item_id: 'it_r', title: LONG, state: 'open', verdict: null, status: 'progress', terminal: false, closed_at: null, alive: 'running',
  summary: 'New head dcc4fb9d3b14c95d8df302e5c0c75647c37b29eb (ls-remote), on main d15505809e1d.', last_report_at: '2026-11-14T11:31:00Z',
})

const GOAL = 'Make the main session reply as fast as the CLI or faster on the same engine, with the same security bar the CLI holds today. '
  + 'Done when: side-by-side benchmarks show a reply in 0.63 s or less on Linux, macOS and Windows. '
  + 'Plan: profile, then fix. Changed: 2026-11-10 09:00 first plan; 2026-11-12T12:00Z the owner added three research tasks.'

function renderPanel(goal: string) {
  api.crewBoard.mockResolvedValue({
    conductor: { schema: 1, slot_key: 'root-1', goal, round: 1, depth: 0, parent_item: null, created_at: '' },
    conductor_alive: 'idle', items: [ITEM({}), ITEM({ item_id: 'it_b', title: 'Second done one' }), RUNNING], take_over_available: false,
    run: { settled: '', open: 1, accepted: 2, not_done: 0, at_limit: false },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><Provider store={createTestStore({ dashboard: { slots: [] } } as never)}><ConductorModeCrewPanel root="root-1" viewing="root-1" active /></Provider></QueryClientProvider>)
  // The board read has landed once its running milestone is on screen.
  return screen.findByText(LONG)
}

describe('Conductor mode Status on a long run', () => {
  beforeEach(() => {
    // One clock, frozen: only Date is faked, so the query's own timers still run.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => { vi.useRealTimers(); vi.clearAllMocks() })

  it('shows the whole goal, the whole Done when and the latest change with its age', async () => {
    await renderPanel(GOAL)
    expect(screen.getByTestId('conductor-mode-goal')).toHaveTextContent('Make the main session reply as fast as the CLI or faster on the same engine, with the same security bar the CLI holds today.')
    expect(screen.getByTestId('conductor-mode-done-when')).toHaveTextContent('Done when: side-by-side benchmarks show a reply in 0.63 s or less on Linux, macOS and Windows')
    const changed = screen.getByTestId('conductor-mode-changed')
    expect(changed).toHaveTextContent(/^Latest change, 2d ago: 2026-11-12T12:00Z the owner added three research tasks$/)
    expect(changed).toHaveAttribute('title', '2026-11-12T12:00Z')
  })

  it('reads the newest entry of a Changed part written one entry per line', async () => {
    await renderPanel('Ship it. Done when: merged.\nPlan: a.\nChanged: 2026-11-12 09:00Z first plan\n2026-11-14 10:00Z re-planned after the review')
    expect(screen.getByTestId('conductor-mode-changed')).toHaveTextContent(/^Latest change, 2h ago: 2026-11-14 10:00Z re-planned after the review$/)
  })

  it('shows no change line while the goal has no Changed entry', async () => {
    await renderPanel('Ship the login fix. Done when: the PR is merged.')
    expect(screen.getByTestId('conductor-mode-goal')).toHaveTextContent('Ship the login fix.')
    expect(screen.queryByTestId('conductor-mode-changed')).toBeNull()
  })

  it('cuts no text: no truncate or line clamp in the Status view, and no ellipsis added', async () => {
    await renderPanel(GOAL)
    const status = screen.getByTestId('conductor-mode-status')
    expect(status.querySelector('.truncate, [class*="line-clamp"]')).toBeNull()
    expect(status.textContent).not.toContain('…')
  })

  it('gives a running milestone its age, not "now", and the done fold only its count', async () => {
    await renderPanel(GOAL)
    const row = screen.getAllByTestId('conductor-mode-milestone').find(r => r.getAttribute('data-state') === 'running')!
    expect(row.textContent).not.toMatch(/\bnow\b/)
    expect(row).toHaveTextContent('29m')
    expect(within(screen.getByTestId('conductor-mode-milestones-done')).getByText('2 done').closest('summary')?.textContent).toBe('2 done')
  })

  it('counts running milestones in the Recap instead of repeating their titles', async () => {
    await renderPanel(GOAL)
    expect(screen.getByTestId('conductor-mode-recap')).toHaveTextContent(/^2 of 3 done; 1 running\.$/)
  })
})

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import InsightsPage from '../pages/InsightsPage'
import type { InsightsAction, InsightsView } from '../api/client/personalInsights'

vi.mock('../api/client', () => ({
  api: {
    personalInsightsLatest: vi.fn(),
    personalInsightsDoIt: vi.fn(),
    personalInsightsUndo: vi.fn(),
  },
}))

function action(overrides: Partial<InsightsAction> = {}): InsightsAction {
  return {
    action_id: 'a1-abc',
    action_key: 'k',
    action_class: 'lesson_proposal',
    behavior_predicate: 'verification_omission_explicit',
    title: 'Verify outputs before claiming done',
    cta: 'Do it',
    executable: true,
    why: 'Shipped defects were caught by hand.',
    display_artifact: 'Re-read the artifact before claiming done.\nNOT: Do not trust the last tool call.',
    expected_observation: 'Fewer owner corrections.',
    verification: 'Check the next session.',
    undo: 'Remove the saved lesson.',
    rank: 1,
    claim_ids: ['f1'],
    state: 'proposed',
    applied_at: null,
    verified_at: null,
    undone_at: null,
    evidence_sessions: 4,
    evidence_total: 36,
    evidence_keys: [],
    ...overrides,
  }
}

function view(actions: InsightsAction[], prior: InsightsView['prior_actions'] = []): InsightsView {
  return {
    run: {
      run_id: '20261009T213430Z-b035f8',
      created_at: 1_791_581_670,
      analyzed: 36,
      status: 'complete',
      artifact_slug: null,
      window_days: 30,
      cataloged: 197,
      served_model: 'unknown',
    },
    actions,
    prior_actions: prior,
    report_before: '# Personal Insights\n\n## What stands out\n\nYou run the agent like a staff.\n',
    report_after: '## What is working\n\n- Product judgment.\n',
    runs: [],
    follow_through_min_sessions: 5,
  }
}

describe('InsightsPage', () => {
  beforeEach(() => { vi.resetAllMocks() })

  it('renders the report around real action cards and flips Do it to Done and verified with Undo', async () => {
    const { api } = await import('../api/client')
    const latest = vi.mocked(api.personalInsightsLatest)
    latest.mockResolvedValueOnce(view([action()]))
    vi.mocked(api.personalInsightsDoIt).mockResolvedValue({
      action_id: 'a1-abc', state: 'applied_verified', message: 'ok', verified: true, undo_available: true,
    })
    latest.mockResolvedValueOnce(view([action({ state: 'applied_verified', applied_at: 1_791_582_000, verified_at: 1_791_582_000 })]))

    renderWithProviders(<InsightsPage />)
    await screen.findByText(/You run the agent like a staff/)
    expect(screen.getByText(/Product judgment/)).toBeInTheDocument()
    const card = screen.getByTestId('insights-action')
    expect(card).toHaveAttribute('data-state', 'proposed')
    expect(within(card).getByText('Evidence: 4 of 36 analyzed sessions')).toBeInTheDocument()

    fireEvent.click(within(card).getByRole('button', { name: 'Do it' }))
    await waitFor(() => expect(api.personalInsightsDoIt).toHaveBeenCalledWith('a1-abc', false))
    await waitFor(() => expect(screen.getByTestId('insights-action')).toHaveAttribute('data-state', 'applied_verified'))
    const applied = screen.getByTestId('insights-action')
    expect(within(applied).getByText('Done and verified')).toBeInTheDocument()
    expect(within(applied).getByRole('button', { name: /Undo/ })).toBeInTheDocument()
    expect(within(applied).queryByRole('button', { name: 'Do it' })).toBeNull()
  })

  it('shows the evidence-check hold with Apply anyway and forwards force on retry', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.personalInsightsLatest).mockResolvedValue(view([action()]))
    const doIt = vi.mocked(api.personalInsightsDoIt)
    doIt.mockResolvedValueOnce({
      action_id: 'a1-abc',
      state: 'held',
      message: 'held',
      overfit: {
        passed: false, supporting_sessions: 1, independent_lineages: 1, distinct_days: 1,
        largest_lineage_share: 1, guidance_overlap: 0, duplicate_method: 'none',
        overlapping_guidance: [], reasons: ['fewer than 2 supporting sessions'],
      },
    })
    doIt.mockResolvedValueOnce({ action_id: 'a1-abc', state: 'applied_verified', message: 'ok' })

    renderWithProviders(<InsightsPage />)
    const card = await screen.findByTestId('insights-action')
    fireEvent.click(within(card).getByRole('button', { name: 'Do it' }))
    await screen.findByText(/Held, not applied/)
    expect(screen.getByText('fewer than 2 supporting sessions')).toBeInTheDocument()
    expect(screen.getByText('Evidence check: 1 sessions, 1 independent threads, 1 distinct days')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Apply anyway' }))
    await waitFor(() => expect(doIt).toHaveBeenLastCalledWith('a1-abc', true))
  })

  it('lists earlier applied actions with Undo it and keeps copy-first classes copy-only', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.personalInsightsLatest).mockResolvedValue(view(
      [action({ action_id: 'a3-prompt', action_class: 'prompt', executable: false, rank: 3, title: 'Standing CR prompt' })],
      [{ run_id: 'r1', action_id: 'a1-old', action_class: 'lesson_proposal', title: 'Older lesson', state: 'applied_verified', applied_at: 1_791_577_210, verified_at: 1_791_577_210, baseline_sessions: 36 }],
    ))
    vi.mocked(api.personalInsightsUndo).mockResolvedValue({ action_id: 'a1-old', state: 'undone', message: 'removed' })

    renderWithProviders(<InsightsPage />)
    await screen.findByText('Applied earlier')
    expect(screen.getByText('Older lesson')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Undo it' }))
    await waitFor(() => expect(api.personalInsightsUndo).toHaveBeenCalledWith('a1-old'))
    const card = screen.getByTestId('insights-action')
    expect(within(card).queryByRole('button', { name: /Use this now|Do it/ })).toBeNull()
    expect(within(card).getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    expect(within(card).getByText(/Copy-first in this release/)).toBeInTheDocument()
  })

  it('explains an empty repository instead of failing', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.personalInsightsLatest).mockRejectedValue(new Error('404 no_runs'))
    renderWithProviders(<InsightsPage />)
    await screen.findByText(/No report yet/)
  })
})

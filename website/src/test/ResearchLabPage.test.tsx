/**
 * Integration test for src/apps/auto-research/ResearchLabPage.tsx.
 *
 * Exercises the major rendering paths end-to-end with a mocked api client.
 * Breadth over depth:
 *   - Empty state + list (active + history) rendering
 *   - Campaign detail: findings, evidence badges, controls, nudge
 *   - Stagnation banner
 *   - Setup wizard: steps, sub-questions, validation pass/fail, submit
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store } from '../store'

vi.mock('../api/client', () => ({
  api: {
    researchCampaigns: vi.fn(),
    researchCampaign: vi.fn(),
    researchValidate: vi.fn(),
    researchGrillExpand: vi.fn(),
    researchCreate: vi.fn(),
    researchAction: vi.fn(),
    researchNudge: vi.fn(),
    researchReport: vi.fn(),
    researchDelete: vi.fn(),
    researchAddQuestion: vi.fn(),
    researchToKnowledge: vi.fn(),
    researchToArtifact: vi.fn(),
    researchKnowledgeStatus: vi.fn(() => Promise.resolve({ in_library: false })),
    researchReportStatus: vi.fn(() => Promise.resolve({ slug: null })),
  },
}))

import { api } from '../api/client'
import ResearchLabPage from '../apps/auto-research/ResearchLabPage'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <MemoryRouter initialEntries={['/auto-research']}>
          <ResearchLabPage />
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

const FINDINGS = [
  {
    cycle: 1, summary: 'Initial scan of rate-limiting approaches.',
    sources_checked: ['web:nginx-docs'], sources_empty: ['internal:wiki'],
    new_findings_count: 2, evidence_strength: 'strong', key_insight: 'Token bucket is common',
    verification: { passed: true },
  },
  {
    cycle: 2, summary: 'Internal teams mostly use sliding window.',
    sources_checked: ['internal:teamX'], sources_empty: [],
    new_findings_count: 1, evidence_strength: 'moderate', key_insight: 'Sliding window internally',
  },
]

const ACTIVE = {
  id: 'aaaa1111', name: 'Rate limiting research', question: 'How do other teams handle API rate limiting effectively?',
  sub_questions: '[]', sources: '["web"]', max_cycles: 30, idle_secs: 60,
  status: 'running', total_cycles: 2, findings: FINDINGS,
}
const DONE = {
  id: 'bbbb2222', name: 'Old campaign', question: 'A finished question about caching strategies',
  sub_questions: '[]', sources: '["web"]', max_cycles: 10, idle_secs: 60,
  status: 'complete', total_cycles: 8, findings: [],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.researchAction).mockResolvedValue({ ok: true })
  vi.mocked(api.researchNudge).mockResolvedValue({ ok: true })
  vi.mocked(api.researchGrillExpand).mockResolvedValue({ nodes: [] })
})

describe('ResearchLabPage', () => {
  it('renders the empty state with a New Campaign button', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([])
    renderPage()
    await waitFor(() =>
      expect(screen.getByText(/Run autonomous research campaigns/i)).toBeInTheDocument(),
    )
    expect(screen.getAllByRole('button', { name: /New Campaign/i }).length).toBeGreaterThan(0)
  })

  it('lists active + history campaigns and disables New Campaign while active', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE, DONE])
    renderPage()
    await waitFor(() => expect(screen.getByText('ACTIVE')).toBeInTheDocument())
    expect(screen.getByText('HISTORY')).toBeInTheDocument()
    expect(screen.getByText('How do other teams handle API rate limiting effectively?')).toBeInTheDocument()
    expect(screen.getByText('A finished question about caching strategies')).toBeInTheDocument()
    expect(screen.getByText(/One campaign at a time/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New Campaign/i })).toBeDisabled()
  })

  it('lists a needs_input campaign under ACTIVE (not HISTORY)', async () => {
    const blocked = { ...ACTIVE, status: 'needs_input' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([blocked])
    renderPage()
    await waitFor(() => expect(screen.getByText('ACTIVE')).toBeInTheDocument())
    expect(screen.queryByText('HISTORY')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New Campaign/i })).toBeDisabled()
  })

  it('shows a distinct state badge for each campaign status at the root', async () => {
    const failed = { ...DONE, id: 'cccc3333', question: 'A campaign that failed', status: 'failed' }
    const stopped = { ...DONE, id: 'dddd4444', question: 'A campaign that was stopped', status: 'stopped' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE, DONE, failed, stopped])
    renderPage()
    await waitFor(() => expect(screen.getByText('ACTIVE')).toBeInTheDocument())
    // running -> Working (active card); complete/failed/stopped -> history badges.
    expect(screen.getByText('Working')).toBeInTheDocument()
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('Stopped')).toBeInTheDocument()
  })

  it('collapses a long campaign brief with a Show more / Show less toggle', async () => {
    const longQ = 'A very long research brief. '.repeat(20) // ~560 chars > 280 threshold
    const longCampaign = { ...ACTIVE, id: 'eeee5555', question: longQ }
    vi.mocked(api.researchCampaigns).mockResolvedValue([longCampaign])
    vi.mocked(api.researchCampaign).mockResolvedValue(longCampaign)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText(new RegExp(longQ.slice(0, 20)))).toBeInTheDocument())
    await user.click(screen.getByText(new RegExp(longQ.slice(0, 20))))
    await waitFor(() => expect(screen.getByText('Show more')).toBeInTheDocument())
    await user.click(screen.getByText('Show more'))
    expect(screen.getByText('Show less')).toBeInTheDocument()
  })

  it('opens campaign detail and renders findings with evidence badges', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE, DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(ACTIVE)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('How do other teams handle API rate limiting effectively?')).toBeInTheDocument())
    await user.click(screen.getByText('How do other teams handle API rate limiting effectively?'))
    await waitFor(() => expect(screen.getByText(/Findings \(/)).toBeInTheDocument())
    expect(screen.getByText('Strong')).toBeInTheDocument()
    expect(screen.getByText('Moderate')).toBeInTheDocument()
    expect(screen.getByText(/Goal met/)).toBeInTheDocument()
    // Expand a finding to show its summary + sources.
    await user.click(screen.getByText(/Token bucket is common/))
    await waitFor(() =>
      expect(screen.getByText(/Initial scan of rate-limiting/)).toBeInTheDocument(),
    )
    // View the synthesized report.
    vi.mocked(api.researchReport).mockResolvedValue({ report: '# Research Report\nExecutive summary here.' })
    await user.click(screen.getByRole('button', { name: /View report/i }))
    await waitFor(() => expect(screen.getByText(/Executive summary here/)).toBeInTheDocument())
    // The full goal/question is shown in the detail view (not just the truncated name).
    expect(screen.getByText('How do other teams handle API rate limiting effectively?')).toBeInTheDocument()
  })

  it('detail action buttons call researchAction; nudge sends text', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE])
    vi.mocked(api.researchCampaign).mockResolvedValue(ACTIVE)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('How do other teams handle API rate limiting effectively?')).toBeInTheDocument())
    await user.click(screen.getByText('How do other teams handle API rate limiting effectively?'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Pause/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Pause/i }))
    expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('aaaa1111', 'pause')
    await user.click(screen.getByRole('button', { name: /Nudge/i }))
    const box = await screen.findByPlaceholderText(/Focus on/i)
    await user.type(box, 'Look at GitHub')
    await user.click(screen.getByRole('button', { name: /^Send$/ }))
    await waitFor(() =>
      expect(vi.mocked(api.researchNudge)).toHaveBeenCalledWith('aaaa1111', 'Look at GitHub'),
    )
  })

  it('shows the stagnation banner for a stagnant campaign', async () => {
    const stagnant = { ...ACTIVE, status: 'stagnant' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([stagnant])
    vi.mocked(api.researchCampaign).mockResolvedValue(stagnant)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('How do other teams handle API rate limiting effectively?')).toBeInTheDocument())
    await user.click(screen.getByText('How do other teams handle API rate limiting effectively?'))
    await waitFor(() => expect(screen.getByText(/Research Stalled/i)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Give direction/i })).toBeInTheDocument()
  })

  it('shows the needs-input question card and answers via nudge', async () => {
    const blocked = { ...ACTIVE, status: 'needs_input', pending_question: 'Which database should I assume?' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([blocked])
    vi.mocked(api.researchCampaign).mockResolvedValue(blocked)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('How do other teams handle API rate limiting effectively?')).toBeInTheDocument())
    await user.click(screen.getByText('How do other teams handle API rate limiting effectively?'))
    await waitFor(() => expect(screen.getByText(/Agent needs input/i)).toBeInTheDocument())
    expect(screen.getByText(/Which database should I assume/i)).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText(/Your answer/i), 'Use SQLite')
    await user.click(screen.getByRole('button', { name: /Answer & resume/i }))
    await waitFor(() =>
      expect(vi.mocked(api.researchNudge)).toHaveBeenCalledWith('aaaa1111', 'Use SQLite'),
    )
  })

  // The delete confirmation must be the in-app dialog, never window.confirm:
  // the native confirm is synchronous and freezes the renderer's event loop,
  // so a Quit event queued behind it fires the instant it dismisses — killing
  // the app before the DELETE request is sent (the campaign survives).
  it('clicking Delete opens the in-app confirm dialog and never calls window.confirm', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    const confirmSpy = vi.spyOn(window, 'confirm')
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('A finished question about caching strategies')).toBeInTheDocument())
    await user.click(screen.getByText('A finished question about caching strategies'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Delete/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Delete/i }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Delete campaign?')).toBeInTheDocument()
    expect(within(dialog).getByText(/This cannot be undone/i)).toBeInTheDocument()
    expect(confirmSpy).not.toHaveBeenCalled()
    // Opening the dialog alone must not delete anything.
    expect(vi.mocked(api.researchDelete)).not.toHaveBeenCalled()
  })

  it('confirming the dialog fires the DELETE', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    vi.mocked(api.researchDelete).mockResolvedValue({ deleted: true })
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('A finished question about caching strategies')).toBeInTheDocument())
    await user.click(screen.getByText('A finished question about caching strategies'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Delete/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Delete/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /Delete/i }))
    await waitFor(() => expect(vi.mocked(api.researchDelete)).toHaveBeenCalledWith('bbbb2222'))
  })

  // A failed DELETE must not be silent: the dialog stays open and surfaces the
  // error inline, so the user gets a message and a retry cue instead of a
  // campaign that just looks un-deleted (mirrors SchedulePage's delete dialog).
  it('a failed DELETE keeps the dialog open and shows the error inline', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    vi.mocked(api.researchDelete).mockRejectedValue(new Error('backend unreachable'))
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('A finished question about caching strategies')).toBeInTheDocument())
    await user.click(screen.getByText('A finished question about caching strategies'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Delete/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Delete/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /Delete campaign/i }))
    await waitFor(() => expect(within(dialog).getByText('backend unreachable')).toBeInTheDocument())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    // Retry path stays live: the confirm button is re-enabled after failure.
    expect(within(dialog).getByRole('button', { name: /Delete campaign/i })).toBeEnabled()
  })

  it('disables both buttons and shows Deleting… while the DELETE is in flight', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    // Never-resolving promise pins the mutation in its pending state.
    vi.mocked(api.researchDelete).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.researchDelete>)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('A finished question about caching strategies')).toBeInTheDocument())
    await user.click(screen.getByText('A finished question about caching strategies'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Delete/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Delete/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /Delete campaign/i }))
    await waitFor(() => expect(within(dialog).getByRole('button', { name: /Deleting/i })).toBeDisabled())
    expect(within(dialog).getByRole('button', { name: /Cancel/i })).toBeDisabled()
  })

  it('cancelling the dialog does not delete', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('A finished question about caching strategies')).toBeInTheDocument())
    await user.click(screen.getByText('A finished question about caching strategies'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Delete/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Delete/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(vi.mocked(api.researchDelete)).not.toHaveBeenCalled()
  })

  it('shows a failed banner with the error and resumes the campaign', async () => {
    const failed = { ...DONE, status: 'failed', error_message: 'No activity — research stalled. Resume to continue.' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([failed])
    vi.mocked(api.researchCampaign).mockResolvedValue(failed)
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText('A finished question about caching strategies')).toBeInTheDocument())
    await user.click(screen.getByText('A finished question about caching strategies'))
    await waitFor(() => expect(screen.getByText(/Research stopped/i)).toBeInTheDocument())
    expect(screen.getByText(/research stalled/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Resume/i }))
    await waitFor(() => expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('bbbb2222', 'resume'))
  })

  it('Grill me seeds the question tree with clarifier + research nodes', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([])
    vi.mocked(api.researchGrillExpand).mockResolvedValue({ nodes: [
      { id: 'c1', parent: null, kind: 'clarifier', text: 'Production or exploration?', recommended: 'production', answer: '', origin: '', status: 'open' },
      { id: 'r1', parent: null, kind: 'research', text: 'What durability guarantees exist?', recommended: '', answer: '', origin: 'grill', status: 'open' },
    ] })
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText(/Run autonomous research/i)).toBeInTheDocument())
    await user.click(screen.getAllByRole('button', { name: /New Campaign/i })[0])
    const ta = await screen.findByPlaceholderText(/How do other teams/i)
    await user.type(ta, 'How should we design the caching layer for this service?')
    await user.click(screen.getByRole('button', { name: /Grill me/i }))
    await waitFor(() =>
      expect(vi.mocked(api.researchGrillExpand)).toHaveBeenCalledWith(
        expect.objectContaining({ node_id: null, mode: 'generate' })),
    )
    // Clarifier renders as text; research renders as an editable (included) sub-question.
    expect(await screen.findByText('Production or exploration?')).toBeInTheDocument()
    expect(screen.getByDisplayValue('What durability guarantees exist?')).toBeInTheDocument()
  })

  it('wizard happy path: validates then creates + starts a campaign', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([])
    vi.mocked(api.researchValidate).mockResolvedValue({
      can_start: true, errors: [], warnings: ['Consider decomposing'],
      estimated_cycles: 30, estimated_duration_min: 60,
    })
    vi.mocked(api.researchCreate).mockResolvedValue({ id: 'cccc3333' })
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText(/Run autonomous research/i)).toBeInTheDocument())
    await user.click(screen.getAllByRole('button', { name: /New Campaign/i })[0])

    // Step 0 — question (sub-questions via Grill are optional; manual/empty is allowed)
    const ta = await screen.findByPlaceholderText(/How do other teams/i)
    await user.type(ta, 'How do other teams handle API rate limiting in production?')
    await user.click(screen.getByRole('button', { name: /Next/i }))
    // Step 1 — limits + definition of done
    await user.type(screen.getByPlaceholderText(/AI code review/i), 'Build passes')
    await user.click(screen.getByRole('checkbox'))  // run unattended (skip questions)
    // The idle picker is a themed Radix select, not a native one: a `change` on the
    // trigger does nothing — open it, then click the option. Asserted in the payload
    // below because the control is string-only and the API field is a NUMBER.
    fireEvent.click(screen.getByRole('combobox', { name: 'Idle between cycles' }))
    fireEvent.click(await screen.findByRole('option', { name: '30s' }))
    await user.click(screen.getByRole('button', { name: /Next/i }))
    // Step 2 — review triggers validate()
    await waitFor(() => expect(screen.getByText(/All checks passed/i)).toBeInTheDocument())
    expect(vi.mocked(api.researchValidate)).toHaveBeenCalled()
    expect(screen.getByText(/Done when: Build passes/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Start Campaign/i }))
    await waitFor(() => expect(vi.mocked(api.researchCreate)).toHaveBeenCalled())
    expect(vi.mocked(api.researchCreate).mock.calls[0][0]).toMatchObject({ success_criteria: 'Build passes', auto_approve: true, idle_secs: 30 })
    await waitFor(() =>
      expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('cccc3333', 'start'),
    )
  })

  it('wizard surfaces validation errors and disables Start', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([])
    vi.mocked(api.researchValidate).mockResolvedValue({
      can_start: false, errors: ['Research scope is too broad'], warnings: [],
      estimated_cycles: 30, estimated_duration_min: 60,
    })
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText(/Run autonomous research/i)).toBeInTheDocument())
    await user.click(screen.getAllByRole('button', { name: /New Campaign/i })[0])
    const ta = await screen.findByPlaceholderText(/How do other teams/i)
    await user.type(ta, 'A sufficiently long research question to pass length check')
    await user.click(screen.getByRole('button', { name: /Next/i }))
    await user.click(screen.getByRole('button', { name: /Next/i }))
    await waitFor(() =>
      expect(screen.getByText(/Research scope is too broad/i)).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /Start Campaign/i })).toBeDisabled()
  })
})

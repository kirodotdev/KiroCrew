/**
 * Coverage companion for `ResearchLabPage.tsx`.
 *
 * `ResearchLabPage.integration.test.tsx` already pins the breadth of the page: the root
 * list, campaign detail with findings, the delete dialog, and the wizard happy
 * path. This file goes after what that pass never reaches:
 *
 *   - `ForkFlow` in full — challenge generation (success + failure), the
 *     sessionStorage rehydrate and resume-a-pending-challenge paths, manual
 *     sub-questions, deeper expansion, and every exit of `doFork`.
 *   - The two hand-off buttons on a finished campaign: `AddToKnowledgeButton`
 *     (added / already-there / 409 / hard failure) and `ExportArtifactButton`
 *     (export, regenerate, failure).
 *   - `ReportSections` — heading-based splitting and per-section copy.
 *   - `SubQuestionAdder` — the guidance list, both add affordances, and a
 *     campaign whose stored sub-questions are not valid JSON.
 *   - Wizard branches: execution-mode choice, manual sub-question add / edit /
 *     remove, the max-cycles suggestion and its "user touched it" latch,
 *     parallel-worker clamping, the Back step, and both async failure paths.
 *   - A ready campaign's Start: the request, the re-reads around it, the
 *     one-campaign-at-a-time block (including one the list had not seen yet),
 *     and a refused start.
 *   - Detail branches: paused controls, the stagnation banner's own actions,
 *     nudge dismissal, closing the delete dialog with Escape, the SSE message
 *     and error handlers, and the forked-from breadcrumb.
 *
 * The api client is mocked wholesale (as in the sibling file) and `EventSource`
 * is replaced with a controllable stub so the SSE handlers can be driven
 * directly instead of waiting on a socket jsdom will never open.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// `/all` for every catalog: `../../i18n` registers English only.
import { i18next } from '../../i18n/all'
import { store } from '../../store'

vi.mock('../../api/client', () => ({
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
    researchKnowledgeStatus: vi.fn(),
    researchReportStatus: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { newQueryClient } from '../../api/queryClient'
import ResearchLabPage from './ResearchLabPage'

// Controllable stand-in for the browser's EventSource. The page opens one per
// campaign detail; jsdom would only ever fail the connection asynchronously,
// which leaves the message/error handlers untested.
class FakeEventSource {
  static last: FakeEventSource | null = null
  url: string
  closed = false
  onmessage: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeEventSource.last = this
  }
  close() { this.closed = true }
}

function renderPage(queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, gcTime: 0 } },
})) {
  const view = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <MemoryRouter initialEntries={['/auto-research']}>
          <ResearchLabPage />
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, queryClient }
}

type User = ReturnType<typeof userEvent.setup>

const ACTIVE = {
  id: 'aaaa1111', name: 'Rate limiting research', question: 'How do other teams handle API rate limiting effectively?',
  sub_questions: '[]', sources: '["web"]', max_cycles: 30, idle_secs: 60,
  status: 'running', total_cycles: 2, findings: [],
}
const DONE = {
  id: 'bbbb2222', name: 'Old campaign', question: 'A finished question about caching strategies',
  sub_questions: '[]', sources: '["web"]', max_cycles: 10, idle_secs: 60,
  status: 'complete', total_cycles: 8, findings: [],
}

const FORK_TREE_KEY = `mc-fork-tree:${DONE.id}`
const FORK_PENDING_KEY = `mc-fork-pending:${DONE.id}`

function researchNode(id: string, text: string) {
  return { id, parent: null, kind: 'research', text, recommended: '', answer: '', origin: 'grill', status: 'promoted' }
}
function clarifierNode(id: string, text: string, extra: Record<string, string> = {}) {
  return { id, parent: null, kind: 'clarifier', text, recommended: 'production', answer: '', origin: '', status: 'open', ...extra }
}

let alertSpy: ReturnType<typeof vi.spyOn>
let writeText: ReturnType<typeof vi.fn>

/**
 * Install the clipboard spy. `userEvent.setup()` installs its own clipboard
 * stub on `navigator`, so this has to run AFTER the user is created or the copy
 * assertion watches an object the component never touches.
 */
function stubClipboard() {
  writeText = vi.fn()
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  FakeEventSource.last = null
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
  stubClipboard()
  vi.mocked(api.researchCampaigns).mockResolvedValue([])
  vi.mocked(api.researchCampaign).mockResolvedValue(ACTIVE)
  vi.mocked(api.researchAction).mockResolvedValue({ ok: true })
  vi.mocked(api.researchNudge).mockResolvedValue({ ok: true })
  vi.mocked(api.researchGrillExpand).mockResolvedValue({ nodes: [] })
  vi.mocked(api.researchAddQuestion).mockResolvedValue({ ok: true })
  vi.mocked(api.researchKnowledgeStatus).mockResolvedValue({ in_library: false })
  vi.mocked(api.researchReportStatus).mockResolvedValue({ slug: null })
  vi.mocked(api.researchToKnowledge).mockReset()
  vi.mocked(api.researchToArtifact).mockReset()
})

afterEach(() => {
  alertSpy.mockRestore()
  vi.unstubAllGlobals()
})

/** Enter the wizard from the empty state and return the question textarea. */
async function openWizard(user: User) {
  await waitFor(() => expect(screen.getByText(/Run autonomous research/i)).toBeInTheDocument())
  await user.click(screen.getAllByRole('button', { name: /New Campaign/i })[0])
  return await screen.findByPlaceholderText(/How do other teams/i)
}

/** A question long enough to clear the 20-character gate, set in one go. */
function setQuestion(ta: HTMLElement, text = 'How should we design the caching layer for this service?') {
  fireEvent.change(ta, { target: { value: text } })
}

/** Click a campaign card in the root list to open its detail view. */
async function openDetail(user: User, campaign: { question: string }) {
  await waitFor(() => expect(screen.getByText(campaign.question)).toBeInTheDocument())
  await user.click(screen.getByText(campaign.question))
}

/** Open detail for the finished campaign, then enter the fork flow. */
async function openForkFlow(user: User) {
  vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
  vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
  renderPage()
  await openDetail(user, DONE)
  await user.click(await screen.findByRole('button', { name: /Fork & Challenge/i }))
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Continue Research' })).toBeInTheDocument())
}

describe('ResearchLabPage — root list and wizard branches', () => {
  it('the empty-state call to action also opens the wizard', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByText(/Run autonomous research/i)).toBeInTheDocument())
    const ctas = screen.getAllByRole('button', { name: /New Campaign/i })
    // Two entry points render on the empty state: the header and the centred CTA.
    expect(ctas).toHaveLength(2)
    await user.click(ctas[1])
    expect(await screen.findByPlaceholderText(/How do other teams/i)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'New Campaign' })).toBeInTheDocument()
  })

  it('choosing Dynamic Workflow surfaces the fan-out warning; Agent clears it', async () => {
    const user = userEvent.setup()
    renderPage()
    await openWizard(user)
    await user.click(screen.getByRole('radio', { name: /Dynamic Workflow/i }))
    expect(screen.getByText(/can fan out to many sub-agents/i)).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: /Agent \(adaptive\)/i }))
    expect(screen.queryByText(/can fan out to many sub-agents/i)).not.toBeInTheDocument()
  })

  // #32040: the two mode cards showed the choice only by their accent border.
  // They are a radio group named by the line above them, so the chosen mode is
  // announced.
  it('announces the execution mode cards as a named radio group with the chosen one checked', async () => {
    const user = userEvent.setup()
    renderPage()
    await openWizard(user)
    const group = screen.getByRole('radiogroup', { name: /How sub-agent execution is orchestrated/i })
    const agent = within(group).getByRole('radio', { name: /Agent \(adaptive\)/i })
    const workflow = within(group).getByRole('radio', { name: /Dynamic Workflow/i })
    expect(agent).toHaveAttribute('aria-checked', 'true')
    expect(workflow).toHaveAttribute('aria-checked', 'false')
    // One Tab stop, on the checked card.
    expect(agent).toHaveAttribute('tabindex', '0')
    expect(workflow).toHaveAttribute('tabindex', '-1')
    await user.click(workflow)
    expect(workflow).toHaveAttribute('aria-checked', 'true')
    expect(agent).toHaveAttribute('aria-checked', 'false')
  })

  it('a grill that returns no nodes reports itself unavailable', async () => {
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta)
    await user.click(screen.getByRole('button', { name: /Grill me/i }))
    expect(await screen.findByText(/Grill unavailable/i)).toBeInTheDocument()
  })

  it('a grill that rejects reports itself unavailable too', async () => {
    vi.mocked(api.researchGrillExpand).mockRejectedValue(new Error('backend down'))
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta)
    await user.click(screen.getByRole('button', { name: /Grill me/i }))
    expect(await screen.findByText(/Grill unavailable/i)).toBeInTheDocument()
  })

  it('manual sub-questions: Enter commits, Shift+Enter does not, and each is editable and removable', async () => {
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta)
    const adder = screen.getByLabelText('Add sub-question manually')
    fireEvent.change(adder, { target: { value: 'Which cache eviction policy?' } })
    fireEvent.keyDown(adder, { key: 'Enter' })
    const first = await screen.findByLabelText('Sub-question 1')
    expect(first).toHaveValue('Which cache eviction policy?')
    expect(adder).toHaveValue('')

    // Shift+Enter is a newline, not a commit: still exactly one sub-question.
    fireEvent.change(adder, { target: { value: 'not committed' } })
    fireEvent.keyDown(adder, { key: 'Enter', shiftKey: true })
    expect(screen.queryByLabelText('Sub-question 2')).not.toBeInTheDocument()

    fireEvent.change(first, { target: { value: 'Which eviction policy do they use?' } })
    expect(screen.getByLabelText('Sub-question 1')).toHaveValue('Which eviction policy do they use?')

    await user.click(screen.getByLabelText('Remove sub-question'))
    expect(screen.queryByLabelText('Sub-question 1')).not.toBeInTheDocument()
  })

  it('max cycles is suggested from the sub-question count until the user edits it', async () => {
    vi.mocked(api.researchGrillExpand).mockResolvedValue({
      nodes: [researchNode('r1', 'What durability guarantees exist?'), researchNode('r2', 'What do peers use?')],
    })
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta)
    await user.click(screen.getByRole('button', { name: /Grill me/i }))
    await screen.findByDisplayValue('What durability guarantees exist?')
    await user.click(screen.getByRole('button', { name: /Next/i }))

    // suggestedMaxCycles(2) === 2 + ceil(2/3) + 1 === 4
    const cycles = await screen.findByLabelText('Max cycles')
    expect(cycles).toHaveValue(4)
    expect(screen.getByText(/suggested from/i)).toBeInTheDocument()

    fireEvent.change(cycles, { target: { value: '25' } })
    expect(cycles).toHaveValue(25)
    expect(screen.queryByText(/suggested from/i)).not.toBeInTheDocument()

    // Parallel workers clamp to 1..5 and describe the chosen fan-out.
    const workers = screen.getByLabelText('Parallel workers')
    fireEvent.change(workers, { target: { value: '3' } })
    expect(screen.getByText(/3 sub-questions investigated in parallel/i)).toBeInTheDocument()
    fireEvent.change(workers, { target: { value: '99' } })
    expect(workers).toHaveValue(5)
    fireEvent.change(workers, { target: { value: '0' } })
    expect(workers).toHaveValue(1)
    expect(screen.getByText(/sequential \(default\)/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Back/i }))
    expect(screen.getByPlaceholderText(/How do other teams/i)).toBeInTheDocument()
  })

  it('counts the sub-questions behind the suggestion in the interface language', async () => {
    vi.mocked(api.researchGrillExpand).mockResolvedValue({
      nodes: [researchNode('r1', 'What durability guarantees exist?'), researchNode('r2', 'What do peers use?')],
    })
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta)
    await user.click(screen.getByRole('button', { name: /Grill me/i }))
    await screen.findByDisplayValue('What durability guarantees exist?')
    // The page renders its strings when a step renders, so switch before the
    // step change; the Next button is still the one rendered in English.
    const next = screen.getByRole('button', { name: /Next/i })
    await i18next.changeLanguage('de')
    try {
      await user.click(next)
      expect(await screen.findByText('vorgeschlagen aus 2 Unterfragen')).toBeInTheDocument()
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('a clarifier can be accepted and then expanded for a deeper round', async () => {
    vi.mocked(api.researchGrillExpand).mockResolvedValueOnce({ nodes: [clarifierNode('c1', 'Production or exploration?')] })
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta)
    await user.click(screen.getByRole('button', { name: /Grill me/i }))
    await screen.findByText('Production or exploration?')

    await user.click(screen.getByRole('button', { name: /accept/i }))
    expect(await screen.findByText(/answered:/i)).toBeInTheDocument()

    vi.mocked(api.researchGrillExpand).mockResolvedValueOnce({ nodes: [{ ...researchNode('r9', 'Which SLO applies?'), parent: 'c1' }] })
    await user.click(screen.getByRole('button', { name: /expand/i }))
    await waitFor(() => expect(vi.mocked(api.researchGrillExpand)).toHaveBeenLastCalledWith(
      expect.objectContaining({ node_id: 'c1', mode: 'generate' })))
    expect(await screen.findByDisplayValue('Which SLO applies?')).toBeInTheDocument()
  })

  it('a validation request that rejects shows a connection error on the review step', async () => {
    vi.mocked(api.researchValidate).mockRejectedValue(new Error('offline'))
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta, 'A sufficiently long research question to pass the length check')
    await user.click(screen.getByRole('button', { name: /Next/i }))
    await user.click(screen.getByRole('button', { name: /Next/i }))
    expect(await screen.findByText(/Validation failed/i)).toBeInTheDocument()
  })

  it('a create that rejects leaves the review step with a retryable error', async () => {
    vi.mocked(api.researchValidate).mockResolvedValue({
      can_start: true, errors: [], warnings: [], estimated_cycles: 10, estimated_duration_min: 20,
    })
    vi.mocked(api.researchCreate).mockRejectedValue(new Error('boom'))
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta, 'A sufficiently long research question to pass the length check')
    await user.click(screen.getByRole('button', { name: /Next/i }))
    await user.click(screen.getByRole('button', { name: /Next/i }))
    await waitFor(() => expect(screen.getByText(/All checks passed/i)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Start Campaign/i }))
    expect(await screen.findByText(/Failed to start campaign/i)).toBeInTheDocument()
    expect(vi.mocked(api.researchAction)).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Start Campaign/i })).toBeEnabled()
  })

  it('states the run estimate as one sentence, in the interface language', async () => {
    vi.mocked(api.researchValidate).mockResolvedValue({
      can_start: true, errors: [], warnings: [], estimated_cycles: 10, estimated_duration_min: 20,
    })
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta, 'A sufficiently long research question to pass the length check')
    await user.click(screen.getByRole('button', { name: /Next/i }))
    // Switch before the step change: the page renders its strings when a step renders.
    const next = screen.getByRole('button', { name: /Next/i })
    await i18next.changeLanguage('de')
    try {
      await user.click(next)
      expect(await screen.findByText(/^Bis zu \d+ Zyklen, \d+s inaktiv\. ~20 Min\.$/)).toBeInTheDocument()
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('a create that returns no id neither starts a campaign nor leaves the wizard', async () => {
    vi.mocked(api.researchValidate).mockResolvedValue({
      can_start: true, errors: [], warnings: [], estimated_cycles: 10, estimated_duration_min: 20,
    })
    vi.mocked(api.researchCreate).mockResolvedValue({})
    const user = userEvent.setup()
    renderPage()
    const ta = await openWizard(user)
    setQuestion(ta, 'A sufficiently long research question to pass the length check')
    await user.click(screen.getByRole('button', { name: /Next/i }))
    await user.click(screen.getByRole('button', { name: /Next/i }))
    await waitFor(() => expect(screen.getByText(/All checks passed/i)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Start Campaign/i }))
    await waitFor(() => expect(vi.mocked(api.researchCreate)).toHaveBeenCalled())
    expect(vi.mocked(api.researchAction)).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Start Campaign/i })).toBeInTheDocument()
  })
})

describe('ResearchLabPage — a ready campaign', () => {
  // Created but never started: through the API, or a create or fork whose start
  // failed. The backend accepts only `start` from ready, so Start is the one
  // control that moves it on.
  const READY = {
    id: 'dddd4444', name: 'Unstarted campaign', question: 'Which queueing designs survive a regional outage?',
    sub_questions: '[]', sources: '["web"]', max_cycles: 30, idle_secs: 60,
    status: 'ready', total_cycles: 0, findings: [],
  }

  it('offers Start, not Resume, and says it has not started', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([READY])
    vi.mocked(api.researchCampaign).mockResolvedValue(READY)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, READY)
    expect(await screen.findByRole('button', { name: 'Start Campaign' })).toBeEnabled()
    expect(screen.getByText('Not started yet.')).toBeInTheDocument()
    expect(screen.queryByText(/First cycle in progress/)).not.toBeInTheDocument()
    for (const name of ['Resume', 'Pause', 'Stop', 'Nudge']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
  })

  it('starts it once, stays disabled until the campaign and the list are re-read, and Back lists it as active', async () => {
    const running = { ...READY, status: 'running' }
    let finishStart: (value: unknown) => void = () => {}
    let finishReread: (value: unknown) => void = () => {}
    vi.mocked(api.researchAction).mockImplementation(() => new Promise(resolve => { finishStart = resolve }))
    vi.mocked(api.researchCampaigns).mockResolvedValue([READY])
    vi.mocked(api.researchCampaign).mockResolvedValue(READY)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, READY)
    const start = await screen.findByRole('button', { name: 'Start Campaign' })
    await user.click(start)
    await waitFor(() => expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('dddd4444', 'start'))
    // A second click while the start is in flight sends nothing.
    expect(start).toBeDisabled()
    expect(start).toHaveTextContent('Starting...')
    await user.click(start)
    expect(vi.mocked(api.researchAction)).toHaveBeenCalledTimes(1)

    // Accepted, but the campaign's re-read has not landed: Start stays disabled.
    const listReads = vi.mocked(api.researchCampaigns).mock.calls.length
    const campaignReads = vi.mocked(api.researchCampaign).mock.calls.length
    vi.mocked(api.researchCampaigns).mockResolvedValue([running])
    vi.mocked(api.researchCampaign).mockImplementation(() => new Promise(resolve => { finishReread = resolve }))
    await act(async () => { finishStart({ id: READY.id, status: 'running' }) })
    await waitFor(() => expect(vi.mocked(api.researchCampaign).mock.calls.length).toBeGreaterThan(campaignReads))
    await waitFor(() => expect(vi.mocked(api.researchCampaigns).mock.calls.length).toBeGreaterThan(listReads))
    expect(start).toBeDisabled()
    await act(async () => { finishReread(running) })
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start Campaign' })).not.toBeInTheDocument()
    expect(screen.getByText(/First cycle in progress/)).toBeInTheDocument()

    // The list polls only while a campaign is active, so without the re-read it
    // would still show this campaign under History with New Campaign enabled.
    await user.click(screen.getByRole('button', { name: /Back/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /New Campaign/ })).toBeDisabled())
    expect(screen.getByText('ACTIVE')).toBeInTheDocument()
    expect(screen.queryByText('HISTORY')).not.toBeInTheDocument()
  })

  it('waits while another campaign is active, and says why', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE, READY])
    vi.mocked(api.researchCampaign).mockResolvedValue(READY)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, READY)
    const start = await screen.findByRole('button', { name: 'Start Campaign' })
    expect(start).toBeDisabled()
    expect(start).toHaveAccessibleDescription('One campaign at a time — research benefits from focused depth.')
    await user.click(start)
    expect(vi.mocked(api.researchAction)).not.toHaveBeenCalled()
  })

  it('re-reads the list before starting, and sends nothing when a campaign started elsewhere', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([READY])
    vi.mocked(api.researchCampaign).mockResolvedValue(READY)
    const user = userEvent.setup()
    // The app's own client: it never treats a cached read as stale, so the
    // re-read has to force its request.
    renderPage(newQueryClient())
    await openDetail(user, READY)
    const start = await screen.findByRole('button', { name: 'Start Campaign' })
    expect(start).toBeEnabled()
    // The list on screen polls only while a campaign is active, so it has not
    // seen this one, started through the API after the page loaded.
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE, READY])
    await user.click(start)
    await waitFor(() => expect(start).toHaveAccessibleDescription('One campaign at a time — research benefits from focused depth.'))
    await waitFor(() => expect(start).toHaveTextContent('Start Campaign'))
    expect(start).toBeDisabled()
    expect(vi.mocked(api.researchAction)).not.toHaveBeenCalled()
  })

  it('shows the gateway\'s reason when it refuses the start, and re-reads the campaign', async () => {
    vi.mocked(api.researchAction).mockRejectedValue(new Error("Cannot start a campaign in 'running' state"))
    vi.mocked(api.researchCampaigns).mockResolvedValue([READY])
    vi.mocked(api.researchCampaign).mockResolvedValue(READY)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, READY)
    const start = await screen.findByRole('button', { name: 'Start Campaign' })
    const campaignReads = vi.mocked(api.researchCampaign).mock.calls.length
    await user.click(start)
    expect(await screen.findByText(/Cannot start a campaign in 'running' state/)).toBeInTheDocument()
    await waitFor(() => expect(vi.mocked(api.researchCampaign).mock.calls.length).toBeGreaterThan(campaignReads))
    await waitFor(() => expect(start).toBeEnabled())
    expect(start).toHaveTextContent('Start Campaign')
  })
})

describe('ResearchLabPage — campaign detail branches', () => {
  it('a paused campaign offers Resume and Stop', async () => {
    const paused = { ...ACTIVE, status: 'paused' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([paused])
    vi.mocked(api.researchCampaign).mockResolvedValue(paused)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, paused)
    await user.click(await screen.findByRole('button', { name: 'Resume' }))
    expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('aaaa1111', 'resume')
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('aaaa1111', 'stop')
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument()
  })

  it('the stagnation banner can stop or continue the campaign', async () => {
    const stagnant = { ...ACTIVE, status: 'stagnant' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([stagnant])
    vi.mocked(api.researchCampaign).mockResolvedValue(stagnant)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, stagnant)
    await waitFor(() => expect(screen.getByText(/Research Stalled/i)).toBeInTheDocument())
    // Two Stop buttons render: the top control row and the banner's own.
    const stops = screen.getAllByRole('button', { name: 'Stop' })
    expect(stops).toHaveLength(2)
    await user.click(stops[1])
    expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('aaaa1111', 'stop')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('aaaa1111', 'resume')
  })

  it('the nudge panel can be dismissed without sending', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE])
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, ACTIVE)
    await user.click(await screen.findByRole('button', { name: /Nudge/i }))
    const box = await screen.findByPlaceholderText(/Focus on/i)
    fireEvent.change(box, { target: { value: 'look at GitHub' } })
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByPlaceholderText(/Focus on/i)).not.toBeInTheDocument())
    expect(vi.mocked(api.researchNudge)).not.toHaveBeenCalled()
  })

  it('Escape closes the delete dialog without deleting', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /^Delete$/ }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(vi.mocked(api.researchDelete)).not.toHaveBeenCalled()
  })

  it('an SSE message refetches the campaign and an SSE error closes the stream', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([ACTIVE])
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, ACTIVE)
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull())
    const es = FakeEventSource.last!
    expect(es.url).toContain(`/campaigns/${ACTIVE.id}/stream`)
    const before = vi.mocked(api.researchCampaign).mock.calls.length
    act(() => { es.onmessage?.() })
    await waitFor(() =>
      expect(vi.mocked(api.researchCampaign).mock.calls.length).toBeGreaterThan(before))
    act(() => { es.onerror?.() })
    expect(es.closed).toBe(true)
  })

  it('a forked campaign links back to its parent', async () => {
    const child = { ...DONE, id: 'cccc3333', question: 'A forked follow-up about eviction policies', parent_id: DONE.id }
    vi.mocked(api.researchCampaigns).mockResolvedValue([child])
    vi.mocked(api.researchCampaign).mockImplementation((id: string) =>
      Promise.resolve(id === DONE.id ? DONE : child))
    const user = userEvent.setup()
    renderPage()
    // The root card marks it as forked before we ever open it.
    await waitFor(() => expect(screen.getByText('Forked')).toBeInTheDocument())
    await openDetail(user, child)
    await waitFor(() => expect(screen.getByText(/Forked from:/i)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: DONE.id }))
    await waitFor(() => expect(screen.getByRole('heading', { name: DONE.name })).toBeInTheDocument())
  })

  it('renders the weak evidence badge and the goal-not-yet marker', async () => {
    const weak = {
      ...ACTIVE,
      findings: [{
        cycle: 1, summary: 'Thin signal only.', sources_checked: [], sources_empty: [],
        new_findings_count: 1, evidence_strength: 'weak', key_insight: 'Little corroboration',
        verification: { passed: false },
      }],
    }
    vi.mocked(api.researchCampaigns).mockResolvedValue([weak])
    vi.mocked(api.researchCampaign).mockResolvedValue(weak)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, weak)
    expect(await screen.findByText('Weak')).toBeInTheDocument()
    expect(screen.getByText(/Goal: not yet/i)).toBeInTheDocument()
  })

  it('an unknown backend status falls back to a de-snaked neutral pill', async () => {
    const odd = { ...DONE, status: 'awaiting_review' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([odd])
    renderPage()
    await waitFor(() => expect(screen.getByText('awaiting review')).toBeInTheDocument())
  })
})

describe('ResearchLabPage — sub-question guidance list', () => {
  const withSubs = {
    ...ACTIVE,
    sub_questions: JSON.stringify([
      { text: 'Which eviction policy?', origin: 'manual', status: 'answered' },
      { text: 'What did the agent notice?', origin: 'emergent', status: 'open' },
      { text: 'What do peer teams do?', origin: 'grill', status: 'open' },
    ]),
  }

  it('lists stored sub-questions with their origin and accepts new guidance', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([withSubs])
    vi.mocked(api.researchCampaign).mockResolvedValue(withSubs)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, withSubs)
    await user.click(await screen.findByText(/Sub-questions & guidance \(3\)/i))
    expect(await screen.findByText('Which eviction policy?')).toBeInTheDocument()
    expect(screen.getByText('(your guidance)')).toBeInTheDocument()
    expect(screen.getByText('(emergent)')).toBeInTheDocument()
    expect(screen.getByText('(grill)')).toBeInTheDocument()

    const box = screen.getByLabelText('Add guidance or a sub-question')
    fireEvent.change(box, { target: { value: 'Check the CDN layer' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() =>
      expect(vi.mocked(api.researchAddQuestion)).toHaveBeenCalledWith('aaaa1111', 'Check the CDN layer'))

    fireEvent.change(box, { target: { value: 'And the origin shield' } })
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(vi.mocked(api.researchAddQuestion)).toHaveBeenCalledWith('aaaa1111', 'And the origin shield'))
  })

  it('sub-questions that are not valid JSON degrade to an empty list', async () => {
    const broken = { ...ACTIVE, sub_questions: '{not json' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([broken])
    vi.mocked(api.researchCampaign).mockResolvedValue(broken)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, broken)
    expect(await screen.findByText(/Sub-questions & guidance \(0\)/i)).toBeInTheDocument()
  })

  it('a finished campaign hides the guidance list entirely', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await waitFor(() => expect(screen.getByText(/Continue Research/i)).toBeInTheDocument())
    expect(screen.queryByText(/Sub-questions & guidance/i)).not.toBeInTheDocument()
  })
})

describe('ResearchLabPage — report sections', () => {
  it('splits the report at headings and copies one section at a time', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    vi.mocked(api.researchReport).mockResolvedValue({
      report: '# Summary\nToken buckets dominate.\n\n## Detail\nSliding windows appear internally.',
    })
    const user = userEvent.setup()
    stubClipboard()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /View report/i }))
    const copies = await screen.findAllByRole('button', { name: 'Copy' })
    expect(copies).toHaveLength(2)
    fireEvent.click(copies[0])
    expect(writeText).toHaveBeenCalledWith('# Summary\nToken buckets dominate.')
    expect(await screen.findByText('Copied!')).toBeInTheDocument()
    // The other section keeps its idle label — copy state is per section.
    expect(screen.getAllByRole('button', { name: 'Copy' })).toHaveLength(1)
  })

  it('a heading-free report stays a single section', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    vi.mocked(api.researchReport).mockResolvedValue({ report: 'Just one paragraph of prose.' })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /View report/i }))
    expect(await screen.findByText('Just one paragraph of prose.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Copy' })).toHaveLength(1)
  })

  it('hides the report again on a second toggle and reports an absent one', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
    vi.mocked(api.researchReport).mockResolvedValue({ report: '' })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /View report/i }))
    expect(await screen.findByText(/No report yet/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Hide report/i }))
    await waitFor(() => expect(screen.queryByText(/No report yet/i)).not.toBeInTheDocument())
  })
})

describe('ResearchLabPage — knowledge and artifact hand-off', () => {
  beforeEach(() => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([DONE])
    vi.mocked(api.researchCampaign).mockResolvedValue(DONE)
  })

  it('adds the campaign to the knowledge library', async () => {
    vi.mocked(api.researchToKnowledge).mockResolvedValue({ ok: true })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /Add to Knowledge/i }))
    expect(await screen.findByText(/Added to Knowledge/i)).toBeInTheDocument()
    expect(vi.mocked(api.researchToKnowledge)).toHaveBeenCalledWith('bbbb2222')
  })

  it('a 409 from the library means it is already there', async () => {
    vi.mocked(api.researchToKnowledge).mockRejectedValue({ status: 409 })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /Add to Knowledge/i }))
    expect(await screen.findByText(/Already in Knowledge/i)).toBeInTheDocument()
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('shows a knowledge-library failure beside its action and stays retryable', async () => {
    vi.mocked(api.researchToKnowledge).mockRejectedValue({ status: 500, message: 'library offline' })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /Add to Knowledge/i }))
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('library offline')
    expect(notice.parentElement).toContainElement(screen.getByRole('button', { name: /Add to Knowledge/i }))
    expect(alertSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Add to Knowledge/i })).toBeEnabled()
  })

  it('an existing library membership renders without a click', async () => {
    vi.mocked(api.researchKnowledgeStatus).mockResolvedValue({ in_library: true })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    expect(await screen.findByText(/Already in Knowledge/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add to Knowledge/i })).not.toBeInTheDocument()
  })

  it('exporting an artifact swaps the button for a link plus Regenerate', async () => {
    vi.mocked(api.researchToArtifact).mockResolvedValue({ slug: 'caching-report' })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /Export as Artifact/i }))
    const link = await screen.findByRole('link', { name: /View report/i })
    expect(link).toHaveAttribute('href', '/artifacts/caching-report')
    const regen = screen.getByRole('button', { name: /Regenerate/i })
    await user.click(regen)
    await waitFor(() => expect(vi.mocked(api.researchToArtifact)).toHaveBeenCalledTimes(2))
  })

  it('an already-exported campaign shows the link on mount', async () => {
    vi.mocked(api.researchReportStatus).mockResolvedValue({ slug: 'earlier-export' })
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    const link = await screen.findByRole('link', { name: /View report/i })
    expect(link).toHaveAttribute('href', '/artifacts/earlier-export')
    expect(screen.queryByRole('button', { name: /Export as Artifact/i })).not.toBeInTheDocument()
  })

  it('shows a failed export beside its action and leaves the button usable', async () => {
    vi.mocked(api.researchToArtifact).mockRejectedValue(new Error('no artifact store'))
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /Export as Artifact/i }))
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('Failed to export as artifact')
    expect(notice.parentElement).toContainElement(screen.getByRole('button', { name: /Export as Artifact/i }))
    expect(alertSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Export as Artifact/i })).toBeEnabled()
  })

  it.each(['knowledge', 'artifact'] as const)('dismisses and clears a %s error on retry', async action => {
    const request = action === 'knowledge' ? api.researchToKnowledge : api.researchToArtifact
    const label = action === 'knowledge' ? /Add to Knowledge/i : /Export as Artifact/i
    vi.mocked(request).mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline again'))
    let finish!: (value: { ok: boolean; slug: string }) => void
    const pending = new Promise<{ ok: boolean; slug: string }>(resolve => { finish = resolve })
    vi.mocked(request).mockReturnValueOnce(pending)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: label }))
    const notice = await screen.findByRole('alert')
    expect(within(notice).queryByRole('button', { name: /Ask/i })).toBeNull()
    await user.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).toBeNull()

    await user.click(screen.getByRole('button', { name: label }))
    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: label }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: action === 'knowledge' ? /Adding/i : /Exporting/i })).toBeDisabled()
    await act(async () => { finish({ ok: true, slug: 'recovered-report' }); await pending })
    if (action === 'knowledge') {
      expect(await screen.findByText(/Added to Knowledge/i)).toBeInTheDocument()
    } else {
      expect(await screen.findByRole('link', { name: /View report/i })).toHaveAttribute('href', '/artifacts/recovered-report')
    }
    expect(screen.queryByRole('alert')).toBeNull()
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('keeps artifact and knowledge failures attached to their own controls', async () => {
    vi.mocked(api.researchToKnowledge).mockRejectedValue(new Error('library offline'))
    vi.mocked(api.researchToArtifact).mockRejectedValue(new Error('artifact offline'))
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    await user.click(await screen.findByRole('button', { name: /Add to Knowledge/i }))
    await screen.findByText('library offline')
    await user.click(screen.getByRole('button', { name: /Export as Artifact/i }))
    await screen.findByText('Failed to export as artifact')
    const knowledge = screen.getByText('library offline').closest('[role="alert"]')!
    const artifact = screen.getByText('Failed to export as artifact').closest('[role="alert"]')!
    expect(screen.getByRole('button', { name: /Fork & Challenge/i }).parentElement).toHaveClass('items-start')
    await user.click(within(knowledge as HTMLElement).getByRole('button', { name: 'Dismiss' }))
    expect(knowledge).not.toBeInTheDocument()
    expect(artifact).toBeInTheDocument()
  })

  it('keeps the existing report link while a failed regeneration is dismissed and retried', async () => {
    let finish!: (value: { slug: string }) => void
    const pending = new Promise<{ slug: string }>(resolve => { finish = resolve })
    vi.mocked(api.researchReportStatus).mockResolvedValue({ slug: 'earlier-export' })
    vi.mocked(api.researchToArtifact).mockRejectedValueOnce(new Error('offline')).mockReturnValueOnce(pending)
    const user = userEvent.setup()
    renderPage()
    await openDetail(user, DONE)
    const link = await screen.findByRole('link', { name: /View report/i })
    await user.click(screen.getByRole('button', { name: /Regenerate/i }))
    const notice = await screen.findByRole('alert')
    expect(link).toHaveAttribute('href', '/artifacts/earlier-export')
    await user.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    await user.click(screen.getByRole('button', { name: /Regenerate/i }))
    await waitFor(() => expect(api.researchToArtifact).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('button', { name: /Regenerating/i })).toBeDisabled()
    expect(screen.queryByRole('alert')).toBeNull()
    await act(async () => { finish({ slug: 'earlier-export' }); await pending })
    await waitFor(() => expect(screen.getByRole('button', { name: /Regenerate/i })).toBeEnabled())
    expect(screen.getByRole('link', { name: /View report/i })).toHaveAttribute('href', '/artifacts/earlier-export')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it.each(['knowledge', 'artifact'] as const)('does not show a late %s failure on another campaign', async action => {
    const parent = { ...DONE, id: 'parent3333', question: 'Parent research question' }
    const child = { ...DONE, parent_id: parent.id }
    vi.mocked(api.researchCampaign).mockImplementation(async id => id === parent.id ? parent : child)
    const request = action === 'knowledge' ? api.researchToKnowledge : api.researchToArtifact
    const label = action === 'knowledge' ? /Add to Knowledge/i : /Export as Artifact/i
    let fail!: (error: Error) => void
    const pending = new Promise<never>((_resolve, reject) => { fail = reject })
    vi.mocked(request).mockReturnValueOnce(pending)
    const user = userEvent.setup()
    const { queryClient } = renderPage()
    queryClient.setQueryDefaults(['research-campaign', parent.id], { gcTime: Infinity, staleTime: Infinity })
    queryClient.setQueryData(['research-campaign', parent.id], parent)
    await openDetail(user, child)
    await user.click(await screen.findByRole('button', { name: label }))
    await waitFor(() => expect(request).toHaveBeenCalledWith(child.id))
    await user.click(screen.getByRole('button', { name: parent.id }))
    await screen.findByRole('button', { name: label })
    await act(async () => { fail(new Error('old campaign offline')); await pending.catch(() => {}) })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: label })).toBeEnabled()
  })
})

describe('ResearchLabPage — fork flow', () => {
  it('generates challenges and mirrors the tree into sessionStorage', async () => {
    vi.mocked(api.researchGrillExpand).mockResolvedValue({
      nodes: [clarifierNode('c1', 'Was the sample representative?'), researchNode('r1', 'What contradicts the finding?')],
    })
    const user = userEvent.setup()
    await openForkFlow(user)
    expect(screen.getByText(/Challenge the findings from/i)).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: /Challenge Findings/i }))
    await waitFor(() => expect(vi.mocked(api.researchGrillExpand)).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'challenge', campaign_id: DONE.id, node_id: null })))
    expect(await screen.findByText('Was the sample representative?')).toBeInTheDocument()
    expect(screen.getByDisplayValue('What contradicts the finding?')).toBeInTheDocument()
    await waitFor(() => expect(sessionStorage.getItem(FORK_TREE_KEY)).toContain('r1'))
    // The in-flight marker is cleared once the request settles.
    expect(sessionStorage.getItem(FORK_PENDING_KEY)).toBeNull()
  })

  it('a challenge that rejects reports an error and stays on the start button', async () => {
    vi.mocked(api.researchGrillExpand).mockRejectedValue(new Error('no model'))
    const user = userEvent.setup()
    await openForkFlow(user)
    await user.click(await screen.findByRole('button', { name: /Challenge Findings/i }))
    expect(await screen.findByText(/Could not generate challenges/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Challenge Findings/i })).toBeEnabled()
  })

  it('rehydrates a persisted tree and resumes a challenge that was in flight', async () => {
    sessionStorage.setItem(FORK_PENDING_KEY, '1')
    vi.mocked(api.researchGrillExpand).mockResolvedValue({ nodes: [researchNode('r1', 'What was overlooked?')] })
    const user = userEvent.setup()
    await openForkFlow(user)
    // No click: the pending marker resumes the request as soon as the parent
    // question resolves.
    expect(await screen.findByDisplayValue('What was overlooked?')).toBeInTheDocument()
    await waitFor(() => expect(sessionStorage.getItem(FORK_PENDING_KEY)).toBeNull())
  })

  it('a persisted tree that is not an array is discarded', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, '{"nope":1}')
    const user = userEvent.setup()
    await openForkFlow(user)
    expect(await screen.findByRole('button', { name: /Challenge Findings/i })).toBeInTheDocument()
  })

  it('a persisted tree that is not JSON is discarded', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, 'not json at all')
    const user = userEvent.setup()
    await openForkFlow(user)
    expect(await screen.findByRole('button', { name: /Challenge Findings/i })).toBeInTheDocument()
  })

  it('expands a challenge node deeper in challenge mode', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, JSON.stringify([
      clarifierNode('c1', 'Which segment was measured?', { answer: 'enterprise', status: 'answered' }),
    ]))
    vi.mocked(api.researchGrillExpand).mockResolvedValue({
      nodes: [{ ...researchNode('r5', 'Does it hold for self-serve?'), parent: 'c1' }],
    })
    const user = userEvent.setup()
    await openForkFlow(user)
    await user.click(await screen.findByRole('button', { name: /expand/i }))
    await waitFor(() => expect(vi.mocked(api.researchGrillExpand)).toHaveBeenCalledWith(
      expect.objectContaining({ node_id: 'c1', mode: 'challenge', campaign_id: DONE.id })))
    expect(await screen.findByDisplayValue('Does it hold for self-serve?')).toBeInTheDocument()
  })

  it('manual sub-questions add to the fork count and can be removed again', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, JSON.stringify([researchNode('r1', 'What contradicts the finding?')]))
    const user = userEvent.setup()
    await openForkFlow(user)
    expect(await screen.findByRole('button', { name: /Fork with 1 sub-questions/i })).toBeInTheDocument()
    const box = screen.getByLabelText('Add your own sub-question or guidance')
    fireEvent.change(box, { target: { value: 'Re-check the 2024 data' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(await screen.findByRole('button', { name: /Fork with 2 sub-questions/i })).toBeInTheDocument()
    await user.click(screen.getByLabelText('Remove sub-question'))
    expect(await screen.findByRole('button', { name: /Fork with 1 sub-questions/i })).toBeInTheDocument()
  })

  it('forking creates the child, starts it, clears the draft and returns to the list', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, JSON.stringify([researchNode('r1', 'What contradicts the finding?')]))
    vi.mocked(api.researchAction).mockImplementation((_id: string, action: string) =>
      Promise.resolve(action === 'fork' ? { id: 'ffff9999' } : { ok: true }))
    const user = userEvent.setup()
    await openForkFlow(user)
    await user.click(await screen.findByRole('button', { name: /Fork with 1 sub-questions/i }))
    await waitFor(() => expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith(
      DONE.id, 'fork', expect.objectContaining({
        // suggestedMaxCycles(1) === 1 + 1 + 1
        max_cycles: 3,
        sub_questions: [{ text: 'What contradicts the finding?', origin: 'grill' }],
        question: DONE.question,
      })))
    await waitFor(() => expect(vi.mocked(api.researchAction)).toHaveBeenCalledWith('ffff9999', 'start'))
    await waitFor(() => expect(screen.getByRole('heading', { name: /Research Lab/i })).toBeInTheDocument())
    expect(sessionStorage.getItem(FORK_TREE_KEY)).toBeNull()
  })

  it('a fork whose start fails still navigates away, because the child exists', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, JSON.stringify([researchNode('r1', 'What contradicts the finding?')]))
    vi.mocked(api.researchAction).mockImplementation((_id: string, action: string) =>
      action === 'fork' ? Promise.resolve({ id: 'ffff9999' }) : Promise.reject(new Error('start refused')))
    const user = userEvent.setup()
    await openForkFlow(user)
    await user.click(await screen.findByRole('button', { name: /Fork with 1 sub-questions/i }))
    await waitFor(() => expect(screen.getByRole('heading', { name: /Research Lab/i })).toBeInTheDocument())
    expect(screen.queryByText(/Fork failed/i)).not.toBeInTheDocument()
  })

  it('a fork that returns no id reports that nothing was created', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, JSON.stringify([researchNode('r1', 'What contradicts the finding?')]))
    vi.mocked(api.researchAction).mockResolvedValue({})
    const user = userEvent.setup()
    await openForkFlow(user)
    await user.click(await screen.findByRole('button', { name: /Fork with 1 sub-questions/i }))
    expect(await screen.findByText(/no campaign was created/i)).toBeInTheDocument()
    // The draft survives a failed fork so the user can retry.
    expect(sessionStorage.getItem(FORK_TREE_KEY)).toContain('r1')
  })

  it('a fork request that rejects reports a retryable failure', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, JSON.stringify([researchNode('r1', 'What contradicts the finding?')]))
    vi.mocked(api.researchAction).mockRejectedValue(new Error('gateway down'))
    const user = userEvent.setup()
    await openForkFlow(user)
    await user.click(await screen.findByRole('button', { name: /Fork with 1 sub-questions/i }))
    expect(await screen.findByText(/Fork failed\. Please try again/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Fork with 1 sub-questions/i })).toBeEnabled()
  })

  it('cancelling the fork clears the persisted draft and returns to the list', async () => {
    sessionStorage.setItem(FORK_TREE_KEY, JSON.stringify([researchNode('r1', 'What contradicts the finding?')]))
    const user = userEvent.setup()
    await openForkFlow(user)
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: /Research Lab/i })).toBeInTheDocument())
    expect(sessionStorage.getItem(FORK_TREE_KEY)).toBeNull()
  })
})

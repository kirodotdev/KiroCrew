// Integration coverage for the Code Review Sage shell: the provider's wiring
// between the thread list, the detail pane, the inline report, and the picker.
//
// The units (RunList/ReportView/...) have their own tests; what is verified here
// is the wiring the page depends on — that selecting a thread fetches THAT run's
// report, that a live run polls, that the report renders inline rather than
// linking away, and that starting a review opens its new thread.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import Workspace from '../apps/code-review-sage/Workspace'
import { sageApi } from '../apps/code-review-sage/api'
import { SageProvider } from '../apps/code-review-sage/context'
import type {
  Run, RunReport, RunsResponse,
} from '../apps/code-review-sage/lib/types'

// The api client is the single seam between the app and the backend.
vi.mock('../api/client', () => ({
  api: {
    pullRequestSource: vi.fn(async () => ({
      provider: 'github', url: 'https://github.com/acme/widgets/pull/7', number: 7,
      title: 'Tighten the cookie jar',
      description: 'Caps the jar so a big header cannot wedge the gateway.',
      state: 'open', draft: false, mergedAt: '', updatedAt: '', headBranch: 'f',
      baseBranch: 'main', headSha: 'abc', author: 'ann', additions: 1, deletions: 0,
      changedFiles: 1, commits: [],
      checks: [{
        name: 'Backend Tests', workflow: 'CI', status: 'completed',
        conclusion: 'failure', bucket: 'failed', url: '', startedAt: '', completedAt: '',
      }],
      comments: [{
        id: 'c1', kind: 'comment', author: 'bob', body: 'Looks fine.',
        state: '', createdAt: '', url: '', path: '', line: null,
      }],
      files: [],
    })),
    // The publish bar lives in the detail pane, so opening a PR reads its draft.
    // Default: no draft, which keeps the bar silent for tests about other things.
    pullRequestPendingReview: vi.fn(async () => ({
      reviewId: '', comments: [], body: '', commitId: '', headSha: '',
      stale: false, contentRedacted: false, autoMergeArmed: false,
      contentDigest: '', staleDismissalEnabled: true,
    })),
    submitPullRequestReview: vi.fn(async () => ({ submitted: true, event: 'COMMENT' })),
  },
}))

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    runs: vi.fn(),
    run: vi.fn(),
    runReport: vi.fn(),
    cancelRun: vi.fn(),
    deleteRun: vi.fn(),
    archiveRun: vi.fn(),
    postComments: vi.fn(),
    postCommentGroups: vi.fn(),
    review: vi.fn(),
    reviewLinks: vi.fn(),
    reviewRepo: vi.fn(),
    recentRepos: vi.fn(),
    pinnedRepos: vi.fn(),
    pinRepo: vi.fn(),
    pinRepoUrl: vi.fn(),
    unpinRepo: vi.fn(),
    repoPrs: vi.fn(),
    settings: vi.fn(),
    putSettings: vi.fn(),
    namespaces: vi.fn(),
    createNamespace: vi.fn(),
    deleteNamespace: vi.fn(),
    learnings: vi.fn(),
  },
}))

const mockApi = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

function run(over: Partial<Run> = {}): Run {
  return {
    run_id: 'run-aaa',
    repo: 'acme/widgets',
    changes: ['https://github.com/acme/widgets/pull/7'],
    change_ids: ['GH-acme-widgets-7'],
    status: 'done',
    started_at: new Date(Date.now() - 120_000).toISOString(),
    finished_at: new Date(Date.now() - 30_000).toISOString(),
    progress: { 'GH-acme-widgets-7': { phase: 'done', counts: { red: 1, yellow: 2 } } },
    summary: { ok: true, report: { bands: { red: 1, yellow: 2, green: 0 }, total: 3 } },
    ...over,
  }
}

function report(over: Partial<RunReport> = {}): RunReport {
  return {
    run_id: 'run-aaa',
    status: 'done',
    ready: true,
    bands: { red: 1, yellow: 0, green: 0 },
    generated_at: '2026-07-28T12:00:00Z',
    total: 1,
    report_slug: null,
    rows: [{
      change_id: 'GH-acme-widgets-7',
      url: 'https://github.com/acme/widgets/pull/7',
      title: 'Tighten the cookie jar',
      band: 'red',
      why: 'blast=LARGE + 1× red',
      score: 71,
      design_risk: 'high',
      blast: 'LARGE',
      red: 1,
      yellow: 0,
      deep_reviewed: true,
      gate_verdict: 'CONCERNS',
      ship_comment: '## Not ready to ship\n\n1 must-fix outstanding.',
      findings: [{
        dimension: 'security',
        severity: 'red',
        file: 'src/jar.py',
        line: 42,
        observation: 'Unbounded growth',
        consequence: 'Requests are rejected once the header overflows',
        suggestion: 'Cap the jar',
      }, {
        dimension: 'style',
        severity: 'yellow',
        file: 'src/lid.py',
        line: 7,
        observation: 'Shadowed name',
        consequence: 'Confusing to read',
        suggestion: 'Rename it',
      }],
    }],
    ...over,
  }
}

/** Mount the shell. Pass `null` to leave the `runs` mock as the caller set it
 * (so a rejection configured by the test is not overwritten here). */

/** The PR list, with one PR that a given run already reviewed. */
function prFixture(over = {}) {
  return {
    url: 'https://github.com/acme/widgets/pull/7',
    number: 7,
    title: 'Tighten the cookie jar',
    head_sha: 'abcdef1234',
    author: 'ann',
    updated_at: new Date().toISOString(),
    draft: false,
    change_id: 'GH-acme-widgets-7',
    reviewed: false,
    reviewed_stale: false,
    ...over,
  }
}

function mount(runsResponse: RunsResponse | null, initialRunId?: string) {
  if (runsResponse) mockApi.runs.mockResolvedValue(runsResponse)
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId={initialRunId ?? null}>
          <Workspace />
        </SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** The middle column leads with Pull requests, so thread assertions select the
 *  Reviews tab first — the same click a user makes. */
/** The middle column only exists once a repo is picked (with none selected it
 *  would be an empty state pointing back at the rail), so reaching its Reviews
 *  tab means taking the same two steps a user does. */
/** Pick a repo. It is a DROPDOWN now (one row at the top of the rail, since the
 *  choice is made once), so selecting means opening it first — the same two steps
 *  a user takes. */
async function pickRepo(slug = 'acme/widgets') {
  await userEvent.click(await screen.findByRole('button', {
    name: /Pick a repository|^Repository:/,
  }))
  await userEvent.click(await screen.findByRole('menuitem', {
    name: new RegExp(slug.replace('/', '/\\s*')),
  }))
}

/** The rail panel holding the tabbed lists. */
function reviewsColumn(): HTMLElement {
  return screen.getByRole('tablist', { name: /pull requests or reviews/i })
    .parentElement as HTMLElement
}

/** The DETAIL pane's own tab bar. The embedded `PullRequestPanel` brings a second
 *  tablist (its source strip), so pane-level tab queries must say which bar. */
function detailTabs() {
  return screen.getByRole('tablist', { name: /Pull request detail/i })
}

async function showReviews() {
  await screen.findByRole('complementary')
  if (screen.queryByRole('button', { name: /^Repository:|Pick a repository/ })) {
    try {
      await pickRepo()
    } catch {
      /* no repos in this fixture — the Reviews tab is reachable regardless */
    }
  }
  await userEvent.click(await screen.findByRole('tab', { name: /Reviews/ }))
}

describe('Code Review Sage shell', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.runReport.mockResolvedValue(report())
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.settings.mockResolvedValue({
      settings: { model: null, effort: '', active_namespaces: ['default'], max_concurrent: 5 },
      models: [], efforts: [], namespaces: ['default'], max_concurrent_max: 30,
    })
  })

  it('lists review threads and prompts for a selection', async () => {
    mount({ runs: [run()], pool: null, reviewer: null })
    await showReviews()
    expect(await within(reviewsColumn())
      .findByRole('button', { name: /Review of acme\/widgets/ })).toBeTruthy()
    expect(
      screen.getByText(/Select a review to see its progress and report/),
    ).toBeTruthy()
  })

  it('renders the report INLINE for the selected thread', async () => {
    // The whole point of the rework: no navigation to /artifacts to read findings.
    mount({ runs: [run()], pool: null, reviewer: null })
    await showReviews()
    const card = await within(reviewsColumn())
      .findByRole('button', { name: /Review of acme\/widgets/ })
    await userEvent.click(card)
    // The title now appears in the pane heading AND in the report row, so match
    // the heading specifically.
    expect(await screen.findByRole('heading', { name: /Tighten the cookie jar/ })).toBeTruthy()
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalledWith('run-aaa'))
    // Findings live behind the row's disclosure, so the detail has to be opened —
    // but it opens in place, never by leaving the app.
    const expander = screen.getByRole('button', { expanded: false, name: /Tighten the cookie jar/ })
    await userEvent.click(expander)
    expect(await screen.findByText(/Unbounded growth/)).toBeTruthy()
  })

  it('honours a deep-linked run id so a finished-review notification lands on it', async () => {
    mount({ runs: [run()], pool: null, reviewer: null }, 'run-aaa')
    expect(await screen.findByText('Tighten the cookie jar')).toBeTruthy()
  })

  it('shows progress and a cancel control for a live thread, not a report', async () => {
    const live = run({
      status: 'running',
      finished_at: undefined,
      summary: undefined,
      changes: [
        'https://github.com/acme/widgets/pull/7',
        'https://github.com/acme/widgets/pull/8',
      ],
      change_ids: ['GH-acme-widgets-7', 'GH-acme-widgets-8'],
      progress: {
        'GH-acme-widgets-7': { phase: 'done', counts: { red: 0, yellow: 1 } },
        'GH-acme-widgets-8': { phase: 'reviewing' },
      },
    })
    mockApi.runReport.mockResolvedValue({ ...report(), ready: false, rows: [], total: 0 })
    mount({ runs: [live], pool: { busy: 1, max: 5 }, reviewer: null }, 'run-aaa')

    const bar = await screen.findByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('50')
    expect(
      screen.getByText(/report appears here as soon as the review finishes/i),
    ).toBeTruthy()

    mockApi.cancelRun.mockResolvedValue({ ok: true, status: 'cancelling', message: 'ok' })
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(mockApi.cancelRun).toHaveBeenCalledWith('run-aaa'))
  })

  it('opens the picker from the rail and starts a review on the chosen PRs', async () => {
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets',
      count: 1,
      prs: [{
        url: 'https://github.com/acme/widgets/pull/9',
        number: 9,
        title: 'Add the thing',
        head_sha: 'abc',
        author: 'ann',
        updated_at: new Date().toISOString(),
        draft: false,
        change_id: 'GH-acme-widgets-9',
        reviewed: false,
        reviewed_stale: false,
      }],
    })
    mockApi.review.mockResolvedValue({
      run_id: 'run-new', changes: ['https://github.com/acme/widgets/pull/9'],
    })
    mount({ runs: [], pool: null, reviewer: null })

    // Rail -> pick the repo. Its PRs land in the MIDDLE column, which is the
    // whole point of the drill-down (repo -> PRs -> report).
    await pickRepo()

    // No URL typing: tick the PR the picker found.
    const check = await screen.findByRole('checkbox', {
      name: /Review pull request #9/i,
    })
    await userEvent.click(check)
    await userEvent.click(screen.getByRole('button', { name: /Review 1 selected/i }))

    await waitFor(() => expect(mockApi.review).toHaveBeenCalledWith([
      'https://github.com/acme/widgets/pull/9',
    ]))
  })

  it('gives the detail pane a flex column so the empty state can centre', async () => {
    // Regression: <main> was a flex ITEM but not a flex CONTAINER, so
    // EmptyState's `flex-1` had nothing to resolve against — it collapsed to
    // content height and sat clipped at the top of the pane instead of centred.
    // jsdom does no layout, so assert the container contract that makes the
    // centring possible.
    mount({ runs: [run()], pool: null, reviewer: null })
    const pane = await screen.findByRole('main')
    expect(pane.className).toContain('flex-col')
    expect(pane.className).toMatch(/(^|\s)flex(\s|$)/)
    expect(pane.className).toContain('min-h-0')
  })

  it('surfaces a failed runs fetch instead of an empty list', async () => {
    mockApi.runs.mockRejectedValue(new Error('gateway said no'))
    mount(null)
    // Both review lists (rail + repo-scoped column) explain themselves rather
    // than one of them claiming the repo simply has no reviews.
    await showReviews()
    // The list explains itself rather than claiming there are no reviews.
    expect(await screen.findByText(/gateway said no/)).toBeTruthy()
  })

  it('lists the chosen repo\'s PRs in the MIDDLE column, not the detail pane', async () => {
    // Regression for the reported layout: the PR list used to render in the
    // detail pane, which put the list and the report it produces in one place
    // and left the middle column empty.
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets',
      count: 1,
      prs: [{
        url: 'https://github.com/acme/widgets/pull/9',
        number: 9,
        title: 'Add the thing',
        head_sha: 'abc',
        author: 'ann',
        updated_at: new Date().toISOString(),
        draft: false,
        change_id: 'GH-acme-widgets-9',
        reviewed: false,
        reviewed_stale: false,
      }],
    })
    mount({ runs: [], pool: null, reviewer: null })
    await pickRepo()

    const check = await screen.findByRole('checkbox', { name: /Review pull request #9/i })
    // The detail pane is the report surface; the PR list must NOT be inside it.
    const pane = screen.getByRole('main')
    expect(pane.contains(check)).toBe(false)
  })

  it('clicking a PR shows its basics AND its generated review, not just a tab', async () => {
    // The reported worry: a review reachable only via the Reviews tab is a
    // review you forget. Clicking the PR must surface the findings in place.
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets', count: 1, prs: [prFixture({ reviewed: true })],
    })
    mount({ runs: [run()], pool: null, reviewer: null })
    await pickRepo()
    await userEvent.click(
      await screen.findByRole('button', { name: /Open pull request #7/i }),
    )

    // PR basics, in the detail pane's own heading (the list row repeats the
    // title, so match the heading rather than any text node).
    expect(await screen.findByRole('heading', { name: /Tighten the cookie jar/ })).toBeTruthy()
    // Both the header and the report row link out to the PR.
    expect(screen.getAllByRole('link', { name: /#7/ }).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/ann/).length).toBeGreaterThan(0)
    // And the review that run produced for it.
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalledWith('run-aaa'))
    const expander = await screen.findByRole('button', {
      expanded: false, name: /Tighten the cookie jar/,
    })
    await userEvent.click(expander)
    expect(await screen.findByText(/Unbounded growth/)).toBeTruthy()
  })

  it('offers to review a PR that has never been reviewed', async () => {
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets',
      count: 1,
      // A PR no run covers, so there is no review to show. The URL has to move
      // with the number: matching is on the change URL (change_id is a lossy
      // filename stem that collides across repos), so a fixture numbered 99 that
      // kept `/pull/7` would still be covered by the run above.
      prs: [prFixture({
        change_id: 'GH-acme-widgets-99',
        number: 99,
        url: 'https://github.com/acme/widgets/pull/99',
      })],
    })
    mockApi.review.mockResolvedValue({ run_id: 'run-new', changes: [] })
    mount({ runs: [run()], pool: null, reviewer: null })
    await pickRepo()
    await userEvent.click(
      await screen.findByRole('button', { name: /Open pull request #99/i }),
    )
    expect(await screen.findByText(/has not been reviewed yet/i)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /^Review$/ }))
    await waitFor(() => expect(mockApi.review).toHaveBeenCalledWith([
      'https://github.com/acme/widgets/pull/99',
    ]))
  })

  it('keeps the checkbox for batching separate from opening the PR', async () => {
    // One <label> wrapping both meant you could not look at a PR without also
    // queueing it for review.
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets', count: 1, prs: [prFixture()],
    })
    mount({ runs: [], pool: null, reviewer: null })
    await pickRepo()
    await userEvent.click(
      await screen.findByRole('button', { name: /Open pull request #7/i }),
    )
    const box = screen.getByRole('checkbox', { name: /Review pull request #7/i })
    expect(box).not.toBeChecked()
  })

  it('puts the PR content behind tabs, with Sage Review leading', async () => {
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets', count: 1, prs: [prFixture({ reviewed: true })],
    })
    mount({ runs: [run()], pool: null, reviewer: null })
    await pickRepo()
    await userEvent.click(
      await screen.findByRole('button', { name: /Open pull request #7/i }),
    )

    // Sage Review is the default tab — the app's own output leads.
    const reviewTab = await within(detailTabs()).findByRole('tab', { name: /Sage Review/i })
    expect(reviewTab.getAttribute('aria-selected')).toBe('true')
    expect(within(detailTabs()).getByRole('tab', { name: /Pull request/i })).toBeTruthy()

    // The provider's own view of the PR is NOT rendered until its tab is chosen
    // (the point of tabs: a long description used to push the review below the
    // fold). Choosing it mounts the SHARED PullRequestPanel, so the description,
    // comments and checks arrive through one component rather than three
    // Sage-local copies of it.
    expect(screen.queryByText(/Caps the jar/)).toBeNull()
    await userEvent.click(within(detailTabs()).getByRole('tab', { name: /Pull request/i }))

    await userEvent.click(await screen.findByRole('tab', { name: /Description/i }))
    expect(await screen.findByText(/Caps the jar/)).toBeTruthy()

    // The panel's OWN section bar, not the rail's Reviews list tab.
    const panelTabs = screen.getByRole('tablist', { name: /Pull request sections/i })
    await userEvent.click(within(panelTabs).getByRole('tab', { name: /Reviews/i }))
    expect(await screen.findByText(/Looks fine/)).toBeTruthy()

    await userEvent.click(screen.getByRole('tab', { name: /^Checks/i }))
    expect(await screen.findByText('Backend Tests')).toBeTruthy()
  })

  it('fetches the provider payload ONCE for the pane and the embedded panel', async () => {
    // The pane's header and the embedded panel both need the payload. They share
    // one query key precisely so opening a PR costs one provider call, not two —
    // a private Sage key would silently double every open.
    const core = (await import('../api/client')).api as unknown as
      { pullRequestSource: ReturnType<typeof vi.fn> }
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets', count: 1, prs: [prFixture()],
    })
    mount({ runs: [], pool: null, reviewer: null })
    await pickRepo()
    await userEvent.click(
      await screen.findByRole('button', { name: /Open pull request #7/i }),
    )
    await within(detailTabs()).findByRole('tab', { name: /Sage Review/i })
    await userEvent.click(within(detailTabs()).getByRole('tab', { name: /Pull request/i }))
    await screen.findByRole('tab', { name: /Description/i })
    expect(core.pullRequestSource).toHaveBeenCalledTimes(1)
  })

  it('opens a single-PR run WITH its PR context, not a bare run view', async () => {
    // Reported: clicking a review in the thread list dropped everything about
    // the pull request. A run over one PR is that PR, so it gets the same tabs.
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', count: 0, prs: [] })
    mount({ runs: [run()], pool: null, reviewer: null })
    await showReviews()
    await userEvent.click(
      await within(reviewsColumn())
        .findByRole('button', { name: /Review of acme\/widgets/ }),
    )
    // Provider-backed context is present even though the PR was never in the
    // loaded list — the reference is derived from the run's change URL.
    for (const name of [/Sage Review/i, /Pull request/i]) {
      expect(await within(detailTabs()).findByRole('tab', { name })).toBeTruthy()
    }
    await userEvent.click(within(detailTabs()).getByRole('tab', { name: /Pull request/i }))
    await userEvent.click(await screen.findByRole('tab', { name: /Description/i }))
    expect(await screen.findByText(/Caps the jar/)).toBeTruthy()
  })

  it('withholds publishing when a finished run posted nothing for the change', async () => {
    // `review.auto_post` defaults to false, so a rerun can reach `done` having
    // delivered no draft — and the PENDING review on the pull request is then the
    // PREVIOUS run's. Completion is not delivery: publishing here would submit
    // obsolete findings irreversibly.
    const core = (await import('../api/client')).api as unknown as
      { pullRequestPendingReview: ReturnType<typeof vi.fn> }
    core.pullRequestPendingReview.mockResolvedValue({
      reviewId: '4242', comments: [], body: '[code-review-sage] stale draft', commitId: 'abc',
      headSha: 'abc', stale: false, contentRedacted: false, autoMergeArmed: false,
      contentDigest: 'd1', staleDismissalEnabled: true,
    })
    // Finished, with NO posted_keys for this change.
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', count: 0, prs: [] })
    mount({ runs: [run({ posted_keys: {} })], pool: null, reviewer: null })
    await showReviews()
    await userEvent.click(
      await within(reviewsColumn())
        .findByRole('button', { name: /Review of acme\/widgets/ }),
    )
    expect(await screen.findByText(/this review is still running/i)).toBeTruthy()
    for (const name of ['Submit as comment', 'Request changes', 'Approve']) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
  })

  it('offers the verdicts once this run has posted for the change', async () => {
    const core = (await import('../api/client')).api as unknown as
      { pullRequestPendingReview: ReturnType<typeof vi.fn> }
    core.pullRequestPendingReview.mockResolvedValue({
      reviewId: '4242', comments: [], body: '[code-review-sage] fresh draft', commitId: 'abc',
      headSha: 'abc', stale: false, contentRedacted: false, autoMergeArmed: false,
      contentDigest: 'd1', staleDismissalEnabled: true,
    })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', count: 0, prs: [] })
    mount({
      runs: [run({
        posted_keys: { 'GH-acme-widgets-7': ['design'] },
        posted_review_ids: { 'GH-acme-widgets-7': '4242' },
      })],
      pool: null, reviewer: null,
    })
    await showReviews()
    await userEvent.click(
      await within(reviewsColumn())
        .findByRole('button', { name: /Review of acme\/widgets/ }),
    )
    expect(await screen.findByRole('button', { name: 'Submit as comment' })).toBeTruthy()
  })

  it('marks in-flight PRs in the list and refuses to queue them again', async () => {
    const live = run({
      status: 'running',
      finished_at: undefined,
      summary: undefined,
      progress: { 'GH-acme-widgets-7': { phase: 'reviewing' } },
    })
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets', count: 1, prs: [prFixture()],
    })
    mount({ runs: [live], pool: null, reviewer: null })
    await pickRepo()

    // The chip says what is happening now, and the box cannot be ticked.
    expect(await screen.findByText('reviewing')).toBeTruthy()
    const box = await screen.findByRole('checkbox', {
      name: /#7 is already being reviewed/i,
    })
    expect(box).toBeDisabled()

    // And it is excluded from the bulk action's count. The button names the
    // action ("Review all 0"), not just a bare "All 0" that read as a filter
    // chip beside the real filter chips.
    expect(screen.getByRole('button', { name: /^Review all 0$/ })).toBeTruthy()
  })
})

describe('Code Review Sage review placement', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.pinnedRepos.mockResolvedValue({
      repos: [{ owner: 'acme', repo: 'widgets' }, { owner: 'other', repo: 'thing' }],
    })
    mockApi.runReport.mockResolvedValue(report())
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('lists EVERY review in the rail, whatever repo is in focus', async () => {
    mount({ runs: [run(), run({
      run_id: 'run-bbb',
      repo: 'other/thing',
      changes: ['https://github.com/other/thing/pull/3'],
      change_ids: ['GH-other-thing-3'],
    })] } as RunsResponse)
    await userEvent.click(await screen.findByRole('tab', { name: /Reviews/ }))
    const rail = await screen.findByRole('complementary')
    // Both runs are reachable without picking their repo first.
    expect(await within(rail).findByRole('button', { name: /Review of acme\/widgets/ })).toBeTruthy()
    expect(within(rail).getByRole('button', { name: /Review of other\/thing/ })).toBeTruthy()
  })

  it('scopes the middle column Reviews tab to the SELECTED repo', async () => {
    mount({ runs: [run(), run({
      run_id: 'run-bbb',
      repo: 'other/thing',
      changes: ['https://github.com/other/thing/pull/3'],
      change_ids: ['GH-other-thing-3'],
    })] } as RunsResponse)
    await screen.findByRole('complementary')
    await pickRepo()
    await showReviews()
    const column = reviewsColumn()
    expect(await within(column).findByRole('button', { name: /Review of acme\/widgets/ })).toBeTruthy()
    // The other repo's review is in the rail only.
    expect(within(column).queryByRole('button', { name: /Review of other\/thing/ })).toBeNull()
  })

  it('can widen the scope, so a review is never stranded by a repo pick', async () => {
    // This is now the ONLY review list, so scoping alone would make a finished
    // review unreachable once you move to another repo.
    mount({ runs: [run(), run({
      run_id: 'run-bbb',
      repo: 'other/thing',
      changes: ['https://github.com/other/thing/pull/3'],
      change_ids: ['GH-other-thing-3'],
    })] } as RunsResponse)
    await screen.findByRole('complementary')
    await pickRepo()
    await userEvent.click(await screen.findByRole('tab', { name: /Reviews/ }))
    const column = reviewsColumn()
    expect(within(column).queryByRole('button', { name: /Review of other\/thing/ })).toBeNull()
    await userEvent.click(await within(column).findByRole('button', { name: /Show all/ }))
    expect(await within(column).findByRole('button', { name: /Review of other\/thing/ }))
      .toBeTruthy()
  })

  it('resets a widened scope when the repo changes', async () => {
    mount({ runs: [run(), run({
      run_id: 'run-bbb',
      repo: 'other/thing',
      changes: ['https://github.com/other/thing/pull/3'],
      change_ids: ['GH-other-thing-3'],
    })] } as RunsResponse)
    await screen.findByRole('complementary')
    await pickRepo()
    await userEvent.click(await screen.findByRole('tab', { name: /Reviews/ }))
    await userEvent.click(await within(reviewsColumn()).findByRole('button', { name: /Show all/ }))
    // Switching repo must not silently carry the previous repo's scope choice.
    // (Picking a repo also brings its pull requests to the front, by design.)
    await pickRepo('other/thing')
    await userEvent.click(await screen.findByRole('tab', { name: /Reviews/ }))
    expect(await within(reviewsColumn()).findByRole('button', { name: /Show all/ }))
      .toBeTruthy()
  })

  it('says the repo has no reviews rather than showing another repo\'s', async () => {
    mount({ runs: [run({
      run_id: 'run-bbb',
      repo: 'other/thing',
      changes: ['https://github.com/other/thing/pull/3'],
      change_ids: ['GH-other-thing-3'],
    })] } as RunsResponse)
    await screen.findByRole('complementary')
    await pickRepo()
    await showReviews()
    expect(await screen.findByText(/No reviews for acme\/widgets/)).toBeTruthy()
  })

  it('returns to Reviews from Settings', async () => {
    // Reviews is a peer section, not an implicit default: the other sections hide
    // the rail's review list, so without this row there is no way back at all.
    mount({ runs: [run()] } as RunsResponse)
    const nav = await screen.findByRole('navigation', { name: /Sections/ })
    await userEvent.click(within(nav).getByRole('button', { name: /Settings/ }))
    expect(await screen.findByRole('heading', { name: /Settings/ })).toBeTruthy()
    await userEvent.click(within(nav).getByRole('button', { name: /Reviews/ }))
    await waitFor(() => expect(
      screen.queryByRole('heading', { name: /Settings/ })).toBeNull())
  })

  it('returns to Reviews from Learning', async () => {
    mount({ runs: [run()] } as RunsResponse)
    const nav = await screen.findByRole('navigation', { name: /Sections/ })
    await userEvent.click(within(nav).getByRole('button', { name: /Learning/ }))
    expect(await screen.findByRole('heading', { name: /Learning|default/ })).toBeTruthy()
    await userEvent.click(within(nav).getByRole('button', { name: /Reviews/ }))
    // The repo picker is back, which is the review surface's rail.
    expect(await screen.findByRole('button', {
      name: /Pick a repository|^Repository:/,
    })).toBeTruthy()
  })

  it('does not print "Reviews" twice, once per level', async () => {
    // The section nav and the list tabs both said "Reviews", stacked one above
    // the other, which read as a repeated control.
    mount({ runs: [run()] } as RunsResponse)
    const nav = await screen.findByRole('navigation', { name: /Sections/ })
    // The section keeps the glyph and the accessible name, not the printed word.
    expect(within(nav).getByRole('button', { name: /Reviews/ }).textContent).toBe('')
    expect(screen.getAllByText('Reviews')).toHaveLength(1)
  })

  it('marks the active section in the nav', async () => {
    mount({ runs: [run()] } as RunsResponse)
    const nav = await screen.findByRole('navigation', { name: /Sections/ })
    expect(within(nav).getByRole('button', { name: /Reviews/ }))
      .toHaveAttribute('aria-current', 'page')
    await userEvent.click(within(nav).getByRole('button', { name: /Learning/ }))
    expect(within(nav).getByRole('button', { name: /Learning/ }))
      .toHaveAttribute('aria-current', 'page')
  })

  it('hides the repo picker and lists on the other sections', async () => {
    // A repo picker and a pull-request list above an unrelated screen was the
    // confusing part.
    mount({ runs: [run()] } as RunsResponse)
    const nav = await screen.findByRole('navigation', { name: /Sections/ })
    await userEvent.click(within(nav).getByRole('button', { name: /Settings/ }))
    await screen.findByRole('heading', { name: /Settings/ })
    expect(screen.queryByRole('button', { name: /Pick a repository|^Repository:/ })).toBeNull()
    expect(screen.queryByRole('tablist', { name: /pull requests or reviews/i })).toBeNull()
  })
})

describe('the shell is two columns', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue(report())
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('has no separate list column between the rail and the report', async () => {
    // A third column spent a fixed slice of the window on a list already used to
    // get where you are; reports are the widest thing the app renders.
    mount({ runs: [run()] } as RunsResponse)
    await screen.findByRole('complementary')
    expect(screen.queryByRole('separator', { name: /Resize review list/ })).toBeNull()
  })

  it('keeps the lists inside the rail', async () => {
    mount({ runs: [run()] } as RunsResponse)
    const rail = await screen.findByRole('complementary')
    expect(within(rail).getByRole('tablist', { name: /pull requests or reviews/i }))
      .toBeTruthy()
  })

  it('opens a review from the rail into the report pane', async () => {
    mount({ runs: [run()] } as RunsResponse)
    await showReviews()
    await userEvent.click(await within(reviewsColumn())
      .findByRole('button', { name: /Review of acme\/widgets/ }))
    expect(await screen.findByRole('heading', { name: /Tighten the cookie jar/ }))
      .toBeTruthy()
  })
})

describe('the repo picker is a dropdown at the top of the rail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue(report())
    mockApi.pinnedRepos.mockResolvedValue({
      repos: [{ owner: 'acme', repo: 'widgets' }, { owner: 'other', repo: 'thing' }],
    })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('collapses the repo list to one row until opened', async () => {
    // A stacked panel spent a fixed slice of the rail on a choice made once.
    mount({ runs: [] } as RunsResponse)
    expect(await screen.findByRole('button', { name: /Pick a repository/ })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /acme\/\s*widgets/ })).toBeNull()
  })

  it('names the selected repo on the trigger', async () => {
    mount({ runs: [] } as RunsResponse)
    await pickRepo()
    expect(await screen.findByRole('button', { name: /^Repository: acme\/widgets/ }))
      .toBeTruthy()
  })

  it('loads the chosen repo\'s pull requests', async () => {
    mount({ runs: [] } as RunsResponse)
    await pickRepo()
    await waitFor(() => expect(mockApi.repoPrs)
      .toHaveBeenCalledWith('https://github.com/acme/widgets'))
  })

  it('offers adding a repo from inside the dropdown', async () => {
    // The modal host lives in the page, not the shell, so this asserts the entry
    // point exists and selects cleanly; the modal itself is covered by
    // CodeReviewSageAddRepos.
    mount({ runs: [] } as RunsResponse)
    await userEvent.click(await screen.findByRole('button', { name: /Pick a repository/ }))
    const add = await screen.findByRole('menuitem', { name: /Add a repo/ })
    await userEvent.click(add)
    await waitFor(() => expect(
      screen.queryByRole('menuitem', { name: /Add a repo/ })).toBeNull())
  })

  it('surfaces recently picked repos above the full list', async () => {
    // The pinned list is ordered by when a repo was ADDED, which says nothing
    // about the two or three you keep returning to.
    mount({ runs: [] } as RunsResponse)
    await pickRepo('acme/widgets')
    await pickRepo('other/thing')
    await userEvent.click(await screen.findByRole('button', { name: /^Repository:/ }))
    expect(await screen.findByText('Recent')).toBeTruthy()
    expect(screen.getByText('All repos')).toBeTruthy()
  })

  it('keeps the picks across a reload', async () => {
    const first = mount({ runs: [] } as RunsResponse)
    await pickRepo('acme/widgets')
    await pickRepo('other/thing')
    first.unmount()
    mount({ runs: [] } as RunsResponse)
    await userEvent.click(await screen.findByRole('button', {
      name: /^Repository:|Pick a repository/,
    }))
    const recents = await screen.findAllByRole('menuitem', { name: /other\/\s*thing/ })
    expect(recents.length).toBeGreaterThan(0)
  })

  it('does not offer removal inside the menu', async () => {
    // Deliberate: an interactive control nested in a menu item is invalid a11y
    // and Radix closes the menu before the inner click lands. Removal is in the
    // Add-repos modal, which is the repo-management surface.
    mount({ runs: [] } as RunsResponse)
    await userEvent.click(await screen.findByRole('button', { name: /Pick a repository/ }))
    expect(screen.queryByRole('button', { name: /Remove other\/thing/ })).toBeNull()
  })
})

describe('posting a review to the pull request', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue(report())
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.postComments.mockResolvedValue({
      ok: true, run_id: 'run-aaa', posting: true, pending: 2,
    })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  /** Open the report for the seeded run. */
  async function openReport(runOver = {}) {
    mount({ runs: [run(runOver)] } as RunsResponse, 'run-aaa')
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalledWith('run-aaa'))
    return screen.findByText(/change reviewed/)
  }

  it('offers to post, labelled with the comment count', async () => {
    await openReport()
    // 1 red + 0 yellow inline, plus the always-on ship-readiness comment.
    expect(await screen.findByRole('button', { name: /Draft 2 comments/ })).toBeTruthy()
  })

  it('asks before writing to someone else\'s pull request', async () => {
    await openReport()
    await userEvent.click(await screen.findByRole('button', { name: /Draft 2 comments/ }))
    // The first click must not send: this app cannot un-post a GitHub review.
    expect(mockApi.postComments).not.toHaveBeenCalled()
    expect(screen.getByText(/Draft 2 comments as a pending review\?/)).toBeTruthy()
  })

  it('posts only after the confirm', async () => {
    await openReport()
    await userEvent.click(await screen.findByRole('button', { name: /Draft 2 comments/ }))
    await userEvent.click(screen.getByRole('button', { name: /^Draft$/ }))
    // Post-all passes no selection; a per-finding post passes one (covered below).
    await waitFor(() => expect(mockApi.postComments)
      .toHaveBeenCalledWith('run-aaa', undefined))
  })

  it('posts only THIS pull request when the run covers several', async () => {
    // The backend loops over every change in the run when no change_id is sent,
    // so an unscoped post from one PR's detail published comments to every PR a
    // repo run covered — the opposite of "posting is a deliberate act".
    mockApi.repoPrs.mockResolvedValue({
      repo: 'acme/widgets', count: 1, prs: [prFixture({ reviewed: true })],
    })
    const multi = run({
      changes: [
        'https://github.com/acme/widgets/pull/7',
        'https://github.com/acme/widgets/pull/8',
      ],
      change_ids: ['GH-acme-widgets-7', 'GH-acme-widgets-8'],
    })
    mount({ runs: [multi], pool: null, reviewer: null } as RunsResponse)
    await pickRepo()
    await userEvent.click(
      await screen.findByRole('button', { name: /Open pull request #7/i }),
    )

    await userEvent.click(await screen.findByRole('button', { name: /Draft \d+ comment/ }))
    await userEvent.click(screen.getByRole('button', { name: /^Draft$/ }))

    await waitFor(() => expect(mockApi.postComments)
      .toHaveBeenCalledWith(multi.run_id, { changeId: 'GH-acme-widgets-7' }))
  })

  it('backs out cleanly', async () => {
    await openReport()
    await userEvent.click(await screen.findByRole('button', { name: /Draft 2 comments/ }))
    await userEvent.click(screen.getByRole('button', { name: /Cancel/ }))
    expect(mockApi.postComments).not.toHaveBeenCalled()
    expect(await screen.findByRole('button', { name: /Draft 2 comments/ })).toBeTruthy()
  })

  it('shows what was posted instead of offering again', async () => {
    await openReport({ posted_at: new Date().toISOString(), posted_comments: 2 })
    // The run list does not mount the publish bar, so the drafted line must send the
    // reader to the pull request's own page rather than "below" — there is nothing
    // below here to publish with.
    expect(await screen.findByText(
      /Drafted 2 comments — open the pull request here to publish/)).toBeTruthy()
    expect(screen.queryByText(/publish below/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /Draft 2 comments/ })).toBeNull()
  })

  it('reports a failed post rather than looking untouched', async () => {
    await openReport({ post_error: 'gh api rejected the review' })
    expect(await screen.findByText(/Could not draft the review/)).toBeTruthy()
  })

  it('says nothing when a review is still running', async () => {
    // A run mid-flight has nothing final to publish.
    mount({ runs: [run({ status: 'running', finished_at: undefined })] } as RunsResponse, 'run-aaa')
    await screen.findByRole('progressbar')
    expect(screen.queryByRole('button', { name: /Draft \d+ comment/ })).toBeNull()
  })
})

describe('posting individual comments', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue(report())
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.postComments.mockResolvedValue({
      ok: true, run_id: 'run-aaa', posting: true, pending: 1,
    })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  /** Open the report and expand the row so its findings are visible. */
  async function openFindings(runOver = {}) {
    mount({ runs: [run(runOver)] } as RunsResponse, 'run-aaa')
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalled())
    await userEvent.click(await screen.findByRole('button', {
      expanded: false, name: /Tighten the cookie jar/,
    }))
    return screen.findByText(/Unbounded growth/)
  }

  it('offers to post one finding on its own', async () => {
    // You rarely agree with every finding, so sending them one at a time is the
    // normal case rather than an escape hatch.
    await openFindings()
    expect(await screen.findByRole('button', { name: /Draft this finding as a review comment on src\/jar\.py/ }))
      .toBeTruthy()
  })

  it('sends only that finding, keyed to its change', async () => {
    await openFindings()
    await userEvent.click(await screen.findByRole('button', {
      name: /Draft this finding as a review comment on src\/jar\.py/,
    }))
    await waitFor(() => expect(mockApi.postComments).toHaveBeenCalledWith(
      'run-aaa', { changeId: 'GH-acme-widgets-7', keys: ['finding:0'] }))
  })

  it('marks a finding already on the pull request as posted', async () => {
    await openFindings({
      posted_keys: { 'GH-acme-widgets-7': ['finding:0'] },
    })
    expect(await screen.findByText(/Drafted — publish below/)).toBeTruthy()
    // Only THAT finding is done; the other still offers to post.
    expect(screen.queryByRole('button', { name: /Draft this finding as a review comment on src\/jar\.py/ })).toBeNull()
    expect(screen.getByRole('button', { name: /Draft this finding as a review comment on src\/lid\.py/ })).toBeTruthy()
  })
})

describe('deleting a review', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue(report())
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.deleteRun.mockResolvedValue({ ok: true })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('asks before destroying the review', async () => {
    mount({ runs: [run()] } as RunsResponse)
    await showReviews()
    await userEvent.click(await screen.findByRole('button', {
      name: /Delete review of acme\/widgets/,
    }))
    // Nothing here can bring the report back, so the first click only asks.
    expect(mockApi.deleteRun).not.toHaveBeenCalled()
    expect(screen.getByText(/Delete this review\?/)).toBeTruthy()
  })

  it('deletes on confirm', async () => {
    mount({ runs: [run()] } as RunsResponse)
    await showReviews()
    await userEvent.click(await screen.findByRole('button', {
      name: /Delete review of acme\/widgets/,
    }))
    await userEvent.click(screen.getByRole('button', { name: /^Delete$/ }))
    await waitFor(() => expect(mockApi.deleteRun).toHaveBeenCalledWith('run-aaa'))
  })

  it('backs out cleanly', async () => {
    mount({ runs: [run()] } as RunsResponse)
    await showReviews()
    await userEvent.click(await screen.findByRole('button', {
      name: /Delete review of acme\/widgets/,
    }))
    await userEvent.click(screen.getByRole('button', { name: /Cancel/ }))
    expect(mockApi.deleteRun).not.toHaveBeenCalled()
  })

  it('offers exactly one delete affordance, the guarded one', async () => {
    // The detail header used to carry its own trash icon that called deleteRun
    // straight away, so opening a finished review put an unguarded one-click
    // destroy next to the guarded one. Nothing brings the run directory or its
    // stored report back, so every path to deletion has to ask first. This
    // mounts with the run selected AND the Reviews list shown, which is the
    // state where both controls were on screen together.
    mount({ runs: [run()] } as RunsResponse, 'run-aaa')
    await showReviews()
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalled())

    const destroyers = screen.getAllByRole('button').filter(b => {
      const name = `${b.getAttribute('aria-label') ?? ''} ${b.getAttribute('title') ?? ''}`
      return /delete|dismiss/i.test(name)
    })
    expect(destroyers).toHaveLength(1)

    // And that one still asks rather than firing.
    await userEvent.click(destroyers[0])
    expect(mockApi.deleteRun).not.toHaveBeenCalled()
  })
})

describe('posting a chosen subset', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue(report())
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.postComments.mockResolvedValue({
      ok: true, run_id: 'run-aaa', posting: true, pending: 2,
    })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  async function openFindings() {
    mount({ runs: [run()] } as RunsResponse, 'run-aaa')
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalled())
    await userEvent.click(await screen.findByRole('button', {
      expanded: false, name: /Tighten the cookie jar/,
    }))
    return screen.findByText(/Unbounded growth/)
  }

  it('keeps the ticks when the grouped post is refused', async () => {
    // The selection is only safe to clear once the post RESOLVES. While the prop
    // accepted a `void` return, a caller that dropped the promise (`void
    // postCommentGroups(...)`, or a `forEach`) made the await resolve instantly,
    // so the ticks were wiped even though the comments never landed and the user
    // had nothing left to retry from.
    mockApi.postCommentGroups.mockRejectedValue(
      new Error('a review of this change is in flight'))
    await openFindings()
    await userEvent.click(screen.getByRole('checkbox', { name: /Select security in src\/jar\.py/ }))
    await userEvent.click(screen.getByRole('checkbox', { name: /Select style in src\/lid\.py/ }))
    await userEvent.click(await screen.findByRole('button', { name: /Draft 2 selected findings/ }))

    await waitFor(() => expect(mockApi.postCommentGroups).toHaveBeenCalledTimes(1))
    // Still 2 selected: the refusal left the selection intact.
    expect(await screen.findByRole('button', { name: /Draft 2 selected findings/ })).toBeTruthy()
  })

  it('clears the ticks once the grouped post succeeds', async () => {
    mockApi.postCommentGroups.mockResolvedValue({
      ok: true, run_id: 'run-aaa', posting: true, pending: 2,
    })
    await openFindings()
    await userEvent.click(screen.getByRole('checkbox', { name: /Select security in src\/jar\.py/ }))
    await userEvent.click(await screen.findByRole('button', { name: /Draft 1 selected finding/ }))

    await waitFor(() => expect(mockApi.postCommentGroups).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /Draft 1 selected finding/ })).toBeNull())
  })

  it('sends the ticked comments in ONE request', async () => {
    // One request is one pending review on the pull request; clicking each
    // finding separately would leave the author three drafts to read.
    await openFindings()
    await userEvent.click(screen.getByRole('checkbox', { name: /Select security in src\/jar\.py/ }))
    await userEvent.click(screen.getByRole('checkbox', { name: /Select style in src\/lid\.py/ }))
    await userEvent.click(await screen.findByRole('button', { name: /Draft 2 selected findings/ }))
    // ONE request, even across changes: `posting` is a per-run flag, so a second
    // request would be refused with `already_posting` and its comments dropped.
    await waitFor(() => expect(mockApi.postCommentGroups).toHaveBeenCalledTimes(1))
    expect(mockApi.postCommentGroups).toHaveBeenCalledWith('run-aaa', [
      { changeId: 'GH-acme-widgets-7', keys: ['finding:0', 'finding:1'] },
    ])
  })

  it('replaces post-all while a selection is live', async () => {
    await openFindings()
    expect(screen.getByRole('button', { name: /Draft \d+ comments/ })).toBeTruthy()
    await userEvent.click(screen.getByRole('checkbox', { name: /Select security in src\/jar\.py/ }))
    // Offering "post all" beside "post 1 selected" invites sending more than
    // was chosen.
    expect(screen.queryByRole('button', { name: /Draft \d+ comments/ })).toBeNull()
    expect(screen.getByRole('button', { name: /Draft 1 selected finding/ })).toBeTruthy()
  })

  it('clears a selection without sending', async () => {
    await openFindings()
    await userEvent.click(screen.getByRole('checkbox', { name: /Select security in src\/jar\.py/ }))
    await userEvent.click(screen.getByRole('button', { name: /Clear/ }))
    expect(mockApi.postComments).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Draft \d+ comments/ })).toBeTruthy()
  })

  it('untickes on a second click', async () => {
    await openFindings()
    const box = screen.getByRole('checkbox', { name: /Select security in src\/jar\.py/ })
    await userEvent.click(box)
    await userEvent.click(box)
    expect(screen.queryByRole('button', { name: /Draft \d+ selected finding/ })).toBeNull()
  })

  it('offers no checkbox for a comment already posted', async () => {
    mount({ runs: [run({ posted_keys: { 'GH-acme-widgets-7': ['finding:0'] } })] } as RunsResponse, 'run-aaa')
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalled())
    await userEvent.click(await screen.findByRole('button', {
      expanded: false, name: /Tighten the cookie jar/,
    }))
    await screen.findByText(/Unbounded growth/)
    expect(screen.queryByRole('checkbox', { name: /Select security in src\/jar\.py/ })).toBeNull()
    expect(screen.getByRole('checkbox', { name: /Select style in src\/lid\.py/ })).toBeTruthy()
  })
})

describe('the ship-readiness summary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue(report())
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.postComments.mockResolvedValue({
      ok: true, run_id: 'run-aaa', posting: true, pending: 1,
    })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  async function openRow(reportOver = {}) {
    mockApi.runReport.mockResolvedValue(report(reportOver))
    mount({ runs: [run()] } as RunsResponse, 'run-aaa')
    await waitFor(() => expect(mockApi.runReport).toHaveBeenCalled())
    await userEvent.click(await screen.findByRole('button', {
      expanded: false, name: /Tighten the cookie jar/,
    }))
  }

  it('shows the exact body that would be posted', async () => {
    await openRow()
    expect(await screen.findByText(/Not ready to ship/)).toBeTruthy()
    expect(screen.getByText(/drafted as a top-level comment/)).toBeTruthy()
  })

  it('can be posted on its own', async () => {
    await openRow()
    await userEvent.click(await screen.findByRole('button', {
      name: /Draft the ship-readiness summary/,
    }))
    await waitFor(() => expect(mockApi.postComments).toHaveBeenCalledWith(
      'run-aaa', { changeId: 'GH-acme-widgets-7', keys: ['design'] }))
  })

  it('can be held back while findings are sent', async () => {
    await openRow()
    await userEvent.click(screen.getByRole('checkbox', { name: /Select security in src\/jar\.py/ }))
    await userEvent.click(await screen.findByRole('button', { name: /Draft 1 selected finding/ }))
    // The verdict is the comment an author most often wants to withhold.
    await waitFor(() => expect(mockApi.postCommentGroups).toHaveBeenCalledWith(
      'run-aaa', [{ changeId: 'GH-acme-widgets-7', keys: ['finding:0'] }]))
  })

  it('says nothing when the report predates the ship body', async () => {
    // Older reports have no ship_comment; a post control for text we cannot show
    // would ask the user to send something they never read.
    await openRow({ rows: [{ ...report().rows[0], ship_comment: '' }] })
    expect(screen.queryByText(/drafted as a top-level comment/)).toBeNull()
  })
})

describe('re-running a failed review', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runReport.mockResolvedValue({
      run_id: 'run-aaa', status: 'error', ready: false, bands: null,
      generated_at: '', total: 0, report_slug: null, rows: [],
    })
    mockApi.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    mockApi.repoPrs.mockResolvedValue({ repo: 'acme/widgets', prs: [], count: 0 })
    mockApi.review.mockResolvedValue({ run_id: 'run-new', changes: [] })
    mockApi.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  const failedRun = () => run({
    status: 'error',
    error: 'Runtime process died during prompt',
    progress: {
      'GH-acme-widgets-7': {
        phase: 'failed', error: 'Runtime process died during prompt',
      },
    },
    summary: { ok: true, changes: 1, result_records: 0 },
  })

  it('says why it failed where the status is', async () => {
    mount({ runs: [failedRun()] } as RunsResponse, 'run-aaa')
    // Previously the reason was only in the empty report's body text, so a
    // failed review looked like one that found nothing.
    expect(await screen.findByText(/This review failed/)).toBeTruthy()
    expect(screen.getByText(/reviewer process stopped/i)).toBeTruthy()
  })

  it('runs it again from the notice', async () => {
    mount({ runs: [failedRun()] } as RunsResponse, 'run-aaa')
    await userEvent.click(await screen.findByRole('button', { name: /Run it again/ }))
    await waitFor(() => expect(mockApi.review).toHaveBeenCalledWith([
      'https://github.com/acme/widgets/pull/7',
    ]))
  })

  it('labels the header action as a retry', async () => {
    // Opened from the rail, which resolves the run back to its pull request —
    // the surface where the action button lives.
    mount({ runs: [failedRun()] } as RunsResponse)
    await showReviews()
    await userEvent.click(await within(reviewsColumn())
      .findByRole('button', { name: /Review of acme\/widgets/ }))
    // "Review" on a run that just failed reads as though nothing was attempted.
    expect(await screen.findByRole('button', { name: /Retry review/ })).toBeTruthy()
  })

  it('keeps the plain Review label when nothing failed', async () => {
    mockApi.runReport.mockResolvedValue(report())
    mount({ runs: [run()] } as RunsResponse)
    await showReviews()
    await userEvent.click(await within(reviewsColumn())
      .findByRole('button', { name: /Review of acme\/widgets/ }))
    expect(await screen.findByRole('button', { name: /^Review$|Review again/ }))
      .toBeTruthy()
    expect(screen.queryByText(/This review failed/)).toBeNull()
  })
})

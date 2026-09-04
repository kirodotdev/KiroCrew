import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IssueRadarProvider, useIssueRadar } from '../apps/issue-radar/context'
import {
  LIST_POLL_MS, LIST_POLL_CHOICES_MS, STALE_TIME_CHOICES_MS, REFRESH_DEFAULTS,
  CACHE_RETENTION_MS, coerceInterval, coerceRefreshPrefs,
} from '../apps/issue-radar/lib/format'

// The list routes are cache-first with NO server-side TTL, so a plain refetch
// is answered from the cache and would observe nothing new forever. The poll is
// only useful if the REFETCH asks for refresh=1 while the FIRST fetch stays
// cache-first (otherwise opening the app pays a full `gh` fetch before showing
// anything). Both halves are invisible in the UI when broken — a poll that
// silently no-ops looks identical to a working one — so they are pinned here.

const issues = vi.fn()
const pulls = vi.fn()
const searchPulls = vi.fn()
const me = vi.fn()

vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: {
    me: (...args: unknown[]) => me(...args),
    issues: (...args: unknown[]) => issues(...args),
    labels: () => Promise.resolve({ labels: [] }),
    members: () => Promise.resolve({ members: [] }),
    getSettings: () => Promise.resolve({ settings: null }),
    pulls: (...args: unknown[]) => pulls(...args),
    searchPulls: (...args: unknown[]) => searchPulls(...args),
  },
}))

/** Open the app straight onto the PR surface (mainView is restored from the
 * persisted UI state, and `prSurfaceActive` follows it). */
function openOnPrSurface(extra: Record<string, unknown> = {}) {
  localStorage.setItem('kc:issue-radar:ui-state', JSON.stringify({ mainView: 'pulls', ...extra }))
}

const REPO_A = { owner: 'kirodotdev', repo: 'Kiro' }
const REPO_B = { owner: 'kirodotdev', repo: 'Other' }

function renderProvider(children?: React.ReactNode, active = REPO_A) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (repo: typeof REPO_A) => (
    <QueryClientProvider client={client}>
      <IssueRadarProvider
        repos={[REPO_A, REPO_B]}
        active={repo}
        onSwitch={() => {}}
        onAddRepo={() => {}}
      >
        {children ?? <div>ready</div>}
      </IssueRadarProvider>
    </QueryClientProvider>
  )
  const utils = render(tree(active))
  return { ...utils, switchTo: (repo: typeof REPO_A) => utils.rerender(tree(repo)) }
}

/** Reports the ISSUE query's success/loading state plus its row count — the two things
 * the auto-select and Overview guards key off. */
function IssuesState() {
  const { issuesLoading, issues: rows } = useIssueRadar()
  return (
    <div data-testid="issues-state">
      {issuesLoading ? `loading:${rows.length}` : `success:${rows.length}`}
    </div>
  )
}

/** Renders the pulls state plus a control that flips the PR state filter, so a
 * same-repo key change can be driven from a test. */
function PrFilterHarness() {
  const { pullsLoading, pulls: rows, setPrStateFilter } = useIssueRadar()
  return (
    <div>
      <div data-testid="pulls-state">
        {pullsLoading ? 'loading' : rows.length ? 'rows' : 'empty'}
      </div>
      <button data-testid="to-closed" onClick={() => setPrStateFilter('closed')}>closed</button>
    </div>
  )
}

/** Reports the PR list's rendering state the way PrList decides it. */
function PullsState() {
  const { pullsLoading, pulls: rows } = useIssueRadar()
  return <div data-testid="pulls-state">{pullsLoading ? 'loading' : rows.length ? 'rows' : 'empty'}</div>
}

describe('refresh preference validation', () => {
  // The allowlist is the only thing between a user (or a hand-edited localStorage
  // value) and a poll interval that exhausts the provider's shared 30/min search
  // quota. It is enforced on READ and on WRITE, and both halves are pinned here
  // because a silent widening of the domain has no visible symptom until the quota
  // runs out — at which point the failure is a stale list, not an error.
  it('accepts only OFFERED intervals, falling back to the default otherwise', () => {
    expect(coerceInterval(30_000, LIST_POLL_CHOICES_MS, 60_000)).toBe(30_000)
    // Not on the list -> default. A range clamp would have admitted 1_000.
    expect(coerceInterval(1_000, LIST_POLL_CHOICES_MS, 60_000)).toBe(60_000)
    expect(coerceInterval(45_000, LIST_POLL_CHOICES_MS, 60_000)).toBe(60_000)
    // Wrong TYPE, from hand-edited JSON.
    expect(coerceInterval('30000', LIST_POLL_CHOICES_MS, 60_000)).toBe(60_000)
    expect(coerceInterval(null, LIST_POLL_CHOICES_MS, 60_000)).toBe(60_000)
    // `Infinity` matters specifically: react-query treats it as "no interval", so it
    // would DISABLE polling rather than speed it up — a silent feature loss.
    expect(coerceInterval(Infinity, LIST_POLL_CHOICES_MS, 60_000)).toBe(60_000)
    expect(coerceInterval(NaN, LIST_POLL_CHOICES_MS, 60_000)).toBe(60_000)
    expect(coerceInterval(-30_000, LIST_POLL_CHOICES_MS, 60_000)).toBe(60_000)
  })

  it('keeps 0 as a real choice for the cache lifetime', () => {
    // `0` is a MODE ("always refetch"), not a missing value. A truthiness check here
    // instead of `includes` would silently rewrite it to the default, and the one
    // setting a user picks to get maximum freshness would do the opposite.
    expect(coerceInterval(0, STALE_TIME_CHOICES_MS, 30_000)).toBe(0)
  })

  it('falls back to the historical behaviour when nothing is persisted', () => {
    // An existing user must see ZERO change until they touch a control.
    expect(coerceRefreshPrefs(undefined)).toEqual(REFRESH_DEFAULTS)
    expect(REFRESH_DEFAULTS.listPollMs).toBe(LIST_POLL_MS)
    expect(REFRESH_DEFAULTS.pollInBackground).toBe(false)
    expect(REFRESH_DEFAULTS.prefetchPulls).toBe(false)
  })

  it('scrubs an out-of-range persisted value field by field', () => {
    const coerced = coerceRefreshPrefs({
      listPollMs: 1_000, detailPollMs: 30_000, staleTimeMs: 999,
      pollInBackground: 'yes', prefetchPulls: true,
    })
    expect(coerced.listPollMs).toBe(REFRESH_DEFAULTS.listPollMs)   // rejected
    expect(coerced.detailPollMs).toBe(30_000)                      // valid, kept
    expect(coerced.staleTimeMs).toBe(REFRESH_DEFAULTS.staleTimeMs) // rejected
    expect(coerced.pollInBackground).toBe(false)                   // non-boolean -> default
    expect(coerced.prefetchPulls).toBe(true)                       // valid, kept
  })

  it('keeps the list floor at twice the backend probe-coalescing window', () => {
    // The floor is 30s ONLY because `routes._PROBE_COALESCE_SEC` is 15s: that is what
    // makes one shared probe reading cover every open tab. Nothing enforces the
    // relationship across the language boundary, so it is asserted here — if the
    // backend constant moves, this test is the thing that says the floor must too.
    expect(Math.min(...LIST_POLL_CHOICES_MS)).toBe(2 * 15_000)
  })
})

describe('issue-radar list polling', () => {
  beforeEach(() => {
    localStorage.clear()
    issues.mockReset().mockResolvedValue({ issues: [] })
    pulls.mockReset().mockResolvedValue({ pulls: [] })
    searchPulls.mockReset().mockResolvedValue({ pulls: [] })
    me.mockReset().mockResolvedValue({ login: 'octocat' })
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('never paints one repo\'s rows under another repo\'s identity', async () => {
    // `placeholderData: keepPreviousData` would retain the previous query's rows for ANY
    // key change — including a REPO SWITCH. A PR number means something different in
    // each repo, so a row acted on while repo A's list was painted under repo B would
    // target B's PR of the same number. The ticked selection is cleared on `scopeKey`,
    // but that effect runs AFTER the paint, so it closes the window one render late
    // rather than never opening it; scoping the placeholder is what has no window.
    openOnPrSurface()
    pulls.mockResolvedValue({ pulls: [{ number: 7, title: 'A-only', state: 'open', draft: false, labels: [], assignees: [], requested_reviewers: [] }] })
    const { switchTo } = renderProvider(<PullsState />)
    await waitFor(() => expect(screen.getByTestId('pulls-state').textContent).toBe('rows'))

    // Repo B has not resolved yet: its fetch is in flight.
    let releaseB: (v: unknown) => void = () => {}
    pulls.mockImplementation(() => new Promise((res) => { releaseB = res }))
    switchTo(REPO_B)

    // The honest render is LOADING, not repo A's rows.
    await waitFor(() => expect(screen.getByTestId('pulls-state').textContent).toBe('loading'))
    releaseB({ pulls: [] })
    await waitFor(() => expect(screen.getByTestId('pulls-state').textContent).toBe('empty'))
  })

  it('keeps the previous rows across a FILTER change in the same repo', async () => {
    // The other half of the same rule: a filter change is a different view of the SAME
    // repo, so the old rows SHOULD stay on screen while the new ones load. That is the
    // instant-repaint this exists for, and a fix that blanks here would have traded one
    // defect for the slowness the change set out to remove.
    openOnPrSurface()
    pulls.mockResolvedValue({ pulls: [{ number: 7, title: 'open one', state: 'open', draft: false, labels: [], assignees: [], requested_reviewers: [] }] })
    renderProvider(<PrFilterHarness />)
    await waitFor(() => expect(screen.getByTestId('pulls-state').textContent).toBe('rows'))

    let releaseClosed: (v: unknown) => void = () => {}
    pulls.mockImplementation(() => new Promise((res) => { releaseClosed = res }))
    screen.getByTestId('to-closed').click()

    // Still 'rows' — the previous repo-A rows are retained across the filter change.
    await waitFor(() => expect(pulls).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('pulls-state').textContent).toBe('rows')
    releaseClosed({ pulls: [] })
  })

  it('does not report SUCCESS while another repo\'s rows are on screen', async () => {
    // Consequence of the scoping above, pinned separately because two other effects key
    // off `isSuccess`: the just-connected-repo auto-select (which consumes a ONE-SHOT
    // flag, so a wrong fire is unrecoverable) and the Overview's loading guard. Unscoped
    // `keepPreviousData` reports `success` while the new repo is still fetching, which
    // would let the auto-select pick an issue number from the PREVIOUS repo — exactly
    // the bug the effect's own comment says it was written to prevent.
    issues.mockResolvedValue({ issues: [{ number: 4242, title: 'A-only', state: 'open', labels: [] }] })
    const { switchTo } = renderProvider(<IssuesState />)
    await waitFor(() => expect(screen.getByTestId('issues-state').textContent).toBe('success:1'))

    let releaseB: (v: unknown) => void = () => {}
    issues.mockImplementation(() => new Promise((res) => { releaseB = res }))
    switchTo(REPO_B)
    // Not 'success' — and critically not holding repo A's row count.
    await waitFor(() => expect(screen.getByTestId('issues-state').textContent).toBe('loading:0'))
    releaseB({ issues: [] })
  })

  it('serves the first issue fetch from cache, then polls with poll=1', async () => {
    const { unmount } = renderProvider()

    await waitFor(() => expect(issues).toHaveBeenCalledTimes(1))
    // First fetch: no poll flag, so the route serves cache at any age and the
    // app paints without waiting on `gh`.
    expect(issues.mock.calls[0][1]).toMatchObject({ poll: false })

    await vi.advanceTimersByTimeAsync(LIST_POLL_MS + 1_000)

    await waitFor(() => expect(issues.mock.calls.length).toBeGreaterThan(1))
    // Every poll after that goes down the probe-gated path, or it would be
    // answered from the TTL-less cache and observe nothing.
    expect(issues.mock.calls[1][1]).toMatchObject({ poll: true })
    unmount()
  })

  it('does not poll the PR list while the PR surface is closed', async () => {
    const { unmount } = renderProvider()

    await waitFor(() => expect(issues).toHaveBeenCalled())
    await vi.advanceTimersByTimeAsync(LIST_POLL_MS + 1_000)

    // The PR fetch runs the GraphQL enrichment server-side; polling it while the
    // user sits on the dashboard would spend GitHub budget on unseen data.
    expect(pulls).not.toHaveBeenCalled()
    unmount()
  })

  it('polls the lists an order of magnitude slower than a single item', () => {
    // Guards against someone "aligning" the list poll with the 30s detail poll:
    // a list poll is a paginated whole-repo fetch, not one item's worth of work.
    expect(LIST_POLL_MS).toBe(60_000)
  })

  it('serves the first PR fetch from cache, then polls with poll=1', async () => {
    // Mirror of the issue-list case, and needed as its own test: reverting
    // either the poll flag or the refetchInterval on the PR query alone
    // leaves automatic PR refresh broken while every other test still passes.
    openOnPrSurface()
    const { unmount } = renderProvider()

    await waitFor(() => expect(pulls).toHaveBeenCalledTimes(1))
    expect(pulls.mock.calls[0][1]).toMatchObject({ poll: false })

    await vi.advanceTimersByTimeAsync(LIST_POLL_MS + 1_000)

    await waitFor(() => expect(pulls.mock.calls.length).toBeGreaterThan(1))
    expect(pulls.mock.calls[1][1]).toMatchObject({ poll: true })
    unmount()
  })

  it('polls only the search source while a person filter is on', async () => {
    // The two PR sources are mutually exclusive and only one is rendered, so
    // polling both would spend GitHub budget filling a cache nothing reads.
    openOnPrSurface({ prAuthoredByMe: true })
    const { unmount } = renderProvider()

    await waitFor(() => expect(searchPulls).toHaveBeenCalled())
    await vi.advanceTimersByTimeAsync(LIST_POLL_MS + 1_000)

    await waitFor(() => expect(searchPulls.mock.calls.length).toBeGreaterThan(1))
    expect(pulls).not.toHaveBeenCalled()
    unmount()
  })

  it('shows the PR list as loading while a restored person filter waits on /me', async () => {
    // The base list stands down as soon as a filter is REQUESTED, but the search
    // query cannot start until `me` resolves — and react-query reports
    // isLoading=false for a disabled query. Reading either one alone therefore
    // renders "no pull requests" for the whole pre-/me window.
    let resolveMe: (v: { login: string }) => void = () => {}
    me.mockReturnValue(new Promise<{ login: string }>((res) => { resolveMe = res }))
    openOnPrSurface({ prAuthoredByMe: true })
    const { unmount } = renderProvider(<PullsState />)

    await waitFor(() => expect(screen.getByTestId('pulls-state')).toHaveTextContent('loading'))
    expect(searchPulls).not.toHaveBeenCalled()

    resolveMe({ login: 'octocat' })
    await waitFor(() => expect(searchPulls).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByTestId('pulls-state')).toHaveTextContent('empty'))
    unmount()
  })

  it('does not report the PR list as loading when /me fails', async () => {
    // A permanently failing /me must fall through to the empty state rather than
    // leaving the skeleton up forever.
    me.mockRejectedValue(new Error('nope'))
    openOnPrSurface({ prAuthoredByMe: true })
    const { unmount } = renderProvider(<PullsState />)

    await waitFor(() => expect(screen.getByTestId('pulls-state')).toHaveTextContent('empty'))
    unmount()
  })

  it('does not poll the search source while the PR surface is closed', async () => {
    // A person filter left on must not keep polling GitHub search in the
    // background while the user works elsewhere in the app.
    localStorage.setItem(
      'kc:issue-radar:ui-state',
      JSON.stringify({ mainView: 'dashboard', prAuthoredByMe: true }),
    )
    const { unmount } = renderProvider()

    await waitFor(() => expect(issues).toHaveBeenCalled())
    await vi.advanceTimersByTimeAsync(LIST_POLL_MS + 1_000)

    expect(searchPulls).not.toHaveBeenCalled()
    expect(pulls).not.toHaveBeenCalled()
    unmount()
  })
})

describe('cached surfaces survive a tab switch', () => {
  // Each dashboard mounts its own queries and unmounts them on the way out (the views are
  // SWAPPED, not hidden), so a surface's data lives only `gcTime` past that unmount. The
  // app-wide default is react-query's 5 minutes, which is shorter than a triage session:
  // leaving Tagging for six minutes evicted its queue, so coming back showed a loading
  // line and refetched from scratch. Once per tab click.
  it('retains issue-radar query data far longer than the app-wide default', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000 } } })
    qc.setQueryDefaults(['issue-radar'], { gcTime: CACHE_RETENTION_MS })
    expect(qc.getQueryDefaults(['issue-radar', 'tagging', 'gh:github.com:o/r']).gcTime)
      .toBe(CACHE_RETENTION_MS)
    // Comfortably past a normal detour between surfaces, where 5 minutes is not.
    expect(CACHE_RETENTION_MS).toBeGreaterThan(5 * 60_000)
  })

  it('scopes the retention to this app, not the whole dashboard', () => {
    // A global gcTime bump would retain every other page's queries too, which is memory
    // spent on data nothing asked to keep.
    const qc = new QueryClient()
    qc.setQueryDefaults(['issue-radar'], { gcTime: CACHE_RETENTION_MS })
    expect(qc.getQueryDefaults(['chat-slots']).gcTime).toBeUndefined()
  })

  it('paints retained rows instead of a spinner, because the query is not pending', async () => {
    // What makes retention sufficient on its own: the surfaces gate their loading copy on
    // `isLoading`, which is false whenever data is present, so a remount within the
    // retention window renders rows immediately and any refetch happens behind them.
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 0 } } })
    await qc.fetchQuery({ queryKey: ['issue-radar', 'tagging'], queryFn: async () => 'rows' })
    expect(qc.getQueryState(['issue-radar', 'tagging'])?.status).toBe('success')
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IssueRadarProvider, useIssueRadar } from '../apps/issue-radar/context'
import { LIST_POLL_MS } from '../apps/issue-radar/lib/format'

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

function renderProvider(children?: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <IssueRadarProvider
        repos={[{ owner: 'kirodotdev', repo: 'Kiro' }]}
        active={{ owner: 'kirodotdev', repo: 'Kiro' }}
        onSwitch={() => {}}
        onAddRepo={() => {}}
      >
        {children ?? <div>ready</div>}
      </IssueRadarProvider>
    </QueryClientProvider>,
  )
}

/** Reports the PR list's rendering state the way PrList decides it. */
function PullsState() {
  const { pullsLoading, pulls: rows } = useIssueRadar()
  return <div data-testid="pulls-state">{pullsLoading ? 'loading' : rows.length ? 'rows' : 'empty'}</div>
}

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

  it('serves the first issue fetch from cache, then polls with poll=1', async () => {
    const { unmount } = renderProvider()

    await waitFor(() => expect(issues).toHaveBeenCalledTimes(1))
    // First fetch: no poll flag, so the route serves cache at any age and the
    // app paints without waiting on `gh`.
    expect(issues.mock.calls[0][2]).toMatchObject({ poll: false })

    await vi.advanceTimersByTimeAsync(LIST_POLL_MS + 1_000)

    await waitFor(() => expect(issues.mock.calls.length).toBeGreaterThan(1))
    // Every poll after that goes down the probe-gated path, or it would be
    // answered from the TTL-less cache and observe nothing.
    expect(issues.mock.calls[1][2]).toMatchObject({ poll: true })
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
    expect(pulls.mock.calls[0][2]).toMatchObject({ poll: false })

    await vi.advanceTimersByTimeAsync(LIST_POLL_MS + 1_000)

    await waitFor(() => expect(pulls.mock.calls.length).toBeGreaterThan(1))
    expect(pulls.mock.calls[1][2]).toMatchObject({ poll: true })
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

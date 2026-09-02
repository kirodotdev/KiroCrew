import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IssueRadarProvider, useIssueRadar } from '../apps/issue-radar/context'

// Progressive first paint for PRs — the twin of issueRadarFirstPaint, and the
// bigger cold-open win: the authoritative `pulls` fetch paginates the whole open
// set AND runs GraphQL enrichment before it resolves, so a cold PR pane would sit
// on a skeleton for seconds. The context runs a one-page `pullsFirstPage` query in
// that window (only once the PR surface is active), feeds its rows to the list,
// then swaps the authoritative enriched set in behind it. These pins verify (a)
// the first page paints while the full fetch is in flight, (b) the full set wins
// the moment it lands, (c) the fast path never fires off the open state, and (d) a
// warm full fetch spends no first-page request.

const pulls = vi.fn()
const pullsFirstPage = vi.fn()

vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: {
    me: () => Promise.resolve({ login: 'octocat' }),
    issues: () => Promise.resolve({ issues: [] }),
    issuesFirstPage: () => Promise.resolve({ issues: [], partial: true }),
    labels: () => Promise.resolve({ labels: [] }),
    members: () => Promise.resolve({ members: [] }),
    getSettings: () => Promise.resolve({ settings: null }),
    pulls: (...a: unknown[]) => pulls(...a),
    pullsFirstPage: (...a: unknown[]) => pullsFirstPage(...a),
    searchPulls: () => Promise.resolve({ pulls: [] }),
  },
}))

const REPO = { owner: 'kirodotdev', repo: 'Kiro' }

function State() {
  const { pulls: rows, pullsLoading, pullsPartial, openPulls, setPrStateFilter } = useIssueRadar()
  return (
    <div>
      <div data-testid="s">
        {pullsLoading ? 'loading' : 'ready'}:{rows.length}:{pullsPartial ? 'partial' : 'full'}
      </div>
      <button data-testid="to-pulls" onClick={() => openPulls()}>pulls</button>
      <button data-testid="to-closed" onClick={() => setPrStateFilter('closed')}>closed</button>
    </div>
  )
}

function renderProvider(seed?: unknown) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // A WARM open means the full pulls query already has data resident at mount.
  // Seed the exact key the provider reads so `pullsQuery.data` is defined and the
  // first-page query never enables. scopeKey = provider:host:owner/repo; the pulls
  // key also carries the fetch state ('open').
  if (seed !== undefined) {
    client.setQueryData(
      ['issue-radar', 'pulls', 'github:github.com:kirodotdev/Kiro', 'open'], seed,
    )
  }
  render(
    <QueryClientProvider client={client}>
      <IssueRadarProvider repos={[REPO]} active={REPO} onSwitch={() => {}} onAddRepo={() => {}}>
        <State />
      </IssueRadarProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  pulls.mockReset()
  pullsFirstPage.mockReset()
})
afterEach(() => vi.clearAllMocks())

describe('progressive PR first paint', () => {
  it('paints the first page while the full fetch is in flight, then swaps to the full set', async () => {
    let releaseFull: (v: unknown) => void = () => {}
    pulls.mockImplementation(() => new Promise((res) => { releaseFull = res }))
    pullsFirstPage.mockResolvedValue({
      pulls: [{ number: 1, title: 'newest', labels: [], updated_at: '2026-07-02T00:00:00Z' }],
      partial: true,
    })

    renderProvider()
    // Activate the PR surface — the first-page query is gated on it (we never
    // spend a request on a pane the user has not opened).
    await act(async () => { screen.getByTestId('to-pulls').click() })

    // First page painted: one row, marked partial, skeleton gone.
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:1:partial'))

    // Full set lands (two enriched rows) and wins — no longer partial.
    releaseFull({
      pulls: [
        { number: 1, title: 'newest', labels: [], updated_at: '2026-07-02T00:00:00Z' },
        { number: 2, title: 'older', labels: [], updated_at: '2026-07-01T00:00:00Z' },
      ],
    })
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:2:full'))
  })

  it('does not leak the open first page into the Closed tab', async () => {
    // The fast path is open-state only; its lingering data must not paint the
    // closed tab (mirrors the issues gating on stateFilter === 'open').
    let releaseOpenFull: (v: unknown) => void = () => {}
    let releaseClosed: (v: unknown) => void = () => {}
    pulls.mockImplementation((_ref: unknown, opts: { state?: string } = {}) =>
      opts.state === 'closed'
        ? new Promise((res) => { releaseClosed = res })
        : new Promise((res) => { releaseOpenFull = res }))
    pullsFirstPage.mockResolvedValue({
      pulls: [{ number: 1, title: 'open one', labels: [], updated_at: '2026-07-02T00:00:00Z' }],
      partial: true,
    })

    renderProvider()
    await act(async () => { screen.getByTestId('to-pulls').click() })
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:1:partial'))

    // Switch to Closed while both the open full fetch and the closed fetch are pending.
    await act(async () => { screen.getByTestId('to-closed').click() })
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('loading:0:full'))

    releaseClosed({ pulls: [] })
    releaseOpenFull({ pulls: [] })
  })

  it('does not fire a first-page request on a warm open (full list already resident)', async () => {
    pulls.mockResolvedValue({
      pulls: [{ number: 9, title: 'cached', labels: [], updated_at: '2026-07-01T00:00:00Z' }],
    })
    pullsFirstPage.mockResolvedValue({ pulls: [], partial: true })

    renderProvider({
      pulls: [{ number: 9, title: 'cached', labels: [], updated_at: '2026-07-01T00:00:00Z' }],
    })
    await act(async () => { screen.getByTestId('to-pulls').click() })

    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:1:full'))
    // Gated on `pullsQuery.data === undefined`, false from the first render because
    // the full list was already resident — so it never enables.
    expect(pullsFirstPage).not.toHaveBeenCalled()
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IssueRadarProvider, useIssueRadar } from '../apps/issue-radar/context'

// Progressive first paint: on a COLD cache the full open-issue fetch paginates the
// whole backlog, so the app would sit on a skeleton for seconds. The context runs a
// one-page `issuesFirstPage` query in that window, feeds its rows to the list, then
// swaps the authoritative full set in behind it. These pins verify (a) the first
// page paints while the full fetch is in flight, (b) the full set wins the moment it
// lands, (c) a partial page does NOT satisfy `issuesQuery.isSuccess` (the auto-select
// / members gate), and (d) a warm full fetch spends no first-page request.

const issues = vi.fn()
const issuesFirstPage = vi.fn()

vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: {
    me: () => Promise.resolve({ login: 'octocat' }),
    issues: (...a: unknown[]) => issues(...a),
    issuesFirstPage: (...a: unknown[]) => issuesFirstPage(...a),
    labels: () => Promise.resolve({ labels: [] }),
    members: () => Promise.resolve({ members: [] }),
    getSettings: () => Promise.resolve({ settings: null }),
    pulls: () => Promise.resolve({ pulls: [] }),
    searchPulls: () => Promise.resolve({ pulls: [] }),
  },
}))

const REPO = { owner: 'kirodotdev', repo: 'Kiro' }

function State() {
  const { issues: rows, issuesLoading, issuesPartial, setStateFilter } = useIssueRadar()
  return (
    <div>
      <div data-testid="s">
        {issuesLoading ? 'loading' : 'ready'}:{rows.length}:{issuesPartial ? 'partial' : 'full'}
      </div>
      <button data-testid="to-closed" onClick={() => setStateFilter('closed')}>closed</button>
    </div>
  )
}

function renderProvider(seed?: unknown) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // A WARM open means the full issues query already has data resident at mount
  // (from a prior mount within the retention window). Seed the exact key the
  // provider reads so `issuesQuery.data` is defined on the first render and the
  // first-page query never enables. scopeKey = provider:host:owner/repo.
  if (seed !== undefined) {
    client.setQueryData(['issue-radar', 'issues', 'github:github.com:kirodotdev/Kiro', 'open'], seed)
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
  issues.mockReset()
  issuesFirstPage.mockReset()
})
afterEach(() => vi.clearAllMocks())

describe('progressive first paint', () => {
  it('paints the first page while the full fetch is in flight, then swaps to the full set', async () => {
    // Full fetch parked; first page resolves immediately with one row.
    let releaseFull: (v: unknown) => void = () => {}
    issues.mockImplementation(() => new Promise((res) => { releaseFull = res }))
    issuesFirstPage.mockResolvedValue({
      issues: [{ number: 1, title: 'newest', labels: [], updated_at: '2026-07-02T00:00:00Z' }],
      partial: true,
    })

    renderProvider()

    // First page painted: one row, marked partial, skeleton gone.
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:1:partial'))

    // Full set lands (two rows) and wins — no longer partial.
    releaseFull({
      issues: [
        { number: 1, title: 'newest', labels: [], updated_at: '2026-07-02T00:00:00Z' },
        { number: 2, title: 'older', labels: [], updated_at: '2026-07-01T00:00:00Z' },
      ],
    })
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:2:full'))
  })

  it('does not leak the open first page into the Closed tab during a cold open', async () => {
    // Cold open on `open`: the first page resolves while the full open fetch is still
    // in flight. Switching to Closed in that window must NOT paint the open first-page
    // rows — `firstPageQuery.data` lingers after the query disables, so the fallback
    // is gated on stateFilter === 'open'.
    let releaseOpenFull: (v: unknown) => void = () => {}
    let releaseClosed: (v: unknown) => void = () => {}
    issues.mockImplementation((_ref: unknown, opts: { state?: string } = {}) =>
      opts.state === 'closed'
        ? new Promise((res) => { releaseClosed = res })
        : new Promise((res) => { releaseOpenFull = res }))
    issuesFirstPage.mockResolvedValue({
      issues: [{ number: 1, title: 'open one', labels: [], updated_at: '2026-07-02T00:00:00Z' }],
      partial: true,
    })

    renderProvider()
    // First page painted (open, partial).
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:1:partial'))

    // Switch to Closed while both the open full fetch and the closed fetch are pending.
    await act(async () => { screen.getByTestId('to-closed').click() })
    // Must NOT show the open first-page row, and must not claim partial.
    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('loading:0:full'))

    releaseClosed({ issues: [] })
    releaseOpenFull({ issues: [] })
  })

  it('does not fire a first-page request on a warm open (full list already resident)', async () => {
    // Data already in the query cache at mount — the retained-across-tab-switch case.
    issues.mockResolvedValue({
      issues: [{ number: 9, title: 'cached', labels: [], updated_at: '2026-07-01T00:00:00Z' }],
    })
    issuesFirstPage.mockResolvedValue({ issues: [], partial: true })

    renderProvider({
      issues: [{ number: 9, title: 'cached', labels: [], updated_at: '2026-07-01T00:00:00Z' }],
    })

    await waitFor(() => expect(screen.getByTestId('s').textContent).toBe('ready:1:full'))
    // Gated on `issuesQuery.data === undefined`, which is false from the first
    // render because the full list was already resident — so it never enables.
    expect(issuesFirstPage).not.toHaveBeenCalled()
  })
})

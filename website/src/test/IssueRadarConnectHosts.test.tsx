// Regression coverage for the two Issue Radar connect behaviours that live in
// the HOSTS rather than the shared panel, and whose failure modes are silent:
//
//   1. ConnectRepoModal refuses to dismiss while a connect is in flight.
//      Closing only unmounts the UI — it does NOT cancel the sequential POST
//      loop — so a dismissable modal keeps connecting repos (and still fires
//      its success callback) after the user believes they cancelled.
//   2. The provider selects the first issue of the JUST-CONNECTED repo once its
//      list resolves. Without it a successful connect lands the user on a blank
//      detail pane; with an unscoped intent it selects an issue from the repo
//      that was active before.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { MemoryRouter } from 'react-router-dom'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

const mockApi = {
  connect: vi.fn(),
  recentRepos: vi.fn(),
  issues: vi.fn(),
  labels: vi.fn(),
  members: vi.fn(),
  getSettings: vi.fn(),
  me: vi.fn(),
  pulls: vi.fn(),
  searchPulls: vi.fn(),
  repos: vi.fn(),
  issueDetail: vi.fn(),
  getIssueAi: vi.fn(),
  disconnect: vi.fn(),
  getRecommendations: vi.fn(),
}
// Partial mock: the provider also imports real CONSTANTS from this module
// (DEFAULT_REPO_SETTINGS et al), so only the client object is replaced.
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  issueRadarApi: mockApi,
}))

const { default: ConnectRepoModal } = await import('../apps/issue-radar/ConnectRepoModal')
const { default: IssueRadarPage } = await import('../apps/issue-radar/IssueRadarPage')
const { default: RepoSettings } = await import('../apps/issue-radar/views/settings/RepoSettings')
const { default: WelcomeCarousel } = await import('../apps/issue-radar/WelcomeCarousel')
const { IssueRadarProvider, useIssueRadar } = await import('../apps/issue-radar/context')
const { markAutoSelectFirstIssue, UI_STATE_KEY } = await import('../apps/issue-radar/lib/format')

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

/** The full page mounts <Workspace>, whose Investigate button reaches for the
 * app's Redux store and the router — so the page-level test needs both. */
function wrapFullApp(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  return render(
    <Provider store={store}>
      <MemoryRouter>
        <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
      </MemoryRouter>
    </Provider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  for (const fn of Object.values(mockApi)) fn.mockReset()
  mockApi.recentRepos.mockResolvedValue({
    repos: [
      { full_name: 'o/alpha', owner: 'o', repo: 'alpha', connected: false, contribution_count: 1 },
      { full_name: 'o/beta', owner: 'o', repo: 'beta', connected: false, contribution_count: 1 },
    ],
    truncated: false,
  })
  mockApi.me.mockResolvedValue({ login: 'octocat' })
  mockApi.labels.mockResolvedValue({ labels: [] })
  mockApi.members.mockResolvedValue({ members: [] })
  mockApi.getSettings.mockResolvedValue({})
  mockApi.pulls.mockResolvedValue({ pulls: [] })
  mockApi.repos.mockResolvedValue({ repos: [] })
  mockApi.issueDetail.mockResolvedValue({ issue: null, timeline: [] })
  mockApi.getIssueAi.mockResolvedValue({ summary: null })
})

/** Renders the modal inside a provider (which it requires) and opens GitHub. */
async function openModal(
  user: ReturnType<typeof userEvent.setup>,
  { onClose = vi.fn(), onConnected = vi.fn() } = {},
) {
  mockApi.issues.mockResolvedValue({ issues: [] })
  wrap(
    <IssueRadarProvider
      repos={[{ owner: 'o', repo: 'existing' }] as never}
      active={{ owner: 'o', repo: 'existing' }}
      onSwitch={vi.fn()}
      onAddRepo={vi.fn()}
    >
      <ConnectRepoModal onConnected={onConnected} onClose={onClose} />
    </IssueRadarProvider>,
  )
  await user.click(screen.getByRole('button', { name: /GitHub/ }))
  await waitFor(() => expect(mockApi.recentRepos).toHaveBeenCalled())
  return { onClose, onConnected }
}

describe('ConnectRepoModal dismissal', () => {
  it('closes on Escape and via the backdrop when idle', async () => {
    const user = userEvent.setup()
    const { onClose } = await openModal(user)

    await user.keyboard('{Escape}')
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))

    await user.click(screen.getByRole('button', { name: 'Close connect dialog' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2))
  })

  it('refuses every dismissal path while a connect is in flight', async () => {
    // A deferred connect holds the mutation open so the guard is observable.
    let release: () => void = () => {}
    mockApi.connect.mockImplementation(
      () => new Promise((res) => { release = () => res({ owner: 'o', repo: 'alpha' }) }),
    )
    const user = userEvent.setup()
    const { onClose, onConnected } = await openModal(user)

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    await user.click(screen.getByRole('button', { name: /^Connect/ }))
    await waitFor(() => expect(mockApi.connect).toHaveBeenCalled())

    // Escape, the backdrop, and the X button must all be inert: the POST loop
    // keeps running regardless of unmount, so closing here would connect repos
    // behind a dismissed dialog.
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Close connect dialog' }))
    expect(screen.getByRole('button', { name: 'Close' })).toBeDisabled()
    expect(onClose).not.toHaveBeenCalled()

    release()
    // Once it finishes, the success path hands control back exactly once.
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith({ owner: 'o', repo: 'alpha' }))
  })
})

describe('post-connect auto-selection', () => {
  /** Surfaces the provider's selected issue so the effect can be asserted. */
  function Probe() {
    const { selectedIssue } = useIssueRadar()
    return <div data-testid="selected">{selectedIssue ?? 'none'}</div>
  }

  function renderProvider(active: { owner: string; repo: string }) {
    return wrap(
      <IssueRadarProvider
        repos={[active] as never}
        active={active}
        onSwitch={vi.fn()}
        onAddRepo={vi.fn()}
      >
        <Probe />
      </IssueRadarProvider>,
    )
  }

  it('opens the first issue of the newly connected repo, per the active sort', async () => {
    // The default sort is number-descending, so #8 is the list's first row —
    // the effect must follow the SORTED order the user actually sees, not the
    // server's response order.
    mockApi.issues.mockResolvedValue({
      issues: [
        { number: 7, title: 'lower number', labels: [], state: 'open', updated_at: '2026-01-02T00:00:00Z', created_at: '2026-01-02T00:00:00Z', assignees: [] },
        { number: 8, title: 'higher number', labels: [], state: 'open', updated_at: '2026-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z', assignees: [] },
      ],
    })
    markAutoSelectFirstIssue({ owner: 'o', repo: 'fresh' })
    renderProvider({ owner: 'o', repo: 'fresh' })

    await waitFor(() => expect(screen.getByTestId('selected')).toHaveTextContent('8'))
  })

  it('leaves selection alone when the active repo is not the connected one', async () => {
    mockApi.issues.mockResolvedValue({
      issues: [
        { number: 99, title: 'old repo issue', labels: [], state: 'open', updated_at: '2026-01-02T00:00:00Z', created_at: '2026-01-02T00:00:00Z', assignees: [] },
      ],
    })
    // The intent belongs to `fresh`, but `stale` is still the active repo while
    // its list refetches — selecting from it would open the wrong repo's issue.
    markAutoSelectFirstIssue({ owner: 'o', repo: 'fresh' })
    renderProvider({ owner: 'o', repo: 'stale' })

    await waitFor(() => expect(mockApi.issues).toHaveBeenCalled())
    expect(screen.getByTestId('selected')).toHaveTextContent('none')
  })
})

describe('IssueRadarPage first connect', () => {
  it('wires onConnected through to the issue view with the first issue open', async () => {
    // End-to-end over the WIRING, not the helper: `onConnected` has to persist
    // the view/filter reset AND record the auto-select intent, and a test that
    // calls markAutoSelectFirstIssue directly stays green if that call is
    // dropped.
    mockApi.repos.mockResolvedValueOnce({ repos: [] })          // first run
      .mockResolvedValue({ repos: [{ owner: 'o', repo: 'fresh' }] })
    mockApi.connect.mockResolvedValue({ owner: 'o', repo: 'fresh' })
    mockApi.issues.mockResolvedValue({
      issues: [
        { number: 5, title: 'only', labels: [], state: 'open', updated_at: '2026-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z', assignees: [] },
      ],
    })
    const user = userEvent.setup()
    wrapFullApp(<IssueRadarPage />)

    // No repos yet → the onboarding carousel. Walk to its connect slide.
    const next = await screen.findByRole('button', { name: /Next/ })
    for (let i = 0; i < 10 && screen.queryByRole('button', { name: /Next/ }); i++) {
      await user.click(screen.getByRole('button', { name: /Next/ }))
    }
    expect(next).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /GitHub/ }))
    await user.type(screen.getByLabelText('Repository URL'), 'https://github.com/o/fresh')
    await user.click(screen.getByRole('button', { name: /^Connect/ }))

    // Lands on the issue LIST (not the dashboard) with the repo's only issue
    // auto-opened — the title therefore appears twice, once per pane.
    await waitFor(() => expect(screen.getAllByText('only').length).toBeGreaterThan(1))
    expect(screen.getByText('1 issue')).toBeInTheDocument()
    // The persisted reset put the workspace on the issues view showing OPEN.
    const persisted = JSON.parse(localStorage.getItem(UI_STATE_KEY) ?? '{}')
    expect(persisted.mainView).toBe('issues')
    expect(persisted.stateFilter).toBe('open')
    expect(persisted.selectedPull).toBeNull()
  })
})

describe('disconnect', () => {
  it('invalidates the connect picker cache as well as the repo list', async () => {
    // The picker caches a `connected` flag per repo. Without this invalidation
    // the just-disconnected repo stays greyed out as "Connected" and
    // un-tickable in the connect dialog until that cache expires.
    mockApi.disconnect.mockResolvedValue({ ok: true })
    mockApi.getRecommendations.mockResolvedValue({ recommendations: null })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    mockApi.issues.mockResolvedValue({ issues: [] })
    const user = userEvent.setup()
    render(
      <QueryClientProvider client={qc}>
        <IssueRadarProvider
          repos={[{ owner: 'o', repo: 'gone' }] as never}
          active={{ owner: 'o', repo: 'gone' }}
          onSwitch={vi.fn()}
          onAddRepo={vi.fn()}
        >
          <RepoSettings repoRef={{ owner: 'o', repo: 'gone' }} />
        </IssueRadarProvider>
      </QueryClientProvider>,
    )

    await user.click(await screen.findByRole('button', { name: /Disconnect/i }))
    await user.click(await screen.findByRole('button', { name: /Confirm disconnect/i }))

    await waitFor(() => expect(mockApi.disconnect).toHaveBeenCalledWith(
      expect.objectContaining({ owner: 'o', repo: 'gone' }),
    ))
    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]))
    expect(keys.some((k) => k.includes('recent-repos'))).toBe(true)
    expect(keys.some((k) => k.includes('[\"issue-radar\",\"repos\"]'))).toBe(true)
  })
})

describe('unmount cancellation', () => {
  it('stops the batch when the host unmounts mid-connect', async () => {
    // The connect loop is sequential and outlives its host, so an SPA
    // navigation must not leave it POSTing the remaining repos invisibly.
    const releases: Array<() => void> = []
    mockApi.connect.mockImplementation(
      () => new Promise((res) => { releases.push(() => res({ owner: 'o', repo: 'alpha' })) }),
    )
    const onConnected = vi.fn()
    const user = userEvent.setup()
    const { onClose } = await openModal(user, { onConnected })
    void onClose

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select o/beta' }))
    await user.click(screen.getByRole('button', { name: /^Connect/ }))
    await waitFor(() => expect(mockApi.connect).toHaveBeenCalledTimes(1))

    // Unmount while the FIRST request is still open, then let it settle.
    cleanup()
    releases[0]()

    // The second target is never dispatched, and completion is never reported.
    await new Promise((r) => setTimeout(r, 50))
    expect(mockApi.connect).toHaveBeenCalledTimes(1)
    expect(onConnected).not.toHaveBeenCalled()
  })
})

describe('WelcomeCarousel Back during a connect', () => {
  it('stays disabled and does not leave the connect slide', async () => {
    // Back only changes the UI — it does not cancel the loop — so allowing it
    // would connect the remaining repos behind a screen the user backed out of.
    let release: () => void = () => {}
    mockApi.connect.mockImplementation(
      () => new Promise((res) => { release = () => res({ owner: 'o', repo: 'alpha' }) }),
    )
    const user = userEvent.setup()
    wrap(<WelcomeCarousel onConnected={vi.fn()} />)

    while (screen.queryByRole('button', { name: /Next/ })) {
      await user.click(screen.getByRole('button', { name: /Next/ }))
    }
    await user.click(screen.getByRole('button', { name: /GitHub/ }))
    await waitFor(() => expect(mockApi.recentRepos).toHaveBeenCalled())
    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    await user.click(screen.getByRole('button', { name: /^Connect/ }))
    await waitFor(() => expect(mockApi.connect).toHaveBeenCalled())

    const back = screen.getByRole('button', { name: /Back/ })
    expect(back).toBeDisabled()
    await user.click(back)
    // Still on the connect slide: the provider list and picker are present.
    expect(screen.getByRole('button', { name: /GitHub/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Repository URL')).toBeInTheDocument()
    release()
  })
})

describe('host card growth', () => {
  // The card's size classes are the ONLY thing that gives the panel room for its
  // two-column body. The hosts used to gate the growth on `provider === 'github'`
  // while the panel switched to two columns for GitLab as well, so picking
  // GitLab put a two-column layout inside a 480px card — which re-stacked, and
  // the repo picker appeared BELOW the provider list with the body scrolling.
  // Both hosts now share `expandsCard`, and both are pinned here because the two
  // copies are exactly what drifted.
  const EXPANDED_W = 'w-[860px]'

  it('ConnectRepoModal grows for GitLab, not only GitHub', async () => {
    const user = userEvent.setup()
    mockApi.issues.mockResolvedValue({ issues: [] })
    wrap(
      <IssueRadarProvider
        repos={[{ owner: 'o', repo: 'existing' }] as never}
        active={{ owner: 'o', repo: 'existing' }}
        onSwitch={vi.fn()}
        onAddRepo={vi.fn()}
      >
        <ConnectRepoModal onConnected={vi.fn()} onClose={vi.fn()} />
      </IssueRadarProvider>,
    )
    const dialog = screen.getByRole('dialog')
    expect(dialog.className).not.toContain(EXPANDED_W)

    await user.click(screen.getByRole('button', { name: /GitLab/ }))
    await waitFor(() => expect(dialog.className).toContain(EXPANDED_W))
  })

  it('WelcomeCarousel grows for GitLab, not only GitHub', async () => {
    const user = userEvent.setup()
    const { container } = wrap(<WelcomeCarousel onConnected={vi.fn()} />)
    while (screen.queryByRole('button', { name: /Next/ })) {
      await user.click(screen.getByRole('button', { name: /Next/ }))
    }
    // The card is the only element carrying the collapsed/expanded size classes.
    const card = container.querySelector('.w-\\[480px\\]')
    expect(card).not.toBeNull()

    await user.click(screen.getByRole('button', { name: /GitLab/ }))
    await waitFor(() => expect((card as HTMLElement).className).toContain(EXPANDED_W))
  })
})

describe('modal live state reset', () => {
  it('clears issue AND pr selection/filters in the mounted provider', async () => {
    // A leftover `selectedPull` is the sharp edge: it's a NUMBER, so #42 from
    // the old repo silently auto-opens the new repo's unrelated #42.
    mockApi.connect.mockResolvedValue({ owner: 'o', repo: 'fresh' })
    mockApi.issues.mockResolvedValue({ issues: [] })

    let seed: (() => void) | null = null
    let seen: { selectedIssue: number | null; selectedPull: number | null; query: string; prQuery: string } | null = null
    function Probe() {
      const c = useIssueRadar()
      seed = () => { c.setSelectedIssue(11); c.setSelectedPull(42); c.setQuery('stale'); c.setPrQuery('stale-pr') }
      seen = {
        selectedIssue: c.selectedIssue, selectedPull: c.selectedPull,
        query: c.query, prQuery: c.prQuery,
      }
      return null
    }
    const user = userEvent.setup()
    wrap(
      <IssueRadarProvider
        repos={[{ owner: 'o', repo: 'existing' }] as never}
        active={{ owner: 'o', repo: 'existing' }}
        onSwitch={vi.fn()}
        onAddRepo={vi.fn()}
      >
        <Probe />
        <ConnectRepoModal onConnected={vi.fn()} onClose={vi.fn()} />
      </IssueRadarProvider>,
    )

    await act(async () => { seed?.() })
    expect(seen?.selectedPull).toBe(42)

    await user.click(screen.getByRole('button', { name: /GitHub/ }))
    await waitFor(() => expect(mockApi.recentRepos).toHaveBeenCalled())
    await user.type(screen.getByLabelText('Repository URL'), 'https://github.com/o/fresh')
    await user.click(screen.getByRole('button', { name: /^Connect/ }))

    await waitFor(() => expect(seen?.selectedPull).toBeNull())
    expect(seen?.selectedIssue).toBeNull()
    expect(seen?.query).toBe('')
    expect(seen?.prQuery).toBe('')
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  IssueRadarProvider, useIssueRadar, type IssueRadarContextValue,
} from '../apps/issue-radar/context'
import { REFRESH_DEFAULTS, UI_STATE_KEY } from '../apps/issue-radar/lib/format'

// Behaviour pins for the Issue Radar data layer (`context.tsx`) that the
// surface-level component tests never reach: the persisted-crew-UI coercions,
// the filter/sort/selection reducers for BOTH lists, the two refresh mutations,
// the bulk-tick rules, the cross-reference stack, and the repo-switch reset.
//
// The provider is driven through the context value itself (a probe component
// captures it) rather than through rendered controls: every branch here is a
// state rule, so asserting on the value is both closer to the contract and
// keeps the harness free of the button clusters the design lint forbids.

const api = {
  me: vi.fn(),
  issues: vi.fn(),
  issuesFirstPage: vi.fn(),
  labels: vi.fn(),
  members: vi.fn(),
  getSettings: vi.fn(),
  pulls: vi.fn(),
  pullsFirstPage: vi.fn(),
  searchPulls: vi.fn(),
  crews: vi.fn(),
}

vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: {
    me: (...a: unknown[]) => api.me(...a),
    issues: (...a: unknown[]) => api.issues(...a),
    issuesFirstPage: (...a: unknown[]) => api.issuesFirstPage(...a),
    labels: (...a: unknown[]) => api.labels(...a),
    members: (...a: unknown[]) => api.members(...a),
    getSettings: (...a: unknown[]) => api.getSettings(...a),
    pulls: (...a: unknown[]) => api.pulls(...a),
    pullsFirstPage: (...a: unknown[]) => api.pullsFirstPage(...a),
    searchPulls: (...a: unknown[]) => api.searchPulls(...a),
    crews: (...a: unknown[]) => api.crews(...a),
  },
}))

const ME = 'octocat'
const ACTIVE = { owner: 'kirodotdev', repo: 'Kiro' }
const OTHER = { owner: 'kirodotdev', repo: 'Other' }
const REPOS = [
  { ...ACTIVE, permissions: { pull: true, triage: true } },
  { ...OTHER, permissions: { pull: true } },
]
/** repoScopeKey(ACTIVE) — provider:host:owner/repo. */
const SCOPE = 'github:github.com:kirodotdev/Kiro'
const CREW_UI_KEY = 'kc:issue-radar:crew-ui'

// #1 authored + assigned to me. #2 authored by a roster member, also assigned to
// me. #3 authored by a stranger who carries a member author_association, which is
// the roster-less fallback the member filters accept.
const ISSUES = [
  {
    number: 1, title: 'Alpha crash', url: '', comments: 0, labels: ['bug'],
    author: ME, assignees: [ME], author_association: 'NONE', updated_at: '2026-07-03T00:00:00Z',
  },
  {
    number: 2, title: 'Beta docs', url: '', comments: 0, labels: ['bug', 'ui'],
    author: 'member1', assignees: [ME], author_association: 'NONE', updated_at: '2026-07-01T00:00:00Z',
  },
  {
    number: 3, title: 'Gamma', url: '', comments: 0, labels: [],
    author: 'stranger', assignees: [], author_association: 'COLLABORATOR', updated_at: '2026-07-02T00:00:00Z',
  },
]
const LABELS = [
  { name: 'ui', color: 'aaaaaa', description: '' },
  { name: 'bug', color: 'bbbbbb', description: '' },
  { name: 'zeta', color: 'cccccc', description: '' },
]
const PULLS = [
  {
    number: 10, title: 'Add widget', url: '', state: 'open', draft: false, labels: ['bug'],
    author: ME, assignees: [ME], requested_reviewers: [ME], author_association: 'NONE',
    merged_at: null, head: 'feat/widget', base: 'main', updated_at: '2026-07-05T00:00:00Z',
  },
  {
    number: 11, title: 'Fix thing', url: '', state: 'open', draft: true, labels: ['ui'],
    author: 'member1', assignees: [], requested_reviewers: [], author_association: 'NONE',
    merged_at: null, head: 'fix/thing', base: 'main', updated_at: '2026-07-04T00:00:00Z',
  },
  {
    number: 12, title: 'Docs pass', url: '', state: 'open', draft: false, labels: [],
    author: 'stranger', assignees: [], requested_reviewers: [], author_association: 'COLLABORATOR',
    merged_at: null, head: 'docs/pass', base: 'main', updated_at: '2026-07-06T00:00:00Z',
  },
]
const CLOSED_PULLS = [
  {
    number: 20, title: 'Merged one', url: '', state: 'closed', draft: false, labels: [],
    author: 'someone', assignees: [], requested_reviewers: [], merged_at: '2026-07-07T00:00:00Z',
    head: 'a', base: 'main', updated_at: '2026-07-07T00:00:00Z',
  },
  {
    number: 21, title: 'Rejected one', url: '', state: 'closed', draft: false, labels: [],
    author: 'someone', assignees: [], requested_reviewers: [], merged_at: null,
    head: 'b', base: 'main', updated_at: '2026-07-08T00:00:00Z',
  },
]
const CREWS = [
  { id: 'c1', name: 'Alpha crew', created_at: '2026-07-01T00:00:00Z' },
  { id: 'c2', name: 'Beta crew', created_at: '2026-07-02T00:00:00Z' },
]

let ctx = null as unknown as IssueRadarContextValue
const onSwitch = vi.fn()

function Probe() {
  ctx = useIssueRadar()
  return null
}

function renderProvider(opts: { seedPulls?: unknown } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (opts.seedPulls !== undefined) {
    client.setQueryData(['issue-radar', 'pulls', SCOPE, 'open'], opts.seedPulls)
  }
  render(
    <QueryClientProvider client={client}>
      <IssueRadarProvider repos={REPOS} active={ACTIVE} onSwitch={onSwitch} onAddRepo={() => {}}>
        <Probe />
      </IssueRadarProvider>
    </QueryClientProvider>,
  )
  return client
}

/** Open straight onto the PR surface — `prSurfaceActive` follows the restored
 * `mainView`, and the PR queries are gated on it. */
function persistUi(state: Record<string, unknown>) {
  localStorage.setItem(UI_STATE_KEY, JSON.stringify(state))
}

/** Run a context action inside act() so the resulting render is flushed. */
async function drive(fn: () => void) {
  await act(async () => { fn() })
}

const issueNumbers = () => ctx.sortedIssues.map((i) => i.number)
const pullNumbers = () => ctx.sortedPulls.map((p) => p.number)

async function readyIssues() {
  await waitFor(() => expect(ctx.issues).toHaveLength(ISSUES.length))
}
async function readyPulls() {
  await waitFor(() => expect(ctx.pulls).toHaveLength(PULLS.length))
}

beforeEach(() => {
  localStorage.clear()
  onSwitch.mockReset()
  for (const fn of Object.values(api)) fn.mockReset()
  api.me.mockResolvedValue({ login: ME })
  api.issues.mockImplementation((_ref: unknown, opts: { state?: string } = {}) =>
    Promise.resolve({ issues: opts.state === 'closed' ? [] : ISSUES }))
  api.issuesFirstPage.mockResolvedValue({ issues: [], partial: false })
  api.labels.mockResolvedValue({ labels: LABELS })
  api.members.mockResolvedValue({ members: [{ login: 'member1', role: 'admin' }] })
  api.getSettings.mockResolvedValue({ settings: null })
  api.pulls.mockImplementation((_ref: unknown, opts: { state?: string } = {}) =>
    Promise.resolve({ pulls: opts.state === 'closed' ? CLOSED_PULLS : PULLS, bulk_max: 5 }))
  api.pullsFirstPage.mockResolvedValue({ pulls: [], partial: false })
  api.searchPulls.mockResolvedValue({ pulls: [PULLS[0]], truncated: true, limit: 100, bulk_max: 7 })
  api.crews.mockResolvedValue({
    crews: CREWS, counts: { on_duty: 2, working: 1, paused: 0 },
    settings: { schema: 1, claim_ttl_hours: 4, needs_human_label: 'needs-human', commit_trailer: 'Crew' },
  })
})

afterEach(() => { vi.clearAllMocks() })

describe('useIssueRadar guard', () => {
  it('throws when read outside the provider', () => {
    function Bare() {
      useIssueRadar()
      return null
    }
    expect(() => render(<Bare />)).toThrow(/must be used within/)
  })
})

describe('persisted crew UI', () => {
  // The roster effect re-points a stale selection one fetch later, so the
  // coercion is only observable while the roster is still in flight.
  function renderWithPendingRoster(stored: string) {
    localStorage.setItem(CREW_UI_KEY, stored)
    api.crews.mockImplementation(() => new Promise(() => {}))
    return renderProvider()
  }

  it('restores a valid crew selection, filter, sort field and direction', async () => {
    renderWithPendingRoster(JSON.stringify({
      crewView: { kind: 'crew', id: 'c2' }, crewFilter: 'paused',
      crewSortKey: 'name', crewSortDir: 'desc',
    }))
    await waitFor(() => expect(ctx.crewView).toEqual({ kind: 'crew', id: 'c2' }))
    expect(ctx.crewFilter).toBe('paused')
    expect(ctx.crewSortKey).toBe('name')
    expect(ctx.crewSortDir).toBe('desc')
  })

  it('drops a crew selection with an unknown kind, and one with no id', async () => {
    renderWithPendingRoster(JSON.stringify({ crewView: { kind: 'squad', id: 'c2' } }))
    await waitFor(() => expect(ctx.crewView).toEqual({ kind: 'none' }))
    localStorage.setItem(CREW_UI_KEY, JSON.stringify({ crewView: { kind: 'crew', id: '' } }))
    renderProvider()
    await waitFor(() => expect(ctx.crewView).toEqual({ kind: 'none' }))
  })

  it('keeps the unselected state for kind:none, and falls back on every bad field', async () => {
    renderWithPendingRoster(JSON.stringify({
      crewView: { kind: 'none' }, crewFilter: 'nope',
      crewSortKey: 'retired', crewSortDir: 'sideways',
    }))
    await waitFor(() => expect(ctx.crewFilter).toBe('all'))
    expect(ctx.crewView).toEqual({ kind: 'none' })
    expect(ctx.crewSortKey).toBe('status')
    expect(ctx.crewSortDir).toBe('asc')
  })

  it('falls back to defaults when the stored blob is not JSON', async () => {
    renderWithPendingRoster('{not json')
    await waitFor(() => expect(ctx.crewFilter).toBe('all'))
    expect(ctx.crewView).toEqual({ kind: 'none' })
    expect(ctx.crewSortKey).toBe('status')
  })

  it('writes the crews UI back to its own key on change', async () => {
    renderProvider()
    await waitFor(() => expect(ctx.crews).toHaveLength(2))
    await drive(() => ctx.setCrewFilter('working'))
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem(CREW_UI_KEY) ?? '{}')).toEqual({
        crewView: { kind: 'crew', id: 'c1' },
        crewFilter: 'working',
        crewSortKey: 'status',
        crewSortDir: 'asc',
      })
    })
  })
})

describe('crew roster', () => {
  it('opens the first crew when nothing valid is selected', async () => {
    localStorage.setItem(CREW_UI_KEY, JSON.stringify({ crewView: { kind: 'crew', id: 'retired' } }))
    renderProvider()
    await waitFor(() => expect(ctx.crewView).toEqual({ kind: 'crew', id: 'c1' }))
    expect(ctx.crewCounts).toEqual({ on_duty: 2, working: 1, paused: 0 })
    expect(ctx.crewSettings?.claim_ttl_hours).toBe(4)
    expect(ctx.crewsLoading).toBe(false)
    expect(ctx.crewsError).toBeNull()
  })

  it('keeps a selection the roster still contains', async () => {
    localStorage.setItem(CREW_UI_KEY, JSON.stringify({ crewView: { kind: 'crew', id: 'c2' } }))
    renderProvider()
    await waitFor(() => expect(ctx.crews).toHaveLength(2))
    expect(ctx.crewView).toEqual({ kind: 'crew', id: 'c2' })
  })

  it('leaves nothing selected on a repo with no crews', async () => {
    api.crews.mockResolvedValue({ crews: [], counts: { on_duty: 0, working: 0, paused: 0 } })
    renderProvider()
    await waitFor(() => expect(ctx.crewsLoading).toBe(false))
    expect(ctx.crewView).toEqual({ kind: 'none' })
    expect(ctx.crewSettings).toBeNull()
  })

  it('reports a roster failure and falls back to defaults', async () => {
    api.crews.mockRejectedValue(new Error('store unreadable'))
    renderProvider()
    await waitFor(() => expect(ctx.crewsError?.message).toBe('store unreadable'))
    expect(ctx.crews).toEqual([])
    expect(ctx.crewCounts).toEqual({ on_duty: 0, working: 0, paused: 0 })
  })

  it('navigates to the crews surface, optionally jumping to a page', async () => {
    renderProvider()
    await waitFor(() => expect(ctx.crews).toHaveLength(2))
    await drive(() => ctx.openCrews())
    expect(ctx.mainView).toBe('crews')
    expect(ctx.expanded).toBe('crews')
    expect(ctx.crewView).toEqual({ kind: 'crew', id: 'c1' })
    await drive(() => ctx.openCrews({ kind: 'crew', id: 'c2' }))
    expect(ctx.crewView).toEqual({ kind: 'crew', id: 'c2' })
  })

  it('flips the direction on the active roster sort field and switches on another', async () => {
    renderProvider()
    await waitFor(() => expect(ctx.crews).toHaveLength(2))
    await drive(() => ctx.cycleCrewSort('status'))
    expect(ctx.mainView).toBe('crews')
    expect(ctx.crewSortKey).toBe('status')
    expect(ctx.crewSortDir).toBe('desc')
    await drive(() => ctx.cycleCrewSort('name'))
    expect(ctx.crewSortKey).toBe('name')
    // Switching fields keeps the stated reading order rather than resetting it.
    expect(ctx.crewSortDir).toBe('desc')
  })
})

describe('issue filters, sort and selection', () => {
  it('derives label colours, counts, member roles and the label ordering', async () => {
    renderProvider()
    await readyIssues()
    await waitFor(() => expect(ctx.memberRoleByLogin.get('member1')).toBe('admin'))
    expect(ctx.colorByName.get('bug')).toBe('bbbbbb')
    expect(ctx.countByLabel.get('bug')).toBe(2)
    expect(ctx.countByLabel.get('ui')).toBe(1)
    // Most-used first; a repo label carried by no open issue sorts last.
    expect(ctx.sortedRepoLabels.map((l) => l.name)).toEqual(['bug', 'ui', 'zeta'])
    expect(ctx.activePermissions).toEqual({ pull: true, triage: true })
    expect(ctx.canWrite).toBe(true)
  })

  it('applies the person and member filters, and clears them together', async () => {
    renderProvider()
    await readyIssues()
    expect(ctx.hasMemberIssues).toBe(true)

    await drive(() => ctx.toggleRequestedByMe())
    expect(ctx.mainView).toBe('issues')
    expect(issueNumbers()).toEqual([1])

    await drive(() => ctx.toggleRequestedByMe())
    await drive(() => ctx.toggleAssignedToMe())
    expect(issueNumbers()).toEqual([2, 1])

    await drive(() => ctx.toggleAssignedToMe())
    await drive(() => ctx.toggleCreatedByMember())
    // #2 via the roster, #3 via its member author_association.
    expect(issueNumbers()).toEqual([3, 2])

    expect(ctx.anyFilterActive).toBe(true)
    await drive(() => ctx.clearFilters())
    expect(ctx.anyFilterActive).toBe(false)
    expect(issueNumbers()).toEqual([3, 2, 1])
  })

  it('intersects selected labels and drops one on a second toggle', async () => {
    renderProvider()
    await readyIssues()
    await drive(() => ctx.toggleLabel('bug'))
    expect(issueNumbers()).toEqual([2, 1])
    await drive(() => ctx.toggleLabel('ui'))
    expect(issueNumbers()).toEqual([2])
    await drive(() => ctx.toggleLabel('ui'))
    expect(issueNumbers()).toEqual([2, 1])
    expect(ctx.selectedLabels.has('bug')).toBe(true)
  })

  it('searches the number, title, author and label names', async () => {
    renderProvider()
    await readyIssues()
    await drive(() => ctx.setQuery('#2'))
    expect(issueNumbers()).toEqual([2])
    await drive(() => ctx.setQuery('gamma'))
    expect(issueNumbers()).toEqual([3])
    await drive(() => ctx.setQuery('stranger'))
    expect(issueNumbers()).toEqual([3])
    await drive(() => ctx.setQuery('ui'))
    expect(issueNumbers()).toEqual([2])
    await drive(() => ctx.setQuery('nothing-matches'))
    expect(issueNumbers()).toEqual([])
  })

  it('cycles the sort field and direction', async () => {
    renderProvider()
    await readyIssues()
    expect(issueNumbers()).toEqual([3, 2, 1])
    await drive(() => ctx.cycleSort('number'))
    expect(ctx.sortDir).toBe('asc')
    expect(issueNumbers()).toEqual([1, 2, 3])
    await drive(() => ctx.cycleSort('updated'))
    expect(ctx.sortKey).toBe('updated')
    expect(issueNumbers()).toEqual([2, 3, 1])
  })

  it('clears the detail pane when the filters exclude the selected issue', async () => {
    renderProvider()
    await readyIssues()
    await drive(() => ctx.setSelectedIssue(2))
    expect(ctx.activeIssue?.number).toBe(2)
    await drive(() => ctx.setQuery('gamma'))
    expect(ctx.activeIssue).toBeNull()
  })

  it('treats unlabeled issues as untriaged under the default settings', async () => {
    renderProvider()
    await readyIssues()
    expect(ctx.repoSettings.unlabeled_is_untriaged).toBe(true)
    expect(ctx.needsTriage(ISSUES[2])).toBe(true)
    expect(ctx.needsTriage(ISSUES[0])).toBe(false)
    expect(ctx.isGoodFirstIssue(ISSUES[0])).toBe(false)
  })

  it("honours the repo's configured triage and good-first-issue labels", async () => {
    api.getSettings.mockResolvedValue({
      settings: {
        triage_labels: ['bug'], unlabeled_is_untriaged: false,
        good_first_issue_labels: ['ui'], notify_on_new_issue: false, revision: 3,
      },
    })
    renderProvider()
    await readyIssues()
    await waitFor(() => expect(ctx.repoSettings.revision).toBe(3))
    expect(ctx.needsTriage(ISSUES[0])).toBe(true)
    // Unlabeled no longer counts once the repo opts out.
    expect(ctx.needsTriage(ISSUES[2])).toBe(false)
    expect(ctx.isGoodFirstIssue(ISSUES[1])).toBe(true)
    expect(ctx.isGoodFirstIssue(ISSUES[0])).toBe(false)
  })

  it('navigates to a dashboard tab', async () => {
    renderProvider()
    await readyIssues()
    await drive(() => ctx.openDashboard('tagging'))
    expect(ctx.mainView).toBe('dashboard')
    expect(ctx.dashboardTab).toBe('tagging')
    expect(ctx.expanded).toBe('dashboards')
  })

  it('re-validates refresh preferences on write', async () => {
    renderProvider()
    await readyIssues()
    // 1ms is not one of the offered intervals — a hand-set value must not install.
    await drive(() => ctx.setRefreshPrefs({ listPollMs: 1 }))
    expect(ctx.refreshPrefs.listPollMs).toBe(REFRESH_DEFAULTS.listPollMs)
    await drive(() => ctx.setRefreshPrefs({ pollInBackground: true }))
    expect(ctx.refreshPrefs.pollInBackground).toBe(true)
  })
})

describe('manual refresh', () => {
  it('replaces the issue and label caches and re-reads the members', async () => {
    renderProvider()
    await readyIssues()
    api.issues.mockResolvedValue({ issues: [ISSUES[0]] })
    api.labels.mockResolvedValue({ labels: [LABELS[1]] })
    await drive(() => ctx.refresh())
    await waitFor(() => expect(ctx.issues).toHaveLength(1))
    expect(ctx.repoLabels.map((l) => l.name)).toEqual(['bug'])
    expect(api.issues).toHaveBeenCalledWith(ACTIVE, { refresh: true, state: 'open' })
    expect(api.labels).toHaveBeenCalledWith(ACTIVE, { refresh: true })
    await waitFor(() => expect(ctx.refreshing).toBe(false))
    expect(ctx.issuesUpdatedAt).toBeGreaterThan(0)
  })
})

describe('pull-request filters, sort and selection', () => {
  it('derives the PR label counts and the server bulk cap', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()
    expect(ctx.countByPrLabel.get('bug')).toBe(1)
    expect(ctx.countByPrLabel.get('ui')).toBe(1)
    expect(ctx.prBulkMax).toBe(5)
    expect(ctx.hasMemberPulls).toBe(true)
    expect(ctx.pullsPartial).toBe(false)
    expect(ctx.pullsError).toBeNull()
  })

  it('filters on drafts, member authorship and labels, then clears', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()

    await drive(() => ctx.togglePrDraftOnly())
    expect(ctx.mainView).toBe('pulls')
    expect(pullNumbers()).toEqual([11])

    await drive(() => ctx.togglePrDraftOnly())
    await drive(() => ctx.togglePrCreatedByMember())
    expect(pullNumbers()).toEqual([12, 11])

    await drive(() => ctx.togglePrCreatedByMember())
    await drive(() => ctx.togglePrLabel('ui'))
    expect(pullNumbers()).toEqual([11])
    await drive(() => ctx.togglePrLabel('ui'))
    expect(pullNumbers()).toEqual([12, 11, 10])

    await drive(() => ctx.togglePrLabel('bug'))
    expect(ctx.anyPrFilterActive).toBe(true)
    await drive(() => ctx.clearPrFilters())
    expect(ctx.anyPrFilterActive).toBe(false)
    expect(pullNumbers()).toEqual([12, 11, 10])
  })

  it('searches the number, title, author, branches and labels', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()
    await drive(() => ctx.setPrQuery('#11'))
    expect(pullNumbers()).toEqual([11])
    await drive(() => ctx.setPrQuery('widget'))
    expect(pullNumbers()).toEqual([10])
    await drive(() => ctx.setPrQuery('stranger'))
    expect(pullNumbers()).toEqual([12])
    await drive(() => ctx.setPrQuery('fix/thing'))
    expect(pullNumbers()).toEqual([11])
    await drive(() => ctx.setPrQuery('main'))
    expect(pullNumbers()).toEqual([12, 11, 10])
    await drive(() => ctx.setPrQuery('ui'))
    expect(pullNumbers()).toEqual([11])
    await drive(() => ctx.setPrQuery('no-such-thing'))
    expect(pullNumbers()).toEqual([])
  })

  it('splits the closed set into merged and unmerged', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()
    await drive(() => ctx.setPrStateFilter('merged'))
    await waitFor(() => expect(pullNumbers()).toEqual([20]))
    await drive(() => ctx.setPrStateFilter('closed'))
    await waitFor(() => expect(pullNumbers()).toEqual([21]))
    // Both filters read the one CLOSED fetch.
    expect(api.pulls).toHaveBeenCalledWith(ACTIVE, { state: 'closed', poll: false })
  })

  it('cycles the PR sort field and direction, and clears a hidden selection', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()
    expect(pullNumbers()).toEqual([12, 11, 10])
    await drive(() => ctx.cyclePrSort('number'))
    expect(ctx.prSortDir).toBe('asc')
    expect(pullNumbers()).toEqual([10, 11, 12])
    await drive(() => ctx.cyclePrSort('updated'))
    expect(ctx.prSortKey).toBe('updated')
    expect(pullNumbers()).toEqual([11, 10, 12])

    await drive(() => ctx.setSelectedPull(10))
    expect(ctx.activePull?.number).toBe(10)
    await drive(() => ctx.setPrQuery('docs'))
    expect(ctx.activePull).toBeNull()
  })

  it('excludes every row when a person filter is on but no login resolved', async () => {
    // /me answered without a login: the person filter is REQUESTED (so the list
    // query stands down) but not ACTIVE, so the client-side person predicates run
    // against the rows already resident in the cache and can match nobody.
    api.me.mockResolvedValue({ login: null })
    persistUi({ mainView: 'pulls', prAuthoredByMe: true })
    renderProvider({ seedPulls: { pulls: PULLS, bulk_max: 5 } })
    await readyPulls()
    await waitFor(() => expect(ctx.me).toBeNull())
    expect(ctx.prPersonFilterActive).toBe(false)
    expect(pullNumbers()).toEqual([])

    // Each person predicate is checked independently, so drop the previous one
    // before asserting the next (an earlier match would short-circuit the row).
    await drive(() => ctx.togglePrAuthoredByMe())
    await drive(() => ctx.togglePrAssignedToMe())
    await waitFor(() => expect(pullNumbers()).toEqual([]))

    await drive(() => ctx.togglePrAssignedToMe())
    await drive(() => ctx.togglePrReviewRequestedByMe())
    await waitFor(() => expect(pullNumbers()).toEqual([]))
  })
})

describe('pull-request search source', () => {
  it('swaps to the whole-repo search when a person filter resolves', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()

    await drive(() => ctx.togglePrAuthoredByMe())
    await waitFor(() => expect(ctx.prPersonFilterActive).toBe(true))
    await waitFor(() => expect(pullNumbers()).toEqual([10]))
    // The search's own cap is reported rather than claimed as complete.
    expect(ctx.prSearchTruncatedAt).toBe(100)
    expect(ctx.prBulkMax).toBe(7)
    expect(api.searchPulls).toHaveBeenCalledWith(ACTIVE, {
      state: 'open', author: ME, assignee: undefined, reviewRequested: undefined,
    })

    // Refresh targets the ACTIVE source — a refetch of the uncached search route.
    const before = api.searchPulls.mock.calls.length
    await drive(() => ctx.refreshPulls())
    await waitFor(() => expect(api.searchPulls.mock.calls.length).toBeGreaterThan(before))
    expect(api.pulls).not.toHaveBeenCalledWith(ACTIVE, expect.objectContaining({ refresh: true }))
  })

  it('falls back to the row count when the search reports no cap', async () => {
    api.searchPulls.mockResolvedValue({ pulls: [PULLS[0], PULLS[1]], truncated: true })
    persistUi({ mainView: 'pulls', prReviewRequestedByMe: true })
    renderProvider()
    await waitFor(() => expect(ctx.prSearchTruncatedAt).toBe(2))
    expect(ctx.pullsLoading).toBe(false)
  })
})

describe('manual PR refresh', () => {
  it('busts the list cache and reports a failed refresh', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()

    api.pulls.mockResolvedValueOnce({ pulls: [PULLS[2]], bulk_max: 5 })
    await drive(() => ctx.refreshPulls())
    await waitFor(() => expect(pullNumbers()).toEqual([12]))
    expect(api.pulls).toHaveBeenCalledWith(ACTIVE, { refresh: true, state: 'open' })

    api.pulls.mockRejectedValueOnce(new Error('rate limited'))
    await drive(() => ctx.refreshPulls())
    await waitFor(() => expect(ctx.pullsError?.message).toBe('rate limited'))
    expect(ctx.pullsRefreshing).toBe(false)
  })
})

describe('bulk PR selection', () => {
  it('ticks one row, ticks and clears every rendered row, and drops the lot', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()

    await drive(() => ctx.togglePullChecked(10))
    expect([...ctx.checkedPulls]).toEqual([10])
    await drive(() => ctx.togglePullChecked(10))
    expect(ctx.checkedPulls.size).toBe(0)

    await drive(() => ctx.toggleAllPullsChecked())
    expect([...ctx.checkedPulls].sort()).toEqual([10, 11, 12])
    // Already all ticked — the same action clears.
    await drive(() => ctx.toggleAllPullsChecked())
    expect(ctx.checkedPulls.size).toBe(0)

    await drive(() => ctx.togglePullChecked(11))
    await drive(() => ctx.clearCheckedPulls())
    expect(ctx.checkedPulls.size).toBe(0)
  })

  it('select-all reaches only the rows the active filter renders', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()
    await drive(() => ctx.togglePrDraftOnly())
    await drive(() => ctx.toggleAllPullsChecked())
    expect([...ctx.checkedPulls]).toEqual([11])
  })

  it('drops the ticks when the PR filters move', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()
    await drive(() => ctx.toggleAllPullsChecked())
    expect(ctx.checkedPulls.size).toBe(3)
    await drive(() => ctx.setPrQuery('widget'))
    await waitFor(() => expect(ctx.checkedPulls.size).toBe(0))
  })
})

describe('cross-reference sheet', () => {
  it('pushes, refuses a duplicate top, pops and closes', async () => {
    renderProvider()
    await readyIssues()
    expect(ctx.refStack).toEqual([])

    await drive(() => ctx.openRef({ kind: 'issue', number: 5 }))
    expect(ctx.refStack).toEqual([{ kind: 'issue', number: 5 }])
    // Re-opening the ref already on top is a no-op.
    await drive(() => ctx.openRef({ kind: 'issue', number: 5 }))
    expect(ctx.refStack).toHaveLength(1)

    await drive(() => ctx.openRef({ kind: 'pull', number: 6 }))
    expect(ctx.refStack).toHaveLength(2)
    await drive(() => ctx.popRef())
    expect(ctx.refStack).toEqual([{ kind: 'issue', number: 5 }])
    await drive(() => ctx.closeRefs())
    expect(ctx.refStack).toEqual([])
  })
})

describe('repo switch', () => {
  it('resets the search, filters, selections and crew page, then hands off', async () => {
    persistUi({ mainView: 'pulls' })
    renderProvider()
    await readyPulls()
    await readyIssues()
    await waitFor(() => expect(ctx.crewView).toEqual({ kind: 'crew', id: 'c1' }))

    await drive(() => ctx.setQuery('alpha'))
    await drive(() => ctx.toggleLabel('bug'))
    await drive(() => ctx.setSelectedIssue(1))
    await drive(() => ctx.setPrQuery('widget'))
    await drive(() => ctx.togglePrDraftOnly())
    await drive(() => ctx.setSelectedPull(11))

    await drive(() => ctx.switchRepo(OTHER))
    expect(onSwitch).toHaveBeenCalledWith(OTHER)
    expect(ctx.query).toBe('')
    expect(ctx.anyFilterActive).toBe(false)
    expect(ctx.selectedIssue).toBeNull()
    expect(ctx.prQuery).toBe('')
    expect(ctx.anyPrFilterActive).toBe(false)
    expect(ctx.selectedPull).toBeNull()
    // A crew id names a crew in ONE repo's store, so the page cannot carry over.
    expect(ctx.crewView).toEqual({ kind: 'none' })
  })
})

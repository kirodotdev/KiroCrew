import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type {
  PullRequest, PrDetailData, PrCheck, PullDetailResponse,
} from '../apps/issue-radar/api'

// Behaviour pins for the PR detail pane (PrDetail.tsx) — the read-only right
// column of Issue Radar's pull-request surface.
//
// What these cover, and why each one is load-bearing:
//
//  * The four state pills (merged / closed-unmerged / draft / open) are derived
//    from three independent fields, so the precedence between them is the thing
//    that can silently invert — a merged PR is `state: 'closed'` too.
//  * Every unknown METRIC renders an em dash rather than a zero. "0 files, +0 −0"
//    would be a confident claim that the PR changes nothing.
//  * "Could not read the checks" is distinguished from "there are no checks":
//    reporting none after a failed fetch hides failing CI behind a reassuring
//    sentence.
//  * A failed REFETCH keeps the previous payload on screen, so the pane must say
//    the rows are stale rather than presenting them as current.
//  * The `reviewed` row resolves its visual through a hasOwnProperty check — a
//    provider-supplied `review_state` of `constructor` would otherwise reach an
//    inherited Object.prototype member and crash the row.
//  * A check's details URL comes from the reporting app, so it is untrusted: only
//    validated http(s) becomes a link.
//  * The freshly-read check tally is patched onto this PR's cached LIST row, and
//    only within its own repo scope — patchRow matches on PR number alone, so an
//    unscoped write would overwrite another repo's PR of the same number.

const api = {
  pullDetail: vi.fn(),
  pullAi: vi.fn(),
}
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: api,
}))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

// Children are stubbed: each one owns its own queries / markdown pipeline and is
// pinned by its own test file. What matters here is WHICH props this pane hands
// them, which the stubs render as plain text.
vi.mock('../apps/issue-radar/components/RefMarkdown', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../apps/issue-radar/components/ReviewButton', () => ({
  default: () => <div>review-button</div>,
}))
vi.mock('../apps/issue-radar/components/PrActionsBar', () => ({
  default: () => <div>pr-actions-bar</div>,
}))
vi.mock('../apps/issue-radar/components/PrRunActions', () => ({
  default: ({ live }: { live: boolean }) => <div>{`pr-run-actions:${live ? 'live' : 'settled'}`}</div>,
}))
vi.mock('../apps/issue-radar/components/AiSummaryCard', () => ({
  default: (p: {
    summary: string; loading: boolean; fetching: boolean
    error: Error | null; staleSince: string | null; onRegenerate: () => void
  }) => (
    <div>
      <span data-testid="ai-state">
        {`${p.loading ? 'loading' : 'idle'}|${p.fetching ? 'fetching' : 'still'}`}
        {`|${p.error ? p.error.message : 'no-error'}|${p.staleSince ?? 'current'}`}
      </span>
      <span data-testid="ai-summary">{p.summary}</span>
      <button type="button" onClick={p.onRegenerate}>regenerate-ai</button>
    </div>
  ),
}))

const PrDetail = (await import('../apps/issue-radar/components/PrDetail')).default

const REF = { owner: 'kirodotdev', repo: 'Kiro' }
const SCOPE = 'github:github.com:kirodotdev/Kiro'
const OTHER_SCOPE = 'github:github.com:kirodotdev/Other'

const ROW: PullRequest = {
  number: 7,
  title: 'Row title',
  url: 'https://github.com/kirodotdev/Kiro/pull/7',
  state: 'open',
  draft: false,
  labels: ['from-row'],
  author: 'alice',
  author_association: 'MEMBER',
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-02T00:00:00Z',
  merged_at: null,
}

function detailData(over: Partial<PrDetailData> = {}): PrDetailData {
  return {
    number: 7,
    title: 'Detail title',
    body: 'The description body',
    state: 'open',
    draft: false,
    merged: false,
    url: 'https://github.com/kirodotdev/Kiro/pull/7#detail',
    author: 'alice',
    author_association: 'MEMBER',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-02T00:00:00Z',
    closed_at: null,
    merged_at: null,
    merged_by: null,
    comments: 1,
    review_comments: 1,
    commits: 3,
    additions: 12,
    deletions: 4,
    changed_files: 2,
    mergeable: true,
    mergeable_state: 'legacy_default_clean',
    base: 'main',
    head: 'feat/thing',
    head_sha: 'abc1234',
    labels: [{ name: 'bug', color: 'ff0000', description: '' }],
    assignees: ['dave'],
    requested_reviewers: ['erin'],
    milestone: { title: 'v1.0', state: 'open', due_on: null },
    ...over,
  }
}

function response(over: Partial<PullDetailResponse> = {}): PullDetailResponse {
  return {
    owner: REF.owner,
    repo: REF.repo,
    number: 7,
    detail: detailData(),
    timeline: [],
    checks: [],
    from_cache: false,
    ...over,
  }
}

function check(over: Partial<PrCheck> = {}): PrCheck {
  return {
    name: 'Backend Tests',
    bucket: 'success',
    status: 'completed',
    conclusion: 'success',
    url: 'https://github.com/kirodotdev/Kiro/runs/1',
    summary: 'all green',
    app: 'GitHub Actions',
    started_at: '2026-07-02T00:00:00Z',
    completed_at: '2026-07-02T00:05:00Z',
    ...over,
  }
}

function renderPane(pull: PullRequest = ROW) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <PrDetail pull={pull} />
    </QueryClientProvider>,
  )
  const sidebar = () => view.container.querySelector('aside') as HTMLElement
  const header = () => view.container.querySelector('header') as HTMLElement
  return { qc, sidebar, header, ...view }
}

/** The sidebar block whose uppercase heading is `title`. */
function block(sidebar: HTMLElement, title: string): HTMLElement {
  const heading = within(sidebar).getByText(title)
  return heading.parentElement!.parentElement as HTMLElement
}

const writeText = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  writeText.mockResolvedValue(undefined)
  // happy-dom's navigator.clipboard is getter-only; defineProperty replaces it.
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  ctx.value = {
    active: REF,
    colorByName: new Map([['bug', 'ff0000']]),
    memberRoleByLogin: new Map([['alice', 'Admin']]),
    canWrite: true,
    refreshPrefs: { detailPollMs: 30_000, pollInBackground: false },
  }
  api.pullDetail.mockResolvedValue(response())
  api.pullAi.mockResolvedValue({
    owner: REF.owner, repo: REF.repo, number: 7,
    summary: 'AI says it is fine', generated_at: '2026-07-02T01:00:00Z', from_cache: true,
  })
})

afterEach(() => vi.clearAllMocks())

describe('PrDetail — header and first paint', () => {
  it('paints the detail title, author identity, and the action affordances', async () => {
    const { header } = renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    const h = header()
    // The #number links out to the provider, using the detail URL when it lands.
    expect(within(h).getByRole('link', { name: '#7' }).getAttribute('href'))
      .toBe('https://github.com/kirodotdev/Kiro/pull/7#detail')
    expect(within(h).getByText('Open')).toBeTruthy()
    expect(within(h).getByText('alice')).toBeTruthy()
    // Admin, from the authoritative member roster rather than author_association.
    expect(within(h).getByText('Admin')).toBeTruthy()
    expect(screen.getByText('review-button')).toBeTruthy()
    expect(screen.getByText('pr-actions-bar')).toBeTruthy()
  })

  it('falls back to the list row before the detail read lands', async () => {
    let release: (v: PullDetailResponse) => void = () => {}
    api.pullDetail.mockImplementation(() => new Promise<PullDetailResponse>((res) => { release = res }))
    const { header } = renderPane({ ...ROW, base: 'row-base', head: 'row-head' })

    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Row title')
    expect(within(header()).getByRole('link', { name: '#7' }).getAttribute('href'))
      .toBe('https://github.com/kirodotdev/Kiro/pull/7')

    release(response())
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))
  })
})

describe('PrDetail — state pill precedence', () => {
  it('reads a merged PR as merged even though its state is closed', async () => {
    api.pullDetail.mockResolvedValue(response({
      detail: detailData({
        state: 'closed', merged: true, merged_at: '2026-07-03T00:00:00Z', merged_by: 'frank',
        mergeable_state: 'unknown',
      }),
    }))
    const { header, sidebar } = renderPane()
    await waitFor(() => expect(within(header()).getByText('Merged')).toBeTruthy())

    // The Dates block gains a Merged row naming who merged it…
    expect(within(block(sidebar(), 'Dates')).getByText(/frank/)).toBeTruthy()
    // …and mergeability is not offered on a PR that is already merged.
    expect(within(sidebar()).queryByText('Mergeable')).toBeNull()
  })

  it('reads a draft PR as a draft', async () => {
    api.pullDetail.mockResolvedValue(response({ detail: detailData({ draft: true }) }))
    const { header } = renderPane()
    await waitFor(() => expect(within(header()).getByText('Draft')).toBeTruthy())
    expect(screen.getByText('pr-run-actions:live')).toBeTruthy()
  })
})

describe('PrDetail — sidebar metadata', () => {
  it('renders every populated block', async () => {
    api.pullDetail.mockResolvedValue(response())
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('erin')).toBeTruthy())

    const bar = sidebar()
    expect(within(block(bar, 'Reviewers')).getByRole('link', { name: 'erin' })
      .getAttribute('href')).toBe('https://github.com/erin')
    expect(within(block(bar, 'Assignees')).getByRole('link', { name: 'dave' })).toBeTruthy()
    expect(within(block(bar, 'Labels')).getByText('bug')).toBeTruthy()
    expect(within(block(bar, 'Milestone')).getByText('v1.0')).toBeTruthy()
    expect(within(block(bar, 'Milestone')).getByText('(open)')).toBeTruthy()

    const branches = block(bar, 'Branches')
    expect(within(branches).getByText('main')).toBeTruthy()
    expect(within(branches).getByText('feat/thing')).toBeTruthy()

    const changes = block(bar, 'Changes')
    expect(within(changes).getByText('3')).toBeTruthy()
    expect(within(changes).getByText('2')).toBeTruthy()
    expect(within(changes).getByText('+12')).toBeTruthy()
    expect(within(changes).getByText('−4')).toBeTruthy()
    // Underscores are cosmetic in the mergeable state.
    expect(within(changes).getByText('legacy default clean')).toBeTruthy()
  })

  it('says "none" rather than inventing entries when the blocks are empty', async () => {
    api.pullDetail.mockResolvedValue(response({
      detail: detailData({
        requested_reviewers: [], assignees: [], labels: [], milestone: null,
        base: null, head: null, mergeable_state: null,
      }),
    }))
    const { sidebar } = renderPane({ ...ROW, labels: [] })
    await waitFor(() => expect(within(sidebar()).getByText('No reviewers requested')).toBeTruthy())

    const bar = sidebar()
    expect(within(bar).getByText('No one assigned')).toBeTruthy()
    expect(within(bar).getByText('None yet')).toBeTruthy()
    expect(within(bar).getByText('No milestone')).toBeTruthy()
    expect(within(bar).getByText('Unknown branches')).toBeTruthy()
    expect(within(bar).queryByText('Mergeable')).toBeNull()
  })

  it('renders an em dash, never a zero, for a metric it does not know', async () => {
    // The detail read never lands and the row carries no enrichment: "0 files,
    // +0 −0" would read as a measured value.
    api.pullDetail.mockRejectedValue(new Error('detail unavailable'))
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('Changes')).toBeTruthy())

    const changes = block(sidebar(), 'Changes')
    expect(within(changes).getAllByText('—')).toHaveLength(3)
    expect(within(changes).queryByText('+0')).toBeNull()
  })

  it('prefers the list row enrichment over unknown when the detail read failed', async () => {
    api.pullDetail.mockRejectedValue(new Error('detail unavailable'))
    const { sidebar } = renderPane({ ...ROW, additions: 5, deletions: 0, changed_files: 1 })
    await waitFor(() => expect(within(sidebar()).getByText('Changes')).toBeTruthy())

    const changes = block(sidebar(), 'Changes')
    expect(within(changes).getByText('+5')).toBeTruthy()
    expect(within(changes).getByText('−0')).toBeTruthy()
    expect(within(changes).getByText('1')).toBeTruthy()
  })
})

describe('PrDetail — auto review checks', () => {
  it('links a check only when its provider-supplied URL is real http(s)', async () => {
    api.pullDetail.mockResolvedValue(response({
      checks: [
        check({ name: 'Linked', bucket: 'failure', url: 'https://ci.example/run/1' }),
        // A legacy commit status supplies an arbitrary target_url — a
        // `javascript:` value would be a script-execution vector in the
        // dashboard's own origin, so it renders as plain text instead.
        check({ name: 'Hostile', bucket: 'failure', url: 'javascript:alert(1)' }),
        check({ name: 'Urlless', bucket: 'failure', url: null }),
      ],
    }))
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('Linked')).toBeTruthy())

    const bar = sidebar()
    expect(within(bar).getByRole('link', { name: /Linked/ }).getAttribute('href'))
      .toBe('https://ci.example/run/1')
    expect(within(bar).queryByRole('link', { name: /Hostile/ })).toBeNull()
    expect(within(bar).getByText('Hostile')).toBeTruthy()
    expect(within(bar).queryByRole('link', { name: /Urlless/ })).toBeNull()
  })

  it('keeps two same-named checks distinct instead of collapsing them', async () => {
    // One workflow started twice for the same head sha: colliding React keys made
    // the group heading count 4 while 6 rows painted below it.
    api.pullDetail.mockResolvedValue(response({
      checks: [
        check({ name: 'Shard', bucket: 'failure', url: 'https://ci.example/a' }),
        check({ name: 'Shard', bucket: 'failure', url: 'https://ci.example/b' }),
      ],
    }))
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getAllByText('Shard')).toHaveLength(2))
    expect(within(sidebar()).getByText('2')).toBeTruthy()
  })

  it('says it is still loading rather than reporting an empty check set', async () => {
    let release: (v: PullDetailResponse) => void = () => {}
    api.pullDetail.mockImplementation(() => new Promise<PullDetailResponse>((res) => { release = res }))
    const { sidebar } = renderPane()
    expect(within(sidebar()).getByText('Loading checks…')).toBeTruthy()

    release(response({ checks: [check()] }))
    await waitFor(() => expect(within(sidebar()).queryByText('Loading checks…')).toBeNull())
  })
})

describe('PrDetail — description and timeline', () => {
  it('renders the description in full and marks it as the opening post', async () => {
    renderPane()
    await waitFor(() => expect(screen.getByText('The description body')).toBeTruthy())
    expect(screen.getByText(/opened this Pull Request/)).toBeTruthy()
  })

  it('says so plainly when the PR has no description', async () => {
    api.pullDetail.mockResolvedValue(response({ detail: detailData({ body: '   ' }) }))
    renderPane({ ...ROW, body: '' })
    await waitFor(() => expect(screen.getByText('No description provided.')).toBeTruthy())
  })

  it('reports an empty timeline rather than leaving the section blank', async () => {
    renderPane()
    await waitFor(() => expect(screen.getByText('No activity yet.')).toBeTruthy())
  })

  it('drops an unsafe cross-reference URL rather than linking it', async () => {
    api.pullDetail.mockResolvedValue(response({
      timeline: [{
        kind: 'cross-referenced', actor: 'bob', created_at: '2026-07-01T01:00:00Z',
        source: { number: 3, title: 't', state: 'open', is_pr: false, url: 'javascript:alert(1)' },
      }],
    }))
    renderPane()
    await waitFor(() => expect(screen.getByText(/issue/)).toBeTruthy())
    expect(screen.getByText('issue#3').getAttribute('href')).toBeNull()
  })

  it('renders each review verdict, and survives a prototype-polluting review_state', async () => {
    api.pullDetail.mockResolvedValue(response({
      timeline: [
        { kind: 'reviewed', actor: 'alice', created_at: '2026-07-01T01:00:00Z', review_state: 'approved', body: 'ship it' },
        { kind: 'reviewed', actor: 'bob', created_at: '2026-07-01T02:00:00Z', review_state: 'changes_requested' },
        { kind: 'reviewed', actor: 'carol', created_at: '2026-07-01T03:00:00Z', review_state: 'commented' },
        { kind: 'reviewed', actor: 'dave', created_at: '2026-07-01T04:00:00Z', review_state: 'dismissed' },
        // `review_state` is provider data. `constructor` resolves on
        // Object.prototype, so a bare index plus `??` would hand back an
        // inherited member and crash the row on `rv.Icon`.
        { kind: 'reviewed', actor: 'erin', created_at: '2026-07-01T05:00:00Z', review_state: 'constructor' },
        { kind: 'reviewed', actor: 'frank', created_at: '2026-07-01T06:00:00Z', review_state: null },
      ],
    }))
    renderPane()
    await waitFor(() => expect(screen.getByText('ship it')).toBeTruthy())

    for (const who of ['alice', 'bob', 'carol', 'dave', 'erin', 'frank']) {
      expect(screen.getAllByText(who).length).toBeGreaterThan(0)
    }
    // The reviewer's identity is resolved from the member roster, same as the author.
    expect(screen.getAllByText('Admin').length).toBeGreaterThan(0)
  })

  it('anchors an inline review comment to its file and line', async () => {
    api.pullDetail.mockResolvedValue(response({
      timeline: [
        {
          kind: 'review_comment', actor: 'bob', created_at: '2026-07-01T01:00:00Z',
          body: 'this line', path: 'src/app.ts', line: 42,
        },
        // No line reported — the path alone is still the actionable part.
        {
          kind: 'review_comment', actor: 'bob', created_at: '2026-07-01T02:00:00Z',
          body: 'file level', path: 'src/other.ts', line: null,
        },
        { kind: 'comment', actor: 'carol', created_at: '2026-07-01T03:00:00Z', body: 'plain comment' },
      ],
    }))
    renderPane()
    await waitFor(() => expect(screen.getByText('src/app.ts:42')).toBeTruthy())
    expect(screen.getByText('src/other.ts')).toBeTruthy()
    expect(screen.getByText('plain comment')).toBeTruthy()
  })

  it('omits a timestamp it cannot parse instead of printing "Invalid Date"', async () => {
    api.pullDetail.mockResolvedValue(response({
      timeline: [
        { kind: 'reopened', actor: 'bob', created_at: 'not-a-date' },
        { kind: 'reopened', actor: 'carol', created_at: '' },
      ],
    }))
    renderPane()
    await waitFor(() => expect(screen.getAllByText('reopened this')).toHaveLength(2))
    expect(screen.queryByText(/Invalid Date/)).toBeNull()
    expect(screen.queryAllByRole('button', { name: '' })).toHaveLength(0)
  })
})

describe('PrDetail — activity errors', () => {
  it('reports a failed first read as an error', async () => {
    api.pullDetail.mockRejectedValue(new Error('gh rate limited'))
    const { container } = renderPane()
    await waitFor(() => expect(screen.getByText(/gh rate limited/)).toBeTruthy())
    expect(container.querySelector('.text-danger')).toBeTruthy()
    // The AI card is not left spinning once the query it is gated on has failed.
    expect(screen.getByTestId('ai-state').textContent).toContain('idle')
    expect(api.pullAi).not.toHaveBeenCalled()
  })

  it('labels the still-visible rows as stale after a failed refetch', async () => {
    api.pullDetail
      .mockResolvedValueOnce(response({ timeline: [{ kind: 'reopened', actor: 'bob', created_at: '2026-07-01T01:00:00Z' }] }))
      .mockRejectedValue(new Error('refresh blew up'))
    const { container } = renderPane()
    await waitFor(() => expect(screen.getByText('reopened this')).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /refresh|re_fetch|Refresh/i }))
    await waitFor(() => expect(screen.getByText(/refresh blew up/)).toBeTruthy())

    // Warn, not danger: the rows on screen are real, just old — and they are
    // still there rather than being wiped.
    expect(container.querySelector('.text-warn')).toBeTruthy()
    expect(screen.getByText('reopened this')).toBeTruthy()
  })
})

describe('PrDetail — refresh, copy, and the AI summary', () => {
  it('forces a provider read on demand, and only then', async () => {
    renderPane()
    await waitFor(() => expect(api.pullDetail).toHaveBeenCalledTimes(1))
    expect(api.pullDetail.mock.calls[0][2]).toEqual({ refresh: false })

    await userEvent.click(screen.getByRole('button', { name: /refresh|re_fetch/i }))
    await waitFor(() => expect(api.pullDetail).toHaveBeenCalledTimes(2))
    expect(api.pullDetail.mock.calls[1][2]).toEqual({ refresh: true })
  })

  it('copies the detail URL and reverts the confirmation on its own', async () => {
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toBeTruthy())

    const copy = screen.getByRole('button', { name: /copy/i })
    await userEvent.click(copy)
    expect(writeText).toHaveBeenCalledWith('https://github.com/kirodotdev/Kiro/pull/7#detail')
    // The tick replaces the copy glyph…
    await waitFor(() => expect(copy.querySelector('.text-ok')).toBeTruthy())
    // …and times out back to it, so the affordance does not read as latched.
    await waitFor(() => expect(copy.querySelector('.text-ok')).toBeNull(), { timeout: 4000 })
  })

  it('stays quiet when the clipboard is unavailable', async () => {
    writeText.mockRejectedValue(new Error('no clipboard permission'))
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toBeTruthy())

    const copy = screen.getByRole('button', { name: /copy/i })
    await userEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    // No tick — the copy did not happen, so nothing claims it did.
    expect(copy.querySelector('.text-ok')).toBeNull()
  })

  it('waits for the detail read before asking for a summary, then regenerates on request', async () => {
    let release: (v: PullDetailResponse) => void = () => {}
    api.pullDetail.mockImplementation(() => new Promise<PullDetailResponse>((res) => { release = res }))
    renderPane()
    // Gated on the detail: /pull-ai reads the cache /pull writes, so asking first
    // would spend a duplicate round of provider calls.
    expect(api.pullAi).not.toHaveBeenCalled()
    expect(screen.getByTestId('ai-state').textContent).toContain('loading')

    release(response())
    await waitFor(() => expect(api.pullAi).toHaveBeenCalledTimes(1))
    expect(api.pullAi.mock.calls[0][2]).toEqual({ refresh: false })
    await waitFor(() => expect(screen.getByTestId('ai-summary').textContent).toBe('AI says it is fine'))

    await userEvent.click(screen.getByRole('button', { name: 'regenerate-ai' }))
    await waitFor(() => expect(api.pullAi).toHaveBeenCalledTimes(2))
    expect(api.pullAi.mock.calls[1][2]).toEqual({ refresh: true })
  })

  it('flags the summary as stale when a CHECK finished after it was written', async () => {
    // A check completing does not bump the PR's updated_at, and the summary names
    // failing checks — so CI going red afterwards is exactly when it misleads.
    api.pullDetail.mockResolvedValue(response({
      detail: detailData({ updated_at: '2026-07-02T00:00:00Z' }),
      checks: [check({ completed_at: '2026-07-02T09:00:00Z' })],
    }))
    renderPane()
    await waitFor(() =>
      expect(screen.getByTestId('ai-state').textContent).toContain('2026-07-02T09:00:00Z'))
  })

  it('does not flag a summary written after the newest activity', async () => {
    api.pullDetail.mockResolvedValue(response({
      detail: detailData({ updated_at: '2026-07-01T00:00:00Z' }),
      checks: [check({ completed_at: null, started_at: '2026-07-01T00:30:00Z' })],
    }))
    renderPane()
    await waitFor(() => expect(screen.getByTestId('ai-summary').textContent).toBe('AI says it is fine'))
    expect(screen.getByTestId('ai-state').textContent).toContain('current')
  })
})

describe('PrDetail — cached list row patching', () => {
  it('patches this PR\'s row in every cached list for its own repo, and no other', async () => {
    const summary = {
      checks_counts: { failure: 1, running: 0, success: 2, other: 0 },
      checks_state: 'failure' as const,
    }
    api.pullDetail.mockResolvedValue(response({ checks_summary: summary }))

    const { qc } = (() => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      client.setQueryData(['issue-radar', 'pulls', SCOPE, 'open'], { pulls: [{ ...ROW }] })
      client.setQueryData(['issue-radar', 'pulls-search', SCOPE, 'q'], { pulls: [{ ...ROW }] })
      // A list for the same repo that does not hold #7 at all.
      client.setQueryData(['issue-radar', 'pulls', SCOPE, 'closed'], { pulls: [{ ...ROW, number: 99 }] })
      // Another repo's #7. patchRow matches on number alone, so an unscoped write
      // would clobber this.
      client.setQueryData(['issue-radar', 'pulls', OTHER_SCOPE, 'open'], { pulls: [{ ...ROW }] })
      render(
        <QueryClientProvider client={client}>
          <PrDetail pull={ROW} />
        </QueryClientProvider>,
      )
      return { qc: client }
    })()

    type Cached = { pulls?: PullRequest[] }
    await waitFor(() => {
      const patched = qc.getQueryData<Cached>(['issue-radar', 'pulls', SCOPE, 'open'])
      expect(patched?.pulls?.[0].checks_state).toBe('failure')
    })
    expect(qc.getQueryData<Cached>(['issue-radar', 'pulls-search', SCOPE, 'q'])?.pulls?.[0].checks_counts)
      .toEqual(summary.checks_counts)
    expect(qc.getQueryData<Cached>(['issue-radar', 'pulls', SCOPE, 'open'])?.pulls?.[0].checks_truncated)
      .toBe(false)
    expect(qc.getQueryData<Cached>(['issue-radar', 'pulls', SCOPE, 'closed'])?.pulls?.[0].checks_state)
      .toBeUndefined()
    expect(qc.getQueryData<Cached>(['issue-radar', 'pulls', OTHER_SCOPE, 'open'])?.pulls?.[0].checks_state)
      .toBeUndefined()
  })

  it('leaves the caches alone when the read carried no check tally', async () => {
    api.pullDetail.mockResolvedValue(response())
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['issue-radar', 'pulls', SCOPE, 'open'], { pulls: [{ ...ROW }] })
    render(
      <QueryClientProvider client={client}>
        <PrDetail pull={ROW} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))
    const cached = client.getQueryData<{ pulls?: PullRequest[] }>(['issue-radar', 'pulls', SCOPE, 'open'])
    expect(cached?.pulls?.[0].checks_state).toBeUndefined()
  })
})

describe('PrDetail — timestamps and long comment bodies', () => {
  it('flips a relative timestamp to the absolute date on click and on the keyboard', async () => {
    api.pullDetail.mockResolvedValue(response({
      timeline: [{ kind: 'reopened', actor: 'bob', created_at: '2026-07-01T01:00:00Z' }],
    }))
    renderPane()
    await waitFor(() => expect(screen.getByText('reopened this')).toBeTruthy())

    const stamps = screen.getAllByRole('button').filter((b) => b.tagName === 'SPAN')
    expect(stamps.length).toBeGreaterThan(0)
    const stamp = stamps[0]
    const relative = stamp.textContent
    const absolute = stamp.getAttribute('title')

    await userEvent.click(stamp)
    expect(stamp.textContent).toBe(absolute)
    // Enter and Space both toggle, so the affordance is reachable without a mouse.
    stamp.focus()
    await userEvent.keyboard('{Enter}')
    expect(stamp.textContent).toBe(relative)
    await userEvent.keyboard(' ')
    expect(stamp.textContent).toBe(absolute)
    // An unrelated key does nothing.
    await userEvent.keyboard('{Escape}')
    expect(stamp.textContent).toBe(absolute)
  })

  it('clamps a long comment to three lines and expands it on demand', async () => {
    // jsdom/happy-dom has no layout, so scrollHeight is always 0 and the clamp
    // would never engage. Stub it to a tall body for this test only.
    const proto = window.HTMLElement.prototype
    const original = Object.getOwnPropertyDescriptor(proto, 'scrollHeight')
    Object.defineProperty(proto, 'scrollHeight', { configurable: true, get: () => 400 })
    try {
      api.pullDetail.mockResolvedValue(response({
        timeline: [{ kind: 'comment', actor: 'bob', created_at: '2026-07-01T01:00:00Z', body: 'a very tall comment' }],
      }))
      renderPane()
      await waitFor(() => expect(screen.getByText('a very tall comment')).toBeTruthy())

      // Two affordances while collapsed: the overlay over the cropped body and
      // the explicit Show more control.
      const overlay = screen.getByRole('button', { name: /expand.comment/i })
      expect(screen.getByText('Show more')).toBeTruthy()

      await userEvent.click(overlay)
      expect(screen.getByText('Show less')).toBeTruthy()
      expect(screen.queryByRole('button', { name: /expand.comment/i })).toBeNull()

      await userEvent.click(screen.getByText('Show less'))
      expect(screen.getByText('Show more')).toBeTruthy()
    } finally {
      if (original) Object.defineProperty(proto, 'scrollHeight', original)
      else delete (proto as unknown as Record<string, unknown>).scrollHeight
    }
  })

  it('leaves a short comment unclamped, with no expand affordance at all', async () => {
    api.pullDetail.mockResolvedValue(response({
      timeline: [{ kind: 'comment', actor: 'bob', created_at: '2026-07-01T01:00:00Z', body: 'short' }],
    }))
    renderPane()
    await waitFor(() => expect(screen.getByText('short')).toBeTruthy())
    expect(screen.queryByText('Show more')).toBeNull()
    expect(screen.queryByRole('button', { name: /expand.comment/i })).toBeNull()
  })
})


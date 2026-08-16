import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type {
  Issue, IssueDetailData, IssueDetailResponse, IssueAiResponse,
  IssuesResponse, Reactions, TimelineEvent,
} from '../apps/issue-radar/api'

// Behaviour pins for the ISSUE detail pane (IssueDetail.tsx) — the right column
// of Kiro Crew's Issue Radar issues surface. Its sibling PrDetail is pinned by
// IssueRadarPrDetailCoverage.test.tsx; this file covers the parts that are
// issue-specific and therefore have no analogue there.
//
// What these cover, and why each one is load-bearing:
//
//  * State pill precedence splits CLOSED on state_reason. "Closed" and "Closed
//    as not planned" are different triage answers, and collapsing them would
//    report a wontfix as shipped.
//  * The close control is a two-step menu, and it is withheld on a read-only
//    repo AND while the state is still unknown. A placeholder row opened from a
//    cross-reference has no state, so offering "Close as completed" there would
//    let a blind write overwrite an existing state_reason.
//  * Both writes patch the react-query caches by hand rather than invalidating,
//    and each patch is scoped by repo. An unscoped write would rewrite another
//    repo's issue that happens to share the number, and the state patch must
//    touch BOTH the open and closed list caches.
//  * Cross-references are lifted OFF the activity rail into their own "Linked"
//    section, deduped by target URL. A same-repo row opens the in-app reference
//    sheet; a foreign-repo row stays a plain provider link. Modified clicks must
//    remain the browser's.
//  * The activity rail renders NEWEST-FIRST, so a reversal bug reads as a
//    reverse-chronological history that looks plausible.
//  * eventVisual has one arm per timeline event kind; the self-assign arm and
//    the two commit-bearing arms differ from their neighbours only in wording,
//    which is exactly the kind of difference a refactor silently loses.
//  * Untrusted URLs (a cross-reference target supplied by the provider) only
//    become links after passing safeHttpUrl.
//  * Empty sidebar blocks say "No one assigned" / "None yet" / "No milestone"
//    rather than rendering nothing, so an absent value is never mistaken for a
//    value that was never fetched.

const api = {
  issueDetail: vi.fn(),
  issueAi: vi.fn(),
  applyLabels: vi.fn(),
  setIssueState: vi.fn(),
}
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: api,
}))

const openRef = vi.fn()
const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

// Children are stubbed: each owns its own queries / markdown pipeline and is
// pinned by its own file. What matters here is WHICH props this pane hands them.
vi.mock('../apps/issue-radar/components/RefMarkdown', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../apps/issue-radar/components/InvestigateButton', () => ({
  default: ({ issue }: { issue: Issue }) => <div>{`investigate:${issue.title}`}</div>,
}))
vi.mock('../apps/issue-radar/components/AiSummaryCard', () => ({
  default: (p: {
    summary: string; fromCache: boolean; loading: boolean; fetching: boolean
    error: Error | null; onRegenerate: () => void; generatedAt: string | null
  }) => (
    <div>
      <span data-testid="ai-state">
        {`${p.loading ? 'loading' : 'idle'}|${p.fromCache ? 'cached' : 'fresh'}`}
        {`|${p.error ? p.error.message : 'no-error'}|${p.generatedAt ?? 'unstamped'}`}
      </span>
      <span data-testid="ai-summary">{p.summary}</span>
      <button type="button" onClick={p.onRegenerate}>regenerate-ai</button>
    </div>
  ),
}))
vi.mock('../apps/issue-radar/components/LabelPicker', () => ({
  default: ({ labels, selected, onToggle }: {
    labels: { name: string }[]; selected: string[]; onToggle: (n: string) => void
  }) => (
    <div>
      <span data-testid="picker-selected">{selected.join(',') || 'none'}</span>
      {labels.map((l) => (
        <button key={l.name} type="button" onClick={() => onToggle(l.name)}>{`pick:${l.name}`}</button>
      ))}
    </div>
  ),
}))

const IssueDetail = (await import('../apps/issue-radar/components/IssueDetail')).default

const REF = { owner: 'kirodotdev', repo: 'Kiro' }
const SCOPE = 'github:github.com:kirodotdev/Kiro'
const OTHER_SCOPE = 'github:github.com:kirodotdev/Other'

const ROW: Issue = {
  number: 11,
  title: 'Row title',
  url: 'https://github.com/kirodotdev/Kiro/issues/11',
  labels: ['from-row'],
  comments: 2,
  author: 'alice',
  author_association: 'MEMBER',
  state: 'open',
  assignees: ['dave'],
  body: 'Row body',
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-02T00:00:00Z',
}

/** A pane opened from a cross-reference: a number and nothing else. */
const PLACEHOLDER: Issue = {
  number: 11, title: '', url: '', labels: [], comments: 0, updated_at: '',
}

function reactions(over: Partial<Reactions> = {}): Reactions {
  return {
    total: 0, plus1: 0, minus1: 0, laugh: 0, hooray: 0,
    confused: 0, heart: 0, rocket: 0, eyes: 0, ...over,
  }
}

function detailData(over: Partial<IssueDetailData> = {}): IssueDetailData {
  return {
    number: 11,
    title: 'Detail title',
    body: 'The description body',
    state: 'open',
    state_reason: null,
    url: 'https://github.com/kirodotdev/Kiro/issues/11#detail',
    author: 'alice',
    author_association: 'MEMBER',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-02T00:00:00Z',
    closed_at: null,
    closed_by: null,
    comments: 2,
    locked: false,
    labels: [{ name: 'bug', color: 'ff0000', description: '' }],
    assignees: ['dave'],
    milestone: { title: 'v1.0', state: 'open', due_on: null },
    reactions: reactions({ total: 3, plus1: 2, heart: 1 }),
    ...over,
  }
}

function response(over: Partial<IssueDetailResponse> = {}): IssueDetailResponse {
  return {
    owner: REF.owner,
    repo: REF.repo,
    number: 11,
    detail: detailData(),
    timeline: [],
    from_cache: false,
    ...over,
  }
}

function ai(over: Partial<IssueAiResponse> = {}): IssueAiResponse {
  return {
    owner: REF.owner,
    repo: REF.repo,
    number: 11,
    summary: 'AI triage summary',
    suggested_labels: [],
    generated_at: '2026-07-02T01:00:00Z',
    from_cache: true,
    ...over,
  }
}

function ev(over: Partial<TimelineEvent> & { kind: TimelineEvent['kind'] }): TimelineEvent {
  return { actor: 'alice', created_at: '2026-07-02T00:00:00Z', ...over }
}

function renderPane(issue: Issue = ROW) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <IssueDetail issue={issue} />
    </QueryClientProvider>,
  )
  const header = () => view.container.querySelector('header') as HTMLElement
  // The header is TWO siblings now — a tall title block that scrolls away and a
  // sticky bar that persists — because `position: sticky` cannot escape its
  // parent. Metadata that may scroll away lives in the title block; pane state
  // and the actions live in the bar, so an assertion has to name its half.
  const titleBlock = () => view.container.querySelector('[data-testid="detail-title-block"]') as HTMLElement
  const sidebar = () => view.container.querySelector('aside') as HTMLElement
  const main = () => view.container.querySelector('main') as HTMLElement
  return { qc, header, titleBlock, sidebar, main, ...view }
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
    colorByName: new Map([['bug', 'ff0000'], ['needs-info', '00ff00']]),
    memberRoleByLogin: new Map([['alice', 'admin']]),
    repoLabels: [
      { name: 'bug', color: 'ff0000', description: '' },
      { name: 'needs-info', color: '00ff00', description: '' },
    ],
    countByLabel: new Map([['bug', 4]]),
    canWrite: true,
    stateFilter: 'open',
    refreshPrefs: { detailPollMs: 30_000, pollInBackground: false },
    // The panes render their own narrow Back control now, inside their sticky
    // header, so they read the drill-down state directly. Desktop here, which is
    // what keeps that row unrendered for the assertions below.
    listDetail: { isMobile: false, showList: true, showDetail: true, openDetail: vi.fn(), closeDetail: vi.fn() },
    openRef,
  }
  api.issueDetail.mockResolvedValue(response())
  api.issueAi.mockResolvedValue(ai())
})

afterEach(() => vi.clearAllMocks())

/** Opens the detail toolbar's overflow menu.
 *
 * Copy-link, Refresh and Close/Reopen are no longer buttons in the toolbar row:
 * `max-two-buttons-per-row` caps that row at two, so everything past the pane's
 * primary action moved behind this trigger. Radix opens on a pointer/keyboard
 * event rather than a synthetic click, and Enter also proves the menu is
 * reachable without a pointer. */
async function openOverflow() {
  const trigger = screen.getByRole('button', { name: /more actions/i })
  fireEvent.keyDown(trigger, { key: 'Enter' })
  await waitFor(() => expect(screen.getAllByRole('menuitem').length).toBeGreaterThan(0))
  return trigger
}

describe('IssueDetail — header and first paint', () => {
  it('paints the detail title, identity, and the action affordances', async () => {
    const { header, titleBlock } = renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    const h = header()
    // The #number links out to the provider, using the detail URL once it lands.
    expect(within(h).getByRole('link', { name: '#11' }).getAttribute('href'))
      .toBe('https://github.com/kirodotdev/Kiro/issues/11#detail')
    expect(within(h).getByText('Open')).toBeTruthy()
    expect(within(titleBlock()).getByText('alice')).toBeTruthy()
    // Admin comes from the authoritative roster, not author_association.
    expect(within(titleBlock()).getByText('Admin')).toBeTruthy()
    expect(screen.getByText('investigate:Detail title')).toBeTruthy()
    // The AI read is its own query, so it can land after the detail heading
    // does -- wait on its content rather than assuming a single flush covers
    // both. Asserting it synchronously passes on a fast machine and races on CI.
    await waitFor(() => expect(screen.getByTestId('ai-summary').textContent).toBe('AI triage summary'))
    expect(screen.getByTestId('ai-state').textContent).toBe('idle|cached|no-error|2026-07-02T01:00:00Z')
  })

  it('paints from the list row before the detail read lands', async () => {
    let release: (v: IssueDetailResponse) => void = () => {}
    api.issueDetail.mockImplementation(() => new Promise<IssueDetailResponse>((res) => { release = res }))
    const { header, sidebar } = renderPane()

    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Row title')
    expect(within(header()).getByRole('link', { name: '#11' }).getAttribute('href'))
      .toBe('https://github.com/kirodotdev/Kiro/issues/11')
    // The row carries only label NAMES, so the chip is synthesized against the
    // repo colour map — a name the map does not know falls back to neutral grey.
    expect(within(sidebar()).getByText('from-row')).toBeTruthy()

    release(response())
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))
    // The authoritative label objects replace the synthesized ones.
    expect(within(sidebar()).queryByText('from-row')).toBeNull()
    expect(within(sidebar()).getByText('bug')).toBeTruthy()
  })

  it('shows skeletons instead of fabricated values for a placeholder row', async () => {
    let release: (v: IssueDetailResponse) => void = () => {}
    api.issueDetail.mockImplementation(() => new Promise<IssueDetailResponse>((res) => { release = res }))
    renderPane(PLACEHOLDER)

    // No heading, no "someone opened", no "No description provided" — and no
    // Investigate button, whose seed prompt names the issue by title.
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull()
    expect(screen.queryByText('No description provided.')).toBeNull()
    expect(screen.queryByText(/^investigate:/)).toBeNull()

    release(response())
    await waitFor(() => expect(screen.getByText('investigate:Detail title')).toBeTruthy())
  })

  it('marks a locked issue and renders the empty-body fallback', async () => {
    api.issueDetail.mockResolvedValue(response({
      detail: detailData({ locked: true, body: '   ', reactions: null }),
    }))
    const { header } = renderPane({ ...ROW, body: '' })
    await waitFor(() => expect(within(header()).getByText('locked')).toBeTruthy())
    expect(screen.getByText('No description provided.')).toBeTruthy()
  })

  it('copies the detail url to the clipboard and flips to a confirmation', async () => {
    // Deliberately NOT userEvent.setup(): its own clipboard stub would replace
    // the spy installed above, and the write under test would land there.
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    await openOverflow()
    await userEvent.click(screen.getByRole('menuitem', { name: 'Copy link to this issue' }))
    expect(writeText).toHaveBeenCalledWith('https://github.com/kirodotdev/Kiro/issues/11#detail')
    // The item stays put and relabels — a select that closed the menu would take
    // the confirmation off screen the instant it was earned.
    const copied = await screen.findByRole('menuitem', { name: 'Link copied' })
    expect(copied.querySelector('.text-ok')).toBeTruthy()
    // …and it times out back, so the affordance does not read as latched.
    await waitFor(
      () => expect(screen.getByRole('menuitem', { name: 'Copy link to this issue' })).toBeTruthy(),
      { timeout: 4000 },
    )
  })

  it('survives a clipboard that refuses the write', async () => {
    writeText.mockRejectedValue(new Error('blocked'))
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    await openOverflow()
    const copy = screen.getByRole('menuitem', { name: 'Copy link to this issue' })
    await userEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    // No tick — the copy did not happen, so nothing claims it did.
    expect(screen.queryByRole('menuitem', { name: 'Link copied' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Copy link to this issue' })).toBeTruthy()
  })

  it('forces a server re-read from the refresh button and the AI regenerate', async () => {
    const user = userEvent.setup()
    renderPane()
    await waitFor(() => expect(api.issueDetail).toHaveBeenCalledTimes(1))
    // First fetch after opening is cache-first.
    expect(api.issueDetail.mock.calls[0][2]).toEqual({ refresh: false })

    await openOverflow()
    await user.click(screen.getByRole('menuitem', { name: 'Refresh issue details' }))
    await waitFor(() => expect(api.issueDetail).toHaveBeenCalledTimes(2))
    expect(api.issueDetail.mock.calls[1][2]).toEqual({ refresh: true })

    await waitFor(() => expect(api.issueAi).toHaveBeenCalledTimes(1))
    expect(api.issueAi.mock.calls[0][2]).toEqual({ refresh: false })
    await user.click(screen.getByRole('button', { name: 'regenerate-ai' }))
    await waitFor(() => expect(api.issueAi).toHaveBeenCalledTimes(2))
    expect(api.issueAi.mock.calls[1][2]).toEqual({ refresh: true })
  })
})

describe('IssueDetail — state pill', () => {
  it('reads a completed close and a not-planned close differently', async () => {
    api.issueDetail.mockResolvedValue(response({
      detail: detailData({
        state: 'closed', state_reason: 'completed',
        closed_at: '2026-07-03T00:00:00Z', closed_by: 'frank',
      }),
    }))
    const { header, sidebar, unmount } = renderPane()
    await waitFor(() => expect(within(header()).getByText('Closed')).toBeTruthy())
    // The Dates block gains a Closed row naming who closed it.
    expect(within(block(sidebar(), 'Dates')).getByText(/frank/)).toBeTruthy()
    unmount()

    api.issueDetail.mockResolvedValue(response({
      detail: detailData({ state: 'closed', state_reason: 'not_planned' }),
    }))
    const second = renderPane()
    await waitFor(() => expect(
      within(second.header()).getByText('Closed as not planned'),
    ).toBeTruthy())
  })
})

describe('IssueDetail — close / reopen writes', () => {
  it('closes as completed through the two-step menu and patches both list caches', async () => {
    const user = userEvent.setup()
    api.setIssueState.mockResolvedValue({ state: 'closed', state_reason: 'completed' })
    const { qc } = renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    const seed = (sf: string, scope: string) => qc.setQueryData<IssuesResponse>(
      ['issue-radar', 'issues', scope, sf],
      { owner: REF.owner, repo: REF.repo, issues: [{ ...ROW }], from_cache: false },
    )
    seed('open', SCOPE)
    seed('closed', SCOPE)
    seed('open', OTHER_SCOPE)

    await openOverflow()
    await user.click(screen.getByRole('menuitem', { name: 'Close as completed' }))

    await waitFor(() => expect(api.setIssueState).toHaveBeenCalledWith(REF, 11, 'closed', 'completed'))
    // Both of this repo's list caches carry the new state…
    for (const sf of ['open', 'closed']) {
      await waitFor(() => expect(
        qc.getQueryData<IssuesResponse>(['issue-radar', 'issues', SCOPE, sf])!.issues[0].state,
      ).toBe('closed'))
    }
    // …and the other repo's identically-numbered issue is untouched.
    expect(qc.getQueryData<IssuesResponse>(['issue-radar', 'issues', OTHER_SCOPE, 'open'])!
      .issues[0].state).toBe('open')
    // The detail cache is patched, so the pane reads Closed without a re-fetch.
    await waitFor(() => expect(screen.getByText('Closed')).toBeTruthy())
  })

  it('closes as not planned from the same menu', async () => {
    const user = userEvent.setup()
    api.setIssueState.mockResolvedValue({ state: 'closed', state_reason: 'not_planned' })
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    await openOverflow()
    await user.click(screen.getByRole('menuitem', { name: 'Close as not planned' }))
    await waitFor(() => expect(api.setIssueState).toHaveBeenCalledWith(REF, 11, 'closed', 'not_planned'))
    await waitFor(() => expect(screen.getByText('Closed as not planned')).toBeTruthy())
  })

  it('dismisses the overflow menu without writing', async () => {
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    const trigger = await openOverflow()
    expect(screen.getByRole('menuitem', { name: 'Close as completed' })).toBeTruthy()
    // Escape rather than a scrim click: the menu is Radix's, so dismissal is its
    // own, and a state write must not be the price of looking at the options.
    fireEvent.keyDown(trigger, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: 'Close as completed' })).toBeNull())
    expect(api.setIssueState).not.toHaveBeenCalled()
  })

  it('offers a single Reopen on a closed issue', async () => {
    const user = userEvent.setup()
    api.issueDetail.mockResolvedValue(response({
      detail: detailData({ state: 'closed', state_reason: 'completed' }),
    }))
    api.setIssueState.mockResolvedValue({ state: 'open', state_reason: null })
    renderPane({ ...ROW, state: 'closed' })

    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))
    await openOverflow()
    const reopen = screen.getByRole('menuitem', { name: 'Reopen' })
    expect(screen.queryByRole('menuitem', { name: /^Close as/ })).toBeNull()
    await user.click(reopen)
    await waitFor(() => expect(api.setIssueState).toHaveBeenCalledWith(REF, 11, 'open', undefined))
    await waitFor(() => expect(screen.getByText('Open')).toBeTruthy())
  })

  it('surfaces a failed state write in the header', async () => {
    const user = userEvent.setup()
    api.setIssueState.mockRejectedValue(new Error('403 not a collaborator'))
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))

    await openOverflow()
    await user.click(screen.getByRole('menuitem', { name: 'Close as completed' }))
    expect(await screen.findByText('403 not a collaborator')).toBeTruthy()
  })

  it('withholds the close control on a read-only repo', async () => {
    ctx.value = { ...ctx.value, canWrite: false }
    renderPane()
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Detail title'))
    await openOverflow()
    expect(screen.queryByRole('menuitem', { name: /^Close as/ })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Reopen' })).toBeNull()
  })

  it('withholds the close control until the state is actually known', async () => {
    api.issueDetail.mockImplementation(() => new Promise<IssueDetailResponse>(() => {}))
    renderPane(PLACEHOLDER)
    // The placeholder has no state; `state` falls back to 'open', so an offered
    // "Close as completed" here would be a blind write.
    await openOverflow()
    expect(screen.getByRole('menuitem', { name: 'Refresh issue details' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /^Close as/ })).toBeNull()
  })
})

describe('IssueDetail — labels sidebar', () => {
  it('renders the applied labels and hides Edit when the repo has none', async () => {
    ctx.value = { ...ctx.value, repoLabels: [] }
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('bug')).toBeTruthy())
    expect(within(sidebar()).queryByRole('button', { name: 'Edit' })).toBeNull()
  })

  it('toggles a label on and off through the picker and patches both caches', async () => {
    const user = userEvent.setup()
    api.applyLabels.mockResolvedValue({
      labels: [
        { name: 'bug', color: 'ff0000', description: '' },
        { name: 'needs-info', color: '00ff00', description: '' },
      ],
    })
    const { qc, sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('bug')).toBeTruthy())
    qc.setQueryData<IssuesResponse>(
      ['issue-radar', 'issues', SCOPE, 'open'],
      { owner: REF.owner, repo: REF.repo, issues: [{ ...ROW }], from_cache: false },
    )

    await user.click(within(sidebar()).getByRole('button', { name: 'Edit' }))
    expect(screen.getByTestId('picker-selected').textContent).toBe('bug')

    // An unapplied label ADDS…
    await user.click(screen.getByRole('button', { name: 'pick:needs-info' }))
    await waitFor(() => expect(api.applyLabels)
      .toHaveBeenCalledWith(REF, 11, ['needs-info'], []))
    await waitFor(() => expect(screen.getByTestId('picker-selected').textContent).toBe('bug,needs-info'))
    expect(qc.getQueryData<IssuesResponse>(['issue-radar', 'issues', SCOPE, 'open'])!
      .issues[0].labels).toEqual(['bug', 'needs-info'])

    // …an applied one REMOVES.
    api.applyLabels.mockResolvedValue({ labels: [{ name: 'needs-info', color: '00ff00', description: '' }] })
    await user.click(screen.getByRole('button', { name: 'pick:bug' }))
    await waitFor(() => expect(api.applyLabels).toHaveBeenLastCalledWith(REF, 11, [], ['bug']))
    await waitFor(() => expect(screen.getByTestId('picker-selected').textContent).toBe('needs-info'))

    // Leaving edit mode returns to the read-only chips.
    await user.click(within(sidebar()).getByRole('button', { name: 'Done' }))
    expect(screen.queryByTestId('picker-selected')).toBeNull()
  })

  it('surfaces a failed label write', async () => {
    const user = userEvent.setup()
    api.applyLabels.mockRejectedValue(new Error('label write refused'))
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('bug')).toBeTruthy())

    await user.click(within(sidebar()).getByRole('button', { name: 'Edit' }))
    await user.click(screen.getByRole('button', { name: 'pick:needs-info' }))
    expect(await screen.findByText('label write refused')).toBeTruthy()
  })

  it('accepts an AI suggestion with one click and drops it once applied', async () => {
    const user = userEvent.setup()
    api.issueAi.mockResolvedValue(ai({
      suggested_labels: [
        { name: 'needs-info', reason: 'no reproduction steps' },
        { name: 'bug', reason: 'already applied, filtered out' },
      ],
    }))
    api.applyLabels.mockResolvedValue({
      labels: [
        { name: 'bug', color: 'ff0000', description: '' },
        { name: 'needs-info', color: '00ff00', description: '' },
      ],
    })
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('Suggested')).toBeTruthy())

    // Only the not-yet-applied suggestion is offered, and the reason is the tip.
    const chip = within(sidebar()).getByRole('button', { name: 'needs-info' })
    expect(chip.getAttribute('title')).toBe('Add “needs-info” — no reproduction steps')
    expect(within(sidebar()).queryByRole('button', { name: 'bug' })).toBeNull()

    await user.click(chip)
    await waitFor(() => expect(api.applyLabels).toHaveBeenCalledWith(REF, 11, ['needs-info'], []))
    // Applied suggestions drop out as the detail cache updates.
    await waitFor(() => expect(within(sidebar()).queryByText('Suggested')).toBeNull())
  })

  it('degrades suggestions to read-only on a repo without push access', async () => {
    const user = userEvent.setup()
    ctx.value = { ...ctx.value, canWrite: false }
    api.issueAi.mockResolvedValue(ai({
      suggested_labels: [{ name: 'needs-info', reason: '' }],
    }))
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('Suggested')).toBeTruthy())

    expect(within(sidebar())
      .getByText('Read-only — connect with triage/push access to apply.')).toBeTruthy()
    const chip = within(sidebar()).getByRole('button', { name: 'needs-info' })
    expect(chip.getAttribute('title'))
      .toBe('Read-only — connect with triage/push access to apply')
    await user.click(chip)
    expect(api.applyLabels).not.toHaveBeenCalled()
  })

  it('says the suggestions are still loading while the AI read is in flight', async () => {
    // AI is gated on the detail landing, so let the detail resolve and hold AI.
    api.issueAi.mockImplementation(() => new Promise<IssueAiResponse>(() => {}))
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('Finding suggestions…')).toBeTruthy())
  })
})

describe('IssueDetail — sidebar metadata', () => {
  it('renders every populated block', async () => {
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('bug')).toBeTruthy())

    const bar = sidebar()
    expect(within(block(bar, 'Assignees')).getByRole('link', { name: 'dave' })
      .getAttribute('href')).toBe('https://github.com/dave')
    expect(within(block(bar, 'Milestone')).getByText('v1.0')).toBeTruthy()
    expect(within(block(bar, 'Milestone')).getByText('(open)')).toBeTruthy()
    const dates = block(bar, 'Dates')
    expect(within(dates).getByText('Opened')).toBeTruthy()
    expect(within(dates).getByText('Updated')).toBeTruthy()
    expect(within(dates).queryByText('Closed')).toBeNull()
  })

  it('says "none" rather than inventing entries when the blocks are empty', async () => {
    api.issueDetail.mockResolvedValue(response({
      detail: detailData({ assignees: [], labels: [], milestone: null }),
    }))
    const { sidebar } = renderPane({ ...ROW, assignees: [], labels: [] })
    await waitFor(() => expect(within(sidebar()).getByText('No one assigned')).toBeTruthy())
    expect(within(sidebar()).getByText('None yet')).toBeTruthy()
    expect(within(sidebar()).getByText('No milestone')).toBeTruthy()
  })

  it('shows the reaction strip only for emoji that actually have a count', async () => {
    const { main } = renderPane()
    await waitFor(() => expect(screen.getByTestId('md').textContent).toBe('The description body'))
    // plus1: 2 and heart: 1 are populated; the other six are not rendered.
    const strip = within(main()).getByText('2').parentElement!.parentElement as HTMLElement
    expect(strip.textContent).toContain('2')
    expect(strip.textContent).toContain('1')
    expect(strip.children.length).toBe(2)
  })

  it('flips a relative timestamp to the absolute date-time when clicked', async () => {
    const user = userEvent.setup()
    const { sidebar } = renderPane()
    await waitFor(() => expect(within(sidebar()).getByText('bug')).toBeTruthy())

    const dates = block(sidebar(), 'Dates')
    const opened = within(dates).getAllByRole('button')[0]
    const relative = opened.textContent
    await user.click(opened)
    expect(opened.textContent).not.toBe(relative)
    expect(opened.textContent).toBe(opened.getAttribute('title'))
    // Keyboard is an equal path back.
    opened.focus()
    await user.keyboard('{Enter}')
    expect(opened.textContent).toBe(relative)
  })

  it('renders no timestamp at all for a missing or unparseable value', async () => {
    api.issueDetail.mockResolvedValue(response({
      detail: detailData({ created_at: '', updated_at: 'not-a-date' }),
    }))
    const { sidebar } = renderPane({ ...ROW, created_at: '', updated_at: '' })
    await waitFor(() => expect(within(sidebar()).getByText('bug')).toBeTruthy())

    const dates = block(sidebar(), 'Dates')
    // An ABSENT timestamp takes the em-dash branch…
    expect(within(dates).getAllByText('—').length).toBe(1)
    // …but an UNPARSEABLE one is truthy, so it reaches RelTime, which renders
    // null: the Updated cell comes out blank rather than an em dash. Not a
    // wrong claim (better than "Invalid Date"), but the two absences do not
    // read alike. Pinned as-is so a deliberate change has to move this line.
    const updatedCell = within(dates).getByText('Updated').nextElementSibling as HTMLElement
    expect(updatedCell.textContent).toBe('')
  })
})

describe('IssueDetail — activity timeline', () => {
  it('orders activity newest-first and keeps the description off the rail', async () => {
    api.issueDetail.mockResolvedValue(response({
      timeline: [
        ev({ kind: 'comment', actor: 'alice', body: 'oldest comment', created_at: '2026-07-02T00:00:00Z' }),
        ev({ kind: 'comment', actor: 'bob', body: 'newest comment', created_at: '2026-07-04T00:00:00Z' }),
      ],
    }))
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText('newest comment')).toBeTruthy())

    const bodies = within(main()).getAllByTestId('md').map((n) => n.textContent)
    // The pinned description first (off the rail), then newest-first activity.
    expect(bodies).toEqual(['The description body', 'newest comment', 'oldest comment'])
    expect(within(main()).getByText('opened this issue')).toBeTruthy()
    expect(within(main()).getAllByText('commented').length).toBe(2)
  })

  it('says so plainly when there is no activity', async () => {
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText('No activity yet.')).toBeTruthy())
  })

  it('reports a failed activity read instead of claiming there is none', async () => {
    api.issueDetail.mockRejectedValue(new Error('rate limited'))
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText(/Couldn't load activity:/)).toBeTruthy())
    expect(within(main()).queryByText('No activity yet.')).toBeNull()
  })

  it('renders one row per non-comment event kind', async () => {
    api.issueDetail.mockResolvedValue(response({
      timeline: [
        ev({ kind: 'labeled', label: { name: 'bug', color: 'ff0000' } }),
        ev({ kind: 'unlabeled', label: { name: 'needs-info', color: '' } }),
        ev({ kind: 'assigned', actor: 'alice', assignee: 'bob' }),
        ev({ kind: 'assigned', actor: 'carol', assignee: 'carol' }),
        ev({ kind: 'unassigned', assignee: 'bob' }),
        ev({ kind: 'reopened' }),
        ev({ kind: 'renamed', rename: { from: 'Old title', to: 'New title' } }),
        ev({ kind: 'milestoned', milestone: 'v2.0' }),
        ev({ kind: 'demilestoned', milestone: 'v2.0' }),
        ev({ kind: 'committed', actor: null }),
      ],
    }))
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText('reopened this')).toBeTruthy())

    // Each sentence is assembled from sibling text nodes and inline chips, so
    // the rail's own text is the addressable unit.
    const rail = main().textContent ?? ''
    expect(rail).toContain('added the')
    expect(rail).toContain('removed the')
    expect(rail).toContain('assigned')
    // A self-assign reads as such rather than "carol assigned carol".
    expect(rail).toContain('self-assigned this')
    expect(rail).toContain('unassigned')
    expect(rail).toContain('changed the title')
    expect(rail).toContain('Old title')
    expect(rail).toContain('New title')
    expect(rail).toContain('added this to the')
    expect(rail).toContain('removed this from the')
    // An unmodelled kind still renders its actor + the raw kind, not a blank row.
    expect(rail).toContain('committed')
    // …and a null actor reads as "someone" rather than "null".
    expect(rail).toContain('someone')
    // An unlabeled event with no colour of its own falls back to the repo map.
    expect(within(main()).getByText('needs-info')).toBeTruthy()
  })

  it('links the closing commit and distinguishes the two close reasons', async () => {
    api.issueDetail.mockResolvedValue(response({
      timeline: [
        ev({ kind: 'closed', state_reason: 'completed', commit_id: 'abcdef1234567890' }),
        ev({ kind: 'closed', state_reason: 'not_planned' }),
        ev({ kind: 'referenced', commit_id: 'fedcba0987654321' }),
        ev({ kind: 'referenced', commit_id: null }),
      ],
    }))
    const { main } = renderPane()
    await waitFor(() => expect(
      within(main()).getByRole('link', { name: 'abcdef1' }),
    ).toBeTruthy())

    const m = main()
    const rail = m.textContent ?? ''
    // The two close reasons are different triage answers, not one.
    expect(rail).toContain('as completed')
    expect(rail).toContain('as not planned')
    expect(rail).toContain('closed this')
    expect(within(m).getByRole('link', { name: 'abcdef1' }).getAttribute('href'))
      .toBe('https://github.com/kirodotdev/Kiro/commit/abcdef1234567890')
    expect(within(m).getByRole('link', { name: 'fedcba0' }).getAttribute('href'))
      .toBe('https://github.com/kirodotdev/Kiro/commit/fedcba0987654321')
    // A commit-less `referenced` still names the event; only the link is dropped.
    expect(rail).toContain('referenced this in commit')
    expect(within(m).getAllByRole('link').length).toBe(2)
  })
})

describe('IssueDetail — linked pull requests and issues', () => {
  const crossRefs = [
    ev({
      kind: 'cross-referenced', actor: 'bob', created_at: '2026-07-05T00:00:00Z',
      source: {
        number: 42, title: 'Fixes the thing', state: 'open', is_pr: true,
        url: 'https://github.com/kirodotdev/Kiro/pull/42',
      },
    }),
    // A duplicate of the same target — deduped by URL.
    ev({
      kind: 'cross-referenced', actor: 'carol', created_at: '2026-07-06T00:00:00Z',
      source: {
        number: 42, title: 'Fixes the thing', state: 'open', is_pr: true,
        url: 'https://github.com/kirodotdev/Kiro/pull/42',
      },
    }),
    // A FOREIGN repo, and a closed one with no title of its own.
    ev({
      kind: 'cross-referenced', actor: null, created_at: '2026-07-07T00:00:00Z',
      source: {
        number: 9, title: '', state: 'closed', is_pr: false,
        url: 'https://github.com/other/Repo/issues/9',
      },
    }),
    // Unusable sources: no url, and no number.
    ev({
      kind: 'cross-referenced', actor: 'bob', created_at: '2026-07-08T00:00:00Z',
      source: {
        number: 8, title: 'no url', state: 'open', is_pr: false, url: '',
      },
    }),
    ev({ kind: 'cross-referenced', actor: 'bob', created_at: '2026-07-09T00:00:00Z' }),
  ]

  it('lifts cross-references off the rail, deduped, and titles the untitled', async () => {
    api.issueDetail.mockResolvedValue(response({ timeline: crossRefs }))
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText('Fixes the thing')).toBeTruthy())

    const m = main()
    expect(within(m).getByText(/Linked/)).toBeTruthy()
    // Two survivors: the deduped PR and the foreign issue. Sources with no url
    // or no source object at all contribute nothing.
    expect(within(m).getAllByText('Fixes the thing').length).toBe(1)
    expect(within(m).getByText('Issue #9')).toBeTruthy()
    expect(within(m).queryByText('no url')).toBeNull()
    // Nothing was left on the activity rail.
    expect(within(m).getByText('No activity yet.')).toBeTruthy()
    expect(within(m).getByText('· 2')).toBeTruthy()
    expect(within(m).getByText('· by bob')).toBeTruthy()
  })

  it('opens a same-repo reference in-app and a foreign one on its provider', async () => {
    const user = userEvent.setup()
    api.issueDetail.mockResolvedValue(response({ timeline: crossRefs }))
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText('Fixes the thing')).toBeTruthy())

    const own = within(main()).getByTitle('Open PR #42 here')
    await user.click(own)
    await waitFor(() => expect(openRef).toHaveBeenCalledTimes(1))
    // parseRepoRef resolved it to the ACTIVE repo, so the sheet is handed the
    // item's kind + number rather than a second repo identity.
    expect(openRef.mock.calls[0][0]).toMatchObject({ kind: 'pull', number: 42 })

    // A different repo keeps its provider link and does not hijack the pane.
    const foreign = within(main()).getByTitle('Open issue #9 on GitHub')
    await user.click(foreign)
    expect(openRef).toHaveBeenCalledTimes(1)
    expect(foreign.getAttribute('href')).toBe('https://github.com/other/Repo/issues/9')
  })

  it('leaves a modified click to the browser', async () => {
    const user = userEvent.setup()
    api.issueDetail.mockResolvedValue(response({ timeline: crossRefs }))
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText('Fixes the thing')).toBeTruthy())

    const own = within(main()).getByTitle('Open PR #42 here')
    await user.keyboard('[MetaLeft>]')
    await user.click(own)
    await user.keyboard('[/MetaLeft]')
    expect(openRef).not.toHaveBeenCalled()
  })

  it('refuses to link an untrusted cross-reference url', async () => {
    api.issueDetail.mockResolvedValue(response({
      timeline: [
        ev({
          kind: 'cross-referenced', actor: 'bob',
          source: {
            number: 5, title: 'Not a web link', state: 'open', is_pr: false,
            url: 'javascript:alert(1)',
          },
        }),
      ],
    }))
    const { main } = renderPane()
    await waitFor(() => expect(within(main()).getByText('Not a web link')).toBeTruthy())
    // The row still renders; only the href is withheld.
    const row = within(main()).getByText('Not a web link').closest('a') as HTMLElement
    expect(row.getAttribute('href')).toBeNull()
  })
})

describe('IssueDetail — provider vocabulary', () => {
  it('reads a GitLab repo in merge-request terms and on its own host', async () => {
    ctx.value = {
      ...ctx.value,
      active: { ...REF, provider: 'gitlab', host: 'gitlab.example.com' },
    }
    api.issueDetail.mockResolvedValue(response({
      timeline: [
        ev({
          kind: 'cross-referenced', actor: 'bob',
          source: {
            number: 42, title: 'Fixes the thing', state: 'open', is_pr: true,
            url: 'https://gitlab.example.com/kirodotdev/Kiro/-/merge_requests/42',
          },
        }),
      ],
    }))
    const { header, main, sidebar } = renderPane()
    await waitFor(() => expect(within(main()).getByText('Fixes the thing')).toBeTruthy())

    expect(within(main()).getByText(/merge requests/)).toBeTruthy()
    expect(within(main()).getByText('MR!42')).toBeTruthy()
    expect(within(header()).getByRole('link', { name: '#11' }).getAttribute('title'))
      .toBe('Open on GitLab')
    // Author links resolve on the self-managed host, not github.com.
    expect(within(sidebar()).getByRole('link', { name: 'dave' }).getAttribute('href'))
      .toBe('https://gitlab.example.com/dave')
  })
})

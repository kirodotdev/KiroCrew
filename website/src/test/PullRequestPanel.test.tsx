import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PullRequestSource, PullRequestStatus } from '../types'
import { MAX_PULL_REQUEST_SOURCES } from '../utils/pullRequestLinks'

const mockApi = vi.hoisted(() => ({
  pullRequestChecks: vi.fn(),
  pullRequestSource: vi.fn(),
  pullRequestStatuses: vi.fn(),
  resolvePullRequestThread: vi.fn(),
  enablePullRequestAutoMerge: vi.fn(),
  markPullRequestReady: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

import PullRequestPanel, {
  CHECK_POLL_MAX_FAILURES,
  SOURCE_REMOUNT_REVALIDATE_MS,
  pullRequestCheckPollDelay,
  pullRequestCiSignal,
  pullRequestIsLive,
  pullRequestLifecycleState,
  pullRequestMergeBlocker,
  selectedSourceStatus,
  shouldRetrySourceRead,
  sourceBusyRetryDelay,
  STATUS_FOLLOWUP_MAX,
  stateLabel,
  statusPollDelay,
} from '../components/PullRequestPanel'
import { pullRequestErrorDetails } from '../utils/pullRequestErrors'

/** An ApiError-shaped rejection: the human message plus the raw body the client
 *  preserves, which is where the machine-readable code lives. */
function apiError(body: Record<string, unknown>): Error & { body: string } {
  const raw = JSON.stringify(body)
  return Object.assign(new Error(String(body.error || '')), { body: raw })
}

describe('source read retry policy', () => {
  it('retries a busy gateway, bounded', () => {
    const busy = apiError({ error: 'Too many source requests are pending.', code: 'source_busy' })
    expect(pullRequestErrorDetails(busy).sourceBusy).toBe(true)
    expect(shouldRetrySourceRead(0, busy)).toBe(true)
    expect(shouldRetrySourceRead(1, busy)).toBe(true)
    // Bounded: a permanently saturated gateway surfaces the error instead of
    // retrying forever.
    expect(shouldRetrySourceRead(2, busy)).toBe(false)
  })

  it('does NOT retry a provider error', () => {
    const provider = apiError({ error: 'gh could not authenticate', code: 'provider_error' })
    expect(pullRequestErrorDetails(provider).sourceBusy).toBe(false)
    expect(shouldRetrySourceRead(0, provider)).toBe(false)
  })

  it('does not treat an unlabelled error as retryable', () => {
    // Pre-fix bodies and plain-text network errors carry no code; retrying them
    // would delay a message the user must act on.
    expect(shouldRetrySourceRead(0, apiError({ error: 'boom' }))).toBe(false)
    expect(shouldRetrySourceRead(0, new Error('network down'))).toBe(false)
  })

  it('backs off between attempts', () => {
    expect(sourceBusyRetryDelay(0)).toBe(2_000)
    expect(sourceBusyRetryDelay(1)).toBe(4_000)
  })
})

describe('owner-not-configured mutation refusal', () => {
  it('recognizes the code and swaps in the localized guidance', () => {
    const denied = apiError({
      error: 'this action needs a configured owner; set the Owner ID in Settings → Channels → Slack, then sign in again',
      code: 'owner_not_configured',
    })
    const details = pullRequestErrorDetails(denied)
    expect(details.ownerNotConfigured).toBe(true)
    // The localized guidance replaces the server's English prose: the code,
    // not the prose, is the contract.
    expect(details.message).toContain('Owner Slack member ID')
    expect(details.message).toContain('Slack')
  })

  it('leaves a generic forbidden untouched', () => {
    const generic = apiError({ error: 'forbidden' })
    const details = pullRequestErrorDetails(generic)
    expect(details.ownerNotConfigured).toBe(false)
    expect(details.message).toBe('forbidden')
  })
})

const github: PullRequestSource = {
  provider: 'github',
  url: 'https://github.com/acme/widgets/pull/12',
  number: 12,
  title: 'Add source tabs',
  description: '## Summary\nSource details.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: '2026-07-13T12:00:00Z',
  headBranch: 'feature/source-tabs',
  baseBranch: 'main',
  headSha: 'abcdef123456',
  author: 'octocat',
  additions: 20,
  deletions: 4,
  changedFiles: 1,
  files: [{ path: 'src/panel.tsx', status: 'modified', additions: 20, deletions: 4, patch: '@@ -1 +1 @@\n-old\n+new' }],
  commits: [{ sha: 'abcdef123456', title: 'Add source tabs', body: '', author: 'octocat', date: '2026-07-13T11:00:00Z', url: 'https://github.com/acme/widgets/commit/abcdef123456' }],
  checks: [{ name: 'test', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: 'https://github.com/acme/widgets/actions/1', startedAt: '', completedAt: '' }],
  comments: [
    { id: '1', kind: 'inline', author: 'reviewer', body: 'Please cover this case.', state: '', createdAt: '2026-07-13T12:00:00Z', url: 'https://github.com/acme/widgets/pull/12#discussion_r1', path: 'src/panel.tsx', line: 9, threadId: 'PRRT_thread1', resolvable: true, resolved: false },
    { id: '2', kind: 'comment', author: 'commenter', body: 'General note.', state: '', createdAt: '2026-07-13T12:05:00Z', url: '', path: '', line: null, threadId: '', resolvable: false, resolved: false },
    { id: '3', kind: 'inline', author: 'reviewer', body: 'Already handled.', state: '', createdAt: '2026-07-13T12:10:00Z', url: '', path: 'src/panel.tsx', line: 20, threadId: 'PRRT_thread2', resolvable: true, resolved: true },
  ],
}

const gitlab: PullRequestSource = {
  ...github,
  provider: 'gitlab',
  url: 'https://gitlab.com/acme/service/-/merge_requests/7',
  number: 7,
  title: 'GitLab source',
  state: 'merged',
  mergedAt: '2026-07-13T10:00:00Z',
}

function renderPanel(onAddToChat = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    client,
    onAddToChat,
    ...render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={[
            { url: github.url, provider: 'github', number: 12, repo: 'widgets' },
            { url: gitlab.url, provider: 'gitlab', number: 7, repo: 'service' },
          ]}
          selectedUrl={github.url}
          onSelect={() => {}}
          onAddToChat={onAddToChat}
        />
      </QueryClientProvider>,
    ),
  }
}

beforeEach(() => {
  mockApi.pullRequestChecks.mockReset()
  mockApi.pullRequestChecks.mockResolvedValue({ checks: github.checks })
  mockApi.pullRequestSource.mockReset()
  mockApi.pullRequestSource.mockImplementation((url: string) => Promise.resolve(new URL(url).hostname === 'gitlab.com' ? gitlab : github))
  mockApi.pullRequestStatuses.mockReset()
  mockApi.pullRequestStatuses.mockResolvedValue({ statuses: {} })
  mockApi.resolvePullRequestThread.mockReset()
  mockApi.resolvePullRequestThread.mockResolvedValue({ resolved: true })
  mockApi.enablePullRequestAutoMerge.mockReset()
  mockApi.enablePullRequestAutoMerge.mockResolvedValue({ autoMerge: true, mergeMethod: 'squash' })
  mockApi.markPullRequestReady.mockReset()
  mockApi.markPullRequestReady.mockResolvedValue({ ready: true })
})

describe('PullRequestPanel', () => {
  it('renders provider identity, PR tabs, changed files, and check status', async () => {
    renderPanel()
    expect(await screen.findByText('Add source tabs')).toBeInTheDocument()
    expect(screen.getByText('Github', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('src/panel.tsx')).toBeInTheDocument()
    expect(screen.getByText('1 File Changed')).toBeInTheDocument()
    // Diffs stay unmounted until explicitly expanded, then mount after the
    // drawer animation deferral. Row CONTENT is not asserted here: Pierre
    // renders it inside a shadow root, which Testing Library cannot query — the
    // loading placeholder giving way to the diff surface is the observable
    // contract from the light DOM.
    expect(screen.queryByTestId('pr-diff-surface')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /src\/panel\.tsx/i }))
    expect(await screen.findByTestId('pr-diff-surface')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /All checks passed/i })).toBeInTheDocument()
    const githubTab = screen.getByRole('tab', { name: /PR #12/i })
    const gitlabTab = screen.getByRole('tab', { name: /MR !7/i })
    expect(githubTab).toBeInTheDocument()
    expect(gitlabTab).toBeInTheDocument()
    expect(githubTab.querySelector('[data-provider-mark="github"]')).toBeInTheDocument()
    expect(gitlabTab.querySelector('[data-provider-mark="gitlab"]')).toBeInTheDocument()
  })

  it('copies the head branch name to the clipboard when the branch chip is clicked', async () => {
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      renderPanel()
      const copyBtn = await screen.findByRole('button', { name: /Copy branch name feature\/source-tabs/i })
      fireEvent.click(copyBtn)
      expect(writeText).toHaveBeenCalledWith('feature/source-tabs')
      await screen.findByRole('button', { name: /Copied branch name feature\/source-tabs/i })
    } finally {
      if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
      else delete (navigator as { clipboard?: unknown }).clipboard
    }
  })

  it('shows lifecycle and CI state on every source tab, not just the selected one', async () => {
    mockApi.pullRequestStatuses.mockResolvedValue({
      statuses: {
        [github.url]: { state: 'open', ci: 'running' },
        [gitlab.url]: { state: 'merged', ci: 'passed' },
      },
    })

    renderPanel()
    await screen.findByText('Add source tabs')

    // One bounded request covers the whole strip.
    await waitFor(() => expect(mockApi.pullRequestStatuses).toHaveBeenCalledWith([github.url, gitlab.url]))

    const gitlabTab = await screen.findByRole('tab', { name: /MR !7/i })
    // Merged is terminal: the merge glyph shows and CI is suppressed.
    expect(within(gitlabTab).getByLabelText('Merged')).toBeInTheDocument()
    expect(within(gitlabTab).queryByLabelText('Checks passed')).not.toBeInTheDocument()

    // The selected tab is driven by its own fully loaded payload (open, all
    // checks passed) rather than the coarser cached status ('running').
    const githubTab = screen.getByRole('tab', { name: /PR #12/i })
    expect(within(githubTab).getByLabelText('Open')).toBeInTheDocument()
    expect(within(githubTab).getByLabelText('Checks passed')).toBeInTheDocument()
    expect(within(githubTab).queryByLabelText('Checks running')).not.toBeInTheDocument()
  })

  it('leaves source tabs unmarked while no status is known yet', async () => {
    renderPanel()
    await screen.findByText('Add source tabs')

    const gitlabTab = screen.getByRole('tab', { name: /MR !7/i })
    expect(within(gitlabTab).queryByLabelText('Merged')).not.toBeInTheDocument()
    expect(within(gitlabTab).queryByLabelText('Open')).not.toBeInTheDocument()
  })

  it('keeps the cached CI glyph when a degraded payload flags checks as partial', async () => {
    // The provider's checks read failed: the full payload carries an EMPTY
    // checks list flagged in partialSections, while the backend's keep-known
    // rule preserved the last CI value in the chip cache.
    mockApi.pullRequestSource.mockImplementation((url: string) => Promise.resolve(
      new URL(url).hostname === 'gitlab.com'
        ? gitlab
        : { ...github, checks: [], partialSections: ['checks'] },
    ))
    mockApi.pullRequestStatuses.mockResolvedValue({
      statuses: { [github.url]: { state: 'open', ci: 'failed' } },
    })

    renderPanel()
    await screen.findByText('Add source tabs')

    const githubTab = screen.getByRole('tab', { name: /PR #12/i })
    // The kept value survives the selected tab's own full-payload projection.
    expect(await within(githubTab).findByLabelText('Checks failed')).toBeInTheDocument()
  })

  it('clears the CI glyph on a clean empty-checks payload despite a stale cached one', async () => {
    // No partial flag: the checks section is authoritatively empty (no CI
    // configured), so a stale cached glyph must NOT be resurrected.
    mockApi.pullRequestSource.mockImplementation((url: string) => Promise.resolve(
      new URL(url).hostname === 'gitlab.com'
        ? gitlab
        : { ...github, checks: [] },
    ))
    mockApi.pullRequestStatuses.mockResolvedValue({
      statuses: {
        [github.url]: { state: 'open', ci: 'failed' },
        [gitlab.url]: { state: 'open', ci: 'failed' },
      },
    })

    renderPanel()
    await screen.findByText('Add source tabs')

    // The unselected tab renders the cached glyph — proof the status batch
    // has landed before the absence below is asserted.
    const gitlabTab = screen.getByRole('tab', { name: /MR !7/i })
    expect(await within(gitlabTab).findByLabelText('Checks failed')).toBeInTheDocument()

    const githubTab = screen.getByRole('tab', { name: /PR #12/i })
    expect(within(githubTab).queryByLabelText('Checks failed')).not.toBeInTheDocument()
    expect(within(githubTab).queryByLabelText('Checks passed')).not.toBeInTheDocument()
    expect(within(githubTab).queryByLabelText('Checks running')).not.toBeInTheDocument()
  })

  it('paces the strip poll by the server TTL, with a bounded fast follow-up', () => {
    // No data yet, or a server that omits the TTL: fall back to 60s.
    expect(statusPollDelay(undefined, 0)).toBe(60_000)
    expect(statusPollDelay({ statuses: {} }, 0)).toBe(60_000)
    // Steady state tracks the server's own cache TTL instead of a hardcoded copy…
    expect(statusPollDelay({ statuses: {}, ttlSecs: 30 }, 0)).toBe(30_000)
    // …clamped against absurd values.
    expect(statusPollDelay({ statuses: {}, ttlSecs: 0.1 }, 0)).toBe(5_000)
    expect(statusPollDelay({ statuses: {}, ttlSecs: 99_999 }, 0)).toBe(300_000)
    // A refresh in flight means a new value is coming: re-poll quickly so a
    // merge or finished CI run is not invisible for another whole interval.
    const refreshing = { statuses: {}, ttlSecs: 60, refreshing: ['https://github.com/a/b/pull/1'] }
    expect(statusPollDelay(refreshing, 0)).toBe(5_000)
    expect(statusPollDelay(refreshing, STATUS_FOLLOWUP_MAX)).toBe(5_000)
    // A provider that never settles cannot hold the panel on the fast interval.
    expect(statusPollDelay(refreshing, STATUS_FOLLOWUP_MAX + 1)).toBe(60_000)
    // Failing polls back off but never stop — one transient error must not
    // freeze every unselected tab's glyph until the user intervenes.
    expect(statusPollDelay(undefined, 0, 1)).toBe(30_000)
    expect(statusPollDelay(undefined, 0, 2)).toBe(60_000)
    expect(statusPollDelay(undefined, 0, 4)).toBe(240_000)
    expect(statusPollDelay(undefined, 0, 99)).toBe(300_000)
    // Backoff outranks the fast follow-up hint from the last good response.
    expect(statusPollDelay(refreshing, 0, 1)).toBe(30_000)
  })

  it('labels the state badge in the same precedence as the tab lifecycle glyph', () => {
    expect(stateLabel(github)).toBe('Open')
    expect(stateLabel({ ...github, state: 'opened' })).toBe('Opened')
    expect(stateLabel({ ...github, draft: true })).toBe('Draft')
    // A GitLab MR keeps `draft` set after being closed as a draft, so the
    // terminal state must win or the badge contradicts the tab glyph.
    expect(stateLabel({ ...gitlab, state: 'closed', mergedAt: '', draft: true })).toBe('Closed')
    expect(stateLabel({ ...gitlab, draft: true })).toBe('Merged')
    expect(stateLabel({ ...gitlab, state: 'locked', mergedAt: '' })).toBe('Locked')
  })

  it('derives chip lifecycle and CI signals from a loaded pull request', () => {
    expect(pullRequestLifecycleState(github)).toBe('open')
    expect(pullRequestLifecycleState(gitlab)).toBe('merged')
    expect(pullRequestLifecycleState({ ...github, draft: true })).toBe('draft')
    expect(pullRequestLifecycleState({ ...github, state: 'CLOSED' })).toBe('closed')
    // Merged outranks draft: a merged pull request is terminal.
    expect(pullRequestLifecycleState({ ...gitlab, draft: true })).toBe('merged')
    // GitLab reports 'opened' for live MRs, and states outside the known set
    // (e.g. 'locked') must show no glyph rather than being mislabeled 'Open'.
    expect(pullRequestLifecycleState({ ...github, state: 'opened' })).toBe('open')
    expect(pullRequestLifecycleState({ ...github, state: 'locked' })).toBeUndefined()
    expect(pullRequestLifecycleState({ ...github, state: '' })).toBeUndefined()

    expect(pullRequestCiSignal([])).toBeUndefined()
    expect(pullRequestCiSignal(github.checks)).toBe('passed')
    expect(pullRequestCiSignal([{ ...github.checks[0], bucket: 'pending' }])).toBe('running')
    expect(pullRequestCiSignal([
      { ...github.checks[0], bucket: 'pending' },
      { ...github.checks[0], bucket: 'failed' },
    ])).toBe('failed')
  })

  it('layers the selected payload over its cached chip status field by field', () => {
    const cached = { state: 'open' as const, ci: 'running' as const, mergeable: 'conflicting', mergeStateStatus: 'dirty' }

    // The payload speaks to every field here, so it wins outright -- that is
    // the point of preferring it: the tab must not lag the header badge above.
    expect(selectedSourceStatus({ ...github, mergeable: 'mergeable', mergeStateStatus: 'clean' }, cached))
      .toEqual({ state: 'open', ci: 'passed', mergeable: 'mergeable', mergeStateStatus: 'clean' })

    // A payload that does not settle the merge pair keeps the cached one. The
    // provider reports '' for "no answer", so an empty string must not erase a
    // value the backend settled earlier -- and the pair must survive AT ALL,
    // which a whole-record rebuild from the payload silently dropped.
    expect(selectedSourceStatus({ ...github, mergeable: '', mergeStateStatus: undefined }, cached))
      .toEqual({ state: 'open', ci: 'passed', mergeable: 'conflicting', mergeStateStatus: 'dirty' })

    // Any field this panel does not recompute rides along untouched, so a new
    // status field does not need this function edited to survive selection.
    expect(selectedSourceStatus(github, { ...cached, extra: 'keep' } as PullRequestStatus))
      .toMatchObject({ extra: 'keep' })

    // No cached entry at all: the payload alone still produces a usable status.
    expect(selectedSourceStatus({ ...github, mergeable: 'mergeable' }, undefined))
      .toEqual({ state: 'open', ci: 'passed', mergeable: 'mergeable', mergeStateStatus: undefined })

    // Degraded checks section: CI falls back to the glyph the backend kept
    // alive rather than being erased, and the merge pair is unaffected by it.
    expect(selectedSourceStatus({ ...github, checks: [], partialSections: ['checks'] }, cached))
      .toEqual({ state: 'open', ci: 'running', mergeable: 'conflicting', mergeStateStatus: 'dirty' })

    // An empty checks section that is NOT flagged partial means "no CI here",
    // so a stale glyph is cleared instead of kept.
    expect(selectedSourceStatus({ ...github, checks: [] }, cached).ci).toBeUndefined()
  })

  it('treats an unsettled merge answer as absent and drops the pair once terminal', () => {
    const cached = { state: 'open' as const, ci: 'passed' as const, mergeable: 'conflicting', mergeStateStatus: 'dirty' }

    // `unknown` is GitHub still computing the merge commit -- a state every push
    // re-enters -- so it must not overwrite a settled value, or the pair would
    // flicker off and back on through the recompute window. Same rule as `''`.
    expect(selectedSourceStatus({ ...github, mergeable: 'unknown', mergeStateStatus: 'unknown' }, cached))
      .toMatchObject({ mergeable: 'conflicting', mergeStateStatus: 'dirty' })

    // A cached value that is ITSELF unsettled is not a value either: the field
    // reads absent rather than reporting 'unknown' as an answer.
    const unsettledCache = { state: 'open' as const, mergeable: 'unknown', mergeStateStatus: '' }
    const fromUnsettled = selectedSourceStatus({ ...github, mergeable: '', mergeStateStatus: '' }, unsettledCache)
    expect(fromUnsettled.mergeable).toBeUndefined()
    expect(fromUnsettled.mergeStateStatus).toBeUndefined()

    // Merged or closed: mergeability is a question about a merge that can still
    // happen, so a retained `conflicting` would be an answer to a question
    // nobody asked. The pair is dropped even though the cache still carries it.
    const merged = selectedSourceStatus({ ...gitlab, mergeable: '', mergeStateStatus: '' }, cached)
    expect(merged.state).toBe('merged')
    expect(merged.mergeable).toBeUndefined()
    expect(merged.mergeStateStatus).toBeUndefined()

    const closed = selectedSourceStatus({ ...github, state: 'CLOSED' }, cached)
    expect(closed.state).toBe('closed')
    expect(closed.mergeable).toBeUndefined()

    // A state outside the known set is NOT terminal — it has no lifecycle glyph,
    // and treating it as terminal would silently discard a settled pair.
    expect(selectedSourceStatus({ ...github, state: 'locked' }, cached))
      .toMatchObject({ mergeable: 'conflicting', mergeStateStatus: 'dirty' })
  })

  it('shows an actionable warning when the local GitHub CLI is not logged in', async () => {
    mockApi.pullRequestSource.mockRejectedValueOnce(
      new Error('{"error":"not logged into any GitHub hosts. Run `gh auth login`, then retry."}'),
    )

    renderPanel()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('GitHub CLI login required')
    expect(alert).toHaveTextContent('Kiro Crew uses your local provider CLI')
    expect(alert).toHaveTextContent('gh auth login')
    expect(alert).not.toHaveTextContent('{"error"')
  })

  it('preserves trusted-install guidance when the local CLI is unavailable', async () => {
    mockApi.pullRequestSource.mockRejectedValueOnce(
      new Error('{"error":"The local GitHub CLI (gh) was not found in a trusted system location. Install a root-owned `gh` at `/usr/local/libexec/kirocrew/gh`, run `gh auth login`, then retry."}'),
    )

    renderPanel()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not load this pull request')
    expect(alert).toHaveTextContent('Install a root-owned')
    expect(alert).toHaveTextContent('/usr/local/libexec/kirocrew/gh')
    expect(alert).toHaveTextContent('gh auth login')
    expect(alert).not.toHaveTextContent('GitHub CLI login required')
    expect(alert).not.toHaveTextContent('{"error"')
  })

  function renderWithRetained(dataUpdatedAt: number) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData<PullRequestSource>(['pull-request-source', github.url], github, {
      updatedAt: dataUpdatedAt,
    })
    render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={[{ url: github.url, provider: 'github', number: 12, repo: 'widgets' }]}
          selectedUrl={github.url}
          onSelect={() => {}}
          onAddToChat={() => {}}
        />
      </QueryClientProvider>,
    )
  }

  it('revalidates a retained payload older than the gateway cache window on mount', async () => {
    // Stale-while-revalidate: the retained payload paints at once (no spinner)
    // and a background refetch runs, because this gateway's own events cannot
    // see a teammate's review or comment.
    renderWithRetained(Date.now() - SOURCE_REMOUNT_REVALIDATE_MS - 1_000)
    expect(screen.getAllByText(github.title).length).toBeGreaterThan(0)
    await waitFor(() => expect(mockApi.pullRequestSource).toHaveBeenCalledWith(github.url, false))
  })

  it('does not refetch a retained payload the gateway would still serve from cache', () => {
    // Inside the window a refetch returns the same bytes; a sibling view that
    // shares this key (Code Review Sage) would otherwise pay two reads per open.
    renderWithRetained(Date.now() - 1_000)
    expect(screen.getAllByText(github.title).length).toBeGreaterThan(0)
    expect(mockApi.pullRequestSource).not.toHaveBeenCalled()
  })

  it('shows a compact notice, not the full error card, when a background revalidation fails', async () => {
    mockApi.pullRequestSource.mockRejectedValue(apiError({ error: 'gh: HTTP 401 Bad credentials' }))
    renderWithRetained(Date.now() - SOURCE_REMOUNT_REVALIDATE_MS - 1_000)
    await screen.findByRole('status')
    // The loaded pull request stays on screen with a one-line notice above it...
    expect(screen.getAllByText(github.title).length).toBeGreaterThan(0)
    expect(screen.getByRole('status')).toHaveTextContent(/showing the last loaded version/i)
    // ...and the full-height "could not load" card never appears over it.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('caps rendered source tabs at the per-slot limit', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const sources = Array.from({ length: MAX_PULL_REQUEST_SOURCES + 5 }, (_, index) => ({
      url: `https://github.com/acme/widgets/pull/${index + 1}`,
      provider: 'github' as const,
      number: index + 1,
      repo: 'widgets',
    }))

    render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={sources}
          selectedUrl={sources[0].url}
          onSelect={() => {}}
          onAddToChat={() => {}}
        />
      </QueryClientProvider>,
    )

    expect(screen.getAllByRole('tab', { name: /PR #/i })).toHaveLength(
      MAX_PULL_REQUEST_SOURCES,
    )
    expect(screen.queryByRole('tab', { name: `PR #${MAX_PULL_REQUEST_SOURCES + 1}` })).not.toBeInTheDocument()
  })

  it('separates network refresh from actual running checks', async () => {
    let resolveRefresh: (value: PullRequestSource) => void = () => {}
    const pendingCheck = {
      ...github.checks[0],
      status: 'IN_PROGRESS',
      conclusion: '',
      bucket: 'pending' as const,
    }
    mockApi.pullRequestChecks.mockResolvedValue({ checks: [pendingCheck] })
    renderPanel()
    await screen.findByText('Add source tabs')
    mockApi.pullRequestSource.mockImplementationOnce(
      () => new Promise<PullRequestSource>(resolve => { resolveRefresh = resolve }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Refresh pull request' }))

    expect(await screen.findByRole('button', { name: 'Refreshing pull request' })).toBeDisabled()
    expect(screen.getByRole('tab', { name: /Checks 1\/1/i })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /All checks passed/i })).not.toBeInTheDocument()
    expect(mockApi.pullRequestSource).toHaveBeenLastCalledWith(github.url, true)

    resolveRefresh({ ...github, checks: [pendingCheck] })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Refresh pull request' })).toBeEnabled(),
    )
    expect(screen.getByRole('tab', { name: /Checks running 0\/1/i })).toBeInTheDocument()
  })
  it('uses bounded backoff only while checks remain pending', () => {
    const pending = [{ ...github.checks[0], bucket: 'pending' as const }]

    expect(pullRequestCheckPollDelay(pending, 0)).toBe(10_000)
    expect(pullRequestCheckPollDelay(pending, 1)).toBe(20_000)
    expect(pullRequestCheckPollDelay(pending, 2)).toBe(40_000)
    expect(pullRequestCheckPollDelay(pending, CHECK_POLL_MAX_FAILURES)).toBe(false)
    expect(pullRequestCheckPollDelay(github.checks, 0)).toBe(false)
  })

  it('polls only checks until pending CI completes, then stops', async () => {
    vi.useFakeTimers()
    const pendingCheck = {
      ...github.checks[0],
      status: 'IN_PROGRESS',
      conclusion: '',
      bucket: 'pending' as const,
    }
    mockApi.pullRequestSource.mockResolvedValue({ ...github, checks: [pendingCheck] })
    mockApi.pullRequestChecks
      .mockResolvedValueOnce({ checks: [pendingCheck] })
      .mockResolvedValueOnce({ checks: github.checks })
    const { client, unmount } = renderPanel()

    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(screen.getByRole('tab', { name: /Checks running 0\/1/i })).toBeInTheDocument()
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000)
        await Promise.resolve()
      })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(2)
      await act(async () => {
        await Promise.resolve()
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(
        client.getQueryData<PullRequestSource>(['pull-request-source', github.url])
          ?.checks[0].bucket,
      ).toBe('passed')
      expect(screen.getByRole('tab', { name: /All checks passed 1\/1/i })).toBeInTheDocument()
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)

      await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(2)
    } finally {
      unmount()
      vi.useRealTimers()
    }
  })

  it('stops polling and removes the spinner after three check-provider failures', async () => {
    vi.useFakeTimers()
    const pendingCheck = {
      ...github.checks[0],
      status: 'IN_PROGRESS',
      conclusion: '',
      bucket: 'pending' as const,
    }
    mockApi.pullRequestSource.mockResolvedValue({ ...github, checks: [pendingCheck] })
    mockApi.pullRequestChecks.mockRejectedValue(new Error('provider unavailable'))
    const { unmount } = renderPanel()

    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000)
        await Promise.resolve()
      })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(2)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(40_000)
        await Promise.resolve()
      })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(CHECK_POLL_MAX_FAILURES)
      const unavailableTab = screen.getByRole('tab', { name: /Checks unavailable 0\/1/i })
      expect(unavailableTab.querySelector('.animate-spin')).toBeNull()
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)

      await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(CHECK_POLL_MAX_FAILURES)
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)
    } finally {
      unmount()
      vi.useRealTimers()
    }
  })

  it('warns when provider page limits may hide results', async () => {
    mockApi.pullRequestSource.mockResolvedValue({
      ...github,
      partialSections: ['files', 'inline review comments'],
    })
    renderPanel()
    const warning = await screen.findByRole('status')
    expect(warning).toHaveTextContent('Provider results may be partial for files, inline review comments')
    expect(warning).toHaveTextContent('Open the pull request for the complete set')
  })

  it('shows commits, checks, and review comments in their tabs', async () => {
    const onAddToChat = vi.fn()
    renderPanel(onAddToChat)
    await screen.findByText('Add source tabs')

    fireEvent.click(screen.getByRole('tab', { name: /Commits 1/i }))
    expect(screen.getAllByText('Add source tabs')).toHaveLength(2)

    fireEvent.click(screen.getByRole('tab', { name: /All checks passed/i }))
    expect(screen.getByText('test')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open test check details' })).toHaveAttribute(
      'href',
      'https://github.com/acme/widgets/actions/1',
    )

    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))
    expect(screen.getByText('Please cover this case.')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: 'Add to chat' })[0])
    expect(onAddToChat).toHaveBeenCalledWith(
      expect.stringContaining('Quoting a pull request comment by reviewer'))
  })

  it('drops the source strip when there is only one source to pick', async () => {
    // A single-source host (the Code Review Sage detail pane, whose left rail
    // already chose the pull request) would otherwise get a tab bar holding one
    // tab that does nothing. Two sources: the strip earns its row.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={[{ url: github.url, provider: 'github', number: 12, repo: 'widgets' }]}
          selectedUrl={github.url}
          onSelect={vi.fn()}
        />
      </QueryClientProvider>,
    )
    await screen.findByText('Add source tabs')
    expect(screen.queryByRole('tablist', { name: 'Pull requests' })).not.toBeInTheDocument()
    // The SECTION bar is still there -- only the source picker is suppressed.
    expect(screen.getByRole('tablist', { name: 'Pull request sections' })).toBeInTheDocument()
  })

  it('offers the comment composer on a pull request with no comments yet', async () => {
    // The thread list owns the "comment on this pull request" box, so gating it on
    // an existing comment would make the FIRST comment the one you cannot post.
    mockApi.pullRequestSource.mockResolvedValue({ ...github, comments: [] })
    renderPanel()
    await screen.findByText('Add source tabs')
    const sections = screen.getByRole('tablist', { name: 'Pull request sections' })
    fireEvent.click(within(sections).getByRole('tab', { name: /Reviews/i }))
    expect(await screen.findByText(/Comment on this pull request/i)).toBeTruthy()
  })

  it('refreshes the SHARED source cache after a thread write', async () => {
    // Sage's detail pane observes the same key, so invalidating anything else
    // leaves both readers showing the state from before the write.
    mockApi.resolvePullRequestThread.mockResolvedValue({ resolved: true })
    const { client } = renderPanel()
    await screen.findByText('Add source tabs')
    const sections = screen.getByRole('tablist', { name: 'Pull request sections' })
    fireEvent.click(within(sections).getByRole('tab', { name: /Reviews/i }))
    const spy = vi.spyOn(client, 'invalidateQueries')
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    await waitFor(() => expect(mockApi.resolvePullRequestThread).toHaveBeenCalled())
    await waitFor(() => expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: ['pull-request-source', github.url] })))
  })

  it('hands a failed CI check off to chat', async () => {
    const failing = { ...github, checks: [...github.checks, { name: 'lint', workflow: 'CI', status: 'COMPLETED', conclusion: 'FAILURE', bucket: 'failed' as const, url: 'https://github.com/acme/widgets/actions/2', startedAt: '', completedAt: '' }] }
    mockApi.pullRequestSource.mockImplementation((url: string) => Promise.resolve(new URL(url).hostname === 'gitlab.com' ? gitlab : failing))
    const onAddToChat = vi.fn()
    renderPanel(onAddToChat)
    await screen.findByText('Add source tabs')
    // Scoped to the section tablist: the source strip above it now carries CI
    // state labels of its own, so a bare /Checks/ query spans both lists.
    const sections = screen.getByRole('tablist', { name: 'Pull request sections' })
    fireEvent.click(within(sections).getByRole('tab', { name: /Checks/i }))
    // Only the failed check exposes Add to chat.
    const addButtons = screen.getAllByRole('button', { name: 'Add to chat' })
    expect(addButtons).toHaveLength(1)
    fireEvent.click(addButtons[0])
    expect(onAddToChat).toHaveBeenCalledWith(expect.stringContaining('Failing CI check'))
    expect(onAddToChat).toHaveBeenCalledWith(expect.stringContaining('lint'))
  })

  it('does not render provider-controlled non-HTTP(S) URLs as links', async () => {
    const unsafe: PullRequestSource = {
      ...github,
      url: 'javascript:alert(1)',
      commits: [{ ...github.commits[0], url: 'data:text/html,unsafe' }],
      checks: [{ ...github.checks[0], url: 'javascript:alert(2)' }],
      comments: [{ ...github.comments[0], url: 'file:///tmp/unsafe' }],
    }
    mockApi.pullRequestSource.mockResolvedValue(unsafe)
    const { container } = renderPanel()
    await screen.findByText('Add source tabs')
    expect(screen.queryByRole('link', { name: 'Open pull request' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Commits 1/i }))
    expect(screen.getAllByText('Add source tabs')).toHaveLength(2)
    expect(screen.getAllByText('Add source tabs')[1].closest('a')).toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: /All checks passed/i }))
    expect(screen.queryByRole('link', { name: 'Open check details' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Reviews 1/i }))
    expect(container.querySelector('a[href^="javascript:"], a[href^="data:"], a[href^="file:"]')).toBeNull()
  })

  it('collapses and expands a comment from the header chevron', async () => {
    renderPanel()
    await screen.findByText('Add source tabs')
    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))
    expect(screen.getByText('Please cover this case.')).toBeInTheDocument()

    fireEvent.click(screen.getAllByRole('button', { name: 'Collapse comment' })[0])
    expect(screen.queryByText('Please cover this case.')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Expand comment' }))
    expect(screen.getByText('Please cover this case.')).toBeInTheDocument()
  })

  it('shows Resolve only on resolvable unresolved comments and posts the resolution', async () => {
    renderPanel()
    await screen.findByText('Add source tabs')
    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))

    // Only the open, resolvable thread offers Resolve; the top-level comment has
    // no thread to resolve. Matched exactly -- /Resolve/i also catches the
    // "Hide resolved" toggle.
    expect(screen.getAllByRole('button', { name: 'Resolve' })).toHaveLength(1)

    // Resolved threads are settled business, so they are behind a toggle rather
    // than inline. Revealing one offers Reopen, not Resolve.
    expect(screen.queryByRole('button', { name: 'Reopen' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Show resolved/i }))
    expect(screen.getByRole('button', { name: 'Reopen' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    await waitFor(() =>
      expect(mockApi.resolvePullRequestThread).toHaveBeenCalledWith(github.url, 'PRRT_thread1'),
    )
  })

  it('shows an inline error when resolving fails', async () => {
    mockApi.resolvePullRequestThread.mockRejectedValue(new Error('boom'))
    renderPanel()
    await screen.findByText('Add source tabs')
    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))

    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    // The provider's own message, surfaced next to the button that failed.
    expect(await screen.findByText('boom')).toBeInTheDocument()
  })

  it('fetches the selected GitLab merge request', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={[{ url: gitlab.url, provider: 'gitlab', number: 7, repo: 'service' }]}
          selectedUrl={gitlab.url}
          onSelect={() => {}}
          onAddToChat={() => {}}
        />
      </QueryClientProvider>,
    )
    expect(await screen.findByText('GitLab source')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.pullRequestSource).toHaveBeenCalledWith(gitlab.url, false))
    expect(screen.getByText('Merged')).toBeInTheDocument()
  })
})

function renderSource(source: PullRequestSource) {
  mockApi.pullRequestSource.mockImplementation(() => Promise.resolve(source))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <PullRequestPanel
        sources={[{ url: source.url, provider: source.provider, number: source.number, repo: 'widgets' }]}
        selectedUrl={source.url}
        onSelect={() => {}}
        onAddToChat={() => {}}
      />
    </QueryClientProvider>,
  )
}

describe('PullRequestPanel actions', () => {
  it('requires a confirming click before arming auto-merge', async () => {
    renderSource(github)
    const enable = await screen.findByRole('button', { name: /Enable auto-merge/i })
    // A draft-only action must not appear on a ready pull request.
    expect(screen.queryByRole('button', { name: /Ready for review/i })).not.toBeInTheDocument()

    fireEvent.click(enable)
    expect(mockApi.enablePullRequestAutoMerge).not.toHaveBeenCalled()
    const confirm = await screen.findByRole('button', { name: /Confirm auto-merge/i })
    expect(screen.getByText(/authorizes the merge/i)).toBeInTheDocument()

    fireEvent.click(confirm)
    // The confirming click asserts nothing about the merge being immediate --
    // only the server knows that, so the acknowledgement is withheld until it asks.
    await waitFor(() =>
      expect(mockApi.enablePullRequestAutoMerge).toHaveBeenCalledWith(github.url, false),
    )
  })

  it('sends the acknowledgement only after the server says the merge is immediate', async () => {
    // Shaped like the real ApiError: the message is already unwrapped to prose,
    // so the structured marker survives only on the raw body.
    const refusal = JSON.stringify({
      error: 'No pipeline is pending, so GitLab would merge this merge request immediately.',
      confirmationRequired: true,
    })
    const apiError = Object.assign(
      new Error('No pipeline is pending, so GitLab would merge this merge request immediately.'),
      { status: 400, body: refusal },
    )
    mockApi.enablePullRequestAutoMerge
      .mockRejectedValueOnce(apiError)
      .mockResolvedValueOnce({ autoMerge: true, mergeMethod: 'pipeline' })
    renderSource(github)

    fireEvent.click(await screen.findByRole('button', { name: /Enable auto-merge/i }))
    fireEvent.click(await screen.findByRole('button', { name: /Confirm auto-merge/i }))

    // The server's own words become the prompt, so the warning describes the
    // real situation instead of a generic caption.
    expect(await screen.findByRole('alert')).toHaveTextContent(/merge this merge request immediately/i)
    const mergeNow = await screen.findByRole('button', { name: /Merge now/i })
    fireEvent.click(mergeNow)

    await waitFor(() =>
      expect(mockApi.enablePullRequestAutoMerge).toHaveBeenLastCalledWith(github.url, true),
    )
  })

  it('keeps confirm and cancel as separate targets so a double-click cannot arm it', async () => {
    renderSource(github)
    const enable = await screen.findByRole('button', { name: /Enable auto-merge/i })
    fireEvent.click(enable)

    // A second click at the original position lands on Cancel, which stands
    // where the arming button was, so an accidental double-click backs out.
    const cancel = await screen.findByRole('button', { name: /^Cancel$/i })
    expect(cancel).not.toBe(await screen.findByRole('button', { name: /Confirm auto-merge/i }))
    fireEvent.click(cancel)

    expect(await screen.findByRole('button', { name: /Enable auto-merge/i })).toBeInTheDocument()
    expect(mockApi.enablePullRequestAutoMerge).not.toHaveBeenCalled()
  })

  it('offers only the ready action on a draft pull request', async () => {
    renderSource({ ...github, draft: true })
    const ready = await screen.findByRole('button', { name: /Ready for review/i })
    // GitHub rejects auto-merge on a draft, so the button is withheld until the
    // pull request leaves draft rather than failing on click.
    expect(screen.queryByRole('button', { name: /auto-merge/i })).not.toBeInTheDocument()

    fireEvent.click(ready)
    await waitFor(() => expect(mockApi.markPullRequestReady).toHaveBeenCalledWith(github.url))
  })

  it('reports armed auto-merge as state instead of an action', async () => {
    renderSource({ ...github, autoMerge: true })
    expect(await screen.findByText('Auto-merge enabled')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Enable auto-merge/i })).not.toBeInTheDocument()
  })

  it('hides both actions once the pull request is no longer live', async () => {
    renderSource(gitlab)
    await screen.findByText('GitLab source')
    expect(screen.queryByRole('button', { name: /auto-merge/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Ready for review/i })).not.toBeInTheDocument()
  })

  it('surfaces the provider reason inline when an action fails', async () => {
    mockApi.markPullRequestReady.mockRejectedValue(
      new Error(JSON.stringify({ error: 'This pull request is already ready for review.' })),
    )
    renderSource({ ...github, draft: true })
    fireEvent.click(await screen.findByRole('button', { name: /Ready for review/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('already ready for review')
  })
})

describe('pullRequestIsLive', () => {
  it('accepts open and draft states and rejects terminal ones', () => {
    expect(pullRequestIsLive(github)).toBe(true)
    expect(pullRequestIsLive({ ...github, state: 'opened' })).toBe(true)
    expect(pullRequestIsLive({ ...github, state: 'closed' })).toBe(false)
    expect(pullRequestIsLive({ ...github, mergedAt: '2026-07-13T10:00:00Z' })).toBe(false)
  })
})

describe('pullRequestMergeBlocker', () => {
  it('flags conflicts as danger with a resolve handoff that reserves force-push for unshared branches', () => {
    const blocker = pullRequestMergeBlocker({ ...github, mergeable: 'conflicting', mergeStateStatus: 'dirty' })
    expect(blocker).toMatchObject({ tone: 'danger', title: 'Merge conflicts' })
    expect(blocker?.detail).toContain('conflicts with main')
    expect(blocker?.handoff).toContain('Resolve the conflicts with main')
    expect(blocker?.handoff).toContain('prefer merging main into the branch')
    expect(blocker?.handoff).toContain('--force-with-lease')
    expect(blocker?.handoff).not.toContain('force-push')
    expect(blocker?.handoff).toContain(github.url)
  })

  it('flags behind-base as warn with a no-history-rewrite update handoff', () => {
    const blocker = pullRequestMergeBlocker({ ...github, mergeable: 'mergeable', mergeStateStatus: 'behind' })
    expect(blocker).toMatchObject({ tone: 'warn', title: 'Branch is behind' })
    expect(blocker?.handoff).toContain('without rewriting history')
    expect(blocker?.handoff).toContain('merge main into the branch')
    expect(blocker?.handoff).not.toContain('--force-with-lease')
  })

  it('flags branch-protection blocks as warn without a handoff', () => {
    const blocker = pullRequestMergeBlocker({ ...github, mergeable: 'mergeable', mergeStateStatus: 'blocked' })
    expect(blocker).toMatchObject({ tone: 'warn', title: 'Merge blocked' })
    expect(blocker?.handoff).toBeUndefined()
  })

  it("banners open GitLab MRs, whose raw provider state is 'opened'", () => {
    const blocker = pullRequestMergeBlocker({
      ...gitlab,
      state: 'opened',
      mergedAt: '',
      mergeable: 'conflicting',
      mergeStateStatus: 'dirty',
    })
    expect(blocker).toMatchObject({ tone: 'danger', title: 'Merge conflicts' })
    expect(blocker?.handoff).toContain('MR !7')
  })

  it('flags GitLab need_rebase with a rebase-aware handoff, never a merge-commit suggestion', () => {
    const blocker = pullRequestMergeBlocker({
      ...gitlab,
      state: 'opened',
      mergedAt: '',
      mergeable: 'mergeable',
      mergeStateStatus: 'need_rebase',
    })
    expect(blocker).toMatchObject({ tone: 'warn', title: 'Rebase required' })
    expect(blocker?.handoff).toContain('merge commits will not unblock the MR')
    expect(blocker?.handoff).toContain('--force-with-lease')
    expect(blocker?.handoff).not.toContain('merge main into the branch')
  })

  it('returns null for clean, unknown, draft, merged, closed, and locked states', () => {
    expect(pullRequestMergeBlocker({ ...github, mergeable: 'mergeable', mergeStateStatus: 'clean' })).toBeNull()
    expect(pullRequestMergeBlocker(github)).toBeNull()
    expect(pullRequestMergeBlocker({ ...github, mergeable: 'unknown', mergeStateStatus: 'unknown' })).toBeNull()
    expect(pullRequestMergeBlocker({ ...github, draft: true, mergeable: 'conflicting' })).toBeNull()
    expect(pullRequestMergeBlocker({ ...github, state: 'MERGED', mergedAt: '2026-07-13T10:00:00Z', mergeable: 'conflicting' })).toBeNull()
    expect(pullRequestMergeBlocker({ ...github, state: 'CLOSED', mergeable: 'conflicting' })).toBeNull()
    expect(pullRequestMergeBlocker({ ...github, state: 'LOCKED', mergeable: 'conflicting' })).toBeNull()
  })

  it('names the base branch generically when the provider omitted it', () => {
    const blocker = pullRequestMergeBlocker({ ...github, baseBranch: '', mergeable: 'conflicting' })
    expect(blocker?.detail).toContain('conflicts with the base branch')
  })
})

describe('PullRequestPanel merge-blocker banner', () => {
  it('shows a conflict banner and hands the rebase off to chat', async () => {
    mockApi.pullRequestSource.mockResolvedValue({ ...github, mergeable: 'conflicting', mergeStateStatus: 'dirty' })
    const { onAddToChat } = renderPanel()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Merge conflicts')
    expect(alert).toHaveTextContent('conflicts with main')

    fireEvent.click(screen.getByRole('button', { name: 'Add to chat' }))
    expect(onAddToChat).toHaveBeenCalledWith(expect.stringContaining('Resolve the conflicts with main'))
    expect(onAddToChat).toHaveBeenCalledWith(expect.stringContaining('PR #12'))
  })

  it('shows no banner when the pull request is mergeable', async () => {
    mockApi.pullRequestSource.mockResolvedValue({ ...github, mergeable: 'mergeable', mergeStateStatus: 'clean' })
    renderPanel()

    expect(await screen.findByText('Add source tabs')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

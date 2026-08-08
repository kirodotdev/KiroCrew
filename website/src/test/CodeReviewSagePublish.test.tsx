import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PullRequestSource } from '../types'

const mockApi = vi.hoisted(() => ({
  pullRequestChecks: vi.fn(),
  pullRequestSource: vi.fn(),
  pullRequestStatuses: vi.fn(),
  resolvePullRequestThread: vi.fn(),
  enablePullRequestAutoMerge: vi.fn(),
  markPullRequestReady: vi.fn(),
  pullRequestPendingReview: vi.fn(),
  submitPullRequestReview: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

import DraftReviewActions from '../apps/code-review-sage/components/DraftReviewActions'

const PR_URL = 'https://github.com/acme/widgets/pull/12'

const source: PullRequestSource = {
  provider: 'github',
  url: PR_URL,
  number: 12,
  title: 'Add source tabs',
  description: 'Summary.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: '2026-08-05T12:00:00Z',
  headBranch: 'feature/tabs',
  baseBranch: 'main',
  headSha: 'abcdef123456',
  author: 'octocat',
  additions: 3,
  deletions: 1,
  changedFiles: 1,
  files: [{ path: 'src/a.ts', status: 'modified', additions: 3, deletions: 1, patch: '@@ -1 +1 @@\n-a\n+b' }],
  commits: [],
  checks: [],
  comments: [],
}

// The page reads its own app endpoints with bare fetch, so only those are stubbed
// here; every pull-request read/write goes through the mocked api client.
const SAGE_PAYLOADS: Record<string, unknown> = {
  '/runs': {
    runs: [{
      run_id: 'r1',
      status: 'done',
      changes: [PR_URL],
      change_ids: ['GH-acme-widgets-12'],
      progress: { 'GH-acme-widgets-12': { phase: 'done', counts: { red: 1, yellow: 2 } } },
    }],
  },
  '/settings': {
    settings: { model: null, effort: '', active_namespaces: ['default'], max_concurrent: 5 },
    models: [], efforts: ['low'], namespaces: ['default'], max_concurrent_max: 30,
  },
  '/namespaces': { namespaces: [{ name: 'default', patterns: 0, candidate: 0, active: true }], active: ['default'] },
  '/learnings': { namespace: 'default', patterns: [], candidate: [] },
}

/** Mount the publish bar on its own.
 *
 *  It fetches its own draft and owns its own state, so it needs nothing from the
 *  app around it -- which is why it can hang in the review detail pane at all. */
// Defaults to the id the mocked pending draft carries: a missing id BLOCKS
// publishing, so a case that is not about identity has to look identified.
function openReviewedPr(draftDelivered = true, expectedReviewId = '4242') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DraftReviewActions
        url={PR_URL}
        draftDelivered={draftDelivered}
        expectedReviewId={expectedReviewId}
      />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(fn => fn.mockReset())
  mockApi.pullRequestSource.mockResolvedValue(source)
  mockApi.pullRequestChecks.mockResolvedValue({ checks: [] })
  mockApi.pullRequestStatuses.mockResolvedValue({ statuses: {} })
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', comments: [], body: '[code-review-sage] draft',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  mockApi.submitPullRequestReview.mockResolvedValue({ submitted: true, event: 'APPROVE' })
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const match = Object.keys(SAGE_PAYLOADS).find(suffix => url.includes(suffix))
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(match ? SAGE_PAYLOADS[match] : {}),
    } as Response)
  }))
})

afterEach(() => { vi.unstubAllGlobals() })

it('publishes the draft with the review id it was shown', async () => {
  openReviewedPr()
  const approve = await screen.findByRole('button', { name: 'Approve' })
  fireEvent.click(approve)
  // Publishing takes two clicks now: the verdict arms a confirmation
  // that restates it, and only this button sends.
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  await waitFor(() => expect(mockApi.submitPullRequestReview).toHaveBeenCalledTimes(1))
  // The id comes from the fetched draft, never a blank or assumed value — a blank
  // id would make the backend resolve whatever draft exists instead.
  expect(mockApi.submitPullRequestReview).toHaveBeenCalledWith(PR_URL, '4242', 'APPROVE', 'd1')
  // The bar collapses to the outcome: the draft is gone, so re-publishing it must
  // not be offered.
  expect(await screen.findByText('Published as')).toBeTruthy()
  // Past tense, not the imperative button label ("Published as Approve").
  expect(screen.getByText('approved')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('shows the draft body before offering to publish it', async () => {
  openReviewedPr()
  // Publishing is irreversible, and a pending review may be one the human started
  // by hand — the contents must be on screen before a verdict button is.
  expect(await screen.findByText('[code-review-sage] draft')).toBeTruthy()
})

it('renders a payload without a comments field instead of crashing', async () => {
  // A bundle newer than the backend answering it gets no `comments`. An unconditional
  // `.length` there takes down the whole panel via the error boundary, so the absence
  // has to read as "no inline comments".
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', body: '[code-review-sage] draft',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: false,
    contentDigest: 'd1', staleDismissalEnabled: true,
  })
  openReviewedPr()
  expect(await screen.findByText('[code-review-sage] draft')).toBeTruthy()
  expect(await screen.findByRole('button', { name: 'Submit as comment' })).toBeTruthy()
})

it('shows the inline comments too, not just the body', async () => {
  // `contentDigest` binds the body AND the inline comments, so a panel that rendered
  // only the body would have the digest certify text the reader never saw -- consent
  // to a summary standing in for consent to whatever hides behind it.
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', body: '[code-review-sage] draft',
    comments: [{ path: 'src/auth.py', line: 42, body: 'this widens the token scope' }],
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: false,
    contentDigest: 'd1', staleDismissalEnabled: true,
  })
  openReviewedPr()
  expect(await screen.findByText(/widens the token scope/)).toBeTruthy()
  // The anchor is shown too: WHERE a comment lands is part of what publishing does.
  expect(screen.getByText('src/auth.py:42')).toBeTruthy()
})

it('withholds the verdicts when the run records no draft id', async () => {
  // Delivered, but with nothing to identify the draft by -- so it cannot be proven to
  // be this run's, and an unprovable publish is exactly what the refusal is for.
  // A missing id must read as superseded, not as permission.
  openReviewedPr(true, '')
  expect(await screen.findByText(/later review replaced this draft/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Submit as comment' })).toBeNull()
})

it('keeps the keyboard on the confirm and lets Escape back out', async () => {
  // Arming swaps the button out, so without an explicit focus move the keyboard lands on
  // <body> and Tab restarts at the top of the page — on the one irreversible action.
  openReviewedPr()
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  const confirm = await screen.findByRole('button', { name: 'Approve' })
  expect(document.activeElement).toBe(confirm)

  fireEvent.keyDown(confirm, { key: 'Escape' })
  // Back to the three verdicts, nothing sent.
  expect(await screen.findByRole('button', { name: 'Request changes' })).toBeTruthy()
  expect(screen.queryByText('Approve this pull request?')).toBeNull()
  expect(mockApi.submitPullRequestReview).not.toHaveBeenCalled()
})

it('disarms when the pull request changes under an armed verdict', async () => {
  // The armed confirmation belongs to the PR that was on screen when it was armed.
  // Carrying it across a selection change would publish on a PR never looked at.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={client}>
      <DraftReviewActions url={PR_URL} draftDelivered expectedReviewId="4242" />
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  expect(await screen.findByText('Approve this pull request?')).toBeTruthy()

  view.rerender(
    <QueryClientProvider client={client}>
      <DraftReviewActions
        url="https://github.com/o/r/pull/99"
        draftDelivered
        expectedReviewId="4242"
      />
    </QueryClientProvider>,
  )
  // The confirm row is gone; the verdicts are offered afresh for the new pull request.
  expect(await screen.findByRole('button', { name: 'Request changes' })).toBeTruthy()
  expect(screen.queryByText('Approve this pull request?')).toBeNull()
  expect(mockApi.submitPullRequestReview).not.toHaveBeenCalled()
})

it('one click alone publishes nothing — the verdict has to be confirmed', async () => {
  // The copy right here calls publishing irreversible and public, and the lighter post
  // step in the same pane already confirms. Arming must not be sending.
  openReviewedPr()
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  expect(await screen.findByText('Approve this pull request?')).toBeTruthy()
  expect(mockApi.submitPullRequestReview).not.toHaveBeenCalled()
})

it('withholds the verdicts when a later run replaced the draft', async () => {
  // The pending draft's id is not the one this run created, so it belongs to a later
  // run -- publishing here would submit its findings under this run's view.
  openReviewedPr(true, '9999')
  expect(await screen.findByText(/later review replaced this draft/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Submit as comment' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('offers the verdicts when the pending draft is the one this run created', async () => {
  openReviewedPr(true, '4242')
  expect(await screen.findByRole('button', { name: 'Submit as comment' })).toBeTruthy()
})

it('offers all three verdicts for a pending draft', async () => {
  openReviewedPr()
  expect(await screen.findByRole('button', { name: 'Submit as comment' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Request changes' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy()
})

it('shows no publish controls when the pull request has no draft', async () => {
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '', comments: [], body: '', commitId: '', headSha: '',
    stale: false, contentRedacted: false, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  openReviewedPr()
  expect(await screen.findByText('No draft review on this pull request.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('reports a failed draft read as a failure, never as "no draft"', async () => {
  // The read is the only thing that knows whether a draft exists, so rendering its
  // failure as an absence tells the user their pending review is gone. `retry:false`
  // means the retry button is the sole way back.
  mockApi.pullRequestPendingReview.mockRejectedValue(new Error('gh: not authenticated'))
  openReviewedPr()
  expect(await screen.findByText(/Couldn't check for a draft review/)).toBeTruthy()
  expect(screen.queryByText('No draft review on this pull request.')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()

  const retry = screen.getByRole('button', { name: 'Try again' })
  const before = mockApi.pullRequestPendingReview.mock.calls.length
  fireEvent.click(retry)
  await waitFor(() =>
    expect(mockApi.pullRequestPendingReview.mock.calls.length).toBeGreaterThan(before))
})

it('blocks publishing a draft whose text needs redaction', async () => {
  // The body renders redacted, but submitting would post GitHub's original text —
  // so the buttons must be absent, not merely fail on click.
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', comments: [], body: 'use [REDACTED] to deploy',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: true, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  openReviewedPr()
  expect(await screen.findByText(/must be redacted/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Submit as comment' })).toBeNull()
})

it('blocks publishing a draft written against an earlier commit', async () => {
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', comments: [], body: '[code-review-sage] draft',
    commitId: 'aaaaaaaaaaaa', headSha: 'abcdef123456',
    stale: true, contentRedacted: false, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  openReviewedPr()
  expect(await screen.findByText(/written against an earlier commit/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('surfaces a rejected publish instead of claiming success', async () => {
  mockApi.submitPullRequestReview.mockRejectedValue(
    new Error('This draft review is no longer pending -- it was already submitted or replaced.'),
  )
  openReviewedPr()
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  // Two clicks: the verdict arms a confirmation that restates it, and only
  // this button sends.
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  expect(await screen.findByText(/no longer pending/)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy()
})

it('reads the draft for the pull request it was given, and only that one', () => {
  // The bar is mounted per pull request, so the URL it receives is the whole of
  // its scope -- it must never resolve "whatever draft exists". Where it mounts
  // at all is the detail pane's call (gated on a run existing for that PR), which
  // is what keeps a never-reviewed PR from reading a draft.
  openReviewedPr()
  expect(mockApi.pullRequestPendingReview).toHaveBeenCalledTimes(1)
  expect(mockApi.pullRequestPendingReview).toHaveBeenCalledWith(PR_URL)
})

it('withholds Approve while auto-merge is armed, and says why', async () => {
  // The one case the post-submit stale-head dismissal cannot repair: an approval
  // can satisfy branch protection and let GitHub merge before the dismissal lands.
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', comments: [], body: '[code-review-sage] draft',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: true, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  openReviewedPr()
  expect(await screen.findByText(/Approve is unavailable while auto-merge is armed/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  // The non-gating verdicts stay available — only APPROVE can let a merge through.
  expect(screen.getByRole('button', { name: 'Submit as comment' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Request changes' })).toBeTruthy()
})

it('sends the digest of the draft it displayed, not a blank', async () => {
  // A blank digest would make the backend skip the content check entirely, so the
  // UI must forward the one it rendered.
  openReviewedPr()
  fireEvent.click(await screen.findByRole('button', { name: 'Submit as comment' }))
  // Two clicks: the verdict arms a confirmation that restates it, and only
  // this button sends.
  fireEvent.click(await screen.findByRole('button', { name: 'Submit as comment' }))
  await waitFor(() => expect(mockApi.submitPullRequestReview).toHaveBeenCalled())
  const args = mockApi.submitPullRequestReview.mock.calls[0]
  expect(args[3]).toBe('d1')
})

it('withholds Approve when the base branch keeps stale approvals', async () => {
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', comments: [], body: '[code-review-sage] draft',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: false,
    contentDigest: 'd1', staleDismissalEnabled: false,
  })
  openReviewedPr()
  expect(await screen.findByText(/does not dismiss approvals when new commits/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Submit as comment' })).toBeTruthy()
})

it('withholds publishing while the row is still being reviewed', async () => {
  // A re-review leaves the PREVIOUS run's draft on the pull request until the new
  // one posts, so publishing from a running row would irreversibly submit obsolete
  // findings. Neither existing defence catches it: the content digest binds "what
  // you saw" to "what you send" and both are that same stale draft, and the
  // stale-head check does not fire because re-reviewing never moves head.sha.
  // Reading the draft stays available; only the verdicts are withheld.
  openReviewedPr(false)
  expect(await screen.findByText(/this review is still running/)).toBeTruthy()
  // The draft is still readable...
  expect(screen.getByText(/\[code-review-sage\] draft/)).toBeTruthy()
  // ...but no verdict can be sent.
  for (const name of ['Submit as comment', 'Request changes', 'Approve']) {
    expect(screen.queryByRole('button', { name })).toBeNull()
  }
  expect(mockApi.submitPullRequestReview).not.toHaveBeenCalled()
})

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ReviewQueueResponse } from '../apps/code-review-sage/lib/types'
import type { SageContextValue } from '../apps/code-review-sage/context'

/**
 * The rail's "Requests" tab: it feeds `gh search prs --review-requested=@me`
 * rows into the shared pick list, and treats an unset-up gh as a first-run
 * notice rather than as an error.
 */
const sage: Record<string, unknown> = {}
const queue = vi.fn<() => Promise<ReviewQueueResponse>>()

vi.mock('../apps/code-review-sage/context', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/code-review-sage/context')>()
  return { ...actual, useSage: () => sage as unknown as SageContextValue }
})
vi.mock('../apps/code-review-sage/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/code-review-sage/api')>()
  return { ...actual, sageApi: { ...actual.sageApi, reviewQueue: () => queue() } }
})

import MiddleColumn from '../apps/code-review-sage/components/MiddleColumn'

const URL = 'https://github.com/zzzacme/widgets/pull/1'

function renderColumn() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MiddleColumn /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.keys(sage).forEach(k => delete sage[k])
  Object.assign(sage, {
    runs: [], repoRuns: [], runsLoading: false, runsError: null, selectedRunId: null,
    selectRun: vi.fn(), listTab: 'queue', setListTab: vi.fn(), activeRepo: null,
    setMainView: vi.fn(), deleteRun: vi.fn(), deleting: null,
    startReview: { mutate: vi.fn(), isPending: false, error: null },
    startRepoReview: { mutate: vi.fn(), isPending: false, error: null },
    startReviewLinks: { mutate: vi.fn(), isPending: false, error: null },
    openAddRepos: vi.fn(), selectedPr: null, selectPr: vi.fn(),
    reviewingChangeUrls: new Set<string>(),
    prs: [], prsLoading: false, prsError: null, refreshPrs: vi.fn(),
  })
})

describe('MiddleColumn review queue tab', () => {
  it('lists the queued PRs with their repo and the review note', async () => {
    queue.mockResolvedValue({
      prs: [{
        url: URL, number: 1, title: 'zzz queued change', repo: 'zzzacme/widgets',
        head_sha: '', change_id: 'cid', reviewed: false, reviewed_stale: false,
      }],
    })
    renderColumn()
    expect(await screen.findByText('zzz queued change')).toBeInTheDocument()
    expect(screen.getByText('zzzacme/widgets')).toBeInTheDocument()
    expect(screen.getByText(/Each pick runs a Sage review/)).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Requests', exact: true })).toHaveAttribute('aria-selected', 'true')
  })

  it('says only the newest requests are shown when the queue was cut off', async () => {
    queue.mockResolvedValue({ prs: [], truncated: true })
    renderColumn()
    expect(await screen.findByText(/Only the newest 100 requests/)).toBeInTheDocument()
  })

  it('shows the gh setup notice, not an error, when gh is not set up', async () => {
    queue.mockResolvedValue({ prs: [], setup_required: true })
    renderColumn()
    expect(await screen.findByText('GitHub CLI not ready')).toBeInTheDocument()
    expect(screen.getByText(/Set up the GitHub CLI/)).toBeInTheDocument()
    expect(screen.queryByText(/add any repo by URL/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows a failed re-read as an error even after a cached setup answer', async () => {
    queue.mockResolvedValueOnce({ prs: [], setup_required: true })
    queue.mockRejectedValueOnce(new Error('zzz upstream service error'))
    renderColumn()
    expect(await screen.findByText('GitHub CLI not ready')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Refresh pull requests/i }))
    expect(await screen.findByText(/zzz upstream service error/)).toBeInTheDocument()
    expect(screen.queryByText('GitHub CLI not ready')).not.toBeInTheDocument()
  })

  it('switches to the queue from another tab', async () => {
    sage.listTab = 'pulls'
    queue.mockResolvedValue({ prs: [] })
    renderColumn()
    await userEvent.click(screen.getByRole('tab', { name: 'Requests', exact: true }))
    expect(sage.setListTab).toHaveBeenCalledWith('queue')
  })
})

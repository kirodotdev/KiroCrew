import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({
  api: {
    runs: vi.fn(async () => ({
      runs: [{
        run_id: 'run-one',
        changes: ['https://github.com/acme/widgets/pull/7'],
        status: 'done',
        started_at: '2026-08-01T00:00:00Z',
      }],
      pool: null,
      reviewer: null,
    })),
    runReport: vi.fn(async () => ({
      run_id: 'run-one', status: 'done', ready: false, bands: { red: 0, yellow: 0, green: 0 },
      rows: [], generated_at: '2026-08-01T00:00:00Z', total: 0, report_slug: null,
    })),
    pinnedRepos: vi.fn(async () => ({ repos: [] })),
    review: vi.fn(async (changes: string[], model?: string) => ({
      run_id: 'run-started', changes, model,
    })),
    reviewLinks: vi.fn(async (links: string, model?: string) => ({
      run_id: 'run-started', changes: [links], model,
    })),
    reviewRepo: vi.fn(async (repo: string, force: boolean, model?: string) => ({
      run_id: 'run-started', repo, force, changes: [], skipped: 0, status: 'started', model,
    })),
  },
}))

vi.mock('../apps/code-review-sage/api', () => ({ sageApi: api }))

import { SageProvider, useSage } from '../apps/code-review-sage/context'

function Probe() {
  const {
    reviewModel, setReviewModel, selectPr, selectRun,
    startReview, startReviewLinks, startRepoReview,
  } = useSage()
  return (
    <>
      <output data-testid="review-model">{reviewModel}</output>
      <button type="button" onClick={() => setReviewModel(' model-concrete ')}>
        select concrete
      </button>
      <button type="button" onClick={() => setReviewModel('')}>
        select auto
      </button>
      <button type="button" onClick={() => startReview.mutate(['change-1'])}>
        start selected
      </button>
      <button type="button" onClick={() => startReviewLinks.mutate('link-1')}>
        start link
      </button>
      <button type="button" onClick={() => startRepoReview.mutate({ repo: 'acme/widgets', force: false })}>
        start repo
      </button>
      <button type="button" onClick={() => selectPr({
        url: 'https://github.com/acme/widgets/pull/7', change_id: 'GH-acme-widgets-7', number: 7,
      })}>
        select pr
      </button>
      <button type="button" onClick={() => selectRun('run-one')}>select run</button>
    </>
  )
}

function renderProvider() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <SageProvider>
        <Probe />
      </SageProvider>
    </QueryClientProvider>,
  )
}

describe('SageProvider review model state', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('passes a concrete selected model through every review-start mutation', async () => {
    const user = userEvent.setup()
    renderProvider()

    await user.click(screen.getByRole('button', { name: 'select concrete' }))
    await user.click(screen.getByRole('button', { name: 'start selected' }))
    await user.click(screen.getByRole('button', { name: 'start link' }))
    await user.click(screen.getByRole('button', { name: 'start repo' }))

    await waitFor(() => {
      expect(api.review).toHaveBeenCalledWith(['change-1'], 'model-concrete')
      expect(api.reviewLinks).toHaveBeenCalledWith('link-1', 'model-concrete')
      expect(api.reviewRepo).toHaveBeenCalledWith('acme/widgets', false, 'model-concrete')
    })
  })

  it('omits Auto at the mutation seam and keeps the choice across PR/run changes', async () => {
    const user = userEvent.setup()
    renderProvider()

    await user.click(screen.getByRole('button', { name: 'select concrete' }))
    await user.click(screen.getByRole('button', { name: 'select pr' }))
    await user.click(screen.getByRole('button', { name: 'select run' }))
    expect(screen.getByTestId('review-model')).toHaveTextContent('model-concrete')

    await user.click(screen.getByRole('button', { name: 'select auto' }))
    await user.click(screen.getByRole('button', { name: 'start selected' }))
    await user.click(screen.getByRole('button', { name: 'start link' }))
    await user.click(screen.getByRole('button', { name: 'start repo' }))

    await waitFor(() => {
      expect(api.review).toHaveBeenCalledWith(['change-1'])
      expect(api.reviewLinks).toHaveBeenCalledWith('link-1')
      expect(api.reviewRepo).toHaveBeenCalledWith('acme/widgets', false)
    })
  })
})

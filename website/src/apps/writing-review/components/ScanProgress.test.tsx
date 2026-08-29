/**
 * Contract tests for ``ScanProgress`` — the polling detail-pane view
 * shown while a scan is in flight.
 *
 * Behaviours pinned:
 *
 * 1. The component reads ``activeJobId`` from context and asks
 *    ``useQuery`` to poll ``writingReviewApi.getJob``.
 * 2. When the job status reaches ``done`` with a ``review_id``, it
 *    calls ``selectReview(review_id)`` and clears the in-flight state
 *    (activeJobId + docName + phase).
 * 3. When the job status is ``done`` without a ``review_id`` (edge
 *    case — persistence failure), it clears the in-flight state
 *    anyway so the pane doesn't stick.
 * 4. When the status is ``failed`` or ``interrupted``, it clears the
 *    in-flight state and renders the failure copy.
 * 5. It publishes the live ``phase`` to ``setActiveJobPhase`` so the
 *    sidebar in-progress card renders the same phase label.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'

vi.mock('../context', () => ({
  useWritingReview: vi.fn(),
}))
vi.mock('../api', () => ({
  writingReviewApi: { getJob: vi.fn() },
  WritingReviewApiError: class WritingReviewApiError extends Error {},
}))

const mockedUseQuery = vi.fn()
const mockedInvalidateQueries = vi.fn()
vi.mock('@tanstack/react-query', async () => {
  const actual =
    await vi.importActual<typeof import('@tanstack/react-query')>('@tanstack/react-query')
  return {
    ...actual,
    useQuery: (...args: unknown[]) => mockedUseQuery(...args),
    useQueryClient: () => ({ invalidateQueries: mockedInvalidateQueries }),
  }
})

import ScanProgress from './ScanProgress'
import { useWritingReview } from '../context'

const mockedUseWritingReview = vi.mocked(useWritingReview)

function makeFakeContextValueForScanProgress() {
  const setActiveJobId = vi.fn()
  const setActiveJobDocName = vi.fn()
  const setActiveJobPhase = vi.fn()
  const selectReview = vi.fn()
  const contextValue = {
    activeJobId: 'job-in-flight',
    setActiveJobId,
    setActiveJobDocName,
    setActiveJobPhase,
    selectReview,
    // Unused fields — supplied to satisfy the structural type
    selectedReviewId: null,
    reviewsQuery: {},
    reviewDetailQuery: {},
    settingsQuery: {},
    activeJobDocName: null,
    activeJobPhase: null,
    newReviewDialogOpen: false,
    openNewReviewDialog: vi.fn(),
    closeNewReviewDialog: vi.fn(),
    settingsDialogOpen: false,
    openSettingsDialog: vi.fn(),
    closeSettingsDialog: vi.fn(),
  }
  return { contextValue, setActiveJobId, setActiveJobDocName, setActiveJobPhase, selectReview }
}

describe('ScanProgress', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the spinner and phase label while the job is running', () => {
    const { contextValue } = makeFakeContextValueForScanProgress()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedUseQuery.mockReturnValue({ data: { status: 'running', phase: 'scanner' } })
    const { container } = render(<ScanProgress />)
    // Spinner icon carries ``aria-hidden`` — screen readers should hear
    // only the phase label text, not "loader icon".
    const spinner = container.querySelector('svg')
    expect(spinner).not.toBeNull()
    expect(spinner).toHaveAttribute('aria-hidden', 'true')
    // Some non-empty text renders below the spinner (title + phase label).
    expect((container.textContent ?? '').trim().length).toBeGreaterThan(0)
  })

  it('selects the finished review and clears in-flight state on status="done" with review_id', async () => {
    const { contextValue, selectReview, setActiveJobId } =
      makeFakeContextValueForScanProgress()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedUseQuery.mockReturnValue({
      data: { status: 'done', phase: 'done', review_id: 'review-abc' },
    })
    render(<ScanProgress />)
    // ``selectReview`` MUST be called with the finished review id so
    // the detail pane opens automatically; ``setActiveJobId(null)``
    // MUST also fire so the sidebar in-progress card disappears.
    await waitFor(() => {
      expect(selectReview).toHaveBeenCalledWith('review-abc')
      expect(setActiveJobId).toHaveBeenCalledWith(null)
    })
    // The reviews list MUST be invalidated so the newly-persisted
    // review appears in the sidebar rail.
    expect(mockedInvalidateQueries).toHaveBeenCalledWith({
      queryKey: ['writing-review', 'reviews'],
    })
  })

  it('clears in-flight state on status="done" WITHOUT a review_id (persistence-failure edge)', async () => {
    const { contextValue, selectReview, setActiveJobId } =
      makeFakeContextValueForScanProgress()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedUseQuery.mockReturnValue({
      data: { status: 'done', phase: 'done' /* no review_id */ },
    })
    render(<ScanProgress />)
    // Terminating the poll without a review MUST NOT crash and MUST
    // clear the in-flight state — leaving the pane stuck on
    // "Scanning" is a worse UX than a missing detail.
    await waitFor(() => expect(setActiveJobId).toHaveBeenCalledWith(null))
    expect(selectReview).not.toHaveBeenCalled()
  })

  it('clears in-flight state on status="failed" and renders the failure copy', async () => {
    const { contextValue, setActiveJobId } = makeFakeContextValueForScanProgress()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedUseQuery.mockReturnValue({
      data: { status: 'failed', phase: 'scanner' },
    })
    const { container } = render(<ScanProgress />)
    await waitFor(() => expect(setActiveJobId).toHaveBeenCalledWith(null))
    // A ``text-danger`` node MUST appear so the user sees why the
    // pane transitioned back to empty.
    expect(container.querySelector('.text-danger')).not.toBeNull()
  })

  it('clears in-flight state on status="interrupted"', async () => {
    const { contextValue, setActiveJobId } = makeFakeContextValueForScanProgress()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedUseQuery.mockReturnValue({
      data: { status: 'interrupted', phase: 'scanner' },
    })
    render(<ScanProgress />)
    await waitFor(() => expect(setActiveJobId).toHaveBeenCalledWith(null))
  })

  it('publishes the live phase to setActiveJobPhase so the sidebar in-progress card matches', async () => {
    const { contextValue, setActiveJobPhase } = makeFakeContextValueForScanProgress()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedUseQuery.mockReturnValue({
      data: { status: 'running', phase: 'cross_validate' },
    })
    render(<ScanProgress />)
    await waitFor(() =>
      expect(setActiveJobPhase).toHaveBeenCalledWith('cross_validate'),
    )
  })

  it('does not push state when the query has no data yet', () => {
    const { contextValue, selectReview, setActiveJobId, setActiveJobPhase } =
      makeFakeContextValueForScanProgress()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedUseQuery.mockReturnValue({ data: undefined })
    render(<ScanProgress />)
    // Guard the "no data yet" branch — the effect returns early if
    // ``jobQuery.data`` is falsy so no context setters fire.
    expect(selectReview).not.toHaveBeenCalled()
    expect(setActiveJobId).not.toHaveBeenCalled()
    expect(setActiveJobPhase).not.toHaveBeenCalled()
  })
})

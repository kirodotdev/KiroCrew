/**
 * Contract tests for ``ReviewList`` — the rail's scrollable history.
 *
 * Four code paths in the render:
 *
 * 1. ``reviewsQuery.isLoading`` → shows the loading placeholder.
 * 2. ``reviewsQuery.isError`` → shows the error placeholder.
 * 3. Empty list AND no in-progress card → shows the empty-history hint.
 * 4. Non-empty list OR in-progress card → shows the actual rail.
 *
 * The in-progress card is a special row prepended when a scan is in
 * flight; its ``aria-live=polite`` is what makes the sidebar
 * accessible during a scan.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('../context', () => ({
  useWritingReview: vi.fn(),
}))
vi.mock('./ReviewCard', () => ({
  default: ({ review }: { review: { id: string; doc_name: string } }) => (
    <div data-testid={`stub-review-card-${review.id}`}>{review.doc_name}</div>
  ),
}))

import ReviewList from './ReviewList'
import { useWritingReview } from '../context'

const mockedUseWritingReview = vi.mocked(useWritingReview)

type FakeReviewsQuery = {
  isLoading?: boolean
  isError?: boolean
  data?: { reviews: Array<{ id: string; doc_name: string; verdict: string; finding_count: number }> }
}

function makeFakeContextValueForReviewList(overrides: {
  reviewsQuery?: FakeReviewsQuery
  activeJobId?: string | null
  activeJobDocName?: string | null
  activeJobPhase?: string | null
} = {}) {
  return {
    reviewsQuery: overrides.reviewsQuery ?? { isLoading: false, isError: false, data: { reviews: [] } },
    selectedReviewId: null,
    selectReview: vi.fn(),
    activeJobId: overrides.activeJobId ?? null,
    activeJobDocName: overrides.activeJobDocName ?? null,
    activeJobPhase: overrides.activeJobPhase ?? null,
    // Unused fields — supplied so the type-cast below satisfies the
    // structural shape the component's destructure expects.
    newReviewDialogOpen: false,
    openNewReviewDialog: vi.fn(),
    closeNewReviewDialog: vi.fn(),
    settingsDialogOpen: false,
    openSettingsDialog: vi.fn(),
    closeSettingsDialog: vi.fn(),
    setActiveJobId: vi.fn(),
    setActiveJobDocName: vi.fn(),
    setActiveJobPhase: vi.fn(),
    reviewDetailQuery: {},
    settingsQuery: {},
  }
}

describe('ReviewList', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the loading placeholder while reviewsQuery.isLoading is true', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewList({
        reviewsQuery: { isLoading: true },
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewList />)
    // The loading branch renders a small muted text node with the
    // localised loading copy; assert something (non-empty) is there
    // and that no cards or in-progress card sit alongside it.
    expect((container.textContent ?? '').trim().length).toBeGreaterThan(0)
    expect(screen.queryByTestId('writing-review-in-progress-card')).toBeNull()
  })

  it('renders the error placeholder when reviewsQuery.isError is true', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewList({
        reviewsQuery: { isError: true },
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewList />)
    // The error branch renders a ``text-danger`` styled node. The
    // colour class is a semantic signal to the operator that fetching
    // the review list failed.
    expect(container.querySelector('.text-danger')).not.toBeNull()
  })

  it('renders the empty-history placeholder when the list is empty and no scan is running', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewList({
        reviewsQuery: { data: { reviews: [] } },
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewList />)
    // Empty state renders a FileText icon and a hint line — no card
    // stubs should be present.
    expect(container.querySelector('svg')).not.toBeNull()
    expect(screen.queryByTestId(/stub-review-card-/)).toBeNull()
    expect(screen.queryByTestId('writing-review-in-progress-card')).toBeNull()
  })

  it('renders one ReviewCard per persisted review', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewList({
        reviewsQuery: {
          data: {
            reviews: [
              { id: 'a1', doc_name: 'doc-A.md', verdict: 'green', finding_count: 0 },
              { id: 'b2', doc_name: 'doc-B.md', verdict: 'yellow', finding_count: 3 },
            ],
          },
        },
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    render(<ReviewList />)
    expect(screen.getByTestId('stub-review-card-a1')).toBeInTheDocument()
    expect(screen.getByTestId('stub-review-card-b2')).toBeInTheDocument()
  })

  it('renders the in-progress card while a scan is running, with the doc name', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewList({
        activeJobId: 'job-in-flight',
        activeJobDocName: 'demo.md',
        activeJobPhase: 'scanner',
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const inProgressCard = screen.getByTestId('writing-review-in-progress-card', {
      container: render(<ReviewList />).container,
    } as never)
    expect(inProgressCard).toBeInTheDocument()
    expect(inProgressCard.getAttribute('aria-live')).toBe('polite')
    expect(inProgressCard.textContent).toContain('demo.md')
  })

  it('renders a fallback label on the in-progress card when the doc name is not yet known', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewList({
        activeJobId: 'job-in-flight',
        activeJobDocName: null,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewList />)
    const inProgressCard = container.querySelector(
      '[data-testid="writing-review-in-progress-card"]',
    )
    expect(inProgressCard).not.toBeNull()
    // The fallback label MUST render as non-empty text so the sidebar
    // does not display a naked spinner while a scan is spinning up.
    expect((inProgressCard?.textContent ?? '').trim().length).toBeGreaterThan(0)
  })
})

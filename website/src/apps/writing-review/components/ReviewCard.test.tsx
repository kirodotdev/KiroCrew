/**
 * Contract tests for ``ReviewCard`` — a single row in the rail history.
 *
 * Behaviours pinned:
 *
 * 1. The card renders doc_name, a ``<VerdictBadge>``, and a finding-count
 *    line.
 * 2. Clicking the card body dispatches ``onSelect``.
 * 3. Clicking the delete button prompts ``window.confirm``; a confirmed
 *    delete calls ``writingReviewApi.deleteReview(review.id)``.
 * 4. A cancelled confirm does NOT dispatch the delete.
 * 5. A delete error surfaces to the card via a ``role=alert`` node.
 * 6. Deleting the currently-selected review clears the selection.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api', () => ({
  writingReviewApi: {
    deleteReview: vi.fn(),
  },
  WritingReviewApiError: class WritingReviewApiError extends Error {},
}))
vi.mock('../context', () => ({
  useWritingReview: vi.fn(),
}))

import ReviewCard from './ReviewCard'
import { writingReviewApi } from '../api'
import { useWritingReview } from '../context'

const mockedDeleteReview = vi.mocked(writingReviewApi.deleteReview)
const mockedUseWritingReview = vi.mocked(useWritingReview)

function makeFakeContextValueForReviewCard(overrides: {
  selectedReviewId?: string | null
} = {}) {
  const selectReview = vi.fn()
  return {
    contextValue: {
      selectedReviewId: overrides.selectedReviewId ?? null,
      selectReview,
      // Unused fields, filled to satisfy the structural type
      reviewsQuery: {},
      newReviewDialogOpen: false,
      openNewReviewDialog: vi.fn(),
      closeNewReviewDialog: vi.fn(),
      settingsDialogOpen: false,
      openSettingsDialog: vi.fn(),
      closeSettingsDialog: vi.fn(),
      activeJobId: null,
      setActiveJobId: vi.fn(),
      activeJobDocName: null,
      setActiveJobDocName: vi.fn(),
      activeJobPhase: null,
      setActiveJobPhase: vi.fn(),
      reviewDetailQuery: {},
      settingsQuery: {},
    },
    selectReview,
  }
}

function renderReviewCardWithQueryClient(props: {
  review: { id: string; doc_name: string; verdict: 'red' | 'yellow' | 'green'; finding_count: number; created_at?: number }
  isSelected?: boolean
  onSelect?: () => void
}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ReviewCard
        review={props.review as never}
        isSelected={props.isSelected ?? false}
        onSelect={props.onSelect ?? vi.fn()}
      />
    </QueryClientProvider>,
  )
}

describe('ReviewCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders doc name, verdict badge, and finding-count line', () => {
    const { contextValue } = makeFakeContextValueForReviewCard()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = renderReviewCardWithQueryClient({
      review: { id: 'r1', doc_name: 'design.md', verdict: 'yellow', finding_count: 5 },
    })
    expect(container.textContent).toContain('design.md')
    // Verdict badge renders as a nested <span>; assert one is present with
    // a warn-token background because this review is yellow.
    expect(container.querySelector('.bg-warn-subtle')).not.toBeNull()
    // Finding-count line is non-empty text under the card body.
    expect((container.textContent ?? '').length).toBeGreaterThan('design.md'.length)
  })

  it('dispatches onSelect when the card body is clicked', () => {
    const { contextValue } = makeFakeContextValueForReviewCard()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    const onSelectSpy = vi.fn()
    renderReviewCardWithQueryClient({
      review: { id: 'r1', doc_name: 'design.md', verdict: 'green', finding_count: 0 },
      onSelect: onSelectSpy,
    })
    // The card body button is the first button in the DOM (delete is
    // the second, in the top-right corner).
    const buttons = screen.getAllByRole('button')
    fireEvent.click(buttons[0])
    expect(onSelectSpy).toHaveBeenCalledTimes(1)
  })

  it('confirms and deletes when the delete button is clicked and confirm is accepted', async () => {
    const { contextValue } = makeFakeContextValueForReviewCard()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedDeleteReview.mockResolvedValueOnce(undefined as never)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderReviewCardWithQueryClient({
      review: { id: 'r1', doc_name: 'design.md', verdict: 'green', finding_count: 0 },
    })
    // Delete button is identified by its aria-label — the trash icon is
    // aria-hidden so screen readers rely on the outer label.
    const deleteButton = screen.getByRole('button', {
      name: /delete/i,
    })
    fireEvent.click(deleteButton)
    await waitFor(() => expect(mockedDeleteReview).toHaveBeenCalledWith('r1'))
  })

  it('does NOT dispatch delete when confirm is cancelled', () => {
    const { contextValue } = makeFakeContextValueForReviewCard()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderReviewCardWithQueryClient({
      review: { id: 'r1', doc_name: 'design.md', verdict: 'green', finding_count: 0 },
    })
    const deleteButton = screen.getByRole('button', { name: /delete/i })
    fireEvent.click(deleteButton)
    expect(mockedDeleteReview).not.toHaveBeenCalled()
  })

  it('surfaces the delete error message on the card', async () => {
    const { contextValue } = makeFakeContextValueForReviewCard()
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedDeleteReview.mockRejectedValueOnce(new Error('review vanished'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderReviewCardWithQueryClient({
      review: { id: 'r1', doc_name: 'design.md', verdict: 'green', finding_count: 0 },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete/i }))
    // The error surface is a ``role=alert`` element so a screen reader
    // announces the failure without the user having to visually scan
    // the card for a red-text update.
    const alertBox = await screen.findByRole('alert')
    expect(alertBox.textContent).toContain('review vanished')
  })

  it('clears the selection when the currently-selected review is deleted', async () => {
    const { contextValue, selectReview } = makeFakeContextValueForReviewCard({
      selectedReviewId: 'r1',
    })
    mockedUseWritingReview.mockReturnValue(
      contextValue as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedDeleteReview.mockResolvedValueOnce(undefined as never)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderReviewCardWithQueryClient({
      review: { id: 'r1', doc_name: 'design.md', verdict: 'green', finding_count: 0 },
      isSelected: true,
    })
    fireEvent.click(screen.getByRole('button', { name: /delete/i }))
    // A deleted selected review MUST clear the selection so the detail
    // pane resets to EmptyState rather than showing a "review not found"
    // error banner.
    await waitFor(() => expect(selectReview).toHaveBeenCalledWith(null))
  })
})

// The rail's scrollable history list. Empty state prompts the user to
// start their first review. When a scan is in flight, we prepend a
// non-selectable "in progress" card so the sidebar reflects the state
// the right pane's ScanProgress is already showing.
import { FileText, Loader2 } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { useWritingReview } from '../context'
import { phaseLabel } from '../lib/phaseLabels'
import ReviewCard from './ReviewCard'

export default function ReviewList() {
  const {
    reviewsQuery,
    selectedReviewId,
    selectReview,
    activeJobId,
    activeJobDocName,
    activeJobPhase,
  } = useWritingReview()

  if (reviewsQuery.isLoading) {
    return (
      <div className="p-3 text-[12px] text-muted">
        {i18nT('apps.writingReview.reviewList.loading')}
      </div>
    )
  }
  if (reviewsQuery.isError) {
    return (
      <div className="p-3 text-[12px] text-danger">
        {i18nT('apps.writingReview.reviewList.error')}
      </div>
    )
  }

  const reviewSummaries = reviewsQuery.data?.reviews ?? []
  const inProgressCard = activeJobId ? (
    <div
      className="flex flex-col gap-1 w-full px-3 py-2 text-left border-b border-border bg-accent-subtle"
      aria-live="polite"
      data-testid="writing-review-in-progress-card"
    >
      <div className="flex items-center gap-2">
        <Loader2
          className="lucide-inline text-accent animate-spin motion-reduce:animate-none"
          aria-hidden="true"
        />
        <span className="flex-1 min-w-0 truncate text-[13px] text-text">
          {activeJobDocName || i18nT('apps.writingReview.reviewList.inProgressDefault')}
        </span>
      </div>
      <div className="text-[11px] text-muted">
        {phaseLabel(activeJobPhase)}
      </div>
    </div>
  ) : null

  if (reviewSummaries.length === 0 && !inProgressCard) {
    return (
      <div className="flex-1 min-h-0 flex flex-col items-center justify-center px-6 text-center gap-2">
        <FileText className="lucide-inline text-muted opacity-50" aria-hidden="true" style={{ fontSize: '22px' }} />
        <div className="text-[12px] text-muted">
          {i18nT('apps.writingReview.reviewList.emptyTitle')}
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      {inProgressCard}
      {reviewSummaries.map(reviewSummary => (
        <ReviewCard
          key={reviewSummary.id}
          review={reviewSummary}
          isSelected={reviewSummary.id === selectedReviewId}
          onSelect={() => selectReview(reviewSummary.id)}
        />
      ))}
    </div>
  )
}

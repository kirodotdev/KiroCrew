// One row in the rail's review history. Shows document name, verdict
// pill, a compact "N findings" summary, and a hover-visible delete
// button that removes the review from disk. Delete uses a native
// browser confirm dialog so the "did my click register?" ambiguity of
// an inline two-step confirm state disappears.
import { Trash2 } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import type { ReviewSummary } from '../lib/types'
import { i18nT } from '../../../i18n/t'
import { writingReviewApi } from '../api'
import { useWritingReview } from '../context'
import VerdictBadge from './VerdictBadge'

export interface ReviewCardProps {
  review: ReviewSummary
  isSelected: boolean
  onSelect: () => void
}

export default function ReviewCard({ review, isSelected, onSelect }: ReviewCardProps) {
  const queryClient = useQueryClient()
  const { selectedReviewId, selectReview } = useWritingReview()
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const deleteMutation = useMutation({
    mutationFn: () => writingReviewApi.deleteReview(review.id),
    onSuccess: () => {
      // If the deleted review was selected, drop the selection so the
      // detail pane clears instead of showing a "review not found".
      if (selectedReviewId === review.id) {
        selectReview(null)
      }
      void queryClient.invalidateQueries({ queryKey: ['writing-review', 'reviews'] })
    },
    onError: (mutationError) => {
      setDeleteError(
        mutationError instanceof Error
          ? mutationError.message
          : i18nT('apps.writingReview.reviewCard.deleteFailedFallback'),
      )
      // Auto-clear the error after 5s so a stale message doesn't linger.
      window.setTimeout(() => setDeleteError(null), 5000)
    },
  })

  const handleDeleteClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    // Stop the click from bubbling up to the card's onSelect handler.
    event.stopPropagation()
    event.preventDefault()
    const confirmed = window.confirm(
      i18nT('apps.writingReview.reviewCard.deleteConfirmPrompt', {
        docName: review.doc_name,
      }),
    )
    if (!confirmed) return
    deleteMutation.mutate()
  }

  return (
    <div
      className={`group relative flex flex-col gap-1 w-full border-b border-border ${
        isSelected ? 'bg-accent-subtle' : ''
      }`}
    >
      <button
        type="button"
        onClick={onSelect}
        className="flex flex-col gap-1 w-full px-3 py-2 text-left hover:bg-bg-hover"
      >
        <div className="flex items-center gap-2 pr-8">
          <span className="flex-1 min-w-0 truncate text-[13px] text-text">
            {review.doc_name}
          </span>
          <VerdictBadge verdict={review.verdict} />
        </div>
        <div className="text-[11.5px] text-muted">
          {i18nT('apps.writingReview.reviewCard.findingCount', {
            count: review.finding_count,
          })}
        </div>
        {deleteError && (
          <div className="text-[11px] text-danger" role="alert">
            {i18nT('apps.writingReview.reviewCard.deleteError', {
              message: deleteError,
            })}
          </div>
        )}
      </button>
      <button
        type="button"
        onClick={handleDeleteClick}
        disabled={deleteMutation.isPending}
        aria-label={i18nT('apps.writingReview.reviewCard.delete')}
        title={i18nT('apps.writingReview.reviewCard.delete')}
        className="absolute top-2 right-2 p-1 rounded opacity-0 group-hover:opacity-100 text-muted hover:text-danger hover:bg-bg-hover transition-opacity disabled:opacity-40 disabled:cursor-default focus:opacity-100 focus:outline-none focus:ring-1 focus:ring-danger"
      >
        <Trash2 className="lucide-inline" aria-hidden="true" />
      </button>
    </div>
  )
}

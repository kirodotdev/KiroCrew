// The left-hand run (thread) list for Code Review Sage.
//
// Used in two slots with different scopes: the rail lists EVERY review, the
// middle column lists only the selected repo's. Several reviews can be live at
// once, so this is a real list — not a single "latest run" readout. It owns the loading skeleton, the error line, the
// centered empty state (with a start-a-review action), and the scrollable stack
// of RunCards with a "New review" affordance pinned above. Selection is lifted:
// the parent holds `selectedRunId` and gets `onSelect` / `onNewReview`.
import { ClipboardList, Plus } from 'lucide-react'
import type { Run } from '../lib/types'
import RunCard from './RunCard'
import ListSkeleton from './ListSkeleton'
import EmptyState from './EmptyState'

import { i18nT } from '../../../i18n/t'
export default function RunList({
  runs, loading, error, selectedRunId, onSelect, onNewReview, onDelete,
  deleting = false,
  emptyTitle = 'No reviews yet', emptyHint = 'Pick a pull request to review.',
}: {
  runs: Run[]
  loading: boolean
  error?: string | null
  selectedRunId: string | null
  onSelect: (runId: string) => void
  /** Omitted in the rail, where the repo-scoped column owns starting work. */
  onNewReview?: () => void
  /** Delete one review and its data. Omitted to render a read-only list. */
  onDelete?: (runId: string) => void
  deleting?: boolean
  emptyTitle?: string
  emptyHint?: string
}) {
  const showEmpty = !loading && !error && runs.length === 0

  return (
    <section className="flex flex-col min-h-0 h-full">
      {/* New-review affordance, pinned above the list. */}
      {onNewReview && (
      <div className="px-2 pt-2 pb-1.5 flex-shrink-0">
        <button
          type="button"
          onClick={onNewReview}
          className={
            'inline-flex w-full items-center justify-center gap-1.5 rounded-lg border border-border '
            + 'bg-card px-3 py-2 text-[13px] font-medium text-text transition-colors hover:bg-bg-hover '
            + 'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 cursor-pointer'
          }
        >
          <Plus size={14} aria-hidden="true" />
          {i18nT('apps.codeReviewSage.components.runList.new_review')}
        </button>
      </div>
      )}

      <div className="relative flex-1 min-h-0">
        <div
          className="absolute inset-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-2"
          style={{ scrollbarWidth: 'none' }}
        >
          {loading && <ListSkeleton />}
          {error && <div className="px-1 py-2 text-[13px] text-danger">{error}</div>}
          {showEmpty && (
            /* No action button here: the column's own "New review" sits a few
               pixels above this block, so a second CTA read as a duplicate. */
            <EmptyState icon={ClipboardList} title={emptyTitle} hint={emptyHint} />
          )}
          {!loading && !error && runs.map((run) => (
            <RunCard
              key={run.run_id}
              run={run}
              selected={run.run_id === selectedRunId}
              onSelect={() => onSelect(run.run_id)}
              onDelete={onDelete ? () => onDelete(run.run_id) : undefined}
              deleting={deleting}
            />
          ))}
        </div>
      </div>
    </section>
  )
}

// Skeleton placeholder cards for the issue / PR list columns.
//
// Shown while the first page of a list is loading, instead of a one-line
// "Loading issues…". The point is layout continuity: each placeholder occupies
// the same box as a real card (same border, radius, padding, and internal row
// rhythm), so when the data lands the column does not jump — and the shimmer
// makes the wait legible as "content is coming", not "nothing is here".
import ShimmerLine from './ShimmerLine'

/** Widths for the title line of each placeholder card, cycled so the stack looks
 * like text of varying length rather than a suspiciously uniform grid. */
const TITLE_WIDTHS = ['86%', '64%', '92%', '72%', '58%']

export default function ListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <>
      {/* Announced OUTSIDE the aria-hidden subtree: aria-hidden removes the whole
          tree from the accessibility tree, so a status element nested inside it
          would never reach a screen reader. */}
      <span className="sr-only" role="status">Loading…</span>
      <div aria-hidden="true" className="flex flex-col gap-2">
        {Array.from({ length: count }, (_, i) => (
          <div key={i} className="w-full rounded-lg border border-border bg-card p-2.5">
            {/* Meta row: state icon + number + author on the left, age on the right. */}
            <div className="flex items-center justify-between gap-2 mb-2">
              <div className="flex items-center gap-1.5">
                <ShimmerLine w="13px" delay={i * 0.06} />
                <ShimmerLine w="28px" delay={i * 0.06 + 0.04} />
                <ShimmerLine w="54px" delay={i * 0.06 + 0.08} />
              </div>
              <ShimmerLine w="42px" delay={i * 0.06 + 0.12} />
            </div>
            {/* Title line. */}
            <ShimmerLine w={TITLE_WIDTHS[i % TITLE_WIDTHS.length]} delay={i * 0.06 + 0.16} />
            {/* Bottom row: a couple of label chips. */}
            <div className="flex items-center gap-1.5 mt-2">
              <ShimmerLine w="48px" delay={i * 0.06 + 0.2} />
              <ShimmerLine w="36px" delay={i * 0.06 + 0.24} />
            </div>
          </div>
        ))}
      </div>
    </>
  )
}

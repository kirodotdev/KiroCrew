// Skeleton placeholder cards for the run (thread) list.
//
// Shown while the first page of runs is loading, instead of a one-line
// "Loading reviews…". Each placeholder occupies the same box as a real RunCard
// (same border, radius, padding, and internal two-row rhythm: an identity +
// status-pill row above an age + band-counts row), so the column does not jump
// when the data lands. Adapted from Issue Radar's ListSkeleton to the run shape.
import ShimmerLine from './ShimmerLine'

import { i18nT } from '../../../i18n/t'
/** Widths for the identity line of each placeholder card, cycled so the stack
 * looks like text of varying length rather than a suspiciously uniform grid. */
const IDENTITY_WIDTHS = ['58%', '44%', '66%', '38%', '52%']

export default function ListSkeleton({ count = 5 }: { count?: number }) {
  return (
    <>
      {/* Announced OUTSIDE the aria-hidden subtree: aria-hidden removes the whole
          tree from the accessibility tree, so a status element nested inside it
          would never reach a screen reader. */}
      <span className="sr-only" role="status">{i18nT('apps.codeReviewSage.components.listSkeleton.loading_reviews')}</span>
      <div aria-hidden="true" className="flex flex-col gap-2">
        {Array.from({ length: count }, (_, i) => (
          <div key={i} className="w-full rounded-lg border border-border bg-card p-2.5">
            {/* Top row: identity on the left, a status pill on the right. */}
            <div className="flex items-center justify-between gap-2 mb-2">
              <ShimmerLine w={IDENTITY_WIDTHS[i % IDENTITY_WIDTHS.length]} delay={i * 0.06} />
              <ShimmerLine w="52px" delay={i * 0.06 + 0.08} />
            </div>
            {/* Bottom row: relative age on the left, a couple of band counts. */}
            <div className="flex items-center justify-between gap-2">
              <ShimmerLine w="46px" delay={i * 0.06 + 0.12} />
              <div className="flex items-center gap-1.5">
                <ShimmerLine w="22px" delay={i * 0.06 + 0.16} />
                <ShimmerLine w="22px" delay={i * 0.06 + 0.2} />
              </div>
            </div>
          </div>
        ))}
      </div>
    </>
  )
}

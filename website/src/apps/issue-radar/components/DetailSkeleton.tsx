// First-load placeholders for the two detail panes.
//
// A pane opened from the LIST paints instantly from its row (title, author,
// labels), so it never needs these. A pane opened from a CROSS-REFERENCE has no
// row — only a number — so every field would otherwise render as a lie for the
// length of one fetch: an empty title, "someone opened", "ghost", "No description
// provided". These render the shape of the missing content instead.
//
// Built on the shared ShimmerLine primitive, so the motion matches the AI summary
// card and the list columns.
import ShimmerLine from './ShimmerLine'

/** Header title + meta row. Mirrors the real header's rhythm: one big line, then
 * a short pill-and-meta row. */
export function HeaderSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      <div className="h-[27px] flex items-center"><ShimmerLine w="62%" /></div>
      <div className="flex items-center gap-2">
        <ShimmerLine w="74px" />
        <ShimmerLine w="52px" delay={0.1} />
        <ShimmerLine w="140px" delay={0.2} />
      </div>
    </div>
  )
}

/** The opening-post card: its author row, then a few body lines. Same border and
 * padding as the real CommentCard so nothing shifts when the body arrives. */
export function CommentCardSkeleton() {
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-2 px-3.5 py-2 border-b border-border bg-bg-elevated/60">
        <ShimmerLine w="88px" />
        <ShimmerLine w="132px" delay={0.1} />
      </div>
      <div className="px-3.5 py-3 flex flex-col gap-2">
        <ShimmerLine w="100%" />
        <ShimmerLine w="94%" delay={0.08} />
        <ShimmerLine w="76%" delay={0.16} />
      </div>
    </div>
  )
}

/** A few activity rows on the timeline rail: the date/dot gutter plus a line of
 * text, repeated. */
export function TimelineSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-3.5 py-1">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="grid grid-cols-[64px_26px_1fr] items-center gap-x-1">
          <ShimmerLine w="46px" delay={i * 0.12} />
          <ShimmerLine w="14px" delay={i * 0.12 + 0.04} />
          <ShimmerLine w={`${72 - i * 12}%`} delay={i * 0.12 + 0.08} />
        </div>
      ))}
    </div>
  )
}

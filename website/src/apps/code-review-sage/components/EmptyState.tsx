// Centered empty state for Code Review Sage columns.
//
// A slightly more general version of Issue Radar's ListEmptyState: instead of
// hard-coding the "search vs filter" copy it takes an icon, a title, an optional
// hint, and optional children (an action button). Centering the block in the
// column and pairing it with an icon makes the emptiness look deliberate rather
// than like a glitch pinned to the top-left.
import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

export default function EmptyState({
  icon: Icon, title, hint, children,
}: {
  icon: LucideIcon
  title: string
  /** Optional secondary line under the title. */
  hint?: string
  /** Optional action (e.g. a "start a review" button) rendered below the hint. */
  children?: ReactNode
}) {
  return (
    // Fills the column so the block lands in the optical centre rather than
    // hugging the top.
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2.5 text-center px-6">
      <Icon size={26} className="text-muted opacity-50" strokeWidth={1.5} aria-hidden="true" />
      <div className="text-[13px] text-text">{title}</div>
      {hint && <div className="text-[11.5px] text-muted opacity-70">{hint}</div>}
      {children && <div className="mt-1">{children}</div>}
    </div>
  )
}

import { ArrowLeft } from 'lucide-react'

/**
 * Return-to-list control for the narrow-viewport list-detail drill-down
 * (see `hooks/useListDetailView`). Rendered only while the detail pane is the
 * one visible pane, so it is the sole way back — component selection state is
 * not history, and a browser back-swipe would leave the page entirely.
 *
 * `label` names the list being returned to ("Skills", "Steering files") rather
 * than a bare "Back", which also means callers reuse the label they already
 * have for the list instead of every shell adding its own catalog key.
 *
 * 44px tall, not the 36px the rest of these headers use: this is the only way
 * out of the pane on a touch device, and 44 is what Apple's HIG builds to
 * (44pt default, 28pt floor) and what Fluent and Primer recommend for mobile.
 * WCAG 2.2 SC 2.5.8's 24px is a floor to clear, not a target to design to.
 */
export default function ListDetailBack({ label, onBack }: { label: string; onBack: () => void }) {
  return (
    <button
      type="button"
      onClick={onBack}
      className="flex shrink-0 items-center gap-1 min-h-11 -ml-1 px-2 rounded-md bg-transparent border-none text-[13px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer transition-colors focus-ring"
    >
      <ArrowLeft size={14} aria-hidden="true" className="lucide-inline" />
      {label}
    </button>
  )
}

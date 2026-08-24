import { ChevronLeft } from 'lucide-react'
import { COARSE_TOUCH_TARGET } from './subNavParams'

/** The iOS-style navigation back bar, shared by EVERY level of the mobile
 *  push stack — SidePanelLayout's tab detail ("‹ Settings") and
 *  SettingsSubNav's drilled-in pane ("‹ Channels") render this one component,
 *  so the bar keeps identical geometry, tint and blur across levels and the
 *  back control does not visibly jump as the user pushes and pops.
 *
 *  Sticky to the nearest scroll container, on a blurred translucent wash of
 *  the page bg (color-mix because the theme vars carry no <alpha-value> —
 *  Tailwind's /opacity modifiers silently no-op) with a hairline bottom edge.
 *  The chevron lands 10px from the content edge (16px inset − 6px optical
 *  pull) at every level; a host whose pane already pads horizontally bleeds
 *  the bar back out via `className` (e.g. `-mx-4`) so the wash stays
 *  full-width. */
export function NavBackBar({ label, onBack, className = '' }: {
  /** The PARENT level's title — iOS labels back with where you came from. */
  label: string
  onBack: () => void
  className?: string
}) {
  return (
    <div className={`sticky top-0 z-10 shrink-0 px-4 backdrop-blur-md bg-[color-mix(in_srgb,var(--bg)_90%,transparent)] shadow-[0_1px_0_var(--border)] ${className}`}>
      <button
        type="button"
        onClick={onBack}
        className={`flex items-center gap-0.5 -ml-1.5 py-2 pr-3 ${COARSE_TOUCH_TARGET} text-[14px] font-medium text-accent bg-transparent border-none cursor-pointer`}
      >
        <ChevronLeft size={19} strokeWidth={2.25} className="shrink-0" />
        {label}
      </button>
    </div>
  )
}

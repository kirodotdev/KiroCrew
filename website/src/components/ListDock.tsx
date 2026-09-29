import { type CSSProperties, type ReactNode } from 'react'

import { useMeasuredHeight } from '../hooks/useMeasuredHeight'

/**
 * The list panel's floating dock: the glass search field, and under it the
 * SHELF (the filter chips, the list-level notices), hover over the top of the
 * list and the rows scroll UNDER them, the way the transcript scrolls under
 * the composer dock. Shared by the Sessions sidebar and the Crew Members
 * roster so both lists carry the same geometry.
 *
 * Two layers, two materials. The FIELD is a Liquid Glass pane: rows show
 * through it blurred, which is the material's point. The shelf's chips and
 * notices are translucent tints tuned for the solid panel they used to sit on
 * (a chip fills at 10% of its colour, a notice at 10% danger), so rows sliding
 * under them rendered text through text. The shelf therefore stands on a
 * SCRIM (`.list-dock-shelf`, index.css): the panel's own surface, faded in
 * over the first pixels below the pane the way the composer's context shelf
 * fades onto the page colour (`.glass-shelf`), so the chips and notices read
 * on their own ground and the rows fade out under them instead of showing
 * through. With nothing on the shelf there is no shelf: the dock ends at the pane.
 *
 * The dock is measured, not assumed: its height changes when a chip row
 * appears or an error notice mounts, so `useMeasuredHeight` (the composer's
 * own strip measurement) writes the live height PLUS the 4px rest gap to
 * `--list-dock-h` on the wrapper, and every scroller inside pads its top — and
 * its scroll-padding — by that much (`LIST_BODY_CLS`, components/listShell.ts).
 * The first row is then fully visible 4px below the dock at rest, a row
 * scrolled into view lands below the dock, and rows only slide under it once
 * the user scrolls. The dock's own bottom edge is the glass pane's (or, with
 * something on the shelf, the shelf's): no padding strip of its own, so a row
 * emerging from under it emerges at the glass, not from a 4px band of a third
 * tone — the first cut had one, and it read as a leftover box behind the pane.
 *
 * Pointer events follow what the eye sees. The dock wrapper is
 * `pointer-events-none`, so the transparent margin around the field (its
 * `px-2 pt-2`) passes a click to whatever is under it; the glass field itself
 * and every focusable in the band (button, link, input, select, textarea, a
 * tabindex'd or role="button" element) re-arm. The SHELF re-arms as a whole:
 * it is opaque, and a click on what reads as blank panel beside a chip must
 * land inert rather than open a session the scrim hides — the first cut let
 * it through, and the blind read named that as an invisible target. The CI
 * spec `playwright/sidebar-glass-dock.spec.ts` hit-tests both: every control
 * to itself, the band beside a chip to the dock.
 *
 * `z-30` puts the dock above a pinned folder header (`FOLDER_ROW_STICKY_Z`,
 * 20): a header pushed off its pin by the next folder travels up through the
 * band, and at `z-10` its opaque surface painted over the glass.
 */
/** Space between the dock's bottom edge and the first row at rest. */
const REST_GAP_PX = 4

export function ListDock({ field, shelf, children }: {
  /** The glass search row (`SearchFilterBar`). */
  field: ReactNode
  /** What hangs under it: filter chips, list-level notices. Optional. */
  shelf?: ReactNode
  children: ReactNode
}) {
  const [dockRef, height] = useMeasuredHeight<HTMLDivElement>()
  return (
    <div
      className="relative flex-1 min-h-0 flex flex-col"
      // `--list-dock-h` is the dock plus the 4px rest gap under it; the pin
      // inset gives that gap back, so a pinned folder header (`.folder-row-sticky`,
      // index.css) sits exactly on the dock's BOTTOM edge — the pane, or a chip
      // row or notice on the shelf — never tucked under the dock's `z-30`
      // surface, never leaving a strip the rows show through.
      style={{ '--list-dock-h': `${height + REST_GAP_PX}px`, '--list-dock-pin-inset': `${REST_GAP_PX}px` } as CSSProperties}
    >
      <div ref={dockRef} className="absolute top-0 inset-x-0 z-30 pointer-events-none [&_.liquid-glass]:pointer-events-auto [&_button]:pointer-events-auto [&_a]:pointer-events-auto [&_input]:pointer-events-auto [&_select]:pointer-events-auto [&_textarea]:pointer-events-auto [&_[tabindex]]:pointer-events-auto [&_[role=button]]:pointer-events-auto [&_[role=alert]]:pointer-events-auto [&_[role=status]]:pointer-events-auto" data-testid="list-dock">
        {field}
        {/* The scrim fades in from the pane's edge; `pt-1` keeps the chips off
            it. Hidden when empty, so a bare field's dock ends at the glass. */}
        <div className="list-dock-shelf pt-1 pointer-events-auto empty:hidden" data-testid="list-dock-shelf">
          {shelf}
        </div>
      </div>
      {children}
    </div>
  )
}

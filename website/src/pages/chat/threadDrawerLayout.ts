/**
 * Where the open thread sits relative to the chat it hangs off.
 *
 * At a desktop width the drawer is a PANE of the chat surface row, not a sheet
 * over it: the transcript reflows into the width left, as it already does for the
 * side panel and the docked search `DetailPanel`, both `shrink-0` children of
 * this row. Laid over instead, the parent's own words and half its composer sit
 * behind the drawer with no gesture that reveals them. Below
 * `THREAD_SPLIT_MIN_W` a split leaves neither column readable (520px of drawer in
 * a 900px row hands the transcript under 400px, less the sidebar), so it goes
 * back to an overlay on the row's right edge.
 *
 * Two things are easy to undo by accident. The breakpoint is a CSS variant spelled
 * out literally, because Tailwind reads this file as TEXT and never generates a
 * class built from an interpolated constant. And the slide stays a transform in
 * both modes: a width reveal is a layout animation, re-laying-out the squeezed
 * transcript every frame, the cost this page already refuses for the mobile side
 * panel.
 */

/** Row width (px) at or above which the drawer splits instead of overlaying. */
export const THREAD_SPLIT_MIN_W = 1100

/** The drawer's width (px) once it is a pane of its own. */
export const THREAD_DRAWER_W = 520

/**
 * Overlay is the base; the `min-[1100px]:` half is the pane, and it names every
 * base rule it must undo so the two cannot half-apply. `relative` rather than
 * `static`: `ThreadPanel` needs this box to stay its positioned ancestor.
 */
export const threadDrawerWrapperClass =
  'absolute top-0 bottom-0 right-0 z-[46] w-full max-w-[520px] border-l border-border shadow-xl overflow-hidden' +
  ' min-[1100px]:relative min-[1100px]:inset-auto min-[1100px]:z-auto' +
  ' min-[1100px]:h-full min-[1100px]:w-[520px] min-[1100px]:max-w-none min-[1100px]:shrink-0'

/**
 * Header drag-gap geometry — the horizontal spans of the 42px title-bar band
 * that carry NO interactive control, so an Electron host can mark exactly those
 * as `-webkit-app-region: drag`.
 *
 * Why this exists: a remote-instance pane is a cross-origin <iframe>. Electron's
 * injected drag bar and its `no-drag` blanket only apply to the host's MAIN
 * frame, and the whole iframe is in that blanket — so the host subtracts the
 * entire top band and the pane's header cannot move the window. Draggable
 * regions do not descend into a subframe, so the pane cannot mark itself
 * draggable either. The fix relays this geometry to the host, which re-adds
 * drag ONLY in the gaps between the pane's own controls.
 *
 * The result is failure-safe by construction: each control's box is grown
 * outward by CONTROL_PAD_PX before it is excluded, so a stale or rounded
 * measurement can only shrink a gap (a small non-draggable sliver), never let a
 * drag strip straddle a control and swallow its clicks.
 */
export interface DragGap {
  /** Left edge in viewport pixels. */
  x: number
  /** Width in pixels. */
  w: number
}

/**
 * Interactive controls that must stay clickable. Mirrors the `no-drag` blanket
 * injected by electron/main.js so the pane's draggable area matches the local
 * header's exactly. `iframe` is omitted — the header carries none.
 */
const INTERACTIVE_SEL = 'a,button,input,select,textarea,[role="button"],[tabindex]'
/** Outward padding on each control box; the safety margin that keeps gaps off controls. */
const CONTROL_PAD_PX = 6
/** Gaps narrower than this are dropped: not worth a region, and rounding-risky. */
const MIN_GAP_PX = 10

/**
 * Compute the drag gaps of `header` within the `[0, bandWidth]` viewport band.
 * Returns an empty list when the header is not laid out (hidden pane) or the
 * band has no width.
 */
export function computeHeaderDragGaps(header: Element, bandWidth: number): DragGap[] {
  const width = Math.max(0, Math.floor(bandWidth))
  if (width === 0) return []
  const controls = Array.from(header.querySelectorAll(INTERACTIVE_SEL)) as HTMLElement[]
  const blocked: Array<[number, number]> = []
  for (const el of controls) {
    const r = el.getBoundingClientRect()
    // Skip controls with no box (display:none, or a not-yet-laid-out pane).
    if (r.width <= 0 || r.height <= 0) continue
    const left = Math.max(0, Math.floor(r.left - CONTROL_PAD_PX))
    const right = Math.min(width, Math.ceil(r.right + CONTROL_PAD_PX))
    if (right > left) blocked.push([left, right])
  }
  // Merge overlapping / touching blocked intervals so the complement is clean.
  blocked.sort((a, b) => a[0] - b[0])
  const merged: Array<[number, number]> = []
  for (const [l, r] of blocked) {
    const last = merged[merged.length - 1]
    if (last && l <= last[1]) last[1] = Math.max(last[1], r)
    else merged.push([l, r])
  }
  // The complement within [0, width] is the set of drag gaps.
  const gaps: DragGap[] = []
  let cursor = 0
  for (const [l, r] of merged) {
    if (l - cursor >= MIN_GAP_PX) gaps.push({ x: cursor, w: l - cursor })
    cursor = Math.max(cursor, r)
  }
  if (width - cursor >= MIN_GAP_PX) gaps.push({ x: cursor, w: width - cursor })
  return gaps
}

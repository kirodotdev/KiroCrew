/**
 * Seat computation for the sidebar's hover hold: where a held row must sit so it
 * stays under the pointer while the list re-sorts around it.
 *
 * Split out of the sidebar component so the geometry is unit-testable without a DOM —
 * the component keeps the pointer-time capture and the render wiring, this file is
 * pure arithmetic over a captured frame.
 */

/**
 * A frame of one lane container as the pointer found it, captured from the
 * committed DOM at `pointerover`.
 *
 * `container` is the identity of the ONE rendered container the row lives in, and
 * is deliberately narrower than `scope`: a folder tree renders every folder's
 * children and the root rows as separate containers that all share the
 * `data-session-scope` nav lane, so a scope-wide frame would count rows the
 * container's own layout never places. `seenOrder` and `heights` therefore cover
 * that container alone.
 *
 * `seenOrder` is keys rather than an index so later re-sorts cannot shift the held
 * slot out from under a captured number, and `heights` is per-key because rows are
 * unequal height (an expanded source-chip row is taller).
 */
export type HoverPin = {
  key: string
  scope: string
  container: string
  seenOrder: string[]
  heights: Record<string, number>
  headerPxAbove: number
  headerH: number
  staleSide: boolean
}

/**
 * The index in `list` (with the held row removed) where the held row must be
 * re-inserted to keep its captured pixel offset, or null when the pin says
 * nothing about this list.
 *
 * Anchors on pixel offset rather than index so a taller row sorting in above
 * cannot push the held row down a slot. The offset is summed over the CAPTURED
 * rows, making it a constant for the pin's lifetime: a row closing under the hold
 * leaves `list`, and its height must not leave the anchor with it.
 *
 * `segmentOf` mirrors the lane's own date-header rule so the walk counts the
 * headers the render will actually emit above each candidate slot.
 */
export function heldSeat<T extends { key: string }>(
  pin: HoverPin,
  list: readonly T[],
  // The identity space of `pin.key`, `seenOrder`, and `heights` — all captured
  // from `data-session-row`, which is origin-qualified for peer rows. REQUIRED,
  // with no default: a default of the raw key silently gives a caller whose rows
  // CAN collide across gateways the wrong identity space, and the only production
  // caller already passes `sessionRowIdentity`. Making it explicit means a new
  // lane has to state which space it is in rather than inheriting the one that
  // happens to be wrong for peer rows. It precedes `segmentOf` because a required
  // parameter cannot follow an optional one.
  idOf: (s: T) => string,
  segmentOf?: (s: T) => string,
): number | null {
  const rank = new Map(pin.seenOrder.map((k, i) => [k, i]))
  const mine = rank.get(pin.key)
  // A row absent from the seen frame has no held slot, so it keeps its live one.
  if (mine == null) return null
  const rest = list.filter(s => idOf(s) !== pin.key)
  const ownH = pin.heights[pin.key] ?? 0
  const heightOf = (k: string) => pin.heights[k] ?? ownH
  if (ownH <= 0) {
    // No layout to measure: degenerate to counting rows, which is what a pixel
    // anchor reduces to when every row is the same height.
    return list.reduce((n, s) => {
      const r = rank.get(idOf(s))
      return n + (r != null && r < mine ? 1 : 0)
    }, 0)
  }
  let anchorPx = pin.headerPxAbove
  for (let i = 0; i < mine; i++) anchorPx += heightOf(pin.seenOrder[i])
  let acc = 0
  let prevSeg = ''
  let bestErr = Infinity
  let held = 0
  for (let i = 0; i <= rest.length; i++) {
    if (Math.abs(acc - anchorPx) < bestErr) { bestErr = Math.abs(acc - anchorPx); held = i }
    if (i < rest.length) {
      // The held row's own header is suppressed, so only later slots accrue one.
      const seg = segmentOf?.(rest[i]) ?? ''
      if (seg && seg !== prevSeg) { acc += pin.headerH; prevSeg = seg }
      acc += heightOf(idOf(rest[i]))
    }
  }
  return held
}

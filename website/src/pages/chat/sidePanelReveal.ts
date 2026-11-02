/** Width of the tab strip's edge fades (`w-6`). A chip revealed flush to an
 *  edge would sit under that fade, close button included. */
export const EDGE_FADE_PX = 24

/**
 * Scrolls a chip fully into its scroller by the minimum amount, and not at all
 * when it is already inside. `inset` keeps that much room on each side, so the
 * chip clears an edge fade; a chip too wide for both insets splits the room it
 * has. At either end of the scroll range the clamp lets the chip reach the
 * edge, which is where the fade is not drawn. A chip wider than the scroller
 * aligns its trailing edge if closable (keeping the close control), else its
 * leading edge.
 *
 * Writes `scrollLeft` directly: smooth scrolling would fight the layout
 * spring, and `scrollIntoView` could scroll ancestors.
 */
export function revealChip(scroller: HTMLElement, chip: HTMLElement, closable: boolean, inset = 0): void {
  const max = scroller.scrollWidth - scroller.clientWidth
  if (max <= 0) return
  const v = scroller.clientWidth
  const c = chip.getBoundingClientRect()
  const s = scroller.getBoundingClientRect()
  const current = scroller.scrollLeft
  let target = current
  if (c.width <= v) {
    const pad = Math.max(0, Math.min(inset, (v - c.width) / 2))
    if (c.left < s.left + pad) target += c.left - (s.left + pad)
    else if (c.right > s.right - pad) target += c.right - (s.right - pad)
  } else if (closable) {
    target += c.right - s.right
  } else {
    target += c.left - s.left
  }
  const clamped = Math.min(Math.max(target, 0), max)
  if (clamped === current) return
  scroller.scrollLeft = clamped
}

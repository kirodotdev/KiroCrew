/**
 * Scrolls a chip fully into its scroller by the minimum amount, and not at all
 * when it is already inside. Edge fades reserve no space. A chip wider than the
 * scroller aligns its trailing edge if closable (keeping the close control),
 * else its leading edge.
 *
 * Writes `scrollLeft` directly: smooth scrolling would fight the layout
 * spring, and `scrollIntoView` could scroll ancestors.
 */
export function revealChip(scroller: HTMLElement, chip: HTMLElement, closable: boolean): void {
  const max = scroller.scrollWidth - scroller.clientWidth
  if (max <= 0) return
  const v = scroller.clientWidth
  const c = chip.getBoundingClientRect()
  const s = scroller.getBoundingClientRect()
  const current = scroller.scrollLeft
  let target = current
  if (c.width <= v) {
    if (c.left < s.left) target += c.left - s.left
    else if (c.right > s.right) target += c.right - s.right
  } else if (closable) {
    target += c.right - s.right
  } else {
    target += c.left - s.left
  }
  const clamped = Math.min(Math.max(target, 0), max)
  if (clamped === current) return
  scroller.scrollLeft = clamped
}

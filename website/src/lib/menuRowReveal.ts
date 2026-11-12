/**
 * Keeps one row of an open Radix menu in view. Only the menu's own `scrollTop` moves:
 * `scrollIntoView` could move the page or another scroller the menu sits inside.
 */

/** Sets only `content.scrollTop`, by the minimum needed to show `row`. */
function revealRowInContent(content: HTMLElement, row: HTMLElement): void {
  const contentRect = content.getBoundingClientRect()
  const rowRect = row.getBoundingClientRect()
  // Rects are scaled while the menu's open animation runs, scrollTop is not, so convert.
  const scale = content.offsetHeight > 0 && contentRect.height > 0 ? contentRect.height / content.offsetHeight : 1
  const visibleTop = contentRect.top + content.clientTop * scale
  const visibleBottom = content.clientHeight > 0
    ? visibleTop + content.clientHeight * scale
    : contentRect.bottom
  if (rowRect.top < visibleTop) {
    content.scrollTop = content.scrollTop - (visibleTop - rowRect.top) / scale
  } else if (rowRect.bottom > visibleBottom) {
    // A row taller than the viewport aligns its top, so its title stays visible.
    const down = Math.min(rowRect.bottom - visibleBottom, rowRect.top - visibleTop) / scale
    content.scrollTop = content.scrollTop + down
  }
}

/**
 * Keeps `row()` in view inside `content`, the open menu, and returns the cleanup.
 *
 * Radix applies the menu's max-height over several layout passes after it opens, so the
 * content only becomes scrollable once its height settles. Keep the row in view across
 * those passes, and stop at the first wheel, touch, key or press so a later resize never
 * undoes the person's own scrolling.
 */
export function keepRowInMenuView(content: HTMLElement, row: () => HTMLElement | null | undefined): () => void {
  const reveal = () => {
    const el = row()
    if (el) revealRowInContent(content, el)
  }
  reveal()
  const observer = new ResizeObserver(reveal)
  observer.observe(content)
  const stop = () => observer.disconnect()
  const takeovers = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
  for (const type of takeovers) content.addEventListener(type, stop, { passive: true })
  return () => {
    observer.disconnect()
    for (const type of takeovers) content.removeEventListener(type, stop)
  }
}

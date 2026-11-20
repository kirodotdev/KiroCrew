/**
 * The decor slot's `clip-path` text (lib/themeDecorKeepClear.ts). Data in, CSS
 * out: no catalog import, no JSX, no DOM access, so every literal here is CSS
 * handed to the style parser and never read as words.
 */

export interface DecorHole { top: number; left: number; bottom: number; right: number }

/**
 * The whole viewport minus each hole, as one even-odd polygon. Each hole is
 * traced from the outline's start corner and back, so the connecting edges
 * have no area. Undefined when there is nothing to cut.
 */
export function decorClipPath(holes: readonly DecorHole[], width: number, height: number): string | undefined {
  // Round to the nearest pixel, not outward: two boxes that touch at a
  // fractional edge then share one integer edge. Rounded outward, they overlap
  // by a pixel, and `evenodd` paints that shared strip back in.
  const cut = holes
    .map(h => ({
      top: Math.max(0, Math.round(h.top)),
      left: Math.max(0, Math.round(h.left)),
      bottom: Math.min(height, Math.round(h.bottom)),
      right: Math.min(width, Math.round(h.right)),
    }))
    .filter(h => h.right > h.left && h.bottom > h.top)
  if (cut.length === 0) return undefined
  const pts = ['0px 0px', `${width}px 0px`, `${width}px ${height}px`, `0px ${height}px`, '0px 0px']
  for (const h of cut) {
    pts.push(
      `${h.left}px ${h.top}px`, `${h.right}px ${h.top}px`, `${h.right}px ${h.bottom}px`,
      `${h.left}px ${h.bottom}px`, `${h.left}px ${h.top}px`, '0px 0px',
    )
  }
  return `polygon(evenodd, ${pts.join(', ')})`
}

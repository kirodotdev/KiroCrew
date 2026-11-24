/**
 * The ring that a panel draws at a press of its live page (`PageSource.pressMark`): where it stands on the panel, and
 * its shape. A ring and not a dot: its middle is open, so the word of the control that was pressed stays readable.
 */

/** The ring's inner and outer edge, in widths of the panel's rim, and how far it stands in front of the page (m). */
export const PRESS_RING = { inner: 3.6, outer: 4.4, liftM: 0.002 } as const

/** What of a mesh the ring's placing needs. */
export interface RingMesh { visible: boolean; position: { set: (x: number, y: number, z: number) => unknown } }

/**
 * Shows the ring at `mark` (CSS pixels of the page, from its top left) or hides it. `page` is the page's rectangle
 * on the panel (its middle and size, in metres); `css` gives the page's size in CSS pixels, asked only when a mark stands.
 */
export function placePressRing(ring: RingMesh | null, mark: { x: number; y: number } | null, css: () => { cssWidth: number; cssHeight: number }, page: { cx: number; cy: number; w: number; h: number }): void {
  if (!ring) return
  ring.visible = mark !== null
  if (!mark) return
  const f = css()
  ring.position.set(page.cx + (mark.x / f.cssWidth - 0.5) * page.w, page.cy + (0.5 - mark.y / f.cssHeight) * page.h, PRESS_RING.liftM)
}

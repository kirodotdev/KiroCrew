// Shared geometry + bottom-up ordering for the anchored input pickers
// ($skill → SkillPickerMenu, @file → FilePickerMenu, /command → SlashCommandMenu).
//
// Centralizing the two drift-prone pieces here keeps the three menus in
// lockstep — otherwise they diverge on above/below math, bottom-up population,
// and scrolling the selection into view:
//   1. menuGeometry — where the menu opens relative to the anchored input.
//   2. bottomUpOrder — display order + initial selection when it opens above.
// Keyboard nav + scroll-into-view live in the shared useListKeyboardNav hook.

/** Max menu height (px); matches the maxHeight the portals render with. */
export const MENU_MAX_HEIGHT = 320

export interface MenuGeometry {
  /** True when the menu opens ABOVE the anchor (the common case — chat input
   *  at the viewport bottom). Drives the bottom-up reversal. */
  above: boolean
  /** CSS `top` for the fixed-position portal. */
  top: number
  /** CSS `left` for the portal (anchor's left edge). */
  left: number
  /** Anchor width — callers clamp their own max (e.g. Math.min(width, 420)). */
  width: number
  maxHeight: number
}

/**
 * Compute where an anchored picker opens and its portal position.
 * `count` is the number of rows; `rowH` the per-row height estimate (px).
 * The menu opens above the input when there's room, else below.
 */
export function menuGeometry(anchor: HTMLElement, count: number, rowH: number): MenuGeometry {
  const rect = anchor.getBoundingClientRect()
  const menuH = Math.min((count || 1) * rowH + 8, MENU_MAX_HEIGHT)
  const aboveTop = rect.top - menuH - 4
  const above = aboveTop > 0
  return {
    above,
    top: above ? aboveTop : rect.bottom + 4,
    left: rect.left,
    width: rect.width,
    maxHeight: MENU_MAX_HEIGHT,
  }
}

/**
 * Populate bottom-up when the menu opens above: reverse the (already ranked)
 * items so the top-ranked/selected row sits at the BOTTOM nearest the cursor,
 * and select that bottom row. Opens-below keeps the top-ranked row at the top
 * with the selection there. `above` comes from menuGeometry().
 */
export function bottomUpOrder<T>(items: T[], above: boolean): { ordered: T[]; initialIndex: number } {
  const ordered = above ? [...items].reverse() : items
  return { ordered, initialIndex: above ? Math.max(ordered.length - 1, 0) : 0 }
}

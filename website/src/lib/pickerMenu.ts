// Shared geometry + bottom-up ordering for the anchored input pickers
// ($skill → SkillPickerMenu, @file → FilePickerMenu, /command → SlashCommandMenu).
//
// Centralizing the two drift-prone pieces here keeps the three menus in
// lockstep — otherwise they diverge on above/below math, bottom-up population,
// and scrolling the selection into view:
//   1. menuGeometry — where the menu opens relative to the anchored input.
//   2. bottomUpOrder — display order + initial selection when it opens above.
// Keyboard nav + scroll-into-view live in the shared useListKeyboardNav hook.
//
// The returned maxHeight is CLAMPED to the space actually available on the
// chosen side (never above MENU_MAX_HEIGHT, and floored at one row + padding
// + extraH). The side choice itself still comes from the count*rowH estimate,
// which under-reads real rows (a two-line @file row measures ~53px against
// the 48px estimate) — the clamp makes that inaccuracy irrelevant to
// clipping: a menu whose real height exceeds the clamp scrolls inside the
// viewport instead of extending past its top (opens-above pins the bottom
// edge and grows upward) or its bottom. The floor takes precedence when the
// chosen side offers less than one row, so the no-clip guarantee holds only
// while that side has at least minSensibleHeight; when it does not and the
// OTHER side offers more, the side flips there (still pure rect math — no
// post-mount measurement).

/** Max menu height (px); the ceiling of the clamped maxHeight the portals render with. */
export const MENU_MAX_HEIGHT = 320

/** Gap (px) between the anchor and the menu edge on either side. */
const MARGIN = 4

export interface MenuGeometry {
  /** True when the menu opens ABOVE the anchor (the common case — chat input
   *  at the viewport bottom). Drives the bottom-up reversal. */
  above: boolean
  /** CSS `top` for the fixed-position portal, for the opens-BELOW case. */
  top: number
  /** CSS `bottom` for the opens-ABOVE case: pins the menu's bottom edge 4px
   *  above the anchor so it grows UPWARD. Placing by `top` there would trust
   *  `count * rowH` as the real height, and a zero-row menu whose copy wraps
   *  past one line is taller than that — the surplus lands on the composer. */
  bottom: number
  /** CSS `left` for the portal (anchor's left edge). */
  left: number
  /** Anchor width — callers clamp their own max (e.g. Math.min(width, 420)). */
  width: number
  /** Height cap for the portal's scrolling container: MENU_MAX_HEIGHT clamped
   *  to the space actually available on the chosen side, floored at one row
   *  plus padding so a degenerate anchor never yields a zero-height menu. */
  maxHeight: number
}

/**
 * Compute where an anchored picker opens and its portal position.
 * `count` is the number of rows; `rowH` the per-row height estimate (px).
 * `extraH` budgets non-row chrome (e.g. a pinned footer) into the height.
 *
 * The estimate chooses the SIDE only. An opens-above menu is then placed by
 * `bottom`, so its real rendered height — which the estimate cannot know for
 * a wrapping zero-row announcement — can never overhang the composer. The
 * returned maxHeight is clamped to the space the chosen side actually has
 * (above: rect.top − margin, itself capped at the viewport for an anchor
 * scrolled past the bottom edge; below: viewport − rect.bottom − margin), so
 * a menu whose real height beats the estimate scrolls instead of clipping.
 * When neither the estimate nor even one row fits the estimate's side and
 * the other side offers more room, the side flips there — otherwise the
 * floor (one row + padding + extraH) wins and the box may overhang a
 * viewport that simply has no room anywhere.
 */
export function menuGeometry(
  anchor: HTMLElement, count: number, rowH: number, extraH = 0,
): MenuGeometry {
  const rect = anchor.getBoundingClientRect()
  const menuH = Math.min((count || 1) * rowH + 8 + extraH, MENU_MAX_HEIGHT)
  // Space each side actually offers. spaceAbove is additionally capped at the
  // viewport: an anchor scrolled past the bottom edge pins the menu's bottom
  // at y=viewport (the returned `bottom` floors at 0), so the box can only
  // ever use the viewport itself, not the anchor's off-screen rect.top.
  const spaceAbove = Math.min(rect.top - MARGIN, window.innerHeight)
  const spaceBelow = window.innerHeight - rect.bottom - MARGIN
  // One row plus padding plus the pinned chrome: a degenerate anchor must
  // never produce a zero-height menu, and the floor must still deliver one
  // usable row after extraH (e.g. the skill picker's scope footer) is paid.
  const minSensibleHeight = rowH + 8 + extraH
  // The estimate picks the side; when its side cannot even show one row and
  // the other side is roomier, flip (pure rect math, no post-mount measuring).
  let above = rect.top - menuH - MARGIN > 0
  const chosen = above ? spaceAbove : spaceBelow
  if (chosen < minSensibleHeight && (above ? spaceBelow : spaceAbove) > chosen) {
    above = !above
  }
  const available = above ? spaceAbove : spaceBelow
  return {
    above,
    top: above ? rect.top - menuH - MARGIN : rect.bottom + MARGIN,
    bottom: Math.max(window.innerHeight - rect.top + MARGIN, 0),
    left: rect.left,
    width: rect.width,
    maxHeight: Math.max(minSensibleHeight, Math.min(MENU_MAX_HEIGHT, available)),
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

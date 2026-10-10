/**
 * The collapsed rail header's logo must clear the rail's rounded corner.
 *
 * The desktop rail is a rounded card (`rounded-xl`, `overflow-hidden`) whose
 * header deliberately carries NO `overflow-hidden`, so the logo's `group-hover`
 * rotate can paint a few px past its own box — a sanctioned hover affordance the
 * hover-transform guard keeps on purpose. That spill is meant to land in the
 * rail's padding, far from the mark.
 *
 * Collapsed, the logo is the icon strip's whole brand mark and sits in the top
 * corner, where the rail's `rounded-xl` curve bites into the content box. At the
 * full-bleed collapsed size the hover-rotated mark's corner reached INTO that
 * curve and the rail sheared a sliver off it: a first-time user read the clipped
 * mark as "something cut off at the left edge of the logo" and could not tell the
 * logo was the expand control (gui-user-test `sidebar-rail-collapse-expand`, run
 * 36550183001, frame `04-left_click.png`). Measured off that frame, the mark's
 * top is sheared to a flat horizontal line where a ghost's head should curve. The
 * expanded logo is smaller and offset by the wordmark, so its rotated extent
 * never reaches the curve.
 *
 * jsdom computes no layout and applies no `:hover`, so a render cannot see the
 * clip. This asserts the geometry over the rail chrome's own source
 * (shell/nav/railChrome.tsx): it reads the
 * collapsed box size and confirms the mark is centred in the strip, then requires
 * the hover-rotated mark box to stay out of the rail's rounded-corner squares.
 * Centring is horizontal-only, so the mark's TOP is pinned by the header's `pt-2`
 * and only a collapsed `mt` nudge lifts it clear of the corner square; the check
 * models both axes separately. The rule treats the whole corner square as unsafe
 * (not merely the sliver outside the arc), which keeps a margin for the mark's
 * own anti-aliased / full-bleed edge — and it recomputes from the shipped size,
 * radius, insets and rail width, so it re-judges any later tweak rather than
 * pinning one class name.
 */
import { describe, it, expect } from 'vitest'

async function appSource(): Promise<string> {
  return (await import('../shell/nav/railChrome.tsx?raw')).default as string
}

/** Tailwind spacing step: `-1` = 0.25rem = 4px. */
const STEP_PX = 4
/** `rounded-xl` = 0.75rem = 12px (the rail card's border-box corner radius). */
const ROUNDED_XL_PX = 12
/** The rail card's 1px border, which the content sits inside. */
const RAIL_BORDER_PX = 1
/** Rail hover tilt: `group-hover:rotate-[-8deg]` on the glyph (RailHeaderGlyph). */
const HOVER_ROTATE_DEG = -8
/** Collapsed rail TRACK width, from useRailWidth (RAIL_W_COLLAPSED). */
const RAIL_TRACK_COLLAPSED = 60
/** The rail card's horizontal margin: `mx-2` = 8px on EACH side. */
const RAIL_MX_EACH_PX = 2 * STEP_PX

/** Rail content width inside the card margin and the 1px border, both sides. */
const RAIL_INNER_WIDTH = RAIL_TRACK_COLLAPSED - 2 * RAIL_MX_EACH_PX - 2 * RAIL_BORDER_PX
/** The content-box corner radius: the border-box radius minus the border. */
const CORNER_SQUARE = ROUNDED_XL_PX - RAIL_BORDER_PX
/** The collapsed header container's top padding: `pt-2` = 8px (App.tsx). The
 *  glyph's TOP is pinned here — `justify-center` centres only the main
 *  (horizontal) axis, so a smaller box or a wider inset never lifts the top. */
const HEADER_PT_PX = 2 * STEP_PX

/** `w-<n> h-<n>` -> box px. Reads the first `w-N h-N` square pair in a class. */
function boxPx(boxClass: string): number {
  const m = boxClass.match(/w-(\d+)\s+h-\1/)
  if (!m) throw new Error(`cannot read a square size from boxClass "${boxClass}"`)
  return Number(m[1]) * STEP_PX
}

/** The collapsed boxClass the component passes to RailHeaderGlyph. */
function collapsedBoxClass(src: string): string {
  // boxClass={branding?.logoClass ?? (effectiveCollapsed ? 'w-9 h-9' : 'w-7 h-7')}
  const m = src.match(/effectiveCollapsed \? '([^']+)' : 'w-7 h-7'/)
  expect(m, 'expected the collapsed-vs-expanded boxClass ternary in railChrome.tsx').not.toBeNull()
  return m![1]
}

/** True when the collapsed header centres the lone glyph in the strip. The mark's
 *  symmetric inset — hence its distance from the rounded corner — depends on it. */
function collapsedCentresLogo(src: string): boolean {
  // className={`group ... w-full ... ${effectiveCollapsed ? 'justify-center' : ''}`}
  return /effectiveCollapsed \? 'justify-center' : ''/.test(src)
}

/** The collapsed top nudge on the glyph wrapper: `mt-<n>` px, or 0 when absent.
 *  Centring is horizontal-only, so this (added to the header's `pt-2`) is the
 *  only lever that lifts the mark's top out of the rounded-corner square. */
function collapsedTopNudgePx(src: string): number {
  // className={`flex items-center gap-2.5 min-w-0 ... ${effectiveCollapsed ? 'mt-1' : ''}`}
  const m = src.match(/min-w-0[^`]*\$\{effectiveCollapsed \? 'mt-(\d+)' : ''\}/)
  return m ? Number(m[1]) * STEP_PX : 0
}

/**
 * After the hover rotate, does any corner of the collapsed mark's box land inside
 * one of the rail's top rounded-corner squares?
 *
 * Each rounded corner occupies a `CORNER_SQUARE`-sided square at a top corner of
 * the content box. A box corner that falls inside either square is in the zone
 * the rail's `overflow-hidden` can shear. The axes are NOT symmetric: the mark is
 * centred horizontally (`justify-center`) so its left/right inset is
 * `(RAIL_INNER_WIDTH - box) / 2`, but its TOP is pinned by the header's `pt-2`
 * plus any collapsed `mt` nudge — `justify-center` never touches the vertical
 * axis. The box is rotated about its own centre.
 */
function rotatedCornerEntersCurve(box: number, topInset: number): {
  entered: boolean
  at: { x: number; y: number } | null
} {
  const padX = (RAIL_INNER_WIDTH - box) / 2
  const top = topInset
  const cx = padX + box / 2
  const cy = top + box / 2
  const t = (HOVER_ROTATE_DEG * Math.PI) / 180
  const cos = Math.cos(t)
  const sin = Math.sin(t)
  const corners: Array<[number, number]> = [
    [padX, top],
    [padX + box, top],
    [padX, top + box],
    [padX + box, top + box],
  ]
  for (const [x, y] of corners) {
    const rx = cx + (x - cx) * cos - (y - cy) * sin
    const ry = cy + (x - cx) * sin + (y - cy) * cos
    const inTopLeft = rx < CORNER_SQUARE && ry < CORNER_SQUARE
    const inTopRight = rx > RAIL_INNER_WIDTH - CORNER_SQUARE && ry < CORNER_SQUARE
    if (inTopLeft || inTopRight) return { entered: true, at: { x: rx, y: ry } }
  }
  return { entered: false, at: null }
}

describe('collapsed rail header logo clears the rail rounded corner', () => {
  it('centres the collapsed brand mark in the icon strip', async () => {
    const src = await appSource()
    expect(
      collapsedCentresLogo(src),
      'expected the collapsed header to centre its lone glyph (justify-center) so the mark keeps '
        + 'symmetric clearance from both rounded corners',
    ).toBe(true)
    const box = boxPx(collapsedBoxClass(src))
    // A box as wide as the strip would touch both straight edges regardless of
    // the corner; centring only buys clearance when the mark is narrower.
    expect(box).toBeLessThan(RAIL_INNER_WIDTH)
  })

  it('the hover-rotated collapsed logo stays out of the rail corner curve', async () => {
    const src = await appSource()
    const box = boxPx(collapsedBoxClass(src))
    const topInset = HEADER_PT_PX + collapsedTopNudgePx(src)
    const { entered, at } = rotatedCornerEntersCurve(box, topInset)
    const padX = (RAIL_INNER_WIDTH - box) / 2
    expect(
      entered,
      `the collapsed logo (${box}px, ${padX}px side inset, ${topInset}px top inset), tilted `
        + `${HOVER_ROTATE_DEG}deg on hover, drives a corner to (${at?.x.toFixed(1)}, `
        + `${at?.y.toFixed(1)}), inside the rail's ${CORNER_SQUARE}px rounded-corner square — the `
        + `rail shears a sliver off the mark. The mark's top is pinned by the header's pt-2, so `
        + `horizontal centring alone cannot lift it clear: give the collapsed mark more TOP clearance `
        + `(a larger mt nudge) so the hover rotate stays out of the corner square.`,
    ).toBe(false)
  })
})

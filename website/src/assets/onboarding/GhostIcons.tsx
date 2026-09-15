// Onboarding mascot icons. The source art (kiro-ghost-var1.svg,
// kiro-ghost-with-arm.svg + its -outline companion) is a static, non-icon
// illustration asset consumed through a plain URL import — NOT svgr/`?react`,
// and no <svg> element or path data in this (or any) TSX file — so it stays
// inside the `use-lucide-icons` brand-mark carve-out.
//
// Two theming mechanisms live here, both reading the palette from the cascade so
// the mascot re-skins live without per-theme asset variants:
//   • GhostVar1 keeps the older filter treatment: a ring of `drop-shadow(...
//     var(--accent))` offsets traces the image's rendered alpha.
//   • GhostWithArm carries the border as its own shape in the source art and
//     tints that layer through a CSS mask. The border is therefore exactly the
//     one drawn in Figma — a chained filter compounds (its effective width
//     drifts from the nominal offset) and a centred stroke would eat into the
//     body, so neither can reproduce the design.
import { BrandGlyph } from '../../components/BrandIcon'
import ghostVar1Url from './kiro-ghost-var1.svg'
import ghostWithArmUrl from './kiro-ghost-with-arm.svg'
import ghostWithArmOutlineUrl from './kiro-ghost-with-arm-outline.svg'

// Export viewBox of the arm-ghost layer pair, the pad the border occupies inside
// it, and the ship width.
//
// BOX_W is DERIVED, not chosen: the mascot's body has rendered ~43.9px wide since
// before the border existed, and the export's frame grows every time the border
// gets heavier (65×52 at a 2-unit offset, 67×54 at 3). Pinning the body width
// instead of the box width is what stops the ghost resizing when only the border
// weight changed.
const BOX_VB_W = 67
const BOX_VB_H = 54
const BORDER_PAD_VB = 3 // outline offset, i.e. the frame's pad on every side
const BODY_VB_W = 60.661 // body silhouette width in viewBox units
const BODY_W = 43.87 // …and the px width it has always rendered at
const BOX_W = (BODY_W * BOX_VB_W) / BODY_VB_W

// 8-way accent outline: offset drop-shadows every 45° at a ~0.9px radius (CSS
// +Y is down). The up-left offset is trimmed further because the ghost's broad
// top-left dome otherwise reads thicker there. Traces the silhouette in the
// theme accent (`var(--accent)`) so it re-skins live. A soft glow trails it.
const OUTLINE = [
  [1.0, 0], [1.1, 1.1], [0, 0.9], [-0.64, 0.64],
  [-0.72, 0], [-0.28, -0.28], [0, -0.7], [0.64, -0.64],
]
  .map(([x, y]) => `drop-shadow(${x}px ${y}px 0 var(--accent))`)
  .join(' ')
const GLOW = 'drop-shadow(0 6px 14px var(--accent-glow))'
const themedStyle = { display: 'block', filter: `${OUTLINE} ${GLOW}`, transform: 'translateY(-2px)' } as const

/** Step 1 (Pick your look) mascot — intrinsic 51×48. */
export function GhostVar1({ width = 52 }: { width?: number }) {
  return (
    <img
      src={ghostVar1Url}
      width={width}
      height={(width * 48) / 51}
      alt=""
      aria-hidden="true"
      style={themedStyle}
    />
  )
}

/**
 * Feature-education mascot (onboarding steps 3-5) — the arm-raised ghost. The
 * Figma source is one drawing of three layers (body, body-outline, eyes); it
 * ships as TWO files so the outline layer alone can follow the theme while the
 * body keeps its own colours, which a single <img> cannot express (CSS never
 * reaches inside an <img>, and inline SVG path data in TSX is blocked outright
 * by `use-lucide-icons`):
 *
 *   1. the outline: `kiro-ghost-with-arm-outline.svg`, painted through a CSS
 *      `mask` over `currentColor` by the shared `BrandGlyph` — so it re-skins
 *      live from `var(--accent)`.
 *   2. the body: `kiro-ghost-with-arm.svg` (body + eyes) as a plain <img> ON TOP.
 *      A full-colour mark is NOT flattened to currentColor.
 *
 * The outline layer is the FILLED EXPANDED SILHOUETTE, not the hollow ring Figma
 * exports. Both describe the same visible band — the body-outline layer is an
 * outset stroke, so its inner contour is the body silhouette and its outer
 * contour lies BORDER_PAD_VB units beyond — but the ring's inner contour is up to
 * 0.34 units off the body's own edge, and stacking two independently-antialiased
 * layers along a near-shared edge let the background through as a dark hairline.
 * Keeping only the outer contour removes that edge: the opaque body covers the
 * interior, so what remains visible is exactly the intended band, seam-free and
 * immune to sub-pixel scaling differences between the mask and the <img>.
 *
 * Border weight is therefore whatever Figma drew — currently a 3-unit offset,
 * i.e. 2.17 CSS px at the ship width. It scales with `width` like any other
 * stroke in the drawing; to change the weight, redraw it in Figma and re-export
 * (then update BORDER_PAD_VB and the viewBox constants above, which the export's
 * frame size tells you).
 *
 * Both files share the viewBox `0 0 67 54`, with the pad the border needs already
 * baked into the coordinates by the export (the body occupies 3..63.66 × 3..50.99),
 * so the layers align by construction with no offset math.
 */
export function GhostWithArm({ width = BOX_W }: { width?: number }) {
  const height = (width * BOX_VB_H) / BOX_VB_W
  // The border pad shifts the art in from the box edge; pull the box back by that
  // much (plus the -2px lift the old mascot carried) so the ghost lands where it
  // did before, whatever the current border weight is.
  const padPx = (BORDER_PAD_VB * width) / BOX_VB_W
  return (
    <span
      aria-hidden="true"
      className="relative block text-accent"
      style={{
        width,
        height,
        filter: GLOW, // accent glow, beneath both layers
        transform: `translate(${-padPx}px, ${-(padPx + 2)}px)`,
      }}
    >
      <BrandGlyph
        url={ghostWithArmOutlineUrl}
        size={width}
        height={height}
        className="absolute inset-0 block"
        testId="ghost-with-arm-outline"
      />
      <img
        src={ghostWithArmUrl}
        width={width}
        height={height}
        alt=""
        aria-hidden="true"
        className="absolute inset-0 block"
      />
    </span>
  )
}

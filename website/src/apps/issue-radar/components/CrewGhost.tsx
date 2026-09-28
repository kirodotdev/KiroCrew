/**
 * CrewGhost — a crew's identity avatar: the Kiro ghost, cropped out of a baked
 * sprite sheet and kept stable for the life of the crew.
 *
 * ── Why an asset and not drawing code ──
 * `website/AUTOSDE.yaml`'s `use-lucide-icons` is BLOCKING, and lucide-react
 * deliberately ships no mascot marks. The rule's BRAND-MARK EXCEPTION is what
 * lets the Kiro ghost exist at all, and it only applies when ALL THREE of its
 * conditions hold. This component satisfies them as follows:
 *
 *   1. Asset, not code — the art is `./crew-ghost-sprite.png`, co-located beside
 *      this component and consumed through a plain URL import (`spriteUrl`).
 *      Nothing here contains an `<svg>` element or path data, so the rule's
 *      unconditional regex gate is satisfied too. The drawing program that
 *      PRODUCED the png lives in `crew-ghost-sprite.gen.mjs` beside it, is run by
 *      hand, and is not part of the app bundle.
 *   2. Render matches the mark — this mark is FULL-COLOUR and its identity
 *      depends on its own colours: the eight looks are told apart by them (purple
 *      witch hat, red / blue / purple capes, teal beanie, gold crown, orange
 *      party hat, blue lens glint, pink blush). So it takes condition 2's
 *      full-colour branch — a plain `<img>`, NOT `BrandGlyph`'s CSS mask over
 *      `currentColor`, which would flatten all eight faces into one silhouette.
 *      Baking the palette also preserves the previous implementation's deliberate
 *      choice that the art reads as content and must not shift with the theme.
 *   3. Brand identity, not affordance — it depicts the Kiro ghost, which the rule
 *      names explicitly. Nothing here stands in for an action, object or status;
 *      those still use lucide-react.
 *
 * ── The sheet ──
 * One png, 8 outfit columns × 2 rows (blush off, blush on), each cell a sprite
 * frame plus a 1-sprite-pixel transparent gutter. `<img>` is absolutely
 * positioned inside an `overflow: hidden` box and shifted by whole cells, which
 * is the plain-`<img>` path applied to a sheet; the gutter is what keeps a
 * fractional device-pixel ratio from oversampling the neighbouring frame's cape.
 * The art is a faithful bake of the eight looks in `pages/scenes/GhostScene.tsx`
 * (same 24×28 `KIRO_GHOST_PIXELS` body, same accessory geometry) at t=0 with the
 * ghost facing right — an avatar is a still frame, so there is no flutter and no
 * blink. If the scene's accessories change, re-run the generator.
 */
import spriteUrl from './crew-ghost-sprite.png'

/** The sprite frame in sprite pixels — the visible crop, gutter excluded.
 *  Exported so a caller can reserve layout space at the right aspect ratio
 *  instead of guessing (and shifting once the image decodes). */
export const CREW_GHOST_FRAME = {
  width: 29,  // 4 left padding + 24 body + 1 right
  height: 37, // 8 top padding + 28 body + 1 bottom
} as const

/** How `crew-ghost-sprite.png` is laid out, in sprite pixels. The cell is one
 *  frame plus the gutter, so a column/row index scales straight into an offset.
 *  Exported because it is the asset's contract: the generator writes to these
 *  numbers and the crop below reads from them. */
export const CREW_GHOST_SHEET = {
  columns: 8,
  rows: 2,
  cellWidth: CREW_GHOST_FRAME.width + 1,
  cellHeight: CREW_GHOST_FRAME.height + 1,
} as const

/** How many distinct looks exist. Exported so callers (the crew editor's face
 *  strip, a legend) never hard-code 8 or re-derive it from a copy. */
export const ghostVariantCount = CREW_GHOST_SHEET.columns

/**
 * Stable non-crypto string hash (djb2) — the same function as `gradientFor` in
 * `components/appstore/gradient.ts` and `pickAnimal` in
 * `pages/scenes/WateringHoleScene.tsx`. Exported so other crew views select
 * per-identity art (or anything else) from the same number instead of writing a
 * fourth copy that could disagree.
 */
export function djb2(s: string): number {
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0
  return h
}

/**
 * Pick a look for an identity by HASHING the seed — not by roster position.
 *
 * GhostScene indexes its outfit table by array position (`OUTFITS[i % len]` over
 * the live agent list), which is fine for a scene where the ghosts are anonymous
 * set dressing, but wrong for an avatar: sorting the crew list, pausing a crew,
 * or adding one shifts every later index and silently repaints everyone with
 * someone else's hat. Hashing the crew's stable id instead keeps a given crew's
 * face for its whole life, which is the same intent
 * `WateringHoleScene.pickAnimal` states for species.
 */
function variantIndexFor(seed: string, variant?: number | null): number {
  if (variant != null && Number.isFinite(variant)) {
    // Non-null `variant` pins the look (the crew editor's "pick a face" flow).
    // Wrapped, and negatives folded, so an out-of-range value cannot scroll the
    // sheet off its own edge and show an empty box.
    return ((Math.trunc(variant) % ghostVariantCount) + ghostVariantCount) % ghostVariantCount
  }
  return djb2(seed) % ghostVariantCount
}

export interface CrewGhostProps {
  /** Stable per-crew identity (the crew id — NOT its display name, which can be
   *  renamed). Chooses the outfit when `variant` is null. */
  seed: string
  /** Requested height in CSS pixels; the width follows the frame's aspect ratio.
   *  Snapped to a whole number of CSS pixels per sprite pixel (see below), so the
   *  rendered height can differ from the request. */
  size?: number
  /** Pins one of the `ghostVariantCount` looks, ignoring the seed. `null` (the
   *  default) means "derive from the seed". */
  variant?: number | null
  /** Draw the cheeks — the caller's signal that this crew is working. */
  blush?: boolean
  className?: string
}

export default function CrewGhost({ seed, size = 34, variant = null, blush = false, className }: CrewGhostProps) {
  // Pixel art shimmers at a fractional zoom, so resolve CSS pixels per sprite
  // pixel to a whole number and derive the box back from it — the same contract
  // the canvas implementation had, minus its dependence on `devicePixelRatio`
  // (an <img> is rasterised by the compositor at the display's own resolution,
  // so the box no longer changes when the window moves between displays, and a
  // retina display gets the sheet's full 4× detail for free).
  const px = Math.max(1, Math.round(size / CREW_GHOST_FRAME.height))
  const col = variantIndexFor(seed, variant)
  const row = blush ? 1 : 0

  return (
    <span
      className={className}
      data-testid="crew-ghost"
      style={{
        display: 'block',
        flexShrink: 0,
        position: 'relative',
        // The crop: everything but this crew's own cell is clipped away.
        overflow: 'hidden',
        width: CREW_GHOST_FRAME.width * px,
        height: CREW_GHOST_FRAME.height * px,
      }}
      // Decorative: the crew's name is always rendered as text next to this, so
      // an accessible name here would only make a screen reader say it twice.
      aria-hidden
    >
      <img
        src={spriteUrl}
        alt=""
        aria-hidden="true"
        // An <img> is draggable by default; a roster row is not a drag source, so
        // dragging an avatar would otherwise start a stray image drag.
        draggable={false}
        width={CREW_GHOST_SHEET.columns * CREW_GHOST_SHEET.cellWidth * px}
        height={CREW_GHOST_SHEET.rows * CREW_GHOST_SHEET.cellHeight * px}
        style={{
          position: 'absolute',
          left: -col * CREW_GHOST_SHEET.cellWidth * px,
          top: -row * CREW_GHOST_SHEET.cellHeight * px,
          // The sheet MUST render at its full computed size, so state that in CSS
          // and not only in the width/height attributes. Tailwind's preflight ships
          // `img { max-width: 100%; height: auto }`, which beats the attributes: the
          // 240px-wide sheet was clamped to the 29px crop and `height: auto` shrank
          // it to 9px, so every crew but the first showed empty space and the first
          // showed a squashed sliver. `maxWidth: none` is what actually lifts the
          // clamp; the explicit width/height then pin both axes so no later global
          // rule can reintroduce the squash.
          maxWidth: 'none',
          width: CREW_GHOST_SHEET.columns * CREW_GHOST_SHEET.cellWidth * px,
          height: CREW_GHOST_SHEET.rows * CREW_GHOST_SHEET.cellHeight * px,
          // NOT `pixelated`, which the canvas implementation needed. The sheet is
          // baked at 4× and `px` is a whole number, so the browser only ever
          // scales it DOWN by an integer ratio — i.e. it resolves a supersample,
          // which reconstructs the antialiasing the old draw program produced at
          // that size. Measured against that program over all 16 frames: smooth
          // scaling leaves ZERO pixels differing by more than a quarter-channel
          // (mean |Δ| 0.30 at 1×, 0.04 at 2×), where nearest-neighbour leaves
          // 2.1% of pixels visibly wrong (max Δ 247) on the accessory edges. The
          // body survives either way — it is uniform 4×4 blocks in the bake, so
          // an aligned integer downscale keeps its edges hard.
          imageRendering: 'auto',
        }}
      />
    </span>
  )
}

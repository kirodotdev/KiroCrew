/**
 * DiceBear style that generates Kiro ghosts.
 *
 * A DiceBear style is a plain object — `{ meta, schema, create({ prng, options }) }`
 * returning `{ attributes: { viewBox }, body }` — so this needs no dependency
 * beyond `@dicebear/core` and is consumed exactly like a published style pack.
 *
 * The brand mark's own art is NOT duplicated here. `kiro-ghost-mark.svg` is imported
 * as raw text and its three paths are read out of it, so the `.svg` asset is the
 * single source of truth in fact rather than by convention — the same art-as-text
 * pattern `apps/mochi/builtinPacks.ts` uses for its animation assets. That keeps
 * bespoke brand art an asset, which is the condition attached to the
 * `use-lucide-icons` exemption in `website/docs/theming-contract.md`.
 *
 * Everything is composed in the mark's own 1200x1200 coordinate space, so there is
 * no transform math that can drift, and with every trait switched off the output IS
 * the mark. What this module authors itself is the VARIANT vocabulary — eyes, brows,
 * mouths, head items, props — which is parameterised by coordinates measured off the
 * mark and therefore cannot be expressed as a set of static files.
 *
 * What is taken from the asset is GEOMETRY, not presentation. `kiro-ghost-mark.svg`
 * is itself a hollow outline (`fill="none" stroke="#000" stroke-width="80"`), and the
 * avatar deliberately renders a filled white body with no stroke instead, which is the
 * app icon's treatment: a saturated tile behind the ghost, black eyes, no outline.
 *
 * That is not a departure from how this asset is used elsewhere — its only other
 * consumer, `components/KiroGhostMark.tsx`, paints it through `BrandGlyph` as a CSS
 * MASK over `currentColor`, discarding its fill and stroke too. And it follows the
 * doctrine `components/GhostPoses.tsx` documents: the body is white, and an outline is
 * added only where contrast demands it (there, a CSS `drop-shadow()` chain on pale
 * palettes). Here contrast is structural instead — every tile in `TILES` is held below
 * L* 78, which the test asserts, so the white silhouette always reads without a stroke.
 *
 * DRAW ORDER IS PART OF EVERY AVATAR'S IDENTITY. The prng is a single stream and
 * each question consumes exactly one step, so inserting a question re-rolls every
 * trait after it and silently changes faces that already exist. Add new traits at
 * the END of `create`, never in the middle; the test pins the trait tuple of
 * several seeds to catch a violation.
 */
import type { Style, StyleCreateProps } from '@dicebear/core'
import markSvg from '../assets/kiro-ghost-mark.svg?raw'

const WHITE = '#ffffff'
/** Ink pieces sit ON the white body; anything floating on the tile is white instead. */
const INK = '#000000'
const BLUSH = '#ff7a9c'

/**
 * The mark's path data, read out of the asset in document order: silhouette first,
 * then the two eye capsules.
 *
 * Fails loud at module load rather than rendering a blank avatar, because a silently
 * empty `d` is invisible until someone looks at a roster. The count is asserted too:
 * an asset that gains a path would otherwise shift which one is read as an eye.
 */
export function markPaths(svg: string): [string, string, string] {
  const found = [...svg.matchAll(/\sd="([^"]+)"/g)].map((m) => m[1])
  if (found.length !== 3) {
    throw new Error(
      `kiro-ghost-mark.svg must hold exactly 3 paths (body, eye, eye); found ${found.length}`,
    )
  }
  return [found[0], found[1], found[2]]
}

/** Silhouette and eyes, bbox 272.9,202.7 654x795 inside the 1200 tile. */
export const [BODY, EYE_A, EYE_B] = markPaths(markSvg)

/** Brand purple, used for every tile when `hueTile` is off. */
export const BRAND_PURPLE = '#9046ff'

/**
 * Tile colors, chosen by farthest-point selection under CIEDE2000 rather than by
 * sampling a hue circle.
 *
 * Sampling hues is the obvious approach and it is wrong: independent draws put
 * neighbouring seeds within a few degrees of each other, and two tiles ~13 dE
 * apart read as a rendering bug rather than as two identities. This set's
 * minimum pairwise distance is 19.0 dE, which the test asserts. Every entry also
 * keeps L* under 78 so the white silhouette stays legible on it — which is why
 * two entries are muddy rather than vivid; 14 colors cannot be both maximally
 * separated and all bright.
 */
export const TILES = [
  '#de2121',
  '#21de21',
  '#21a5de',
  '#3d259d',
  '#eeae4f',
  '#ee4fee',
  '#259d85',
  '#9d2561',
  '#9d6725',
  '#25679d',
  '#979d25',
  '#ee4f7e',
  '#21d4de',
  '#ee7e4f',
]

/** Eye centres, measured off the shipped paths. */
const EL = 637.5
const ER = 772.6
const EY = 486.7
const CANON = `<path d="${EYE_A}" fill="${INK}"/><path d="${EYE_B}" fill="${INK}"/>`

/** Emit one fragment per eye, so a symmetric variant is written once. */
const pair = (fn: (x: number) => string): string => [EL, ER].map(fn).join('')

/** A closed eyelid. Negative `dip` curves up (content), positive curves down (tired). */
const lid = (x: number, dip: number): string =>
  `<path d="M${x - 44} ${EY + (dip > 0 ? -20 : 26)}q44 ${dip} 88 0" stroke="${INK}" ` +
  `stroke-width="30" fill="none" stroke-linecap="round"/>`

/* Pieces shared by the still (`EYES`) and animated (`WORKING_EYES`) tables. Named
 * once so the two tables cannot drift apart: the working variant of an eye must
 * draw exactly the geometry the seed drew, only wrapped in animation groups. */

/** A four-point twinkle beside an eye. */
const star = (x: number): string =>
  `<path d="M${x + 12} ${EY - 46}l11 24 24 11-24 11-11 24-11-24-24-11 24-11z" ` +
  `fill="${WHITE}"/>`

const VISOR_BAR =
  `<rect x="${EL - 52}" y="${EY - 50}" width="${ER - EL + 104}" height="100" rx="50" ` +
  `fill="${INK}"/>`
const VISOR_GLINT =
  `<rect x="${EL - 22}" y="${EY - 26}" width="34" height="20" rx="10" fill="${WHITE}" ` +
  `opacity="0.85"/>`

const GLASSES_FRAMES =
  pair(
    (x) => `<circle cx="${x}" cy="${EY}" r="66" fill="none" stroke="${INK}" stroke-width="18"/>`,
  ) +
  // Absolute `L` endpoint rather than a relative `h` run: a template chunk that
  // opens with `h` is read as an hours unit by the i18n unit-literal gate.
  `<path d="M${EL + 66} ${EY}L${ER - 66} ${EY}" stroke="${INK}" stroke-width="16"/>` +
  `<path d="M${EL - 66} ${EY - 10}l-46-26" stroke="${INK}" stroke-width="16" ` +
  `stroke-linecap="round"/>`

const heartEye = (x: number): string =>
  `<path d="M${x} ${EY + 44}C${x - 60} ${EY} ${x - 42} ${EY - 48} ${x} ${EY - 18}` +
  `C${x + 42} ${EY - 48} ${x + 60} ${EY} ${x} ${EY + 44}Z" fill="${INK}"/>`

export const EYES: Record<string, string> = {
  /** The mark itself. Weighted so most of a roster looks exactly like Kiro. */
  canon: CANON,
  closed: pair((x) => lid(x, -52)),
  sleepy: pair((x) => lid(x, 52)),
  wink: `<path d="${EYE_A}" fill="${INK}"/>${lid(ER, -52)}`,
  wide:
    pair((x) => `<circle cx="${x}" cy="${EY}" r="54" fill="${INK}"/>`) +
    pair((x) => `<circle cx="${x + 17}" cy="${EY - 20}" r="16" fill="${WHITE}"/>`),
  sparkle: CANON + pair(star),
  /** One bar instead of two eyes: reads as a machine rather than a face. */
  visor: VISOR_BAR + VISOR_GLINT,
  glasses: CANON + GLASSES_FRAMES,
  cross:
    `<path d="M${EL - 38} ${EY - 34}l76 34-76 34z" fill="${INK}"/>` +
    `<path d="M${ER + 38} ${EY - 34}l-76 34 76 34z" fill="${INK}"/>`,
  squint: pair(
    (x) => `<rect x="${x - 42}" y="${EY - 14}" width="84" height="28" rx="14" fill="${INK}"/>`,
  ),
  swirl:
    pair(
      (x) =>
        `<circle cx="${x}" cy="${EY}" r="46" fill="none" stroke="${INK}" stroke-width="20"/>`,
    ) + pair((x) => `<circle cx="${x}" cy="${EY}" r="10" fill="${INK}"/>`),
  heart: pair(heartEye),
  cyclops:
    `<ellipse cx="${(EL + ER) / 2}" cy="${EY}" rx="66" ry="84" fill="${INK}"/>` +
    `<circle cx="${(EL + ER) / 2 + 22}" cy="${EY - 28}" r="20" fill="${WHITE}"/>`,
}

/* ────────────────────────── Working animation ──────────────────────────
 *
 * A running crew's avatar is ALIVE, not a different avatar. The rule that makes
 * that safe: the working variant may only MOVE what the seed drew — animation
 * groups and a `<style>` block wrap the same geometry `EYES` renders, and every
 * keyframe is a transform or opacity that returns to rest. No trait is added,
 * removed, or recolored (blush in particular stays exactly as the seed rolled
 * it), so identity is untouched and `working` never consumes a prng draw.
 *
 * Eye idioms are per-variant because the eye vocabulary is semantic: a visor has
 * no eyelid to blink and `closed` is already shut. A variant without an idiom
 * falls back to its still art and gets the body layer only.
 */

export type WorkingIntensity = 'subtle' | 'full'

/** Wrap `art` in an animation group, optionally pinning the transform origin
 *  (user units of the 1200-space; SVG `transform-box` defaults to view-box). */
const anim = (cls: string, art: string, origin?: [number, number], delay?: string): string =>
  `<g class="${cls}"` +
  (origin || delay
    ? ` style="${origin ? `transform-origin:${origin[0]}px ${origin[1]}px` : ''}` +
      `${origin && delay ? ';' : ''}${delay ? `animation-delay:${delay}` : ''}"`
    : '') +
  `>${art}</g>`

/** Eye-center origin shared by both-eye squash animations (blink, squeeze). */
const EYE_ORIGIN: [number, number] = [(EL + ER) / 2, 487]

/**
 * Animated counterparts of `EYES`. Each entry re-composes the SAME pieces its
 * still twin is built from (`CANON`, `star`, `VISOR_*`, …), so the geometry
 * cannot drift; the test suite strips the animation wrappers and asserts the
 * remaining shapes equal the still art byte for byte.
 */
export const WORKING_EYES: Record<string, string> = {
  /** Double-blink plus an occasional glance to the side. */
  canon: anim('kg-look', anim('kg-blink', CANON, EYE_ORIGIN)),
  /** Already shut — no idiom; the body layer alone reads as meditative work. */
  closed: EYES.closed,
  /** Drowsy lids lift and fall: fighting sleep but still on the job. */
  sleepy: anim('kg-drowse', EYES.sleepy),
  /** Only the open eye blinks; the wink is identity and stays. */
  wink: anim('kg-blink', `<path d="${EYE_A}" fill="${INK}"/>`, [EL, 487]) + lid(ER, -52),
  /** Alert darting: the wide stare scans around while it works. */
  wide: anim('kg-dart', EYES.wide),
  /** Canon eyes blink; the sparkles twinkle in alternation. */
  sparkle:
    anim('kg-blink', CANON, EYE_ORIGIN) +
    anim('kg-tw', star(EL), [EL + 12, EY - 11]) +
    anim('kg-tw', star(ER), [ER + 12, EY - 11], '.45s'),
  /** The glint sweeps the bar: a machine scanning. */
  visor: VISOR_BAR + anim('kg-scan', VISOR_GLINT),
  /** Only the eyes behind the lenses blink; the frames are furniture. */
  glasses: anim('kg-blink', CANON, EYE_ORIGIN) + GLASSES_FRAMES,
  /** Strained high-frequency shiver: bearing down on the task. */
  cross: anim('kg-jitter', EYES.cross),
  /** Periodically squints even harder: taking aim. */
  squint: anim('kg-squeeze', EYES.squint, EYE_ORIGIN),
  /** Off-center rotation makes the spiral visibly churn: deep in thought. */
  swirl:
    pair((x) =>
      anim(
        'kg-spin',
        `<circle cx="${x}" cy="${EY}" r="46" fill="none" stroke="${INK}" stroke-width="20"/>`,
        [x + 14, EY],
      ),
    ) + pair((x) => `<circle cx="${x}" cy="${EY}" r="10" fill="${INK}"/>`),
  /** Heartbeat: loves the work. */
  heart: pair((x) => anim('kg-hb', heartEye(x), [x, EY])),
  /** One slow blink of the single eye. */
  cyclops: anim('kg-cyblink', EYES.cyclops, [(EL + ER) / 2, 487]),
}

/**
 * Body-layer motion, two tiers: `subtle` for dense surfaces (roster rows) and
 * `full` for a surface showing one avatar (a DM header). Same keyframes, only
 * amplitudes and tempo differ.
 *
 * The `kg-fit` wrapper pre-shrinks and lowers the whole drawing so the bob's
 * rise plus the stretch's lift cannot push tall head accessories (antenna ball
 * at y≈104, halo, party hat) past the top of the 1200 tile — the margin above
 * the artwork is smaller than the full-tier excursion, so headroom is made, not
 * assumed.
 */
const MOTION: Record<
  WorkingIntensity,
  { fitScale: number; fitDrop: number; rise: number; sink: number; stretch: number; sway: number; secs: number }
> = {
  subtle: { fitScale: 0.97, fitDrop: 12, rise: 14, sink: 5, stretch: 0.02, sway: 1.6, secs: 2.4 },
  full: { fitScale: 0.94, fitDrop: 28, rise: 34, sink: 12, stretch: 0.05, sway: 3.2, secs: 1.6 },
}

/** The `<style>` block for a working avatar. Eye idiom tempo is shared across
 *  tiers — at roster size the eye is barely legible, so only body amplitude
 *  scales. Reduced-motion users get the still frame: identity is identical, so
 *  dropping every animation is a complete, lossless fallback. */
export function workingStyle(intensity: WorkingIntensity): string {
  const m = MOTION[intensity]
  return (
    '<style>' +
    `@keyframes kg-bob{0%,100%{transform:translateY(${m.sink}px)}50%{transform:translateY(-${m.rise}px)}}` +
    `@keyframes kg-sq{0%,100%{transform:scale(${1 + m.stretch},${1 - m.stretch})}50%{transform:scale(${1 - m.stretch / 2},${1 + m.stretch})}}` +
    `@keyframes kg-sway{0%,100%{transform:rotate(-${m.sway}deg)}50%{transform:rotate(${m.sway}deg)}}` +
    '@keyframes kg-blink{0%,80%,88%,96%,100%{transform:scaleY(1)}84%,92%{transform:scaleY(0.06)}}' +
    '@keyframes kg-look{0%,26%,100%{transform:translateX(0)}34%,50%{transform:translateX(30px)}60%,84%{transform:translateX(-26px)}}' +
    '@keyframes kg-drowse{0%,50%,100%{transform:translateY(0)}62%,80%{transform:translateY(-18px)}}' +
    '@keyframes kg-dart{0%,18%,100%{transform:translate(0,0)}24%,42%{transform:translate(24px,-8px)}48%,66%{transform:translate(-20px,8px)}72%,88%{transform:translate(8px,-12px)}}' +
    '@keyframes kg-tw{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.1;transform:scale(.5)}}' +
    '@keyframes kg-scan{0%,100%{transform:translateX(-24px)}50%{transform:translateX(130px)}}' +
    '@keyframes kg-jitter{0%,100%{transform:translateX(0)}25%{transform:translateX(-8px)}50%{transform:translateX(7px)}75%{transform:translateX(-5px)}}' +
    '@keyframes kg-squeeze{0%,66%,100%{transform:scaleY(1)}76%,90%{transform:scaleY(0.45)}}' +
    '@keyframes kg-spin{to{transform:rotate(360deg)}}' +
    '@keyframes kg-hb{0%,40%,100%{transform:scale(1)}12%{transform:scale(1.3)}26%{transform:scale(1.1)}}' +
    '@keyframes kg-cyblink{0%,86%,96%,100%{transform:scaleY(1)}91%{transform:scaleY(0.05)}}' +
    `.kg-fit{transform:translateY(${m.fitDrop}px) scale(${m.fitScale});transform-origin:600px 600px}` +
    `.kg-sway{animation:kg-sway ${m.secs * 1.85}s ease-in-out infinite;transform-origin:600px 620px}` +
    `.kg-bob{animation:kg-bob ${m.secs}s ease-in-out infinite}` +
    `.kg-sq{animation:kg-sq ${m.secs}s ease-in-out infinite;transform-origin:600px 985px}` +
    '.kg-blink{animation:kg-blink 2.6s infinite}' +
    '.kg-look{animation:kg-look 4.4s ease-in-out infinite}' +
    '.kg-drowse{animation:kg-drowse 3.6s ease-in-out infinite}' +
    '.kg-dart{animation:kg-dart 2.4s ease-in-out infinite}' +
    '.kg-tw{animation:kg-tw .9s infinite}' +
    '.kg-scan{animation:kg-scan 1.3s ease-in-out infinite}' +
    '.kg-jitter{animation:kg-jitter .3s linear infinite}' +
    '.kg-squeeze{animation:kg-squeeze 2.4s ease-in-out infinite}' +
    '.kg-spin{animation:kg-spin 1.4s linear infinite}' +
    '.kg-hb{animation:kg-hb 1.1s infinite}' +
    '.kg-cyblink{animation:kg-cyblink 3.2s infinite}' +
    // `animation:none` alone is not a still frame: `.kg-fit` is a STATIC
    // transform, so it must be reset too or a reduced-motion user sees a
    // shrunken, lowered ghost with no motion to justify that headroom.
    '@media (prefers-reduced-motion:reduce){*{animation:none!important}.kg-fit{transform:none}}' +
    '</style>'
  )
}

/** Brows sit just above the eyes. The mark is browless, so `none` is weighted. */export const BROWS: Record<string, string> = {
  none: '',
  raised: pair(
    (x) =>
      `<path d="M${x - 40} 402q40-34 80 0" stroke="${INK}" stroke-width="20" fill="none" ` +
      `stroke-linecap="round"/>`,
  ),
  angry:
    `<path d="M${EL - 42} 380l84 30" stroke="${INK}" stroke-width="22" stroke-linecap="round"/>` +
    `<path d="M${ER + 42} 380l-84 30" stroke="${INK}" stroke-width="22" stroke-linecap="round"/>`,
  flat: pair(
    (x) =>
      `<path d="M${x - 40} 396h80" stroke="${INK}" stroke-width="20" stroke-linecap="round"/>`,
  ),
}

/** The mark has no mouth, so `none` is weighted heavily. */
export const MOUTHS: Record<string, string> = {
  none: '',
  smile:
    `<path d="M660 622q45 42 90 0" stroke="${INK}" stroke-width="26" fill="none" ` +
    `stroke-linecap="round"/>`,
  open: `<path d="M662 612q43 78 86 0Z" fill="${INK}"/>`,
  cat:
    `<path d="M662 612q22 30 43 0q22 30 43 0" stroke="${INK}" stroke-width="24" fill="none" ` +
    `stroke-linecap="round"/>`,
  oh: `<ellipse cx="705" cy="628" rx="30" ry="38" fill="${INK}"/>`,
  grin:
    `<path d="M652 606q53 82 106 0Z" fill="${INK}"/>` +
    `<path d="M660 618h90" stroke="${WHITE}" stroke-width="18"/>`,
  tongue:
    `<path d="M662 606q43 78 86 0Z" fill="${INK}"/>` +
    `<path d="M688 648q17 34 34 0Z" fill="${BLUSH}"/>`,
  wobble:
    `<path d="M660 620l22 18 22-18 22 18 22-18" stroke="${INK}" stroke-width="20" fill="none" ` +
    `stroke-linecap="round" stroke-linejoin="round"/>`,
  smirk:
    `<path d="M666 626q52 30 78-16" stroke="${INK}" stroke-width="24" fill="none" ` +
    `stroke-linecap="round"/>`,
}

/** Head-top furniture, in ink where it overlaps the body and white where it floats. */
export const ACCESSORIES: Record<string, string> = {
  antenna:
    `<path d="M628 250V120" stroke="${WHITE}" stroke-width="34" stroke-linecap="round"/>` +
    `<circle cx="628" cy="104" r="44" fill="${WHITE}"/>`,
  halo: `<ellipse cx="632" cy="150" rx="196" ry="52" fill="none" stroke="${WHITE}" stroke-width="34"/>`,
  cap:
    `<path d="M380 330a262 262 0 0 1 500 24l-8 26q-244-96-492-50Z" fill="${INK}"/>` +
    `<path d="M872 380q120 6 150 54-96 26-158-28Z" fill="${INK}"/>`,
  phones:
    `<path d="M352 470V392a288 262 0 0 1 556 30" fill="none" stroke="${INK}" stroke-width="40" ` +
    `stroke-linecap="round"/>` +
    `<rect x="296" y="440" width="112" height="180" rx="52" fill="${INK}"/>` +
    `<rect x="856" y="440" width="112" height="180" rx="52" fill="${INK}"/>`,
  bow:
    `<path d="M436 300l-96-58v120zM436 300l96-58v120z" fill="${INK}"/>` +
    `<circle cx="436" cy="300" r="28" fill="${INK}"/>`,
  crown: `<path d="M470 320v-118l74 62 66-86 66 86 74-62v118Z" fill="${INK}"/>`,
  beanie:
    `<path d="M396 336a244 244 0 0 1 456 10Z" fill="${INK}"/>` +
    `<path d="M392 336h462v52H392Z" fill="${INK}"/>` +
    `<circle cx="624" cy="164" r="46" fill="${WHITE}"/>` +
    `<path d="M624 210v-46" stroke="${WHITE}" stroke-width="20"/>`,
  party:
    `<path d="M624 118l112 214H512Z" fill="${INK}"/>` +
    `<circle cx="624" cy="104" r="34" fill="${WHITE}"/>`,
  flower:
    [0, 72, 144, 216, 288]
      .map((a) => {
        const r = (a * Math.PI) / 180
        const cx = (470 + Math.cos(r) * 44).toFixed(1)
        const cy = (300 + Math.sin(r) * 44).toFixed(1)
        return `<circle cx="${cx}" cy="${cy}" r="30" fill="${INK}"/>`
      })
      .join('') + `<circle cx="470" cy="300" r="24" fill="${WHITE}"/>`,
  bandana:
    // Kept above y=380 so it cannot collide with the brow line at y=396.
    `<path d="M392 342q230-96 470-22l-14 62q-232-70-448 20Z" fill="${INK}"/>` +
    `<path d="M392 342l-104 40 56 34Z" fill="${INK}"/>`,
  hardhat:
    `<path d="M416 336a212 212 0 0 1 416 12Z" fill="${INK}"/>` +
    `<path d="M356 348h536v50H356Z" fill="${INK}"/>` +
    `<path d="M624 336V190" stroke="${WHITE}" stroke-width="22"/>`,
}

/**
 * A prop on the lower-right flank, reading as something the ghost is holding.
 *
 * Ink, not white. White confines a prop to the 273-unit margin beside the
 * silhouette — 22% of the tile at most, which is unidentifiable once the avatar
 * renders at 28px; ink can straddle the body edge and still read against both the
 * white silhouette and the saturated tile, so the prop gets twice the size.
 *
 * `PROP_AT` sits below the mouth band (y 606..648) because a prop and a mouth can
 * co-occur. Authoring each prop small at the origin and scaling by `PROP_SCALE`
 * keeps them all at one visual weight, controlled by a single number.
 */
const PROP_SCALE = 3.1
const PROP_AT = [764, 726]
const prop = (d: string): string =>
  `<g transform="translate(${PROP_AT[0]},${PROP_AT[1]}) scale(${PROP_SCALE})">${d}</g>`

export const PROPS: Record<string, string> = {
  none: '',
  mug: prop(
    `<rect x="0" y="44" width="80" height="72" rx="14" fill="${INK}"/>` +
      `<path d="M80 64h22a22 22 0 0 1 0 40H80" fill="none" stroke="${INK}" stroke-width="14"/>` +
      `<path d="M18 26q10-22 20 0" stroke="${INK}" stroke-width="10" fill="none" ` +
      `stroke-linecap="round"/>` +
      `<path d="M50 26q10-22 20 0" stroke="${INK}" stroke-width="10" fill="none" ` +
      `stroke-linecap="round"/>`,
  ),
  glass: prop(
    `<circle cx="46" cy="52" r="40" fill="none" stroke="${INK}" stroke-width="16"/>` +
      `<path d="M76 82l34 34" stroke="${INK}" stroke-width="20" stroke-linecap="round"/>`,
  ),
  wrench: prop(
    `<path d="M14 8l34 34-22 22L-8 30A36 36 0 0 1 14 8Z" fill="${INK}"/>` +
      `<path d="M34 56l68 68" stroke="${INK}" stroke-width="22" stroke-linecap="round"/>`,
  ),
  bolt: prop(`<path d="M64 0L4 92h40L22 168 96 66H54Z" fill="${INK}"/>`),
  star: prop(
    `<path d="M56 4l18 40 44 5-33 30 10 43-39-23-39 23 10-43-33-30 44-5Z" fill="${INK}"/>`,
  ),
  term: prop(
    `<rect x="0" y="12" width="112" height="86" rx="12" fill="none" stroke="${INK}" ` +
      `stroke-width="13"/>` +
      `<path d="M22 44l16 13-16 13" stroke="${INK}" stroke-width="11" fill="none" ` +
      `stroke-linecap="round"/>` +
      `<path d="M50 70h30" stroke="${INK}" stroke-width="11" stroke-linecap="round"/>`,
  ),
  heart: prop(`<path d="M56 104C-8 60 14 6 56 34C98 6 120 60 56 104Z" fill="${INK}"/>`),
}

/**
 * Weighted pick lists. Repeating a key IS how DiceBear expresses weight — there is
 * no probability parameter — and `canon` / `none` are repeated so a roster reads as
 * Kiro rather than as a costume box.
 */
const EYE_PICKS = [
  'canon',
  'canon',
  'canon',
  'canon',
  'closed',
  'closed',
  'sleepy',
  'wink',
  'wide',
  'sparkle',
  'visor',
  'glasses',
  'cross',
  'squint',
  'swirl',
  'heart',
  'cyclops',
]
const BROW_PICKS = ['none', 'none', 'none', 'none', 'raised', 'angry', 'flat']
const MOUTH_PICKS = [
  'none',
  'none',
  'none',
  'smile',
  'smile',
  'open',
  'cat',
  'oh',
  'grin',
  'tongue',
  'wobble',
  'smirk',
]
const ACCESSORY_PICKS = [
  'antenna',
  'halo',
  'cap',
  'phones',
  'bow',
  'crown',
  'beanie',
  'party',
  'flower',
  'bandana',
  'hardhat',
]
const PROP_PICKS = ['mug', 'glass', 'wrench', 'bolt', 'star', 'term', 'heart']

export interface KiroGhostOptions {
  accessoryProbability: number
  blushProbability: number
  flipProbability: number
  propProbability: number
  /** Off gives every tile the brand purple; on gives each seed a palette entry. */
  hueTile: boolean
  /** Render the working (animated) variant at this intensity. A render option,
   *  not a trait: it consumes no prng draw and defaults to the still frame. */
  working: WorkingIntensity
}

export interface KiroGhostTraits {
  eyes: string
  brows: string
  mouth: string
  accessory: string
  prop: string
  blush: boolean
  flip: boolean
  tile: string
}

/**
 * The only composition path. `create` calls it with prng-drawn traits and tests
 * call it with explicit ones, so a fixture cannot drift from real output.
 *
 * `working` is a RENDER parameter, not a trait: it is never drawn from the prng
 * (so it cannot re-roll a face) and with it unset the output is byte-identical
 * to what this function has always produced. Set, it swaps each eye for its
 * animated twin and wraps the drawing in the body-motion groups.
 */
export function compose(t: KiroGhostTraits, working?: WorkingIntensity | null): string {
  const eyesArt = working ? (WORKING_EYES[t.eyes] ?? EYES[t.eyes] ?? '') : (EYES[t.eyes] ?? '')
  const inner = [
    `<path d="${BODY}" fill="${WHITE}"/>`,
    t.blush
      ? `<ellipse cx="${EL - 20}" cy="600" rx="46" ry="26" fill="${BLUSH}" opacity="0.55"/>` +
        `<ellipse cx="${ER + 34}" cy="600" rx="46" ry="26" fill="${BLUSH}" opacity="0.55"/>`
      : '',
    eyesArt,
    BROWS[t.brows] ?? '',
    MOUTHS[t.mouth] ?? '',
    ACCESSORIES[t.accessory] ?? '',
    PROPS[t.prop] ?? '',
  ].join('')
  // Body motion wraps INSIDE the flip group, so transform origins stay in the
  // unmirrored coordinate space and a flipped ghost sways identically.
  const core = working
    ? `<g class="kg-fit"><g class="kg-sway"><g class="kg-bob"><g class="kg-sq">${inner}</g></g></g></g>`
    : inner
  // The tile rect is painted here rather than through core's `backgroundColor` so
  // its color is drawn from the same prng as every other trait. It stays outside
  // the mirror group so flipping cannot move it.
  return (
    (working ? workingStyle(working) : '') +
    `<rect width="1200" height="1200" fill="${t.tile}"/>` +
    (t.flip ? `<g transform="translate(1200,0) scale(-1,1)">${core}</g>` : core)
  )
}

/**
 * Corner radius, as DiceBear's percent-of-viewport `radius` option and the
 * equivalent rect `rx` in the mark's 1200-unit space. Both spellings exist
 * because the seeded avatar path goes through DiceBear (which clips) while a
 * pinned face is composed directly and must clip itself identically.
 */
export const GHOST_RADIUS_PCT = 12
const GHOST_RADIUS_UNITS = (1200 * GHOST_RADIUS_PCT) / 100

/**
 * Compose explicit traits into the same data-URI form the seeded DiceBear
 * path emits. Lives here (a `.ts` module, beside `compose`) rather than in a
 * component: the use-lucide-icons rule bans an `<svg>` literal in any `.tsx`
 * file unconditionally, and this wrapper is brand-mark geometry, which is
 * exactly what this module already owns.
 */
export function ghostDataUri(traits: KiroGhostTraits, working?: WorkingIntensity | null): string {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 1200">` +
    `<clipPath id="r"><rect width="1200" height="1200" rx="${GHOST_RADIUS_UNITS}"/></clipPath>` +
    `<g clip-path="url(#r)">${compose(traits, working)}</g></svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

export const kiroGhost: Style<KiroGhostOptions> = {
  /**
   * `creator` only, deliberately. `@dicebear/core` always emits a Dublin Core
   * metadata block, and its rights line prefixes "Remix of" whenever a `title` is
   * set and the license is not MIT — which would put a false remix claim in every
   * avatar, since this art is first-party. Omitting the title yields the accurate
   * `Design by "Kiro"` instead.
   */
  meta: { creator: 'Kiro' },
  schema: {
    properties: {
      accessoryProbability: { type: 'integer', minimum: 0, maximum: 100, default: 55 },
      blushProbability: { type: 'integer', minimum: 0, maximum: 100, default: 35 },
      flipProbability: { type: 'integer', minimum: 0, maximum: 100, default: 40 },
      propProbability: { type: 'integer', minimum: 0, maximum: 100, default: 30 },
      hueTile: { type: 'boolean', default: true },
      working: { type: 'string', enum: ['subtle', 'full'] },
    },
  },
  create({ prng, options }: StyleCreateProps<KiroGhostOptions>) {
    // Draw order is frozen: append new traits below `prop`, never above.
    const eyes = prng.pick(EYE_PICKS, 'canon')
    const mouth = prng.pick(MOUTH_PICKS, 'none')
    const accessory = prng.bool(options.accessoryProbability ?? 55)
      ? prng.pick(ACCESSORY_PICKS, 'antenna')
      : 'none'
    const blush = prng.bool(options.blushProbability ?? 35)
    // The mark faces right; mirroring gives a second reading of the same shape.
    const flip = prng.bool(options.flipProbability ?? 40)
    const tile = (options.hueTile ?? true) ? prng.pick(TILES, TILES[0]) : BRAND_PURPLE
    const brows = prng.pick(BROW_PICKS, 'none')
    const prop = prng.bool(options.propProbability ?? 30) ? prng.pick(PROP_PICKS, 'star') : 'none'

    const traits: KiroGhostTraits = {
      eyes,
      brows,
      mouth,
      accessory,
      prop,
      blush,
      flip,
      tile,
    }
    return {
      attributes: { viewBox: '0 0 1200 1200' },
      body: compose(traits, options.working ?? null),
      extra: () => ({ ...traits }),
    }
  },
}

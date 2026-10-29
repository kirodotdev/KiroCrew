/**
 * The ICON avatar tier: a flat ghost drawn in one of a fixed set of POSES, over
 * a solid background the user picks.
 *
 * This is the avatar tier new crews are built with. The older ghost-builder
 * ("捏脸") and appearance-pack tiers stay in the code and keep rendering for crews
 * that already wear them (see `CrewAvatar.tsx`), but a NEW avatar is authored
 * here: pick one of the shipped poses, pick a background colour, done. There is
 * no per-trait vocabulary and no reaction layer — the pose IS the whole face.
 *
 * A pose does still show the crew AT WORK, at the ghost's full body-motion
 * amplitude and tempo (`BODY_MOTION` in `kiroGhostAvatar.ts`), so an icon crew
 * bobs and sways beside a ghost crew in the same rhythm. The motion is NOT drawn
 * inside the SVG: `CrewAvatar` paints the tile itself and moves one still image
 * of the silhouette (`poseBodyDataUri`) with page CSS (`crewIconMotion.css`),
 * fed by `iconMotionVars()`. A CSS animation inside an `<img>`'s SVG makes the
 * browser re-raster the picture every frame, and iOS Safari re-rasters it in
 * pieces: the head, the tail and one eye came out of step, so the face looked
 * torn. A still raster moved as one layer cannot come apart: eyes and body are
 * the same pixels. Reduced-motion users get the still frame unchanged.
 *
 * The art is a set of hand-drawn SVGs (`assets/avatarPoses/pose-N.svg`), each a
 * white ghost silhouette with two black eye cut-outs over a background rect.
 * They are imported as RAW TEXT and recoloured here rather than being inlined as
 * markup, the same art-as-asset pattern `kiroGhostAvatar.ts` uses for the brand
 * mark — which keeps the bespoke art an ASSET (the condition on the
 * `use-lucide-icons` exemption in `theming-contract.md`) and keeps this a `.ts`
 * module with no `<svg>` literal to lint.
 *
 * ONLY the background is parameterised. The silhouette stays white and the eyes
 * stay black, exactly as drawn — the whole colour model is one value. The
 * authored background colour in each source file is discarded; the chosen `bg`
 * is painted in its place.
 *
 * Rendered as an `<img>` carrying a data URI, for the two reasons `CrewAvatar`
 * documents: no `dangerouslySetInnerHTML`, and a data URI is its own document so
 * several on one page cannot collide on internal ids.
 */

// `?raw` imports — Vite hands back the file's text. Ordered pose-1..11 so
// `POSE_IDS` below reads in the same order the picker shows them.
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import pose1 from '../assets/avatarPoses/pose-1.svg?raw'
import pose2 from '../assets/avatarPoses/pose-2.svg?raw'
import pose3 from '../assets/avatarPoses/pose-3.svg?raw'
import pose4 from '../assets/avatarPoses/pose-4.svg?raw'
import pose5 from '../assets/avatarPoses/pose-5.svg?raw'
import pose6 from '../assets/avatarPoses/pose-6.svg?raw'
import pose7 from '../assets/avatarPoses/pose-7.svg?raw'
import pose8 from '../assets/avatarPoses/pose-8.svg?raw'
import pose9 from '../assets/avatarPoses/pose-9.svg?raw'
import pose10 from '../assets/avatarPoses/pose-10.svg?raw'
import pose11 from '../assets/avatarPoses/pose-11.svg?raw'

import { createAvatar } from '@dicebear/core'
import {
  BODY_MOTION,
  BODY_ORIGINS,
  BODY_TILE,
  BRAND_PURPLE,
  SWAY_TEMPO,
  TILES,
  type WorkingIntensity,
} from './kiroGhostAvatar'

/** The icon background palette offered in the avatar builder (brand purple +
 *  the tile colours) — the set a name-seeded default colour is drawn from, so
 *  it is always one the user could pick by hand. */
export const ICON_BG_PALETTE: readonly string[] = [BRAND_PURPLE, ...TILES]

/** The pose id as stored on the record (`{kind:'icon', pose}`) → its raw SVG. */
const POSE_SVG: Record<string, string> = {
  'pose-1': pose1,
  'pose-2': pose2,
  'pose-3': pose3,
  'pose-4': pose4,
  'pose-5': pose5,
  'pose-6': pose6,
  'pose-7': pose7,
  'pose-8': pose8,
  'pose-9': pose9,
  'pose-10': pose10,
  'pose-11': pose11,
}

/** The pose ids, in picker order. The first is the default a fresh icon draft
 *  starts on. */
export const POSE_IDS: readonly string[] = [
  'pose-1',
  'pose-2',
  'pose-3',
  'pose-4',
  'pose-5',
  'pose-6',
  'pose-7',
  'pose-8',
  'pose-9',
  'pose-10',
  'pose-11',
]

/** The pose a junk or absent id falls back to — the same thing the record's
 *  reader resolves an unknown pose to, so preview === roster. */
export const DEFAULT_POSE = POSE_IDS[0]

/** Pins a stored background to a hex colour, mirroring the `tile` rule in
 *  `kiroGhostAvatar`/`_safe_avatar`: it is interpolated into SVG markup, so
 *  anything but `#rrggbb` is rejected rather than reaching the template. */
export const POSE_BG_RE = /^#[0-9a-f]{6}$/i

/** The fallback background when a record names none, or names junk. Brand
 *  purple — the colour the source art ships with — so a pose with no stored bg
 *  still reads as intended rather than as a missing value. */
export const DEFAULT_POSE_BG = '#9046ff'

/** Does the pose map have THIS pose as its own key? `Object.hasOwn`, not a
 *  truthy `POSE_SVG[pose]` lookup: the latter walks the prototype chain, so a
 *  pose id of `"constructor"` / `"toString"` / `"__proto__"` (all admitted by
 *  the backend's `_AVATAR_POSE_RE` charset) would read as a truthy inherited
 *  value and resolve to a function, which `poseDataUri` then calls `.replace`
 *  on and throws — a render crash behind only the app-shell boundary. The
 *  module's contract is "degrade, never crash", so an own-key test is the
 *  guard it already promises. */
const hasPose = (pose: string): boolean => Object.hasOwn(POSE_SVG, pose)

/** Resolve a stored pose id to one this build can draw. An unknown id (a pose a
 *  newer client added, or a prototype-key string) renders the default rather
 *  than nothing or a crash, the same forgiveness `ghostTraitsFrom` gives an
 *  unknown trait. */
export function resolvePose(pose: string | undefined): string {
  return pose && hasPose(pose) ? pose : DEFAULT_POSE
}

/**
 * The pose a FRESH icon draft starts on, derived from the crew's seed string
 * (its name) — the icon tier's parity with the ghost, whose fresh default is
 * likewise seeded from the name (`seededTraits(name)` in `CrewAvatarBuilder`).
 *
 * Without this a brand-new icon crew always opened on `pose-1`, so a roster of
 * un-customized icon crews read as identical faces — the "too samey" feedback
 * the ghost's name-seeding already avoids. The pose is drawn through DiceBear's
 * seeded `prng` — the SAME generation mechanism the ghost tier (`kiroGhost`)
 * uses — so the icon default is produced the way every other seeded avatar in
 * the app is, rather than through a bespoke hash. `prng.pick` over `POSE_IDS`
 * is deterministic in the seed, so the same crew always opens on the same pose
 * and two different names spread across the set.
 *
 * This is a DEFAULT only: it seeds the builder's initial draft, and the user's
 * explicit pick overrides it. An empty/whitespace name falls back to the first
 * pose rather than seeding an arbitrary one — a nameless crew has nothing to
 * vary on, and `DEFAULT_POSE` is the honest "no basis to seed" answer.
 */
export function seedIconPose(name: string | undefined): string {
  const seed = (name ?? '').trim()
  if (!seed) return DEFAULT_POSE
  // The pose/bg salt split keeps colour and pose from moving in lockstep, the
  // same separation the djb2 version expressed with a `:bg` suffix.
  return seededIconPick(seed, POSE_IDS, DEFAULT_POSE)
}

/**
 * Draw one entry from `choices` deterministically from `seed`, through
 * DiceBear's own seeded `prng` (the public `createAvatar` path, exactly how the
 * `kiroGhost` style is driven). The one-off style's `create` makes a single
 * `prng.pick` and hands it back via `extra`, so no avatar SVG is rendered and
 * no `@dicebear/core` internal is imported. `fallback` is returned if the pick
 * somehow misses (an empty `choices`), mirroring `prng.pick`'s own contract.
 */
function seededIconPick(seed: string, choices: readonly string[], fallback: string): string {
  const picks = [...choices]
  const { extra } = createAvatar(
    {
      // No `meta`: this style exists only to drive the seeded prng and hand the
      // pick back through `extra` — nothing is rendered, so it needs no creator
      // or title (both are optional on the DiceBear Style type).
      schema: {},
      create: ({ prng }) => ({
        attributes: { viewBox: '0 0 1 1' },
        body: '',
        extra: () => ({ picked: prng.pick(picks, fallback) }),
      }),
    },
    { seed },
  ).toJson()
  const picked = extra.picked
  return typeof picked === 'string' ? picked : fallback
}

/**
 * The background colour a FRESH icon default wears, derived from the name like
 * `seedIconPose` derives the pose — so the colour follows the name as the user
 * types, is stable for a given name, and spreads two names across the palette
 * instead of always opening on brand purple. It runs through the same DiceBear
 * seeded `prng`, salted with a `:bg` suffix so colour and pose are drawn from
 * independent streams and never move in lockstep. An empty/whitespace name
 * falls back to the brand default.
 *
 * This is a DEFAULT only: it seeds the create preview and the stored value
 * when the user never picks, and any explicit colour pick overrides it.
 */
export function seedIconBg(name: string | undefined): string {
  const seed = (name ?? '').trim()
  if (!seed) return DEFAULT_POSE_BG
  return seededIconPick(seed + ':bg', ICON_BG_PALETTE, DEFAULT_POSE_BG)
}

/** Resolve a stored background to a legal hex, or the default. */
export function resolvePoseBg(bg: string | undefined): string {
  return bg && POSE_BG_RE.test(bg) ? bg : DEFAULT_POSE_BG
}

/**
 * The authored `<rect …/>` background in a source pose, matched so its fill can
 * be replaced. Every shipped pose opens with exactly one full-tile rect; the
 * match is anchored to a `fill="…"` inside a `<rect>` so a stray rect elsewhere
 * (none ship today) could not be recoloured by accident.
 */
const RECT_FILL_RE = /(<rect\b[^>]*\bfill=")[^"]*(")/

/**
 * The one amplitude an icon moves at. The ghost scales its body motion down to
 * `subtle` in a dense roster because its eyes animate too and the two together
 * read as busy at 40px; a pose has no moving eyes, so the body is the whole
 * cue, and at the subtle amplitude it is too small to register as motion at
 * all. One amplitude, the legible one, on every surface.
 */
const ICON_WORKING: WorkingIntensity = 'full'

/**
 * Compose a pose + background into the still data URI the roster and the
 * builder both render. The silhouette and eyes are left exactly as drawn; only
 * the background rect's fill is swapped for the chosen colour.
 *
 * A pose whose source somehow lacks the expected rect still renders — the body
 * and eyes draw over whatever the file declares — rather than throwing on a
 * roster, which is the same "degrade, never crash" rule the trait readers hold.
 */
export function poseDataUri(pose: string | undefined, bg: string | undefined): string {
  const svg = POSE_SVG[resolvePose(pose)]
  const painted = svg.replace(RECT_FILL_RE, `$1${resolvePoseBg(bg)}$2`)
  return `data:image/svg+xml;utf8,${encodeURIComponent(painted)}`
}

/** The pose's background rect, matched whole so the moving layer can drop it. */
const RECT_RE = /<rect\b[^>]*\/>/

/**
 * The pose with its background tile removed: the white silhouette and the black
 * eyes on a transparent canvas, the layer a WORKING icon moves. Paths and
 * colours are byte-identical to the still frame's, so the moving face is the
 * still face. The tile is painted by `CrewAvatar` behind it and stays put, as
 * the ghost's tile does under its own body motion.
 */
export function poseBodyDataUri(pose: string | undefined): string {
  const body = POSE_SVG[resolvePose(pose)].replace(RECT_RE, '')
  return `data:image/svg+xml;utf8,${encodeURIComponent(body)}`
}

/** A length in the ghost's 1200-unit tile as a share of the avatar box, in
 *  hundredths. Unitless: the stylesheet multiplies it by `1%`. */
const pct = (units: number) => String(+((units / BODY_TILE) * 100).toFixed(4))

/**
 * The CSS custom properties `crewIconMotion.css` reads: the ghost's own
 * body-motion numbers at `ICON_WORKING`, with every offset and origin turned
 * from 1200-unit pixels into a unitless share of the avatar box, which the
 * stylesheet turns into a percentage. A percentage
 * scales with the box, so the 40px roster row and the large header move the
 * same way, and the tempo has one source with the ghost tier.
 */
export function iconMotionVars(): Record<string, string> {
  const m = BODY_MOTION[ICON_WORKING]
  return {
    '--crew-icon-fit-drop': pct(m.fitDrop),
    '--crew-icon-fit-scale': String(m.fitScale),
    '--crew-icon-rise': pct(-m.rise),
    '--crew-icon-sink': pct(m.sink),
    '--crew-icon-stretch-x': String(1 + m.stretch),
    '--crew-icon-stretch-y': String(1 - m.stretch),
    '--crew-icon-squash-x': String(1 - m.stretch / 2),
    '--crew-icon-squash-y': String(1 + m.stretch),
    '--crew-icon-sway-from': `${-m.sway}deg`,
    '--crew-icon-sway-to': `${m.sway}deg`,
    '--crew-icon-secs': `${m.secs}s`,
    '--crew-icon-sway-secs': `${+(m.secs * SWAY_TEMPO).toFixed(4)}s`,
    '--crew-icon-fit-origin-x': pct(BODY_ORIGINS.fit[0]),
    '--crew-icon-fit-origin-y': pct(BODY_ORIGINS.fit[1]),
    '--crew-icon-sway-origin-x': pct(BODY_ORIGINS.sway[0]),
    '--crew-icon-sway-origin-y': pct(BODY_ORIGINS.sway[1]),
    '--crew-icon-sq-origin-x': pct(BODY_ORIGINS.sq[0]),
    '--crew-icon-sq-origin-y': pct(BODY_ORIGINS.sq[1]),
  }
}

/** The seed a blank name falls back to, so an unnamed draft still previews a
 *  stable icon instead of the theme-accent default. */
export const BLANK_LOOK_SEED = 'crewmate'

/** The string a name seeds its icon from: the trimmed name, or the blank
 *  fallback for an empty one. */
export function lookSeed(name: string): string {
  return name.trim() || BLANK_LOOK_SEED
}

/**
 * The avatar a new crewmate gets when the user never opens the editor: an ICON
 * (a shipped pose over a solid background), seeded from the name — NEVER the
 * trait ghost that a missing avatar falls back to on render. This is the single
 * default every create path should apply (the dialog, the template-enroll tab),
 * so a crewmate born without an explicit pick is still an icon and never a
 * ghost the user did not choose (#18616).
 */
export function defaultLookFor(name: string): CrewAvatarOverride {
  const seed = lookSeed(name)
  return { kind: 'icon', pose: seedIconPose(seed), bg: seedIconBg(seed) }
}

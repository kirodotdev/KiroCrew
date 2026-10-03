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
 * Compose a pose + background into the data URI the roster and the builder
 * both render. The silhouette and eyes are left exactly as drawn; only the
 * background rect's fill is swapped for the chosen colour.
 *
 * A pose whose source somehow lacks the expected rect still renders — the body
 * and eyes draw over whatever the file declares — rather than throwing on a
 * roster, which is the same "degrade, never crash" rule the trait readers hold.
 */
export function poseDataUri(pose: string | undefined, bg: string | undefined): string {
  const svg = POSE_SVG[resolvePose(pose)]
  const colour = resolvePoseBg(bg)
  const painted = svg.replace(RECT_FILL_RE, `$1${colour}$2`)
  return `data:image/svg+xml;utf8,${encodeURIComponent(painted)}`
}

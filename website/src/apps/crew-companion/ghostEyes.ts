/**
 * Ghost eye geometry — the single source of truth for WHERE the default ghost's
 * eyes sit, shared by the live pet and by every static preview.
 *
 * The body SVGs are deliberately EYELESS: the pet draws its eyes as a live
 * overlay so they can track the cursor, blink and hold per-state offsets. The
 * consequence is that anything rendering the raw SVG (the avatar gallery card,
 * the per-state thumbnails in the pack detail view) shows a blank face unless it
 * draws the eyes too — which is exactly the bug this module exists to prevent
 * happening again in a third place.
 *
 * Coordinates are percentages of the padded pet box, taken from the Figma art.
 * They transfer unchanged to any square box that renders the SVG with
 * object-fit: contain, since the letterboxing is identical.
 */
import type { PetState, PetMood } from './types'

export type EyeSpec = { x: number; y: number; w: number; h: number }

export const GHOST_EYE_INK = '#1a1526'

export const GHOST_EYE_MAP: Record<string, EyeSpec[]> = {
  primary: [{ x: 54.33, y: 38.79, w: 8.98, h: 12.37 }, { x: 69.82, y: 38.79, w: 8.98, h: 12.37 }],
  /*
   * '2nd' and '4th' are pulled LEFT from the values the desktop app ships, and that
   * divergence is deliberate.
   *
   * Those numbers were authored against posed body footage, where each pose has its
   * own silhouette. The built-in ghost here has only ONE body (`kiro_idle.svg`,
   * byte-identical to the app's) — the pose changes the eyes and nothing else. So the
   * '4th' pair landed where the 4th pose's head USED to be: measured against the idle
   * art, its right eye's centre sat at 82.3% while the head's right edge is at 80.3%,
   * i.e. the whole eye hung outside the silhouette, straddling the black outline.
   * '2nd' was the milder version of the same thing, pressed against the edge.
   *
   * Each pair was slid horizontally — spacing and vertical position untouched — until
   * both eyes clear the outline with ~1.5% of margin, so the pose still reads as the
   * same glance direction. The three poses that already landed correctly are
   * unchanged. `GhostEyePoses.test.ts` pins this against the head bounds.
   */
  '2nd':   [{ x: 58.93, y: 33.77, w: 7.62, h: 10.9 },  { x: 71.99, y: 34.56, w: 7.62, h: 10.9 }],
  '3rd':   [{ x: 47.38, y: 33.7,  w: 6.46, h: 12.39 }, { x: 58.42, y: 32.69, w: 6.46, h: 12.39 }],
  '4th':   [{ x: 61.93, y: 40.37, w: 6.76, h: 13.95 }, { x: 73.46, y: 39.16, w: 6.76, h: 13.95 }],
  // Docked pose (Figma 10:335). Derived from the baked eye paths (Vector_2/3)
  // mapped through the 232.201×247.245 viewBox → 128px pet box (xMidYMid meet).
  docked:  [{ x: 46.4, y: 22.9, w: 9, h: 13 }, { x: 61.9, y: 22.6, w: 9, h: 13 }],
}

const MOOD_POSE: Record<string, string> = {
  happy: '4th', sleepy: 'primary', curious: '3rd', busy: '3rd', scared: '4th',
}

const STATE_POSE: Record<string, string> = {
  idle: 'primary', walking: '2nd', thinking: '3rd', working: '3rd',
  error: '4th', offline: 'primary', peeking: 'primary', peekThinking: '3rd',
}

export function ghostPoseFor(state: PetState, mood: PetMood): string {
  if (mood && mood !== 'neutral' && MOOD_POSE[mood]) return MOOD_POSE[mood]
  return STATE_POSE[state] || 'primary'
}

/** Pose for a bare state/mood key, as used by pack thumbnails. */
export function ghostPoseForKey(key: string): string {
  return STATE_POSE[key] || MOOD_POSE[key] || 'primary'
}

/**
 * Where the eyes sit for a reaction, in eye-span units (1 = the eye-to-eye gap).
 *
 * Measured from the mascot footage, not invented: frames were differenced against
 * that same video's own idle frames. Carried verbatim from the desktop app.
 *
 * SIGNS ARE MIRRORED relative to the footage. The filmed mascot faces LEFT; this
 * art faces RIGHT, so "toward the tail" is +x on film and −x here.
 *
 * These offsets are ART-relative — unlike the cursor gaze they are NOT negated when
 * the art is mirrored, so the eyes and the body tilt always move to the same side of
 * the face whichever way the companion faces.
 */
export function ghostEyeOffsetFor(anim?: string | null): { dx: number; dy: number } {
  switch (anim) {
    // Look — eyes ONLY: the film shows the gaze sliding across with the body lean
    // unchanged. That is the whole difference from Curious, which moves the body.
    case 'look':
      return { dx: -0.95, dy: 0 }
    // Curious — eyes go up AND toward the tilt. The sideways half is scaled back
    // because the mascot gets there partly by turning in 3D, which a single static
    // SVG cannot do; the full amount would slide the eyes off the head.
    case 'curious':
      return { dx: -0.85, dy: -0.55 }
    // Ponder — idle eyes sit 35.5% down / 32.3% across, ponder 20.8% / 41.6%: i.e.
    // 14.7% of body height higher and 9.3% of body width further back, which in
    // eye-span units is dy −0.79 / dx −0.42. Within the clip the eyes never move —
    // ponder is a body bob with the eyes parked up here.
    case 'ponder':
    case 'ponder-loop':
      return { dx: -0.42, dy: -0.79 }
    // Walking — NO extra offset. The walking pose ('2nd') already sits ~0.4 span
    // further forward than the resting pose, so adding more pushed the eyes out to
    // the edge of the head. The art is already doing the "looking where I'm going".
    case 'fly':
      return { dx: 0, dy: 0 }
    default:
      return { dx: 0, dy: 0 }
  }
}

/**
 * Reactions that POSE the eyes: cursor tracking is switched off for these so the
 * held expression stays where the footage puts it. idle, celebrate and error keep
 * the live resting behaviour instead.
 */
export const POSED_ANIMS = new Set(['look', 'curious', 'ponder', 'ponder-loop', 'fly'])

/**
 * One-off eye squashes, timed from the start of the reaction.
 *
 * Error opens on the mascot's flat wide "wince" dash, its signature error beat.
 * Curious squeezes the eyes narrow on the snap: the mascot TURNS to look, and for a
 * couple of frames the two eyes foreshorten into one bean — the squeeze stands in for
 * a 3D turn a flat SVG cannot do, and it is what makes the move land.
 *
 * No ponder entry on purpose: the clip blinks once mid-hold, but thinning the eye at
 * this size reads as a rendering glitch, so ponder keeps open eyes.
 */
export const EYE_BEATS: Record<string, { at: number; ms: number; sx: number; sy: number }> = {
  error: { at: 0, ms: 220, sx: 1.2, sy: 0.22 },
  curious: { at: 90, ms: 170, sx: 0.45, sy: 1.04 },
}

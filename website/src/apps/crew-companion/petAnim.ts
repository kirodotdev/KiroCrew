/**
 * petAnim — WHICH body motion the companion is playing, as one pure decision.
 *
 * Ported from the desktop app's `activeAnim` chain in PetWidget. The order is the
 * behaviour, not a detail: a companion that is both walking and curious cocks its
 * head rather than floating, because the expression is the more informative of the
 * two. Extracted as a pure function (the same split as walkMath / bubbleLayout) so
 * the precedence can be unit-tested without React or a DOM.
 *
 * Two things the source gates on `docked` and this keeps: a companion tucked against
 * a screen edge goes QUIET — it does not shake, cock its head or float, because those
 * motions would push a half-off-screen body around. A celebration is deliberately NOT
 * gated: finishing a job is worth a hop even from the edge.
 *
 * `ponder-loop` sits LAST and is this build's own addition (the desktop app's live pet
 * had no busy motion; only its previews did). It therefore never displaces one of the
 * ported reactions.
 */

/** A body motion, matching the `.kg-anim-*` class suffixes in petMotion.css. */
export type PetAnim =
  | 'error'
  | 'celebrate'
  | 'curious'
  | 'fly'
  | 'ponder-loop'
  | 'look'
  | null

export interface AnimInputs {
  /** The display state being held: idle / loading / done / error / breathing phase. */
  state: string
  /** Current mood. `curious` carries a body move of its own, not just an eye pose. */
  mood?: string
  /** Tucked against a screen edge. */
  docked?: boolean
  /** Travelling — a walk leg or an idle hop both count. */
  walking?: boolean
}

/**
 * Priority: error > celebrate > curious > fly > ponder.
 *
 * Breathing phases fall through to `null` on purpose: the breathing overlay drives
 * the scale itself, and a second animation would fight the phase timing.
 */
/**
 * How long the celebrate hop runs. Matches the `kg-celebrate` keyframe exactly —
 * the keyframe was traced from the mascot footage, so this is a measurement.
 */
export const CELEBRATE_MS = 900

/**
 * Extra time a celebrate prop is worn after the hop ends.
 *
 * Without it the prop vanishes mid-bounce: the hop's last frames are the settle,
 * and a party hat disappearing on the way down reads as a glitch.
 */
export const CELEBRATE_PROP_HOLD_MS = 450

export function activeAnimFor({ state, mood, docked = false, walking = false }: AnimInputs): PetAnim {
  if (state === 'error' && !docked) return 'error'
  if (state === 'done') return 'celebrate'
  /*
   * A happy mood hops too.
   *
   * The desktop app drives the celebrate hop from `mood === 'happy'`, not from a
   * display state — so anything that sets the mood (a completion, a notification
   * reaction) makes the companion bounce. This port only checked `state === 'done'`,
   * so every other happy path was silent: the face changed and the body did not,
   * which is the same "unmoved by its own notifications" gap the eye-pose fix
   * closed on the other side.
   */
  if (mood === 'happy' && !docked) return 'celebrate'
  if (mood === 'curious' && !docked) return 'curious'
  if (walking && !docked) return 'fly'
  if (state === 'loading' && !docked) return 'ponder-loop'
  return null
}

/** The stylesheet class for a motion, or undefined when the companion is still. */
export function animClassFor(anim: PetAnim): string | undefined {
  return anim ? `kg-anim-${anim}` : undefined
}

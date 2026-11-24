/**
 * A tray's press and its standing still under a pointer (D192). `Dash3d.tsx` keeps a down on a tray until the up
 * decides; `Crew.tsx` holds a Bot's idle drift still while its resting tray is under a pointer, so the tray it carries
 * holds still with it.
 */
import type { PinchKind } from './pinchRoute'

/**
 * The Bots whose resting tray a pointer lies on now, or keeps a down on. `Dash3d.tsx` writes it each frame; the
 * Crew reads it in its own frame (one frame later at most). One room is drawn at a time, as `pinchRoute` is one.
 */
export const trayHeld = new Set<string>()

/** Whether a tray stands still now: under a pointer or a kept down, at rest at a Bot that neither walks nor is carried. */
export function trayHolds(at: { pointed: boolean; down: boolean; resting: boolean; carried: boolean; walking: boolean }): boolean {
  return (at.pointed || at.down) && at.resting && !at.carried && !at.walking
}

/**
 * Whether an up on a tray takes the press kept on it. Only the up of the pointer that went down, on a drawn piece of
 * this same tray (its handler is this tray's), and not a drag. A leave between the two decides nothing.
 */
export function upTakes(down: { id: number; pinch?: number | null } | null, at: { pointerId: number; drawn: boolean; ending: PinchKind | null; pinch?: number | null }): boolean {
  // A kept down belongs to the pinch that made it (REVIEW-73 M244): a later pinch's up takes nothing without its own
  // down. In the flat preview there is no pinch (`null`).
  return down?.id === at.pointerId && at.drawn && at.ending !== 'drag' && (down.pinch ?? null) === (at.pinch ?? null)
}

/**
 * Whether a down kept on a tray in a session is let go now, with nothing taken: the pinch is over and its up came to
 * something else (no pinch owns the button any more), a later pinch began (`stale`: the other hand pinched while the
 * first one's fingers were open, so no frame saw no pinch, M244), the quiet time began, or the session is not visible.
 */
export function downLetGo(at: { pinching: boolean; quiet: boolean; visible: boolean; stale?: boolean }): boolean {
  return !at.pinching || !!at.stale || at.quiet || !at.visible
}

/** How long a Bot's drift takes to come back to its pace once its tray is let go (ms): its place moves on with no jump. */
export const DRIFT_EASE_MS = 300

/**
 * A Bot's own drift clock, which stands while it is held still (`still`) and comes back to the wall clock's pace over
 * `DRIFT_EASE_MS` after. `t` is the drift's time (ms) to read the drift at; `rate` its pace now, 0 to 1.
 */
export function driftStep(clock: { t: number; rate: number }, still: boolean, dtMs: number): { t: number; rate: number } {
  const rate = still ? 0 : clock.rate + (1 - clock.rate) * Math.min(1, Math.max(0, dtMs) / DRIFT_EASE_MS)
  return { t: clock.t + Math.max(0, dtMs) * rate, rate }
}

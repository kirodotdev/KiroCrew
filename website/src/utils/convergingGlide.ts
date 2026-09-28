import { CONVERGE_MAX_MS, claimScrollOwnership, pollRowSettled } from './searchScroll'

/**
 * A programmatic scroll that is SMOOTH and lands EXACTLY, on a scroller whose
 * geometry is still moving while it scrolls — the pinned-prompt jump.
 *
 * Two facts make a native `scrollTo({ behavior: 'smooth' })` the wrong tool:
 *
 * - Any other `scrollTop` write cancels a native animation where it stands, and
 *   writes DO land mid-flight: the virtualizer's anchor compensation as rows
 *   above the viewport measure in, its height-sync compensation, a re-measuring
 *   row. Each strands the scroll part-way — the "moved a few hundred pixels and
 *   stopped" report.
 * - The destination itself moves. Rows mount and measure as the viewport passes
 *   them, images load, and the banner the landing must clear swaps the moment
 *   the target un-pins — a swap CAUSED by the landing write, so it is only
 *   visible on the frame after. A single computed destination is therefore
 *   stale by the time it is reached.
 *
 * So the glide owns every frame's write (uncancellable by other writers) and
 * re-derives its destination from live geometry every frame. It runs in two
 * phases:
 *
 * 1. TRAVEL — an eased interpolation from the starting position to the LIVE
 *    goal, over `durationMs`, driven by this module's own frame loop. Because
 *    the goal is re-read each frame, drift during travel is folded into the
 *    remaining motion — for as long as there IS remaining motion to fold it
 *    into. Late in an ease-out there is almost none, so a destination that moves
 *    there arrives whole in a single frame, against the direction of travel.
 *    `GLIDE_MAX_BACK_STEP_PX` bounds what one frame may give back, and travel
 *    runs on for a few frames to finish anything over that bound. The final
 *    travel frame lands on the goal as read that frame.
 * 2. CONVERGE — `pollRowSettled` (utils/searchScroll), with the live goal as
 *    the measured quantity and "write the goal" as the step: keep writing the
 *    live goal until it has held still for `quietMs` (and at least two frames),
 *    or the backstop expires. This is what catches the post-landing shifts: the
 *    banner swap, a late image, a row that measured after the last travel
 *    frame — exactly the readings that the landing write itself invalidates.
 *    The quiet window is timed from the first converge frame, so a goal that
 *    moves on the frame after landing simply restarts it.
 *
 * The glide is a scroll owner (`activeScrollOwner` in searchScroll) from its
 * FIRST frame: `runConvergingGlide` claims ownership before scheduling travel,
 * so starting a glide retires a running search-match poll, and a search poll
 * started at any point — mid-travel or during convergence — retires the glide
 * (its `onEnd` reports `cancelled` and the queued frame is dropped). That is
 * the intended mutual exclusion — both drive the same scroller, two writers per
 * frame fight, and the LATER intent must win. Convergence keeps the claim by
 * handing it to `pollRowSettled`: the travel-phase claim is released right
 * before the poll claims, so the poll finds no owner to retire and the glide
 * never supersedes itself.
 *
 * End reasons: `lost` is reachable only during travel (a null goal there has
 * nothing to interpolate toward). During convergence a null goal is an absent
 * target to the poll, which idles and ends with `timeout` at the backstop.
 *
 * Reduced motion skips travel, not convergence: the reader asked for no eased
 * motion, not for an inexact landing.
 *
 * Pure: clock, frame scheduler and every DOM read/write are injected, so the
 * two-phase contract is unit-testable without a live scroller.
 */

/** Shortest travel, for a jump within a couple of viewports. */
export const GLIDE_MIN_MS = 450
/** Longest travel: a jump across tens of thousands of pixels still reads as a
 *  scroll rather than a blur, without making the reader wait on it. */
export const GLIDE_MAX_MS = 900
/** Travel speed that scales the duration between the two bounds. */
export const GLIDE_PX_PER_MS = 24

/**
 * Time a converging glide holds still before it is settled. Long enough to
 * outlast the banner swap and a row measuring on the frame after landing (both
 * one or two frames), short enough that the reader never notices the hold.
 * Deliberately shorter than `MIN_QUIET_MS`: that window waits out a widget
 * iframe build for a jump INTO a widget, and a pinned prompt is never one.
 */
export const GLIDE_QUIET_MS = 250

/**
 * Largest single-frame move AGAINST the direction of travel that the glide will
 * write.
 *
 * Travel interpolates toward a LIVE goal, so when the goal is refined the write
 * moves by the refinement. Most refinements are small and the remaining eased
 * motion swallows them — the reader sees a slowdown. One is not small: on a far
 * jump the goal is the height-index estimate until the target row mounts, and
 * the row mounts near the end of the ease, where the estimate's whole error
 * arrives at once and there is no remaining motion left to swallow it.
 *
 * The number is the point where the glide's own speed stops covering the
 * correction. Interpolating an `estimateRowTop` error of 86px over a 24 500px
 * travel, the correction and the frame's forward motion cross at 46.5px: a
 * correction up to that size is smaller than the step the glide was already
 * taking, so it reads as a slowdown and travel writes it unchanged. Past it the
 * correction would be a visible lurch backwards, so it is paid down at HALF this
 * budget per frame until what remains is within that slice — not merely within
 * the budget, which would leave a final frame of up to a whole budget landing at
 * tail speed. So a correction travel does not write whole is never written
 * faster than half of this.
 */
export const GLIDE_MAX_BACK_STEP_PX = 47

/**
 * Frames travel may run past `durationMs` to finish paying down a correction.
 * At half the budget each, this covers a correction of ~190px, far beyond the
 * height-index error a far jump produces. A goal that keeps receding faster
 * than the pay-down exhausts this instead of extending travel without bound,
 * and the remainder is left to the CONVERGE phase, which exists for a goal that
 * is still moving.
 */
export const GLIDE_BACK_CATCHUP_FRAMES = 8

/** Travel duration for a jump of `distancePx`, clamped to the bounds above. */
export function glideDurationMs(distancePx: number): number {
  const byDistance = Math.abs(distancePx) / GLIDE_PX_PER_MS
  return Math.min(GLIDE_MAX_MS, Math.max(GLIDE_MIN_MS, Math.round(byDistance)))
}

export type GlideEnd = 'settled' | 'timeout' | 'cancelled' | 'lost'

export interface ConvergingGlideDeps {
  /**
   * Live destination in scroller `scrollTop` pixels, or `null` when it cannot be
   * derived this frame (the target row is gone and no estimate exists). A null
   * goal during travel ends the glide with reason `lost`; during convergence
   * the poll waits for it and ends with `timeout` at the backstop.
   */
  goal: () => number | null
  /** Current `scrollTop`. */
  read: () => number
  /** Write `scrollTop`. Called at most once per frame. */
  write: (top: number) => void
  /** Travel duration; see `glideDurationMs`. Ignored when `reduced`. */
  durationMs: number
  /** Skip the eased travel (prefers-reduced-motion). Convergence still runs. */
  reduced?: boolean
  /** Quiet window before the goal counts as settled. Default `GLIDE_QUIET_MS`. */
  quietMs?: number
  /** Wall-clock backstop for the CONVERGE phase, timed from its start. */
  convergeMaxMs?: number
  now?: () => number
  raf?: (cb: () => void) => number
  cancelRaf?: (id: number) => void
  onEnd?: (reason: GlideEnd) => void
}

const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3)

/**
 * Start a converging glide. Returns a cancel function — idempotent, and a no-op
 * after the glide has ended on its own.
 */
export function runConvergingGlide(deps: ConvergingGlideDeps): () => void {
  const {
    goal,
    read,
    write,
    durationMs,
    reduced = false,
    quietMs = GLIDE_QUIET_MS,
    convergeMaxMs = CONVERGE_MAX_MS,
    now = () => performance.now(),
    raf = (cb) => requestAnimationFrame(cb),
    cancelRaf = (id) => cancelAnimationFrame(id),
  } = deps
  const t0 = now()
  const from = read()
  let done = false
  let frameId = 0
  let cancelConverge: (() => void) | null = null
  let releaseOwnership: () => void = () => {}
  // Every frame — travel's own and the poll's — is scheduled through here so
  // `finish` can drop whichever one is queued; the poll has no cancelRaf of its
  // own.
  const schedule = (cb: () => void) => (frameId = raf(cb))
  const finish = (reason: GlideEnd) => {
    if (done) return
    done = true
    cancelRaf(frameId)
    releaseOwnership()
    cancelConverge?.()
    deps.onEnd?.(reason)
  }
  // Travel writes `scrollTop` every frame without a poll, so the glide holds the
  // scroll-ownership claim itself for that phase: a search poll started
  // mid-travel supersedes the glide here, ending it and dropping its queued
  // frame instead of running two writers per frame until convergence.
  releaseOwnership = claimScrollOwnership(() => finish('cancelled'))
  const converge = () => {
    // Hand the claim to the poll rather than stacking the two: the poll claims
    // on start and retires whatever owner it finds, and that owner would be
    // this glide. Releasing first is simpler than teaching the supersede
    // callback to recognise its own poll — the release is a compare-and-clear,
    // so a later owner is never disturbed, and the poll's `onEnd` still ends
    // the glide when a search poll supersedes it during convergence.
    releaseOwnership()
    // The goal read by `measure` is the value `step` writes on the same frame:
    // one read per frame, and the compared quantity is the written one.
    let g: number | null = null
    cancelConverge = pollRowSettled({
      measure: () => (g = goal()),
      step: () => { if (g != null) write(g) },
      settleFrames: 2,
      minQuietMs: quietMs,
      maxMs: convergeMaxMs,
      raf: schedule,
      now,
      onEnd: finish,
    })
  }
  let pos = from
  let dir = 0
  let paying = false
  let catchup = 0
  const travel = () => {
    if (done) return
    const g = goal()
    if (g == null) return finish('lost')
    const t = reduced ? 1 : Math.min(1, (now() - t0) / durationMs)
    // Where the ease says this frame belongs, against the goal as it reads now:
    // a live endpoint, so a destination that moves is folded into the motion
    // that is left. The final frame's nominal IS the goal.
    const nominal = t < 1 ? from + (g - from) * easeOutCubic(t) : g
    if (dir === 0) dir = Math.sign(g - from)
    const step = nominal - pos
    // A step against the travel direction is a refinement of the destination
    // arriving after the glide has already passed it. Within the budget it is
    // smaller than the motion the glide was making, so it reads as a slowdown
    // and goes through untouched. Over the budget it would be a lurch, so it is
    // paid a slice at a time — and once a pay-down is under way the test tightens
    // to the slice, because dropping back to the budget here would dump whatever
    // is left, up to a whole budget, into one frame at tail speed: the same lurch
    // one size smaller.
    const slice = GLIDE_MAX_BACK_STEP_PX / 2
    const lurching = dir !== 0
      && Math.sign(step) === -dir
      && Math.abs(step) > (paying ? slice : GLIDE_MAX_BACK_STEP_PX)
    paying = lurching
    pos = lurching ? pos - dir * slice : nominal
    write(pos)
    if (t < 1) {
      schedule(travel)
      return
    }
    // Travel is over on the clock, but a correction still outstanding would
    // otherwise be handed to convergence as the single lurch this budget
    // exists to prevent.
    if (lurching && catchup++ < GLIDE_BACK_CATCHUP_FRAMES) {
      schedule(travel)
      return
    }
    converge()
  }
  schedule(travel)
  return () => finish('cancelled')
}

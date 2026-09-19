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
 *    the goal is re-read each frame, drift during travel is absorbed into the
 *    remaining motion rather than left over. The final travel frame lands on
 *    the goal as read that frame.
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
  const travel = () => {
    if (done) return
    const g = goal()
    if (g == null) return finish('lost')
    const t = reduced ? 1 : Math.min(1, (now() - t0) / durationMs)
    // The goal is live, so the interpolation's endpoint moves with it and the
    // residual is folded into the remaining motion; the last frame lands on it.
    write(t < 1 ? from + (g - from) * easeOutCubic(t) : g)
    if (t < 1) {
      schedule(travel)
      return
    }
    converge()
  }
  schedule(travel)
  return () => finish('cancelled')
}

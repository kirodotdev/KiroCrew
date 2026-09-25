import { describe, expect, it } from 'vitest'
import {
  GLIDE_BACK_CATCHUP_FRAMES,
  GLIDE_MAX_BACK_STEP_PX,
  GLIDE_MAX_MS,
  GLIDE_MIN_MS,
  GLIDE_PX_PER_MS,
  GLIDE_QUIET_MS,
  glideDurationMs,
  runConvergingGlide,
  type GlideEnd,
} from '../utils/convergingGlide'
import { pollRowSettled } from '../utils/searchScroll'

/**
 * Deterministic harness: an injected clock and frame queue, a scroller that is
 * just a number, and a goal the test moves at will. Every assertion here is a
 * behaviour a live scroller could only show under a real layout engine.
 */
function harness(opts: {
  goal: () => number | null
  durationMs?: number
  reduced?: boolean
  quietMs?: number
  convergeMaxMs?: number
  from?: number
}) {
  let clock = 0
  const frames: { id: number; cb: () => void }[] = []
  let nextId = 1
  let scrollTop = opts.from ?? 0
  const writes: number[] = []
  let ended: GlideEnd | null = null
  const cancel = runConvergingGlide({
    goal: opts.goal,
    read: () => scrollTop,
    write: (top) => { scrollTop = top; writes.push(top) },
    durationMs: opts.durationMs ?? 400,
    reduced: opts.reduced,
    quietMs: opts.quietMs,
    convergeMaxMs: opts.convergeMaxMs,
    now: () => clock,
    raf: (cb) => { const id = nextId++; frames.push({ id, cb }); return id },
    cancelRaf: (id) => { const i = frames.findIndex(f => f.id === id); if (i >= 0) frames.splice(i, 1) },
    onEnd: (reason) => { ended = reason },
  })
  const flush = (at: number) => {
    const f = frames.shift()
    if (!f) throw new Error(`no frame queued at ${at}`)
    clock = at
    f.cb()
  }
  return {
    flush,
    cancel,
    get scrollTop() { return scrollTop },
    get writes() { return writes },
    get pending() { return frames.length },
    get ended() { return ended },
  }
}

describe('glideDurationMs', () => {
  it('floors at GLIDE_MIN_MS for a short hop', () => {
    expect(glideDurationMs(0)).toBe(GLIDE_MIN_MS)
    expect(glideDurationMs(GLIDE_MIN_MS * GLIDE_PX_PER_MS - 1)).toBe(GLIDE_MIN_MS)
  })

  it('scales with distance and caps at GLIDE_MAX_MS', () => {
    const mid = (GLIDE_MIN_MS + GLIDE_MAX_MS) / 2
    expect(glideDurationMs(mid * GLIDE_PX_PER_MS)).toBe(mid)
    expect(glideDurationMs(10 * GLIDE_MAX_MS * GLIDE_PX_PER_MS)).toBe(GLIDE_MAX_MS)
  })

  it('is direction-agnostic', () => {
    expect(glideDurationMs(-20000)).toBe(glideDurationMs(20000))
  })
})

describe('runConvergingGlide', () => {
  it('travels through intermediate positions and lands on the goal', () => {
    const h = harness({ goal: () => 1000, durationMs: 400 })
    h.flush(0)
    expect(h.scrollTop).toBe(0)
    h.flush(200)
    // easeOutCubic(0.5) = 0.875
    expect(h.scrollTop).toBeCloseTo(875, 5)
    h.flush(400)
    expect(h.scrollTop).toBe(1000)
    // Every write moved forward: a glide, never a teleport.
    for (let i = 1; i < h.writes.length; i++) expect(h.writes[i]).toBeGreaterThanOrEqual(h.writes[i - 1])
  })

  it('keeps converging after travel and lands on a goal that moved AFTER the last travel frame', () => {
    // The banner-swap case: the landing write itself changes the destination,
    // which is only visible on the following frame. Travel alone stops at the
    // goal as it read on its final frame — the half-way landing this guards.
    let goal = 1000
    const h = harness({ goal: () => goal, durationMs: 400 })
    h.flush(0)
    h.flush(400)
    expect(h.scrollTop).toBe(1000)
    expect(h.pending).toBe(1)
    goal = 940
    h.flush(416)
    expect(h.scrollTop).toBe(940)
    h.flush(432)
    h.flush(448)
    expect(h.ended).toBeNull()
    h.flush(416 + GLIDE_QUIET_MS)
    expect(h.scrollTop).toBe(940)
    expect(h.ended).toBe('settled')
    expect(h.pending).toBe(0)
  })

  it('requires the goal to hold still for the quiet window, not just two equal frames', () => {
    const h = harness({ goal: () => 500, durationMs: 100, quietMs: 250 })
    h.flush(0)
    h.flush(100)
    h.flush(116)
    h.flush(132)
    h.flush(148)
    // Three equal converge frames, but only 32ms of quiet: not settled yet.
    expect(h.ended).toBeNull()
    // The window is timed from the first converge frame, not the landing.
    h.flush(116 + 250 - 1)
    expect(h.ended).toBeNull()
    h.flush(116 + 250)
    expect(h.ended).toBe('settled')
  })

  it('folds a goal that moves DURING travel into the remaining motion', () => {
    let goal = 1000
    const h = harness({ goal: () => goal, durationMs: 400 })
    h.flush(0)
    h.flush(200)
    goal = 2000
    h.flush(400)
    expect(h.scrollTop).toBe(2000)
  })

  // The far pinned-prompt jump: the anchor row is not mounted, so the goal is
  // the height-index estimate until the row measures in. The estimate is long by
  // `LATE_RESIDUAL_PX`, so the correction has to land — the row really is that
  // far from where the height index guessed, and by the time it mounts the
  // glide has already travelled past it. It is a glide, so no single frame may
  // lurch against the direction of travel faster than the glide was moving that
  // frame, and that holds WHEREVER in the ease the row happens to mount: late in
  // an ease-out the per-frame motion is vanishing, which is exactly where a
  // whole residual dropped in one frame is most visible.
  const LATE_RESIDUAL_PX = 86
  const LATE_DURATION_MS = 937
  const LATE_FROM_PX = 24500

  function measureLateHandoff(handoffAtMs: number, residualPx = LATE_RESIDUAL_PX) {
    let clock = 0
    const h = harness({
      goal: () => (clock >= handoffAtMs ? residualPx : 0),
      durationMs: LATE_DURATION_MS,
      from: LATE_FROM_PX,
    })
    let atDeadline: number | null = null
    for (let at = 0; at <= LATE_DURATION_MS + 600; at += 1000 / 60) {
      if (h.pending === 0) break
      clock = at
      h.flush(at)
      if (atDeadline == null && at >= LATE_DURATION_MS) atDeadline = h.scrollTop
    }
    const w = h.writes
    // Travel descends, so a step that INCREASES scrollTop moves backward. The
    // speed a backward step is judged against is the last FORWARD step before
    // it: once a correction is under way every step is backward, and comparing
    // one backward step with the previous backward step compares a quantity
    // with itself.
    let worstBack = 0
    let speedThen = 0
    let lastFwd = 0
    let backFrames = 0
    let clampedFrames = 0
    for (let i = 1; i < w.length; i++) {
      const step = w[i] - w[i - 1]
      if (step < 0) { lastFwd = -step; continue }
      if (step > 0) backFrames++
      if (Math.abs(step - GLIDE_MAX_BACK_STEP_PX / 2) < 1e-6) clampedFrames++
      if (step > worstBack) { worstBack = step; speedThen = lastFwd }
    }
    return { worstBack, speedThen, backFrames, clampedFrames, atDeadline, landed: w[w.length - 1] }
  }

  // Wherever the row mounts, the correction lands and no frame lurches back by
  // more than the budget.
  for (const handoffAtMs of [600, 750, 780, 821, 880, 930, 940]) {
    it(`pays a ${handoffAtMs}ms goal move down within the backward budget`, () => {
      const m = measureLateHandoff(handoffAtMs)
      expect(m.landed).toBe(LATE_RESIDUAL_PX)
      expect(m.worstBack).toBeLessThanOrEqual(GLIDE_MAX_BACK_STEP_PX)
    })
  }

  // Past the crossover the correction no longer fits in one frame, so a pay-down
  // runs — and then the budget is not the bound that matters. A pay-down that
  // stopped as soon as what remained fit inside the BUDGET would write that
  // remainder, up to a whole budget, in one frame at tail speed: the same lurch
  // one size smaller. It runs until what remains fits inside the SLICE, so no
  // frame of a paid-down correction exceeds half the budget.
  for (const handoffAtMs of [821, 880, 930, 940]) {
    it(`holds a paid-down ${handoffAtMs}ms correction to the slice, not the budget`, () => {
      const m = measureLateHandoff(handoffAtMs)
      expect(m.landed).toBe(LATE_RESIDUAL_PX)
      expect(m.worstBack).toBeLessThanOrEqual(GLIDE_MAX_BACK_STEP_PX / 2)
    })
  }

  // Below the budget the stronger guarantee holds and is kept: the correction is
  // smaller than the step the glide was already taking, so the reader sees the
  // travel slow rather than reverse. This cannot hold once the ease has decayed
  // past the crossover — reaching a destination the glide has passed requires
  // backward motion, and there the budget above is the whole guarantee.
  for (const handoffAtMs of [600, 750, 780]) {
    it(`keeps a ${handoffAtMs}ms goal move under the speed the glide was making`, () => {
      const m = measureLateHandoff(handoffAtMs)
      expect(m.worstBack).toBeLessThanOrEqual(m.speedThen)
    })
  }

  // `GLIDE_MAX_BACK_STEP_PX` is not a taste setting, and this recomputes where it
  // comes from so the basis is executable rather than a number measured once.
  // Walking the mount time forward, the correction grows while the frame's own
  // motion decays. The widest correction travel still writes whole is the last
  // one before the two cross, and the budget is that reading at whole-pixel
  // resolution. Past the crossing the pay-down takes over, which shows up as the
  // observed backward step DROPPING — the correction itself only ever grows —
  // so that drop is where the scan stops.
  it('derives the backward budget from the widest correction the ease still covers', () => {
    let widest = 0
    let widestSpeed = 0
    let previous = 0
    for (let at = 600; at <= 940; at += 5) {
      const m = measureLateHandoff(at)
      if (m.worstBack < previous) break
      previous = m.worstBack
      if (m.worstBack > widest) { widest = m.worstBack; widestSpeed = m.speedThen }
    }
    // It is still under the motion the glide was making, so it reads as the
    // travel slowing rather than reversing.
    expect(widest).toBeLessThanOrEqual(widestSpeed)
    // And the budget is that reading, rounded up to a whole pixel.
    expect(widest).toBeLessThanOrEqual(GLIDE_MAX_BACK_STEP_PX)
    expect(widest).toBeGreaterThan(GLIDE_MAX_BACK_STEP_PX - 1)
  })

  it('stops paying down after the catch-up frames and leaves the rest to convergence', () => {
    // A correction far larger than the catch-up can cover: at half the budget a
    // frame, finishing it would take a dozen frames more than travel is allowed,
    // whatever the cap is set to. Travel must not keep extending itself for a
    // destination that has moved this far — CONVERGE exists for a goal that is
    // still moving — so the extension is capped and the remainder handed over.
    const huge = (GLIDE_BACK_CATCHUP_FRAMES + 12) * (GLIDE_MAX_BACK_STEP_PX / 2)
    const m = measureLateHandoff(940, huge)
    // Every clamped frame is one travel wrote. The first is travel's own last
    // frame on the clock; the rest are the extension, which the cap bounds.
    expect(m.clampedFrames - 1).toBe(GLIDE_BACK_CATCHUP_FRAMES)
    // The landing is still exact: convergence finishes what the cap cut off.
    expect(m.landed).toBe(huge)
  })

  it('absorbs a correction inside the budget in ONE frame and pays a larger one down over several', () => {
    // A correction the remaining ease can absorb is written whole, on the frame
    // it arrives: the reader sees the travel slow, and travel still lands on its
    // own clock. Starting a pay-down here would put backward drift into a jump
    // that does not need it.
    const absorbed = measureLateHandoff(780)
    expect(absorbed.backFrames).toBe(1)
    // The whole 46.5px correction, in that one frame — not a paid-down slice of
    // it. This is the largest backward frame travel writes without paying down,
    // and it is the reading the budget above is derived from.
    expect(absorbed.worstBack).toBeCloseTo(46.5, 1)
    expect(absorbed.atDeadline).toBe(LATE_RESIDUAL_PX)
    // One the ease cannot absorb is spread, so it takes more than one frame and
    // is still outstanding when the clock runs out.
    const paid = measureLateHandoff(930)
    expect(paid.backFrames).toBeGreaterThan(1)
    expect(paid.atDeadline).not.toBe(LATE_RESIDUAL_PX)
  })

  it('under reduced motion skips travel but still converges', () => {
    let goal = 1000
    const h = harness({ goal: () => goal, durationMs: 400, reduced: true })
    h.flush(0)
    expect(h.scrollTop).toBe(1000)
    expect(h.pending).toBe(1)
    goal = 950
    h.flush(16)
    expect(h.scrollTop).toBe(950)
    h.flush(32)
    h.flush(16 + GLIDE_QUIET_MS)
    expect(h.ended).toBe('settled')
  })

  it('gives up at the converge backstop when the goal never holds still', () => {
    let n = 0
    const h = harness({ goal: () => 1000 + (n++ * 10), durationMs: 100, convergeMaxMs: 2000 })
    h.flush(0)
    h.flush(100)
    let t = 100
    while (h.ended == null) {
      t += 16
      h.flush(t)
      if (t > 5000) throw new Error('backstop did not fire')
    }
    expect(h.ended).toBe('timeout')
    expect(t).toBeGreaterThanOrEqual(2100)
    expect(t).toBeLessThan(2200)
    expect(h.pending).toBe(0)
  })

  it('ends with `lost` when the goal cannot be derived during travel', () => {
    let goal: number | null = 1000
    const h = harness({ goal: () => goal, durationMs: 400 })
    h.flush(0)
    goal = null
    h.flush(200)
    expect(h.ended).toBe('lost')
    expect(h.pending).toBe(0)
  })

  it('a goal that vanishes DURING convergence waits for the backstop and ends with `timeout`', () => {
    // The poll treats a null measurement as an absent target: no write, but the
    // wall clock keeps running so the glide still terminates.
    let goal: number | null = 1000
    const h = harness({ goal: () => goal, durationMs: 100, convergeMaxMs: 2000 })
    h.flush(0)
    h.flush(100)
    goal = null
    const writesAtLanding = h.writes.length
    let t = 100
    while (h.ended == null) {
      t += 16
      h.flush(t)
      if (t > 5000) throw new Error('backstop did not fire')
    }
    expect(h.ended).toBe('timeout')
    expect(h.writes.length).toBe(writesAtLanding)
    expect(h.pending).toBe(0)
  })

  it('cancel stops the loop, drops the queued frame and is idempotent', () => {
    const h = harness({ goal: () => 1000, durationMs: 400 })
    h.flush(0)
    expect(h.pending).toBe(1)
    h.cancel()
    expect(h.ended).toBe('cancelled')
    expect(h.pending).toBe(0)
    h.cancel()
    expect(h.ended).toBe('cancelled')
  })

  it('cancel during convergence drops the queued poll frame', () => {
    const h = harness({ goal: () => 1000, durationMs: 100 })
    h.flush(0)
    h.flush(100)
    h.flush(116)
    expect(h.pending).toBe(1)
    h.cancel()
    expect(h.ended).toBe('cancelled')
    expect(h.pending).toBe(0)
  })

  it('cancel after a natural end does not re-report', () => {
    const h = harness({ goal: () => 500, durationMs: 100, quietMs: 0 })
    h.flush(0)
    h.flush(100)
    h.flush(116)
    h.flush(132)
    h.flush(148)
    expect(h.ended).toBe('settled')
    h.cancel()
    expect(h.ended).toBe('settled')
  })

  it('a search poll started during convergence supersedes the glide', () => {
    // Both drive the same scroller; the later claim wins (activeScrollOwner).
    const h = harness({ goal: () => 1000, durationMs: 100 })
    h.flush(0)
    h.flush(100)
    h.flush(116)
    const stop = pollRowSettled({ measure: () => 0, step: () => {}, raf: () => 0 })
    expect(h.ended).toBe('cancelled')
    expect(h.pending).toBe(0)
    stop()
  })

  it('a search poll started DURING TRAVEL cancels the glide and its queued frame', () => {
    // The glide claims ownership from its first frame, not at convergence: the
    // reader's newer search jump must win over the earlier banner click, and
    // travel must not keep writing alongside the poll.
    const h = harness({ goal: () => 1000, durationMs: 400 })
    h.flush(0)
    h.flush(200)
    expect(h.ended).toBeNull()
    expect(h.pending).toBe(1)
    const writesBefore = h.writes.length
    const pollFrames: (() => void)[] = []
    let pollEnded: string | null = null
    const stop = pollRowSettled({
      measure: () => 0,
      step: () => {},
      raf: (cb) => { pollFrames.push(cb); return 1 },
      now: () => 0,
      onEnd: (r) => { pollEnded = r },
    })
    expect(h.ended).toBe('cancelled')
    expect(h.pending).toBe(0)
    // The poll is the surviving owner: the glide's teardown did not revoke it.
    pollFrames.shift()?.()
    expect(pollEnded).toBeNull()
    expect(h.writes.length).toBe(writesBefore)
    stop()
    expect(pollEnded).toBe('cancelled')
  })

  it('starting a glide supersedes a running search poll', () => {
    const frames: (() => void)[] = []
    let ended: string | null = null
    pollRowSettled({
      measure: () => 0,
      step: () => {},
      raf: (cb) => { frames.push(cb); return 1 },
      now: () => 0,
      onEnd: (r) => { ended = r },
    })
    frames.shift()?.()
    expect(ended).toBeNull()
    // The claim lands at glide START, before its first frame runs.
    const h = harness({ goal: () => 1000, durationMs: 100 })
    expect(ended).toBe('cancelled')
    expect(h.ended).toBeNull()
    // The glide's own convergence hands the claim to its poll without
    // superseding itself: travel, landing and settle all complete.
    h.flush(0)
    h.flush(100)
    h.flush(116)
    h.flush(132)
    h.flush(116 + GLIDE_QUIET_MS)
    expect(h.ended).toBe('settled')
    expect(h.pending).toBe(0)
  })
})

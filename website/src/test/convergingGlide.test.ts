import { describe, expect, it } from 'vitest'
import {
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

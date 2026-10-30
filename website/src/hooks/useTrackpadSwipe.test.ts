/**
 * The trackpad run reader: a horizontal wheel run the page owns becomes one drag
 * (start, a move per event carrying the summed deltaX, an end), its momentum
 * tail never drags, and runs that belong to something else (a vertical scroll, a
 * pinch, a sideways scroller, a direction with nothing to do) are never taken.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import {
  createTrackpadSwipeReader, isClearSwipe, SWIPE_GESTURE_IDLE_MS, swipeTiming, useTrackpadSwipe, canScrollX,
  relayedWheelTime, FRAME_WHEEL_MAX_AGE_MS, FINGER_HOLD_MAX_MS,
  type TrackpadSwipeDir, type WheelSample, type WheelVerdict,
} from './useTrackpadSwipe'

/** A run of `n` wheel events 16ms apart, starting at `at`. */
function run(dx: number, dy: number, n: number, at: number, extra: Partial<WheelSample> = {}): WheelSample[] {
  return Array.from({ length: n }, (_, i) => ({ dx, dy, mode: 0, ctrl: false, owned: false, at: at + i * 16, ...extra }))
}

/** A decaying tail after `at`: each event 0.8x the previous. */
function tail(dx: number, n: number, at: number): WheelSample[] {
  return Array.from({ length: n }, (_, i) => ({ dx: dx * 0.8 ** (i + 1), dy: 0, mode: 0, ctrl: false, owned: false, at: at + (i + 1) * 16 }))
}

const yes = () => true

function feedAll(samples: WheelSample[], wants: (d: TrackpadSwipeDir, f: boolean) => boolean = yes) {
  const reader = createTrackpadSwipeReader(wants)
  const verdicts: WheelVerdict[] = samples.map((s) => reader.feed(s))
  return {
    reader,
    verdicts,
    starts: verdicts.flatMap((v) => (v.start ? [v.start] : [])),
    moves: verdicts.flatMap((v) => (v.move !== undefined ? [v.move] : [])),
    ends: verdicts.filter((v) => v.end || v.endPrev).length,
    consumed: verdicts.filter((v) => v.consume).length,
  }
}

describe('createTrackpadSwipeReader', () => {
  it('reads a leftward run (positive deltaX) as one drag whose offset follows the summed deltaX', () => {
    const { starts, moves, consumed } = feedAll(run(10, 1, 5, 0))
    expect(starts).toEqual(['left'])
    expect(moves).toEqual([10, 20, 30, 40, 50])
    expect(consumed).toBe(5)
  })

  it('reads a negative deltaX as a rightward drag', () => {
    const { starts, moves } = feedAll(run(-12, 0, 3, 0))
    expect(starts).toEqual(['right'])
    expect(moves).toEqual([-12, -24, -36])
  })

  it('releases at the third step of a geometric tail, and the tail never moves the drag', () => {
    const samples = [...run(20, 0, 6, 0), ...tail(20, 12, 5 * 16)]
    const { verdicts, moves, consumed } = feedAll(samples)
    const endAt = verdicts.findIndex((v) => v.end)
    // Three steps at a near-constant ratio are the tail's signature.
    expect(endAt).toBe(8)
    // Only the finger's deltas moved it: the shrinking ones were held back.
    expect(moves).toEqual([20, 40, 60, 80, 100, 120])
    // Every later tail event is still the page's (held), and moves nothing.
    expect(consumed).toBe(samples.length)
  })

  it('a held tail ends at a turn back: the reversed swipe is a new drag at once, with no gap', () => {
    const samples = [...run(20, 0, 6, 0), ...tail(20, 4, 5 * 16)]
    const at = 5 * 16 + 4 * 16
    samples.push(...run(-15, 0, 3, at + 16))
    const { starts, verdicts } = feedAll(samples)
    expect(verdicts.findIndex((v) => v.end)).toBe(8)
    expect(starts).toEqual(['left', 'right'])
  })

  it('a held tail ends when the deltas grow again: two steps each 1.3x or more', () => {
    // The tail decays 20 -> 6.6; then 8, 11, 15: new fingers, same way.
    const samples = [...run(20, 0, 6, 0), ...tail(20, 5, 5 * 16)]
    const at = 5 * 16 + 5 * 16
    ;[8, 11, 15, 15].forEach((dx, i) => samples.push({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: at + (i + 1) * 16 }))
    const { starts, verdicts } = feedAll(samples)
    expect(starts).toEqual(['left', 'left'])
    // The new drag starts at the second rise.
    expect(verdicts.findIndex((v, i) => i > 8 && v.start)).toBe(samples.length - 2)
  })

  it('a pure decaying tail, however long, never starts a drag again; nor does one spike in it', () => {
    const samples = [...run(20, 0, 6, 0), ...tail(20, 30, 5 * 16)]
    expect(feedAll(samples).starts).toEqual(['left'])
    const spiked = [...run(20, 0, 6, 0), ...tail(20, 6, 5 * 16)]
    spiked.push({ dx: 12, dy: 0, mode: 0, ctrl: false, owned: false, at: 5 * 16 + 7 * 16 })
    spiked.push({ dx: 4, dy: 0, mode: 0, ctrl: false, owned: false, at: 5 * 16 + 8 * 16 })
    spiked.push({ dx: 3, dy: 0, mode: 0, ctrl: false, owned: false, at: 5 * 16 + 9 * 16 })
    expect(feedAll(spiked).starts).toEqual(['left'])
  })

  it('a drag the page committed (rule C) holds the rest of its run, and a turn back starts a new one', () => {
    const reader = createTrackpadSwipeReader(yes)
    const starts: string[] = []
    let moves = 0
    const feed = (dx: number, at: number) => {
      const v = reader.feed({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at })
      if (v.start) starts.push(v.start)
      if (v.move !== undefined) moves += 1
      return v
    }
    feed(20, 0)
    feed(20, 16)
    reader.commit()
    // The same fingers, then their momentum: held, nothing moves.
    feed(20, 32)
    feed(16, 48)
    expect(moves).toBe(2)
    expect(starts).toEqual(['left'])
    // Fingers back the other way, 16ms later: a new drag at once.
    expect(feed(-10, 64).start).toBe('right')
  })

  it('a single smaller delta mid-swipe is the fingers, not the tail', () => {
    const deltas = [10, 20, 24, 22, 25, 25]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { moves, ends } = feedAll(samples)
    expect(ends).toBe(0)
    // The held-back 22 is applied once the run picks up again.
    expect(moves).toEqual([10, 30, 54, 101, 126])
  })

  it('on a trackpad-shaped swipe the release lands within 80ms of the momentum starting', () => {
    // The shape of a two-finger swipe on a Mac trackpad: the finger phase ramps
    // up and wobbles, then the momentum phase decays geometrically from the
    // speed the fingers left at. Synthetic (this host has no trackpad), but with
    // the event rate and decay of the real stream.
    const finger = [3, 8, 14, 19, 23, 24, 23, 26, 25, 24, 25]
    const momentum = Array.from({ length: 40 }, (_, i) => Math.round(24 * 0.88 ** (i + 1) * 10) / 10)
    const samples: WheelSample[] = []
    let at = 0
    for (const dx of finger) { samples.push({ dx, dy: 0.4, mode: 0, ctrl: false, owned: false, at }); at += 12 }
    const decayStartsAt = at
    for (const dx of momentum) { samples.push({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at }); at += 16 }
    const { verdicts, moves } = feedAll(samples)
    const end = verdicts.findIndex((v) => v.end)
    expect(end).toBeGreaterThan(-1)
    expect(samples[end].at - decayStartsAt).toBeLessThanOrEqual(80)
    // At the third momentum event, 32ms in.
    expect(samples[end].at - decayStartsAt).toBe(32)
    // The drag stopped where the fingers left it: the tail added nothing.
    expect(moves[moves.length - 1]).toBe(finger.reduce((x, y) => x + y, 0))
  })

  it('a finger that is only slowing down keeps following 1:1: uneven shrinking is not a tail', () => {
    const deltas = [8, 7, 6.5, 5, 4.6, 3, 3.2, 2.1, 1.9]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { moves, ends } = feedAll(samples)
    expect(ends).toBe(0)
    // Nothing is dropped: the drag ends at the fingers' full travel.
    expect(moves[moves.length - 1]).toBeCloseTo(deltas.reduce((x, y) => x + y, 0), 6)
  })

  it('a creeping tail after a fast run ends the drag at its third tiny step, and moves nothing', () => {
    // The shape measured in a recording of a small swipe: a fast stroke, then
    // momentum that creeps 2px, 1px, 0, 2px... for a third of a second.
    const deltas = [6, 20, 28, 30, 2, 1, 0, 2, 1, 1, 1]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { verdicts, moves, consumed } = feedAll(samples)
    // The third non-zero tiny step (the zero between does not break the streak).
    expect(verdicts.findIndex((v) => v.end)).toBe(7)
    expect(moves).toEqual([6, 26, 54, 84])
    expect(consumed).toBe(samples.length)
  })

  it('a slow, uneven drag (a burst, then 1-2px steps) is never read as its tail: it follows every pixel', () => {
    const deltas = [2, 6, 9, 2, 1, 1, 2, 1, 7, 1, 1, 1, 2, 5, 1, 1, 1, 1]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { reader, ends } = feedAll(samples)
    expect(ends).toBe(0)
    reader.finish()
    const applied = reader.takeFlush()
    expect(applied ?? reader.summary().sumX).toBe(deltas.reduce((x, y) => x + y, 0))
  })

  it('a drag that is slow all along is never cut: it follows every pixel', () => {
    const deltas = [2, 3, 4, 3, 2, 3, 4, 3, 2, 1, 3, 4]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { moves, ends } = feedAll(samples)
    expect(ends).toBe(0)
    expect(moves[moves.length - 1]).toBe(deltas.reduce((x, y) => x + y, 0))
  })

  it('tiny steps followed by a bigger one were the fingers: they are applied with it', () => {
    const deltas = [20, 24, 3, 2, 12]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { moves, ends } = feedAll(samples)
    expect(ends).toBe(0)
    expect(moves).toEqual([20, 44, 61])
  })

  it('a gap release applies held-back deltas that never proved to be a tail', () => {
    // Two shrinking steps look like the start of a tail and are held back; then
    // the fingers stop. They were the fingers: 20+20+20+15+12 = 87, not 60.
    const deltas = [20, 20, 20, 15, 12]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 30 }))
    const { reader, moves } = feedAll(samples)
    expect(moves[moves.length - 1]).toBe(60)
    expect(reader.finish(4 * 30 + 200)).toBe(true)
    expect(reader.takeFlush()).toBe(87)
    expect(reader.summary().sumX).toBe(87)
    // A recognised tail is still dropped: nothing to apply there.
    const tailRun = feedAll([...run(20, 0, 6, 0), ...tail(20, 12, 5 * 16)])
    tailRun.reader.finish(10_000)
    expect(tailRun.reader.takeFlush()).toBeUndefined()
  })

  it('a turn back that keeps shrinking is the fingers, not a tail: it moves the drag and ends nothing', () => {
    // The reversed steps shrink by 0.8 each, inside the tail's band by size
    // alone. A tail never changes direction, so they are the fingers.
    const deltas = [20, 20, 20, 20, -16, -12.8, -10.24]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { reader, moves, ends } = feedAll(samples)
    expect(ends).toBe(0)
    // The turn itself applies at once; the two after it are held as a streak.
    expect(moves).toEqual([20, 40, 60, 80, 64])
    expect(reader.finish(6 * 16 + 200)).toBe(true)
    expect(reader.takeFlush()).toBeCloseTo(40.96, 6)
    expect(reader.summary().sumX).toBeCloseTo(40.96, 6)
  })

  it('a turn back applies what a streak held, before the turn', () => {
    const deltas = [20, 20, 16, 12.8, -5]
    const samples = deltas.map((dx, i) => ({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    const { moves, ends } = feedAll(samples)
    expect(ends).toBe(0)
    expect(moves[moves.length - 1]).toBeCloseTo(63.8, 6)
  })

  it('a gap that ends a drag at the next event reports the applied sum and the earlier run', () => {
    const reader = createTrackpadSwipeReader(yes)
    for (const [dx, at] of [[20, 0], [20, 30], [20, 60], [15, 90], [12, 120]]) reader.feed({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at })
    const v = reader.feed({ dx: 0, dy: 30, mode: 0, ctrl: false, owned: false, at: 120 + 500 })
    expect(v.endPrev).toBe(true)
    expect(v.prevMove).toBe(87)
    expect(v.prevAt).toBe(120)
    expect(v.prevSummary).toEqual({ dir: 'left', sumX: 87, sumY: 0 })
  })

  it('a 60ms pause mid-drag is the fingers holding still, not a lift-off', () => {
    const first = run(5, 0, 10, 0)
    const resume = run(5, 0, 10, first[first.length - 1].at + 60)
    const { moves, ends, starts } = feedAll([...first, ...resume])
    expect(starts).toEqual(['left'])
    expect(ends).toBe(0)
    expect(moves[moves.length - 1]).toBe(100)
  })

  it('a new run right after a drag ends is a new drag (the page may grab its settle); a tail never is', () => {
    const first = run(20, 0, 5, 0)
    const quiet = first[first.length - 1].at
    const reader = createTrackpadSwipeReader(yes)
    first.forEach((s) => reader.feed(s))
    reader.finish(quiet)
    const soon = run(-20, 0, 3, quiet + SWIPE_GESTURE_IDLE_MS + 1).map((s) => reader.feed(s))
    expect(soon[0].start).toBe('right')
    expect(soon.map((v) => v.move)).toEqual([-20, -40, -60])
    // A recognised tail holds the rest of its own run: it never starts a drag.
    const r2 = createTrackpadSwipeReader(yes)
    const tailRun = [...run(20, 0, 6, 0), ...tail(20, 12, 5 * 16)].map((s) => r2.feed(s))
    expect(tailRun.filter((v) => v.start)).toHaveLength(1)
  })

  it('a vertical scroll right after a drag scrolls: the cooldown holds only horizontal runs', () => {
    const reader = createTrackpadSwipeReader(yes)
    run(20, 0, 5, 0).forEach((s) => reader.feed(s))
    reader.finish(64)
    const scroll = run(0, 30, 40, 64 + SWIPE_GESTURE_IDLE_MS + 1).map((s) => reader.feed(s))
    expect(scroll.some((v) => v.consume)).toBe(false)
  })

  it('a run that keeps going without a gap past the cooldown is still decided afresh once it pauses', () => {
    const reader = createTrackpadSwipeReader(yes)
    run(20, 0, 5, 0).forEach((s) => reader.feed(s))
    reader.finish(64)
    // A long vertical scroll, no gaps, well past the cooldown: never consumed.
    const scroll = run(0, 25, 60, 64 + SWIPE_GESTURE_IDLE_MS + 1).map((s) => reader.feed(s))
    expect(scroll.some((v) => v.consume)).toBe(false)
  })

  it('leaves a vertical scroll alone, even when it drifts sideways later', () => {
    const { starts, consumed } = feedAll([...run(1, 20, 5, 0), ...run(20, 1, 10, 80)])
    expect(starts).toEqual([])
    expect(consumed).toBe(0)
  })

  it('ignores a pinch zoom (ctrlKey wheel)', () => {
    const { starts, consumed } = feedAll(run(20, 0, 20, 0, { ctrl: true }))
    expect(starts).toEqual([])
    expect(consumed).toBe(0)
  })

  it('leaves a run that starts over content that can still scroll sideways to that content', () => {
    const { starts, consumed } = feedAll([...run(20, 0, 3, 0, { owned: true }), ...run(20, 0, 10, 48)])
    expect(starts).toEqual([])
    expect(consumed).toBe(0)
  })

  it('leaves a direction the page declines to the browser, and tells it where a run came from', () => {
    const asked: Array<[TrackpadSwipeDir, boolean]> = []
    const { starts, consumed } = feedAll(run(-20, 0, 5, 0, { fromFrame: true }), (d, f) => { asked.push([d, f]); return false })
    expect(starts).toEqual([])
    expect(consumed).toBe(0)
    expect(asked).toEqual([['right', true]])
  })

  it('a gap ends a drag left open and decides the next run afresh', () => {
    const reader = createTrackpadSwipeReader(yes)
    run(20, 0, 3, 0).forEach((s) => reader.feed(s))
    const next = reader.feed({ dx: 0, dy: 30, mode: 0, ctrl: false, owned: false, at: 32 + SWIPE_GESTURE_IDLE_MS + 1 })
    expect(next.endPrev).toBe(true)
    expect(next.consume).toBe(false)
  })

  it('normalizes line-mode deltas', () => {
    expect(feedAll(run(3, 0, 2, 0, { mode: 1 })).moves).toEqual([48, 96])
  })
})

describe('isClearSwipe (reduced motion)', () => {
  it('needs the threshold and a clearly horizontal run', () => {
    expect(isClearSwipe('left', 100, 10)).toBe(true)
    expect(isClearSwipe('left', 60, 0)).toBe(false)
    expect(isClearSwipe('left', 100, 80)).toBe(false)
    expect(isClearSwipe('right', -100, 0)).toBe(true)
  })
})

describe('useTrackpadSwipe: the quiet check after a run', () => {
  afterEach(() => {
    vi.useRealTimers()
    document.documentElement.style.overscrollBehaviorX = ''
    document.body.replaceChildren()
  })

  it('a scroll that starts before the quiet check runs is not reset by it: a later sideways drift stays with the scroll', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const onDragStart = vi.fn()
    const onDragEnd = vi.fn()
    renderHook(() => useTrackpadSwipe({ current: root }, {
      enabled: true, wants: () => true, onDragStart, onDragMove: () => {}, onDragEnd, onSwipe: () => {},
    }))
    /** One wheel at event time `at`; returns whether the page took it. */
    const wheel = (dx: number, dy: number, at: number) => {
      const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: dy, bubbles: true, cancelable: true })
      Object.defineProperty(ev, 'timeStamp', { value: at })
      root.dispatchEvent(ev)
      return ev.defaultPrevented
    }
    for (let i = 0; i < 4; i++) expect(wheel(20, 0, 1 + i * 16)).toBe(true)
    expect(onDragStart).toHaveBeenCalledTimes(1)
    expect(document.documentElement.style.overscrollBehaviorX).toBe('contain')
    // The quiet timer fires; its check waits for the next frame.
    vi.advanceTimersByTime(swipeTiming.idleMs)
    expect(onDragEnd).not.toHaveBeenCalled()
    // Before that frame: a vertical scroll, past the gap. It ends the drag and
    // is the browser's; the drag's hold on overscroll goes with it.
    const scrollAt = 1 + 3 * 16 + swipeTiming.idleMs + 20
    expect(wheel(0, 30, scrollAt)).toBe(false)
    expect(onDragEnd).toHaveBeenCalledTimes(1)
    expect(document.documentElement.style.overscrollBehaviorX).toBe('')
    // The old run's check runs now. It must not end the scroll's run.
    vi.advanceTimersToNextFrame()
    // A sideways drift later in the same scroll still belongs to the scroll.
    expect(wheel(25, 5, scrollAt + 16)).toBe(false)
    expect(onDragStart).toHaveBeenCalledTimes(1)
    expect(onDragEnd).toHaveBeenCalledTimes(1)
  })
})

describe('createTrackpadSwipeReader with the finger phases', () => {
  it('fingers down: a pause past the gap keeps the drag, and the lift ends it', () => {
    let down = true
    const reader = createTrackpadSwipeReader(yes, () => down)
    const first = run(20, 0, 3, 0).map((s) => reader.feed(s))
    expect(first[0].start).toBe('left')
    // 300ms with the fingers resting on the pad, then they move on.
    const later = reader.feed({ dx: 20, dy: 0, mode: 0, ctrl: false, owned: false, at: 32 + 300 })
    expect(later.endPrev).toBeUndefined()
    expect(later.start).toBeUndefined()
    expect(later.move).toBe(80)
    down = false
    expect(reader.lift()).toBe(true)
    // The rest of the run is the tail: held, never moving the drag.
    const tailV = tail(20, 4, 332).map((s) => reader.feed(s))
    expect(tailV.every((v) => v.consume && v.move === undefined && !v.start)).toBe(true)
    expect(reader.lift()).toBe(false)
  })

  it('fingers down: small or decaying steps are the fingers, never an inferred tail', () => {
    const reader = createTrackpadSwipeReader(yes, () => true)
    // A 16px step, then three 3px steps (the creeping-tail shape), then a
    // geometric decay (the momentum shape), all with the fingers on the pad.
    const deltas = [16, 3, 3, 3, 3, 12, 9.6, 7.68, 6.14]
    const vs = deltas.map((dx, i) => reader.feed({ dx, dy: 0, mode: 0, ctrl: false, owned: false, at: i * 16 }))
    expect(vs.some((v) => v.end)).toBe(false)
    expect(vs[vs.length - 1].move).toBeCloseTo(deltas.reduce((x, y) => x + y, 0), 6)
    // The real lift ends it.
    expect(reader.lift()).toBe(true)
  })

  it('a lift keeps what was held back as a possible tail: it was the fingers', () => {
    // Without the phases: two shrinking steps are held back as a possible tail,
    // and the shell's lift (heard late) applies them.
    let down = false
    const reader = createTrackpadSwipeReader(yes, () => down)
    for (const s of run(20, 0, 3, 0)) reader.feed(s)
    reader.feed({ dx: 16, dy: 0, mode: 0, ctrl: false, owned: false, at: 48 })
    reader.feed({ dx: 12.8, dy: 0, mode: 0, ctrl: false, owned: false, at: 64 })
    expect(reader.lift()).toBe(true)
    expect(reader.takeFlush()).toBeCloseTo(88.8)
    // With the fingers known down, the same steps are applied as they come.
    down = true
    const r2 = createTrackpadSwipeReader(yes, () => down)
    for (const s of run(20, 0, 3, 0)) r2.feed(s)
    r2.feed({ dx: 16, dy: 0, mode: 0, ctrl: false, owned: false, at: 48 })
    expect(r2.feed({ dx: 12.8, dy: 0, mode: 0, ctrl: false, owned: false, at: 64 }).move).toBeCloseTo(88.8)
    expect(r2.lift()).toBe(true)
  })

  it('fingers down still ends the drag past the hold cap', () => {
    const reader = createTrackpadSwipeReader(yes, () => true)
    for (const s of run(20, 0, 3, 0)) reader.feed(s)
    const v = reader.feed({ dx: 20, dy: 0, mode: 0, ctrl: false, owned: false, at: 32 + FINGER_HOLD_MAX_MS + 1 })
    expect(v.endPrev).toBe(true)
    expect(v.start).toBe('left')
  })

  it('a new touch drops a held tail, so the next event decides a new run', () => {
    const reader = createTrackpadSwipeReader(yes, () => true)
    for (const s of run(20, 0, 3, 0)) reader.feed(s)
    reader.lift()
    expect(reader.dropHeld()).toBe(true)
    expect(reader.dropHeld()).toBe(false)
    expect(reader.feed({ dx: -15, dy: 0, mode: 0, ctrl: false, owned: false, at: 60 }).start).toBe('right')
  })

  it('without the phases (a browser), the same pause ends the drag as before', () => {
    const reader = createTrackpadSwipeReader(yes)
    for (const s of run(20, 0, 3, 0)) reader.feed(s)
    const v = reader.feed({ dx: 20, dy: 0, mode: 0, ctrl: false, owned: false, at: 32 + 300 })
    expect(v.endPrev).toBe(true)
  })
})

describe('useTrackpadSwipe: the shell\'s finger phases', () => {
  type Bridge = { emit: ((p: unknown) => void) | null; stops: number }
  function installBridge(): Bridge {
    const bridge: Bridge = { emit: null, stops: 0 }
    ;(window as Window & { electronAPI?: unknown }).electronAPI = {
      onScrollTouch: (cb: (p: unknown) => void) => {
        bridge.emit = cb
        return () => { bridge.stops += 1; bridge.emit = null }
      },
    }
    return bridge
  }
  afterEach(() => {
    vi.useRealTimers()
    delete (window as Window & { electronAPI?: unknown }).electronAPI
    document.documentElement.style.overscrollBehaviorX = ''
    document.body.replaceChildren()
  })

  function mount() {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame', 'performance'] })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const onDragStart = vi.fn()
    const onDragMove = vi.fn()
    const onDragEnd = vi.fn()
    const hook = renderHook(() => useTrackpadSwipe({ current: root }, {
      enabled: true, wants: () => true, onDragStart, onDragMove, onDragEnd, onSwipe: () => {},
    }))
    /** One wheel now, on the (fake) event clock. */
    const wheel = (dx: number) => {
      const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: 0, bubbles: true, cancelable: true })
      Object.defineProperty(ev, 'timeStamp', { value: performance.now() })
      root.dispatchEvent(ev)
      return ev.defaultPrevented
    }
    /** `n` wheels 16ms apart. */
    const swipe = (dx: number, n: number) => {
      for (let i = 0; i < n; i++) { wheel(dx); vi.advanceTimersByTime(16) }
    }
    return { hook, wheel, swipe, onDragStart, onDragMove, onDragEnd }
  }

  it('releases on the real lift-off, not on a pause, with the lift time', () => {
    const bridge = installBridge()
    const { swipe, wheel, onDragStart, onDragMove, onDragEnd } = mount()
    bridge.emit!('begin')
    swipe(20, 4)
    expect(onDragStart).toHaveBeenCalledWith('left')
    // The fingers rest for 400ms: far past the gap, still one drag.
    vi.advanceTimersByTime(400)
    vi.advanceTimersToNextFrame()
    expect(onDragEnd).not.toHaveBeenCalled()
    swipe(20, 2)
    const liftAt = performance.now()
    bridge.emit!('end')
    // The release waits one frame for wheels queued ahead of the message.
    expect(onDragEnd).not.toHaveBeenCalled()
    vi.advanceTimersToNextFrame()
    expect(onDragEnd).toHaveBeenCalledTimes(1)
    expect(onDragEnd).toHaveBeenCalledWith('lift', liftAt)
    expect(onDragMove).toHaveBeenLastCalledWith(120, expect.any(Number))
    // The momentum tail after the lift is still the page's, and moves nothing.
    const moves = onDragMove.mock.calls.length
    expect(wheel(18)).toBe(true)
    expect(wheel(14)).toBe(true)
    expect(onDragMove.mock.calls.length).toBe(moves)
    expect(onDragStart).toHaveBeenCalledTimes(1)
    // New fingers during the tail: a new drag at once.
    bridge.emit!('begin')
    expect(document.documentElement.style.overscrollBehaviorX).toBe('')
    wheel(-20)
    expect(onDragStart).toHaveBeenCalledTimes(2)
    expect(onDragStart).toHaveBeenLastCalledWith('right')
  })

  it('a lost end still releases at the hold cap', () => {
    const bridge = installBridge()
    const { swipe, onDragEnd } = mount()
    bridge.emit!('begin')
    swipe(20, 4)
    vi.advanceTimersByTime(FINGER_HOLD_MAX_MS + 50)
    vi.advanceTimersToNextFrame()
    expect(onDragEnd).toHaveBeenCalledTimes(1)
    expect(onDragEnd.mock.calls[0][0]).toBe('gap')
  })

  it('a begin that lands while a lift waits for its frame settles the old drag first', () => {
    const bridge = installBridge()
    const { swipe, wheel, onDragStart, onDragEnd } = mount()
    bridge.emit!('begin')
    swipe(20, 4)
    bridge.emit!('end')
    bridge.emit!('begin')
    expect(onDragEnd).toHaveBeenCalledWith('lift', expect.any(Number))
    // The new touch is not swallowed as the old drag's tail.
    wheel(20)
    expect(onDragStart).toHaveBeenCalledTimes(2)
  })

  it('unsubscribes on unmount', () => {
    const bridge = installBridge()
    const { hook } = mount()
    hook.unmount()
    expect(bridge.stops).toBe(1)
  })

  it('without the bridge (a browser), the gap still ends the drag', () => {
    const { swipe, onDragEnd } = mount()
    swipe(20, 4)
    vi.advanceTimersByTime(swipeTiming.idleMs)
    vi.advanceTimersToNextFrame()
    expect(onDragEnd).toHaveBeenCalledTimes(1)
    expect(onDragEnd.mock.calls[0][0]).toBe('gap')
  })
})

describe('relayedWheelTime', () => {
  const now = 5000
  const epoch = (at: number) => performance.timeOrigin + at
  it("reads a frame's wheel time on this page's clock", () => {
    expect(relayedWheelTime(epoch(4800), 4999, now)).toBeCloseTo(4800, 3)
  })
  it('falls back to the arrival time for a time that is missing, not a number, in the future, or too old', () => {
    expect(relayedWheelTime(undefined, 4999, now)).toBe(4999)
    expect(relayedWheelTime('4800', 4999, now)).toBe(4999)
    expect(relayedWheelTime(Number.NaN, 4999, now)).toBe(4999)
    expect(relayedWheelTime(Infinity, 4999, now)).toBe(4999)
    expect(relayedWheelTime(epoch(now + 50), 4999, now)).toBe(4999)
    expect(relayedWheelTime(epoch(now - FRAME_WHEEL_MAX_AGE_MS - 1), 4999, now)).toBe(4999)
    // No arrival time either: now.
    expect(relayedWheelTime(undefined, 0, now)).toBe(now)
  })
  it('never reads a time past now, though two origins may differ by a fraction of a ms', () => {
    expect(relayedWheelTime(epoch(now + 0.5), 4999, now)).toBe(now)
  })
})

describe('canScrollX', () => {
  afterEach(() => { document.body.replaceChildren() })
  /** A scroller with 500px to scroll, at `scrollLeft`. */
  const scroller = (scrollLeft: number, direction: 'ltr' | 'rtl') => {
    const el = document.createElement('div')
    el.style.overflowX = 'auto'
    el.style.direction = direction
    Object.defineProperty(el, 'scrollWidth', { value: 800 })
    Object.defineProperty(el, 'clientWidth', { value: 300 })
    Object.defineProperty(el, 'scrollLeft', { value: scrollLeft })
    document.body.appendChild(el)
    return el
  }

  it('right to left, at its start (scrollLeft 0): it can scroll on with a negative deltaX, not back', () => {
    const el = scroller(0, 'rtl')
    expect(canScrollX(el, -20, el)).toBe(true)
    expect(canScrollX(el, 20, el)).toBe(false)
  })

  it('right to left, at its far end (scrollLeft -max): it can only scroll back', () => {
    const el = scroller(-500, 'rtl')
    expect(canScrollX(el, -20, el)).toBe(false)
    expect(canScrollX(el, 20, el)).toBe(true)
  })

  it('left to right keeps its bounds: [0, max]', () => {
    const start = scroller(0, 'ltr')
    expect(canScrollX(start, -20, start)).toBe(false)
    expect(canScrollX(start, 20, start)).toBe(true)
    const end = scroller(500, 'ltr')
    expect(canScrollX(end, 20, end)).toBe(false)
    expect(canScrollX(end, -20, end)).toBe(true)
  })
})

import { describe, it, expect, vi } from 'vitest'
import {
  pickSearchScrollBehavior,
  RAPID_STEP_MS,
  scrollCurrentMatchIntoView,
  pollRowSettled,
  glideOnceStep,
  attachUserScrollIntent,
} from '../utils/searchScroll'

describe('pickSearchScrollBehavior', () => {
  it('snaps (auto) when stepping faster than the threshold', () => {
    expect(pickSearchScrollBehavior(1000, 900)).toBe('auto') // 100ms apart
    expect(pickSearchScrollBehavior(1000, 1000)).toBe('auto') // same instant
  })

  it('settles (smooth) when the gap is at or beyond the threshold', () => {
    expect(pickSearchScrollBehavior(1000, 1000 - RAPID_STEP_MS)).toBe('smooth') // exactly 250ms
    expect(pickSearchScrollBehavior(2000, 1000)).toBe('smooth') // 1s apart
  })

  it('settles on the first step (lastStepAt = 0)', () => {
    expect(pickSearchScrollBehavior(5000, 0)).toBe('smooth')
  })

  it('respects a custom threshold', () => {
    expect(pickSearchScrollBehavior(1000, 940, 50)).toBe('smooth') // 60ms >= 50
    expect(pickSearchScrollBehavior(1000, 970, 50)).toBe('auto')   // 30ms < 50
  })
})

// A deterministic frame scheduler + clock so the condition-based converge loops
// can be driven one frame at a time without real rAF / timers.
function makeDriver() {
  let queue: Array<() => void> = []
  let t = 0
  return {
    raf: (cb: () => void) => { queue.push(cb); return queue.length },
    now: () => t,
    advance: (ms: number) => { t += ms },
    pending: () => queue.length,
    /** Run up to `n` queued frames (each may re-queue the next). */
    flush: (n = 1) => {
      for (let i = 0; i < n; i++) {
        const cb = queue.shift()
        if (!cb) break
        cb()
      }
    },
    drain: (maxFrames = 500, msPerFrame = 8) => {
      for (let i = 0; i < maxFrames && queue.length; i++) {
        t += msPerFrame
        const cb = queue.shift()
        cb?.()
      }
    },
  }
}

describe('pollRowSettled: nested scroll ownership (GPT MEDIUM round 13)', () => {
  // Navigating to a search result starts a ROW-level convergence (centre
  // display index N); once that row mounts, the message component starts a
  // finer MARK-level convergence (centre the exact occurrence inside it). The
  // row poll re-scrolls every frame for the whole quiet window, so without a
  // handoff it repeatedly undid the mark centring and the viewport ended up on
  // the containing turn rather than on the match.
  it('a newer poll retires an older one that has already stepped', () => {
    const d = makeDriver()
    const rowSteps: number[] = []
    const rowEnd: string[] = []
    pollRowSettled({
      measure: () => 10,
      step: () => rowSteps.push(1),
      raf: d.raf,
      now: d.now,
      onEnd: (r) => rowEnd.push(r),
    })
    d.flush(1)
    expect(rowSteps.length).toBe(1)
    expect(rowEnd).toEqual([])

    // The nested mark-level poll claims ownership.
    const markSteps: number[] = []
    pollRowSettled({
      measure: () => 20,
      step: () => markSteps.push(1),
      raf: d.raf,
      now: d.now,
    })
    expect(rowEnd).toEqual(['cancelled'])

    // The retired poll must stop stepping; the new one keeps going.
    const rowStepsAtHandoff = rowSteps.length
    d.drain(20)
    expect(rowSteps.length).toBe(rowStepsAtHandoff)
    expect(markSteps.length).toBeGreaterThan(0)
  })

  // The row poll's first step is what MOUNTS the nested target. Retiring it
  // before that step would mean the mark never appears and NEITHER scroll
  // happens — the far-jump no-op this PR exists to fix.
  it('defers retirement until the superseded poll has stepped once', () => {
    const d = makeDriver()
    let rowPresent = false
    const rowSteps: number[] = []
    const rowEnd: string[] = []
    pollRowSettled({
      measure: () => (rowPresent ? 10 : null),
      step: () => rowSteps.push(1),
      raf: d.raf,
      now: d.now,
      onEnd: (r) => rowEnd.push(r),
    })
    d.flush(2)
    expect(rowSteps.length).toBe(0)

    // A nested poll claims ownership while the row is still unmounted.
    pollRowSettled({ measure: () => null, step: () => {}, raf: d.raf, now: d.now })
    // Not retired yet — it still owes its mounting step.
    expect(rowEnd).toEqual([])

    rowPresent = true
    d.flush(1)
    expect(rowSteps.length).toBe(1)
    expect(rowEnd).toEqual(['cancelled'])
  })

  it('a retired poll\'s teardown cannot revoke the newer poll\'s claim', () => {
    const d = makeDriver()
    const cancelA = pollRowSettled({ measure: () => 10, step: () => {}, raf: d.raf, now: d.now })
    d.flush(1)
    const bSteps: number[] = []
    pollRowSettled({
      measure: () => 10,
      step: () => bSteps.push(1),
      raf: d.raf,
      now: d.now,
    })
    // Let B actually step, so C's claim retires it immediately rather than
    // deferring (the deferral case is covered by the test above).
    d.flush(3)
    // A late cancel() on the already-retired poll A must not clear B's claim —
    // otherwise C would find no owner to supersede and both would run at once.
    cancelA()
    const bStepsBeforeC = bSteps.length
    expect(bStepsBeforeC).toBeGreaterThan(0)
    const cSteps: number[] = []
    pollRowSettled({
      measure: () => 10,
      step: () => cSteps.push(1),
      raf: d.raf,
      now: d.now,
    })
    d.drain(20)
    expect(cSteps.length).toBeGreaterThan(0)
    // B was superseded by C, so it stopped stepping.
    expect(bSteps.length).toBe(bStepsBeforeC)
  })
})

describe('glideOnceStep (GPT MEDIUM round 3)', () => {
  // Re-issuing a smooth scroll cancels the in-flight animation and restarts it.
  // A convergence poll steps once per frame, so a NEAR jump that kept
  // behavior:'smooth' on every step stuttered/stalled until the poll ended.
  it('uses the requested behavior once, then snaps instantly', () => {
    const seen: ScrollBehavior[] = []
    const step = glideOnceStep((b) => seen.push(b), 'smooth')
    step(); step(); step(); step()
    expect(seen).toEqual(['smooth', 'auto', 'auto', 'auto'])
  })

  it('passes an already-instant behavior straight through', () => {
    const seen: ScrollBehavior[] = []
    const step = glideOnceStep((b) => seen.push(b), 'auto')
    step(); step()
    expect(seen).toEqual(['auto', 'auto'])
  })

  it('each call site gets its own latch (a new jump may glide again)', () => {
    const a: ScrollBehavior[] = []
    const b: ScrollBehavior[] = []
    const stepA = glideOnceStep((x) => a.push(x), 'smooth')
    const stepB = glideOnceStep((x) => b.push(x), 'smooth')
    stepA(); stepA(); stepB()
    expect(a).toEqual(['smooth', 'auto'])
    expect(b).toEqual(['smooth'])
  })

  it('under a real poll, exactly ONE smooth scroll is issued across the quiet window', () => {
    const d = makeDriver()
    const seen: ScrollBehavior[] = []
    pollRowSettled({
      measure: () => 300, // stable, so the 500ms quiet window is what keeps it alive
      step: glideOnceStep((b) => seen.push(b), 'smooth'),
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
    })
    d.drain(500, 8)
    // Many steps across the quiet window, but only the first animated.
    expect(seen.length).toBeGreaterThan(2)
    expect(seen.filter((b) => b === 'smooth')).toHaveLength(1)
    expect(seen[0]).toBe('smooth')
  })
})

describe('pollRowSettled: user scroll aborts convergence (GPT MEDIUM round 2)', () => {
  // The poll re-scrolls every frame for up to ~2s. If the user wheels during
  // that window, continuing to step drags the viewport back to the target and
  // fights their input. navToDisplayIndex now cancels on wheel/touchmove and
  // detaches the listeners when the poll ends; these assertions pin the
  // contract that makes that wiring correct.
  it('cancel() stops all further step() calls mid-flight', () => {
    const d = makeDriver()
    const step = vi.fn()
    let reason = ''
    // A target that never settles, so only a cancel can end the loop.
    let h = 100
    const cancel = pollRowSettled({
      measure: () => (h += 10),
      step,
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      onEnd: (r) => { reason = r },
    })
    d.drain(5, 8)
    const callsBefore = step.mock.calls.length
    expect(callsBefore).toBeGreaterThan(0)

    cancel() // simulates the wheel/touchmove handler firing
    expect(reason).toBe('cancelled')

    d.drain(20, 8)
    expect(step.mock.calls.length).toBe(callsBefore) // no further scrolling
  })

  it('onEnd fires exactly once on cancel, so listener cleanup cannot double-run', () => {
    const d = makeDriver()
    const onEnd = vi.fn()
    const cancel = pollRowSettled({
      measure: () => 100,
      step: () => {},
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      minQuietMs: 999_999, // never settles naturally
      onEnd,
    })
    d.drain(3, 8)
    cancel()
    cancel() // idempotent
    d.drain(10, 8)
    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd).toHaveBeenCalledWith('cancelled')
  })

  it('onEnd also fires on natural settle, so listeners are detached without a cancel', () => {
    const d = makeDriver()
    const onEnd = vi.fn()
    pollRowSettled({
      measure: () => 100,
      step: () => {},
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      onEnd,
    })
    d.drain(500, 8)
    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd).toHaveBeenCalledWith('settled')
  })
})

describe('pollRowSettled: delayed widget growth (GPT MEDIUM)', () => {
  it('does NOT settle on a 2-frame streak when growth starts after the widget build delay', () => {
    const d = makeDriver()
    // A widget row: static 120px until its ~450ms iframe build lands, then it
    // grows to its real height. A frame-count-only settle would declare victory
    // at ~16-32ms, stop stepping, and let the later growth push the match
    // off-centre — the exact first-click miss this poll exists to prevent.
    const GROW_AT_MS = 450
    const FINAL = 900
    let lastStepHeight = 0
    let reason = ''
    pollRowSettled({
      measure: () => (d.now() >= GROW_AT_MS ? FINAL : 120),
      step: () => { lastStepHeight = d.now() >= GROW_AT_MS ? FINAL : 120 },
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      settleFrames: 2,
      onEnd: (r) => { reason = r },
    })
    // Run a handful of frames — enough for a 2-frame streak, nowhere near the
    // quiet window. The poll must still be alive and must not have settled.
    d.drain(6, 8) // ~48ms
    expect(reason).toBe('')
    // Carry on past the growth point and let it genuinely settle.
    d.drain(400, 8)
    expect(reason).toBe('settled')
    // Critically, the final step ran against the POST-growth height.
    expect(lastStepHeight).toBe(FINAL)
  })

  it('still settles promptly once the measurement is genuinely quiet', () => {
    const d = makeDriver()
    let reason = ''
    pollRowSettled({
      measure: () => 200, // stable from the very first frame
      step: () => {},
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      onEnd: (r) => { reason = r },
    })
    d.drain(500, 8)
    expect(reason).toBe('settled')
  })

  it('an explicit minQuietMs of 0 restores pure frame-streak settling', () => {
    const d = makeDriver()
    let reason = ''
    pollRowSettled({
      measure: () => 200,
      step: () => {},
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      settleFrames: 2,
      minQuietMs: 0,
      onEnd: (r) => { reason = r },
    })
    d.drain(4, 8) // ~32ms — would NOT be enough with the default quiet window
    expect(reason).toBe('settled')
  })
})

describe('pollRowSettled', () => {
  it('waits for a target that mounts AFTER the old 30-frame ceiling, then settles', () => {
    const d = makeDriver()
    let frame = 0
    const mountAt = 40 // well past the retired MAX_FRAMES = 30 ceiling
    const step = vi.fn()
    let reason = ''
    // Row is absent (null) until frame 40, then a stable measured height.
    pollRowSettled({
      measure: () => (++frame >= mountAt ? 100 : null),
      step,
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      settleFrames: 2,
      onEnd: (r) => { reason = r },
    })
    d.drain()
    // It did NOT bail early: the scroll ran only once the row mounted (>30
    // frames in), and the loop converged rather than silently no-op'ing.
    expect(step).toHaveBeenCalled()
    expect(reason).toBe('settled')
    expect(d.pending()).toBe(0) // terminated — not spinning
  })

  it('re-reads/re-scrolls while a widget target keeps growing, settling once stable', () => {
    const d = makeDriver()
    // Height climbs (widget iframe building) then holds — mimics the ~450ms
    // PROGRAMMATIC_BUILD_DELAY_MS growth the frame-count cap used to miss.
    const heights = [50, 60, 90, 140, 140, 140]
    let i = 0
    const step = vi.fn()
    let reason = ''
    pollRowSettled({
      measure: () => heights[Math.min(i++, heights.length - 1)],
      step,
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      settleFrames: 2,
      onEnd: (r) => { reason = r },
    })
    d.drain()
    // Scrolled on every present frame while growing (re-reading the offset),
    // and only stopped once the height stopped moving.
    expect(step.mock.calls.length).toBeGreaterThanOrEqual(4)
    expect(reason).toBe('settled')
  })

  it('terminates via the wall-clock backstop when the target never mounts', () => {
    const d = makeDriver()
    const step = vi.fn()
    let reason = ''
    pollRowSettled({
      measure: () => null, // never present
      step,
      raf: d.raf,
      now: d.now,
      maxMs: 100,
      onEnd: (r) => { reason = r },
    })
    d.drain(500, 16) // 16ms/frame → crosses the 100ms backstop quickly
    expect(step).not.toHaveBeenCalled()
    expect(reason).toBe('timeout')
    expect(d.pending()).toBe(0) // did not spin forever
  })

  it('cancel() stops the loop and fires onEnd exactly once', () => {
    const d = makeDriver()
    const step = vi.fn()
    const onEnd = vi.fn()
    const cancel = pollRowSettled({
      measure: () => 100,
      step,
      raf: d.raf,
      now: d.now,
      maxMs: 5000,
      onEnd,
    })
    d.flush(1)               // one frame runs (scrolls once)
    const before = step.mock.calls.length
    cancel()
    d.flush(5)               // any queued frames must no-op after cancel
    expect(step.mock.calls.length).toBe(before)
    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd).toHaveBeenCalledWith('cancelled')
    cancel()                 // idempotent
    expect(onEnd).toHaveBeenCalledTimes(1)
  })
})

describe('scrollCurrentMatchIntoView', () => {
  it('returns a cancel function so callers can abort the converge loop', () => {
    const cancel = scrollCurrentMatchIntoView(document.body, { maxMs: 50 })
    expect(typeof cancel).toBe('function')
    cancel()
  })

  it('cancel is idempotent and safe to call (no throw, removes listeners)', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const cancel = scrollCurrentMatchIntoView(document.body, { maxMs: 50 })
    expect(() => { cancel(); cancel() }).not.toThrow()
    // cancel() tears down the wheel/touchmove listeners it registered.
    expect(remove).toHaveBeenCalledWith('wheel', expect.any(Function))
    expect(remove).toHaveBeenCalledWith('touchmove', expect.any(Function))
    remove.mockRestore()
  })

  it('aborts (tears down listeners) when the user scrolls with the wheel', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const cancel = scrollCurrentMatchIntoView(document.body, { maxMs: 5000 })
    // User takes over — the loop must bail so it never fights them.
    window.dispatchEvent(new Event('wheel'))
    expect(remove).toHaveBeenCalledWith('wheel', expect.any(Function))
    expect(remove).toHaveBeenCalledWith('touchmove', expect.any(Function))
    cancel()
    remove.mockRestore()
  })

  it('aborts on touchmove', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const cancel = scrollCurrentMatchIntoView(document.body, { maxMs: 5000 })
    window.dispatchEvent(new Event('touchmove'))
    expect(remove).toHaveBeenCalledWith('touchmove', expect.any(Function))
    cancel()
    remove.mockRestore()
  })
})

// Regression: the abort listened only for wheel/touchmove, so dragging the
// scrollbar or scrolling with the keyboard did not stop convergence and the
// user was recentered for up to CONVERGE_MAX_MS. The same gap existed
// independently in ChatPage, which is why this now lives in one shared helper.
describe('attachUserScrollIntent', () => {
  function harness() {
    const el = document.createElement('div')
    const onUser = vi.fn()
    const detach = attachUserScrollIntent(el, onUser)
    return { el, onUser, detach }
  }

  it('fires on a scrollbar drag (pointerdown), not just wheel and touch', () => {
    const { el, onUser, detach } = harness()
    el.dispatchEvent(new Event('pointerdown'))
    expect(onUser).toHaveBeenCalledTimes(1)
    el.dispatchEvent(new Event('wheel'))
    el.dispatchEvent(new Event('touchmove'))
    expect(onUser).toHaveBeenCalledTimes(3)
    detach()
  })

  it('fires on scrolling keys', () => {
    const { el, onUser, detach } = harness()
    for (const key of ['ArrowDown', 'PageUp', 'Home', 'End', ' ']) {
      el.dispatchEvent(new KeyboardEvent('keydown', { key }))
    }
    expect(onUser).toHaveBeenCalledTimes(5)
    detach()
  })

  it('ignores typing, so searching does not cancel its own convergence', () => {
    const { el, onUser, detach } = harness()
    for (const key of ['a', 'Z', '1', 'Shift', 'Enter', 'Backspace']) {
      el.dispatchEvent(new KeyboardEvent('keydown', { key }))
    }
    expect(onUser).not.toHaveBeenCalled()
    detach()
  })

  it('detaches every listener it attached', () => {
    const { el, onUser, detach } = harness()
    detach()
    el.dispatchEvent(new Event('wheel'))
    el.dispatchEvent(new Event('touchmove'))
    el.dispatchEvent(new Event('pointerdown'))
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    expect(onUser).not.toHaveBeenCalled()
  })

  it('is a no-op with no target and its detach is safe to call', () => {
    const detach = attachUserScrollIntent(undefined, () => { throw new Error('unreachable') })
    expect(() => detach()).not.toThrow()
  })

  it('a scrollbar drag aborts a real convergence poll', () => {
    // Synchronous, manually-pumped rAF so no timers are involved: each pump
    // runs at most one queued frame, which is what makes "no further steps
    // after the abort" a real assertion rather than a timing artifact.
    let queued: (() => void) | null = null
    const raf = (cb: () => void) => { queued = cb; return 1 }
    const pump = () => { const c = queued; queued = null; c?.() }
    const step = vi.fn()
    let t = 0
    const cancel = pollRowSettled({
      // A changing measurement keeps the poll alive (never settles), so the
      // only thing that can end it is the abort under test.
      measure: () => (t += 7),
      step,
      raf,
      now: () => (t += 16),
      maxMs: 100_000,
    })
    pump()
    pump()
    expect(step.mock.calls.length).toBeGreaterThan(0)

    // Wire the shared helper the way the production sites do.
    const el = document.createElement('div')
    const detach = attachUserScrollIntent(el, cancel)
    el.dispatchEvent(new Event('pointerdown'))
    const callsAtAbort = step.mock.calls.length
    pump()
    pump()
    expect(step.mock.calls.length).toBe(callsAtAbort)
    detach()
    cancel()
  })
})

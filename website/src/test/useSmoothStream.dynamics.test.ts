/**
 * useSmoothStream dynamics: the constant-latency controller.
 *
 * The reveal aims to trail the live edge by ~LAG_SECS of text (rate = backlog /
 * lag, slew-limited). These tests drive the rAF loop with a manual, deterministic
 * frame pump and pin the two properties the redesign exists for:
 *
 *  1. NO STARVATION: during a short gap in the incoming stream (shorter than the
 *     standing lag) the reveal keeps flowing instead of freezing at the live
 *     edge. A design that reveals straight to the edge freezes, then surges on
 *     the next burst — the freeze→surge cycle users perceive as "text showing
 *     up in chunks".
 *
 *  2. NO SNAPPING: a large single burst mounts over multiple frames as a smooth
 *     ramp (slew-limited rate), never as one giant frame delta.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSmoothStream } from '../hooks/useSmoothStream'

// ---- Manual rAF pump ----
let rafQueue: Map<number, FrameRequestCallback>
let rafId: number
let now: number

beforeEach(() => {
  rafQueue = new Map()
  rafId = 0
  now = 0
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    rafQueue.set(++rafId, cb)
    return rafId
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue.delete(id) })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Advance one 16ms frame: run every queued rAF callback at the new timestamp. */
function frame() {
  now += 16
  const cbs = [...rafQueue.values()]
  rafQueue.clear()
  act(() => { cbs.forEach(cb => cb(now)) })
}

const CHARS_PER_FRAME = 4 // ~250 cps steady feed

describe('useSmoothStream constant-latency dynamics', () => {
  it('keeps revealing through a short inter-burst gap (no freeze at the live edge)', () => {
    let content = ''
    const { result, rerender } = renderHook(
      ({ c, s }) => useSmoothStream(c, s, true, 1),
      { initialProps: { c: content, s: true } },
    )

    // Steady feed for 60 frames (~1s) to reach equilibrium: standing backlog of
    // ~LAG_SECS worth of text, reveal rate tracking the input rate. 250 cps is
    // deliberately low: a design with a fixed reveal ceiling (~400 cps)
    // converges onto the live edge and freezes during gaps (this test fails
    // there); the controller instead holds a standing lag.
    for (let i = 0; i < 60; i++) {
      content += 'x'.repeat(CHARS_PER_FRAME)
      rerender({ c: content, s: true })
      frame()
    }
    const atEquilibrium = result.current.length
    expect(atEquilibrium).toBeGreaterThan(0)
    // The controller trails the live edge — there must be a standing backlog.
    expect(atEquilibrium).toBeLessThan(content.length)

    // GAP: the model goes quiet for 10 frames (~160ms, well under the ~400ms
    // standing lag). The reveal must keep advancing on every frame.
    const lens: number[] = [atEquilibrium]
    for (let i = 0; i < 10; i++) {
      frame() // no new content
      lens.push(result.current.length)
    }
    for (let i = 1; i < lens.length; i++) {
      expect(lens[i]).toBeGreaterThan(lens[i - 1])
    }
  })

  it('tracks a fast model with bounded, non-growing lag (no runaway backlog)', () => {
    // 500 cps is above a fixed 400 cps reveal ceiling at speed 1 — a hook with
    // that ceiling falls behind at ~100 chars/sec forever. The controller has
    // no ceiling: the rate tracks any input rate and the lag settles at
    // ~LAG_SECS worth of text.
    const fast = 8 // chars per 16ms frame ≈ 500 cps
    let content = ''
    const { result, rerender } = renderHook(
      ({ c, s }) => useSmoothStream(c, s, true, 1),
      { initialProps: { c: content, s: true } },
    )
    let backlogAt2s = 0
    for (let i = 0; i < 250; i++) {
      content += 'x'.repeat(fast)
      rerender({ c: content, s: true })
      frame()
      if (i === 124) backlogAt2s = content.length - result.current.length
    }
    const backlogAt4s = content.length - result.current.length
    // Lag must have settled (not kept growing) and stay near the design target
    // (~0.4s × 500 cps = 200 chars).
    expect(backlogAt4s).toBeLessThan(320)
    expect(backlogAt4s - backlogAt2s).toBeLessThan(60)
  })

  it('ramps a large burst over multiple frames instead of snapping it in', () => {
    let content = ''
    const { result, rerender } = renderHook(
      ({ c, s }) => useSmoothStream(c, s, true, 1),
      { initialProps: { c: content, s: true } },
    )

    // Reach equilibrium on a modest feed.
    for (let i = 0; i < 40; i++) {
      content += 'x'.repeat(CHARS_PER_FRAME)
      rerender({ c: content, s: true })
      frame()
    }

    // One paste-like burst: 600 chars in a single delta.
    const before = result.current.length
    content += 'y'.repeat(600)
    rerender({ c: content, s: true })
    frame()
    const firstDelta = result.current.length - before
    // The slew-limited rate cannot jump: the first frame after the burst must
    // mount only a small slice of it, not the whole block.
    expect(firstDelta).toBeLessThan(120)

    // And the burst must still drain in bounded time (~2s of frames).
    for (let i = 0; i < 125 && result.current.length < content.length - 50; i++) frame()
    expect(result.current.length).toBeGreaterThan(content.length - 60)
  })

  it('caps the reveal speed on a fat cold-start first chunk (no perceptual blur)', () => {
    // A large FIRST chunk (typical after a long thinking/tool phase) would
    // demand backlog/lag = thousands of cps with no ceiling — smooth in the
    // math, a blur to the eye. MAX_CPS bounds the cascade: 600 cps ≈ 10
    // chars/frame at 60fps (a 1000-char burst is under the bounded-drain
    // escape threshold, so the hard ceiling is what applies).
    const { result, rerender } = renderHook(
      ({ c, s }) => useSmoothStream(c, s, true, 1),
      { initialProps: { c: '', s: true } },
    )
    const burst = 'x'.repeat(1000)
    rerender({ c: burst, s: true })

    let prev = result.current.length
    let maxFrameDelta = 0
    for (let i = 0; i < 40; i++) {
      frame()
      maxFrameDelta = Math.max(maxFrameDelta, result.current.length - prev)
      prev = result.current.length
    }
    // ~10 chars/frame ceiling (+ integration slack). Without the cap this
    // reaches 30+ chars/frame within the slew window.
    expect(maxFrameDelta).toBeLessThanOrEqual(13)
    // And it is still making brisk progress — the cap is a ceiling, not a crawl.
    expect(result.current.length).toBeGreaterThan(200)
  })

  it('drains the residue smoothly after streaming ends — never snaps it in', () => {
    let content = ''
    const { result, rerender } = renderHook(
      ({ c, s }) => useSmoothStream(c, s, true, 1),
      { initialProps: { c: content, s: true } },
    )
    for (let i = 0; i < 30; i++) {
      content += 'x'.repeat(CHARS_PER_FRAME)
      rerender({ c: content, s: true })
      frame()
    }
    // Stream ends with a standing backlog still unrevealed (the ~LAG_SECS
    // cushion is the controller's design, so this residue ALWAYS exists).
    const atEnd = result.current.length
    expect(atEnd).toBeLessThan(content.length)
    rerender({ c: content, s: false })
    // The streaming→false commit itself must not snap (this discriminates
    // against the unguarded grow-while-not-streaming effect, which pinned to
    // full length the moment the prop flipped).
    expect(result.current.length).toBe(atEnd)

    // The residue drains over MULTIPLE frames, each revealing a bounded slice…
    let prev = atEnd
    let maxFrameDelta = 0
    let framesToDrain = 0
    for (let i = 0; i < 200 && result.current.length < content.length; i++) {
      frame()
      maxFrameDelta = Math.max(maxFrameDelta, result.current.length - prev)
      prev = result.current.length
      framesToDrain++
    }
    // …ending complete, having taken several frames (not one), with no
    // single-frame block dump.
    expect(result.current).toBe(content)
    expect(framesToDrain).toBeGreaterThan(3)
    expect(maxFrameDelta).toBeLessThan(60)
  })

  it('still snaps a variant switch on an idle, fully-drained message', () => {
    let content = ''
    const { result, rerender } = renderHook(
      ({ c, s }) => useSmoothStream(c, s, true, 1),
      { initialProps: { c: content, s: true } },
    )
    for (let i = 0; i < 20; i++) {
      content += 'x'.repeat(CHARS_PER_FRAME)
      rerender({ c: content, s: true })
      frame()
    }
    rerender({ c: content, s: false })
    for (let i = 0; i < 200 && result.current.length < content.length; i++) frame()
    expect(result.current).toBe(content) // fully drained → idle semantics restored

    // Variant switch on the idle message: render instantly, no animation.
    const variant = 'a completely different regenerated answer'
    rerender({ c: variant, s: false })
    expect(result.current).toBe(variant)
  })
})

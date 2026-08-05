import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  planReveal,
  prefersReducedMotion,
  hasRevealed,
  markRevealed,
  __resetRevealedForTests,
  REVEAL_MAX_FRAMES,
  REVEAL_MAX_SOURCE_BYTES,
  REVEAL_STEP_MS,
} from '../components/mcpAppReveal'

/** N distinct element-ish objects, the shape a diagram tool actually sends. */
function elements(n: number): { id: string }[] {
  return Array.from({ length: n }, (_, i) => ({ id: `e${i}` }))
}

describe('planReveal', () => {
  it('returns null for payloads with nothing array-shaped to reveal', () => {
    expect(planReveal(null)).toBeNull()
    expect(planReveal('a string')).toBeNull()
    expect(planReveal([1, 2, 3])).toBeNull() // top level must be the arguments OBJECT
    expect(planReveal({ url: 'https://example.com/a.pdf' })).toBeNull()
    expect(planReveal({ prose: 'not json at all' })).toBeNull()
  })

  it('returns null below three elements — the app drops each frame\'s last element', () => {
    // excalidraw's excludeIncompleteLastItem means a 1- or 2-element array has
    // no frame that both shows something AND is short of the whole diagram.
    for (const n of [1, 2]) {
      expect(planReveal({ elements: elements(n) })).toBeNull()
      expect(planReveal({ elements: JSON.stringify(elements(n)) })).toBeNull()
    }
  })

  it('reveals a JSON-STRING encoded array as growing JSON strings', () => {
    // excalidraw's create_view passes `elements` as a JSON string, and its
    // parsePartialElements tolerates truncation — so frames must stay strings.
    const plan = planReveal({ elements: JSON.stringify(elements(4)) })!
    expect(plan.key).toBe('elements')
    expect(plan.frames.length).toBe(2)
    for (const frame of plan.frames) {
      expect(typeof frame.elements).toBe('string')
    }
    const lengths = plan.frames.map((f) => (JSON.parse(f.elements as string) as unknown[]).length)
    expect(lengths).toEqual([2, 3])
  })

  it('reveals a NATIVE array as growing arrays', () => {
    const plan = planReveal({ elements: elements(3) })!
    const lengths = plan.frames.map((f) => (f.elements as unknown[]).length)
    expect(lengths).toEqual([2])
    expect(Array.isArray(plan.frames[0].elements)).toBe(true)
  })

  it('never emits the COMPLETE array in a partial frame', () => {
    // The completeness contract: `tool-input-partial` means "may change",
    // `tool-input` means complete. A partial equal to the whole payload would
    // make an app that only listens to partials believe it had the final state.
    const total = 8
    const plan = planReveal({ elements: elements(total) })!
    for (const frame of plan.frames) {
      expect((frame.elements as unknown[]).length).toBeLessThan(total)
    }
  })

  it('preserves the other arguments untouched in every frame', () => {
    const plan = planReveal({ elements: elements(3), title: 'Arch diagram', theme: 'dark' })!
    for (const frame of plan.frames) {
      expect(frame.title).toBe('Arch diagram')
      expect(frame.theme).toBe('dark')
    }
  })

  it('reveals the LARGEST array, not an incidental small one', () => {
    const plan = planReveal({ tags: ['a', 'b'], elements: elements(9) })!
    expect(plan.key).toBe('elements')
  })

  it('caps frame count and keeps prefixes strictly increasing for a big diagram', () => {
    const plan = planReveal({ elements: elements(500) })!
    expect(plan.frames.length).toBeLessThanOrEqual(REVEAL_MAX_FRAMES)
    const lengths = plan.frames.map((f) => (f.elements as unknown[]).length)
    for (let i = 1; i < lengths.length; i++) {
      expect(lengths[i]).toBeGreaterThan(lengths[i - 1])
    }
    expect(lengths[lengths.length - 1]).toBeLessThan(500)
  })

  it('declines payloads too large to re-encode per frame', () => {
    // One oversized element is enough: the guard is about encoded bytes, not count.
    const huge = [{ id: 'a', blob: 'x'.repeat(REVEAL_MAX_SOURCE_BYTES + 10) }, { id: 'b' }]
    expect(planReveal({ elements: JSON.stringify(huge) })).toBeNull()
    expect(planReveal({ elements: huge })).toBeNull()
  })

  it('counts SIBLING arguments against the size cap, not just the revealed array', () => {
    // Every frame is {...toolInput, [key]: prefix}, so each frame clones every
    // sibling too. A small array beside a huge sibling would otherwise ship
    // (frames x sibling) bytes through postMessage.
    const plan = planReveal({
      elements: elements(25),
      backdrop: 'y'.repeat(REVEAL_MAX_SOURCE_BYTES + 10),
    })
    expect(plan).toBeNull()
  })

  it('declines circular arguments instead of throwing', () => {
    const circular: Record<string, unknown> = { elements: elements(4) }
    circular.self = circular
    expect(() => planReveal(circular)).not.toThrow()
    expect(planReveal(circular)).toBeNull()
  })

  it('reveals ONE element per frame for realistic diagram sizes', () => {
    // The property that actually makes the draw-on legible. Batching (two or
    // more elements appearing in one step) reads as a jump rather than as
    // drawing, so the frame ceiling must be high enough that diagrams of a
    // realistic size never reach it. Regression guard: an earlier revision
    // capped at 60 frames, which started batching at ~62 elements.
    //
    // 82 is the exact knee: frames = n-2, capped at REVEAL_MAX_FRAMES.
    // Past that, batching is accepted deliberately — holding one element per
    // step for a 300-element diagram would run ~60s.
    //
    // Derived from the constant rather than hardcoded, so a legitimate retune
    // of the step or the total cap does not fail this for no defect.
    for (const n of [20, 40, REVEAL_MAX_FRAMES, REVEAL_MAX_FRAMES + 2]) {
      const plan = planReveal({ elements: JSON.stringify(elements(n)) })!
      const lengths = plan.frames.map(
        (f) => (JSON.parse(f.elements as string) as unknown[]).length,
      )
      const steps = lengths.slice(1).map((len, i) => len - lengths[i])
      expect(steps.every((s) => s === 1)).toBe(true)
      // And the last partial stops one short of the whole diagram.
      expect(lengths[lengths.length - 1]).toBe(n - 1)
    }
  })

  it('paces per element rather than squeezing a diagram into a fixed budget', () => {
    // The reference harness (dev-mock streamElements) uses 120ms per element.
    const small = planReveal({ elements: elements(6) })!
    const large = planReveal({ elements: elements(400) })!
    expect(small.stepMs).toBe(REVEAL_STEP_MS)
    expect(large.stepMs).toBe(REVEAL_STEP_MS)
    // A small diagram gets one element per frame rather than batching.
    expect(small.frames.map((f) => (f.elements as unknown[]).length)).toEqual([2, 3, 4, 5])
  })

  it('bounds total cloned bytes by trading frames against payload size', () => {
    // A fat-but-legal payload animates in fewer, larger steps instead of
    // cloning frames x payload without limit.
    const fat = planReveal({
      elements: elements(300),
      backdrop: 'z'.repeat(400_000),
    })!
    expect(fat.frames.length).toBeLessThan(REVEAL_MAX_FRAMES)
    expect(fat.frames.length).toBeGreaterThan(0)
  })
})

describe('revealed-spool cache', () => {
  afterEach(() => {
    __resetRevealedForTests()
  })

  it('evicts the OLDEST entry at the cap, not the whole set', () => {
    // Clearing wholesale would let every already-seen app re-animate after the
    // cap is crossed — the exact history-replay this cache prevents.
    __resetRevealedForTests()
    markRevealed('first')
    for (let i = 0; i < 300; i++) markRevealed(`spool-${i}`)
    // 'first' is gone (evicted as oldest), but recent entries survive.
    expect(hasRevealed('first')).toBe(false)
    expect(hasRevealed('spool-299')).toBe(true)
    expect(hasRevealed('spool-298')).toBe(true)
  })
})

describe('prefersReducedMotion', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reports the reduce preference when the media query matches', () => {
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: true }))
    expect(prefersReducedMotion()).toBe(true)
  })

  it('defaults to false when matchMedia is unavailable or throws', () => {
    vi.stubGlobal('matchMedia', undefined)
    expect(prefersReducedMotion()).toBe(false)
    vi.stubGlobal('matchMedia', vi.fn().mockImplementation(() => { throw new Error('nope') }))
    expect(prefersReducedMotion()).toBe(false)
  })
})

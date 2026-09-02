import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createLevelMeter, createAudioSample } from '../hooks/mic'

/**
 * The additive half of `createLevelMeter`: one analyser feeding TWO consumers
 * with different needs — a throttled/quantized callback for the React bar, and
 * an unthrottled in-place struct for the shader's render loop.
 *
 * These tests drive the RAF loop by hand so the envelope's time behaviour is
 * deterministic (real rAF timing would make attack/release untestable).
 */

type RafCb = (t: number) => void

let frames: RafCb[]
let analyserTimeData: number[]
let analyserFreqData: number[]
let closed: boolean

/** Fill the time-domain buffer with a constant deviation from the 128 midpoint,
 * which produces a predictable RMS. */
function setLoudness(dev: number) {
  analyserTimeData = new Array(512).fill(128 + dev)
}

beforeEach(() => {
  frames = []
  closed = false
  setLoudness(0)
  analyserFreqData = new Array(256).fill(0)

  vi.stubGlobal('requestAnimationFrame', (cb: RafCb) => { frames.push(cb); return frames.length })
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  vi.stubGlobal('AudioContext', class {
    createMediaStreamSource() { return { connect: vi.fn() } }
    createAnalyser() {
      return {
        fftSize: 0,
        frequencyBinCount: 256,
        connect: vi.fn(),
        getByteTimeDomainData: (b: Uint8Array) => { b.set(analyserTimeData.slice(0, b.length)) },
        getByteFrequencyData: (b: Uint8Array) => { b.set(analyserFreqData.slice(0, b.length)) },
      }
    }
    close() { closed = true }
  })
})
afterEach(() => { vi.unstubAllGlobals() })

/** Run the single pending frame at time `t`. */
function step(t: number) {
  const cb = frames.pop()
  if (!cb) throw new Error('no pending animation frame')
  frames = []
  cb(t)
}

const stream = {} as MediaStream

describe('createLevelMeter — throttled bar signal (pre-existing contract)', () => {
  it('quantizes to 25 steps and emits at most ~15fps', () => {
    const onLevel = vi.fn()
    createLevelMeter(stream, onLevel)
    setLoudness(64) // rms ~0.5 * 2.2 gain -> clamps high
    step(100)
    expect(onLevel).toHaveBeenCalledTimes(1)
    const first = onLevel.mock.calls[0][0]
    expect(first).toBeCloseTo(Math.round(first * 25) / 25, 10)
    // A second frame 20ms later is inside the throttle window -> no emit.
    step(120)
    expect(onLevel).toHaveBeenCalledTimes(1)
  })

  it('works with no sampleRef at all (callers that only draw the bar)', () => {
    const onLevel = vi.fn()
    expect(() => { createLevelMeter(stream, onLevel); step(16) }).not.toThrow()
  })
})

describe('createLevelMeter — per-frame AudioSample for the shader', () => {
  it('writes into the caller-owned ref IN PLACE (identity is stable)', () => {
    const ref = { current: createAudioSample() }
    const original = ref.current
    createLevelMeter(stream, vi.fn(), ref)
    setLoudness(32)
    step(16)
    step(32)
    // Same object mutated, never replaced: the render loop holds this reference.
    expect(ref.current).toBe(original)
    expect(ref.current.level).toBeGreaterThan(0)
  })

  it('is not throttled — every frame advances the envelope', () => {
    const ref = { current: createAudioSample() }
    createLevelMeter(stream, vi.fn(), ref)
    setLoudness(64)
    step(16)
    const after1 = ref.current.level
    step(32) // well inside the 66ms bar-throttle window
    expect(ref.current.level).toBeGreaterThan(after1)
  })

  it('attacks faster than it releases (the anti-strobe property)', () => {
    const ref = { current: createAudioSample() }
    createLevelMeter(stream, vi.fn(), ref)

    // Rise from silence over one 20ms frame.
    setLoudness(64)
    step(20)
    step(40)
    const peak = ref.current.level
    expect(peak).toBeGreaterThan(0)

    // Now go silent and measure the fall over an identical 20ms frame.
    const beforeFall = ref.current.level
    setLoudness(0)
    step(60)
    const fallen = beforeFall - ref.current.level

    // Re-run the rise in a fresh meter to compare like for like.
    const ref2 = { current: createAudioSample() }
    createLevelMeter(stream, vi.fn(), ref2)
    setLoudness(64)
    step(20)
    step(40)
    const risen = ref2.current.level

    // 50ms attack vs 250ms release => the rise covers much more ground.
    expect(risen).toBeGreaterThan(fallen)
  })

  it('does not jump the envelope on the first frame or after a background gap', () => {
    const ref = { current: createAudioSample() }
    createLevelMeter(stream, vi.fn(), ref)
    setLoudness(64)
    step(1000) // first frame at a large timestamp: dt must be treated as 0
    expect(ref.current.level).toBe(0)
    // A 30s gap is clamped to the 50ms ceiling, so level cannot snap to full.
    step(31000)
    expect(ref.current.level).toBeGreaterThan(0)
    expect(ref.current.level).toBeLessThan(1)
  })

  it('reports a spectral centroid that rises with high-frequency energy', () => {
    const ref = { current: createAudioSample() }
    createLevelMeter(stream, vi.fn(), ref)
    setLoudness(20)
    // Energy concentrated in the low bins.
    analyserFreqData = new Array(256).fill(0).map((_, i) => (i < 20 ? 200 : 0))
    for (const t of [16, 32, 48, 64, 80, 96]) step(t)
    const low = ref.current.centroid
    // Now concentrated high.
    analyserFreqData = new Array(256).fill(0).map((_, i) => (i > 200 ? 200 : 0))
    for (const t of [112, 128, 144, 160, 176, 192, 208, 224]) step(t)
    expect(ref.current.centroid).toBeGreaterThan(low)
  })

  it('produces an onset spike on a transient and decays it', () => {
    const ref = { current: createAudioSample() }
    createLevelMeter(stream, vi.fn(), ref)
    setLoudness(0)
    step(16)
    setLoudness(90) // sudden loud attack
    step(32)
    const spike = ref.current.onset
    expect(spike).toBeGreaterThan(0)
    // Hold the same loudness: derivative is 0, so the spike must decay away.
    step(48)
    step(64)
    step(80)
    expect(ref.current.onset).toBeLessThan(spike)
  })

  it('zeroes the sample and closes the context on stop', () => {
    const ref = { current: createAudioSample() }
    const onLevel = vi.fn()
    const stop = createLevelMeter(stream, onLevel, ref)
    setLoudness(64)
    step(16)
    step(32)
    expect(ref.current.level).toBeGreaterThan(0)
    stop()
    // A stale non-zero level would leave the shader lit after recording ended.
    expect(ref.current.level).toBe(0)
    expect(ref.current.onset).toBe(0)
    expect(onLevel).toHaveBeenLastCalledWith(0)
    expect(closed).toBe(true)
  })

  it('stops advancing the sample after stop()', () => {
    const ref = { current: createAudioSample() }
    const stop = createLevelMeter(stream, vi.fn(), ref)
    setLoudness(64)
    step(16)
    stop()
    // The loop scheduled another frame before we stopped; running it must no-op.
    if (frames.length) step(32)
    expect(ref.current.level).toBe(0)
  })

  it('survives an unavailable AudioContext without throwing', () => {
    vi.stubGlobal('AudioContext', class { constructor() { throw new Error('no audio') } })
    const ref = { current: createAudioSample() }
    let stop!: () => void
    expect(() => { stop = createLevelMeter(stream, vi.fn(), ref) }).not.toThrow()
    expect(() => stop()).not.toThrow()
    expect(ref.current.level).toBe(0)
  })
})

/**
 * SpriteRenderer frame auto-detection — when `totalFrames` is omitted the
 * renderer probes the strip back-to-front with getImageData and skips empty
 * trailing frames. These tests pin that detection path: the probe context is
 * requested with `willReadFrequently`, probing stops at the first frame with
 * content, and the animation loop then cycles only the detected frames.
 */
import React from 'react'
import { render, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SpriteRenderer } from '../apps/shared/SpriteRenderer'

const FRAME = 64
const BYTES_PER_FRAME = FRAME * FRAME * 4

/** Deterministic clock shared by rAF, setTimeout, and performance.now. */
let now = 0
let rafQueue = new Map<number, FrameRequestCallback>()
let rafHandle = 0

function tickFrame(ms: number) {
  now += ms
  vi.advanceTimersByTime(ms)
  const pending = rafQueue
  rafQueue = new Map()
  pending.forEach(cb => cb(now))
}

function makeCtx() {
  return {
    clearRect: vi.fn(),
    drawImage: vi.fn(),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(BYTES_PER_FRAME) })),
  }
}

let displayCtx: ReturnType<typeof makeCtx>
let probeCtx: ReturnType<typeof makeCtx>
/** Strip width the fake image reports, in frames. */
let stripFrames = 4

class FakeImage {
  src = ''
  get naturalWidth() { return stripFrames * FRAME }
  addEventListener(type: string, cb: EventListenerOrEventListenerObject) {
    // Fire `load` synchronously so the effect body runs inside the test scope.
    if (type === 'load') (cb as EventListener)(new Event('load'))
  }
  removeEventListener() {}
}

beforeEach(() => {
  vi.useFakeTimers()
  now = 0
  rafQueue = new Map()
  rafHandle = 0
  stripFrames = 4
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    rafHandle += 1
    rafQueue.set(rafHandle, cb)
    return rafHandle
  })
  vi.stubGlobal('cancelAnimationFrame', (h: number) => { rafQueue.delete(h) })
  vi.stubGlobal('Image', FakeImage)
  vi.spyOn(performance, 'now').mockImplementation(() => now)

  displayCtx = makeCtx()
  probeCtx = makeCtx()
  // The probe context is the one requested with `willReadFrequently`; the
  // display canvas context is requested with no options.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    function (_id: string, opts?: unknown) {
      const probing = Boolean((opts as { willReadFrequently?: boolean } | undefined)?.willReadFrequently)
      return (probing ? probeCtx : displayCtx) as unknown as CanvasRenderingContext2D
    } as typeof HTMLCanvasElement.prototype.getContext,
  )
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('SpriteRenderer frame auto-detection (no totalFrames prop)', () => {
  it('skips empty trailing frames and animates only the detected range', () => {
    // 4-frame strip: frames 0-1 have content, frames 2-3 are fully transparent.
    // The probe draws frame i at source-x = i * FRAME; answer by that offset.
    probeCtx.getImageData.mockImplementation(() => {
      const calls = probeCtx.drawImage.mock.calls
      const sx = calls[calls.length - 1][1] as number
      const data = new Uint8ClampedArray(BYTES_PER_FRAME)
      if (sx <= FRAME) data.fill(255)
      return { data }
    })

    render(<SpriteRenderer src="strip.png" frameWidth={FRAME} frameHeight={FRAME} fps={8} />)

    // Back-to-front probe: frame 3 (empty), frame 2 (empty), frame 1 (content) — then stop.
    expect(probeCtx.getImageData).toHaveBeenCalledTimes(3)
    const probedSx = probeCtx.drawImage.mock.calls.map(c => c[1])
    expect(probedSx).toEqual([3 * FRAME, 2 * FRAME, 1 * FRAME])

    // 1 second at 8fps: every animated draw must come from frames 0-1 only.
    for (let i = 0; i < 120; i++) tickFrame(1000 / 120)
    const animatedSx = displayCtx.drawImage.mock.calls.map(c => c[1])
    expect(animatedSx.length).toBeGreaterThanOrEqual(4)
    expect(new Set(animatedSx)).toEqual(new Set([0, FRAME]))
  })

  it('keeps every frame when no probed frame has content', () => {
    // Fully transparent strip: the probe exhausts without a match, so the
    // renderer falls back to animating the full detected width.
    render(<SpriteRenderer src="blank.png" frameWidth={FRAME} frameHeight={FRAME} fps={8} />)

    // All three probe-able frames sampled (3, 2, 1), none matched.
    expect(probeCtx.getImageData).toHaveBeenCalledTimes(3)

    for (let i = 0; i < 240; i++) tickFrame(1000 / 120)
    const animatedSx = new Set(displayCtx.drawImage.mock.calls.map(c => c[1]))
    expect(animatedSx).toEqual(new Set([0, FRAME, 2 * FRAME, 3 * FRAME]))
  })

  it('renders a sub-frame-width strip as a single static frame without probing', () => {
    stripFrames = 0 // naturalWidth 0: nothing to probe, clamps to one frame
    render(<SpriteRenderer src="tiny.png" frameWidth={FRAME} frameHeight={FRAME} fps={8} />)

    expect(probeCtx.getImageData).not.toHaveBeenCalled()
    expect(displayCtx.drawImage).toHaveBeenCalledTimes(1)
    const scheduled = rafQueue.size
    for (let i = 0; i < 120; i++) tickFrame(1000 / 120)
    expect(rafQueue.size).toBe(scheduled)
    expect(displayCtx.drawImage).toHaveBeenCalledTimes(1)
  })
})

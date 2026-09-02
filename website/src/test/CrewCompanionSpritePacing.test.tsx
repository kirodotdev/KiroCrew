/**
 * SpriteRenderer scheduling — the animation loop must wake at the sprite's
 * fps, not the display's refresh rate, and must not run at all for a
 * single-frame (static) sprite.
 *
 * The pet overlay window disables backgroundThrottling, so nothing external
 * ever throttles this loop: a wakeup scheduled here runs forever. These tests
 * pin the wakeup *count*, because that is the resource being spent.
 */
import React from 'react'
import { render, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SpriteRenderer } from '../apps/crew-companion/SpriteRenderer'

/** Deterministic clock shared by rAF, setTimeout, and performance.now. */
let now = 0
/** Pending rAF callbacks, keyed by handle. */
let rafQueue = new Map<number, FrameRequestCallback>()
let rafHandle = 0
let rafCalls = 0

/** Run one display frame: advance the clock and fire pending rAF callbacks. */
function tickFrame(ms: number) {
  now += ms
  vi.advanceTimersByTime(ms)
  const pending = rafQueue
  rafQueue = new Map()
  pending.forEach(cb => cb(now))
}

function make2dContext() {
  return {
    clearRect: vi.fn(),
    drawImage: vi.fn(),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(64 * 64 * 4).fill(255) })),
  }
}

let drawCtx: ReturnType<typeof make2dContext>

beforeEach(() => {
  vi.useFakeTimers()
  now = 0
  rafQueue = new Map()
  rafHandle = 0
  rafCalls = 0
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    rafCalls += 1
    rafHandle += 1
    rafQueue.set(rafHandle, cb)
    return rafHandle
  })
  vi.stubGlobal('cancelAnimationFrame', (h: number) => { rafQueue.delete(h) })
  vi.spyOn(performance, 'now').mockImplementation(() => now)

  drawCtx = make2dContext()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => drawCtx as unknown as CanvasRenderingContext2D,
  )
  // Sprite strips load through an Image(); fire `load` synchronously so the
  // effect body runs inside the test's fake-timer scope.
  vi.spyOn(Image.prototype, 'addEventListener').mockImplementation(
    function (this: HTMLImageElement, type: string, cb: EventListenerOrEventListenerObject) {
      if (type === 'load') (cb as EventListener)(new Event('load'))
    },
  )
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('crew-companion SpriteRenderer wakeup pacing', () => {
  it('wakes at the sprite fps, not once per display frame', () => {
    render(
      <SpriteRenderer src="strip.png" frameWidth={64} frameHeight={64} fps={8} totalFrames={4} />,
    )
    const baseline = rafCalls
    // Simulate 1 second of a 120Hz display: 120 frames of ~8.33ms.
    for (let i = 0; i < 120; i++) tickFrame(1000 / 120)
    const wakeups = rafCalls - baseline
    // At 8fps over 1s the loop needs ~8 wakeups; the old vsync-chained loop
    // took ~120. Allow slack for timer/vsync alignment, but pin the order of
    // magnitude.
    expect(wakeups).toBeGreaterThanOrEqual(6)
    expect(wakeups).toBeLessThanOrEqual(20)
  })

  it('advances frames at the configured fps while paced', () => {
    render(
      <SpriteRenderer src="strip.png" frameWidth={64} frameHeight={64} fps={8} totalFrames={4} />,
    )
    drawCtx.drawImage.mockClear()
    for (let i = 0; i < 120; i++) tickFrame(1000 / 120)
    // ~8 draws expected in 1s at 8fps (first draw lands one interval in).
    expect(drawCtx.drawImage.mock.calls.length).toBeGreaterThanOrEqual(6)
    expect(drawCtx.drawImage.mock.calls.length).toBeLessThanOrEqual(10)
  })

  it('draws a single-frame sprite once and schedules no loop', () => {
    render(
      <SpriteRenderer src="static.png" frameWidth={64} frameHeight={64} fps={8} totalFrames={1} />,
    )
    expect(drawCtx.drawImage).toHaveBeenCalledTimes(1)
    const baseline = rafCalls
    for (let i = 0; i < 120; i++) tickFrame(1000 / 120)
    expect(rafCalls).toBe(baseline)
    expect(drawCtx.drawImage).toHaveBeenCalledTimes(1)
  })

  it('stops scheduling after unmount (no timer or rAF leak)', () => {
    const { unmount } = render(
      <SpriteRenderer src="strip.png" frameWidth={64} frameHeight={64} fps={8} totalFrames={4} />,
    )
    for (let i = 0; i < 12; i++) tickFrame(1000 / 120)
    unmount()
    const baseline = rafCalls
    drawCtx.drawImage.mockClear()
    for (let i = 0; i < 120; i++) tickFrame(1000 / 120)
    expect(rafCalls).toBe(baseline)
    expect(drawCtx.drawImage).not.toHaveBeenCalled()
  })
})

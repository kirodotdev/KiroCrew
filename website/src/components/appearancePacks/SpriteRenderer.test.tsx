/**
 * SpriteRenderer — which pixels it samples out of the sheet.
 *
 * The scheduling half (wake at the sprite's fps, never for a static frame) is
 * pinned by `test/CrewCompanionSpritePacing.test.tsx`. What is here is the other
 * half: WHERE in the sheet a frame comes from, which is what a multi-row pack
 * depends on, and how many frames a row really has when the author padded it.
 */
import { cleanup, render } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SpriteRenderer } from './SpriteRenderer'

const FW = 16
const FH = 16

/** `drawImage` calls, as (sx, sy) source offsets. */
let draws: { sx: number; sy: number }[] = []
/** Alpha the stubbed `getImageData` reports, per probed frame index. */
let frameAlpha: (frameIndex: number) => number

function context() {
  return {
    clearRect: vi.fn(),
    drawImage: vi.fn((_img: unknown, sx: number, sy: number) => {
      draws.push({ sx, sy })
    }),
    getImageData: vi.fn(() => {
      // The detection loop probes frames from the last one backwards, so answer
      // from the LAST recorded draw's source x.
      const last = draws[draws.length - 1]
      const index = last ? Math.round(last.sx / FW) : 0
      const data = new Uint8ClampedArray(FW * FH * 4)
      data.fill(frameAlpha(index))
      return { data }
    }),
  }
}

let ctx: ReturnType<typeof context>

beforeEach(() => {
  vi.useFakeTimers()
  draws = []
  frameAlpha = () => 255
  ctx = context()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ctx as unknown as CanvasRenderingContext2D,
  )
  // A sheet loads through an Image(); fire `load` synchronously so the effect
  // body runs inside the test.
  vi.spyOn(Image.prototype, 'addEventListener').mockImplementation(
    function (this: HTMLImageElement, type: string, cb: EventListenerOrEventListenerObject) {
      if (type === 'load') (cb as EventListener)(new Event('load'))
    },
  )
  Object.defineProperty(Image.prototype, 'naturalWidth', { value: FW * 4, configurable: true })
  Object.defineProperty(Image.prototype, 'naturalHeight', { value: FH * 2, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('row selection', () => {
  it('samples row 0 by default, so a single-row strip is unchanged', () => {
    render(
      <SpriteRenderer src="strip.png" frameWidth={FW} frameHeight={FH} totalFrames={1} />,
    )
    expect(draws).toEqual([{ sx: 0, sy: 0 }])
  })

  it('offsets by the row it is told to draw', () => {
    // `rowAssignments` maps a slot to its row, so one sheet carries idle on row 0
    // and working on row 1.
    render(
      <SpriteRenderer src="sheet.png" frameWidth={FW} frameHeight={FH} totalFrames={1} row={1} />,
    )
    expect(draws).toEqual([{ sx: 0, sy: FH }])
  })

  it('refuses a negative row rather than sampling above the sheet', () => {
    render(
      <SpriteRenderer src="sheet.png" frameWidth={FW} frameHeight={FH} totalFrames={1} row={-4} />,
    )
    expect(draws).toEqual([{ sx: 0, sy: 0 }])
  })

  it('probes the assigned row when it counts the row own frames', () => {
    // The empty-frame detection must read the SAME row it will animate;
    // probing row 0 would count a different clip's frames.
    render(<SpriteRenderer src="sheet.png" frameWidth={FW} frameHeight={FH} row={1} />)
    expect(draws.length).toBeGreaterThan(0)
    expect(draws.every((d) => d.sy === FH)).toBe(true)
  })
})

describe('frame counting', () => {
  it('trusts an explicit totalFrames and probes nothing', () => {
    render(
      <SpriteRenderer src="strip.png" frameWidth={FW} frameHeight={FH} fps={8} totalFrames={4} />,
    )
    // Straight to the first animated frame — no detection pass.
    expect(ctx.getImageData).not.toHaveBeenCalled()
  })

  it('skips empty trailing frames when the author padded the row', () => {
    // The sheet is 4 frames wide but only the first two are drawn; animating the
    // blanks reads as a flicker.
    frameAlpha = (index) => (index <= 1 ? 255 : 0)
    render(<SpriteRenderer src="strip.png" frameWidth={FW} frameHeight={FH} fps={8} />)
    // Probed 3 and 2 (both empty), found content at 1, so the row is 2 frames.
    expect(ctx.getImageData).toHaveBeenCalledTimes(3)
  })

  it('animates a fully-drawn row without probing past its last frame', () => {
    frameAlpha = () => 255
    render(<SpriteRenderer src="strip.png" frameWidth={FW} frameHeight={FH} fps={8} />)
    expect(ctx.getImageData).toHaveBeenCalledTimes(1)
  })
})

describe('a row the sheet does not have', () => {
  it('reports it rather than drawing a transparent frame', () => {
    // The sheet DECODED, so `error` never fires -- but `rowAssignments` is
    // third-party data and can name a row past the image. Off-image sampling
    // draws nothing, and nothing would have told the caller.
    const onError = vi.fn()
    render(
      <SpriteRenderer src="sheet.png" frameWidth={FW} frameHeight={FH} totalFrames={1} row={2} onError={onError} />,
    )
    expect(onError).toHaveBeenCalledTimes(1)
    expect(draws).toEqual([])
  })

  it('still draws the last row that fits', () => {
    const onError = vi.fn()
    render(
      <SpriteRenderer src="sheet.png" frameWidth={FW} frameHeight={FH} totalFrames={1} row={1} onError={onError} />,
    )
    expect(onError).not.toHaveBeenCalled()
    expect(draws).toEqual([{ sx: 0, sy: FH }])
  })
})

describe('geometry the sheet cannot be cut by', () => {
  // The frame count is `naturalWidth / frameWidth`; a width of 0 makes it
  // Infinity, and the trailing-frame scan walks down from Infinity on the main
  // thread. A hand-written manifest can say `"0"`, and `"0" >= 1` is false
  // exactly like `0 >= 1`, so the string form is refused by the same check.
  it.each([
    ['a zero width', 0, FH],
    ['a negative width', -16, FH],
    ['a NaN height', FW, NaN],
    ['a string width', '0' as unknown as number, FH],
    ['a fractional width below one', 0.5, FH],
  ])('refuses %s before fetching the sheet, and reports it', (_label, w, h) => {
    const onError = vi.fn()
    const created = vi.spyOn(Image.prototype, 'addEventListener')
    created.mockClear()
    render(<SpriteRenderer src="sheet.png" frameWidth={w} frameHeight={h} onError={onError} />)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(created).not.toHaveBeenCalled()
    expect(draws).toEqual([])
  })

  it('refuses a row of more frames than any strip holds, before scanning it', () => {
    // The empty-frame scan is one synchronous getImageData per candidate frame;
    // its length is naturalWidth / frameWidth. A 1px frame over a wide transparent
    // sheet is thousands of readbacks with no early exit — refused instead.
    Object.defineProperty(Image.prototype, 'naturalWidth', { value: 2000, configurable: true })
    const onError = vi.fn()
    render(<SpriteRenderer src="wide.png" frameWidth={1} frameHeight={FH} onError={onError} />)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(ctx.getImageData).not.toHaveBeenCalled()
    expect(draws).toEqual([])
  })

  it('still scans a wide row that fits the ceiling', () => {
    Object.defineProperty(Image.prototype, 'naturalWidth', { value: 512 * FW, configurable: true })
    frameAlpha = (i) => (i === 0 ? 255 : 0)
    const onError = vi.fn()
    render(<SpriteRenderer src="wide.png" frameWidth={FW} frameHeight={FH} onError={onError} />)
    expect(onError).not.toHaveBeenCalled()
    expect(draws.length).toBeGreaterThan(0)
  })

  it('accepts a one-pixel frame, the smallest real one', () => {
    const onError = vi.fn()
    render(<SpriteRenderer src="sheet.png" frameWidth={1} frameHeight={1} totalFrames={1} onError={onError} />)
    expect(onError).not.toHaveBeenCalled()
    expect(draws).toHaveLength(1)
  })
})

describe('a sheet that will not load', () => {
  it('reports the failure to its caller', () => {
    // Without this, a missing or corrupt sheet left an all-transparent canvas and
    // the caller believed it had drawn a face.
    vi.restoreAllMocks()
    const listeners: Record<string, EventListener> = {}
    vi.spyOn(Image.prototype, 'addEventListener').mockImplementation(
      function (this: HTMLImageElement, type: string, cb: EventListenerOrEventListenerObject) {
        listeners[type] = cb as EventListener
      },
    )
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
      () => ctx as unknown as CanvasRenderingContext2D,
    )
    const onError = vi.fn()
    render(<SpriteRenderer src="missing.png" frameWidth={FW} frameHeight={FH} onError={onError} />)

    listeners.error?.(new Event('error'))
    expect(onError).toHaveBeenCalledTimes(1)
    expect(draws).toEqual([])
  })
})

describe('still frames', () => {
  it('draws once and starts no loop when asked not to play', () => {
    // What a roster renders for an off-screen avatar: one drawImage, no timers.
    const raf = vi.spyOn(globalThis, 'requestAnimationFrame')
    render(
      <SpriteRenderer
        src="strip.png"
        frameWidth={FW}
        frameHeight={FH}
        fps={8}
        totalFrames={4}
        playing={false}
      />,
    )
    expect(draws).toEqual([{ sx: 0, sy: 0 }])
    expect(raf).not.toHaveBeenCalled()
  })

  it('sizes the canvas to the frame and scales it to the display box', () => {
    const { container } = render(
      <SpriteRenderer
        src="strip.png"
        frameWidth={FW}
        frameHeight={FH}
        totalFrames={1}
        displaySize={38}
      />,
    )
    const canvas = container.querySelector('canvas')!
    // The backing store stays at the frame's own resolution; only the CSS box
    // scales, which is what keeps pixel art crisp.
    expect(canvas.getAttribute('width')).toBe(String(FW))
    expect(canvas.style.width).toBe('38px')
    expect(canvas.style.imageRendering).toBe('pixelated')
  })
})

import { describe, it, expect, vi } from 'vitest'
import { isScreenSnipSupported, normalizeRect, canvasToFile, captureScreen, cropCanvas, currentTabCaptureDeps } from '../hooks/useScreenSnip'

describe('currentTabCaptureDeps', () => {
  it('requests getDisplayMedia with preferCurrentTab (streamlined browser prompt)', () => {
    const getDisplayMedia = vi.fn(() => Promise.resolve({} as MediaStream))
    vi.stubGlobal('navigator', { mediaDevices: { getDisplayMedia } })
    try {
      void currentTabCaptureDeps().getDisplayMedia()
      expect(getDisplayMedia).toHaveBeenCalledWith(expect.objectContaining({ video: true, preferCurrentTab: true }))
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('isScreenSnipSupported', () => {
  it('is true when getDisplayMedia is a function', () => {
    expect(isScreenSnipSupported({ mediaDevices: { getDisplayMedia: () => {} } })).toBe(true)
  })
  it('is false when getDisplayMedia is missing', () => {
    expect(isScreenSnipSupported({ mediaDevices: {} })).toBe(false)
  })
  it('is false when mediaDevices is missing (mobile/webview)', () => {
    expect(isScreenSnipSupported({})).toBe(false)
  })
})

describe('normalizeRect', () => {
  const bounds = { width: 100, height: 80 }

  it('computes x/y/width/height for a top-left to bottom-right drag', () => {
    expect(normalizeRect({ x: 10, y: 20 }, { x: 40, y: 50 }, bounds))
      .toEqual({ x: 10, y: 20, width: 30, height: 30 })
  })

  it('normalizes a reversed (bottom-right to top-left) drag to a positive rect', () => {
    expect(normalizeRect({ x: 40, y: 50 }, { x: 10, y: 20 }, bounds))
      .toEqual({ x: 10, y: 20, width: 30, height: 30 })
  })

  it('clamps a rect that extends past the source bounds', () => {
    expect(normalizeRect({ x: 90, y: 70 }, { x: 200, y: 200 }, bounds))
      .toEqual({ x: 90, y: 70, width: 10, height: 10 })
  })

  it('clamps a negative origin to zero', () => {
    expect(normalizeRect({ x: -30, y: -10 }, { x: 20, y: 20 }, bounds))
      .toEqual({ x: 0, y: 0, width: 20, height: 20 })
  })

  it('floors fractional coordinates to integers', () => {
    expect(normalizeRect({ x: 10.7, y: 20.2 }, { x: 40.9, y: 50.6 }, bounds))
      .toEqual({ x: 10, y: 20, width: 31, height: 31 })
  })
})

describe('canvasToFile', () => {
  // Duck-typed canvas: canvasToFile only needs toBlob.
  const fakeCanvas = (blob: Blob) =>
    ({ toBlob: (cb: (b: Blob | null) => void) => cb(blob) } as unknown as HTMLCanvasElement)

  it('resolves an image/png File', async () => {
    const file = await canvasToFile(fakeCanvas(new Blob(['x'], { type: 'image/png' })))
    expect(file).toBeInstanceOf(File)
    expect(file.type).toBe('image/png')
  })

  it('defaults to a snip-<timestamp>.png name', async () => {
    const file = await canvasToFile(fakeCanvas(new Blob(['x'], { type: 'image/png' })))
    expect(file.name).toMatch(/^snip-\d+\.png$/)
  })

  it('uses a provided name', async () => {
    const file = await canvasToFile(fakeCanvas(new Blob(['x'], { type: 'image/png' })), 'region.png')
    expect(file.name).toBe('region.png')
  })

  it('rejects when toBlob yields null', async () => {
    const nullCanvas = { toBlob: (cb: (b: Blob | null) => void) => cb(null) } as unknown as HTMLCanvasElement
    await expect(canvasToFile(nullCanvas)).rejects.toThrow()
  })
})

describe('captureScreen', () => {
  function fakeDeps() {
    const stop = vi.fn()
    const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream
    const drawImage = vi.fn()
    const ctx = { drawImage } as unknown as CanvasRenderingContext2D
    const canvas = { width: 0, height: 0, getContext: () => ctx } as unknown as HTMLCanvasElement
    const video = {
      videoWidth: 320,
      videoHeight: 200,
      srcObject: null as unknown,
      play: vi.fn().mockResolvedValue(undefined),
    } as unknown as HTMLVideoElement
    const getDisplayMedia = vi.fn().mockResolvedValue(stream)
    return {
      deps: { getDisplayMedia, createVideo: () => video, createCanvas: () => canvas },
      stop, drawImage, canvas, getDisplayMedia, video,
    }
  }

  it('captures a frame sized to the video and draws it', async () => {
    const f = fakeDeps()
    const out = await captureScreen(f.deps)
    expect(f.getDisplayMedia).toHaveBeenCalled()
    expect(out).toBe(f.canvas)
    expect(f.canvas.width).toBe(320)
    expect(f.canvas.height).toBe(200)
    expect(f.drawImage).toHaveBeenCalledWith(f.video, 0, 0)
  })

  it('stops the capture stream tracks so no share indicator lingers', async () => {
    const f = fakeDeps()
    await captureScreen(f.deps)
    expect(f.stop).toHaveBeenCalledTimes(1)
  })

  it('returns null when the user cancels or denies the picker', async () => {
    const getDisplayMedia = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    const out = await captureScreen({
      getDisplayMedia,
      createVideo: () => ({}) as HTMLVideoElement,
      createCanvas: () => ({}) as HTMLCanvasElement,
    })
    expect(out).toBeNull()
  })

  it('returns null and stops tracks when the frame grab fails', async () => {
    const f = fakeDeps()
    ;(f.video.play as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('play failed'))
    const out = await captureScreen(f.deps)
    expect(out).toBeNull()
    expect(f.stop).toHaveBeenCalledTimes(1)
  })
})

describe('cropCanvas', () => {
  it('crops the source sub-region into a rect-sized canvas', () => {
    const drawImage = vi.fn()
    const canvas = { width: 0, height: 0, getContext: () => ({ drawImage }) } as unknown as HTMLCanvasElement
    const source = {} as CanvasImageSource
    const out = cropCanvas(source, { x: 5, y: 8, width: 20, height: 12 }, () => canvas)
    expect(out).toBe(canvas)
    expect(canvas.width).toBe(20)
    expect(canvas.height).toBe(12)
    // Source-rect (sx,sy,sw,sh) → dest (0,0,sw,sh)
    expect(drawImage).toHaveBeenCalledWith(source, 5, 8, 20, 12, 0, 0, 20, 12)
  })
})

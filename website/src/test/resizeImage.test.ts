import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { resizeImageForModel, MODEL_IMAGE_LIMITS } from '../utils/resizeImage'

/**
 * jsdom implements neither createImageBitmap nor a real canvas 2d context, so
 * we stub them. The stubs let us assert the decision logic, scaled dimensions,
 * and the byte-cap shrink path without a real imaging backend.
 */

const makeFile = (type: string, name = 'shot', size = 3) =>
  new File([new Uint8Array(size)], name, { type })

let renders: Array<{ w: number; h: number; mime: string }>

/**
 * Stub imaging. `blobSize(mime)` lets a test control the encoded size per
 * format so we can exercise the PNG->JPEG byte-cap fallback.
 */
function stubImaging(bitmapW: number, bitmapH: number, blobSize: (mime: string) => number = () => 10) {
  renders = []
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn().mockResolvedValue({ width: bitmapW, height: bitmapH, close: vi.fn() }),
  )
  vi.stubGlobal('OffscreenCanvas', undefined) // force the HTMLCanvasElement path
  vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
    if (tag !== 'canvas') return {} as HTMLElement
    const canvas: Record<string, unknown> = {
      width: 0,
      height: 0,
      getContext: () => ({ drawImage: vi.fn() }),
      toBlob: (cb: (b: Blob | null) => void, type: string) => {
        renders.push({ w: canvas.width as number, h: canvas.height as number, mime: type })
        cb(new Blob([new Uint8Array(blobSize(type))], { type }))
      },
    }
    return canvas as unknown as HTMLElement
  }) as typeof document.createElement)
}

const KB = 1024
const MB = 1024 * 1024

describe('resizeImageForModel', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => vi.unstubAllGlobals())

  it('returns SVG unchanged (vector, no raster dims)', async () => {
    const f = makeFile('image/svg+xml', 'icon.svg')
    expect(await resizeImageForModel(f)).toEqual({ file: f, info: null })
  })

  it('returns GIF unchanged (canvas would flatten animation)', async () => {
    const f = makeFile('image/gif', 'anim.gif')
    expect(await resizeImageForModel(f)).toEqual({ file: f, info: null })
  })

  it('returns non-image unchanged', async () => {
    const f = makeFile('application/pdf', 'doc.pdf')
    expect(await resizeImageForModel(f)).toEqual({ file: f, info: null })
  })

  it('returns original when within both dimension and byte limits', async () => {
    stubImaging(800, 600)
    const f = makeFile('image/png', 'small.png', 100 * KB)
    const r = await resizeImageForModel(f)
    expect(r.file).toBe(f)
    expect(r.info).toBeNull()
  })

  it('returns original at exactly the dimension limit', async () => {
    stubImaging(MODEL_IMAGE_LIMITS.maxEdge, 1000)
    const f = makeFile('image/png', 'edge.png', 100 * KB)
    const r = await resizeImageForModel(f)
    expect(r.file).toBe(f)
    expect(r.info).toBeNull()
  })

  it('downscales an oversized PNG, preserving PNG format + dims/info', async () => {
    stubImaging(3000, 2000)
    const r = await resizeImageForModel(makeFile('image/png', 'big.png', 1 * MB))
    expect(r.file.type).toBe('image/png')
    expect(r.file.name).toBe('big.png')
    expect(renders.at(-1)).toMatchObject({ w: 1568, h: 1045, mime: 'image/png' })
    expect(r.info).toMatchObject({ name: 'big.png', fromW: 3000, fromH: 2000, toW: 1568, toH: 1045 })
  })

  it('downscales an oversized JPEG, keeping JPEG format and .jpg name', async () => {
    stubImaging(4000, 3000)
    const r = await resizeImageForModel(makeFile('image/jpeg', 'big.jpeg', 1 * MB))
    expect(r.file.type).toBe('image/jpeg')
    expect(r.file.name).toBe('big.jpg')
    expect(Math.max(renders.at(-1)!.w, renders.at(-1)!.h)).toBe(MODEL_IMAGE_LIMITS.maxEdge)
  })

  it('switches PNG->JPEG when the PNG still exceeds the byte cap', async () => {
    // Within dimension limit but a huge PNG; PNG stays over budget, JPEG fits.
    stubImaging(1200, 900, mime => (mime === 'image/png' ? 6 * MB : 1 * MB))
    const r = await resizeImageForModel(makeFile('image/png', 'heavy.png', 6 * MB))
    expect(r.file.type).toBe('image/jpeg')
    expect(r.file.name).toBe('heavy.jpg')
    // First attempt PNG (over cap), second attempt JPEG (under cap).
    expect(renders[0].mime).toBe('image/png')
    expect(renders[1].mime).toBe('image/jpeg')
    expect(r.info).not.toBeNull()
  })

  it('steps the edge down when JPEG at full edge still exceeds the byte cap', async () => {
    // JPEG always "too big" until the edge shrinks below the original.
    let calls = 0
    stubImaging(4000, 3000, () => (calls++ < 1 ? 6 * MB : 1 * MB))
    const r = await resizeImageForModel(makeFile('image/jpeg', 'huge.jpeg', 8 * MB))
    expect(r.file.type).toBe('image/jpeg')
    // Final render edge should be below the initial 1568 cap after a step-down.
    expect(Math.max(renders.at(-1)!.w, renders.at(-1)!.h)).toBeLessThan(MODEL_IMAGE_LIMITS.maxEdge)
  })

  it('returns original when it cannot get under the byte cap', async () => {
    // Every encode stays over budget across all attempts -> honest fallback.
    stubImaging(4000, 3000, () => 9 * MB)
    const f = makeFile('image/png', 'toobig.png', 9 * MB)
    const r = await resizeImageForModel(f)
    expect(r.file).toBe(f)
    expect(r.info).toBeNull()
  })

  it('returns original when the image cannot be decoded', async () => {
    vi.stubGlobal('createImageBitmap', vi.fn().mockRejectedValue(new Error('decode fail')))
    const f = makeFile('image/png', 'corrupt.png')
    expect(await resizeImageForModel(f)).toEqual({ file: f, info: null })
  })

  it('returns original when createImageBitmap is unavailable', async () => {
    vi.stubGlobal('createImageBitmap', undefined)
    const f = makeFile('image/png', 'noapi.png')
    expect(await resizeImageForModel(f)).toEqual({ file: f, info: null })
  })
})

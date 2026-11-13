import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  HEIC_LIMITS,
  HeicPrepareError,
  heifDeclaredSize,
  isHeicSignature,
  looksLikeHeic,
  needsHeicConversion,
  prepareHeicImage,
  type HeicDecoder,
} from '../utils/heicImage'
import { MODEL_IMAGE_LIMITS } from '../utils/resizeImage'

/**
 * The HEIF containers here are built byte by byte rather than read from
 * fixture files: the code under test reads only box headers (`ftyp`, then
 * `meta` > `iprp` > `ipco` > `ispe`), so a container with those boxes and no
 * coded image is a faithful input, and no binary lands in the repo. Decoding is
 * stubbed (jsdom has neither an HEVC decoder nor a canvas); real iPhone-style
 * files are exercised in a browser against a running gateway.
 */

const enc = new TextEncoder()

function box(type: string, ...parts: Uint8Array[]): Uint8Array {
  const len = 8 + parts.reduce((n, p) => n + p.length, 0)
  const out = new Uint8Array(len)
  new DataView(out.buffer).setUint32(0, len)
  out.set(enc.encode(type), 4)
  let at = 8
  for (const p of parts) { out.set(p, at); at += p.length }
  return out
}

const u32 = (...vals: number[]) => {
  const out = new Uint8Array(vals.length * 4)
  vals.forEach((v, i) => new DataView(out.buffer).setUint32(i * 4, v))
  return out
}
const brands = (...b: string[]) => enc.encode(b.join(''))
const fullBox = (type: string, ...parts: Uint8Array[]) => box(type, u32(0), ...parts)

function ftyp(major: string, ...compatible: string[]): Uint8Array {
  return box('ftyp', brands(major), u32(0), brands(...compatible))
}

/** A HEIF with one `ispe` per given size (the primary image, a thumbnail, ...). */
function heif(major: string, compatible: string[], sizes: Array<[number, number]>): Uint8Array {
  const ispes = sizes.map(([w, h]) => fullBox('ispe', u32(w, h)))
  const meta = fullBox('meta', fullBox('hdlr', u32(0), brands('pict'), u32(0, 0, 0), new Uint8Array(1)), box('iprp', box('ipco', ...ispes)))
  const head = ftyp(major, ...compatible)
  const out = new Uint8Array(head.length + meta.length)
  out.set(head, 0)
  out.set(meta, head.length)
  return out
}

const iphone = (w = 4032, h = 3024) => heif('heic', ['mif1', 'heic'], [[w, h], [320, 240]])
const fileOf = (bytes: Uint8Array, name: string, type: string) => new File([bytes], name, { type })

let renders: Array<{ w: number; h: number; mime: string; quality?: number }>
let nativeDecode: ReturnType<typeof vi.fn>

/** Stub the canvas encoder; `blobSize` decides each encode's byte size. */
function stubCanvas(blobSize: (w: number, quality?: number) => number = () => 1000) {
  renders = []
  vi.stubGlobal('OffscreenCanvas', undefined)
  vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
    if (tag !== 'canvas') return {} as HTMLElement
    const canvas: Record<string, unknown> = {
      width: 0,
      height: 0,
      getContext: () => ({ drawImage: vi.fn() }),
      toBlob: (cb: (b: Blob | null) => void, type: string, quality?: number) => {
        renders.push({ w: canvas.width as number, h: canvas.height as number, mime: type, quality })
        cb(new Blob([new Uint8Array(blobSize(canvas.width as number, quality))], { type }))
      },
    }
    return canvas as unknown as HTMLElement
  }) as typeof document.createElement)
}

/** Native decoding refuses HEIC (as in Chromium); `decoded` is what libheif returns. */
function stubDecoders(decoded: { width: number; height: number }) {
  nativeDecode = vi.fn().mockRejectedValue(new DOMException('unsupported', 'InvalidStateError'))
  vi.stubGlobal('createImageBitmap', nativeDecode)
  const close = vi.fn()
  const decoder = vi.fn<HeicDecoder>().mockResolvedValue({ ...decoded, close } as unknown as ImageBitmap)
  const load = vi.fn().mockResolvedValue(decoder)
  return { load, decoder, close }
}

beforeEach(() => vi.clearAllMocks())
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('detecting a HEIC photo', () => {
  it('trusts a HEIC or HEIF type, any case', () => {
    for (const type of ['image/heic', 'image/heif', 'image/HEIC', 'image/heic-sequence']) {
      expect(looksLikeHeic(fileOf(new Uint8Array(1), 'x', type))).toBe(true)
    }
  })

  it('reads the extension, any case, only when the type is empty or generic', () => {
    expect(looksLikeHeic(fileOf(new Uint8Array(1), 'IMG_0001.HEIC', ''))).toBe(true)
    expect(looksLikeHeic(fileOf(new Uint8Array(1), 'photo.heif', 'application/octet-stream'))).toBe(true)
    expect(looksLikeHeic(fileOf(new Uint8Array(1), 'notes.heic', 'text/plain'))).toBe(false)
    expect(looksLikeHeic(fileOf(new Uint8Array(1), 'shot.png', 'image/png'))).toBe(false)
  })

  it('recognises HEVC brands and a generic brand that lists one', () => {
    expect(isHeicSignature(ftyp('heic', 'mif1'))).toBe(true)
    expect(isHeicSignature(ftyp('heix'))).toBe(true)
    expect(isHeicSignature(ftyp('mif1', 'miaf', 'heic'))).toBe(true)
  })

  it('does not take AVIF, video or other formats for HEIC', () => {
    expect(isHeicSignature(ftyp('avif', 'mif1', 'miaf'))).toBe(false)
    expect(isHeicSignature(ftyp('mif1', 'avif', 'miaf'))).toBe(false)
    expect(isHeicSignature(ftyp('isom', 'mp41'))).toBe(false)
    expect(isHeicSignature(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0, 0, 0, 0, 0]))).toBe(false)
    expect(isHeicSignature(new Uint8Array(4))).toBe(false)
  })

  it('catches HEIC bytes wearing a .jpeg name and type', async () => {
    expect(await needsHeicConversion(fileOf(iphone(), 'IMG_1234.jpeg', 'image/jpeg'))).toBe(true)
  })

  it('leaves real JPEGs and non-images alone without reading them as HEIC', async () => {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, ...new Array(60).fill(0)])
    expect(await needsHeicConversion(fileOf(jpeg, 'a.jpg', 'image/jpeg'))).toBe(false)
    expect(await needsHeicConversion(fileOf(iphone(), 'report.pdf', 'application/pdf'))).toBe(false)
  })
})

describe('reading the declared size', () => {
  it('returns the largest ispe, the primary image rather than its thumbnail', () => {
    expect(heifDeclaredSize(iphone())).toEqual({ width: 4032, height: 3024 })
  })

  it('returns null for a container with no meta box or no ispe', () => {
    expect(heifDeclaredSize(ftyp('heic', 'mif1'))).toBeNull()
    expect(heifDeclaredSize(heif('heic', ['mif1'], []))).toBeNull()
  })

  it('stops at a box whose length runs past the data', () => {
    const bytes = iphone()
    // Claim a meta box far longer than the file: nothing inside it is read.
    new DataView(bytes.buffer).setUint32(ftyp('heic', 'mif1', 'heic').length, 0x7fffffff)
    expect(heifDeclaredSize(bytes)).toBeNull()
  })
})

describe('prepareHeicImage', () => {
  it('returns null for a file that is not HEIC', async () => {
    const png = fileOf(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), 'shot.png', 'image/png')
    expect(await prepareHeicImage(png)).toBeNull()
  })

  it('converts a HEIC to a .jpg image/jpeg capped to the model edge', async () => {
    stubCanvas()
    const { load, close } = stubDecoders({ width: 4032, height: 3024 })
    const r = await prepareHeicImage(fileOf(iphone(), 'IMG_0001.HEIC', 'image/heic'), MODEL_IMAGE_LIMITS, load)
    expect(r?.file.name).toBe('IMG_0001.jpg')
    expect(r?.file.type).toBe('image/jpeg')
    expect(renders).toEqual([{ w: 1568, h: 1176, mime: 'image/jpeg', quality: 0.92 }])
    expect(r?.info).toMatchObject({ name: 'IMG_0001.HEIC', fromW: 4032, fromH: 3024, toW: 1568, toH: 1176 })
    expect(close).toHaveBeenCalled()
  })

  it('keeps a small photo at its own size and reports no resize', async () => {
    stubCanvas()
    const { load } = stubDecoders({ width: 96, height: 64 })
    const r = await prepareHeicImage(fileOf(heif('heic', ['mif1'], [[96, 64]]), 'small.heif', ''), MODEL_IMAGE_LIMITS, load)
    expect(r?.file.name).toBe('small.jpg')
    expect(renders).toEqual([{ w: 96, h: 64, mime: 'image/jpeg', quality: 0.92 }])
    expect(r?.info).toBeNull()
  })

  it('keeps portrait orientation: the decoded (rotated) size is what gets drawn', async () => {
    // The container stores the sensor's landscape frame; libheif applies the
    // rotation, so the bitmap and the JPEG are portrait.
    stubCanvas()
    const { load } = stubDecoders({ width: 3024, height: 4032 })
    const r = await prepareHeicImage(fileOf(iphone(4032, 3024), 'portrait.heic', 'image/heic'), MODEL_IMAGE_LIMITS, load)
    expect(renders[0]).toMatchObject({ w: 1176, h: 1568 })
    expect(r?.info).toMatchObject({ toW: 1176, toH: 1568 })
  })

  it('uses the browser decoder when it reads HEIC and never loads libheif', async () => {
    stubCanvas()
    const { load } = stubDecoders({ width: 1, height: 1 })
    nativeDecode.mockResolvedValue({ width: 800, height: 600, close: vi.fn() })
    const r = await prepareHeicImage(fileOf(iphone(800, 600), 'a.heic', 'image/heic'), MODEL_IMAGE_LIMITS, load)
    expect(load).not.toHaveBeenCalled()
    expect(r?.file.type).toBe('image/jpeg')
  })

  it('steps quality down, then size, until the JPEG fits the byte budget', async () => {
    const limits = { maxEdge: 1000, maxBytes: 500 }
    // Over budget at every quality at 1000px and 850px; fits at 723px, q0.85.
    stubCanvas((w, q) => (w > 800 ? 900 : q === 0.92 ? 600 : 400))
    const { load } = stubDecoders({ width: 2000, height: 1000 })
    const r = await prepareHeicImage(fileOf(iphone(2000, 1000), 'b.heic', 'image/heic'), limits, load)
    expect(renders.map(x => [x.w, x.quality])).toEqual([
      [1000, 0.92], [1000, 0.85], [1000, 0.78], [1000, 0.68],
      [850, 0.92], [850, 0.85], [850, 0.78], [850, 0.68],
      [723, 0.92], [723, 0.85],
    ])
    expect(r?.file.size).toBe(400)
  })

  it('refuses output that cannot fit, as its own failure', async () => {
    stubCanvas(() => 10_000)
    const { load } = stubDecoders({ width: 2000, height: 1000 })
    const err = await prepareHeicImage(fileOf(iphone(2000, 1000), 'c.heic', 'image/heic'), { maxEdge: 1000, maxBytes: 500 }, load)
      .catch(e => e)
    expect(err).toBeInstanceOf(HeicPrepareError)
    expect(err.kind).toBe('cannot_fit')
    expect(err.message).toContain('c.heic')
    expect(renders.at(-1)?.w).toBe(256)
  })

  it('refuses an input over the byte cap before reading or decoding it', async () => {
    const { load } = stubDecoders({ width: 1, height: 1 })
    const huge = fileOf(iphone(), 'huge.heic', 'image/heic')
    Object.defineProperty(huge, 'size', { value: HEIC_LIMITS.maxInputBytes + 1 })
    const err = await prepareHeicImage(huge, MODEL_IMAGE_LIMITS, load).catch(e => e)
    expect(err.kind).toBe('too_large')
    expect(load).not.toHaveBeenCalled()
  })

  it('refuses a declared size over the pixel cap before decoding', async () => {
    const { load } = stubDecoders({ width: 1, height: 1 })
    const err = await prepareHeicImage(fileOf(iphone(10000, 7000), 'pano.heic', 'image/heic'), MODEL_IMAGE_LIMITS, load)
      .catch(e => e)
    expect(err.kind).toBe('too_many_pixels')
    expect(nativeDecode).not.toHaveBeenCalled()
    expect(load).not.toHaveBeenCalled()
  })

  it('calls a file with no readable container unreadable without loading a decoder', async () => {
    const { load } = stubDecoders({ width: 1, height: 1 })
    const truncated = iphone().slice(0, 40)
    const err = await prepareHeicImage(fileOf(truncated, 'broken.heic', 'image/heic'), MODEL_IMAGE_LIMITS, load).catch(e => e)
    expect(err.kind).toBe('unreadable')
    expect(load).not.toHaveBeenCalled()
  })

  it('calls a decoder refusal unreadable, and a decoder that never loaded convert_failed', async () => {
    const { load, decoder } = stubDecoders({ width: 1, height: 1 })
    decoder.mockRejectedValue(new Error('heif_error_Invalid_input'))
    const bad = await prepareHeicImage(fileOf(iphone(), 'bad.heic', 'image/heic'), MODEL_IMAGE_LIMITS, load).catch(e => e)
    expect(bad.kind).toBe('unreadable')

    load.mockRejectedValue(new TypeError('Failed to fetch dynamically imported module'))
    const offline = await prepareHeicImage(fileOf(iphone(), 'ok.heic', 'image/heic'), MODEL_IMAGE_LIMITS, load).catch(e => e)
    expect(offline.kind).toBe('convert_failed')
  })

  it('calls a canvas that cannot encode convert_failed', async () => {
    vi.stubGlobal('OffscreenCanvas', undefined)
    vi.spyOn(document, 'createElement').mockImplementation((() => ({ getContext: () => null })) as unknown as typeof document.createElement)
    const { load, close } = stubDecoders({ width: 100, height: 100 })
    const err = await prepareHeicImage(fileOf(iphone(100, 100), 'd.heic', 'image/heic'), MODEL_IMAGE_LIMITS, load).catch(e => e)
    expect(err.kind).toBe('convert_failed')
    expect(close).toHaveBeenCalled()
  })
})

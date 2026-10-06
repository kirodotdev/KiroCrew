import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyImageToClipboard, imageBlobToPng } from '../utils/clipboard'

// Image copy for the viewer's toolbar. Two properties carry the whole feature and
// neither is visible by reading the call: the bytes must be re-encoded to PNG
// (Chromium writes only the types on its allowlist), and the ClipboardItem must be
// built from the PENDING promise (WebKit checks user activation at write time, so
// awaiting the bytes first spends the click). Both are asserted directly, because
// a version that awaits and relabels passes every "did it copy?" test in Chromium
// and fails in Safari on a gesture that plainly happened.

const png = () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' })
const jpeg = () => new Blob([new Uint8Array([255, 216, 255])], { type: 'image/jpeg' })

/** Install a ClipboardItem that records how it was constructed, plus a write
 *  that resolves or rejects on command. Returns the recorder. */
function stubClipboard(opts: { write?: (items: unknown[]) => Promise<void> } = {}) {
  const parts: Array<Record<string, unknown>> = []
  class FakeClipboardItem {
    constructor(items: Record<string, unknown>) { parts.push(items) }
  }
  vi.stubGlobal('ClipboardItem', FakeClipboardItem)
  const write = vi.fn(opts.write ?? (async () => {}))
  vi.stubGlobal('navigator', { ...navigator, clipboard: { write } })
  return { parts, write }
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('imageBlobToPng', () => {
  it('passes a PNG through untouched, without a canvas round-trip', async () => {
    const original = png()
    const ctx = vi.spyOn(HTMLCanvasElement.prototype, 'getContext')
    await expect(imageBlobToPng(original)).resolves.toBe(original)
    expect(ctx).not.toHaveBeenCalled()
  })

  it('re-encodes any other type through a canvas', async () => {
    const drawImage = vi.fn()
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 4, height: 3, close: vi.fn() })))
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(cb => cb(png()))
    const out = await imageBlobToPng(jpeg())
    expect(out.type).toBe('image/png')
    expect(drawImage).toHaveBeenCalled()
  })

  it('rejects rather than returning a blank image when the encode yields nothing', async () => {
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 1, height: 1, close: vi.fn() })))
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(cb => cb(null))
    await expect(imageBlobToPng(jpeg())).rejects.toThrow()
  })

  it('releases the decoded bitmap even when the encode fails', async () => {
    const close = vi.fn()
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 1, height: 1, close })))
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    await expect(imageBlobToPng(jpeg())).rejects.toThrow()
    expect(close).toHaveBeenCalled()
  })
})

describe('copyImageToClipboard', () => {
  it('writes the image as PNG and reports success', async () => {
    const { parts, write } = stubClipboard()
    await expect(copyImageToClipboard(Promise.resolve(png()))).resolves.toBe(true)
    expect(write).toHaveBeenCalledOnce()
    expect(Object.keys(parts[0])).toEqual(['image/png'])
  })

  it('builds the ClipboardItem from the UNRESOLVED promise', async () => {
    const { parts } = stubClipboard()
    let release: (b: Blob) => void = () => {}
    const pending = new Promise<Blob>(resolve => { release = resolve })
    const copying = copyImageToClipboard(pending)
    // Constructed already, while the bytes are still in flight: this is the
    // property that keeps the write inside the click's user activation.
    expect(parts).toHaveLength(1)
    const handed = parts[0]['image/png'] as { then?: unknown }
    expect(typeof handed.then).toBe('function')
    release(png())
    await expect(copying).resolves.toBe(true)
  })

  it('reports failure instead of throwing when the write is refused', async () => {
    stubClipboard({ write: async () => { throw new DOMException('denied', 'NotAllowedError') } })
    await expect(copyImageToClipboard(Promise.resolve(png()))).resolves.toBe(false)
  })

  it('reports failure when the bytes never arrive', async () => {
    // A real `write()` consumes the promise it was handed, so a failed fetch
    // surfaces as a rejected write rather than as a copy that claims success.
    const rec: { parts: Array<Record<string, unknown>> } = { parts: [] }
    const stub = stubClipboard({ write: async () => { await rec.parts[0]['image/png'] } })
    rec.parts = stub.parts
    await expect(copyImageToClipboard(Promise.reject(new Error('HTTP 404')))).resolves.toBe(false)
  })

  it('attempts nothing, and leaves no unhandled rejection, without a Clipboard API', async () => {
    // A plain-HTTP LAN or remote gateway: `navigator.clipboard` does not exist,
    // and there is no execCommand path for an image to fall back to.
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined })
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)
    try {
      await expect(copyImageToClipboard(Promise.reject(new Error('HTTP 500')))).resolves.toBe(false)
      await new Promise(r => setTimeout(r, 0))
      expect(unhandled).not.toHaveBeenCalled()
    } finally {
      process.off('unhandledRejection', unhandled)
    }
  })
})

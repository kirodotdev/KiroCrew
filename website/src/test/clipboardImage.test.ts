import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyImageElementNatively, copyImageFromUrl, copyImageToClipboard, imageBlobToPng, nativeImageCopyAvailable } from '../utils/clipboard'

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

describe('copyImageFromUrl', () => {
  it('writes the fetched PNG bytes', async () => {
    const { parts } = stubClipboard()
    const bytes = png()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: async () => bytes }))
    await expect(copyImageFromUrl('/api/file-raw/chart.png')).resolves.toBe(true)
    expect(fetch).toHaveBeenCalledWith('/api/file-raw/chart.png')
    await expect(parts[0]['image/png']).resolves.toBe(bytes)
  })

  it('reports failure on an HTTP error instead of copying the error page', async () => {
    const rec: { parts: Array<Record<string, unknown>> } = { parts: [] }
    const stub = stubClipboard({ write: async () => { await rec.parts[0]['image/png'] } })
    rec.parts = stub.parts
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, blob: async () => png() }))
    await expect(copyImageFromUrl('/api/file-raw/missing.png')).resolves.toBe(false)
  })
})

describe('copyImageElementNatively', () => {
  const placed = (rect: Partial<DOMRect>) => {
    const img = document.createElement('img')
    document.body.appendChild(img)
    img.getBoundingClientRect = () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}), ...rect }) as DOMRect
    return img
  }
  const shell = (ok = true) => {
    const copyImageAt = vi.fn(async () => ok)
    ;(window as unknown as { electronAPI: unknown }).electronAPI = { copyImageAt }
    return copyImageAt
  }
  const hit = (fn: (x: number, y: number) => Element | null) => {
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: vi.fn(fn) })
  }
  afterEach(() => {
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    document.body.replaceChildren()
  })

  it('is unavailable without the shell bridge', async () => {
    expect(nativeImageCopyAvailable()).toBe(false)
    await expect(copyImageElementNatively(placed({ left: 10, top: 10, right: 50, bottom: 30 }))).resolves.toBe(false)
  })

  it('copies at the middle of the visible part of the image', async () => {
    const copyImageAt = shell()
    const img = placed({ left: -100, top: 20, right: 100, bottom: 60 })
    hit(() => img)
    await expect(copyImageElementNatively(img)).resolves.toBe(true)
    expect(copyImageAt).toHaveBeenCalledWith(50, 40)
  })

  it('waits until nothing covers the image', async () => {
    const copyImageAt = shell()
    const img = placed({ left: 0, top: 0, right: 40, bottom: 40 })
    const menu = document.createElement('div')
    let frames = 0
    hit(() => (frames++ < 3 ? menu : img))
    await expect(copyImageElementNatively(img)).resolves.toBe(true)
    expect(copyImageAt).toHaveBeenCalledTimes(1)
    expect(frames).toBeGreaterThan(3)
  })

  it('gives up without copying when the image stays covered', async () => {
    const copyImageAt = shell()
    const img = placed({ left: 0, top: 0, right: 40, bottom: 40 })
    hit(() => document.body)
    await expect(copyImageElementNatively(img, 50)).resolves.toBe(false)
    expect(copyImageAt).not.toHaveBeenCalled()
  })

  it('gives up on an image scrolled out of view', async () => {
    const copyImageAt = shell()
    const img = placed({ left: 0, top: -80, right: 40, bottom: -10 })
    hit(() => img)
    await expect(copyImageElementNatively(img)).resolves.toBe(false)
    expect(copyImageAt).not.toHaveBeenCalled()
  })

  it('copies at the right-click point when the visible centre is covered', async () => {
    const copyImageAt = shell()
    const img = placed({ left: 0, top: 0, right: 100, bottom: 100 })
    const composer = document.createElement('div')
    hit((_x, y) => (y >= 40 ? composer : img))
    await expect(copyImageElementNatively(img, 50, { x: 20, y: 10 })).resolves.toBe(true)
    expect(copyImageAt).toHaveBeenCalledWith(20, 10)
  })

  it('reports failure when the shell leaves no image on the clipboard', async () => {
    const copyImageAt = shell(false)
    const img = placed({ left: 0, top: 0, right: 40, bottom: 40 })
    hit(() => img)
    await expect(copyImageElementNatively(img)).resolves.toBe(false)
    expect(copyImageAt).toHaveBeenCalledTimes(1)
  })

  it('falls back to the visible centre when the right-click point is covered', async () => {
    const copyImageAt = shell()
    const img = placed({ left: 0, top: 0, right: 100, bottom: 100 })
    const menu = document.createElement('div')
    hit(x => (x === 20 ? menu : img))
    await expect(copyImageElementNatively(img, 50, { x: 20, y: 10 })).resolves.toBe(true)
    expect(copyImageAt).toHaveBeenCalledWith(50, 50)
  })
})

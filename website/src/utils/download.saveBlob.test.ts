import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const artifactDownloadUrl = vi.fn()
vi.mock('../api/client', () => ({
  api: { artifactDownloadUrl: (...args: unknown[]) => artifactDownloadUrl(...args) },
}))

import { saveBlob } from './download'

const WEBVIEW_UA = 'Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0 Mobile Safari/537.36'
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3])

describe('saveBlob', () => {
  const clicked: string[] = []
  let create: ReturnType<typeof vi.fn>
  const origCreate = URL.createObjectURL
  beforeEach(() => {
    clicked.length = 0
    artifactDownloadUrl.mockReset()
    create = vi.fn(() => 'blob:x')
    URL.createObjectURL = create as unknown as typeof URL.createObjectURL
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(this: HTMLAnchorElement) {
      clicked.push(`${this.getAttribute('href')} ${this.download}`)
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    URL.createObjectURL = origCreate
  })

  it('keeps the blob download in every other browser', async () => {
    await saveBlob(new Blob(['hi'], { type: 'text/plain' }), 'a.txt')
    expect(clicked).toEqual(['blob:x a.txt'])
    expect(artifactDownloadUrl).not.toHaveBeenCalled()
  })

  it('in an Android in-app browser hands text to the gateway and downloads its URL', async () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(WEBVIEW_UA)
    artifactDownloadUrl.mockResolvedValue({ url: '/api/artifact-downloads/abc' })
    await saveBlob(new Blob(['<svg/>'], { type: 'image/svg+xml;charset=utf-8' }), 'd.svg')
    expect(artifactDownloadUrl).toHaveBeenCalledWith('d.svg', 'image/svg+xml', btoa('<svg/>'), 'base64')
    expect(clicked).toEqual(['/api/artifact-downloads/abc d.svg'])
    expect(create).not.toHaveBeenCalled()
  })

  it('in an Android in-app browser keeps non-UTF-8 bytes exactly', async () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(WEBVIEW_UA)
    artifactDownloadUrl.mockResolvedValue({ url: '/api/artifact-downloads/l' })
    // Latin-1 'caf\xe9': blob.text() would turn the last byte into U+FFFD.
    const latin1 = new Uint8Array([0x63, 0x61, 0x66, 0xe9])
    await saveBlob(new Blob([latin1], { type: 'image/svg+xml' }), 'old.svg')
    const [, , content, encoding] = artifactDownloadUrl.mock.calls[0]
    expect(encoding).toBe('base64')
    expect(Array.from(atob(content as string), (c) => c.charCodeAt(0))).toEqual(Array.from(latin1))
  })

  it('in an Android in-app browser sends a png as base64', async () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(WEBVIEW_UA)
    artifactDownloadUrl.mockResolvedValue({ url: '/api/artifact-downloads/png' })
    await saveBlob(new Blob([PNG], { type: 'image/png' }), 'share.png')
    const [, type, content] = artifactDownloadUrl.mock.calls[0]
    expect(type).toBe('image/png')
    expect(Array.from(atob(content as string), (c) => c.charCodeAt(0))).toEqual(Array.from(PNG))
    expect(clicked).toEqual(['/api/artifact-downloads/png share.png'])
  })

  it('in an Android in-app browser rejects on a failed hand-off and builds no blob', async () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(WEBVIEW_UA)
    artifactDownloadUrl.mockRejectedValue(new Error('offline'))
    await expect(saveBlob(new Blob(['x'], { type: 'text/plain' }), 'a.txt')).rejects.toThrow('offline')
    expect(clicked).toEqual([])
    expect(create).not.toHaveBeenCalled()
  })
})

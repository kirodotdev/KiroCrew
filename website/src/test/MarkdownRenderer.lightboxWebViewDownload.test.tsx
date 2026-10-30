import { render, fireEvent, waitFor, screen, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The lightbox's Download inside an Android in-app browser, where a blob:
// click is dropped: the picture is handed to the gateway and saved from the
// URL it returns, and a failed hand-off is reported rather than silent.

const artifactDownloadUrl = vi.fn()
vi.mock('../api/client', () => ({
  api: { artifactDownloadUrl: (...args: unknown[]) => artifactDownloadUrl(...args) },
}))

import { Lightbox } from '../components/MarkdownRenderer'

const WEBVIEW_UA = 'Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0 Mobile Safari/537.36'
// The click runs fetch -> blob -> base64 -> a dynamic import of the API
// client -> the hand-off, so a wait sits behind a lazy import and a loaded CI
// shard can take longer than Testing Library's default 1 s.
const HAND_OFF_TIMEOUT = { timeout: 5000 }

const png = () => new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])], { type: 'image/png' })

function open() {
  window.dispatchEvent(new CustomEvent('lightbox', {
    detail: { images: [{ src: '/api/file-raw?path=/tmp/pic.png', alt: 'pic' }], index: 0 },
  }))
}

describe('lightbox Download in an Android in-app browser', () => {
  const clicked: string[] = []
  let openSpy: ReturnType<typeof vi.fn>
  beforeEach(() => {
    clicked.length = 0
    artifactDownloadUrl.mockReset()
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(WEBVIEW_UA)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, blob: async () => png() })))
    openSpy = vi.fn()
    vi.stubGlobal('open', openSpy)
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(this: HTMLAnchorElement) {
      clicked.push(`${this.getAttribute('href')} ${this.download}`)
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('saves the picture from the gateway URL', async () => {
    artifactDownloadUrl.mockResolvedValue({ url: '/api/artifact-downloads/p' })
    render(<Lightbox />)
    act(() => open())
    fireEvent.keyDown(window, { key: 'd' })
    await waitFor(() => expect(clicked).toEqual(['/api/artifact-downloads/p pic.png']), HAND_OFF_TIMEOUT)
    expect(artifactDownloadUrl.mock.calls[0][1]).toBe('image/png')
    expect(screen.queryByTestId('lightbox-download-error')).toBeNull()
  })

  it('drops a failure that settles after the viewer moved to another image', async () => {
    let fail: (e: Error) => void = () => {}
    artifactDownloadUrl.mockReturnValue(new Promise((_res, rej) => { fail = rej }))
    render(<Lightbox />)
    act(() => {
      window.dispatchEvent(new CustomEvent('lightbox', {
        detail: { images: [{ src: '/api/file-raw?path=/tmp/a.png', alt: 'a' }, { src: '/api/file-raw?path=/tmp/b.png', alt: 'b' }], index: 0 },
      }))
    })
    fireEvent.keyDown(window, { key: 'd' })
    await waitFor(() => expect(artifactDownloadUrl).toHaveBeenCalled(), HAND_OFF_TIMEOUT)
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    await act(async () => { fail(new Error('offline')); await Promise.resolve() })
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    expect(screen.queryByTestId('lightbox-download-error')).toBeNull()
  })

  it('reports a failed hand-off and opens no tab', async () => {
    artifactDownloadUrl.mockRejectedValue(new Error('offline'))
    render(<Lightbox />)
    act(() => open())
    fireEvent.keyDown(window, { key: 'd' })
    expect(await screen.findByTestId('lightbox-download-error', {}, HAND_OFF_TIMEOUT)).toBeInTheDocument()
    expect(clicked).toEqual([])
    expect(openSpy).not.toHaveBeenCalled()
  })
})

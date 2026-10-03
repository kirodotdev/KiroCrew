import { render, fireEvent, act, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Lightbox } from '../components/MarkdownRenderer'

// Copy image from the viewer's toolbar. The viewer's only way out used to be the
// download button, so a user pasting a screenshot into a ticket had to save a file
// first — or right-click, which the desktop shell renders no menu for.
//
// What matters here is that the confirmation is EARNED: every assertion below
// reads the button's own accessible name, because a tick shown over an unchanged
// clipboard is discovered at paste time, and the two failures a user actually hits
// (no Clipboard API on a plain-HTTP gateway, a refused permission) are silent.

const png = () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' })

function open(count = 1, index = 0) {
  window.dispatchEvent(new CustomEvent('lightbox', {
    detail: {
      images: Array.from({ length: count }, (_, i) => ({ src: `/api/file-raw?path=/tmp/p${i}.png`, alt: `p${i}` })),
      index,
    },
  }))
}

/** Serve the image bytes and record clipboard writes. `write` decides the
 *  outcome, which is the only thing the toolbar is allowed to report on. */
function stubEnv(opts: { write?: () => Promise<void>; clipboard?: boolean } = {}) {
  const { clipboard = true } = opts
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, blob: async () => png() })))
  class FakeClipboardItem { constructor(public items: Record<string, unknown>) {} }
  vi.stubGlobal('ClipboardItem', FakeClipboardItem)
  const write = vi.fn(opts.write ?? (async () => {}))
  vi.stubGlobal('navigator', { ...navigator, clipboard: clipboard ? { write } : undefined })
  return { write }
}

const copyBtn = (c: HTMLElement) => c.querySelector('[data-testid="lightbox-copy-image"]') as HTMLElement
const label = (c: HTMLElement) => copyBtn(c).getAttribute('aria-label')
const status = (c: HTMLElement) => (c.querySelector('[data-testid="lightbox-copy-status"]') as HTMLElement)

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('Lightbox copy image', () => {
  it('copies the shown image and confirms only after the write lands', async () => {
    const { write } = stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open())
    expect(label(container)).toBe('Copy image')
    fireEvent.click(copyBtn(container))
    await waitFor(() => expect(label(container)).toBe('Image copied'))
    expect(write).toHaveBeenCalledOnce()
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/file-raw?path=/tmp/p0.png')
  })

  it('says so when the clipboard refuses, instead of flashing a tick', async () => {
    stubEnv({ write: async () => { throw new DOMException('denied', 'NotAllowedError') } })
    const { container } = render(<Lightbox />)
    act(() => open())
    fireEvent.click(copyBtn(container))
    await waitFor(() => expect(label(container)).toBe('Copy failed'))
  })

  it('reports failure on an origin with no Clipboard API at all', async () => {
    stubEnv({ clipboard: false })
    const { container } = render(<Lightbox />)
    act(() => open())
    fireEvent.click(copyBtn(container))
    await waitFor(() => expect(label(container)).toBe('Copy failed'))
  })

  it('announces the outcome in a live region, which the tick cannot do', async () => {
    stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open())
    // Present before the copy, so the region is not inserted together with its
    // text — a region that appears with its content is announced inconsistently.
    expect(status(container).getAttribute('aria-live')).toBe('polite')
    expect(status(container).textContent).toBe('')
    fireEvent.click(copyBtn(container))
    await waitFor(() => expect(status(container).textContent).toBe('Image copied'))
  })

  it('copies on a bare c and leaves Ctrl/Cmd+C to the platform', async () => {
    const { write } = stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open())
    act(() => { fireEvent.keyDown(window, { key: 'c', metaKey: true }) })
    act(() => { fireEvent.keyDown(window, { key: 'c', ctrlKey: true }) })
    expect(write).not.toHaveBeenCalled()
    act(() => { fireEvent.keyDown(window, { key: 'c' }) })
    await waitFor(() => expect(label(container)).toBe('Image copied'))
  })

  it('drops the confirmation when the viewer pages to another image', async () => {
    stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open(3))
    fireEvent.click(copyBtn(container))
    await waitFor(() => expect(label(container)).toBe('Image copied'))
    // The next image is NOT on the clipboard, so the tick must not carry over.
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(label(container)).toBe('Copy image')
    expect(status(container).textContent).toBe('')
  })

  it('leaves the download button in place, the only path without a clipboard', () => {
    stubEnv({ clipboard: false })
    const { container } = render(<Lightbox />)
    act(() => open())
    expect(container.querySelector('[aria-label="Download image"]')).not.toBeNull()
  })
})

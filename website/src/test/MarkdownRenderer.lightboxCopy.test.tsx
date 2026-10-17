import { render, fireEvent, act, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Lightbox } from '../components/MarkdownRenderer'

// Copy image from the viewer. The viewer's only way out used to be the download
// button, so a user pasting a screenshot into a ticket had to save a file
// first — or right-click, which the desktop shell renders no menu for.
//
// Copy and Download share the toolbar's overflow menu, so the pill keeps its
// two-action cap (overflow + close) — `max-two-buttons-per-row`. What matters
// here is that the confirmation is EARNED (a tick shown over an unchanged
// clipboard is discovered at paste time) and that the two failures a user hits
// (no Clipboard API on a plain-HTTP gateway, a refused permission) are not
// silent: a refusal surfaces through `ErrorNotice`, not a toned-down pill
// (`errors-use-error-notice`), and on an origin with no Clipboard API the copy
// control is not offered at all — Download, always present, is the path there.

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

const menuBtn = (c: HTMLElement) => c.querySelector('[data-testid="lightbox-actions-menu"]') as HTMLElement
// Radix opens the menu on the pointer gesture, not a bare click, in jsdom.
const openMenu = (c: HTMLElement) => act(() => {
  const t = menuBtn(c)
  fireEvent.pointerDown(t, { button: 0 })
  fireEvent.pointerUp(t)
  fireEvent.click(t)
})
/** The copy item lives in a Radix menu portal, which renders to document.body. */
const copyItem = () => document.body.querySelector('[data-testid="lightbox-copy-image"]') as HTMLElement | null
const downloadItem = () => document.body.querySelector('[data-testid="lightbox-download-image"]') as HTMLElement | null
// Radix selects a menu item on the pointer gesture, not a bare click.
const selectItem = (el: HTMLElement) => act(() => {
  fireEvent.pointerDown(el, { button: 0 })
  fireEvent.pointerUp(el)
  fireEvent.click(el)
})
const copyLabel = () => copyItem()?.getAttribute('aria-label')
const status = (c: HTMLElement) => (c.querySelector('[data-testid="lightbox-copy-status"]') as HTMLElement)
const errorNotice = (c: HTMLElement) => c.querySelector('[data-testid="lightbox-copy-error"]') as HTMLElement | null

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('Lightbox copy image', () => {
  it('copies the shown image and confirms only after the write lands', async () => {
    const { write } = stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open())
    openMenu(container)
    expect(copyLabel()).toBe('Copy image')
    selectItem(copyItem()!)
    await waitFor(() => expect(status(container).textContent).toBe('Image copied'))
    expect(write).toHaveBeenCalledOnce()
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/file-raw?path=/tmp/p0.png')
  })

  it('surfaces a refused clipboard write through ErrorNotice, not a tick', async () => {
    stubEnv({ write: async () => { throw new DOMException('denied', 'NotAllowedError') } })
    const { container } = render(<Lightbox />)
    act(() => open())
    openMenu(container)
    selectItem(copyItem()!)
    await waitFor(() => expect(errorNotice(container)).not.toBeNull())
    // It is a real error alert naming the next step — not a polite status pill.
    const notice = errorNotice(container)!
    expect(notice.getAttribute('role')).toBe('alert')
    expect(within(notice).getByText(/actions menu and choose Download/i)).toBeTruthy()
    // The success live region never claimed a copy.
    expect(status(container).textContent).toBe('')
  })

  it('keeps the failure notice until the user dismisses it, not on a timer', async () => {
    vi.useFakeTimers()
    try {
      stubEnv({ write: async () => { throw new DOMException('denied', 'NotAllowedError') } })
      const { container } = render(<Lightbox />)
      act(() => open())
      openMenu(container)
      selectItem(copyItem()!)
      await vi.waitFor(() => expect(errorNotice(container)).not.toBeNull())
      // The success tick auto-clears; a failure must not — it carries the only
      // recovery hint, so advancing well past the success-flash window leaves it up.
      act(() => { vi.advanceTimersByTime(10000) })
      expect(errorNotice(container)).not.toBeNull()
      // Dismissing it (the notice's own ✕) is what clears it.
      const dismiss = within(errorNotice(container)!).getByRole('button')
      act(() => { fireEvent.click(dismiss) })
      expect(errorNotice(container)).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not offer copy on an origin with no Clipboard API at all', () => {
    stubEnv({ clipboard: false })
    const { container } = render(<Lightbox />)
    act(() => open())
    // No Copy means no two-action menu: Download is a DIRECT button (its old
    // one-press behaviour), not buried behind an overflow trigger. Copy — a
    // button that could only ever fail — is absent entirely.
    expect(menuBtn(container)).toBeNull()
    expect(copyItem()).toBeNull()
    expect(downloadItem()).not.toBeNull()
  })

  it('announces the success in a live region, which the tick cannot do', async () => {
    stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open())
    // Present before the copy, so the region is not inserted together with its
    // text — a region that appears with its content is announced inconsistently.
    expect(status(container).getAttribute('aria-live')).toBe('polite')
    expect(status(container).textContent).toBe('')
    openMenu(container)
    selectItem(copyItem()!)
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
    await waitFor(() => expect(status(container).textContent).toBe('Image copied'))
  })

  it('leaves the bare c inert on an origin with no Clipboard API', () => {
    const { write } = stubEnv({ clipboard: false })
    render(<Lightbox />)
    act(() => open())
    act(() => { fireEvent.keyDown(window, { key: 'c' }) })
    expect(write).not.toHaveBeenCalled()
  })

  it('drops the confirmation when the viewer pages to another image', async () => {
    stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open(3))
    openMenu(container)
    selectItem(copyItem()!)
    await waitFor(() => expect(status(container).textContent).toBe('Image copied'))
    // The next image is NOT on the clipboard, so the confirmation must not carry over.
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(status(container).textContent).toBe('')
  })

  it('keeps Download a direct one-press button when there is no clipboard', () => {
    stubEnv({ clipboard: false })
    const { container } = render(<Lightbox />)
    act(() => open())
    // UX BLOCK fix: where Copy is not offered the overflow menu would hold only
    // Download, so burying it behind `•••` costs an extra click for nothing.
    // Download is reachable directly — present without opening any menu — and
    // no actions-menu trigger is rendered at all.
    expect(menuBtn(container)).toBeNull()
    expect(downloadItem()).not.toBeNull()
  })

  // The global keydown listener runs in capture phase, so without standing down
  // it would see the Escape/arrow a user means for the OPEN actions menu first
  // and close the whole viewer (or page the image behind the menu) before Radix
  // ever sees the key. While the menu is open those keys belong to the menu.
  it('leaves Escape to the menu while it is open — closes the menu, not the viewer', () => {
    stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open())
    openMenu(container)
    expect(copyItem()).not.toBeNull()
    // Escape while the menu is open must NOT dismiss the viewer: the overlay and
    // its actions trigger are still present (Radix closes only the menu).
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(menuBtn(container)).not.toBeNull()
  })

  it('leaves the arrows to the menu while it is open — does not page the image behind it', async () => {
    stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open(3))
    openMenu(container)
    // Copy image 0, then press ArrowRight while the menu is still open. The
    // viewer must NOT page (which would clear the confirmation and move the
    // image behind the open menu); the arrow belongs to the menu.
    selectItem(copyItem()!)
    await waitFor(() => expect(status(container).textContent).toBe('Image copied'))
    openMenu(container)
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    // Still on image 0: the confirmation for image 0 survives, proving no page.
    expect(status(container).textContent).toBe('Image copied')
  })

  it('still closes the viewer on Escape once the menu is closed', () => {
    stubEnv()
    const { container } = render(<Lightbox />)
    act(() => open())
    openMenu(container)
    // Radix closes the menu on Escape; fire its onOpenChange(false) path by
    // pressing Escape on the open menu content, then press Escape again with the
    // menu closed — now the viewer's own handler runs and dismisses it.
    act(() => { fireEvent.keyDown(copyItem()!, { key: 'Escape' }) })
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(menuBtn(container)).toBeNull()
  })
})

/**
 * The bubble menu replaces the browser's own, so a right-click on a link inside
 * the bubble must still offer the link copies that native menu had.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import MessageContextMenu, { type MessageMenuItem } from '../pages/chat/MessageContextMenu'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyImageFromUrl: vi.fn().mockResolvedValue(true),
  copyImageElementNatively: vi.fn().mockResolvedValue(true),
  nativeImageCopyAvailable: vi.fn().mockReturnValue(false),
}))
import { copyImageElementNatively, copyImageFromUrl, copyToClipboard, nativeImageCopyAvailable } from '../utils/clipboard'

const items: MessageMenuItem[] = [
  { id: 'quote', label: 'Quote message', icon: null, onSelect: () => {} },
  { id: 'copy', label: 'Copy text', icon: null, onSelect: () => {} },
]

const bubble = (
  <div data-testid="bubble">
    <p data-testid="prose">Run build <a href="https://example.com/build/42">42</a> passed.</p>
    <a href="https://example.com/icon" data-testid="icon-link" aria-label="icon"><svg /></a>
  </div>
)

const labels = () => screen.getAllByRole('menuitem').map(i => i.textContent)

beforeEach(() => { vi.mocked(copyToClipboard).mockClear().mockResolvedValue(true) })

describe('MessageContextMenu on a link', () => {
  it('adds Open link in new tab, Copy link address and Copy link text above the host items', () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    expect(labels()).toEqual(['Open link in new tab', 'Copy link address', 'Copy link text', 'Quote message', 'Copy text'])
  })

  it('copies the resolved href and the visible text', async () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.click(screen.getByTestId('message-context-copy-link-address'))
    expect(copyToClipboard).toHaveBeenCalledWith('https://example.com/build/42')
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.click(screen.getByTestId('message-context-copy-link-text'))
    expect(copyToClipboard).toHaveBeenLastCalledWith('42')
  })

  it('omits Copy link text for a link with no text', () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByTestId('icon-link').querySelector('svg')!)
    expect(labels()).toEqual(['Open link in new tab', 'Copy link address', 'Quote message', 'Copy text'])
  })

  it('opens the resolved href in a new tab with no opener', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
      fireEvent.contextMenu(screen.getByText('42'))
      fireEvent.click(screen.getByTestId('message-context-open-link-new-tab'))
      expect(open).toHaveBeenCalledWith('https://example.com/build/42', '_blank', 'noopener,noreferrer')
    } finally {
      open.mockRestore()
    }
  })

  it('offers no new-tab open for a link that is not a web page', () => {
    render(
      <MessageContextMenu items={items}>
        <div><a href="mailto:a@example.com">mail</a></div>
      </MessageContextMenu>,
    )
    fireEvent.contextMenu(screen.getByText('mail'))
    expect(labels()).toEqual(['Copy link address', 'Copy link text', 'Quote message', 'Copy text'])
  })

  it('offers no new-tab open for a web link that carries userinfo', () => {
    render(
      <MessageContextMenu items={items}>
        <div><a href="https://user:pass@example.com/x">creds</a></div>
      </MessageContextMenu>,
    )
    fireEvent.contextMenu(screen.getByText('creds'))
    expect(labels()).toEqual(['Copy link address', 'Copy link text', 'Quote message', 'Copy text'])
  })

  it('shows only the host items off a link', () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.keyDown(screen.getByTestId('message-context-menu'), { key: 'Escape' })
    fireEvent.contextMenu(screen.getByTestId('prose'))
    expect(labels()).toEqual(['Quote message', 'Copy text'])
  })

  it('reports a refused clipboard write', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    const onCopyFailed = vi.fn()
    render(<MessageContextMenu items={items} onCopyFailed={onCopyFailed}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.click(screen.getByTestId('message-context-copy-link-address'))
    await waitFor(() => expect(onCopyFailed).toHaveBeenCalledTimes(1))
  })
})

describe('MessageContextMenu on an image', () => {
  const imageBubble = (
    <div data-testid="bubble">
      <p data-testid="prose">Chart below.</p>
      <img data-testid="chart" src="/api/file-raw/chart.png" alt="chart" />
      <a href="https://example.com/full"><img data-testid="linked" src="/api/file-raw/thumb.png" alt="thumb" /></a>
      <img data-testid="remote" src="https://images.example.org/shot.png" alt="remote" />
    </div>
  )
  const loaded = (el: HTMLElement, width = 480) => {
    Object.defineProperty(el, 'complete', { configurable: true, value: true })
    Object.defineProperty(el, 'naturalWidth', { configurable: true, value: width })
    return el
  }

  beforeEach(() => {
    vi.mocked(copyImageFromUrl).mockClear().mockResolvedValue(true)
    vi.mocked(copyImageElementNatively).mockClear().mockResolvedValue(true)
    vi.mocked(nativeImageCopyAvailable).mockReturnValue(false)
    vi.stubGlobal('ClipboardItem', class { constructor(_items: unknown) {} })
    vi.stubGlobal('navigator', { ...navigator, clipboard: { write: vi.fn(), writeText: vi.fn() } })
  })
  afterEach(() => { vi.unstubAllGlobals() })
  const copiedShown = () => waitFor(() => expect(screen.getByTestId('message-context-image-copied')).toHaveTextContent('Image copied'))

  it('adds Copy image above the host items', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
    expect(labels()).toEqual(['Copy image', 'Quote message', 'Copy text'])
  })

  it('copies the image at its resolved src', async () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
    fireEvent.click(screen.getByTestId('message-context-copy-image'))
    expect(copyImageFromUrl).toHaveBeenCalledWith(`${location.origin}/api/file-raw/chart.png`)
    await copiedShown()
  })

  it('offers the link copies and Copy image for an image inside a link', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('linked')))
    expect(labels()).toEqual(['Open link in new tab', 'Copy link address', 'Copy image', 'Quote message', 'Copy text'])
  })

  it('omits Copy image for an image that failed to load', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart'), 0))
    expect(labels()).toEqual(['Quote message', 'Copy text'])
  })

  it('reports a failed image copy', async () => {
    vi.mocked(copyImageFromUrl).mockResolvedValue(false)
    const onCopyFailed = vi.fn()
    render(<MessageContextMenu items={items} onCopyFailed={onCopyFailed}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
    fireEvent.click(screen.getByTestId('message-context-copy-image'))
    await waitFor(() => expect(onCopyFailed).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('message-context-image-copied-status')).toHaveTextContent('')
    expect(screen.queryByTestId('message-context-image-copied')).toBeNull()
  })

  it('confirms a successful image copy, then clears the confirmation', async () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')), { clientX: 300, clientY: 120 })
    fireEvent.click(screen.getByTestId('message-context-copy-image'))
    const status = screen.getByTestId('message-context-image-copied-status')
    await waitFor(() => expect(status).toHaveTextContent('Image copied'))
    expect(screen.getByTestId('message-context-image-copied')).toHaveStyle({ left: '300px', top: '112px' })
    await waitFor(() => expect(status).toHaveTextContent(''), { timeout: 2500 })
    expect(screen.queryByTestId('message-context-image-copied')).toBeNull()
  })

  it('keeps the confirmation inside the window near its edge', async () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')), { clientX: 10, clientY: 5 })
    fireEvent.click(screen.getByTestId('message-context-copy-image'))
    const status = screen.getByTestId('message-context-image-copied-status')
    await waitFor(() => expect(status).toHaveTextContent('Image copied'))
    expect(screen.getByTestId('message-context-image-copied')).toHaveStyle({ left: '72px', top: '40px' })
    await waitFor(() => expect(status).toHaveTextContent(''), { timeout: 2500 })
    expect(screen.queryByTestId('message-context-image-copied')).toBeNull()
  })

  it('keeps the host items but omits Copy image on a cross-origin image', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    const remote = loaded(screen.getByTestId('remote'))
    fireEvent.pointerDown(remote, { button: 2 })
    fireEvent.contextMenu(remote)
    expect(labels()).toEqual(['Quote message', 'Copy text'])
  })

  it('keeps the host items but omits Copy image when the Clipboard API is missing', () => {
    vi.stubGlobal('ClipboardItem', undefined)
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    const chart = loaded(screen.getByTestId('chart'))
    fireEvent.pointerDown(chart, { button: 2 })
    fireEvent.contextMenu(chart)
    expect(labels()).toEqual(['Quote message', 'Copy text'])
  })

  describe('in the desktop shell', () => {
    beforeEach(() => { vi.mocked(nativeImageCopyAvailable).mockReturnValue(true) })

    it('copies a cross-origin image through the shell', async () => {
      render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
      const remote = loaded(screen.getByTestId('remote'))
      fireEvent.contextMenu(remote, { clientX: 30, clientY: 12 })
      expect(labels()).toEqual(['Copy image', 'Quote message', 'Copy text'])
      fireEvent.click(screen.getByTestId('message-context-copy-image'))
      expect(copyImageElementNatively).toHaveBeenCalledWith(remote, 1000, { x: 30, y: 12 })
      expect(copyImageFromUrl).not.toHaveBeenCalled()
      await copiedShown()
    })

    it('copies through the shell when the Clipboard API is missing', async () => {
      vi.stubGlobal('ClipboardItem', undefined)
      render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
      const chart = loaded(screen.getByTestId('chart'))
      fireEvent.contextMenu(chart)
      fireEvent.click(screen.getByTestId('message-context-copy-image'))
      expect(copyImageElementNatively).toHaveBeenCalledWith(chart, 1000, { x: 0, y: 0 })
      await copiedShown()
    })

    it('still fetches an image the page can read', async () => {
      render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
      fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
      fireEvent.click(screen.getByTestId('message-context-copy-image'))
      expect(copyImageFromUrl).toHaveBeenCalledTimes(1)
      expect(copyImageElementNatively).not.toHaveBeenCalled()
      await copiedShown()
    })

    it('reports a failed shell copy', async () => {
      vi.mocked(copyImageElementNatively).mockResolvedValue(false)
      const onCopyFailed = vi.fn()
      render(<MessageContextMenu items={items} onCopyFailed={onCopyFailed}>{imageBubble}</MessageContextMenu>)
      fireEvent.contextMenu(loaded(screen.getByTestId('remote')))
      fireEvent.click(screen.getByTestId('message-context-copy-image'))
      await waitFor(() => expect(onCopyFailed).toHaveBeenCalledTimes(1))
    })
  })
})

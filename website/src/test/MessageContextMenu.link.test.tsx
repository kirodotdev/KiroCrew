/**
 * The bubble menu replaces the browser's own, so a right-click on a link inside
 * the bubble must still offer the link copies that native menu had.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import MessageContextMenu, { type MessageMenuItem } from '../pages/chat/MessageContextMenu'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyImageToClipboard: vi.fn().mockResolvedValue(true),
  imageBlobToPng: vi.fn(async (blob: Blob) => blob),
}))
import { copyImageToClipboard, copyToClipboard } from '../utils/clipboard'

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
  it('adds Copy link address and Copy link text above the host items', () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    expect(labels()).toEqual(['Copy link address', 'Copy link text', 'Quote message', 'Copy text'])
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
    expect(labels()).toEqual(['Copy link address', 'Quote message', 'Copy text'])
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
  const png = new Blob(['png'], { type: 'image/png' })

  beforeEach(() => {
    vi.mocked(copyImageToClipboard).mockClear().mockResolvedValue(true)
    vi.stubGlobal('ClipboardItem', class { constructor(_items: unknown) {} })
    vi.stubGlobal('navigator', { ...navigator, clipboard: { write: vi.fn(), writeText: vi.fn() } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: async () => png }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('adds Copy image above the host items', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
    expect(labels()).toEqual(['Copy image', 'Quote message', 'Copy text'])
  })

  it('copies the image bytes fetched from its src', async () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
    fireEvent.click(screen.getByTestId('message-context-copy-image'))
    expect(fetch).toHaveBeenCalledWith(`${location.origin}/api/file-raw/chart.png`)
    expect(copyImageToClipboard).toHaveBeenCalledTimes(1)
    await expect(vi.mocked(copyImageToClipboard).mock.calls[0][0]).resolves.toBe(png)
  })

  it('offers the link copies and Copy image for an image inside a link', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('linked')))
    expect(labels()).toEqual(['Copy link address', 'Copy image', 'Quote message', 'Copy text'])
  })

  it('omits Copy image for an image that failed to load', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart'), 0))
    expect(labels()).toEqual(['Quote message', 'Copy text'])
  })

  it('reports a failed image copy', async () => {
    vi.mocked(copyImageToClipboard).mockResolvedValue(false)
    const onCopyFailed = vi.fn()
    render(<MessageContextMenu items={items} onCopyFailed={onCopyFailed}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
    fireEvent.click(screen.getByTestId('message-context-copy-image'))
    await waitFor(() => expect(onCopyFailed).toHaveBeenCalledTimes(1))
  })

  it('yields to the native menu on a cross-origin image', () => {
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    const remote = loaded(screen.getByTestId('remote'))
    fireEvent.pointerDown(remote, { button: 2 })
    fireEvent.contextMenu(remote)
    expect(screen.queryByTestId('message-context-menu')).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('hands copyImageToClipboard a rejecting promise when the image fetch fails', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 404, blob: async () => png } as Response)
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    fireEvent.contextMenu(loaded(screen.getByTestId('chart')))
    fireEvent.click(screen.getByTestId('message-context-copy-image'))
    await expect(vi.mocked(copyImageToClipboard).mock.calls[0][0]).rejects.toThrow('HTTP 404')
  })

  it('yields to the native menu on an image when the Clipboard API is missing', () => {
    vi.stubGlobal('ClipboardItem', undefined)
    render(<MessageContextMenu items={items}>{imageBubble}</MessageContextMenu>)
    const chart = loaded(screen.getByTestId('chart'))
    fireEvent.pointerDown(chart, { button: 2 })
    fireEvent.contextMenu(chart)
    expect(screen.queryByTestId('message-context-menu')).toBeNull()
  })
})

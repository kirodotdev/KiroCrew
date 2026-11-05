import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FileCard } from '../components/FileCard'
import MarkdownRenderer, { Lightbox } from '../components/MarkdownRenderer'

// A file_send image card opens the shared image viewer: the preview is a
// button, it joins the turn's set with the reply's markdown images, and the
// viewer's Download saves under the file's own name.

type Detail = { images: { src: string; alt: string; filename?: string }[]; index: number }

function captureLightbox() {
  const events: Detail[] = []
  const spy = (e: Event) => events.push((e as CustomEvent<Detail>).detail)
  window.addEventListener('lightbox', spy)
  return { events, stop: () => window.removeEventListener('lightbox', spy) }
}

const PNG = { filename: 'dashboard.png', content_type: 'image/png', description: 'Dashboard preview' }

describe('FileCard image preview', () => {
  let cap: ReturnType<typeof captureLightbox>
  beforeEach(() => { cap = captureLightbox() })
  afterEach(() => cap.stop())

  it('renders the preview as a named button and keeps the Save link', () => {
    render(<FileCard file={PNG} />)
    const open = screen.getByRole('button', { name: 'Open preview of dashboard.png' })
    expect(open).toHaveAttribute('type', 'button')
    expect(open).toHaveClass('cursor-zoom-in')
    expect(open.querySelector('img')?.getAttribute('src')).toBe('/api/outbox/dashboard.png')
    const save = document.querySelector('a[download]')
    expect(save).toHaveAccessibleName(/Save/)
    expect(save?.getAttribute('href')).toBe('/api/outbox/dashboard.png?download=1')
  })

  it('opens the viewer at the image on click, carrying the real filename', () => {
    render(<FileCard file={PNG} />)
    fireEvent.click(screen.getByRole('button', { name: /Open preview of/ }))
    expect(cap.events).toHaveLength(1)
    expect(cap.events[0].index).toBe(0)
    expect(cap.events[0].images).toHaveLength(1)
    expect(cap.events[0].images[0].src).toContain('/api/outbox/dashboard.png')
    expect(cap.events[0].images[0].alt).toBe('Dashboard preview')
    expect(cap.events[0].images[0].filename).toBe('dashboard.png')
  })

  it.each(['{Enter}', ' '])('opens the viewer from the keyboard (%s)', async (key) => {
    const user = userEvent.setup()
    render(<FileCard file={PNG} />)
    screen.getByRole('button', { name: /Open preview of/ }).focus()
    await user.keyboard(key)
    expect(cap.events).toHaveLength(1)
  })

  it('does not let the click reach a transcript ancestor', () => {
    const outer = vi.fn()
    // A native listener ABOVE the React root, standing in for a transcript
    // ancestor that binds a fold toggle on click.
    const parent = document.body.appendChild(document.createElement('section'))
    const root = parent.appendChild(document.createElement('div'))
    parent.addEventListener('click', outer)
    try {
      const { unmount } = render(<FileCard file={PNG} />, { container: root })
      fireEvent.click(screen.getByRole('button', { name: /Open preview of/ }))
      expect(outer).not.toHaveBeenCalled()
      // The probe is live: the card's Save link, which does not stop
      // propagation, does reach the same listener.
      fireEvent.click(document.querySelector('a[download]') as HTMLElement)
      expect(outer).toHaveBeenCalledTimes(1)
      unmount()
    } finally {
      parent.remove()
    }
  })

  it('swaps a preview that fails to load for an inline error, keeping Save', () => {
    render(<FileCard file={PNG} />)
    fireEvent.error(document.querySelector('img') as HTMLImageElement)
    expect(screen.queryByRole('button', { name: /Open preview of/ })).toBeNull()
    expect(screen.getByTestId('file-card-image-error')).toHaveTextContent('Image failed to load')
    expect(document.querySelector('a[download]')).toHaveAccessibleName(/Save/)
  })

  it('leaves SVG and non-image files without a viewer control', () => {
    const { unmount } = render(<FileCard file={{ filename: 'diagram.svg', content_type: 'image/svg+xml' }} />)
    expect(screen.queryByRole('button', { name: /Open preview of/ })).toBeNull()
    expect(document.querySelector('img')).toBeNull()
    unmount()
    render(<FileCard file={{ filename: 'report.pdf', content_type: 'application/pdf' }} />)
    expect(screen.queryByRole('button', { name: /Open preview of/ })).toBeNull()
    expect(document.querySelectorAll('a[download]')).toHaveLength(1)
  })

  it('pages with the markdown images of the same turn, in transcript order', () => {
    render(
      <div data-image-turn="">
        <MarkdownRenderer content={'Before\n\n![chart](/tmp/chart.png)'} />
        <FileCard file={PNG} />
        <MarkdownRenderer content={'![after](/tmp/after.png)'} />
      </div>,
    )
    fireEvent.click(screen.getByRole('button', { name: /Open preview of/ }))
    expect(cap.events).toHaveLength(1)
    const { images, index } = cap.events[0]
    expect(images.map(i => i.alt)).toEqual(['chart', 'Dashboard preview', 'after'])
    expect(index).toBe(1)
    // A markdown image opens the same set, at its own position.
    fireEvent.click(document.querySelector('img[alt="after"]') as HTMLImageElement)
    expect(cap.events[1].images.map(i => i.alt)).toEqual(['chart', 'Dashboard preview', 'after'])
    expect(cap.events[1].index).toBe(2)
  })

  it('leaves images in folded working-step rows out of the turn set', () => {
    render(
      <div data-image-turn="">
        <div data-collapsed="true">
          <MarkdownRenderer content={'![folded](/tmp/folded.png)'} />
        </div>
        <div {...{ inert: '' }}>
          <MarkdownRenderer content={'![inert](/tmp/inert.png)'} />
        </div>
        <MarkdownRenderer content={'![chart](/tmp/chart.png)'} />
        <FileCard file={PNG} />
      </div>,
    )
    fireEvent.click(screen.getByRole('button', { name: /Open preview of/ }))
    expect(cap.events[0].images.map(i => i.alt)).toEqual(['chart', 'Dashboard preview'])
    expect(cap.events[0].index).toBe(1)
  })

  it('keeps a markdown image to its own message where no turn wrapper exists', () => {
    render(
      <div>
        <MarkdownRenderer content={'![chart](/tmp/chart.png)'} />
        <FileCard file={PNG} />
      </div>,
    )
    fireEvent.click(document.querySelector('img[alt="chart"]') as HTMLImageElement)
    expect(cap.events[0].images.map(i => i.alt)).toEqual(['chart'])
  })
})

describe('FileCard image in the viewer', () => {
  let clicks: { download: string; href: string }[]
  let clickSpy: ReturnType<typeof vi.spyOn>
  let createObjectURL: typeof URL.createObjectURL
  let revokeObjectURL: typeof URL.revokeObjectURL

  beforeEach(() => {
    clicks = []
    createObjectURL = URL.createObjectURL
    revokeObjectURL = URL.revokeObjectURL
    URL.createObjectURL = vi.fn(() => 'blob:card')
    URL.revokeObjectURL = vi.fn()
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push({ download: this.download, href: this.getAttribute('href') ?? '' })
    })
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, blob: () => Promise.resolve(new Blob(['png'])) } as unknown as Response)))
  })

  afterEach(() => {
    clickSpy.mockRestore()
    URL.createObjectURL = createObjectURL
    URL.revokeObjectURL = revokeObjectURL
    vi.unstubAllGlobals()
  })

  it.each([
    ['dashboard.png', 'dashboard.png'],
    // No extension in the URL, so the name comes from the card, not the alt text.
    ['chart', 'chart'],
    ['q3 report (final).png', 'q3 report (final).png'],
  ])('downloads %s under its real filename', async (filename, expected) => {
    render(<><Lightbox /><FileCard file={{ filename, content_type: 'image/png', description: 'A description' }} /></>)
    fireEvent.click(screen.getByRole('button', { name: /Open preview of/ }))
    // `d` is the viewer's download shortcut; it runs the same path as the menu item.
    act(() => { fireEvent.keyDown(window, { key: 'd' }) })
    await waitFor(() => expect(clicks).toHaveLength(1))
    expect(clicks[0].download).toBe(expected)
  })
})

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import type { Artifact } from '../types'

// The frame loads a real DOCUMENT minted by the gateway, not a `blob:` URL built
// in the browser: some WebKit-based in-app browsers refuse a blob load outright
// ("invalid url or response") and can take the whole page down with it, and a
// sandboxed `srcdoc` frame blank-renders on WebKit.
//
// Pinned here: the frame addresses the minted URL, it never builds a blob for
// itself (that form renders fine in Chromium, which is why it could come back
// unnoticed), and the URL is cleared when content empties so the frame is never
// left pointing at a stale document — the same class of bug the previous blob
// lifecycle test guarded.

const SLUG = 'my-widget'
const HTML_CONTENT = '<p>hello</p>'
const DOC_URL = '/sandbox-doc/abc123/1700000000.mac'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }),
}))

vi.mock('../hooks/useCommentBridge', () => ({
  useCommentBridge: () => ({ scrollToAnchor: vi.fn() }),
}))

vi.mock('../lib/widgetSrcdoc', () => ({
  THEME_VAR_NAMES: [] as string[],
  buildSrcdoc: ({ html }: { html: string }) => html,
}))

const mintSpy = vi.fn()
vi.mock('../api/client', () => ({
  api: { sandboxDocUrl: (html: string) => mintSpy(html) },
  ApiError: class extends Error {},
}))

import { ArtifactBodyIframe } from '../components/ArtifactBody'

function makeArtifact(content: string): Artifact {
  return { slug: SLUG, name: 'Widget', kind: 'widget', content } as unknown as Artifact
}

describe('ArtifactBodyIframe document URL lifecycle', () => {
  const originalCreate = globalThis.URL.createObjectURL

  beforeEach(() => {
    mintSpy.mockReset()
    mintSpy.mockResolvedValue({ url: DOC_URL })
    // Any use of this for the frame is a regression to the crashing form.
    globalThis.URL.createObjectURL = vi.fn(() => {
      throw new Error('the artifact frame must not use a blob: URL')
    }) as never
  })

  afterEach(() => {
    globalThis.URL.createObjectURL = originalCreate
  })

  it('points the iframe at the minted document URL', async () => {
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => {
      const frame = document.querySelector('iframe')
      expect(frame?.getAttribute('src')).toBe(DOC_URL)
    })
    expect(mintSpy).toHaveBeenCalledWith(HTML_CONTENT)
  })

  it('builds no blob URL for the frame', async () => {
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => expect(mintSpy).toHaveBeenCalled())
    expect(globalThis.URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('keeps the frame invisible until its document reports load', async () => {
    // Swapping the themed placeholder for the iframe the moment the URL arrives
    // shows the ENGINE's own canvas for the length of the document fetch, and
    // some engines paint that canvas white whatever this element's background
    // says — a flash on every open. The frame therefore starts transparent with
    // a themed panel underneath, exactly as WidgetFrame already did.
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    expect(frame.style.opacity).toBe('0')

    fireEvent.load(frame)
    expect(frame.style.opacity).toBe('1')
  })

  it('re-hides the frame when a new document is minted', async () => {
    // A theme change or retry navigates the frame again; the previous load must
    // not keep the new, still-loading document visible.
    const { rerender } = render(
      <ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />,
    )
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    expect(frame.style.opacity).toBe('1')

    mintSpy.mockResolvedValue({ url: '/sandbox-doc/second/tok' })
    rerender(<ArtifactBodyIframe artifact={makeArtifact('<p>changed</p>')} />)
    await waitFor(() =>
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(
        '/sandbox-doc/second/tok',
      ),
    )
    expect(document.querySelector('iframe')?.style.opacity).toBe('0')
  })

  it('offers a retry instead of an eternal progress label when the mint fails', async () => {
    // A failed mint used to leave the "Rendering…" placeholder up forever — a
    // label asserting progress when nothing is in flight. This matters more now
    // that the document is single-use: a spent URL is a real outcome, so the
    // retry IS the recovery path rather than a courtesy.
    mintSpy.mockRejectedValueOnce(new Error('gateway said no'))
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)

    const failure = await screen.findByText(/couldn't render this artifact/i)
    expect(failure).toBeTruthy()
    expect(screen.queryByText(/rendering…/i)).toBeNull()
    expect(document.querySelector('iframe')).toBeNull()

    // Retry must MINT AGAIN. Re-rendering the spent URL would recover nothing,
    // so a second call to the gateway is the behaviour under test.
    mintSpy.mockResolvedValue({ url: DOC_URL })
    const retry = screen.getByRole('button', { name: /retry/i })
    retry.click()

    await waitFor(() => {
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL)
    })
    expect(mintSpy).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/couldn't render this artifact/i)).toBeNull()
  })

  it('holds the previous document while a new one is in flight', async () => {
    // Clearing the URL before re-minting flashed an open artifact out to the
    // placeholder on every theme change, since a theme change rebuilds the
    // document and now costs a round trip.
    const { rerender } = render(
      <ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />,
    )
    await waitFor(() =>
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL),
    )

    let release: (v: { url: string }) => void = () => {}
    mintSpy.mockReturnValueOnce(
      new Promise<{ url: string }>((resolve) => {
        release = resolve
      }),
    )
    rerender(<ArtifactBodyIframe artifact={makeArtifact('<p>changed</p>')} />)

    // Still showing the OLD document, not a placeholder, while the mint runs.
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL)
    expect(screen.queryByText(/rendering…/i)).toBeNull()

    release({ url: '/sandbox-doc/second/tok' })
    await waitFor(() =>
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(
        '/sandbox-doc/second/tok',
      ),
    )
  })

  it('clears the URL when content empties, leaving no stale document', async () => {
    const { rerender } = render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    rerender(<ArtifactBodyIframe artifact={makeArtifact('')} />)
    await waitFor(() => expect(document.querySelector('iframe')).toBeNull())
  })

  it('renders no frame when minting fails', async () => {
    mintSpy.mockRejectedValueOnce(new Error('gateway down'))
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => expect(mintSpy).toHaveBeenCalled())
    expect(document.querySelector('iframe')).toBeNull()
  })
})

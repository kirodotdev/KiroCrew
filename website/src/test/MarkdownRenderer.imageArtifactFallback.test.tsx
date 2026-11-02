import { fireEvent, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { ThemeProvider } from '../hooks/useTheme'
import { api } from '../api/client'
import { deriveImageArtifactSlug } from '../lib/imageArtifactSlug'

describe('MarkdownRenderer durable chat image fallback', () => {
  it('retries a missing local image from its deterministic artifact asset', async () => {
    const ts = '1779995123.456789'
    const { container } = render(
      <MarkdownRenderer content="![screenshot](/tmp/shot.png)" messageTs={ts} slotKey="chat-7" />,
    )
    const image = container.querySelector('img')!
    expect(image.getAttribute('src')).toContain('/api/file-raw?path=')

    fireEvent.error(image)

    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 0)}/asset?session=chat-7`,
      )
    })
  })

  it('uses each image ordinal and falls back to the broken-image chip only after artifact failure', async () => {
    const ts = 'ts-two-images'
    const { container } = render(
      <MarkdownRenderer
        content={'![first](/tmp/first.png)\n\n![second](/tmp/second.png)'}
        messageTs={ts} slotKey="chat-7"
      />,
    )
    const images = Array.from(container.querySelectorAll('img'))
    expect(images).toHaveLength(2)

    fireEvent.error(images[1])
    await waitFor(() => {
      expect(container.querySelectorAll('img')[1]?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 1)}/asset?session=chat-7`,
      )
    })

    fireEvent.error(container.querySelectorAll('img')[1])
    await waitFor(() => expect(container.textContent).toContain('second'))
    expect(container.querySelectorAll('img')).toHaveLength(1)
  })

  it('counts openers hidden in widget markup the way the backend does', async () => {
    // The backend scans the RAW persisted text, so the opener inside the widget
    // title is ordinal 0 there and the real image is ordinal 1. The frontend must
    // not renumber from stripped block bodies.
    const ts = 'ts-widget-title'
    const md = '<mcwidget title="![ghost](/tmp/ghost.png)"><div>x</div></mcwidget>\n\n![real](/tmp/real.png)'
    // A real <WidgetFrame> mounts for the widget block; mirror the app's providers.
    vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MarkdownRenderer content={md} messageTs={ts} slotKey="chat-7" />
        </ThemeProvider>
      </QueryClientProvider>,
    )
    const real = container.querySelector('img[src*="real.png"]') as HTMLImageElement
    expect(real).not.toBeNull()

    fireEvent.error(real)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 1)}/asset?session=chat-7`,
      )
    })
  })

  it('prefers its exact raw ordinal and never a different copy or image', async () => {
    // Backend raw-text order: fence body (0), widget title (1), rendered image (2).
    // The block's raw span is known and nothing in it was stripped, so the
    // rendered image resolves to its own ordinal 2 and nothing else: not the
    // fence copy (0) of the same destination, not the ghost (1).
    const ts = 'ts-repeated-body'
    const md = [
      '```md',
      '![real](/tmp/real.png)',
      '```',
      '',
      '<mcwidget title="![ghost](/tmp/ghost.png)"><div>x</div></mcwidget>',
      '',
      '![real](/tmp/real.png)',
    ].join('\n')
    vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MarkdownRenderer content={md} messageTs={ts} slotKey="chat-7" />
        </ThemeProvider>
      </QueryClientProvider>,
    )
    const real = container.querySelector('img[src*="real.png"]') as HTMLImageElement
    expect(real).not.toBeNull()

    fireEvent.error(real)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 2)}/asset?session=chat-7`,
      )
    })
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => expect(container.querySelector('img')).toBeNull())
    expect(container.textContent).toContain('real')
  })

  it('resolves an image after a same-line widget close whose title repeats it', async () => {
    // Raw order: widget title opener (0), then the rendered image which starts
    // mid-line right after `</mcwidget>` (1). The block's raw span begins after
    // the tag, so the title copy counts as a raw occurrence BEFORE the block and
    // the rendered image resolves to its exact ordinal 1 and to nothing else.
    const ts = 'ts-empty-widget'
    const md = '<mcwidget title="![real](/tmp/real.png)"></mcwidget> ![real](/tmp/real.png)'
    vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MarkdownRenderer content={md} messageTs={ts} slotKey="chat-7" />
        </ThemeProvider>
      </QueryClientProvider>,
    )
    const real = container.querySelector('img[src*="real.png"]') as HTMLImageElement
    expect(real).not.toBeNull()

    fireEvent.error(real)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 1)}/asset?session=chat-7`,
      )
    })
    // Exact copy missing -> the broken-image chip. The title's copy (0) is the
    // same destination but is never tried: a source rewritten between the two
    // captures would make it a different picture.
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => expect(container.querySelector('img')).toBeNull())
    expect(container.textContent).toContain('real')
  })

  it('ignores openers the renderer strips before parsing (stray protocol tags)', async () => {
    // Raw order: ghost inside a stray <tool_use> (0), real (1). MarkdownBlock
    // strips the tag before react-markdown sees the text, so any positional
    // count over the rendered source would say 0.
    const ts = 'ts-stripped-tag'
    const md = '<tool_use>![ghost](/tmp/ghost.png)</tool_use>\n\n![real](/tmp/real.png)'
    const { container } = render(<MarkdownRenderer content={md} messageTs={ts} slotKey="chat-7" />)
    const real = container.querySelector('img[src*="real.png"]') as HTMLImageElement
    expect(real).not.toBeNull()

    fireEvent.error(real)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 1)}/asset?session=chat-7`,
      )
    })
  })

  it('starts over from the original file when the message version changes under it', async () => {
    // Variant browsing swaps messageTs without remounting the <img>. A fallback
    // reached for the OLD variant must not carry over: the new variant's own
    // file is tried first, and a later failure resolves the NEW ts's artifact.
    const md = '![shot](/tmp/shot.png)'
    const { container, rerender } = render(<MarkdownRenderer content={md} messageTs="ts-old" slotKey="chat-7" />)
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug('ts-old', 0)}/asset?session=chat-7`,
      )
    })
    rerender(<MarkdownRenderer content={md} messageTs="ts-new" slotKey="chat-7" />)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toContain('/api/file-raw?path=')
      expect(container.querySelector('img')?.getAttribute('src')).toContain('v=ts-new')
    })
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug('ts-new', 0)}/asset?session=chat-7`,
      )
    })
  })

  it('starts over when the owning session changes under the same ts and src', async () => {
    // A slot switch can reuse the <img> instance for a message with the same
    // ts and src in ANOTHER session. The fallback that session A exhausted, or
    // the copy it resolved under A's ownership, must not carry over: B tries its
    // own file first, and a later failure resolves B's copy under B's owner check.
    const md = '![shot](/tmp/shot.png)'
    const ts = 'ts-shared'
    const { container, rerender } = render(<MarkdownRenderer content={md} messageTs={ts} slotKey="chat-a" />)
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 0)}/asset?session=chat-a`,
      )
    })
    // Session A exhausts its only candidate: broken-image chip.
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => expect(container.querySelector('img')).toBeNull())
    rerender(<MarkdownRenderer content={md} messageTs={ts} slotKey="chat-b" />)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toContain('/api/file-raw?path=')
    })
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 0)}/asset?session=chat-b`,
      )
    })
  })

  it('makes no artifact request at all when it does not know its owning session', async () => {
    // The slug is (message ts, ordinal) only, and two slots can stamp a row in
    // the same clock tick; the `?session=` owner check on the asset endpoint is
    // what keeps a colliding slug from serving another chat's picture. A surface
    // that renders a message without its slot key (completion cards, the plain
    // transcript view) cannot make that claim, so a missing image there goes
    // straight to the broken-image chip rather than to an ownerless request.
    const ts = 'ts-ownerless'
    const { container } = render(<MarkdownRenderer content="![shot](/tmp/shot.png)" messageTs={ts} />)
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => expect(container.querySelector('img')).toBeNull())
    expect(container.innerHTML).not.toContain('/api/artifacts/')
    expect(container.textContent).toContain('shot')
  })

  it('numbers images over the stored message text, not the derived rendering', async () => {
    // AssistantMessage renders a text that protocol strippers have edited; the
    // backend numbered the STORED text. A stripper that swallowed an earlier
    // `![alt]` opener would otherwise shift this image onto its neighbour's slug,
    // a copy that exists and is the wrong picture.
    const ts = 'ts-derived'
    const stored = '![first](/tmp/first.png)\n\n![second](/tmp/second.png)'
    const rendered = '![second](/tmp/second.png)'
    const { container } = render(
      <MarkdownRenderer content={rendered} imageSource={stored} messageTs={ts} slotKey="chat-7" />,
    )
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 1)}/asset?session=chat-7`,
      )
    })
  })

  it('gives a repeated destination no fallback when the rendering is derived', async () => {
    // Same-destination images are told apart by their position, and positions
    // in a derived rendering do not line up with the stored text's; rather
    // than guess, a duplicate gets the broken-image chip.
    const ts = 'ts-derived-dup'
    const stored = 'note\n\n![a](/tmp/same.png)\n\n![b](/tmp/same.png)'
    const rendered = '![a](/tmp/same.png)\n\n![b](/tmp/same.png)'
    const { container } = render(
      <MarkdownRenderer content={rendered} imageSource={stored} messageTs={ts} slotKey="chat-7" />,
    )
    const images = Array.from(container.querySelectorAll('img'))
    expect(images).toHaveLength(2)
    fireEvent.error(images[0])
    await waitFor(() => expect(container.querySelectorAll('img')).toHaveLength(1))
    expect(container.innerHTML).not.toContain('/api/artifacts/')
  })

  it("never routes a reference-style image to a direct image's artifact", async () => {
    const ts = 'ts-reference-image'
    const md = '![ref image][shot]\n\n![direct](/tmp/direct.png)\n\n[shot]: /tmp/missing-ref.png'
    const { container } = render(<MarkdownRenderer content={md} messageTs={ts} slotKey="chat-7" />)
    const images = Array.from(container.querySelectorAll('img'))
    expect(images).toHaveLength(2)

    // The reference-style image has no registered artifact: on failure it goes
    // straight to the broken-image chip instead of borrowing ordinal 0.
    fireEvent.error(images[0])
    await waitFor(() => expect(container.querySelectorAll('img')).toHaveLength(1))
    expect(container.textContent).toContain('ref image')

    // The direct image is still ordinal 0 among direct openers.
    fireEvent.error(container.querySelector('img')!)
    await waitFor(() => {
      expect(container.querySelector('img')?.getAttribute('src')).toBe(
        `/api/artifacts/${deriveImageArtifactSlug(ts, 0)}/asset?session=chat-7`,
      )
    })
  })
})

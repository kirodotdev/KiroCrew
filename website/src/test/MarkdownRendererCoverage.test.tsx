import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import MarkdownRenderer, {
  Lightbox,
  BasePathCtx,
  artifactSlugFromHref,
  soleLinkInParagraph,
  fixCodeFences,
  rehypeSanitize,
} from '../components/MarkdownRenderer'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { api } from '../api/client'

// ── pure helpers ───────────────────────────────────────────────────────────

describe('artifactSlugFromHref', () => {
  it('keeps the raw segment when it is not valid percent-encoding', () => {
    // `%E0%A4%A` is a truncated UTF-8 escape: decodeURIComponent throws, and the
    // href must still resolve to *something* rather than dropping the link.
    expect(artifactSlugFromHref('/artifacts/%E0%A4%A')).toBe('%E0%A4%A')
  })

  it('decodes a well-formed encoded slug and ignores query/hash', () => {
    expect(artifactSlugFromHref('/artifacts/my%20plan?v=2#top')).toBe('my plan')
  })
})

describe('soleLinkInParagraph', () => {
  it('returns null when handed no node at all', () => {
    expect(soleLinkInParagraph()).toBeNull()
  })

  it('returns null for a paragraph that carries no anchor', () => {
    expect(
      soleLinkInParagraph({
        type: 'element',
        tagName: 'p',
        properties: {},
        children: [{ type: 'text', value: '   ' }],
      }),
    ).toBeNull()
  })
})

describe('fixCodeFences ordered-list escaping', () => {
  it('escapes a bare "N." line so it does not become an ordered list', () => {
    const out = fixCodeFences('Step count\n\n1.\n')
    expect(out).toContain('1\\.')
  })

  it('leaves a bare "N." inside a fence alone and resumes escaping after it closes', () => {
    // Fence tracking: the opening ``` arms it, the matching ``` disarms it, so
    // `1.` is code and `2.` is prose.
    const out = fixCodeFences('```\n1.\n```\n2.\n')
    expect(out).toContain('\n1.\n')
    expect(out).toContain('2\\.')
  })

  it('does not treat a shorter tilde run as closing a longer backtick fence', () => {
    const out = fixCodeFences('````\n3.\n``\n4.\n````\n5.\n')
    // Still inside the ```` fence for 3. and 4.; only 5. is prose.
    expect(out).not.toContain('3\\.')
    expect(out).not.toContain('4\\.')
    expect(out).toContain('5\\.')
  })
})

// ── sanitize / escaped-tag reconstruction ──────────────────────────────────

describe('rehypeSanitize reconstruction', () => {
  it('escapes an unknown tag nested inside another unknown tag', () => {
    const { container } = render(
      <MarkdownRenderer content={'<outertag><innertag>deep</innertag></outertag>'} />,
    )
    const text = container.textContent || ''
    expect(text).toContain('<outertag>')
    expect(text).toContain('<innertag>')
    expect(text).toContain('deep')
    expect(text).toContain('</innertag>')
    expect(text).toContain('</outertag>')
  })

  it('keeps an inline data:image src on an img but drops other data: URLs', () => {
    // Exercised against the exported policy directly: react-markdown's own
    // urlTransform rejects data: URLs before the component ever sees them, so
    // the allowance can only be observed at the plugin level.
    type SanitizeTree = Parameters<ReturnType<typeof rehypeSanitize>>[0]
    const png = 'data:image/png;base64,iVBORw0KGgo='
    const tree = {
      type: 'root',
      children: [
        { type: 'element', tagName: 'img', properties: { src: png }, children: [] },
        {
          type: 'element',
          tagName: 'img',
          properties: { src: 'data:text/html,<b>no</b>' },
          children: [],
        },
      ],
    } as unknown as SanitizeTree
    rehypeSanitize()(tree)
    const imgs = (tree as unknown as { children: { properties: Record<string, unknown> }[] }).children
    expect(imgs[0].properties.src).toBe(png)
    expect(imgs[1].properties.src).toBeUndefined()
  })
})

// ── markdown images ────────────────────────────────────────────────────────

describe('MarkdownRenderer images', () => {
  it('renders nothing for an image with no source', () => {
    const { container } = render(<MarkdownRenderer content={'![empty]()'} />)
    expect(container.querySelector('img')).toBeNull()
  })

  it('resolves a relative image against the document base path', () => {
    const { container } = render(
      <BasePathCtx.Provider value={'/notes/plan/readme.md'}>
        <MarkdownRenderer content={'![shot](pics/shot.png)'} />
      </BasePathCtx.Provider>,
    )
    const img = container.querySelector('img')!
    expect(img.getAttribute('src')).toBe(
      `/api/file-raw?path=${encodeURIComponent('/notes/plan/pics/shot.png')}`,
    )
  })

  it('swaps in an attachment fallback once the image fails to load', () => {
    const { container } = render(<MarkdownRenderer content={'![broken shot](/tmp/gone.png)'} />)
    const img = container.querySelector('img')!
    fireEvent.error(img)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('broken shot')
  })

  it('releases the layout placeholder once the image has loaded', () => {
    const { container } = render(<MarkdownRenderer content={'![shot](/tmp/shot.png)'} />)
    const img = () => container.querySelector('img')!
    expect(img().getAttribute('style')).toContain('min-height: 120px')
    fireEvent.load(img())
    expect(img().getAttribute('style') || '').not.toContain('min-height')
  })

  it('opens the lightbox for the clicked image and reports its siblings', () => {
    const details: { images: { src: string }[]; index: number }[] = []
    const spy = (e: Event) => details.push((e as CustomEvent).detail)
    window.addEventListener('lightbox', spy)
    try {
      const { container } = render(
        <MarkdownRenderer content={'![one](/tmp/one.png)\n\n![two](/tmp/two.png)'} />,
      )
      const imgs = container.querySelectorAll('img')
      expect(imgs).toHaveLength(2)
      fireEvent.click(imgs[1])
      expect(details).toHaveLength(1)
      expect(details[0].index).toBe(1)
      expect(details[0].images).toHaveLength(2)
    } finally {
      window.removeEventListener('lightbox', spy)
    }
  })
})

// ── streaming decorations ──────────────────────────────────────────────────

describe('MarkdownRenderer streaming decorations', () => {
  it('glows only the trailing words of a long tail, cut at a word boundary', () => {
    const content = 'The gateway finished indexing every skill in the workspace already'
    const { container } = render(<MarkdownRenderer content={content} streaming glow />)
    const glowEl = container.querySelector('.streaming-glow')
    expect(glowEl).not.toBeNull()
    const tail = glowEl!.textContent || ''
    // Shorter than the whole paragraph, and starts at a word boundary.
    expect(tail.length).toBeLessThan(content.length)
    expect(content.endsWith(tail.trim())).toBe(true)
    expect(tail.startsWith(' ')).toBe(true)
  })

  it('falls back to a root-level caret when the tail block holds only code', () => {
    // An indented code block is a <pre>, which both the caret walk and the glow
    // walk skip — so there is no eligible text node to anchor to.
    const { container } = render(<MarkdownRenderer content={'    indented only'} streaming glow />)
    const caret = container.querySelector('.streaming-caret')
    expect(caret).not.toBeNull()
    expect(caret!.closest('pre')).toBeNull()
    expect(container.querySelector('.streaming-glow')).toBeNull()
  })

  it('keeps the caret out of a fenced-looking pre while still glowing the prose', () => {
    const { container } = render(
      <MarkdownRenderer content={'    held code\n\nlive prose tail'} streaming glow />,
    )
    const caret = container.querySelector('.streaming-caret')
    expect(caret).not.toBeNull()
    expect(caret!.closest('pre')).toBeNull()
  })

  it('decorates a trailing text run that sits directly at the document root', () => {
    // A block-level raw HTML element followed by text on the same HTML block
    // leaves that text as a ROOT child rather than inside a <p>, which is the
    // one shape where the caret and the glow splice into the root's children.
    const { container } = render(
      <MarkdownRenderer content={'<div>lead block</div> trailing tail words'} streaming glow />,
    )
    const glowEl = container.querySelector('.streaming-glow')
    expect(glowEl).not.toBeNull()
    expect(glowEl!.textContent).toContain('trailing tail words')
    expect(glowEl!.closest('p')).toBeNull()
    expect(container.querySelector('.streaming-caret')).not.toBeNull()
  })

  it('does not defer a trailing table when the streamed tail ends on a blank line', () => {
    const { container } = render(
      <MarkdownRenderer content={'| a | b |\n| --- | --- |\n| 1 | 2 |\n\n'} streaming glow />,
    )
    expect(container.querySelector('table')).not.toBeNull()
  })

  it('settles the smooth reveal edge to ft-idle once the content stops changing', async () => {
    vi.useFakeTimers()
    try {
      const { container } = render(<MarkdownRenderer content={'streaming words'} streaming smooth glow />)
      const root = container.querySelector('.ft-anim-smooth')!
      expect(root.className).not.toContain('ft-idle')
      await act(async () => { vi.advanceTimersByTime(600) })
      expect(container.querySelector('.ft-anim-smooth')!.className).toContain('ft-idle')
    } finally {
      vi.useRealTimers()
    }
  })

  it('renders raw source verbatim in rawMode', () => {
    const { container } = render(<MarkdownRenderer content={'# Heading\n\n**bold**'} rawMode />)
    const pre = container.querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre!.textContent).toBe('# Heading\n\n**bold**')
    expect(container.querySelector('h1')).toBeNull()
  })
})

// ── heading / structural component overrides ───────────────────────────────

describe('MarkdownRenderer structural elements', () => {
  it('slugifies heading ids at every level, reading through nested inline markup', () => {
    const md = [
      '# Top *One*',
      '### Third `here`',
      '#### Fourth',
      '##### Fifth',
      '###### Sixth',
    ].join('\n\n')
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.querySelector('h1')!.id).toBe('top-one')
    expect(container.querySelector('h3')!.id).toBe('third-here')
    expect(container.querySelector('h4')!.id).toBe('fourth')
    expect(container.querySelector('h5')!.id).toBe('fifth')
    expect(container.querySelector('h6')!.id).toBe('sixth')
  })

  it('uses the image alt text when a heading is just an image', () => {
    const { container } = render(<MarkdownRenderer content={'## ![Logo Mark](/tmp/logo.png)'} />)
    expect(container.querySelector('h2')!.id).toBe('logo-mark')
  })

  it('renders blockquotes, rules and fenced code through the overrides', () => {
    const { container } = render(
      <MarkdownRenderer content={'> quoted line\n\n---\n\n```js\nconst a = 1\n```'} />,
    )
    expect(container.querySelector('blockquote')).not.toBeNull()
    expect(container.querySelector('hr')).not.toBeNull()
    expect(container.textContent).toContain('const a = 1')
  })

  it('renders a fence nested in a blockquote through the inline code override', async () => {
    // The block assembler only splits fences that START a line, so a quoted
    // fence stays inside the markdown block and reaches the `code` override.
    // Note the fence still renders as a code block rather than staying inside
    // the quote: fixCodeFences' "blank line before a glued opening fence" pass
    // rewrites `> ```js` to `> ` + a top-level fence, which empties the quote
    // and carries the `> ` markers into the code text. Asserted here as the
    // CURRENT behaviour, not as the desirable one.
    const { container } = render(
      <MarkdownRenderer content={'> ```js\n> const quoted = 1\n> ```'} />,
    )
    await waitFor(() => expect(container.textContent).toContain('const quoted = 1'))
    expect(container.querySelector('.code-block')).not.toBeNull()
  })
})

// ── block routing ──────────────────────────────────────────────────────────

describe('MarkdownRenderer block routing', () => {
  const diff = '```diff\n@@ -1 +1 @@\n-old line\n+new line\n```'

  it('takes the diff path hint from the preceding chat text', async () => {
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={`Updated \`/tmp/report.md\`:\n...\n\n${diff}`} onFileOpen={onFileOpen} />,
    )
    await waitFor(() => expect(container.textContent).toContain('new line'))
    expect(container.textContent).toContain('report.md')
  })

  it('leaves the diff header-less when the preceding text names no path', async () => {
    const { container } = render(<MarkdownRenderer content={`Here is the change:\n\n${diff}`} />)
    await waitFor(() => expect(container.textContent).toContain('new line'))
    expect(container.textContent).not.toContain('/tmp/')
  })

  it('wraps a streaming diff in the smooth-resize shell', async () => {
    const { container } = render(
      <MarkdownRenderer content={`Wrote /tmp/out.txt\n\n${diff}`} streaming smooth />,
    )
    await waitFor(() => expect(container.textContent).toContain('new line'))
  })

  it('shows a placeholder for an unclosed widget and the frame once it closes', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const wrap = (ui: ReactNode) =>
      render(
        <QueryClientProvider client={queryClient}>
          <ThemeProvider>{ui}</ThemeProvider>
        </QueryClientProvider>,
      )
    const open = wrap(
      <MarkdownRenderer content={'<mcwidget title="Queue">\n<p>partial</p>'} streaming />,
    )
    expect(open.container.querySelector('iframe')).toBeNull()
    open.unmount()

    const done = wrap(
      <MarkdownRenderer
        content={'<mcwidget title="Queue">\n<p>done</p>\n</mcwidget>'}
        messageTs={'2026-08-11T00:00:00Z'}
      />,
    )
    await waitFor(() => expect(done.container.querySelector('iframe')).not.toBeNull())
    done.unmount()
    queryClient.clear()
  })
})

// ── path chip activation fallback ──────────────────────────────────────────

describe('MarkdownRenderer path chip with no file handler', () => {
  const realFetch = globalThis.fetch
  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  it('reveals the file in the OS file manager when no onFileOpen is wired', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, headers: new Headers({ 'X-Path-Kind': 'file' }) } as Response),
    ) as unknown as typeof fetch
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
    const { container } = render(<MarkdownRenderer content={'`/tmp/notes/plan.md`'} />)
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]') as HTMLElement | null
      expect(c).not.toBeNull()
      return c!
    })
    fireEvent.click(chip)
    expect(reveal).toHaveBeenCalledWith('/tmp/notes/plan.md')
  })
})

// ── lightbox toolbar, dismissal and download ───────────────────────────────

describe('Lightbox toolbar and dismissal', () => {
  function open(images: { src: string; alt?: string }[], index = 0) {
    window.dispatchEvent(new CustomEvent('lightbox', {
      detail: { images: images.map(i => ({ src: i.src, alt: i.alt ?? '' })), index },
    }))
  }

  it('steps zoom with the toolbar buttons and resets to fit', () => {
    const { container, getByLabelText } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const style = () => container.querySelector('img')!.getAttribute('style') || ''
    // At fit, both shrink controls are inert.
    expect((getByLabelText('Zoom out (-)') as HTMLButtonElement).disabled).toBe(true)
    expect((getByLabelText('Reset zoom (0)') as HTMLButtonElement).disabled).toBe(true)
    act(() => { fireEvent.click(getByLabelText('Zoom in (+)')) })
    expect(style()).toContain('scale(1.5)')
    act(() => { fireEvent.click(getByLabelText('Zoom in (+)')) })
    expect(style()).toContain('scale(2)')
    act(() => { fireEvent.click(getByLabelText('Zoom out (-)')) })
    expect(style()).toContain('scale(1.5)')
    act(() => { fireEvent.click(getByLabelText('Reset zoom (0)')) })
    expect(style()).toContain('scale(1)')
  })

  it('closes via the close button', () => {
    const { container, getByLabelText } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    act(() => { fireEvent.click(getByLabelText('Close')) })
    expect(container.firstChild).toBeNull()
  })

  it('closes when the backdrop itself is clicked', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const backdrop = container.querySelector('[role="button"]')!
    act(() => { fireEvent.click(backdrop) })
    expect(container.firstChild).toBeNull()
  })

  it('closes when a lightbox event arrives with no detail', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    expect(container.querySelector('img')).not.toBeNull()
    act(() => { window.dispatchEvent(new CustomEvent('lightbox')) })
    expect(container.firstChild).toBeNull()
  })

  it('suppresses the native image drag so it cannot be dropped out of the viewer', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const dragEvent = fireEvent.dragStart(container.querySelector('img')!)
    expect(dragEvent).toBe(false)
  })

  it('does not pan while the image is at fit, and ignores moves with no pointer down', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const imgEl = () => container.querySelector('img')!
    Object.defineProperty(imgEl(), 'offsetWidth', { configurable: true, value: 3000 })
    Object.defineProperty(imgEl(), 'offsetHeight', { configurable: true, value: 3000 })
    // pointerDown at fit is a no-op, so the following move has nothing to drag.
    act(() => { fireEvent.pointerDown(imgEl(), { clientX: 500, clientY: 500, pointerId: 1 }) })
    act(() => { fireEvent.pointerMove(imgEl(), { clientX: 200, clientY: 200, pointerId: 1 }) })
    expect(imgEl().getAttribute('style')).toContain('translate(0px, 0px)')
    expect(imgEl().className).toContain('cursor-default')
    // A stray move with no preceding down is ignored even once zoomed.
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    act(() => { fireEvent.pointerMove(imgEl(), { clientX: 40, clientY: 40, pointerId: 1 }) })
    expect(imgEl().getAttribute('style')).toContain('translate(0px, 0px)')
  })
})

describe('Lightbox download', () => {
  let clicks: { download: string; href: string }[]
  let clickSpy: ReturnType<typeof vi.spyOn>
  let createObjectURL: typeof URL.createObjectURL
  let revokeObjectURL: typeof URL.revokeObjectURL
  let fetchMock: ReturnType<typeof vi.fn>
  let openMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    clicks = []
    createObjectURL = URL.createObjectURL
    revokeObjectURL = URL.revokeObjectURL
    URL.createObjectURL = vi.fn(() => 'blob:shot')
    URL.revokeObjectURL = vi.fn()
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push({ download: this.download, href: this.getAttribute('href') ?? '' })
    })
    fetchMock = vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, blob: () => Promise.resolve(new Blob(['bytes'])) } as unknown as Response),
    )
    openMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('open', openMock)
  })

  afterEach(() => {
    clickSpy.mockRestore()
    URL.createObjectURL = createObjectURL
    URL.revokeObjectURL = revokeObjectURL
    vi.unstubAllGlobals()
  })

  function open(src: string, alt = '') {
    window.dispatchEvent(new CustomEvent('lightbox', { detail: { images: [{ src, alt }], index: 0 } }))
  }

  it('downloads the served bytes, naming the file from the ?path= query', async () => {
    vi.useFakeTimers()
    try {
      const { getByLabelText } = render(<Lightbox />)
      act(() => open('/api/file-raw?path=%2Ftmp%2Fshots%2Fpanel.png', 'panel'))
      act(() => { fireEvent.click(getByLabelText('Download image')) })
      await vi.waitFor(() => expect(clicks).toHaveLength(1))
      expect(clicks[0]).toEqual({ download: 'panel.png', href: 'blob:shot' })
      expect(fetchMock).toHaveBeenCalledWith('/api/file-raw?path=%2Ftmp%2Fshots%2Fpanel.png')
      expect(document.querySelector('a[download]')).toBeNull()
      // The object URL is released shortly after the click.
      vi.advanceTimersByTime(1000)
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:shot')
    } finally {
      vi.useRealTimers()
    }
  })

  it('names a remote image from its URL basename', async () => {
    const { getByLabelText } = render(<Lightbox />)
    act(() => open('https://example.invalid/media/diagram.svg', 'diagram'))
    act(() => { fireEvent.click(getByLabelText('Download image')) })
    await waitFor(() => expect(clicks).toHaveLength(1))
    expect(clicks[0].download).toBe('diagram.svg')
  })

  it('falls back to the alt text when the source carries no filename', async () => {
    const { getByLabelText } = render(<Lightbox />)
    act(() => open('data:image/png;base64,iVBORw0KGgo=', 'Kiro Crew banner'))
    act(() => { fireEvent.click(getByLabelText('Download image')) })
    await waitFor(() => expect(clicks).toHaveLength(1))
    expect(clicks[0].download).toBe('Kiro_Crew_banner')
  })

  it('opens the image in a new tab when the fetch is refused', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 403 } as unknown as Response)
    const { getByLabelText } = render(<Lightbox />)
    act(() => open('https://example.invalid/blocked.png', 'blocked'))
    act(() => { fireEvent.click(getByLabelText('Download image')) })
    await waitFor(() => expect(openMock).toHaveBeenCalled())
    expect(openMock).toHaveBeenCalledWith('https://example.invalid/blocked.png', '_blank', 'noopener,noreferrer')
    expect(clicks).toHaveLength(0)
  })

  it('downloads the current image on the "d" shortcut', async () => {
    const { getByLabelText } = render(<Lightbox />)
    act(() => open('/api/file-raw?path=%2Ftmp%2Fshot.png', 'shot'))
    // The toolbar button is present, but this path is the keyboard one.
    expect(getByLabelText('Download image')).not.toBeNull()
    act(() => { fireEvent.keyDown(window, { key: 'd' }) })
    await waitFor(() => expect(clicks).toHaveLength(1))
    expect(clicks[0].download).toBe('shot.png')
  })

  it('leaves the "d" shortcut alone while the user is typing in a field', async () => {
    render(<Lightbox />)
    act(() => open('/api/file-raw?path=%2Ftmp%2Fshot.png', 'shot'))
    const input = document.createElement('input')
    document.body.appendChild(input)
    try {
      act(() => { fireEvent.keyDown(input, { key: 'd' }) })
      await Promise.resolve()
      expect(clicks).toHaveLength(0)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      input.remove()
    }
  })
})

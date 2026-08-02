import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, waitFor } from '@testing-library/react'
import MarkdownRenderer, { Lightbox, dispatchLightbox, isPathCandidate } from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { api } from '../api/client'

type LightboxDetail = { images: { src: string; alt: string }[]; index: number }

describe('MarkdownRenderer list indentation', () => {
  it('renders ul with pl-8 and marker:text-muted', () => {
    const { container } = render(<MarkdownRenderer content={'- a\n- b'} />)
    const ul = container.querySelector('ul')
    expect(ul).not.toBeNull()
    expect(ul!.className).toContain('pl-8')
    expect(ul!.className).toContain('marker:text-muted')
  })

  it('renders ol with pl-8 and marker:text-muted', () => {
    const { container } = render(<MarkdownRenderer content={'1. a\n2. b'} />)
    const ol = container.querySelector('ol')
    expect(ol).not.toBeNull()
    expect(ol!.className).toContain('pl-8')
    expect(ol!.className).toContain('marker:text-muted')
  })
})

describe('MarkdownRenderer streaming caret', () => {
  it('appends an inline streaming caret after the trailing text while streaming', () => {
    const { container } = render(<MarkdownRenderer content={'Hello world'} streaming glow />)
    const caret = container.querySelector('.streaming-caret')
    expect(caret).not.toBeNull()
    // Inline placement: the caret lives inside the paragraph (same line as the
    // last word), not as a bare block-level sibling of the root container.
    expect(caret!.closest('p')).not.toBeNull()
  })

  it('does not render a caret when not streaming', () => {
    const { container } = render(<MarkdownRenderer content={'Hello world'} />)
    expect(container.querySelector('.streaming-caret')).toBeNull()
  })

  it('places the caret AFTER a trailing inline code span (not before it)', () => {
    const { container } = render(<MarkdownRenderer content={'Hello `world`'} streaming glow />)
    const code = container.querySelector('code')
    const caret = container.querySelector('.streaming-caret')
    expect(code).not.toBeNull()
    expect(caret).not.toBeNull()
    // The caret must follow the <code> element in document order.
    expect(!!(code!.compareDocumentPosition(caret!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true)
  })
})

describe('MarkdownRenderer dollar-sign handling (currency vs math)', () => {
  it('treats single-$ currency as plain text, not inline math', () => {
    // Regression for: chat messages like `$9.99` accidentally parsed as
    // inline math spanning multiple $ signs, crashing KaTeX + React commit.
    const { container } = render(
      <MarkdownRenderer content={'Product A = $9.99 and Product B = $19.95'} />
    )
    // No KaTeX math span should be produced
    expect(container.querySelector('.katex')).toBeNull()
    // The raw dollar amounts should still appear as text
    expect(container.textContent).toContain('$9.99')
    expect(container.textContent).toContain('$19.95')
  })

  it('does not treat currency + em-dash + en-dash as math (prior crash trigger)', () => {
    // En-dash (U+2013) inside a would-be math block triggered KaTeX strict
    // warning -> bad HTML -> React commit crash ("String contains an invalid
    // character"). With singleDollarTextMath=false, this should render cleanly.
    const content = 'Total — see line items 1 – 3: $10.00 plus $5.00 tax'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('$10.00')
    expect(container.textContent).toContain('$5.00')
  })

  it('still renders $$...$$ display math via KaTeX', () => {
    // Regression guard: disabling singleDollarTextMath must NOT break real math.
    const { container } = render(<MarkdownRenderer content={'$$a^2 + b^2 = c^2$$'} />)
    // Display math produces a .katex-display wrapper or at least a .katex span
    const katex = container.querySelector('.katex, .katex-display')
    expect(katex).not.toBeNull()
  })
})

describe('MarkdownRenderer XSS sanitization', () => {
  it('strips iframe elements from markdown', () => {
    const { container } = render(
      <MarkdownRenderer content={'<iframe srcdoc="<script>alert(1)</script>"></iframe>'} />
    )
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('strips script elements from markdown', () => {
    const { container } = render(
      <MarkdownRenderer content={'<script>fetch("/api/config")</script>'} />
    )
    expect(container.querySelector('script')).toBeNull()
  })

  it('strips event handler attributes', () => {
    const { container } = render(
      <MarkdownRenderer content={'<img src="x" onerror="alert(1)">'} />
    )
    const img = container.querySelector('img')
    expect(img?.getAttribute('onerror')).toBeNull()
  })

  it('strips javascript: hrefs', () => {
    const { container } = render(
      <MarkdownRenderer content={'<a href="javascript:alert(1)">click</a>'} />
    )
    const a = container.querySelector('a')
    // href is deleted entirely — either element has no href or doesn't render as <a>
    if (a) {
      expect(a.getAttribute('href')).toBeNull()
    }
    // Verify no javascript: anywhere in the output
    expect(container.innerHTML).not.toContain('javascript:')
  })

  it('preserves safe HTML elements like details/summary', () => {
    const { container } = render(
      <MarkdownRenderer content={'<details><summary>Info</summary>Content</details>'} />
    )
    expect(container.querySelector('details')).not.toBeNull()
    expect(container.querySelector('summary')).not.toBeNull()
  })

  it('preserves safe elements like kbd and mark', () => {
    const { container } = render(
      <MarkdownRenderer content={'Press <kbd>Ctrl+C</kbd> to copy'} />
    )
    expect(container.querySelector('kbd')).not.toBeNull()
  })

  it('strips javascript: with embedded control characters (bypass variant)', () => {
    const { container } = render(
      <MarkdownRenderer content={'<a href="java\tscript:alert(1)">click</a>'} />
    )
    expect(container.innerHTML).not.toContain('javascript:')
  })

  it('strips data: URI XSS payloads in href', () => {
    const { container } = render(
      <MarkdownRenderer content={'<a href="data:text/html,<script>alert(1)</script>">click</a>'} />
    )
    expect(container.innerHTML).not.toContain('data:text/html')
  })
})

describe('MarkdownRenderer GFM task-list checkboxes', () => {
  it('renders - [ ] and - [x] as checkbox inputs', () => {
    const { container } = render(
      <MarkdownRenderer content={'- [ ] unchecked\n- [x] checked'} />
    )
    const checkboxes = container.querySelectorAll('input[type="checkbox"]')
    expect(checkboxes).toHaveLength(2)
    expect((checkboxes[0] as HTMLInputElement).checked).toBe(false)
    expect((checkboxes[1] as HTMLInputElement).checked).toBe(true)
    expect((checkboxes[0] as HTMLInputElement).disabled).toBe(true)
  })

  it('still strips non-checkbox input elements (XSS safety)', () => {
    const { container } = render(
      <MarkdownRenderer content={'<input type="text" value="xss">'} />
    )
    expect(container.querySelector('input[type="text"]')).toBeNull()
  })

  it('renders task-list ul without bullet disc', () => {
    const { container } = render(
      <MarkdownRenderer content={'- [ ] foo\n- [x] bar'} />
    )
    const ul = container.querySelector('ul')
    expect(ul!.className).toContain('list-none')
    expect(ul!.className).not.toContain('list-disc')
  })
})

/**
 * Stage 1 of path-chip detection: the syntactic pre-filter.
 *
 * Pure and fetch-free. Its job is NOT to decide whether something is a path —
 * that needs a stat — but to reject strings that cannot be one, so the probe is
 * never spent on a git ref or a MIME type.
 */
describe('isPathCandidate — path chip pre-filter', () => {
  it('accepts rooted, home-relative and explicitly relative paths', () => {
    expect(isPathCandidate('/Users/me/project/KiroCrew')).toBe(true)
    expect(isPathCandidate('/home/user/reports/2026-05-17T05:46.md')).toBe(true)
    expect(isPathCandidate('~/.kiro/crew/workspace')).toBe(true)
    expect(isPathCandidate('./src/index.ts')).toBe(true)
    expect(isPathCandidate('../sibling/file.json')).toBe(true)
  })

  it('accepts a bare relative path when the last segment has an extension', () => {
    expect(isPathCandidate('src/main.py')).toBe(true)
    expect(isPathCandidate('website/src/components/MarkdownRenderer.tsx')).toBe(true)
  })

  it('rejects git refs — the regression that made this gate necessary', () => {
    // These rendered as clickable "files" and could only ever 404.
    expect(isPathCandidate('refs/heads/fix/investigation-record-403')).toBe(false)
    expect(isPathCandidate('origin/main')).toBe(false)
    expect(isPathCandidate('HEAD')).toBe(false)
  })

  it('rejects other slash-separated identifiers that are not paths', () => {
    expect(isPathCandidate('owner/repo')).toBe(false)
    expect(isPathCandidate('kirodotdev/KiroCrew')).toBe(false)
    expect(isPathCandidate('text/plain')).toBe(false)
    expect(isPathCandidate('@scope/pkg')).toBe(false)
    expect(isPathCandidate('2026/08/02')).toBe(false)
    expect(isPathCandidate('and/or')).toBe(false)
  })

  it('rejects URLs and strings with no separator at all', () => {
    expect(isPathCandidate('https://example.com/path/file.txt')).toBe(false)
    expect(isPathCandidate('4a72aec5f04d3f44ba8042931226db051242d48a')).toBe(false)
    expect(isPathCandidate('someIdentifier')).toBe(false)
  })
})

/**
 * Stage 2: the stat gate. A candidate is inert until the backend confirms what
 * it is, and a directory gets a folder affordance rather than the file viewer's
 * "not found" placeholder.
 */
describe('MarkdownRenderer path chips — stat gate', () => {
  const realFetch = globalThis.fetch

  /** Stub the HEAD probe with a real Headers instance, so header lookup behaves
   *  exactly as it does against the live endpoint. */
  function stubKind(kind: 'file' | 'dir' | null, ok = true) {
    const headers = new Headers(kind ? { 'X-Path-Kind': kind } : {})
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ ok, status: ok ? 200 : 404, headers } as Response),
    ) as unknown as typeof fetch
  }

  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  it('renders a confirmed file as a clickable chip with a leading glyph', async () => {
    stubKind('file')
    const { container } = render(<MarkdownRenderer content={'`/home/user/a.md`'} />)
    await waitFor(() => {
      const code = container.querySelector('code')!
      expect(code.className).toContain('cursor-pointer')
      expect(code.dataset.pathKind).toBe('file')
      expect(code.dataset.path).toBe('/home/user/a.md')
      // The glyph is what distinguishes an actionable chip from an inert one at
      // rest — without it the two differ only on hover.
      expect(code.querySelector('svg')).not.toBeNull()
    })
  })

  it('leaves an inert chip glyph-free, so the affordance stays meaningful', async () => {
    stubKind(null, false)
    const { container } = render(<MarkdownRenderer content={'`/home/user/ghost.md`'} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    const code = container.querySelector('code')!
    expect(code.querySelector('svg')).toBeNull()
    expect(code.className).not.toContain('cursor-pointer')
  })

  it('renders a confirmed directory as a folder chip, not a broken file link', async () => {
    stubKind('dir', false)
    const { container } = render(<MarkdownRenderer content={'`/Users/me/workspace/KiroCrew`'} />)
    await waitFor(() => {
      const code = container.querySelector('code')!
      expect(code.dataset.pathKind).toBe('dir')
      expect(code.className).toContain('cursor-pointer')
      // Folder glyph distinguishes it from a file chip at a glance.
      expect(code.querySelector('svg')).not.toBeNull()
    })
  })

  it('leaves a path that is not on disk as plain text', async () => {
    stubKind(null, false) // 404 + X-Path-Kind: missing (header absent here)
    const { container } = render(<MarkdownRenderer content={'`/home/user/ghost.md`'} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    const code = container.querySelector('code')!
    expect(code.className).not.toContain('cursor-pointer')
    expect(code.dataset.pathKind).toBeUndefined()
  })

  it('discloses the resolved path in the tooltip', async () => {
    // Surrounding markup can visually cover a chip (raw HTML plus absolute
    // positioning — possible on the base commit too, and there it needs no
    // probe at all). A native tooltip paints above page content and any overlay
    // must be pointer-events-none to pass the click through, so hover remains a
    // trustworthy channel for "what will this actually open?".
    stubKind('file')
    const { container } = render(<MarkdownRenderer content={'`/home/user/a.md`'} />)
    await waitFor(() => {
      const code = container.querySelector('code[data-path-kind]')!
      expect(code.getAttribute('title')).toContain('/home/user/a.md')
    })
  })

  it('never probes a non-candidate — the pre-filter saves the request', async () => {
    stubKind('file')
    render(<MarkdownRenderer content={'`refs/heads/fix/investigation-record-403`'} />)
    await Promise.resolve()
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('does not probe while the message is still streaming', async () => {
    stubKind('file')
    // Mid-stream, '/Users' is itself a valid candidate en route to the real
    // path; probing every chunk would flash the wrong affordance.
    render(<MarkdownRenderer content={'`/Users/me/pro`'} streaming />)
    await Promise.resolve()
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('probes each distinct path once however many chips mention it', async () => {
    stubKind('file')
    render(<MarkdownRenderer content={'`/home/user/a.md` and again `/home/user/a.md`'} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    expect((globalThis.fetch as unknown as { mock: { calls: unknown[] } }).mock.calls).toHaveLength(1)
  })
})

describe('MarkdownRenderer path chips — activation routing', () => {
  const realFetch = globalThis.fetch

  function stubKind(kind: 'file' | 'dir', ok: boolean) {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ ok, status: ok ? 200 : 404, headers: new Headers({ 'X-Path-Kind': kind }) } as Response),
    ) as unknown as typeof fetch
  }

  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  it('routes a file chip to onFileOpen', async () => {
    stubKind('file', true)
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/a.md`'} onFileOpen={onFileOpen} />,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind]')
      expect(c).not.toBeNull()
      return c!
    })
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith('/home/user/a.md')
  })

  it('routes a directory chip to onFolderOpen, never onFileOpen', async () => {
    stubKind('dir', false)
    const onFileOpen = vi.fn()
    const onFolderOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'`/Users/me/ws`'} onFileOpen={onFileOpen} onFolderOpen={onFolderOpen} />,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="dir"]')
      expect(c).not.toBeNull()
      return c!
    })
    fireEvent.click(chip)
    expect(onFolderOpen).toHaveBeenCalledWith('/Users/me/ws')
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('falls back to reveal-in-OS for a directory when no folder handler is wired', async () => {
    stubKind('dir', false)
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
    const { container } = render(<MarkdownRenderer content={'`/Users/me/ws`'} onFileOpen={vi.fn()} />)
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="dir"]')
      expect(c).not.toBeNull()
      return c!
    })
    fireEvent.click(chip)
    expect(reveal).toHaveBeenCalledWith('/Users/me/ws')
  })

  it('shift-click reveals instead of opening', async () => {
    stubKind('file', true)
    const onFileOpen = vi.fn()
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/a.md`'} onFileOpen={onFileOpen} />,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind]')
      expect(c).not.toBeNull()
      return c!
    })
    fireEvent.click(chip, { shiftKey: true })
    expect(reveal).toHaveBeenCalledWith('/home/user/a.md')
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('is keyboard reachable: Enter activates a chip', async () => {
    stubKind('file', true)
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/a.md`'} onFileOpen={onFileOpen} />,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind]') as HTMLElement | null
      expect(c).not.toBeNull()
      return c!
    })
    // The chip advertises itself as a button, so it must answer the keyboard.
    expect(chip.getAttribute('role')).toBe('button')
    expect(chip.tabIndex).toBe(0)
    fireEvent.keyDown(chip, { key: 'Enter' })
    expect(onFileOpen).toHaveBeenCalledWith('/home/user/a.md')
  })
})

describe('MarkdownRenderer path chips — forgery resistance', () => {
  const realFetch = globalThis.fetch
  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  /**
   * The chip's `data-path` / `data-path-kind` attributes ARE the activation
   * contract for the container's delegated handler. rehypeSanitize allowlists
   * every `data-*` attribute (isAllowedAttr: `k.startsWith('data')`), so raw
   * HTML in a message reaches the DOM with them intact — a forged chip could
   * otherwise open a hidden path that differs from its visible text, which is
   * strictly worse than the old behaviour (that read textContent, so the user
   * always opened what they saw).
   */
  it('ignores a chip forged via raw HTML with a hidden path', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, headers: new Headers({ 'X-Path-Kind': 'file' }) } as Response),
    ) as unknown as typeof fetch
    const onFileOpen = vi.fn()
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
    const { container } = render(
      <MarkdownRenderer
        content={'<code data-path-kind="file" data-path="/etc/hosts">totally harmless</code>'}
        onFileOpen={onFileOpen}
      />,
    )
    const code = container.querySelector('code')!
    // The component must not let an inbound data-path* reach the DOM.
    expect(code.dataset.path).toBeUndefined()
    expect(code.dataset.pathKind).toBeUndefined()
    fireEvent.click(code)
    expect(onFileOpen).not.toHaveBeenCalled()
    expect(reveal).not.toHaveBeenCalled()
  })

  it('ignores a chip whose data-path disagrees with its visible text', async () => {
    // Defence in depth: even if an attribute reached the DOM by another route,
    // activation must never act on a path the user cannot read.
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={'plain text'} onFileOpen={onFileOpen} />,
    )
    const root = container.querySelector('[data-image-scope]')!
    const forged = document.createElement('code')
    forged.setAttribute('data-path-kind', 'file')
    forged.setAttribute('data-path', '/etc/shadow')
    forged.textContent = '/home/user/innocent.md'
    root.appendChild(forged)
    fireEvent.click(forged)
    expect(onFileOpen).not.toHaveBeenCalled()
  })
})

describe('Lightbox keyboard navigation', () => {
  function open(images: { src: string; alt?: string }[], index = 0) {
    window.dispatchEvent(new CustomEvent('lightbox', {
      detail: { images: images.map(i => ({ src: i.src, alt: i.alt ?? '' })), index },
    }))
  }

  it('renders nothing initially', () => {
    const { container } = render(<Lightbox />)
    expect(container.firstChild).toBeNull()
  })

  it('closes on Escape', async () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    expect(container.querySelector('img')).not.toBeNull()
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(container.firstChild).toBeNull()
  })

  it('ArrowRight advances index, ArrowLeft retreats, both clamp at the ends', () => {
    const { container } = render(<Lightbox />)
    act(() => open([
      { src: 'a.png', alt: 'a' },
      { src: 'b.png', alt: 'b' },
      { src: 'c.png', alt: 'c' },
    ], 0))
    expect(container.querySelector('img')!.getAttribute('src')).toBe('a.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('b.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('c.png')
    // Clamp at end
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('c.png')
    // Walk back
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('b.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('a.png')
    // Clamp at start
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('a.png')
  })

  it('arrow keys are no-ops with a single image', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'only.png', alt: 'only' }]))
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('only.png')
  })

  it('keyboard events are ignored when the viewer is closed', () => {
    const { container } = render(<Lightbox />)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(container.firstChild).toBeNull()
  })

  it('accepts the legacy { src, alt } payload as a single-image set', () => {
    const { container } = render(<Lightbox />)
    act(() => {
      window.dispatchEvent(new CustomEvent('lightbox', { detail: { src: 'legacy.png', alt: 'legacy' } }))
    })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('legacy.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('legacy.png')
  })

  it('dispatchLightbox reports all sibling images and the clicked index', () => {
    const events: LightboxDetail[] = []
    const spy = (e: Event) => events.push((e as CustomEvent).detail)
    window.addEventListener('lightbox', spy)
    const root = document.createElement('div')
    root.setAttribute('data-image-scope', '')
    const a = document.createElement('img'); a.src = 'https://x.invalid/a.png'; a.alt = 'a'; a.setAttribute('data-lightbox-image', '')
    const b = document.createElement('img'); b.src = 'https://x.invalid/b.png'; b.alt = 'b'; b.setAttribute('data-lightbox-image', '')
    const c = document.createElement('img'); c.src = 'https://x.invalid/c.png'; c.alt = 'c'; c.setAttribute('data-lightbox-image', '')
    root.append(a, b, c)
    document.body.appendChild(root)
    try {
      dispatchLightbox(b)
      expect(events).toHaveLength(1)
      expect(events[0].images.map(i => i.src)).toEqual([
        'https://x.invalid/a.png',
        'https://x.invalid/b.png',
        'https://x.invalid/c.png',
      ])
      expect(events[0].index).toBe(1)
    } finally {
      document.body.removeChild(root)
      window.removeEventListener('lightbox', spy)
    }
  })

  it('dispatchLightbox falls back to a single-image payload when no scope ancestor is present', () => {
    const events: LightboxDetail[] = []
    const spy = (e: Event) => events.push((e as CustomEvent).detail)
    window.addEventListener('lightbox', spy)
    const orphan = document.createElement('img'); orphan.src = 'https://x.invalid/lone.png'; orphan.alt = 'lone'
    document.body.appendChild(orphan)
    try {
      dispatchLightbox(orphan)
      expect(events[0].images).toEqual([{ src: 'https://x.invalid/lone.png', alt: 'lone' }])
      expect(events[0].index).toBe(0)
    } finally {
      document.body.removeChild(orphan)
      window.removeEventListener('lightbox', spy)
    }
  })

  it('enlarges and shrinks the image via +/- keys, clamped, and resets on close', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const style = () => container.querySelector('img')!.getAttribute('style') || ''
    // Fit-to-screen baseline: scale(1), fit box.
    expect(style()).toContain('scale(1)')
    expect(style()).toContain('max-width: 90vw')
    // Zoom in one step rides the transform, not the fit box.
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    expect(style()).toContain('scale(1.5)')
    expect(style()).toContain('max-width: 90vw')
    // Zoom back out to the fit floor and clamp there.
    act(() => { fireEvent.keyDown(window, { key: '-' }) })
    expect(style()).toContain('scale(1)')
    act(() => { fireEvent.keyDown(window, { key: '-' }) })
    expect(style()).toContain('scale(1)')
    // Re-zoom, then '0' resets to fit.
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    act(() => { fireEvent.keyDown(window, { key: '0' }) })
    expect(style()).toContain('scale(1)')
  })

  it('ignores +/-/0 when chorded with a browser-zoom modifier', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const style = () => container.querySelector('img')!.getAttribute('style') || ''
    act(() => { fireEvent.keyDown(window, { key: '+', metaKey: true }) })
    expect(style()).toContain('scale(1)')
    act(() => { fireEvent.keyDown(window, { key: '+', ctrlKey: true }) })
    expect(style()).toContain('scale(1)')
  })

  it('clicking the image does not change zoom (zoom lives in the toolbar/keyboard)', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const imgEl = () => container.querySelector('img')!
    const style = () => imgEl().getAttribute('style') || ''
    expect(style()).toContain('scale(1)')
    // Clicks on the image are inert now — no zoom stepping.
    act(() => { fireEvent.click(imgEl()) })
    expect(style()).toContain('scale(1)')
    act(() => { fireEvent.click(imgEl()) })
    expect(style()).toContain('scale(1)')
    // Zoom still works via the keyboard.
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    expect(style()).toContain('scale(1.5)')
  })

  it('resets zoom when navigating to another image', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }, { src: 'b.png', alt: 'b' }], 0))
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    expect(container.querySelector('img')!.getAttribute('style')).toContain('scale(1.5)')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('b.png')
    expect(container.querySelector('img')!.getAttribute('style')).toContain('scale(1)')
  })

  it('drags an enlarged image to pan it, and a pan-drag does not step the zoom', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    const imgEl = () => container.querySelector('img')!
    // Give the image a layout box larger than the viewport so the clamp allows travel.
    Object.defineProperty(imgEl(), 'offsetWidth', { configurable: true, value: 3000 })
    Object.defineProperty(imgEl(), 'offsetHeight', { configurable: true, value: 3000 })
    // Zoom in first (fit can't pan) — via keyboard, since clicks are inert.
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    expect(imgEl().getAttribute('style')).toContain('scale(2)')
    const styleBefore = imgEl().getAttribute('style') || ''
    expect(styleBefore).toContain('translate(0px, 0px)')
    // Drag: pointer down, move well past the 4px threshold, up.
    act(() => { fireEvent.pointerDown(imgEl(), { clientX: 500, clientY: 500, pointerId: 1 }) })
    act(() => { fireEvent.pointerMove(imgEl(), { clientX: 380, clientY: 420, pointerId: 1 }) })
    act(() => { fireEvent.pointerUp(imgEl(), { clientX: 380, clientY: 420, pointerId: 1 }) })
    expect(imgEl().getAttribute('style')).toContain('translate(-120px, -80px)')
    expect(imgEl().className).toContain('cursor-grab')
    // A click after the drag must not change zoom (stays 2x) or close anything.
    act(() => { fireEvent.click(imgEl()) })
    expect(imgEl().getAttribute('style')).toContain('scale(2)')
  })
})

describe('MarkdownRenderer mcwidget strip is inline-code-aware', () => {
  it('preserves prose when an unclosed widget tag appears inside an inline-code span', () => {
    // Pre-fix bug: <mcwidget[\s\S]*$ alternative ate from the literal opening
    // tag (inside backticks) to end-of-block, dropping the rest of the prose.
    const content = 'In a chat: ask the agent to emit any `<mcwidget>` (e.g. "render a CR queue widget"), then click Bookmark.'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('emit any')
    expect(text).toContain('render a CR queue widget')
    expect(text).toContain('click Bookmark')
  })

  it('preserves a balanced inline-code mention of a widget tag pair', () => {
    const content = 'Use `<mcwidget>hello</mcwidget>` to embed HTML.'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('to embed HTML')
  })

  it('preserves real prose AFTER a backtick-wrapped tag mention earlier in the block', () => {
    // The smoking-gun scenario from the bug screenshot.
    const content = [
      '- Sidebar shows Artifacts',
      '- In a chat: ask the agent to emit any `<mcwidget>` (e.g. "render a CR queue widget")',
      '- Navigate to /artifacts',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('Sidebar shows Artifacts')
    expect(text).toContain('render a CR queue widget')
    expect(text).toContain('Navigate to /artifacts')
  })
})

describe('MarkdownRenderer strips leaked <tool_use> protocol markup', () => {
  it('strips a complete <tool_use>...</tool_use> block and renders surrounding markdown', () => {
    // Repro from chat where the agent leaked the full Anthropic tool_use
    // wrapper as text. Pre-fix: the unknown <tool_use> element trapped the
    // JSON body (including escaped \n literals) into a single paragraph,
    // dropping all the headers and rating callouts. Post-fix: the wrapper
    // and its body are gone, the surrounding prose renders normally.
    const content = [
      "I'll generate the review.",
      '',
      '<tool_use> {"tool_calls": [{"tool_name": "write_file", "parameters": {"file_path": "/tmp/x.md", "content": "### Heading\\n\\n**Rating:** Mixed"}}]} </tool_use>',
      '',
      'Review saved.',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain("I'll generate the review.")
    expect(text).toContain('Review saved.')
    // Tag itself and its JSON body must NOT leak through
    expect(text).not.toContain('tool_calls')
    expect(text).not.toContain('write_file')
    expect(text).not.toContain('<tool_use>')
    // getElementsByTagName (not querySelector('tool_use')): happy-dom parses the
    // arg as a CSS selector and rejects the bare `tool_use` tag name as invalid,
    // whereas getElementsByTagName takes a literal tag name on every engine.
    expect(container.getElementsByTagName('tool_use')).toHaveLength(0)
  })

  it('strips an unclosed <tool_use> opener (mid-stream)', () => {
    // During streaming the closing tag may not have arrived yet. The strip
    // regex falls through to the `<tool_use[\s\S]*$` alternative and removes
    // everything from the opener to end of block.
    const content = 'Working on it…\n\n<tool_use> {"tool_calls": [{"tool_name": "write_'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('Working on it')
    expect(text).not.toContain('tool_calls')
    expect(text).not.toContain('write_')
  })

  it('strips multiple <tool_use> blocks in the same message', () => {
    const content = [
      'First action:',
      '<tool_use>{"a": 1}</tool_use>',
      'Second action:',
      '<tool_use>{"b": 2}</tool_use>',
      'Done.',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('First action:')
    expect(text).toContain('Second action:')
    expect(text).toContain('Done.')
    expect(text).not.toContain('"a"')
    expect(text).not.toContain('"b"')
  })

  it('preserves <tool_use> mentions inside inline-code spans', () => {
    // Author documenting the protocol in prose: e.g. `<tool_use>` should
    // remain visible. The strip pass uses the same maskInlineCode helper as
    // the widget strip, so backtick-wrapped tag mentions are not removed.
    const content = 'When the agent emits a literal `<tool_use>` tag, the dashboard now strips it.'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('<tool_use>')
    expect(text).toContain('the dashboard now strips it.')
  })

  it('does NOT strip <tool_use> mentions inside fenced code blocks', () => {
    // When the agent is documenting the protocol in a code block, the tags
    // are real content — the fence makes the markdown renderer treat them
    // as literal text and the strip pass operates on markdown blocks only,
    // not extracted code blocks. Regression guard for documentation messages.
    const content = '```\n<tool_use>{"x": 1}</tool_use>\n```'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('<tool_use>')
    expect(text).toContain('"x"')
  })
})

describe('MarkdownRenderer softBreaks', () => {
  it('converts a soft line break to <br> when softBreaks is set', () => {
    const { container } = render(<MarkdownRenderer content={'line one\nline two'} softBreaks />)
    expect(container.querySelectorAll('br').length).toBe(1)
    expect(container.textContent).toContain('line one')
    expect(container.textContent).toContain('line two')
  })

  it('collapses a soft line break by default (no softBreaks, no <br>)', () => {
    const { container } = render(<MarkdownRenderer content={'line one\nline two'} />)
    expect(container.querySelector('br')).toBeNull()
  })

  it('does not inject <br> between loose list items — block spacing stays normal', () => {
    // A blank line between items makes a "loose" list. The soft-break plugin
    // must only touch soft breaks inside text; block separators (parsed as
    // distinct blocks) stay untouched, so list items keep normal spacing and
    // no literal blank line is rendered between them.
    const { container } = render(<MarkdownRenderer content={'1. first\n\n2. second'} softBreaks />)
    expect(container.querySelectorAll('ol > li').length).toBe(2)
    expect(container.querySelector('br')).toBeNull()
  })

  it('preserves multiple soft breaks in a paragraph as multiple <br> when softBreaks is set', () => {
    const { container } = render(<MarkdownRenderer content={'a\nb\nc'} softBreaks />)
    expect(container.querySelectorAll('br').length).toBe(2)
  })
})

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'

/**
 * REGRESSION GUARD — a path chip's glyph must not change the paragraph's width.
 *
 * `InlineCode` gives a CONFIRMED path chip a leading 12px glyph with a 4px
 * margin. The confirmation is asynchronous (`usePathKind` probes the backend), so
 * without a reserve those 16px appear mid-paragraph after the text is already
 * laid out. The glyph is an inline atom, so the gain can push a line over and
 * change the row's height — measured in a real browser at 336–564px container
 * widths (phones sit inside that band), a hit costs 24px, one line, and it lands
 * under a reader scrolling history, because a path is probed the first time its
 * row mounts.
 *
 * The fix is the rule the image reserve already follows (`reservedImageStyle`):
 * hold the box before the async answer arrives, so the answer restyles rather
 * than reflows. jsdom computes no layout, so these pin the mechanism instead of
 * the pixels — the reserve's PRESENCE in every unconfirmed state, its absence on
 * text that is not path-shaped, and the single source of the geometry that makes
 * the two states equal.
 */

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

const realFetch = globalThis.fetch

/** No probe ever answers: the span stays in the "confirmation pending" state. */
function stubNeverResolves() {
  globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch
}

/** The probe answers "this is not a path", the state a reserve must also hold. */
function stubMissing() {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: false, status: 404, headers: new Headers() } as Response),
  ) as unknown as typeof fetch
}

function stubKind(kind: 'file' | 'dir') {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, headers: new Headers({ 'X-Path-Kind': kind }) } as Response),
  ) as unknown as typeof fetch
}

/** The leading icon inside the code span, reserve or real glyph alike. */
function glyphOf(container: HTMLElement): SVGElement | null {
  return container.querySelector('code svg')
}

beforeEach(() => {
  vi.clearAllMocks()
  __resetPathKindCache()
})

afterEach(() => {
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

describe('path chip glyph reserve', () => {
  it('holds the glyph box while the probe is still in flight', () => {
    stubNeverResolves()
    const { container } = render(<MarkdownRenderer content={'See `src/hooks/useVirtualChat.ts` here.'} />)
    const g = glyphOf(container)
    expect(g).not.toBeNull()
    // Invisible, not merely dim: the visible glyph is what tells a reader which
    // paths the backend confirmed, so the placeholder must not read as one.
    expect(g!.getAttribute('class')).toContain('opacity-0')
  })

  it('KEEPS the box after the probe answers "not a path"', async () => {
    stubMissing()
    const { container } = render(<MarkdownRenderer content={'See `src/nope/gone.ts` here.'} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    // A reserve that only covered the pending window would move the re-wrap to
    // the moment it went away, which is the same defect one tick later.
    await waitFor(() => {
      const g = glyphOf(container)
      expect(g).not.toBeNull()
      expect(g!.getAttribute('class')).toContain('opacity-0')
    })
  })

  it('is in place BEFORE probing is enabled, so the end of streaming does not shift', () => {
    stubNeverResolves()
    // `streaming` switches PathProbeCtx on, so anything keyed to the probe being
    // enabled would appear exactly when the text becomes final.
    const { container } = render(
      <MarkdownRenderer content={'See `src/hooks/useVirtualChat.ts` here.'} streaming={true} />,
    )
    expect(globalThis.fetch).not.toHaveBeenCalled()
    const g = glyphOf(container)
    expect(g).not.toBeNull()
    expect(g!.getAttribute('class')).toContain('opacity-0')
  })

  it('reserves nothing for inline code that is not path-shaped', () => {
    stubNeverResolves()
    const { container } = render(<MarkdownRenderer content={'Run `npm test` first.'} />)
    // Ordinary inline code far outnumbers path chips; a reserve here would put a
    // 16px hole in front of every one of them.
    expect(glyphOf(container)).toBeNull()
  })

  it('swaps the reserve for a visible glyph once the path is confirmed', async () => {
    stubKind('file')
    const { container } = render(<MarkdownRenderer content={'See `src/hooks/useVirtualChat.ts` here.'} />)
    await waitFor(() => {
      const g = glyphOf(container)
      expect(g).not.toBeNull()
      expect(g!.getAttribute('class')).toContain('opacity-70')
    })
    expect(container.querySelector('code[data-path]')).not.toBeNull()
  })

  it('draws both states from ONE size and ONE margin, so they cannot drift apart', () => {
    // The equality is the mechanism, and jsdom cannot measure it. What it can
    // check is that neither site carries its own copy of the numbers: a second
    // literal is how the reserve silently stops matching the glyph it stands in
    // for. Both must render `CHIP_GLYPH_SIZE` / `CHIP_GLYPH_GEOMETRY`.
    const src = readFileSync(join(__dirname, '..', 'components', 'MarkdownRenderer.tsx'), 'utf8')
    expect(src).toMatch(/const CHIP_GLYPH_SIZE = 12/)
    expect(src).toMatch(/const CHIP_GLYPH_GEOMETRY = 'inline align-middle mr-1'/)
    const glyphTags = src.match(/<Glyph[^>]*\/>/g) ?? []
    expect(glyphTags.length).toBeGreaterThan(0)
    for (const tag of glyphTags) {
      expect(tag).toContain('size={CHIP_GLYPH_SIZE}')
      expect(tag).toContain('${CHIP_GLYPH_GEOMETRY}')
    }
    // The reserve is keyed to path SHAPE, never to the probe being enabled or in
    // flight — that is what makes it already present when streaming ends.
    expect(src).toMatch(/pathResolution\.shaped \? <ChipGlyphReserve/)
  })
})

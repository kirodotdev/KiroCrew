import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * REGRESSION GUARD — an edited local image must not render its previous bytes.
 *
 * `/api/file-raw?path=…` addresses a file by PATH. The agent-authoring flow
 * rewrites ONE file across turns (the image-authoring skill mandates keeping a
 * single editable source and showing it as `![alt](/abs/path.svg)`), so every
 * impression used to resolve to the same URL — and a browser treats one URL in
 * one document as one resource.
 *
 * Measured in real Chrome, not assumed: with an identical URL the second <img>
 * is served from the in-document cache with NO network request, so a new message
 * paints the old bytes. No response header changes that, because the request is
 * never issued — `ETag`, `Cache-Control: no-cache` and `no-store` were each
 * tested and each left the render stale. Only a distinct URL re-fetches, and it
 * also lets an earlier impression keep the bytes it originally loaded.
 *
 * These tests assert the URL is scoped per message and stable within one, which
 * is the mechanism. They cannot observe the browser cache itself.
 */

function srcOf(container: HTMLElement): string {
  const img = container.querySelector('img')
  if (!img) throw new Error('no <img> rendered')
  return img.getAttribute('src') || ''
}

const SVG = '![diagram](/Users/me/work/logo.svg)'

describe('local image URLs are scoped to the message that renders them', () => {
  it('PREMISE: a local image path renders through the file-raw endpoint', () => {
    const { container } = render(<MarkdownRenderer content={SVG} messageTs="2026-08-08T00:00:00Z" />)
    expect(srcOf(container)).toContain('/api/file-raw?path=')
    expect(srcOf(container)).toContain(encodeURIComponent('/Users/me/work/logo.svg'))
  })

  it('gives two messages distinct URLs for the SAME path', () => {
    // The fix. Same file, two turns: the later impression must be able to reach
    // the current bytes rather than inheriting the earlier resource.
    const a = render(<MarkdownRenderer content={SVG} messageTs="2026-08-08T00:00:01Z" />)
    const b = render(<MarkdownRenderer content={SVG} messageTs="2026-08-08T00:00:02Z" />)
    expect(srcOf(a.container)).not.toBe(srcOf(b.container))
  })

  it('keeps the URL stable within one message', () => {
    // Re-render and streaming must NOT change the URL, or every keystroke of a
    // streaming message would re-request the image.
    const { container, rerender } = render(
      <MarkdownRenderer content={SVG} messageTs="2026-08-08T00:00:03Z" />,
    )
    const before = srcOf(container)
    rerender(<MarkdownRenderer content={SVG} messageTs="2026-08-08T00:00:03Z" streaming />)
    expect(srcOf(container)).toBe(before)
  })

  it('leaves remote images untouched', () => {
    const { container } = render(
      <MarkdownRenderer content="![x](https://example.com/a.png)" messageTs="2026-08-08T00:00:04Z" />,
    )
    expect(srcOf(container)).toBe('https://example.com/a.png')
  })

  it('adds nothing when the caller has no message context', () => {
    // FileViewer and ContentRenderer render markdown outside a transcript. They
    // pass no messageTs, and their URLs must not change.
    const { container } = render(<MarkdownRenderer content={SVG} />)
    expect(srcOf(container)).toBe(`/api/file-raw?path=${encodeURIComponent('/Users/me/work/logo.svg')}`)
  })
})

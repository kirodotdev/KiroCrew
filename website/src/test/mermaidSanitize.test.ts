// @vitest-environment jsdom
import { describe, it, expect } from 'vitest'
import DOMPurify from 'dompurify'
import { MERMAID_DOMPURIFY_CONFIG } from '../components/markdown/mermaidSanitize'

/**
 * Mermaid hands each HTML label to DOMPurify with this config. A label must not
 * keep anything the browser fetches on its own the moment the diagram renders:
 * remote images in chat stay click-to-load (remoteMedia.tsx).
 */
const clean = (html: string) => DOMPurify.sanitize(html, MERMAID_DOMPURIFY_CONFIG).toString()

describe('mermaid label sanitizer', () => {
  it('drops a remote image and keeps the label text', () => {
    const out = clean("Logo <img src='https://example.invalid/mermaid.png'> here")
    expect(out).not.toMatch(/<img|example\.invalid/)
    expect(out).toContain('Logo')
    expect(out).toContain('here')
  })

  it.each([
    "<svg><image href='https://example.invalid/a.png'></image></svg>",
    "<picture><source srcset='https://example.invalid/a.png'></picture>",
    "<video poster='https://example.invalid/a.png'></video>",
    "<span style=\"background:url('https://example.invalid/a.png')\">x</span>",
    "<iframe src='https://example.invalid/'></iframe>",
    "x<svg><filter id='f'><feImage href='https://example.invalid/a.png'></feImage></filter><rect width='1' height='1' filter='url(#f)'></rect></svg>",
    "x<svg><filter id='f'><feImage xlink:href='https://example.invalid/a.png'></feImage></filter></svg>",
    "<svg><use href='https://example.invalid/s.svg#a'></use></svg>",
  ])('drops a self-loading URL in %s', html => {
    expect(clean(html)).not.toContain('example.invalid')
  })

  it('drops a remote image twice-sanitized, as mermaid sanitizes a label and then the whole SVG', () => {
    const html = "x<svg><filter id='f'><feImage href='https://example.invalid/a.png'></feImage></filter></svg>"
    expect(clean(clean(html))).not.toContain('example.invalid')
  })

  it('keeps ordinary label markup', () => {
    expect(clean('<b>bold</b><br/><i>it</i>')).toBe('<b>bold</b><br><i>it</i>')
  })

  it("keeps the inline colours of mermaid's own icon SVG", () => {
    // The architecture-beta database icon's shape: colour lives only in `style`.
    const out = clean('<svg><g><rect width="80" height="80" style="fill: #087ebf; stroke-width: 0px;"></rect><path d="m20,20h40" style="fill: none; stroke: #fff; stroke-width: 2px;"></path></g></svg>')
    expect(out).toContain('fill: #087ebf')
    expect(out).toContain('stroke: #fff')
  })

  it("keeps the centring of mermaid's KaTeX label wrapper", () => {
    const out = clean('<div style="display: flex; align-items: center; justify-content: center;"><span class="katex">x</span></div>')
    expect(out).toContain('justify-content: center')
  })

  it.each([
    "background:url('https://example.invalid/a.png')",
    'background-image: URL(https://example.invalid/a.png)',
    'background-image: image-set("https://example.invalid/a.png" 1x)',
    'background-image: -webkit-image-set("https://example.invalid/a.png" 1x)',
    'background-image: \\75 rl(https://example.invalid/a.png)',
    'fill: #087ebf; cursor: url(https://example.invalid/c.cur), auto',
  ])('drops a style that can fetch: %s', style => {
    const out = clean(`<span style='${style}'>x</span>`)
    expect(out).not.toContain('example.invalid')
    expect(out).not.toContain('style=')
    expect(out).toContain('x')
  })

  it.each(['mask', 'fill', 'clip-path', 'marker-start', 'filter'])(
    'drops an external url() in the SVG %s attribute',
    attr => {
      const out = clean(`y <svg><rect width='20' height='20' ${attr}='url(https://example.invalid/x.png?d=1)'></rect></svg>`)
      expect(out).not.toContain('example.invalid')
      expect(out).toContain('<rect')
    },
  )

  it("keeps a same-document url(#id), as mermaid's markers and filters use", () => {
    const out = clean("<svg><rect width='1' height='1' filter='url(#f)' marker-end=\"url( '#arrow' )\"></rect></svg>")
    expect(out).toContain('filter="url(#f)"')
    expect(out).toContain('marker-end=')
  })

  it('drops a table background, which loads by itself', () => {
    expect(clean("<table background='https://example.invalid/a.png'><tr><td>x</td></tr></table>")).not.toContain('example.invalid')
  })

  it('leaves a sanitize call without the mermaid config alone', () => {
    const html = "<span style=\"background:url('https://example.invalid/a.png')\">x</span>"
    expect(DOMPurify.sanitize(html).toString()).toContain('example.invalid')
  })
})

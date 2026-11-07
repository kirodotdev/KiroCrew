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
  ])('drops a self-loading URL in %s', html => {
    expect(clean(html)).not.toContain('example.invalid')
  })

  it('keeps ordinary label markup', () => {
    expect(clean('<b>bold</b><br/><i>it</i>')).toBe('<b>bold</b><br><i>it</i>')
  })
})

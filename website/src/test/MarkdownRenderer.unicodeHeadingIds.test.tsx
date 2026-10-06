// @vitest-environment happy-dom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'

// A heading's id is the target a `[text](#heading)` link lands on. The slug
// rule keeps letters, numbers and combining marks of every script, so a heading
// written in Japanese (or any non-Latin script) gets an id, and a mixed heading
// keeps its non-Latin words instead of collapsing onto the few ASCII characters
// it happens to contain. An ASCII heading produces exactly the id it always
// has, so existing `#heading` links keep landing.

const heading = (container: HTMLElement, tag: string, text: string) =>
  Array.from(container.querySelectorAll(tag)).find(h => h.textContent === text)

describe('MarkdownRenderer heading ids keep non-Latin scripts', () => {
  const realScroll = Element.prototype.scrollIntoView
  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { Element.prototype.scrollIntoView = realScroll; vi.restoreAllMocks() })

  it('gives a Japanese-only heading its text as the id', () => {
    const { container } = render(<MarkdownRenderer content={'## 示唆\n\ntext\n'} />)
    expect(container.querySelector('h2')!.id).toBe('示唆')
  })

  it('keeps the non-Latin words of a mixed heading', () => {
    const { container } = render(<MarkdownRenderer content={'## Layers (第1階層 8フォルダの差分)\n\ntext\n'} />)
    expect(container.querySelector('h2')!.id).toBe('layers-第1階層-8フォルダの差分')
  })

  it('keeps accented Latin letters and combining marks', () => {
    // `é` precomposed in the first heading, `e` + U+0301 (a combining mark) in the second.
    const { container } = render(<MarkdownRenderer content={'## Résumé\n\na\n\n## Cafe\u0301 notes\n\nb\n'} />)
    expect(heading(container, 'h2', 'Résumé')!.id).toBe('résumé')
    expect(heading(container, 'h2', 'Cafe\u0301 notes')!.id).toBe('cafe\u0301-notes')
  })

  it.each([
    ['## Getting Started!', 'getting-started'],
    ['# Setup', 'setup'],
    ['### C++ & Rust: notes', 'c-rust-notes'],
    ['## snake_case stays   wide-open', 'snake_case-stays-wide-open'],
    ['## -- dashes around --', 'dashes-around'],
    ['## `code` and *emphasis*', 'code-and-emphasis'],
  ])('ASCII heading %j keeps its id %j', (md, id) => {
    const { container } = render(<MarkdownRenderer content={`${md}\n\ntext\n`} />)
    expect(container.querySelector('h1,h2,h3')!.id).toBe(id)
  })

  it('gives two distinct Japanese headings distinct ids instead of colliding on a digit', () => {
    const { container } = render(<MarkdownRenderer content={'## 第1章 概要\n\na\n\n## 第1章 詳細\n\nb\n'} />)
    expect(heading(container, 'h2', '第1章 概要')!.id).toBe('第1章-概要')
    expect(heading(container, 'h2', '第1章 詳細')!.id).toBe('第1章-詳細')
  })

  it.each([
    // U+26A0 U+FE0F, U+1F6E0 U+FE0F, U+0031 U+FE0F U+20E3: the emoji form is
    // spelled with combining marks that carry no text.
    ['## \u26A0\uFE0F Risks', 'risks'],
    ['## \u{1F6E0}\uFE0F Setup', 'setup'],
    ['## 1\uFE0F\u20E3 First step', '1-first-step'],
    ['## \u2705 Done', 'done'],
  ])('emoji-prefixed heading %j keeps the id %j it has without the emoji', (md, id) => {
    const { container } = render(<MarkdownRenderer content={`${md}\n\ntext\n`} />)
    expect(container.querySelector('h2')!.id).toBe(id)
  })

  it('lands a #link written the GitHub way on the Japanese heading', () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const { container } = render(<MarkdownRenderer content={'[Go to 示唆](#示唆)\n\nintro\n\n## 示唆\n\ntext\n'} />)
    const a = container.querySelector('a[href^="#"]')!
    // The markdown parser percent-encodes the destination; the click handler
    // decodes it before matching, so the link lands on the raw-text id.
    expect(decodeURIComponent(a.getAttribute('href')!)).toBe('#示唆')
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true })
    a.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(scroll).toHaveBeenCalledTimes(1)
    expect(scroll.mock.contexts[0]).toBe(container.querySelector('h2'))
  })
})

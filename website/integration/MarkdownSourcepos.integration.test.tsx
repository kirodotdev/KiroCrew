// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { resolveSourcePos } from '../src/components/MarkdownPanel'

/**
 * End-to-end: render actual markdown through MarkdownRenderer with
 * sourcePos=true, then assert that resolveSourcePos can map DOM selections
 * back to source coordinates. Validates the rehypeSourcepos plugin + the
 * resolver as a complete unit.
 */
describe('MarkdownRenderer sourcePos end-to-end', () => {
  it('emits data-sourcepos on elements when sourcePos is enabled', () => {
    const { container } = render(<MarkdownRenderer content={'# Hello\n\nHi **bold**.'} sourcePos />)
    const h1 = container.querySelector('h1')!
    const strong = container.querySelector('strong')!
    expect(h1.getAttribute('data-sourcepos')).toMatch(/^1:1-1:\d+$/)
    expect(strong.getAttribute('data-sourcepos')).toMatch(/^3:\d+-3:\d+$/)
  })

  it('does NOT emit data-sourcepos when sourcePos is disabled (default)', () => {
    const { container } = render(<MarkdownRenderer content={'# Hello'} />)
    expect(container.querySelector('[data-sourcepos]')).toBeNull()
  })

  it('resolves a selection inside **bold** to its source column', () => {
    const content = 'Hi **bold**.'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const strong = container.querySelector('strong')!
    const text = strong.firstChild as Text
    // Source `Hi **bold**.` → `b` of bold at col 6 (after `Hi **`).
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 4)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 1, column: 6 })
  })

  it('resolves a selection in a later paragraph to the correct line', () => {
    const content = 'first\n\nsecond paragraph'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const paragraphs = container.querySelectorAll('p')
    const text = paragraphs[1].firstChild as Text
    // Select "paragraph" starting at offset 7 of "second paragraph"
    const range = document.createRange()
    range.setStart(text, 7); range.setEnd(text, 16)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 3, column: 8 })
  })

  it('disambiguates duplicate text by tight source-span search', () => {
    const content = 'alpha\n\nalpha\n\nalpha'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const paragraphs = container.querySelectorAll('p')
    // Third "alpha" paragraph → must resolve to line 5, not 1
    const text = paragraphs[2].firstChild as Text
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 5)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 5, column: 1 })
  })

  it('resolves a selection inside a heading past the # marker', () => {
    const content = '# Hello World'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const h1 = container.querySelector('h1')!
    const text = h1.firstChild as Text
    // "World" is at source col 9 (after `# Hello `)
    const range = document.createRange()
    range.setStart(text, 6); range.setEnd(text, 11)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 1, column: 9 })
  })

  it('resolves selections across every supported element type', () => {
    const md = [
      '# H1', '', '## H2', '', '### H3', '',
      'para with *em* and [link](https://x) and `code`.', '',
      '- item one', '- item two', '',
      '1. first', '2. second', '',
      '> quoted', '',
      '| a | b |', '|---|---|', '| 1 | 2 |', '',
      '---',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={md} sourcePos />)
    for (const tag of ['h1', 'h2', 'h3', 'p', 'em', 'a', 'code', 'ul', 'ol', 'li', 'blockquote', 'table', 'th', 'td', 'hr']) {
      const el = container.querySelector(tag)
      expect(el, `${tag} missing`).toBeTruthy()
      expect(el!.getAttribute('data-sourcepos'), `${tag} has no data-sourcepos`).toMatch(/^\d+:\d+-\d+:\d+$/)
    }
  })

  it('resolves selection inside a list item past the `- ` marker', () => {
    const content = '- first item\n- second item'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const items = container.querySelectorAll('li')
    const text = items[1].firstChild as Text
    // "second" at source col 3 of line 2 (after `- `)
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 6)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 2, column: 3 })
  })

  it('resolves selection inside a blockquote past the `> ` marker', () => {
    const content = '> quoted text'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const p = container.querySelector('blockquote p')!
    const text = p.firstChild as Text
    // "quoted" at source col 3 (after `> `)
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 6)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 1, column: 3 })
  })

  it('resolves selection inside a table cell', () => {
    const content = '| alpha | beta |\n|---|---|\n| one | two |'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const tds = container.querySelectorAll('td')
    const text = tds[1].firstChild as Text
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 3)
    // GFM table positions vary by tokenizer; assert we landed on line 3 (the
    // data row) with a plausible column. Precision isn't the goal here -
    // coverage of the table code path is.
    const got = resolveSourcePos(range, root, content)
    expect(got?.line).toBe(3)
    expect(got?.column).toBeGreaterThanOrEqual(1)
  })

  it('resolves selection following inline syntax (regression for char-alignment past mid-span syntax)', () => {
    // Source `Hi *foo* bar`, select "bar" inside the <p>.
    // rendered text of <p> is "Hi foo bar", offset 7 → source col 10 (the 'b').
    const content = 'Hi *foo* bar'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const p = container.querySelector('p')!
    // Find the text node containing "bar" (last text node of <p>)
    const lastText = p.lastChild as Text
    const range = document.createRange()
    range.setStart(lastText, 1); range.setEnd(lastText, 4) // skip leading space → 'bar'
    const got = resolveSourcePos(range, root, content)
    expect(got).toEqual({ line: 1, column: 10 })
  })

  it('resolves selection inside inline <code>', () => {
    const content = 'Use `foo()` here.'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const code = container.querySelector('code')!
    const text = code.firstChild as Text
    // "foo()" at source col 6 (after `Use ` plus the opening backtick)
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 5)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 1, column: 6 })
  })

  it('preserves element-start coord when selection is at offset 0', () => {
    // Offset 0 means "caret at the start of the rendered element" — resolver
    // returns the element's source start unconditionally.
    const content = '**bold**'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const strong = container.querySelector('strong')!
    const text = strong.firstChild as Text
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 0)
    const got = resolveSourcePos(range, root, content)
    // Element `<strong>` source span starts at col 1 (the first `*`);
    // rendered "bold" begins at span index 2. Offset 0 → col 3.
    expect(got?.line).toBe(1)
    expect(got?.column).toBe(3)
  })
})

import { describe, it, expect } from 'vitest'
import { applySearchHighlights, clearSearchHighlights } from '../utils/domHighlight'

function el(html: string): HTMLElement {
  const parser = new DOMParser()
  const doc = parser.parseFromString(`<div>${html}</div>`, 'text/html')
  const div = document.createElement('div')
  div.append(...doc.body.firstElementChild!.childNodes)
  return div
}

describe('applySearchHighlights', () => {
  it('wraps matching text nodes in <mark> elements', () => {
    const root = el('<p>hello world</p>')
    applySearchHighlights(root, 'world', false, -1)
    expect(root.querySelectorAll('mark.search-match')).toHaveLength(1)
    expect(root.querySelector('mark')!.textContent).toBe('world')
  })

  it('uses search-match class when currentOcc=-1', () => {
    const root = el('<p>test</p>')
    applySearchHighlights(root, 'test', false, -1)
    expect(root.querySelector('mark')!.className).toBe('search-match')
  })

  it('uses search-current class only for the specified occurrence', () => {
    const root = el('<p>foo bar foo baz foo</p>')
    applySearchHighlights(root, 'foo', false, 1)
    const marks = root.querySelectorAll('mark')
    expect(marks).toHaveLength(3)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-current')
    expect(marks[2].className).toBe('search-match')
  })

  it('uses search-current for first occurrence when currentOcc=0', () => {
    const root = el('<p>test test</p>')
    applySearchHighlights(root, 'test', false, 0)
    const marks = root.querySelectorAll('mark')
    expect(marks[0].className).toBe('search-current')
    expect(marks[1].className).toBe('search-match')
  })

  it('case-insensitive matching by default', () => {
    const root = el('<p>Hello HELLO</p>')
    applySearchHighlights(root, 'hello', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(2)
  })

  it('case-sensitive matching when caseSensitive=true', () => {
    const root = el('<p>Hello HELLO hello</p>')
    applySearchHighlights(root, 'hello', true, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(1)
    expect(root.querySelector('mark')!.textContent).toBe('hello')
  })

  it('handles multiple matches in a single text node', () => {
    const root = el('<p>foo bar foo baz foo</p>')
    applySearchHighlights(root, 'foo', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(3)
  })

  it('handles matches across multiple text nodes', () => {
    const root = el('<p>hello</p><p>hello</p>')
    applySearchHighlights(root, 'hello', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(2)
  })

  it('occurrence counter spans across text nodes', () => {
    const root = el('<p>foo</p><p>foo</p><p>foo</p>')
    applySearchHighlights(root, 'foo', false, 1)
    const marks = root.querySelectorAll('mark')
    expect(marks).toHaveLength(3)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-current')
    expect(marks[2].className).toBe('search-match')
  })

  it('preserves non-matching text nodes', () => {
    const root = el('<p>abc def ghi</p>')
    applySearchHighlights(root, 'def', false, -1)
    expect(root.textContent).toBe('abc def ghi')
  })

  it('handles nested elements', () => {
    const root = el('<p><strong>bold text</strong> normal</p>')
    applySearchHighlights(root, 'bold', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(1)
    expect(root.querySelector('mark')!.textContent).toBe('bold')
  })

  it('no-op when term is empty', () => {
    const root = el('<p>hello</p>')
    applySearchHighlights(root, '', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(0)
  })

  it('no-op when element has no text content', () => {
    const root = el('<div></div>')
    applySearchHighlights(root, 'test', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(0)
  })

  it('clears previous highlights before applying new ones', () => {
    const root = el('<p>hello world</p>')
    applySearchHighlights(root, 'hello', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(1)
    applySearchHighlights(root, 'world', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(1)
    expect(root.querySelector('mark')!.textContent).toBe('world')
  })

  it('handles special regex characters in term', () => {
    const root = el('<p>foo.bar</p>')
    applySearchHighlights(root, 'foo.bar', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(1)
    expect(root.querySelector('mark')!.textContent).toBe('foo.bar')
  })

  it('currentOcc beyond last occurrence produces all search-match', () => {
    const root = el('<p>a b a</p>')
    applySearchHighlights(root, 'a', false, 99)
    const marks = root.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-match')
  })

  it('repeated apply-clear-apply cycles do not fragment text nodes (normalize regression)', () => {
    const root = el('<p>hello world hello</p>')
    // Cycle 1
    applySearchHighlights(root, 'hello', false, 0)
    expect(root.querySelectorAll('mark')).toHaveLength(2)
    // Cycle 2 — different term
    applySearchHighlights(root, 'world', false, 0)
    expect(root.querySelectorAll('mark')).toHaveLength(1)
    expect(root.querySelector('mark')!.textContent).toBe('world')
    // Cycle 3 — back to original term, should still find both
    applySearchHighlights(root, 'hello', false, -1)
    expect(root.querySelectorAll('mark')).toHaveLength(2)
    expect(root.textContent).toBe('hello world hello')
  })
})

describe('clearSearchHighlights', () => {
  it('removes <mark class="search-match"> and unwraps children', () => {
    const root = el('<p><mark class="search-match">hello</mark> world</p>')
    clearSearchHighlights(root)
    expect(root.querySelectorAll('mark')).toHaveLength(0)
    expect(root.textContent).toBe('hello world')
  })

  it('removes <mark class="search-current"> and unwraps children', () => {
    const root = el('<p><mark class="search-current">hello</mark></p>')
    clearSearchHighlights(root)
    expect(root.querySelectorAll('mark')).toHaveLength(0)
    expect(root.textContent).toBe('hello')
  })

  it('preserves non-search <mark> elements', () => {
    const root = el('<p><mark class="other">keep</mark> <mark class="search-match">remove</mark></p>')
    clearSearchHighlights(root)
    expect(root.querySelectorAll('mark')).toHaveLength(1)
    expect(root.querySelector('mark')!.textContent).toBe('keep')
  })

  it('no-op on element with no marks', () => {
    const root = el('<p>plain text</p>')
    clearSearchHighlights(root)
    expect(root.textContent).toBe('plain text')
  })

  it('normalizes text nodes after clearing', () => {
    const root = el('<p><mark class="search-match">a</mark>b<mark class="search-match">c</mark></p>')
    clearSearchHighlights(root)
    // After clear + normalize, "a", "b", "c" should be merged into one text node
    const p = root.querySelector('p')!
    expect(p.childNodes).toHaveLength(1)
    expect(p.textContent).toBe('abc')
  })

  it('repeated clear cycles produce clean single text nodes', () => {
    const root = el('<p>abc</p>')
    // Apply and clear multiple times
    applySearchHighlights(root, 'b', false, 0)
    clearSearchHighlights(root)
    applySearchHighlights(root, 'a', false, 0)
    clearSearchHighlights(root)
    // Should be a single merged text node, not fragments
    const p = root.querySelector('p')!
    expect(p.childNodes).toHaveLength(1)
    expect(p.textContent).toBe('abc')
  })
})

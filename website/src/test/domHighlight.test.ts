import { describe, it, expect, beforeAll, beforeEach, afterEach } from 'vitest'
import {
  applySearchHighlights,
  clearSearchHighlights,
  getCurrentSearchRange,
  SEARCH_HL_MATCH,
  SEARCH_HL_CURRENT,
} from '../utils/domHighlight'
import { installHighlightApiStub, paintedText, registeredRanges } from './highlightApiStub'

function el(html: string): HTMLElement {
  const parser = new DOMParser()
  const doc = parser.parseFromString(`<div>${html}</div>`, 'text/html')
  const div = document.createElement('div')
  div.append(...doc.body.firstElementChild!.childNodes)
  return div
}

/** Serialize the fixture's node structure, to assert a walk left it untouched. */
function shape(root: Node): string {
  const parts: string[] = []
  const walk = (n: Node) => {
    parts.push(n.nodeType === Node.TEXT_NODE ? `#${(n as Text).data}` : (n as Element).tagName.toLowerCase())
    n.childNodes.forEach(walk)
  }
  walk(root)
  return parts.join(',')
}

beforeAll(() => { installHighlightApiStub() })

const mounted: HTMLElement[] = []
function mount(root: HTMLElement): HTMLElement {
  document.body.appendChild(root)
  mounted.push(root)
  return root
}
afterEach(() => {
  for (const r of mounted.splice(0)) { clearSearchHighlights(r); r.remove() }
})

describe('applySearchHighlights', () => {
  let root: HTMLElement
  beforeEach(() => { root = el('') })
  afterEach(() => clearSearchHighlights(root))

  it('paints matching text as ranges', () => {
    root = el('<p>hello world</p>')
    applySearchHighlights(root, 'world', false, -1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['world'])
    expect(paintedText(SEARCH_HL_CURRENT)).toEqual([])
  })

  it('does not mutate the DOM it paints', () => {
    root = el('<p>hello <strong>world</strong> hello</p>')
    const before = shape(root)
    applySearchHighlights(root, 'hello', false, 0)
    expect(shape(root)).toBe(before)
    expect(root.querySelectorAll('mark')).toHaveLength(0)
    expect(root.querySelector('p')!.childNodes).toHaveLength(3)
  })

  it('paints every occurrence as a plain match when currentOcc=-1', () => {
    root = el('<p>test test</p>')
    applySearchHighlights(root, 'test', false, -1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['test', 'test'])
    expect(paintedText(SEARCH_HL_CURRENT)).toEqual([])
  })

  it('paints only the specified occurrence as current', () => {
    root = el('<p>foo bar foo baz foo</p>')
    applySearchHighlights(root, 'foo', false, 1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['foo', 'foo'])
    const current = registeredRanges(SEARCH_HL_CURRENT)
    expect(current).toHaveLength(1)
    expect(current[0].startOffset).toBe(8)
    expect(current[0].toString()).toBe('foo')
  })

  it('paints the first occurrence as current when currentOcc=0', () => {
    root = el('<p>test test</p>')
    applySearchHighlights(root, 'test', false, 0)
    expect(registeredRanges(SEARCH_HL_CURRENT)[0].startOffset).toBe(0)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['test'])
  })

  it('case-insensitive matching by default', () => {
    root = el('<p>Hello HELLO</p>')
    applySearchHighlights(root, 'hello', false, -1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['Hello', 'HELLO'])
  })

  it('case-sensitive matching when caseSensitive=true', () => {
    root = el('<p>Hello HELLO hello</p>')
    applySearchHighlights(root, 'hello', true, -1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['hello'])
  })

  it('handles matches across multiple text nodes', () => {
    root = el('<p>hello</p><p>hello</p>')
    applySearchHighlights(root, 'hello', false, -1)
    expect(registeredRanges(SEARCH_HL_MATCH)).toHaveLength(2)
  })

  it('occurrence counter spans across text nodes', () => {
    root = el('<p>foo</p><p>foo</p><p>foo</p>')
    applySearchHighlights(root, 'foo', false, 1)
    expect(registeredRanges(SEARCH_HL_MATCH)).toHaveLength(2)
    const current = registeredRanges(SEARCH_HL_CURRENT)
    expect(current).toHaveLength(1)
    expect(current[0].startContainer).toBe(root.querySelectorAll('p')[1].firstChild)
  })

  it('reaches text inside nested elements', () => {
    root = el('<p><strong>bold text</strong> normal</p>')
    applySearchHighlights(root, 'bold', false, -1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['bold'])
  })

  it('no-op when term is empty', () => {
    root = el('<p>hello</p>')
    applySearchHighlights(root, '', false, -1)
    expect(registeredRanges(SEARCH_HL_MATCH)).toHaveLength(0)
  })

  it('no-op when element has no text content', () => {
    root = el('<div></div>')
    applySearchHighlights(root, 'test', false, -1)
    expect(registeredRanges(SEARCH_HL_MATCH)).toHaveLength(0)
  })

  it('withdraws the previous term before painting the next', () => {
    root = el('<p>hello world</p>')
    applySearchHighlights(root, 'hello', false, -1)
    applySearchHighlights(root, 'world', false, -1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['world'])
  })

  it('treats regex metacharacters in the term literally', () => {
    root = el('<p>foo.bar fooXbar</p>')
    applySearchHighlights(root, 'foo.bar', false, -1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['foo.bar'])
  })

  it('currentOcc beyond the last occurrence paints all as plain matches', () => {
    root = el('<p>a b a</p>')
    applySearchHighlights(root, 'a', false, 99)
    expect(registeredRanges(SEARCH_HL_MATCH)).toHaveLength(2)
    expect(registeredRanges(SEARCH_HL_CURRENT)).toHaveLength(0)
  })

  it('repeated apply cycles keep the text nodes whole', () => {
    root = el('<p>hello world hello</p>')
    applySearchHighlights(root, 'hello', false, 0)
    applySearchHighlights(root, 'world', false, 0)
    applySearchHighlights(root, 'hello', false, -1)
    expect(root.querySelector('p')!.childNodes).toHaveLength(1)
    expect(paintedText(SEARCH_HL_MATCH)).toEqual(['hello', 'hello'])
  })
})

describe('clearSearchHighlights', () => {
  it('withdraws only the given element\'s ranges', () => {
    const a = el('<p>hello</p>')
    const b = el('<p>hello hello</p>')
    applySearchHighlights(a, 'hello', false, -1)
    applySearchHighlights(b, 'hello', false, 0)
    expect(registeredRanges(SEARCH_HL_MATCH)).toHaveLength(2)
    expect(registeredRanges(SEARCH_HL_CURRENT)).toHaveLength(1)
    clearSearchHighlights(b)
    expect(registeredRanges(SEARCH_HL_MATCH).map(r => r.startContainer)).toEqual([a.querySelector('p')!.firstChild])
    expect(registeredRanges(SEARCH_HL_CURRENT)).toHaveLength(0)
    clearSearchHighlights(a)
    expect(registeredRanges(SEARCH_HL_MATCH)).toHaveLength(0)
  })

  it('no-op on an element that painted nothing', () => {
    const root = el('<p>plain text</p>')
    expect(() => clearSearchHighlights(root)).not.toThrow()
    expect(root.textContent).toBe('plain text')
  })
})

describe('getCurrentSearchRange', () => {
  it('is null when nothing is painted as current', () => {
    const root = mount(el('<p>a b</p>'))
    applySearchHighlights(root, 'a', false, -1)
    expect(getCurrentSearchRange()).toBeNull()
  })

  it('returns the current range while its text node is in the document', () => {
    const root = mount(el('<p>a b a</p>'))
    applySearchHighlights(root, 'a', false, 1)
    const r = getCurrentSearchRange()
    expect(r).not.toBeNull()
    expect(r!.startOffset).toBe(4)
    expect(r!.toString()).toBe('a')
  })

  it('is null once the painted text node leaves the document', () => {
    const root = mount(el('<p>a b a</p>'))
    applySearchHighlights(root, 'a', false, 0)
    root.querySelector('p')!.textContent = 'rewritten'
    expect(getCurrentSearchRange()).toBeNull()
  })

  it('is null after the owning element withdraws', () => {
    const root = mount(el('<p>a</p>'))
    applySearchHighlights(root, 'a', false, 0)
    clearSearchHighlights(root)
    expect(getCurrentSearchRange()).toBeNull()
  })

  it('follows the most recent owner when the current match moves between bubbles', () => {
    const a = mount(el('<p>x</p>'))
    const b = mount(el('<p>x</p>'))
    applySearchHighlights(a, 'x', false, 0)
    applySearchHighlights(b, 'x', false, -1)
    expect(getCurrentSearchRange()!.startContainer).toBe(a.querySelector('p')!.firstChild)
    applySearchHighlights(a, 'x', false, -1)
    applySearchHighlights(b, 'x', false, 0)
    expect(getCurrentSearchRange()!.startContainer).toBe(b.querySelector('p')!.firstChild)
  })
})

describe('without the CSS Custom Highlight API', () => {
  it('paints nothing and mutates nothing, but still tracks the current match for scrolling', () => {
    const g = globalThis as unknown as { Highlight?: unknown }
    const saved = g.Highlight
    delete g.Highlight
    try {
      const root = mount(el('<p>hello world</p>'))
      const before = shape(root)
      const painted = registeredRanges(SEARCH_HL_MATCH).length + registeredRanges(SEARCH_HL_CURRENT).length
      expect(() => applySearchHighlights(root, 'world', false, 0)).not.toThrow()
      expect(shape(root)).toBe(before)
      expect(registeredRanges(SEARCH_HL_MATCH).length + registeredRanges(SEARCH_HL_CURRENT).length).toBe(painted)
      const cur = getCurrentSearchRange()
      expect(cur).not.toBeNull()
      expect(cur!.toString()).toBe('world')
      expect(() => clearSearchHighlights(root)).not.toThrow()
      expect(getCurrentSearchRange()).toBeNull()
    } finally {
      g.Highlight = saved
    }
  })
})

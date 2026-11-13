import { describe, it, expect } from 'vitest'
import { findBestOccurrence, findLooseSpan, indexPage, indexTextNodes, rangesForAnchor, spanPieces, unselectableSpans } from '../hooks/useMarkdownCommentHighlights'

/** Build a detached DOM tree from an HTML snippet (ACAT-safe, no innerHTML). */
function mount(html: string): HTMLDivElement {
  const root = document.createElement('div')
  root.appendChild(document.createRange().createContextualFragment(html))
  return root
}

/** The first range `rangesForAnchor` paints, with nothing to skip. */
const firstRange = (idx: Parameters<typeof rangesForAnchor>[0], a: Parameters<typeof rangesForAnchor>[1]) => rangesForAnchor(idx, a, [])[0] ?? null

describe('rangesForAnchor occurrence matching (startOffset)', () => {
  /** Absolute start offset of a Range within the joined text-node index. */
  const startOf = (r: Range, idx: ReturnType<typeof indexTextNodes>) =>
    idx.nodes.find(n => n.node === r.startContainer)!.start + r.startOffset

  it('picks the occurrence nearest startOffset when prefix/suffix are absent', () => {
    // "foo" at offsets 0, 8, 16. This is the reported bug: without a stored
    // offset the first match always won; the offset pins the real selection.
    const root = mount('<p>foo bar foo baz foo</p>')
    const idx = indexTextNodes(root)
    const second = idx.text.indexOf('foo', 1) // 8
    const r = firstRange(idx, { id: 'c1', quote: 'foo', prefix: '', suffix: '', startOffset: second })
    expect(r).not.toBeNull()
    expect(startOf(r!, idx)).toBe(second)
  })

  it('picks a later repeat with identical surrounding context via startOffset', () => {
    // Identical sentences → prefix/suffix can't disambiguate; only the offset can.
    const root = mount('<p>pick me. pick me. pick me.</p>')
    const idx = indexTextNodes(root)
    const third = idx.text.lastIndexOf('pick me') // 18
    const r = firstRange(idx, { id: 'c1', quote: 'pick me', prefix: '', suffix: '', startOffset: third })
    expect(r).not.toBeNull()
    expect(startOf(r!, idx)).toBe(third)
  })

  it('lets prefix/suffix override a stale offset (content shifted since capture)', () => {
    // Offset points at the FIRST occurrence, but the stored prefix matches the
    // SECOND. Prefix/suffix is the primary key, so the second still wins — a
    // stale positional guess never beats a content match.
    const root = mount('<p>we set the value. later we set the value.</p>')
    const idx = indexTextNodes(root)
    const first = idx.text.indexOf('set the value')
    const second = idx.text.indexOf('set the value', first + 1)
    const r = firstRange(idx, { id: 'c1', quote: 'set the value', prefix: 'later we ', suffix: '', startOffset: first })
    expect(r).not.toBeNull()
    expect(startOf(r!, idx)).toBe(second)
  })

  it('falls back to the nearest occurrence when startOffset is out of range', () => {
    // Offset past the end (content shrank) → nearest is the last occurrence.
    const root = mount('<p>foo bar foo baz foo</p>')
    const idx = indexTextNodes(root)
    const last = idx.text.lastIndexOf('foo') // 16
    const r = firstRange(idx, { id: 'c1', quote: 'foo', prefix: '', suffix: '', startOffset: 9999 })
    expect(r).not.toBeNull()
    expect(startOf(r!, idx)).toBe(last)
  })

  it('cuts the range around text a reader cannot select, such as the Copy buttons under a table', () => {
    const root = mount('<table><tbody><tr><td>per change</td><td>Ryan</td></tr></tbody></table>'
      + '<div class="select-none"><button>Copy Markdown</button><button>Copy CSV</button></div><p>After-table paragraph.</p>')
    const idx = indexTextNodes(root)
    const a = { id: 'c1', quote: 'per changeRyanCopy MarkdownCopy CSVAfter-table', prefix: '', suffix: '', startOffset: 0 }
    expect(rangesForAnchor(idx, a, unselectableSpans(root, idx.nodes)).map(r => r.toString())).toEqual(['per changeRyan', 'After-table'])
    expect(rangesForAnchor(idx, a, []).map(r => r.toString())).toEqual([a.quote])
  })

  it('starts a range in the cell that holds its first character, not at the end of the cell before', () => {
    const root = mount('<table><tbody><tr><td>Swarm</td><td>More review</td></tr></tbody></table>')
    const idx = indexTextNodes(root)
    const [r] = rangesForAnchor(idx, { id: 'c1', quote: 'More review', prefix: '', suffix: '', startOffset: 5 }, [])
    expect((r.startContainer as Text).data).toBe('More review')
    expect(r.startOffset).toBe(0)
  })

  it('keeps a quote whole, its edge whitespace included, when no unselectable text touches it', () => {
    const root = mount('<p>alpha beta gamma</p><div class="select-none">Copy</div>')
    const idx = indexTextNodes(root)
    const a = { id: 'c1', quote: ' beta ', prefix: '', suffix: '', startOffset: 5 }
    expect(rangesForAnchor(idx, a, unselectableSpans(root, idx.nodes)).map(r => r.toString())).toEqual([' beta '])
  })
})

describe('MarkdownPanel inline highlight matching (startOffset)', () => {
  it('returns first occurrence when no startOffset is stored (backward compat)', () => {
    const text = 'foo bar foo baz foo'
    expect(findBestOccurrence(text, 'foo')).toBe(0)
    expect(findBestOccurrence(text, 'foo', undefined)).toBe(0)
  })

  it('picks second occurrence when startOffset matches it', () => {
    const text = 'foo bar foo baz foo'
    const second = text.indexOf('foo', 1) // 8
    expect(findBestOccurrence(text, 'foo', second)).toBe(second)
  })

  it('picks third occurrence when startOffset matches it', () => {
    const text = 'foo bar foo baz foo'
    const third = text.lastIndexOf('foo') // 16
    expect(findBestOccurrence(text, 'foo', third)).toBe(third)
  })

  it('picks the nearest occurrence when startOffset is between two matches', () => {
    // "foo" at 0, 8, 16. startOffset=10 is nearest to 8.
    const text = 'foo bar foo baz foo'
    expect(findBestOccurrence(text, 'foo', 10)).toBe(8)
  })

  it('picks the nearest occurrence when startOffset is past all matches', () => {
    const text = 'foo bar foo baz foo'
    const last = text.lastIndexOf('foo') // 16
    expect(findBestOccurrence(text, 'foo', 9999)).toBe(last)
  })

  it('returns -1 when anchor is not found', () => {
    expect(findBestOccurrence('hello world', 'missing')).toBe(-1)
    expect(findBestOccurrence('hello world', 'missing', 5)).toBe(-1)
  })

  it('handles single occurrence with startOffset correctly', () => {
    const text = 'unique phrase here'
    expect(findBestOccurrence(text, 'unique phrase', 0)).toBe(0)
    expect(findBestOccurrence(text, 'unique phrase', 999)).toBe(0) // only one, always picks it
  })

  it('handles identical repeated sentences', () => {
    const text = 'set the value. set the value. set the value.'
    const first = 0
    const second = text.indexOf('set the value', 1) // 15
    const third = text.lastIndexOf('set the value') // 30
    expect(findBestOccurrence(text, 'set the value', first)).toBe(first)
    expect(findBestOccurrence(text, 'set the value', second)).toBe(second)
    expect(findBestOccurrence(text, 'set the value', third)).toBe(third)
  })
})

describe('findBestOccurrence guards and optimizations', () => {
  it('returns -1 for empty anchor (prevents infinite loop)', () => {
    expect(findBestOccurrence('some text', '', 5)).toBe(-1)
    expect(findBestOccurrence('some text', '')).toBe(-1)
    expect(findBestOccurrence('', '', 0)).toBe(-1)
  })

  it('early-exits once distance starts growing (V-shaped optimization)', () => {
    // "foo" at offsets 0, 8, 16. startOffset=8 (exact match on second).
    // The function should find offset 8 and stop — not scan to 16.
    // We verify correctness (the optimization must not break results).
    const text = 'foo bar foo baz foo'
    expect(findBestOccurrence(text, 'foo', 8)).toBe(8)
    // startOffset=9 is closer to 8 than to 16, so should pick 8
    expect(findBestOccurrence(text, 'foo', 9)).toBe(8)
    // startOffset=12 is equidistant (4 from 8, 4 from 16) — picks first seen (8)
    expect(findBestOccurrence(text, 'foo', 12)).toBe(8)
    // startOffset=13 is closer to 16 than to 8 — but early-exit after 8
    // means we break before seeing 16. This is the tradeoff: early-exit
    // picks the last-improving occurrence, which for offset=13 is 8 (dist=5)
    // vs 16 (dist=3). The V-shape assumption only holds when startOffset is
    // between occurrences. When startOffset is past the nearest, we still
    // need to keep scanning. Let me verify the actual behavior:
    // i=0 dist=13, bestDist=13, bestIdx=0
    // i=8 dist=5 < 13, bestDist=5, bestIdx=8
    // i=16 dist=3 < 5, bestDist=3, bestIdx=16 — this would NOT early-exit
    // Actually dist=3 < bestDist=5 so it improves! The break fires only
    // when dist >= bestDist. So for offset=13: 0->13, 8->5, 16->3 — all
    // improving, no break until indexOf returns -1. Correct result = 16.
    expect(findBestOccurrence(text, 'foo', 13)).toBe(16)
  })

  it('early-exit does not break when startOffset is before first occurrence', () => {
    // startOffset=0, "foo" at 4, 12, 20
    const text = '    foo bar foo baz foo'
    // i=4 dist=4, i=12 dist=12 > 4 → break. Result = 4. Correct!
    expect(findBestOccurrence(text, 'foo', 0)).toBe(4)
  })

  it('early-exit does not break when startOffset is after last occurrence', () => {
    const text = 'foo bar foo end'
    const last = text.lastIndexOf('foo') // 8
    // startOffset=999: i=0 dist=999, i=8 dist=991 < 999, then -1 → break
    expect(findBestOccurrence(text, 'foo', 999)).toBe(last)
  })
})

// The quotes below are the strings Chrome 155 returned from Selection.toString()
// over a rendered markdown table and paragraphs in the file viewer. The page
// strings are the same selections' text-node text, which the highlighter
// searches.
describe('findLooseSpan: a selection-string quote with no renderedQuote', () => {
  const loose = (text: string, anchor: string, startOffset?: number) => findLooseSpan(indexPage(text), anchor, startOffset)
  const span = (text: string, needle: string, from = 0) => {
    const start = text.indexOf(needle, from)
    return { start, end: start + needle.length }
  }

  it('matches across two table cells, which the selection joins with a tab and the page with nothing', () => {
    const text = 'ChangeWhy it mattersSwarm mode on more of my workMore review per changeRyan'
    expect(loose(text, 'of my work\tMore review')).toEqual(span(text, 'of my workMore review'))
  })

  it('matches across table rows, a header row, an empty cell, a <br> and a whole row', () => {
    const text = 'Why it mattersOwnerSwarm mode on more of my workMore review per changeRyanCells withA linebreak inside a cellNon breaking'
    expect(loose(text, 'Ryan\nCells with')).toEqual(span(text, 'RyanCells with'))
    expect(loose(text, 'Why it matters\tOwner\nSwarm mode')).toEqual(span(text, 'Why it mattersOwnerSwarm mode'))
    expect(loose(text, 'inside a cell\t\nNon')).toEqual(span(text, 'inside a cellNon'))
    expect(loose(text, 'A line\nbreak inside')).toEqual(span(text, 'A linebreak inside'))
    expect(loose(text, 'Swarm mode on more of my work\tMore review per change\tRyan'))
      .toEqual(span(text, 'Swarm mode on more of my workMore review per changeRyan'))
  })

  it('matches a blank line between paragraphs and a soft line break, which the selection writes as a space', () => {
    const text = 'Intro paragraph one with a soft\nline break in the middle.\nSecond paragraph.'
    expect(loose(text, 'soft line break')).toEqual(span(text, 'soft\nline break'))
    expect(loose(text, 'one with a soft line break in the middle.\n\nSecond'))
      .toEqual({ start: text.indexOf('one with'), end: text.indexOf('Second') + 6 })
  })

  it('picks the copy nearest startOffset when the same cells repeat', () => {
    const row = 'Swarm mode on more of my workMore review per changeRyan'
    const text = `${row}Cells with${row}`
    const second = text.lastIndexOf('of my work')
    expect(loose(text, 'of my work\tMore review', second)).toEqual({ start: second, end: second + 'of my workMore review'.length })
    expect(loose(text, 'of my work\tMore review', 3)).toEqual(span(text, 'of my workMore review'))
    // An edit above the table moves both copies; the nearer one is still the same cells.
    expect(loose(`New intro.${text}`, 'of my work\tMore review', 3)).toEqual(span(`New intro.${text}`, 'of my workMore review'))
  })

  it('without an offset takes only a match the quote has nowhere else', () => {
    expect(loose('a\nb then x', 'a b')).toEqual({ start: 0, end: 3 })
    expect(loose('a\nb then a\tb', 'a b')).toBeNull()
    // One cell holding "foobar", then a row whose two cells are "foo" and "bar":
    // a comment on the two cells, saved with no offset, can't say which it was.
    expect(loose('foobarxfoobar', 'foo\tbar')).toBeNull()
  })

  it('ignores only whitespace: any other difference is no match', () => {
    expect(loose('of my work-More review', 'of my work\tMore review')).toBeNull()
    expect(loose('some text', 'missing', 3)).toBeNull()
    expect(loose('some text', '')).toBeNull()
    expect(loose('some text', ' \t\n', 0)).toBeNull()
  })

  it('treats a non-breaking space as whitespace', () => {
    expect(loose('Non\u00a0breaking space', 'Non breaking')).toEqual({ start: 0, end: 12 })
  })

  it('skips text a reader cannot select, such as the Copy buttons under a table', () => {
    // Chrome left the table's Copy Markdown / Copy CSV row out of this selection string.
    const root = mount('<table><tbody><tr><td>per change</td><td>Ryan</td></tr></tbody></table>'
      + '<div class="select-none"><button>Copy Markdown</button><button>Copy CSV</button></div><p>After-table paragraph.</p>')
    const idx = indexTextNodes(root)
    const skipped = unselectableSpans(root, idx.nodes)
    expect(skipped).toEqual([[14, 27], [27, 35]])
    expect(findLooseSpan(indexPage(idx.text, skipped), 'per change\tRyan\nAfter-table', 0))
      .toEqual({ start: 0, end: idx.text.indexOf('After-table') + 11 })
    // Without the skip the buttons' words sit between the cells and the paragraph.
    expect(findLooseSpan(indexPage(idx.text), 'per change\tRyan\nAfter-table', 0)).toBeNull()
  })

  it('finds nothing to skip outside a select-none element', () => {
    const root = mount('<p>alpha</p><p>beta</p>')
    expect(unselectableSpans(root, indexTextNodes(root).nodes)).toEqual([])
  })

  it('takes a select-none element inside another once, and keeps the spans in page order', () => {
    const root = mount('<p>one</p><div class="select-none">ab<span class="select-none">cd</span></div>'
      + '<p>two</p><div class="select-none">ef</div>')
    const idx = indexTextNodes(root)
    expect(idx.text).toBe('oneabcdtwoef')
    expect(unselectableSpans(root, idx.nodes)).toEqual([[3, 5], [5, 7], [10, 12]])
  })
})

describe('spanPieces: a matched span with unselectable text cut out', () => {
  const text = 'per changeRyanWidenCopy MarkdownCopy CSV\nAfter-table'
  const buttons: [number, number][] = [[14, 19], [19, 32], [32, 40]]

  it('paints around the cut, trimming whitespace and dropping empty pieces', () => {
    const pieces = spanPieces(text, 0, text.length, buttons)
    expect(pieces.map(([s, e]) => text.slice(s, e))).toEqual(['per changeRyan', 'After-table'])
  })

  it('returns the span whole when nothing in it is cut', () => {
    expect(spanPieces(text, 0, 14, buttons)).toEqual([[0, 14]])
    expect(spanPieces(text, 0, 14, [])).toEqual([[0, 14]])
  })

  it('returns nothing when the span is all cut or all whitespace', () => {
    expect(spanPieces(text, 14, 40, buttons)).toEqual([])
    expect(spanPieces('a \n b', 1, 4, [])).toEqual([])
  })
})

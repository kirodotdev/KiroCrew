import { describe, it, expect } from 'vitest'
import { findBestOccurrence, indexTextNodes, rangeForAnchor } from '../hooks/useMarkdownCommentHighlights'

/** Build a detached DOM tree from an HTML snippet (ACAT-safe, no innerHTML). */
function mount(html: string): HTMLDivElement {
  const root = document.createElement('div')
  root.appendChild(document.createRange().createContextualFragment(html))
  return root
}

describe('rangeForAnchor occurrence matching (startOffset)', () => {
  /** Absolute start offset of a Range within the joined text-node index. */
  const startOf = (r: Range, idx: ReturnType<typeof indexTextNodes>) =>
    idx.nodes.find(n => n.node === r.startContainer)!.start + r.startOffset

  it('picks the occurrence nearest startOffset when prefix/suffix are absent', () => {
    // "foo" at offsets 0, 8, 16. This is the reported bug: without a stored
    // offset the first match always won; the offset pins the real selection.
    const root = mount('<p>foo bar foo baz foo</p>')
    const idx = indexTextNodes(root)
    const second = idx.text.indexOf('foo', 1) // 8
    const r = rangeForAnchor(idx, { id: 'c1', quote: 'foo', prefix: '', suffix: '', startOffset: second })
    expect(r).not.toBeNull()
    expect(startOf(r!, idx)).toBe(second)
  })

  it('picks a later repeat with identical surrounding context via startOffset', () => {
    // Identical sentences → prefix/suffix can't disambiguate; only the offset can.
    const root = mount('<p>pick me. pick me. pick me.</p>')
    const idx = indexTextNodes(root)
    const third = idx.text.lastIndexOf('pick me') // 18
    const r = rangeForAnchor(idx, { id: 'c1', quote: 'pick me', prefix: '', suffix: '', startOffset: third })
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
    const r = rangeForAnchor(idx, { id: 'c1', quote: 'set the value', prefix: 'later we ', suffix: '', startOffset: first })
    expect(r).not.toBeNull()
    expect(startOf(r!, idx)).toBe(second)
  })

  it('falls back to the nearest occurrence when startOffset is out of range', () => {
    // Offset past the end (content shrank) → nearest is the last occurrence.
    const root = mount('<p>foo bar foo baz foo</p>')
    const idx = indexTextNodes(root)
    const last = idx.text.lastIndexOf('foo') // 16
    const r = rangeForAnchor(idx, { id: 'c1', quote: 'foo', prefix: '', suffix: '', startOffset: 9999 })
    expect(r).not.toBeNull()
    expect(startOf(r!, idx)).toBe(last)
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

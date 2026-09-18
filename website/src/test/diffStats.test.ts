import { describe, it, expect } from 'vitest'
// Both from `utils/diffLineCounts`, not from the component/page that re-exports
// them: importing those pulled the Pierre diff runtime, framer-motion,
// react-markdown, katex and highlight.js into this fork — measured 144.65s, of
// which 51ms was the tests.
import { countLines, countDiffStats, changedLineSpan } from '../utils/diffLineCounts'

describe('countLines (diff stats)', () => {
  it('returns zeros for identical content', () => {
    expect(countLines('hello', 'hello')).toEqual({ added: 0, removed: 0 })
  })

  it('counts added lines for new file', () => {
    const { added, removed } = countLines('', 'line1\nline2\nline3')
    expect(added).toBe(3)
    expect(removed).toBe(0)
  })

  it('counts removed lines for deleted content', () => {
    const { added, removed } = countLines('line1\nline2\nline3', '')
    expect(added).toBe(0)
    expect(removed).toBe(3)
  })

  it('counts both added and removed for modifications', () => {
    const { added, removed } = countLines('old1\nold2\nkeep', 'keep\nnew1\nnew2\nnew3')
    expect(added).toBeGreaterThan(0)
    expect(removed).toBeGreaterThan(0)
  })

  it('handles single line change', () => {
    const { added, removed } = countLines('before', 'after')
    expect(added).toBe(1)
    expect(removed).toBe(1)
  })
})

// Test the countDiffStats from ActivityViewer (parsing unified diff output)
describe('countDiffStats (unified diff parsing)', () => {

  it('returns zeros for empty diff', () => {
    expect(countDiffStats('')).toEqual({ added: 0, removed: 0 })
  })

  it('counts added lines from unified diff', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,4 @@
 keep
+new line 1
+new line 2
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 2, removed: 0 })
  })

  it('counts removed lines from unified diff', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,4 +1,2 @@
 keep
-removed 1
-removed 2
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 0, removed: 2 })
  })

  it('counts both added and removed', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,3 @@
 keep
-old line
+new line
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 1, removed: 1 })
  })

  it('ignores --- and +++ header lines', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1 +1 @@
-old
+new`
    expect(countDiffStats(diff)).toEqual({ added: 1, removed: 1 })
  })
})

describe('changedLineSpan (bounded change locality)', () => {
  const lines = (arr: string[]) => arr
  it('returns null for identical content', () => {
    expect(changedLineSpan(['a', 'b', 'c'], ['a', 'b', 'c'])).toBeNull()
  })

  it('finds a single deep edit as a one-line span on both sides', () => {
    const before = Array.from({ length: 1000 }, (_, i) => `L${i}`)
    const after = [...before]
    after[869] = 'L869 changed'
    expect(changedLineSpan(before, after)).toEqual({ oldStart: 869, oldEnd: 870, newStart: 869, newEnd: 870 })
  })

  it('covers scattered edits with one outer span', () => {
    const before = lines(['a', 'b', 'c', 'd', 'e'])
    const after = lines(['a', 'B', 'c', 'D', 'e'])
    // First diff at index 1, last diff at index 3 -> [1,4) on both sides.
    expect(changedLineSpan(before, after)).toEqual({ oldStart: 1, oldEnd: 4, newStart: 1, newEnd: 4 })
  })

  it('handles a pure insertion (empty removed range on the old side)', () => {
    const before = lines(['a', 'b', 'c'])
    const after = lines(['a', 'x', 'y', 'b', 'c'])
    const span = changedLineSpan(before, after)!
    // Common prefix 'a' (1), common suffix 'b','c' (2): old range is empty, new range is the two inserts.
    expect(span.oldStart).toBe(1)
    expect(span.oldEnd).toBe(1)
    expect(span.newStart).toBe(1)
    expect(span.newEnd).toBe(3)
  })

  it('handles append at end and prepend at start', () => {
    expect(changedLineSpan(['a', 'b'], ['a', 'b', 'c'])).toEqual({ oldStart: 2, oldEnd: 2, newStart: 2, newEnd: 3 })
    expect(changedLineSpan(['b', 'c'], ['a', 'b', 'c'])).toEqual({ oldStart: 0, oldEnd: 0, newStart: 0, newEnd: 1 })
  })

  it('treats a whole-file replacement as a full-range span', () => {
    expect(changedLineSpan(['a', 'b'], ['x', 'y', 'z'])).toEqual({ oldStart: 0, oldEnd: 2, newStart: 0, newEnd: 3 })
  })
})

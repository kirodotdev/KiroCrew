import { describe, it, expect } from 'vitest'
// Both from `utils/diffLineCounts`, not from the component/page that re-exports
// them: importing those pulled the Pierre diff runtime, framer-motion,
// react-markdown, katex and highlight.js into this fork — measured 144.65s, of
// which 51ms was the tests.
import { countLines, countDiffStats } from '../utils/diffLineCounts'

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

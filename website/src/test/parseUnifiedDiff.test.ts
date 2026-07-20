import { describe, expect, it } from 'vitest'
import { parseUnifiedDiff } from '../utils/parseUnifiedDiff'

describe('parseUnifiedDiff', () => {
  it('numbers add, del, and context rows from the hunk header', () => {
    const rows = parseUnifiedDiff('@@ -1,3 +1,3 @@\n context\n-old\n+new\n')
    expect(rows).toEqual([
      { kind: 'context', oldLine: 1, newLine: 1, text: 'context' },
      { kind: 'del', oldLine: 2, newLine: null, text: 'old' },
      { kind: 'add', oldLine: null, newLine: 2, text: 'new' },
    ])
  })

  it('emits a leading gap when the first hunk starts past line 1', () => {
    const rows = parseUnifiedDiff('@@ -10,2 +10,2 @@\n a\n b\n')
    expect(rows[0]).toEqual({ kind: 'hunk-gap', hiddenCount: 9 })
  })

  it('sizes gaps between hunks from old-file line numbers', () => {
    const rows = parseUnifiedDiff('@@ -1,2 +1,2 @@\n a\n b\n@@ -150,2 +150,2 @@\n c\n d\n')
    const gap = rows.find(row => row.kind === 'hunk-gap')
    expect(gap).toEqual({ kind: 'hunk-gap', hiddenCount: 147 })
  })

  it('skips no-newline markers', () => {
    const rows = parseUnifiedDiff('@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n')
    expect(rows.map(row => row.kind)).toEqual(['del', 'add'])
  })

  it('returns no rows for an empty patch', () => {
    expect(parseUnifiedDiff('')).toEqual([])
  })
})

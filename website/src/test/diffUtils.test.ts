import { describe, it, expect } from 'vitest'
import { parseDiffLines, isDiffText, DIFF_BG, DIFF_FG, type DiffLine } from '../utils/diffUtils'

describe('parseDiffLines', () => {
  it('parses standard unified diff', () => {
    const lines = parseDiffLines("--- a/file.ts\n+++ b/file.ts\n@@ -1,3 +1,3 @@\n const a = 1\n-const b = 2\n+const b = 3\n const c = 4")
    const types = lines.map(l => l.type)
    expect(types).toEqual(['meta', 'meta', 'hunk', 'context', 'del', 'add', 'context'])
  })

  it('strips +/- prefix from content', () => {
    const lines = parseDiffLines("-old line\n+new line")
    expect(lines[0].content).toBe('old line')
    expect(lines[1].content).toBe('new line')
  })

  it('strips leading space from context lines', () => {
    const lines = parseDiffLines(" context line")
    expect(lines[0].content).toBe('context line')
    expect(lines[0].type).toBe('context')
  })

  it('parses hunk headers and extracts line numbers', () => {
    const lines = parseDiffLines("@@ -10,5 +20,7 @@")
    expect(lines[0].type).toBe('hunk')
    expect(lines[0].content).toBe('@@ -10,5 +20,7 @@')
  })

  it('tracks line numbers through changes', () => {
    const lines = parseDiffLines("@@ -1,3 +1,3 @@\n context\n-deleted\n+added")
    const del = lines.find(l => l.type === 'del')!
    const add = lines.find(l => l.type === 'add')!
    expect(del.oldNum).toBe(2)
    expect(add.newNum).toBe(2)
  })

  it('handles kiro-cli +N:content format', () => {
    const lines = parseDiffLines("+10:const x = 1\n-5:const y = 2")
    expect(lines[0]).toEqual({ type: 'add', content: 'const x = 1', newNum: 10 })
    expect(lines[1]).toEqual({ type: 'del', content: 'const y = 2', oldNum: 5 })
  })

  it('treats diff --git and index lines as meta', () => {
    const lines = parseDiffLines("diff --git a/f.ts b/f.ts\nindex abc..def 100644\n@@ -1,1 +1,1 @@\n-old\n+new")
    expect(lines[0].type).toBe('meta')
    expect(lines[1].type).toBe('meta')
  })

  it('returns empty array for empty input', () => {
    expect(parseDiffLines('')).toEqual([{ type: 'context', content: '', oldNum: 0, newNum: 0 }])
  })

  describe('hidden count on hunk lines', () => {
    it('leaves hidden undefined on the first hunk', () => {
      const lines = parseDiffLines('@@ -140,3 +140,3 @@\n context\n-old\n+new')
      expect(lines[0].type).toBe('hunk')
      expect(lines[0].hidden).toBeUndefined()
    })

    it('computes unchanged lines skipped between hunks', () => {
      // First hunk consumes old lines 1-3 (context, del, context) → next old
      // line is 4. Second hunk starts at old line 150 → 146 lines skipped.
      const lines = parseDiffLines('@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n@@ -150,2 +150,2 @@\n d\n-e\n+E')
      const hunks = lines.filter(l => l.type === 'hunk')
      expect(hunks).toHaveLength(2)
      expect(hunks[0].hidden).toBeUndefined()
      expect(hunks[1].hidden).toBe(146)
    })

    it('clamps hidden to zero for adjacent hunks', () => {
      const lines = parseDiffLines('@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -3,1 +3,1 @@\n-c\n+C')
      const hunks = lines.filter(l => l.type === 'hunk')
      expect(hunks[1].hidden).toBe(0)
    })

    it('resets hunk state at file boundaries — no cross-file separator', () => {
      // Two files in one patch: file B's first hunk must NOT inherit file A's
      // line counters (that fabricated an "unchanged lines" separator joining
      // unrelated files).
      const multi = [
        '--- a/a.ts', '+++ b/a.ts',
        '@@ -1,2 +1,2 @@', ' x', '-y', '+Y',
        '--- a/b.ts', '+++ b/b.ts',
        '@@ -500,2 +500,2 @@', ' p', '-q', '+Q',
      ].join('\n')
      const lines = parseDiffLines(multi)
      const hunks = lines.filter(l => l.type === 'hunk')
      expect(hunks).toHaveLength(2)
      expect(hunks[0].hidden).toBeUndefined()
      // First hunk of the SECOND file: also undefined, not 500-minus-file-A-state.
      expect(hunks[1].hidden).toBeUndefined()
    })

    it('resets hunk state at diff --git headers appearing mid-patch', () => {
      const multi = [
        '@@ -1,2 +1,2 @@', ' x', '-y', '+Y',
        'diff --git a/b.ts b/b.ts',
        '@@ -300,2 +300,2 @@', ' p', '-q', '+Q',
      ].join('\n')
      const lines = parseDiffLines(multi)
      const hunks = lines.filter(l => l.type === 'hunk')
      expect(hunks[1].hidden).toBeUndefined()
      // The mid-patch header itself is meta, not a context line.
      const header = lines.find(l => l.content.startsWith('diff --git'))!
      expect(header.type).toBe('meta')
    })

    it('does not mistake a deleted line starting with -- for a file header', () => {
      // Deleting the SQL comment `-- total rows` renders as `--- total rows`
      // in the patch. While the hunk is still consuming its declared lines,
      // that row is a DELETION, not a `---` file header — no state reset.
      const patch = '@@ -1,3 +1,2 @@\n SELECT 1;\n--- total rows\n SELECT 2;'
      const lines = parseDiffLines(patch)
      expect(lines.map(l => l.type)).toEqual(['hunk', 'context', 'del', 'context'])
      const del = lines[2]
      expect(del.content).toBe('-- total rows')
      expect(del.oldNum).toBe(2)
      // Numbering continues past it — no restart at 0.
      expect(lines[3]).toMatchObject({ oldNum: 3, newNum: 2 })
    })

    it('does not mistake an added line starting with ++ for a file header', () => {
      const patch = '@@ -1,1 +1,2 @@\n a\n+++ b'
      const lines = parseDiffLines(patch)
      expect(lines.map(l => l.type)).toEqual(['hunk', 'context', 'add'])
      expect(lines[2].content).toBe('++ b')
      expect(lines[2].newNum).toBe(2)
    })

    it('still treats --- as a file header once the hunk is exhausted', () => {
      // Same shape as the multi-file test but with the header IMMEDIATELY
      // after the last declared line — the count-consumption gate must have
      // reached zero exactly there.
      const patch = '@@ -1,1 +1,1 @@\n-x\n+X\n--- a/next.ts\n+++ b/next.ts\n@@ -9,1 +9,1 @@\n-p\n+P'
      const lines = parseDiffLines(patch)
      const metas = lines.filter(l => l.type === 'meta')
      expect(metas.map(l => l.content)).toEqual(['--- a/next.ts', '+++ b/next.ts'])
      const hunks = lines.filter(l => l.type === 'hunk')
      expect(hunks[1].hidden).toBeUndefined()
    })

    it('skips no-newline markers without consuming declared line counts', () => {
      // Old file ends without a newline: the marker sits between the deletion
      // and the additions. It must not render, must not advance numbering,
      // and must not eat the hunk's remaining counts — otherwise the
      // following `+++ b` addition would be misread as a file header.
      const patch = '@@ -1,2 +1,3 @@\n a\n-last$\n\\ No newline at end of file\n+last\n+++ b'
      const lines = parseDiffLines(patch)
      expect(lines.map(l => l.type)).toEqual(['hunk', 'context', 'del', 'add', 'add'])
      expect(lines.find(l => l.content.includes('No newline'))).toBeUndefined()
      expect(lines[4].content).toBe('++ b')
      expect(lines[4].newNum).toBe(3)
    })

    it('skips a trailing no-newline marker on the new side too', () => {
      const patch = '@@ -1,1 +1,1 @@\n-x\n+y\n\\ No newline at end of file\n--- a/next.ts\n+++ b/next.ts\n@@ -5,1 +5,1 @@\n-p\n+P'
      const lines = parseDiffLines(patch)
      // Counts were already exhausted before the marker; the headers after it
      // still parse as file headers and the second file starts fresh.
      const metas = lines.filter(l => l.type === 'meta')
      expect(metas).toHaveLength(2)
      expect(lines.filter(l => l.type === 'hunk')[1].hidden).toBeUndefined()
    })
  })
})

describe('isDiffText', () => {
  it('returns true for text with @@ hunks', () => {
    expect(isDiffText('@@ -1,3 +1,3 @@\n-old\n+new')).toBe(true)
  })

  it('returns true for text with ---/+++ file headers', () => {
    expect(isDiffText('--- a/file.ts\n+++ b/file.ts\n-old\n+new')).toBe(true)
  })

  it('returns false for plain text', () => {
    expect(isDiffText('just some text')).toBe(false)
  })

  it('returns false for JSON', () => {
    expect(isDiffText('{"key": "value"}')).toBe(false)
  })

  it('returns false for markdown lists', () => {
    expect(isDiffText('- item one\n- item two\n+ not a diff')).toBe(false)
  })

  it('returns false for negative numbers', () => {
    expect(isDiffText('-5 degrees')).toBe(false)
  })

  it('does not false-positive on YAML front matter with +++ heading', () => {
    expect(isDiffText('---\ntitle: doc\n+++ heading')).toBe(false)
  })
})

describe('DIFF_BG and DIFF_FG', () => {
  it('has entries for all line types', () => {
    const types: DiffLine['type'][] = ['add', 'del', 'context', 'hunk', 'meta']
    for (const t of types) {
      expect(DIFF_BG[t]).toBeDefined()
      expect(DIFF_FG[t]).toBeDefined()
    }
  })

  it('uses correct Tailwind classes', () => {
    expect(DIFF_BG.add).toBe('bg-diff-add')
    expect(DIFF_BG.del).toBe('bg-diff-del')
    expect(DIFF_FG.add).toBe('text-diff-add-text')
    expect(DIFF_FG.del).toBe('text-diff-del-text')
  })
})

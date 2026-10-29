import { describe, it, expect } from 'vitest'
import { splitFrontmatter } from '../components/markdown/frontmatter'

describe('splitFrontmatter', () => {
  it('parses an OKF frontmatter block into ordered entries and keeps the body line-aligned', () => {
    const raw = [
      '---', // line 1
      'okf_version: "0.1"', // line 2
      'title: Quarterly incident retrospective', // line 3
      'tags: [retrospective, reliability]', // line 4
      '---', // line 5
      '# Quarterly incident retrospective', // line 6
      '',
      'Body here.',
    ].join('\n')
    const r = splitFrontmatter(raw)
    expect(r.hasFrontmatter).toBe(true)
    expect(r.entries.map((e) => e.key)).toEqual(['okf_version', 'title', 'tags'])
    expect(r.entries[0]).toEqual({ key: 'okf_version', value: '0.1' })
    expect(r.entries[2].value).toBe('retrospective, reliability')
    // The raw frontmatter block (the `key: value` lines) is no longer in the body.
    expect(r.body).not.toContain('okf_version:')
    // But the heading keeps its true source line: it is on line 6 of the full
    // document, so it must be on line 6 of the padded body too (lines are
    // 1-based, so index 5 after splitting on '\n'). Without the pad the resolver
    // that indexes the full file would anchor body comments to the wrong line.
    const bodyLines = r.body.split('\n')
    expect(bodyLines[5]).toBe('# Quarterly incident retrospective')
    // The five frontmatter source lines are preserved as blank padding.
    expect(bodyLines.slice(0, 5)).toEqual(['', '', '', '', ''])
  })

  it('returns the raw document unchanged when there is no frontmatter', () => {
    const raw = '# Just a heading\n\nNo frontmatter.'
    const r = splitFrontmatter(raw)
    expect(r.hasFrontmatter).toBe(false)
    expect(r.body).toBe(raw)
    expect(r.entries).toEqual([])
  })

  it('does not treat a lone horizontal rule as frontmatter', () => {
    const raw = '---\n\nnot a closing fence, just a rule above text'
    const r = splitFrontmatter(raw)
    expect(r.hasFrontmatter).toBe(false)
    expect(r.body).toBe(raw)
  })

  it('requires the closing fence to be exactly --- on its own line', () => {
    // A `---text` line and a `-----` line are not closing fences; the whole
    // document must pass through unchanged rather than being mis-split.
    for (const raw of ['---\na: 1\n---frontmatterish\nbody', '---\na: 1\n-----\nbody']) {
      const r = splitFrontmatter(raw)
      expect(r.hasFrontmatter).toBe(false)
      expect(r.body).toBe(raw)
    }
  })

  it('tolerates CRLF line endings and trailing spaces on the fences', () => {
    const raw = '--- \r\ntitle: Hello\r\n--- \r\n# Hello\r\n'
    const r = splitFrontmatter(raw)
    expect(r.hasFrontmatter).toBe(true)
    expect(r.entries).toEqual([{ key: 'title', value: 'Hello' }])
    // `# Hello` is the 4th source line (--- / title / --- / # Hello), so after
    // three blank pad lines it sits on body line 4 (index 3). The body keeps
    // the document's original CRLF endings, so split on '\n' leaves the '\r'.
    expect(r.body.split('\n')[3]).toBe('# Hello\r')
  })

  it('pads the body so the body keeps the same line count below the block', () => {
    const raw = '---\na: 1\nb: 2\n---\nline6\nline7\n'
    const r = splitFrontmatter(raw)
    expect(r.hasFrontmatter).toBe(true)
    // Block spans lines 1-4 (--- / a / b / ---); body text starts on line 5.
    const lines = r.body.split('\n')
    expect(lines[4]).toBe('line6')
    expect(lines[5]).toBe('line7')
    expect(lines.slice(0, 4)).toEqual(['', '', '', ''])
  })
})

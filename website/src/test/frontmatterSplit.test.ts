import { describe, it, expect } from 'vitest'
import { splitFrontmatter } from '../components/markdown/frontmatter'

describe('splitFrontmatter', () => {
  it('parses an OKF frontmatter block into ordered entries and strips it from the body', () => {
    const raw = [
      '---',
      'okf_version: "0.1"',
      'title: Quarterly incident retrospective',
      'tags: [retrospective, reliability]',
      '---',
      '# Quarterly incident retrospective',
      '',
      'Body here.',
    ].join('\n')
    const r = splitFrontmatter(raw)
    expect(r.hasFrontmatter).toBe(true)
    expect(r.entries.map((e) => e.key)).toEqual(['okf_version', 'title', 'tags'])
    expect(r.entries[0]).toEqual({ key: 'okf_version', value: '0.1' })
    expect(r.entries[2].value).toBe('retrospective, reliability')
    expect(r.body.startsWith('# Quarterly incident retrospective')).toBe(true)
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
    expect(r.body.startsWith('# Hello')).toBe(true)
  })
})

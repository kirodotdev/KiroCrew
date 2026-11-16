import { describe, it, expect } from 'vitest'
import { findFrontmatterBlock, splitFrontmatter } from '../components/markdown/frontmatter'

/** The strip's value for the first (only) key of a one-key YAML block. */
function flatOf(yaml: string): string {
  const r = splitFrontmatter(`---\n${yaml}\n---\nbody`)
  if (!r.hasFrontmatter) throw new Error('no frontmatter entries')
  return r.entries[0].value
}

describe('frontmatter.findFrontmatterBlock', () => {
  it('finds a leading block and reports the body start and line count', () => {
    const raw = '---\na: 1\nb: 2\n---\nbody line\n'
    const found = findFrontmatterBlock(raw)
    expect(found).not.toBeNull()
    expect(found!.block).toBe('a: 1\nb: 2')
    // Block spans 4 source lines (--- / a / b / ---); the body starts after them.
    expect(found!.blockLineCount).toBe(4)
    expect(raw.slice(found!.bodyStart)).toBe('body line\n')
  })

  it('returns null when the document does not open with a fence', () => {
    expect(findFrontmatterBlock('# Heading\n')).toBeNull()
    expect(findFrontmatterBlock('text\n---\na: 1\n---\n')).toBeNull()
  })

  it('returns null when there is no closing fence', () => {
    expect(findFrontmatterBlock('---\na: 1\nno close')).toBeNull()
  })

  it('requires the closing fence to be exactly --- on its own line', () => {
    expect(findFrontmatterBlock('---\na: 1\n---x\nbody')).toBeNull()
    expect(findFrontmatterBlock('---\na: 1\n-----\nbody')).toBeNull()
  })

  it('handles a block that ends at end-of-document with no trailing body', () => {
    const raw = '---\na: 1\n---'
    const found = findFrontmatterBlock(raw)
    expect(found).not.toBeNull()
    expect(found!.block).toBe('a: 1')
    expect(raw.slice(found!.bodyStart)).toBe('')
  })
})

describe('frontmatter entry values (flattened by the shared parseFrontmatter)', () => {
  it('renders a scalar verbatim', () => {
    expect(flatOf('k: hello world')).toBe('hello world')
  })

  it('renders an empty scalar as an empty string', () => {
    expect(flatOf('k:')).toBe('')
  })

  it('joins a sequence with ", "', () => {
    expect(flatOf('k: [a, b, c]')).toBe('a, b, c')
  })

  it('keeps every key and value of a nested map', () => {
    const v = flatOf('k:\n  x: 1\n  y: 2')
    expect(v).toContain('x: 1')
    expect(v).toContain('y: 2')
  })

  it('agrees with parseFrontmatter on the same block', async () => {
    const { parseFrontmatter } = await import('../components/SkillForm')
    const yaml = 'name: demo\ntags: [a, b]\ndescription: |\n  two\n  lines'
    const r = splitFrontmatter(`---\n${yaml}\n---\nbody`)
    expect(Object.fromEntries(r.entries.map((e) => [e.key, e.value]))).toEqual(
      parseFrontmatter(`---\n${yaml}\n---\nbody`).meta,
    )
  })
})

describe('frontmatter blocks that hold no listable fields', () => {
  it('passes the document through unchanged when the block is not a mapping', () => {
    // A bare scalar or a sequence is not frontmatter the strip can list.
    for (const yaml of ['- just\n- a\n- list', 'just a scalar']) {
      const raw = `---\n${yaml}\n---\nbody`
      const r = splitFrontmatter(raw)
      expect(r.hasFrontmatter).toBe(false)
      expect(r.body).toBe(raw)
    }
  })
})

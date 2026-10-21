import { describe, it, expect } from 'vitest'
import { parseDocument } from 'yaml'
import { findFrontmatterBlock, flattenValue, parseFrontmatterMap } from '../components/markdown/frontmatter'

/** Flatten the first (only) value of a one-key YAML block. */
function flatOf(yaml: string): string {
  const map = parseFrontmatterMap(yaml)
  if (!map) throw new Error('not a map')
  return flattenValue(map.items[0].value as never)
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

describe('frontmatter.flattenValue', () => {
  it('renders a scalar verbatim', () => {
    expect(flatOf('k: hello world')).toBe('hello world')
  })

  it('renders an empty scalar as an empty string', () => {
    expect(flatOf('k:')).toBe('')
  })

  it('joins a sequence with ", "', () => {
    expect(flatOf('k: [a, b, c]')).toBe('a, b, c')
  })

  it('flattens a nested map to a single line that keeps every key and value', () => {
    const v = flatOf('k:\n  x: 1\n  y: 2')
    // Compact one-line YAML (flow form) — no newlines, nothing dropped.
    expect(v).not.toContain('\n')
    expect(v).toContain('x')
    expect(v).toContain('1')
    expect(v).toContain('y')
    expect(v).toContain('2')
  })

  it('returns empty string for a null node', () => {
    expect(flattenValue(null)).toBe('')
  })
})

describe('frontmatter.parseFrontmatterMap', () => {
  it('parses a mapping block to a YAMLMap', () => {
    expect(parseFrontmatterMap('a: 1\nb: 2')).not.toBeNull()
  })

  it('returns null for a block that is not a mapping', () => {
    // A bare scalar or a sequence is not frontmatter the strip can list.
    expect(parseFrontmatterMap('- just\n- a\n- list')).toBeNull()
    expect(parseFrontmatterMap('just a scalar')).toBeNull()
    // Confirm: the yaml lib agrees these are not maps.
    expect(parseDocument('- a').contents?.toString()).toBeDefined()
  })
})

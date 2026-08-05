import { describe, it, expect } from 'vitest'
import { fixCodeFences } from '../components/MarkdownRenderer'

describe('fixCodeFences — bare number-dot escaping', () => {
  it('escapes bare number-dot lines', () => {
    expect(fixCodeFences('4.')).toBe('4\\.')
    expect(fixCodeFences('2.')).toBe('2\\.')
    expect(fixCodeFences('42.')).toBe('42\\.')
    expect(fixCodeFences('0.')).toBe('0\\.')
  })

  it('escapes bare number-dot with trailing spaces/tabs', () => {
    expect(fixCodeFences('4.  ')).toBe('4\\.  ')
    expect(fixCodeFences('4.\t')).toBe('4\\.\t')
  })

  it('escapes with 1-3 leading spaces (CommonMark list item range)', () => {
    expect(fixCodeFences(' 4.')).toBe(' 4\\.')
    expect(fixCodeFences('  4.')).toBe('  4\\.')
    expect(fixCodeFences('   4.')).toBe('   4\\.')
  })

  it('does NOT escape with 4+ leading spaces (indented code block)', () => {
    expect(fixCodeFences('    4.')).toBe('    4.')
    expect(fixCodeFences('     4.')).toBe('     4.')
  })

  it('does not escape number-dot followed by text (real list items)', () => {
    expect(fixCodeFences('1. First item')).toBe('1. First item')
    expect(fixCodeFences('2. Second item')).toBe('2. Second item')
    expect(fixCodeFences(' 1. Indented list item')).toBe(' 1. Indented list item')
  })

  it('does not escape numbers without dot', () => {
    expect(fixCodeFences('4')).toBe('4')
  })

  it('does not escape number-dot mid-sentence', () => {
    expect(fixCodeFences('The answer is 4.')).toBe('The answer is 4.')
  })

  it('handles multiline with mixed content', () => {
    const input = 'Result:\n4.\nDone'
    const result = fixCodeFences(input)
    expect(result).toContain('4\\.')
    expect(result).toContain('Result:')
    expect(result).toContain('Done')
  })

  it('preserves blank lines after a bare number-dot line', () => {
    const input = 'Result:\n4.\n\nDone'
    expect(fixCodeFences(input)).toBe('Result:\n4\\.\n\nDone')
  })

  it('preserves CRLF line endings on matched lines', () => {
    expect(fixCodeFences('4.\r\n')).toBe('4\\.\r\n')
  })

  it('does not escape tab-indented lines (tab expands to 4+ spaces per CommonMark tab-stop rule)', () => {
    expect(fixCodeFences('\t4.')).toBe('\t4.')
  })

  it('does not escape bare number-dot inside fenced code blocks', () => {
    const input = '```\n4.\n```'
    expect(fixCodeFences(input)).not.toContain('\\.')
  })

  it('escapes bare number-dot before and after code blocks but not inside', () => {
    const input = '4.\n```\n4.\n```\n4.'
    const result = fixCodeFences(input)
    const lines = result.split('\n')
    expect(lines[0]).toBe('4\\.')
    expect(lines[2]).toBe('4.')
    expect(lines[4]).toBe('4\\.')
  })

  it('does not escape inside tilde-fenced code blocks', () => {
    const input = '~~~\n4.\n~~~'
    expect(fixCodeFences(input)).not.toContain('\\.')
  })

  it('handles nested fences (inner shorter fence is content, not boundary)', () => {
    const input = '````\n```\n4.\n```\n````'
    expect(fixCodeFences(input)).toContain('4.')
    expect(fixCodeFences(input)).not.toContain('4\\.')
  })

  it('does not escape inside indented code fences (0-3 leading spaces)', () => {
    const input = '  ```\n4.\n  ```'
    expect(fixCodeFences(input)).not.toContain('4\\.')
  })

  it('does not escape inside unclosed fence at end of input', () => {
    const input = '```\n4.'
    expect(fixCodeFences(input)).not.toContain('4\\.')
  })

  it('closes fenced code blocks with CRLF line endings', () => {
    const input = '```\r\n4.\r\n```\r\n4.'
    const lines = fixCodeFences(input).split('\r\n')
    expect(lines[1]).toBe('4.')       // inside fence: not escaped
    expect(lines[3]).toBe('4\\.')     // after fence: escaped
  })

  it('does not treat closing fence with info string as fence boundary', () => {
    const input = '```python\nprint("hello")\n```python\n4.\n```'
    expect(fixCodeFences(input)).not.toContain('4\\.')
  })
})

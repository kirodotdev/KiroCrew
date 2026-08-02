import { describe, it, expect } from 'vitest'
import { esc, md, fmtSpeed } from '../api/helpers'

describe('esc', () => {
  it('escapes HTML entities', () => {
    expect(esc('<script>alert("xss")</script>')).toBe('&lt;script&gt;alert("xss")&lt;/script&gt;')
  })
  it('handles null/undefined', () => {
    expect(esc(null)).toBe('')
    expect(esc(undefined)).toBe('')
  })
  it('converts numbers to string', () => {
    expect(esc(42)).toBe('42')
  })
  it('escapes ampersands', () => {
    expect(esc('a & b')).toBe('a &amp; b')
  })
})

describe('md', () => {
  it('renders bold text', () => {
    expect(md('**hello**')).toContain('<strong>hello</strong>')
  })
  it('renders italic text', () => {
    expect(md('*hello*')).toContain('<em>hello</em>')
  })
  it('renders inline code', () => {
    expect(md('use `npm install`')).toContain('<code>npm install</code>')
  })
  it('renders code blocks', () => {
    const result = md('```js\nconsole.log(1)\n```')
    expect(result).toContain('<pre>')
    expect(result).toContain('<code>')
  })
  it('sanitizes dangerous HTML', () => {
    const result = md('<img src=x onerror=alert(1)>')
    // md() escapes HTML first, so the tag becomes &lt;img...&gt; — safe
    expect(result).not.toContain('<img')
  })
})

describe('fmtSpeed', () => {
  // Input is BINARY KiB/s (1 unit = 1024 bytes); the ECMA-402 labels are
  // DECIMAL. 500 KiB/s = 512000 B/s = 512 kB/s.
  it('formats KB/s, converting binary input to the decimal label', () => {
    expect(fmtSpeed(500)).toBe('512kB/s')
  })
  it('formats MB/s', () => {
    expect(fmtSpeed(2048)).toBe('2.1MB/s')
  })
  it('rounds KB/s', () => {
    expect(fmtSpeed(99.7)).toBe('102kB/s')
  })
  it('crosses to MB at one decimal megabyte, not at 1000 KiB', () => {
    expect(fmtSpeed(976)).toBe('999kB/s')
    expect(fmtSpeed(977)).toBe('1.0MB/s')
  })
})

import { parseTags } from '../pages/overview/VectorMemoryCard'

describe('parseTags', () => {
  it('parses a JSON array string', () => {
    expect(parseTags('["a","b"]')).toEqual(['a', 'b'])
  })
  it('wraps a plain non-JSON string as single-element array', () => {
    expect(parseTags('some-tag')).toEqual(['some-tag'])
  })
  it('wraps a JSON-encoded bare string as single-element array', () => {
    expect(parseTags('"some-tag"')).toEqual(['some-tag'])
  })
  it('passes through a native array', () => {
    expect(parseTags(['x', 'y'])).toEqual(['x', 'y'])
  })
  it('returns [] for null/undefined', () => {
    expect(parseTags(null)).toEqual([])
    expect(parseTags(undefined)).toEqual([])
  })
  it('returns [] for non-string non-array values', () => {
    expect(parseTags(42)).toEqual([])
    expect(parseTags({})).toEqual([])
  })
  it('returns [] for empty string', () => {
    expect(parseTags('')).toEqual([])
  })
})

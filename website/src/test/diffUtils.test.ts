import { describe, it, expect } from 'vitest'
import { isDiffText } from '../utils/diffUtils'


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

import { describe, it, expect } from 'vitest'
import { baseCommandLabel, trustBasePattern, truncateCommandLabel } from './trustPatterns'

// These assertions pin the EXACT strings, not just the shape. The pattern decides
// how much a trust grant widens, so a change that quietly broadens it (dropping
// the space before `*`, trusting a whole family where one command was meant)
// must fail here rather than ship.

describe('trustBasePattern — the grant-widening pattern', () => {
  it('trusts a single base command with any arguments', () => {
    expect(trustBasePattern('cat')).toBe('cat *')
  })

  it('trusts every base of a piped/chained command, not just the first', () => {
    expect(trustBasePattern('cat,wc')).toBe('cat *,wc *')
  })

  it('trims whitespace the gateway may leave around each base', () => {
    expect(trustBasePattern('cat, wc ,head')).toBe('cat *,wc *,head *')
  })

  it('separates bases with a bare comma — no space, which would not match', () => {
    expect(trustBasePattern('cat,wc')).not.toContain(', ')
  })

  it('keeps the trailing " *" that scopes the grant to that binary', () => {
    // 'npm*' (no space) would also match 'npmfoo'; 'npm' alone would match
    // nothing with args. The space matters.
    expect(trustBasePattern('npm')).toBe('npm *')
  })

  it('does not widen an empty base into a bare wildcard', () => {
    // A grant of '*' would trust everything. Empty in, empty-ish out.
    expect(trustBasePattern('')).toBe(' *')
    expect(trustBasePattern('')).not.toBe('*')
  })
})

describe('baseCommandLabel — display only', () => {
  it('leaves a single base unchanged', () => {
    expect(baseCommandLabel('cat')).toBe('cat')
  })

  it('spaces out a multi-base list for reading', () => {
    expect(baseCommandLabel('cat,wc')).toBe('cat, wc')
  })

  it('is never usable as a pattern (differs from trustBasePattern)', () => {
    expect(baseCommandLabel('cat,wc')).not.toBe(trustBasePattern('cat,wc'))
  })
})

describe('truncateCommandLabel — label only, never the pattern', () => {
  it('leaves a short command untouched', () => {
    expect(truncateCommandLabel('ls /tmp')).toBe('ls /tmp')
  })

  it('leaves a command of exactly the max length untouched', () => {
    const exactly30 = 'a'.repeat(30)
    expect(truncateCommandLabel(exactly30)).toBe(exactly30)
  })

  it('truncates one character past the max and marks it with an ellipsis', () => {
    const long = 'a'.repeat(31)
    expect(truncateCommandLabel(long)).toBe('a'.repeat(30) + '…')
  })

  it('honours a custom max', () => {
    expect(truncateCommandLabel('abcdefghij', 4)).toBe('abcd…')
  })

  it('shortens for display without altering what would be granted', () => {
    // The caller passes the untruncated command as the trust_command pattern;
    // this helper only feeds the button label.
    const long = 'find /very/long/path -name "*.tsx" -exec grep -l something'
    const label = truncateCommandLabel(long)
    expect(label).not.toBe(long)
    expect(label.endsWith('…')).toBe(true)
    expect(long.startsWith(label.slice(0, -1))).toBe(true)
  })
})

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
    const exactly64 = 'a'.repeat(64)
    expect(truncateCommandLabel(exactly64)).toBe(exactly64)
  })

  it('elides the middle and never exceeds the budget', () => {
    const long = 'a'.repeat(65) + 'TAIL'
    const label = truncateCommandLabel(long)
    expect(label.length).toBe(64)
    expect(label).toContain('…')
    // The tail survives -- that is the whole point.
    expect(label.endsWith('TAIL')).toBe(true)
    expect(label.startsWith('a')).toBe(true)
  })

  it('keeps two commands distinguishable when they share a prefix LONGER than the budget', () => {
    // The defect head-truncation leaves behind: a longer owner/repo pushes the
    // distinguishing filename past any fixed head budget, so the two collide
    // again. Middle-ellipsis keeps the tail, where they actually differ.
    const base = 'gh api repos/rapid7-security-platform-team/ai-vault-service/contents/'
    const config = `${base}config.json --jq .sha`
    const secrets = `${base}secrets.json --jq .sha`
    expect(base.length).toBeGreaterThan(64)
    expect(truncateCommandLabel(config)).not.toBe(truncateCommandLabel(secrets))
    // ...and pin that HEAD truncation is what collided, so reverting to it fails.
    expect(config.slice(0, 64)).toBe(secrets.slice(0, 64))
  })

  it('does not collide two commands that share a long prefix', () => {
    // The label is the only thing the user reads before granting an exact-string
    // match, so two different commands must not render as the same string.
    const config = 'gh api repos/owner/some-repository/contents/config.json --jq .sha'
    const secrets = 'gh api repos/owner/some-repository/contents/secrets.json --jq .sha'
    expect(truncateCommandLabel(config)).not.toBe(truncateCommandLabel(secrets))
    // ...and pin that the OLD budget is what made them collide, so this test
    // fails if the budget is narrowed back.
    expect(truncateCommandLabel(config, 30)).toBe(truncateCommandLabel(secrets, 30))
  })

  it('degrades gracefully at a tiny budget, still never exceeding it', () => {
    // No production caller passes `max`, so there is no small-budget special
    // case: middle-ellipsis handles it without a second branch.
    expect(truncateCommandLabel('abcdefghij', 4)).toBe('ab…j')
    expect(truncateCommandLabel('abcdefghij', 4).length).toBe(4)
    expect(truncateCommandLabel('abcdefghij', 2).length).toBeLessThanOrEqual(2)
  })

  it('never splits an astral character into a lone surrogate', () => {
    // `slice` counts UTF-16 code units, so a cut through an emoji or a CJK
    // extension-B ideograph would emit half a code point and the label -- which a
    // user reads to make a security decision -- renders as mojibake.
    const lone = (s: string) =>
      [...s].some(ch => {
        const c = ch.charCodeAt(0)
        return c >= 0xd800 && c <= 0xdfff && ch.length === 1
      })

    // Sweep the emoji across the WHOLE string so it straddles the head cut and
    // the tail cut in turn -- a sweep that only crosses one of them leaves the
    // other snap untested (measured: it did).
    const total = 90
    for (let i = 0; i <= total; i++) {
      const cmd = `gh ${'a'.repeat(i)}😀${'b'.repeat(total - i)}`
      const label = truncateCommandLabel(cmd)
      expect(lone(label), `emoji at ${i} produced a lone surrogate: ${label}`).toBe(false)
      expect(label.length).toBeLessThanOrEqual(64)
    }
    // ...and at a small budget, where head and tail are only a few chars each.
    for (let pad = 0; pad < 12; pad++) {
      const label = truncateCommandLabel(`${'a'.repeat(pad)}😀${'b'.repeat(30)}`, 8)
      expect(lone(label), `small-budget pad=${pad}: ${label}`).toBe(false)
    }
  })

  it('shortens for display without altering what would be granted', () => {
    // The caller passes the untruncated command as the trust_command pattern;
    // this helper only feeds the button label.
    const long = 'find /very/long/path -name "*.tsx" -exec grep -l something-quite-long {} +'
    const label = truncateCommandLabel(long)
    expect(label).not.toBe(long)
    expect(label).toContain('…')
    expect(label.length).toBeLessThan(long.length)
  })
})

import { describe, it, expect } from 'vitest'
import {
  MAX_ACCESSIBILITY_CHARS,
  MAX_CLIPBOARD_CHARS,
  MAX_SUMMARY_CHARS,
  isSensitiveContent,
  shouldPromptForClipboard,
  truncateAccessibilityContext,
  truncateSummary,
  unionBounds,
  validateCaptureRegion,
} from './utils'

describe('truncateAccessibilityContext', () => {
  it('returns short context unchanged', () => {
    expect(truncateAccessibilityContext('hello')).toBe('hello')
  })

  it('cuts context at exactly the accessibility limit', () => {
    const long = 'a'.repeat(MAX_ACCESSIBILITY_CHARS + 50)
    const out = truncateAccessibilityContext(long)
    expect(out).toHaveLength(MAX_ACCESSIBILITY_CHARS)
    expect(out).toBe(long.slice(0, MAX_ACCESSIBILITY_CHARS))
  })

  it('keeps context of exactly the limit whole', () => {
    const exact = 'b'.repeat(MAX_ACCESSIBILITY_CHARS)
    expect(truncateAccessibilityContext(exact)).toBe(exact)
  })
})

describe('isSensitiveContent', () => {
  it('flags a long single-line token-shaped string', () => {
    expect(isSensitiveContent('abcdefghij0123456789KLMN')).toBe(true)
  })

  it('does not flag a short token-shaped string', () => {
    expect(isSensitiveContent('abc123')).toBe(false)
  })

  it('does not flag token-shaped text that contains spaces', () => {
    expect(isSensitiveContent('this is a normal sentence of words')).toBe(false)
  })

  it('does not treat a token-shaped string longer than 256 characters as a single token', () => {
    const long = 'Z'.repeat(300)
    expect(isSensitiveContent(long)).toBe(false)
  })

  it('flags an AWS access key id', () => {
    expect(isSensitiveContent('AKIA' + 'ABCDEFGHIJKLMNOP')).toBe(true)
  })

  it('flags an OpenAI style secret key', () => {
    expect(isSensitiveContent('sk-' + 'x'.repeat(24))).toBe(true)
  })

  it('flags a GitHub personal access token', () => {
    expect(isSensitiveContent('ghp_' + 'Q'.repeat(36))).toBe(true)
  })

  it('flags a Slack bot token', () => {
    expect(isSensitiveContent('xoxb' + '-123-456-abc')).toBe(true)
  })

  it('flags a bearer token case-insensitively', () => {
    expect(isSensitiveContent('bearer abc.def-ghi')).toBe(true)
  })

  it('flags an inline password assignment', () => {
    expect(isSensitiveContent('password = hunter2')).toBe(true)
  })

  it('flags an inline secret assignment', () => {
    expect(isSensitiveContent('secret: swordfish')).toBe(true)
  })

  it('flags a PEM private key header', () => {
    expect(isSensitiveContent('-----BEGIN ' + 'RSA PRIVATE KEY-----')).toBe(true)
  })

  it('does not flag ordinary prose', () => {
    expect(isSensitiveContent('Please review the meeting notes')).toBe(false)
  })
})

describe('shouldPromptForClipboard', () => {
  it('prompts for ordinary clipboard text', () => {
    expect(shouldPromptForClipboard('Ship the fix before lunch')).toBe(true)
  })

  it('ignores empty clipboard content', () => {
    expect(shouldPromptForClipboard('')).toBe(false)
  })

  it('ignores content longer than the clipboard limit', () => {
    expect(shouldPromptForClipboard('x '.repeat(MAX_CLIPBOARD_CHARS))).toBe(false)
  })

  it('ignores clipboard content that looks sensitive', () => {
    expect(shouldPromptForClipboard('AKIA' + 'ABCDEFGHIJKLMNOP')).toBe(false)
  })
})

describe('truncateSummary', () => {
  it('returns a short summary unchanged', () => {
    expect(truncateSummary('Build finished')).toBe('Build finished')
  })

  it('cuts a long summary to the summary limit', () => {
    const out = truncateSummary('s'.repeat(MAX_SUMMARY_CHARS + 20))
    expect(out).toHaveLength(MAX_SUMMARY_CHARS)
  })

  it('counts each CJK character as one unit when cutting', () => {
    const cjk = '界'.repeat(MAX_SUMMARY_CHARS + 5)
    const out = truncateSummary(cjk)
    expect([...out]).toHaveLength(MAX_SUMMARY_CHARS)
    expect(out).toBe('界'.repeat(MAX_SUMMARY_CHARS))
  })
})

describe('unionBounds', () => {
  it('returns an empty rectangle for no screens', () => {
    expect(unionBounds([])).toEqual({ x: 0, y: 0, width: 0, height: 0 })
  })

  it('returns the single screen rectangle at the origin by default', () => {
    expect(unionBounds([{ width: 1920, height: 1080 }])).toEqual({
      x: 0,
      y: 0,
      width: 1920,
      height: 1080,
    })
  })

  it('covers a left-side monitor with negative coordinates', () => {
    const union = unionBounds([
      { x: 0, y: 0, width: 1920, height: 1080 },
      { x: -1280, y: 0, width: 1280, height: 1024 },
    ])
    expect(union).toEqual({ x: -1280, y: 0, width: 3200, height: 1080 })
  })

  it('covers a monitor stacked above the primary one', () => {
    const union = unionBounds([
      { x: 0, y: 0, width: 1000, height: 800 },
      { x: 0, y: -600, width: 1000, height: 600 },
    ])
    expect(union).toEqual({ x: 0, y: -600, width: 1000, height: 1400 })
  })
})

describe('validateCaptureRegion', () => {
  const screen = { x: 0, y: 0, width: 1920, height: 1080 }

  it('accepts a region fully inside the screen', () => {
    expect(validateCaptureRegion({ x: 10, y: 10, width: 100, height: 100 }, screen)).toEqual({ valid: true })
  })

  it('accepts a region that touches the far edges exactly', () => {
    expect(validateCaptureRegion({ x: 1820, y: 980, width: 100, height: 100 }, screen)).toEqual({ valid: true })
  })

  it('rejects zero or negative dimensions with the size in the reason', () => {
    const result = validateCaptureRegion({ x: 0, y: 0, width: 0, height: 50 }, screen)
    expect(result).toEqual({ valid: false, reason: 'Non-positive dimensions: 0×50' })
  })

  it('rejects a region left of the leftmost edge', () => {
    const result = validateCaptureRegion({ x: -5, y: 0, width: 10, height: 10 }, screen)
    expect(result).toEqual({ valid: false, reason: 'x=-5 is left of leftmost screen edge (0)' })
  })

  it('rejects a region above the topmost edge', () => {
    const result = validateCaptureRegion({ x: 0, y: -5, width: 10, height: 10 }, screen)
    expect(result).toEqual({ valid: false, reason: 'y=-5 is above topmost screen edge (0)' })
  })

  it('rejects a region whose right edge passes the union width', () => {
    const result = validateCaptureRegion({ x: 1900, y: 0, width: 100, height: 10 }, screen)
    expect(result).toEqual({
      valid: false,
      reason: 'Region right edge 2000 exceeds union width 1920',
    })
  })

  it('rejects a region whose bottom edge passes the union height', () => {
    const result = validateCaptureRegion({ x: 0, y: 1000, width: 10, height: 100 }, screen)
    expect(result).toEqual({
      valid: false,
      reason: 'Region bottom edge 1100 exceeds union height 1080',
    })
  })

  it('accepts a region on a secondary monitor with negative origin', () => {
    const screens = [
      { x: 0, y: 0, width: 1920, height: 1080 },
      { x: -1280, y: 0, width: 1280, height: 1024 },
    ]
    expect(validateCaptureRegion({ x: -1200, y: 100, width: 300, height: 200 }, screens)).toEqual({ valid: true })
  })

  it('validates against a list of screens, not just one', () => {
    const screens = [{ x: 0, y: 0, width: 800, height: 600 }, { x: 800, y: 0, width: 800, height: 600 }]
    expect(validateCaptureRegion({ x: 700, y: 0, width: 200, height: 100 }, screens)).toEqual({ valid: true })
  })
})

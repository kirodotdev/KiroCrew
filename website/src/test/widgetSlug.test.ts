import { describe, it, expect } from 'vitest'
import { deriveWidgetSlug, effectiveWidgetSlug } from '../lib/widgetSlug'

describe('deriveWidgetSlug', () => {
  it('produces a 16-hex-char string', () => {
    const slug = deriveWidgetSlug('1779995123.456789', 0)
    expect(slug).toMatch(/^[0-9a-f]{16}$/)
  })

  it('is deterministic — same inputs produce the same slug', () => {
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995123.456789', 0)
    expect(a).toBe(b)
  })

  it('is sensitive to the widget index — same message, different widget = different slug', () => {
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995123.456789', 1)
    expect(a).not.toBe(b)
  })

  it('is sensitive to the message ts — different message = different slug', () => {
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995124.000000', 0)
    expect(a).not.toBe(b)
  })

  it('matches the artifact-store slug regex', () => {
    // Slug regex from artifacts.py: ^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$
    const re = /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/
    for (const ts of ['1', '1779995123.456789', '0', '9999999999.999999']) {
      for (const idx of [0, 1, 99]) {
        expect(deriveWidgetSlug(ts, idx)).toMatch(re)
      }
    }
  })

  it('handles unicode in the message ts deterministically', () => {
    // The hash function operates on charCodes; non-ASCII shouldn't crash.
    const slug = deriveWidgetSlug('emoji-🎉-ts', 0)
    expect(slug).toMatch(/^[0-9a-f]{16}$/)
  })

  it('has reasonable avalanche behavior — flipping one input bit changes many output bits', () => {
    // Regression guard against using the 64-bit FNV prime with Math.imul,
    // which silently truncates to multiply-by-435 and produces extremely
    // poor avalanche (code review caught this on rev 1 of). With
    // the 32-bit prime + different offset bases, near-identical seeds
    // should diverge across most output bits.
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995123.456789', 1)
    let diffBits = 0
    for (let i = 0; i < 16; i++) {
      const ai = parseInt(a[i], 16)
      const bi = parseInt(b[i], 16)
      // Count the differing bits in this nibble.
      let xor = ai ^ bi
      while (xor) {
        diffBits += xor & 1
        xor >>>= 1
      }
    }
    // 64-bit hash, ideal avalanche flips ~32 bits on a 1-bit input change.
    // Demand at least 16 — comfortably above the ~3 bits the broken
    // multiply-by-435 hash produced for adjacent indices.
    expect(diffBits).toBeGreaterThanOrEqual(16)
  })
})

describe('effectiveWidgetSlug', () => {
  it('prefers an explicit slug over derived', () => {
    const result = effectiveWidgetSlug({
      explicitSlug: 'cr-queue',
      messageTs: '1779995123.456789',
      widgetIndex: 0,
    })
    expect(result).toBe('cr-queue')
  })

  it('derives from messageTs + widgetIndex when no explicit slug', () => {
    const result = effectiveWidgetSlug({
      messageTs: '1779995123.456789',
      widgetIndex: 0,
    })
    expect(result).toMatch(/^[0-9a-f]{16}$/)
    // Should match the direct call.
    expect(result).toBe(deriveWidgetSlug('1779995123.456789', 0))
  })

  it('returns null when neither explicit slug nor message context is available', () => {
    expect(effectiveWidgetSlug({})).toBeNull()
    expect(effectiveWidgetSlug({ messageTs: '1779995123.456789' })).toBeNull()
    expect(effectiveWidgetSlug({ widgetIndex: 0 })).toBeNull()
  })

  it('treats empty-string explicit slug as no slug — falls back to derived', () => {
    const result = effectiveWidgetSlug({
      explicitSlug: '',
      messageTs: '1779995123.456789',
      widgetIndex: 0,
    })
    expect(result).toMatch(/^[0-9a-f]{16}$/)
  })
})

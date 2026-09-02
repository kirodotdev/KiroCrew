import { describe, it, expect } from 'vitest'
import { sanitizeCssValue, CSS_VALUE_MAX_LEN } from '../lib/cssSanitize'

describe('sanitizeCssValue', () => {
  describe('accepts legitimate CSS values', () => {
    it.each([
      ['hex color', '#0b1220'],
      ['shorthand hex', '#fff'],
      ['rgb()', 'rgb(240, 240, 240)'],
      ['rgba() with alpha', 'rgba(0, 0, 0, 0.5)'],
      ['hsl()', 'hsl(210, 50%, 50%)'],
      ['oklch() modern color syntax', 'oklch(0.7 0.2 250)'],
      ['color-mix slash syntax', 'rgb(255 0 0 / 50%)'],
      ['calc with percentage', 'calc(100% / 2)'],
      ['named color', 'tomato'],
      ['length with unit', '16px'],
      ['percentage', '42%'],
      ['empty -> rejected', ''],
    ])('%s', (_label, input) => {
      const out = sanitizeCssValue(input)
      // Empty input sanitizes to empty; everything else round-trips trimmed.
      expect(out).toBe(input.trim() === '' ? '' : input.trim())
    })
  })

  describe('rejects structural CSS injection', () => {
    it.each([
      ['semicolon (second declaration)', 'red; background:black'],
      ['close-brace (rule escape)', 'red} body { display:none'],
      ['open-brace', '{ color: red'],
      ['colon', 'red:override'],
      ['at-rule', '@media (print)'],
      ['quote', '"; }body{display:none}'],
      ['single quote', "red' onload='alert(1)"],
      ['backslash escape', '\\75 rl(x)'],
      ['angle bracket', 'red</style>'],
      ['ampersand', 'red&lt;'],
      ['bang', 'red !important'],
      ['star (comment)', 'red /* bad */'],
    ])('%s', (_label, input) => {
      expect(sanitizeCssValue(input)).toBe('')
    })
  })

  describe('rejects dangerous CSS functions', () => {
    it.each([
      ['url()', 'url(http://evil.example)'],
      ['URL() uppercase', 'URL(http://evil.example)'],
      ['url  (  ) with whitespace', 'url  (http://evil.example)'],
      ['expression() legacy IE XSS', 'expression(alert(1))'],
      ['image()', 'image(http://evil.example)'],
      ['image-set()', '-webkit-image-set(url(x) 1x)'],
      ['paint() Houdini worklet', 'paint(myWorklet)'],
      ['element() DOM reference', 'element(#hero)'],
      ['url() smuggled via var() fallback', 'var(--foo, url(http://evil.example))'],
    ])('%s', (_label, input) => {
      expect(sanitizeCssValue(input)).toBe('')
    })
  })

  describe('rejects non-string / oversized inputs', () => {
    it('rejects undefined', () => {
      expect(sanitizeCssValue(undefined)).toBe('')
    })
    it('rejects null', () => {
      expect(sanitizeCssValue(null)).toBe('')
    })
    it('rejects number', () => {
      expect(sanitizeCssValue(42)).toBe('')
    })
    it('rejects object', () => {
      expect(sanitizeCssValue({})).toBe('')
    })
    it('rejects whitespace-only', () => {
      expect(sanitizeCssValue('   ')).toBe('')
    })
    it(`rejects strings longer than ${CSS_VALUE_MAX_LEN} chars`, () => {
      const huge = 'a'.repeat(CSS_VALUE_MAX_LEN + 1)
      expect(sanitizeCssValue(huge)).toBe('')
    })
    it(`accepts strings at exactly ${CSS_VALUE_MAX_LEN} chars`, () => {
      const just = 'a'.repeat(CSS_VALUE_MAX_LEN)
      expect(sanitizeCssValue(just)).toBe(just)
    })
  })

  describe('trims outer whitespace', () => {
    it('trims leading/trailing spaces', () => {
      expect(sanitizeCssValue('  #0b1220  ')).toBe('#0b1220')
    })
  })
})

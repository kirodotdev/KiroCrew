/**
 * contrast.ts — the panel's runtime WCAG math.
 *
 * Previously untested. Expectations derive from the WCAG formulas the module
 * implements and from the real values its own header documents (the kiro-dark
 * accent purple measuring 3.57:1 on the card — below AA — is the reason this
 * module exists).
 */
import { describe, it, expect } from 'vitest'

import {
  AA_TEXT,
  compositeOver,
  contrastRatio,
  luminance,
  parseCssColor,
  pickReadable,
} from '../apps/crew-companion/contrast'

const WHITE = { r: 255, g: 255, b: 255, a: 1 }
const BLACK = { r: 0, g: 0, b: 0, a: 1 }

describe('parseCssColor', () => {
  it('parses 6- and 3-digit hex', () => {
    expect(parseCssColor('#8E48FF')).toEqual({ r: 142, g: 72, b: 255, a: 1 })
    expect(parseCssColor('#fff')).toEqual(WHITE)
  })

  it('parses the forms getComputedStyle returns', () => {
    expect(parseCssColor('rgb(10, 20, 30)')).toEqual({ r: 10, g: 20, b: 30, a: 1 })
    expect(parseCssColor('rgba(142, 72, 255, 0.3)')).toEqual({ r: 142, g: 72, b: 255, a: 0.3 })
    // Space-separated modern syntax.
    expect(parseCssColor('rgb(10 20 30 / 0.5)')).toEqual({ r: 10, g: 20, b: 30, a: 0.5 })
  })

  it('returns null for named colours and garbage — callers must fall back', () => {
    expect(parseCssColor('rebeccapurple')).toBeNull()
    expect(parseCssColor('')).toBeNull()
    expect(parseCssColor('rgb(a,b,c)')).toBeNull()
  })
})

describe('compositeOver', () => {
  it('flattens a translucent tint onto its background', () => {
    // 30% white over black -> 76.5 grey on every channel.
    const out = compositeOver({ ...WHITE, a: 0.3 }, BLACK)
    expect(out.r).toBeCloseTo(76.5)
    expect(out.a).toBe(1)
  })

  it('an opaque colour is unchanged', () => {
    expect(compositeOver(BLACK, WHITE)).toEqual(BLACK)
  })
})

describe('luminance and contrastRatio', () => {
  it('white and black anchor the WCAG scale', () => {
    expect(luminance(WHITE)).toBeCloseTo(1)
    expect(luminance(BLACK)).toBeCloseTo(0)
    expect(contrastRatio(WHITE, BLACK)).toBeCloseTo(21)
    expect(contrastRatio(BLACK, BLACK)).toBeCloseTo(1)
  })

  it('ratio is symmetric in fg/bg for opaque colours', () => {
    const purple = parseCssColor('#8E48FF')!
    expect(contrastRatio(purple, WHITE)).toBeCloseTo(contrastRatio(WHITE, purple))
  })

  it('translucent foregrounds are flattened before measuring', () => {
    // A fully transparent fg over white IS white: ratio 1, not black's 21.
    expect(contrastRatio({ ...BLACK, a: 0 }, WHITE)).toBeCloseTo(1)
  })
})

describe('pickReadable', () => {
  it('keeps the preferred colour when it clears AA', () => {
    expect(pickReadable('#000000', '#ffffff', '#333333')).toBe('#000000')
  })

  it('falls back when the theme accent misses AA — the documented kiro-dark case', () => {
    // The module header measures accent purple at 3.57:1 on the dark card,
    // below AA_TEXT (4.5) — exactly the case the panel must not ship as text.
    const picked = pickReadable('#8E48FF', '#1e1b26', '#e8e6ef')
    expect(picked).toBe('#e8e6ef')
  })

  it('treats unparseable input as unsafe', () => {
    expect(pickReadable('var(--accent)', '#ffffff', '#111111')).toBe('#111111')
    expect(AA_TEXT).toBe(4.5)
  })
})

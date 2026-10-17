import { describe, it, expect } from 'vitest'
import {
  generatePalette, generateContrastRamp, generateHarmony,
  parseColor, computePaletteBoost, resolveDefaultColor, colorName,
  PALETTE_NAMES, PALETTE_SIZE, INTENSITY_NAMES, INTENSITY_THRESHOLDS,
} from '../utils/sessionColors'

const SEED = '#10b981'
const DARK_BG = '#12141a'
const LIGHT_BG = '#fafafa'
const HEX_RE = /^#[0-9a-f]{6}$/

describe('generatePalette', () => {
  it('returns 7 hex colors for each strategy', () => {
    for (const p of PALETTE_NAMES) {
      const colors = generatePalette(SEED, p, DARK_BG)
      expect(colors).toHaveLength(PALETTE_SIZE)
      colors.forEach(c => expect(c).toMatch(HEX_RE))
    }
  })

  it('returns 7 colors on light backgrounds', () => {
    for (const p of PALETTE_NAMES) {
      expect(generatePalette(SEED, p, LIGHT_BG)).toHaveLength(PALETTE_SIZE)
    }
  })

  it('produces distinct colors (no duplicates)', () => {
    for (const p of PALETTE_NAMES) {
      expect(new Set(generatePalette(SEED, p, DARK_BG)).size).toBe(PALETTE_SIZE)
    }
  })

  it('handles zero-chroma (grey) seed', () => {
    const colors = generatePalette('#808080', 'horizon', DARK_BG)
    expect(colors).toHaveLength(PALETTE_SIZE)
    expect(new Set(colors).size).toBeGreaterThan(1)
  })

  it('returns empty array for invalid input', () => {
    expect(generatePalette('', 'horizon')).toEqual([])
    expect(generatePalette('not-a-color', 'horizon')).toEqual([])
  })

  it('works without bgHex (uses default)', () => {
    expect(generatePalette(SEED, 'horizon')).toHaveLength(PALETTE_SIZE)
  })
})

describe('generateContrastRamp', () => {
  it('trailhead has less hue variation than horizon', () => {
    const trail = generateContrastRamp(SEED, DARK_BG, 40).map(c => parseColor(c)!)
    const horiz = generateContrastRamp(SEED, DARK_BG, 90).map(c => parseColor(c)!)
    const spread = (colors: [number, number, number][]) =>
      colors.reduce((sum, c, i) => i === 0 ? 0 : sum + Math.abs(c[0] - colors[i - 1][0]) + Math.abs(c[1] - colors[i - 1][1]) + Math.abs(c[2] - colors[i - 1][2]), 0)
    expect(spread(horiz)).toBeGreaterThan(spread(trail))
  })
})

describe('generateHarmony', () => {
  it('gamut clamps without crashing on saturated seeds', () => {
    const colors = generateHarmony('#ff0000', DARK_BG, false)
    expect(colors).toHaveLength(PALETTE_SIZE)
    colors.forEach(c => expect(c).toMatch(HEX_RE))
  })

  it('odyssey produces valid output', () => {
    expect(generateHarmony(SEED, DARK_BG, true)).toHaveLength(PALETTE_SIZE)
  })
})

describe('computePaletteBoost', () => {
  const colors = generatePalette(SEED, 'horizon', DARK_BG)

  it('returns arrays of length 7', () => {
    const boost = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true)
    expect(boost.idlePct).toHaveLength(PALETTE_SIZE)
    expect(boost.activePct).toHaveLength(PALETTE_SIZE)
    expect(boost.hoverPct).toHaveLength(PALETTE_SIZE)
    expect(boost.mutedColors).toHaveLength(PALETTE_SIZE)
  })

  it('idle <= hover <= active', () => {
    const boost = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true)
    for (let i = 0; i < PALETTE_SIZE; i++) {
      expect(boost.idlePct[i]).toBeLessThanOrEqual(boost.hoverPct[i])
      expect(boost.hoverPct[i]).toBeLessThanOrEqual(boost.activePct[i])
    }
  })

  it('vivid needs higher opacity than soft', () => {
    const soft = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'soft')
    const vivid = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'vivid')
    const avg = (a: number[]) => a.reduce((s, v) => s + v, 0) / a.length
    expect(avg(vivid.idlePct)).toBeGreaterThanOrEqual(avg(soft.idlePct))
  })

  it('caps idle at 80 and preserves +7/+20 spacing', () => {
    const boost = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'vivid')
    boost.idlePct.forEach(p => expect(p).toBeLessThanOrEqual(80))
    boost.hoverPct.forEach((p, i) => expect(p).toBe(boost.idlePct[i] + 7))
    boost.activePct.forEach((p, i) => expect(p).toBe(boost.idlePct[i] + 20))
  })

  it('handles invalid colors gracefully', () => {
    const boost = computePaletteBoost(['invalid'], '#000', '#888', '#fff', true)
    expect(boost.idlePct).toHaveLength(1)
  })
})

describe('parseColor', () => {
  it('parses 6-digit hex', () => { expect(parseColor('#ff0000')).toEqual([255, 0, 0]) })
  it('parses 3-digit hex', () => { expect(parseColor('#f00')).toEqual([255, 0, 0]) })
  it('parses rgba', () => { expect(parseColor('rgba(10, 20, 30, 0.5)')).toEqual([10, 20, 30]) })
  it('returns null for invalid', () => { expect(parseColor('')).toBeNull() })
})

describe('colorName', () => {
  it('names primary hues', () => {
    expect(colorName('#ff0000')).toBe('Red')
    expect(colorName('#00ff00')).toBe('Green')
    expect(colorName('#0000ff')).toBe('Blue')
    expect(colorName('#ffd700')).toBe('Yellow')
  })
  it('names neutrals by lightness', () => {
    expect(colorName('#ffffff')).toBe('White')
    expect(colorName('#000000')).toBe('Black')
    expect(colorName('#808080')).toBe('Gray')
  })
  it('qualifies very light / very dark hues', () => {
    expect(colorName('#220000')).toBe('Dark red')
    expect(colorName('#ffe0e0')).toBe('Light red')
  })
  it('treats near-white / near-black as neutral despite a 1-bit channel delta', () => {
    // HSL saturation is singular near l=0/l=1; a chroma floor avoids false hue labels.
    expect(colorName('#fefefd')).toBe('White')
    expect(colorName('#010100')).toBe('Black')
    expect(colorName('#fffbff')).toBe('White')
  })
  it('falls back to the raw string for unparseable input', () => {
    expect(colorName('not-a-color')).toBe('not-a-color')
  })
})

describe('resolveDefaultColor', () => {
  it('returns null for null setting', () => { expect(resolveDefaultColor(null, 5)).toBeNull() })
  it('returns slot count mod 7 for auto', () => {
    expect(resolveDefaultColor('auto', 0)).toBe(0)
    expect(resolveDefaultColor('auto', 7)).toBe(0)
    expect(resolveDefaultColor('auto', 3)).toBe(3)
  })
  it('returns the specific index for numeric setting', () => {
    expect(resolveDefaultColor(2, 10)).toBe(2)
    expect(resolveDefaultColor(0, 0)).toBe(0)
  })
})

describe('intense intensity', () => {
  const colors = generatePalette(SEED, 'horizon', DARK_BG)

  it('produces higher idle opacity than vivid', () => {
    const vivid = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'vivid')
    const intense = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'intense')
    const avg = (a: number[]) => a.reduce((s, v) => s + v, 0) / a.length
    expect(avg(intense.idlePct)).toBeGreaterThan(avg(vivid.idlePct))
  })

  it('maintains idle <= hover <= active ordering', () => {
    const intense = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'intense')
    for (let i = 0; i < PALETTE_SIZE; i++) {
      expect(intense.idlePct[i]).toBeLessThanOrEqual(intense.hoverPct[i])
      expect(intense.hoverPct[i]).toBeLessThanOrEqual(intense.activePct[i])
    }
  })

  it('respects idle cap of 80 and preserves spacing', () => {
    const intense = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'intense')
    intense.idlePct.forEach(p => expect(p).toBeLessThanOrEqual(80))
    intense.hoverPct.forEach((p, i) => expect(p).toBe(intense.idlePct[i] + 7))
    intense.activePct.forEach((p, i) => expect(p).toBe(intense.idlePct[i] + 20))
  })
})

describe('mutedColors format', () => {
  const colors = generatePalette(SEED, 'horizon', DARK_BG)
  const boost = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true)

  it('returns valid hex for each color', () => {
    boost.mutedColors.forEach(c => {
      expect(c).toMatch(HEX_RE)
    })
  })
})

describe('text-strong fallback', () => {
  it('shifts toward textStrong when text cannot achieve target Lc', () => {
    // Solarized dark: muted and text are close, textStrong is far
    const colors = generatePalette('rgba(42,161,152,.15)', 'odyssey', '#073642')
    const boostWithout = computePaletteBoost(colors, '#073642', '#879da5', '#93a1a1', true, 'intense')
    const boostWith = computePaletteBoost(colors, '#073642', '#879da5', '#93a1a1', true, 'intense', '#eee8d5')
    // With textStrong, boosted colors should shift further than text alone
    const withoutAll93 = boostWithout.mutedColors.every(c => c === '#93a1a1')
    const withHasShifted = boostWith.mutedColors.some(c => c !== '#93a1a1' && c !== '#879da5')
    expect(withoutAll93).toBe(true)
    expect(withHasShifted).toBe(true)
  })

  it('prefers text over textStrong when text is sufficient', () => {
    // Emerald dark: text (#e4e4e7) has plenty of contrast
    const colors = generatePalette('#10b981', 'horizon', DARK_BG)
    const boost = computePaletteBoost(colors, DARK_BG, '#71717a', '#e4e4e7', true, 'intense', '#fafafa')
    // Should not shift all the way to textStrong
    boost.mutedColors.forEach(c => { expect(c).not.toBe('#fafafa') })
  })
})

describe('constants', () => {
  it('has 4 palette names', () => { expect(PALETTE_NAMES).toEqual(['trailhead', 'horizon', 'voyage', 'odyssey']) })
  it('has 5 intensity names', () => { expect(INTENSITY_NAMES).toHaveLength(5) })
  it('does not include removed palettes', () => {
    for (const old of ['wide', 'narrow', 'tight', 'radix', 'tailwind']) expect(PALETTE_NAMES).not.toContain(old)
  })
  it('intensity thresholds are ordered', () => {
    expect(INTENSITY_THRESHOLDS.soft).toBeLessThan(INTENSITY_THRESHOLDS.clear)
    expect(INTENSITY_THRESHOLDS.clear).toBeLessThan(INTENSITY_THRESHOLDS.vivid)
    expect(INTENSITY_THRESHOLDS.vivid).toBeLessThan(INTENSITY_THRESHOLDS.bold)
    expect(INTENSITY_THRESHOLDS.bold).toBeLessThan(INTENSITY_THRESHOLDS.intense)
  })
})

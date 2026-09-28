import { describe, it, expect } from 'vitest'
import { ansiPaletteFromVars, brighten } from '../utils/terminalPalette'

/** A reader over a fixed map, standing in for getComputedStyle. */
const reader = (vars: Record<string, string>) => (name: string) => vars[name] ?? ''

const NORD_DARK = {
  '--bg': '#2e3440',
  '--text': '#d8dee9',
  '--danger': '#bf616a',
  '--ok': '#a3be8c',
  '--warn': '#ebcb8b',
  '--info': '#81a1c1',
}

const NORD_LIGHT = {
  '--bg': '#eceff4',
  '--text': '#4c566a',
  '--danger': '#bf616a',
  '--ok': '#689d6a',
  '--warn': '#d08770',
  '--info': '#5e81ac',
}

describe('ansiPaletteFromVars', () => {
  it('maps the semantic variables of the active theme onto ANSI slots', () => {
    const p = ansiPaletteFromVars(reader(NORD_DARK))
    expect(p.red).toBe('#bf616a')
    expect(p.green).toBe('#a3be8c')
    expect(p.yellow).toBe('#ebcb8b')
    expect(p.blue).toBe('#81a1c1')
  })

  it('orders black and white by luminance, not by background and foreground', () => {
    // ANSI 0 and 7 mean dark and light. Reading them as bg/fg would make
    // `\e[30m` output invisible on a light theme, where the background is the
    // light end — so the two extremes swap with the theme's polarity.
    const dark = ansiPaletteFromVars(reader(NORD_DARK))
    expect(dark.black).toBe('#2e3440')
    expect(dark.white).toBe('#d8dee9')

    const light = ansiPaletteFromVars(reader(NORD_LIGHT))
    expect(light.black).toBe('#4c566a')
    expect(light.white).toBe('#eceff4')
  })

  it('falls back for the two hues no semantic variable carries', () => {
    const p = ansiPaletteFromVars(reader(NORD_DARK))
    expect(p.magenta).toBe('#c678dd')
    expect(p.cyan).toBe('#56b6c2')
  })

  it('lets a theme override magenta and cyan', () => {
    const p = ansiPaletteFromVars(reader({
      ...NORD_DARK, '--term-magenta': '#b48ead', '--term-cyan': '#88c0d0',
    }))
    expect(p.magenta).toBe('#b48ead')
    expect(p.cyan).toBe('#88c0d0')
  })

  it('never hands an empty string to xterm when a theme defines nothing', () => {
    const p = ansiPaletteFromVars(reader({}))
    for (const [slot, value] of Object.entries(p)) {
      expect(value, slot).toMatch(/^#[0-9a-f]{6}$/i)
    }
  })

  it('keeps every bright entry distinct from its base colour', () => {
    const p = ansiPaletteFromVars(reader(NORD_DARK))
    const pairs: [string, string][] = [
      [p.red, p.brightRed], [p.green, p.brightGreen], [p.yellow, p.brightYellow],
      [p.blue, p.brightBlue], [p.magenta, p.brightMagenta], [p.cyan, p.brightCyan],
      [p.black, p.brightBlack], [p.white, p.brightWhite],
    ]
    for (const [base, bright] of pairs) expect(bright).not.toBe(base)
  })
})

describe('brighten', () => {
  it('moves towards white on a dark theme and towards black on a light one', () => {
    expect(brighten('#000000', true)).toBe('#4d4d4d')
    expect(brighten('#ffffff', false)).toBe('#b3b3b3')
  })

  it('expands shorthand hex', () => {
    expect(brighten('#fff', false)).toBe('#b3b3b3')
  })

  it('returns a non-hex value untouched rather than mangling it', () => {
    // A theme is free to use rgb() or a colour function. A duplicate colour
    // degrades better than a wrong one.
    expect(brighten('rgb(10, 20, 30)', true)).toBe('rgb(10, 20, 30)')
    expect(brighten('', true)).toBe('')
  })
})

import { describe, it, expect } from 'vitest'
import { FONT_PROBE_TEXT, MONO_FONT_CANDIDATES } from '../utils/monoFontCandidates'

describe('MONO_FONT_CANDIDATES', () => {
  it('holds no duplicate family name', () => {
    expect(new Set(MONO_FONT_CANDIDATES).size).toBe(MONO_FONT_CANDIDATES.length)
  })

  it('leads with plainly-installed families before the generated Nerd Font names', () => {
    const firstNerd = MONO_FONT_CANDIDATES.findIndex(name => name.includes('Nerd Font'))
    expect(MONO_FONT_CANDIDATES.indexOf('JetBrains Mono')).toBeLessThan(firstNerd)
  })

  it('expands each Nerd Font base into the cell-width variants a patcher installs', () => {
    for (const suffix of ['Nerd Font', 'Nerd Font Mono', 'Nerd Font Propo']) {
      expect(MONO_FONT_CANDIDATES).toContain(`JetBrainsMono ${suffix}`)
    }
  })

  it("carries powerlevel10k's recommended build, whose name says nothing about Nerd Font", () => {
    expect(MONO_FONT_CANDIDATES).toContain('MesloLGS NF')
  })

  it('covers the per-platform defaults a terminal falls back to', () => {
    for (const name of ['Menlo', 'Consolas', 'DejaVu Sans Mono', 'SF Mono']) {
      expect(MONO_FONT_CANDIDATES).toContain(name)
    }
  })
})

describe('probe text', () => {
  it('mixes the widest and narrowest glyphs, long enough to accumulate a difference', () => {
    expect(FONT_PROBE_TEXT).toMatch(/m/)
    expect(FONT_PROBE_TEXT).toMatch(/W/)
    expect(FONT_PROBE_TEXT).toMatch(/i/)
    expect(FONT_PROBE_TEXT.length).toBeGreaterThan(24)
  })
})

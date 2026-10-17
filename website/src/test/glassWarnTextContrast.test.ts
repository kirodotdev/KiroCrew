/**
 * Warn-hued text on a warn-tinted fill clears the WCAG 1.4.3 text floor (4.5:1)
 * in every built-in LIGHT palette. Issue #19920: plain `--warn` on a
 * `glass-warn` pane read as gold on pale yellow (1.9:1 in rosepine-light, 4.0:1
 * in `light`). Issue #20071: the same hue on `bg-warn-subtle` / `bg-warn/N`
 * fills, 64 source lines outside the glass. One token now carries both:
 * `--warn-text` is `--warn` in dark palettes and, under `[data-mode="light"]`,
 * half the hue darkened toward black (still amber-brown, so it reads as a
 * warning); `text-warn-text` paints it on the fills and `.glass-warn-fg` on the
 * glass. This file reads that mix from
 * index.css and measures it against every tint the source actually pairs with
 * it, and refuses a source line that still pairs plain `text-warn` with a warn
 * tint or a `glass-warn` pane. A tint that only appears on `hover:` is paired
 * with `hover:text-warn-text`, so the control keeps its plain hue at rest.
 *
 * SURFACES: a tint is translucent, so the exact pixel under the text depends
 * on what is behind it. The test takes the fill over `--bg`, `--bg-elevated`
 * and `--card` (the surfaces the pairings sit on; the first two are also the
 * glass's reduce-transparency fallbacks) and requires the floor against each.
 *
 * SCOPE: light palettes only, which is what both issues ask; dark palettes keep
 * plain `--warn`, and a test pins that. Theme packs installed at runtime are
 * out of reach.
 *
 * The scan and the arithmetic live in statusTextTint.ts, shared with the ok and
 * danger tokens (statusTextContrast.test.ts).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { contrastRatio } from '../lib/iconContrast'
import { resolveVar } from './themePalette'
import {
  ACTIVE, DARK_THEMES, FILES, LIGHT_THEMES, LINES, MIN_TEXT, SURFACES,
  lightShare, lightText, lum, mix, pairedTints, patterns, plainOnTint, subtleShare, tokenRgb,
} from './statusTextTint'

/** `.glass-warn` mixes `--warn` at 12% into its tint (index.css). */
const GLASS_TINT = 0.12
const GLASS_WARN = /(?<![\w-])glass-warn(?![\w-])/

const plainWarnOnTint = (text: string) => plainOnTint(text, 'warn')

describe('warn text on warn tints in light palettes (#19920, #20071)', () => {
  it('anchors the premise: the rules exist and the light palettes are found', () => {
    expect(ACTIVE).toMatch(/:root\s*\{\s*--warn-text\s*:\s*var\(--warn\)\s*;?\s*\}/)
    expect(ACTIVE).toMatch(/\.glass-warn-fg\s*\{\s*color:\s*var\(--warn-text\)/)
    expect(readFileSync(resolve(__dirname, '../tailwind-theme.css'), 'utf8')).toMatch(/--color-warn-text:\s*var\(--warn-text\);/)
    expect(LIGHT_THEMES.length).toBeGreaterThanOrEqual(15)
    for (const t of ['light', 'rosepine-light', 'everforest-light']) expect(LIGHT_THEMES).toContain(t)
    expect(FILES.size, 'scanned no source files').toBeGreaterThan(100)
  })

  it('leaves dark palettes on plain --warn', () => {
    // The token is derived outside the [data-theme] blocks: `:root` sets it to
    // `--warn` and only `[data-mode="light"]` overrides it. No theme block and
    // no dark-mode rule may set it, or a dark palette's warn text would drift.
    expect(ACTIVE).not.toMatch(/\[data-mode="dark"\][^{}]*\{[^{}]*--warn-text\s*:/)
    for (const t of DARK_THEMES) expect(resolveVar(t, '--warn-text'), `${t} overrides --warn-text`).toBe('var(--warn)')
    expect(DARK_THEMES.length).toBeGreaterThanOrEqual(15)
  })

  it('plain --warn would fail somewhere (so the mix is doing real work)', () => {
    const failing = LIGHT_THEMES.filter((t) => {
      const warn = tokenRgb(t, '--warn')
      return contrastRatio(lum(warn), lum(mix(warn, tokenRgb(t, '--bg'), subtleShare(t, 'warn')))) < MIN_TEXT
    })
    expect(failing.length).toBeGreaterThan(0)
  })

  it('clears 4.5:1 on the glass-warn tint in every light palette', () => {
    const w = lightShare('warn')
    const failures: string[] = []
    for (const t of LIGHT_THEMES) {
      const warn = tokenRgb(t, '--warn')
      const fg = lum(lightText(t, 'warn', w))
      for (const base of ['--bg-elevated', '--bg']) {
        const ratio = contrastRatio(fg, lum(mix(warn, tokenRgb(t, base), GLASS_TINT)))
        if (ratio < MIN_TEXT) failures.push(`${t}: warn ${w * 100}% vs warn/12 over ${base} is ${ratio.toFixed(2)}:1`)
      }
    }
    expect(failures, `glass-warn text below 4.5:1:\n${failures.join('\n')}`).toEqual([])
  })

  it('clears 4.5:1 on every warn tint the source pairs with text-warn-text, in every light palette', () => {
    const w = lightShare('warn')
    const { tints, sites } = pairedTints('warn')
    // The premise: the pairings exist (the issue listed 64 lines) and the
    // strongest tint the token is tuned for is still among them, so a new
    // `bg-warn/40 text-warn-text` is measured here rather than assumed.
    expect(sites.length).toBeGreaterThanOrEqual(50)
    expect([...tints.keys()].sort()).toEqual(expect.arrayContaining(['subtle', '/10', '/15', '/30']))
    const failures: string[] = []
    for (const t of LIGHT_THEMES) {
      const warn = tokenRgb(t, '--warn')
      const fg = lum(lightText(t, 'warn', w))
      for (const [name, share] of tints) {
        const n = share === 'subtle' ? subtleShare(t, 'warn') : share
        for (const base of SURFACES) {
          const ratio = contrastRatio(fg, lum(mix(warn, tokenRgb(t, base), n)))
          if (ratio < MIN_TEXT) failures.push(`${t}: warn ${w * 100}% vs bg-warn${name === 'subtle' ? '-subtle' : name} over ${base} is ${ratio.toFixed(2)}:1`)
        }
      }
    }
    expect(failures, `text-warn-text below 4.5:1:\n${failures.join('\n')}`).toEqual([])
  })

  it('tells a tint at rest from a tint that only appears on hover', () => {
    // The scan's own premise: the four controls that have no tint until hover
    // keep their plain hue at rest, and a real pairing is still refused.
    expect(plainWarnOnTint('bg-warn-subtle text-warn')).toBe(true)
    expect(plainWarnOnTint('text-warn bg-warn/10 border-warn')).toBe(true)
    expect(plainWarnOnTint('hover:text-warn hover:bg-warn-subtle')).toBe(true)
    expect(plainWarnOnTint('text-warn hover:bg-warn/10')).toBe(true)
    expect(plainWarnOnTint('text-warn hover:text-warn-text hover:bg-warn/10')).toBe(false)
    expect(plainWarnOnTint('text-muted hover:text-warn-text hover:bg-warn-subtle')).toBe(false)
    expect(plainWarnOnTint('bg-warn-subtle text-warn-text')).toBe(false)
    expect(plainWarnOnTint('bg-warn text-warn-fg hover:bg-warn/80')).toBe(false)
  })

  it('leaves no warn tint or glass-warn pane painting plain text-warn in the source', () => {
    // A line that lays a warn tint (`bg-warn-subtle`, `bg-warn/N`) or a
    // `glass-warn` pane under plain `text-warn` is the pairing these issues fix.
    // `text-warn-text` (fills) and `glass-warn-fg` (glass) are the sanctioned
    // replacements. Lines are the unit, as in the issue's own census: a ternary
    // that puts the tint and the text in two literals on one line is still one
    // element wearing both. A tint under a variant is matched with the text of
    // that variant (see plainOnTint).
    const plain = patterns('warn').plain
    const offenders: string[] = []
    for (const l of LINES) {
      if (!plain.test(l.text)) continue
      if (plainWarnOnTint(l.text) || GLASS_WARN.test(l.text)) offenders.push(`${l.file}:${l.line}`)
    }
    expect(offenders, `warn tints still painting plain text-warn:\n${offenders.join('\n')}`).toEqual([])
  })
})

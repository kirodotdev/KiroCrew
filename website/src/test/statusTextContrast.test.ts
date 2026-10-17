/**
 * Ok-, danger- and info-hued text on its own tinted fill clears the WCAG 1.4.3
 * text floor (4.5:1) in every built-in LIGHT palette. Plain `--ok` on
 * `--ok-subtle` reads 1.97:1 in amoled-light, plain `--danger` on
 * `--danger-subtle` 2.37:1 in tokyonight-light and plain `--info` on
 * `bg-info-subtle` 1.69:1 in amoled-light. `--ok-text` / `--danger-text` /
 * `--info-text` have the shape of `--warn-text`: the plain hue in dark palettes
 * and, under `[data-mode="light"]`, the hue darkened toward black, so they
 * still read as success, error and info. `text-<hue>-text` paints them.
 *
 * This file reads each mix from index.css, measures it against every tint the
 * source pairs with it over `--bg`, `--bg-elevated` and `--card`, and refuses a
 * source line that still pairs plain `text-ok` / `text-danger` with that hue's
 * tint. A tint that only appears on `hover:` is paired with
 * `hover:text-<hue>-text`, so the control keeps its plain hue at rest.
 *
 * SCOPE: light palettes only; dark palettes keep the plain hue, and a test pins
 * that. Theme packs installed at runtime are out of reach. The scan and the
 * arithmetic live in statusTextTint.ts, shared with glassWarnTextContrast.
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

const HUES = ['ok', 'danger', 'info'] as const
/** How many source lines pair each hue's tint with its text token, at least:
 *  the premise that the measured set is the real one. */
const MIN_SITES: Record<(typeof HUES)[number], number> = { ok: 30, danger: 30, info: 10 }

describe('ok, danger and info text on their own tints in light palettes', () => {
  it('anchors the premise: the rules exist and the light palettes are found', () => {
    const bridge = readFileSync(resolve(__dirname, '../tailwind-theme.css'), 'utf8')
    for (const hue of HUES) {
      expect(ACTIVE).toMatch(new RegExp(`:root\\s*\\{[^{}]*--${hue}-text\\s*:\\s*var\\(--${hue}\\)\\s*;`))
      expect(bridge).toMatch(new RegExp(`--color-${hue}-text:\\s*var\\(--${hue}-text\\);`))
    }
    expect(LIGHT_THEMES.length).toBeGreaterThanOrEqual(15)
    for (const t of ['light', 'amoled-light', 'tokyonight-light']) expect(LIGHT_THEMES).toContain(t)
    expect(FILES.size, 'scanned no source files').toBeGreaterThan(100)
  })

  for (const hue of HUES) {
    describe(`--${hue}-text`, () => {
      it('leaves dark palettes on the plain hue', () => {
        expect(ACTIVE).not.toMatch(new RegExp(`\\[data-mode="dark"\\][^{}]*\\{[^{}]*--${hue}-text\\s*:`))
        for (const t of DARK_THEMES) expect(resolveVar(t, `--${hue}-text`), `${t} overrides --${hue}-text`).toBe(`var(--${hue})`)
        expect(DARK_THEMES.length).toBeGreaterThanOrEqual(15)
      })

      it('the plain hue fails somewhere (so the mix is doing real work)', () => {
        const failing = LIGHT_THEMES.filter((t) => {
          const c = tokenRgb(t, `--${hue}`)
          return contrastRatio(lum(c), lum(mix(c, tokenRgb(t, '--bg'), subtleShare(t, hue)))) < MIN_TEXT
        })
        expect(failing.length).toBeGreaterThan(0)
      })

      it(`clears 4.5:1 on every ${hue} tint the source pairs with text-${hue}-text, in every light palette`, () => {
        const w = lightShare(hue)
        const { tints, sites } = pairedTints(hue)
        // The premise: the pairings exist and the subtle fill plus the common
        // alpha tint are among them, so a new stronger tint is measured here
        // rather than assumed.
        expect(sites.length).toBeGreaterThanOrEqual(MIN_SITES[hue])
        expect([...tints.keys()]).toEqual(expect.arrayContaining(['subtle', '/10']))
        const failures: string[] = []
        for (const t of LIGHT_THEMES) {
          const c = tokenRgb(t, `--${hue}`)
          const fg = lum(lightText(t, hue, w))
          for (const [name, share] of tints) {
            const n = share === 'subtle' ? subtleShare(t, hue) : share
            for (const base of SURFACES) {
              const ratio = contrastRatio(fg, lum(mix(c, tokenRgb(t, base), n)))
              if (ratio < MIN_TEXT) failures.push(`${t}: ${hue} ${w * 100}% vs bg-${hue}${name === 'subtle' ? '-subtle' : name} over ${base} is ${ratio.toFixed(2)}:1`)
            }
          }
        }
        expect(failures, `text-${hue}-text below 4.5:1:\n${failures.join('\n')}`).toEqual([])
      })

      it(`leaves no ${hue} tint painting plain text-${hue} in the source`, () => {
        const plain = patterns(hue).plain
        const offenders = LINES.filter((l) => plain.test(l.text) && plainOnTint(l.text, hue)).map((l) => `${l.file}:${l.line}`)
        expect(offenders, `${hue} tints still painting plain text-${hue}:\n${offenders.join('\n')}`).toEqual([])
      })
    })
  }

  it('tells a tint at rest from a tint that only appears on hover', () => {
    expect(plainOnTint('bg-ok-subtle text-ok', 'ok')).toBe(true)
    expect(plainOnTint('text-danger bg-danger/10 border-danger', 'danger')).toBe(true)
    expect(plainOnTint('text-danger hover:bg-danger/10', 'danger')).toBe(true)
    expect(plainOnTint('text-danger hover:text-danger-text hover:bg-danger/10', 'danger')).toBe(false)
    expect(plainOnTint('bg-ok-subtle text-ok-text', 'ok')).toBe(false)
    expect(plainOnTint('bg-danger text-danger-fg hover:bg-danger/80', 'danger')).toBe(false)
    // The hues do not cross: an ok tint does not govern danger text.
    expect(plainOnTint('bg-ok-subtle text-danger', 'danger')).toBe(false)
  })
})

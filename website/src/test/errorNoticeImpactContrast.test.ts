/**
 * The colours an `ErrorNotice` paints at each `impact` level, measured in every
 * built-in palette against the fill each one sits on: the danger tint
 * (blocking), the warn tint (partial, and a nested notice's host) and the bare
 * surface (minor), each tint laid over `--bg`, `--bg-elevated` and `--card`.
 *
 * Two bars, because the palettes differ in what they can reach:
 * - WCAG 2.2 AA (4.5:1 text, 3:1 button border and focus ring) in every LIGHT
 *   palette and in the default dark palettes. That is the bar
 *   statusTextContrast.test.ts holds light status text to.
 * - Never below today's colour for the same part in every other DARK palette.
 *   Several of them paint their own `--text` below 4.5:1 on these tints
 *   (solarized-dark: 3.81:1 on the warn tint over `--bg-elevated`), so no mix
 *   toward `--text` can reach AA there; the mixes still only raise contrast.
 *
 * The notice colours are `color-mix()` tokens in index.css; this file reads
 * each mix from the stylesheet, so a change to the rule is measured rather than
 * a copy of it. Theme packs installed at runtime are out of reach.
 */
import { describe, it, expect } from 'vitest'
import { contrastRatio } from '../lib/iconContrast'
import {
  ACTIVE, DARK_THEMES, LIGHT_THEMES, MIN_TEXT, SURFACES,
  lightShare, lightText, lum, mix, subtleShare, tokenRgb, type Rgb,
} from './statusTextTint'

const MIN_NON_TEXT = 3
const DEFAULT_DARK = DARK_THEMES.filter((t) => t === 'dark' || t === 'kiro-dark')
const AA_THEMES = [...LIGHT_THEMES, ...DEFAULT_DARK]
const OTHER_DARK = DARK_THEMES.filter((t) => !DEFAULT_DARK.includes(t))

/** `--<hue>-text` as the theme resolves it: plain in dark, darkened in light. */
function hueText(theme: string, hue: 'danger' | 'warn'): Rgb {
  return LIGHT_THEMES.includes(theme) ? lightText(theme, hue, lightShare(hue)) : tokenRgb(theme, `--${hue}`)
}

function base(theme: string, prop: string): Rgb {
  if (prop === '--danger-text') return hueText(theme, 'danger')
  if (prop === '--warn-text') return hueText(theme, 'warn')
  return tokenRgb(theme, prop)
}

/** A notice token, read from its `:root` rule: `color-mix(in srgb, var(--a) N%, var(--b))`. */
function token(theme: string, name: string): Rgb {
  const m = new RegExp(`--${name}\\s*:\\s*color-mix\\(in srgb,\\s*var\\((--[\\w-]+)\\)\\s*(\\d+)%,\\s*var\\((--[\\w-]+)\\)\\)`).exec(ACTIVE)
  expect(m, `index.css has no color-mix rule for --${name}`).not.toBeNull()
  return mix(base(theme, m![1]), base(theme, m![3]), Number(m![2]) / 100)
}

type Fill = 'danger' | 'warn' | 'surface'
/** The fill under a part: a hue's tint at `alpha` (default its `--<hue>-subtle`) over a surface. */
function fill(theme: string, f: Fill, surface: string, alpha?: number): Rgb {
  if (f === 'surface') return tokenRgb(theme, surface)
  return mix(tokenRgb(theme, `--${f}`), tokenRgb(theme, surface), alpha ?? subtleShare(theme, f))
}

/** Today's block notice: `text-danger-text` on `bg-danger/10`. */
const today = (t: string, surface: string) => contrastRatio(lum(hueText(t, 'danger')), lum(fill(t, 'danger', surface, 0.1)))

const PAIRS: Array<{ part: string; fg: (t: string) => Rgb; on: Fill[]; min: number; was: (t: string, surface: string) => number }> = [
  { part: 'blocking title, icon, link', fg: (t) => token(t, 'danger-ink'), on: ['danger'], min: MIN_TEXT, was: today },
  { part: 'blocking body', fg: (t) => token(t, 'danger-body'), on: ['danger'], min: MIN_TEXT, was: today },
  { part: 'nested blocking title, icon, link', fg: (t) => token(t, 'danger-ink-nested'), on: ['warn'], min: MIN_TEXT, was: today },
  { part: 'nested blocking body', fg: (t) => token(t, 'danger-body-nested'), on: ['warn'], min: MIN_TEXT, was: today },
  { part: 'partial body', fg: (t) => tokenRgb(t, '--text-strong'), on: ['warn'], min: MIN_TEXT, was: today },
  { part: 'minor body', fg: (t) => tokenRgb(t, '--muted'), on: ['surface'], min: MIN_TEXT, was: today },
  {
    part: 'minor link', fg: (t) => token(t, 'minor-link'), on: ['surface', 'warn'], min: MIN_TEXT,
    was: (t, s) => contrastRatio(lum(tokenRgb(t, '--accent')), lum(tokenRgb(t, s))),
  },
  {
    part: 'action button border', fg: (t) => token(t, 'notice-btn-border'), on: ['danger', 'warn', 'surface'], min: MIN_NON_TEXT,
    was: (t, s) => contrastRatio(lum(tokenRgb(t, '--border-hover')), lum(fill(t, 'danger', s))),
  },
  {
    part: 'action button focus ring', fg: (t) => token(t, 'notice-ring'), on: ['danger', 'warn', 'surface'], min: MIN_NON_TEXT,
    was: (t, s) => contrastRatio(lum(tokenRgb(t, '--ring')), lum(fill(t, 'danger', s))),
  },
]

describe('ErrorNotice impact colours in every palette', () => {
  it('finds the palettes it measures', () => {
    expect(LIGHT_THEMES.length).toBeGreaterThanOrEqual(15)
    expect(OTHER_DARK.length).toBeGreaterThanOrEqual(10)
    expect(DEFAULT_DARK).toContain('dark')
  })

  for (const { part, fg, on, min, was } of PAIRS) {
    it(`${part} clears ${min}:1 in every light palette and the default dark one`, () => {
      const failures: string[] = []
      for (const t of AA_THEMES) {
        const c = lum(fg(t))
        for (const s of SURFACES) for (const f of on) {
          const ratio = contrastRatio(c, lum(fill(t, f, s)))
          if (ratio < min) failures.push(`${t}: on ${f} over ${s} is ${ratio.toFixed(2)}:1`)
        }
      }
      expect(failures, `${part} below ${min}:1:\n${failures.join('\n')}`).toEqual([])
    })

    it(`${part} is never below today's colour in the other dark palettes`, () => {
      const failures: string[] = []
      for (const t of OTHER_DARK) {
        const c = lum(fg(t))
        for (const s of SURFACES) for (const f of on) {
          const ratio = contrastRatio(c, lum(fill(t, f, s)))
          const before = was(t, s)
          if (ratio < min && ratio < before) failures.push(`${t}: on ${f} over ${s} is ${ratio.toFixed(2)}:1, was ${before.toFixed(2)}:1`)
        }
      }
      expect(failures, `${part} lost contrast:\n${failures.join('\n')}`).toEqual([])
    })
  }
})

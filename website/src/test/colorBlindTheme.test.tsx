/**
 * The Color-blind safe theme keeps its status colours apart for the three
 * dichromacies, and keeps every foreground readable.
 *
 * Simulation: Machado, Oliveira & Fernandes (2009), "A Physiologically-based
 * Model for Simulation of Color Vision Deficiency", severity 1.0 matrices,
 * applied to linear-light sRGB. Severity 1.0 is full dichromacy (the cone class
 * is missing), the worst case; anomalous trichromats see more difference.
 *
 * Distance: CIEDE2000 between the two simulated colours. About 2.3 is a
 * just-noticeable difference for two patches side by side, but a status colour
 * is read on its own (one badge, one dot, one diff line), not next to the other
 * status, so it must be recognised from memory. 20 is the floor used here: it
 * is roughly the gap between two colours people name differently, and it is
 * what separates High Contrast's green ok from its yellow warn for typical
 * vision (35.7) while that pair falls to 4.3 under deuteranopia, which the
 * control case below pins so the floor is shown to have teeth.
 *
 * Contrast: WCAG 2.x relative luminance. Each `--<x>-fg` must reach 4.5:1 on
 * its own `--<x>` fill (text on a filled badge or button), and body text 4.5:1
 * on every surface it is painted over. Status hues are also non-text UI
 * indicators (WCAG 1.4.11), so each must reach 3:1 against the page.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { contrastRatio } from '../lib/iconContrast'
import { THEMES as PICKER, THEME_LABEL_KEY, ThemeProvider, useTheme } from '../hooks/useTheme'
import { THEMES as CSS_THEMES, resolveVar } from './themePalette'

type Lin = [number, number, number]

const VARIANTS = ['colorblind-dark', 'colorblind-light'] as const
const STATUS_PAIRS = [
  ['ok', 'danger'],
  ['ok', 'warn'],
  ['danger', 'warn'],
] as const
const MIN_DELTA_E = 20
const MIN_TEXT = 4.5
const MIN_UI = 3

const MACHADO: Record<'protanopia' | 'deuteranopia' | 'tritanopia', number[][]> = {
  protanopia: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  deuteranopia: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
  tritanopia: [
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930809, 0.147602],
    [0.004733, 0.691367, 0.3039],
  ],
}
const VISIONS = ['typical', ...Object.keys(MACHADO)] as const

const linearize = (v: number) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)

function hexLin(hex: string): Lin {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) throw new Error(`expected an opaque #rgb or #rrggbb colour, got ${hex}`)
  const full = m[1].length === 3 ? [...m[1]].map((d) => d + d).join('') : m[1]
  return [0, 2, 4].map((i) => linearize(parseInt(full.slice(i, i + 2), 16) / 255)) as Lin
}

function token(theme: string, name: string): Lin {
  const v = resolveVar(theme, `--${name}`)
  if (!v) throw new Error(`${theme} has no --${name}`)
  return hexLin(v)
}

function simulate(c: Lin, vision: (typeof VISIONS)[number]): Lin {
  if (vision === 'typical') return c
  const m = MACHADO[vision]
  return m.map((r) => Math.min(1, Math.max(0, r[0] * c[0] + r[1] * c[1] + r[2] * c[2]))) as Lin
}

const lum = (c: Lin) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

function lab([r, g, b]: Lin): Lin {
  const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : (841 / 108) * t + 4 / 29)
  const x = f((r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047)
  const y = f(r * 0.2126729 + g * 0.7151522 + b * 0.072175)
  const z = f((r * 0.0193339 + g * 0.119192 + b * 0.9503041) / 1.08883)
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)]
}

/** CIEDE2000 (Sharma, Wu & Dalal 2005 formulation). */
function deltaE00(c1: Lin, c2: Lin): number {
  const [L1, a1, b1] = lab(c1)
  const [L2, a2, b2] = lab(c2)
  const rad = Math.PI / 180
  const Cb = (Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2
  const G = 0.5 * (1 - Math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
  const ap1 = a1 * (1 + G)
  const ap2 = a2 * (1 + G)
  const Cp1 = Math.hypot(ap1, b1)
  const Cp2 = Math.hypot(ap2, b2)
  const hp1 = (Math.atan2(b1, ap1) / rad + 360) % 360
  const hp2 = (Math.atan2(b2, ap2) / rad + 360) % 360
  let dh = 0
  if (Cp1 * Cp2 !== 0) {
    dh = hp2 - hp1
    if (dh > 180) dh -= 360
    else if (dh < -180) dh += 360
  }
  const dL = L2 - L1
  const dC = Cp2 - Cp1
  const dH = 2 * Math.sqrt(Cp1 * Cp2) * Math.sin((dh * rad) / 2)
  const Lb = (L1 + L2) / 2
  const Cpb = (Cp1 + Cp2) / 2
  let hpb = hp1 + hp2
  if (Cp1 * Cp2 !== 0) {
    if (Math.abs(hp1 - hp2) > 180) hpb += hpb < 360 ? 360 : -360
    hpb /= 2
  }
  const T =
    1 -
    0.17 * Math.cos((hpb - 30) * rad) +
    0.24 * Math.cos(2 * hpb * rad) +
    0.32 * Math.cos((3 * hpb + 6) * rad) -
    0.2 * Math.cos((4 * hpb - 63) * rad)
  const dTheta = 30 * Math.exp(-(((hpb - 275) / 25) ** 2))
  const Rc = 2 * Math.sqrt(Cpb ** 7 / (Cpb ** 7 + 25 ** 7))
  const Sl = 1 + (0.015 * (Lb - 50) ** 2) / Math.sqrt(20 + (Lb - 50) ** 2)
  const Sc = 1 + 0.045 * Cpb
  const Sh = 1 + 0.015 * Cpb * T
  const Rt = -Math.sin(2 * dTheta * rad) * Rc
  return Math.sqrt((dL / Sl) ** 2 + (dC / Sc) ** 2 + (dH / Sh) ** 2 + Rt * (dC / Sc) * (dH / Sh))
}

/** Smallest CIEDE2000 between two tokens of `theme` across every vision. */
function worstSeparation(theme: string, a: string, b: string): { vision: string; de: number } {
  let worst = { vision: '', de: Infinity }
  for (const vision of VISIONS) {
    const de = deltaE00(simulate(token(theme, a), vision), simulate(token(theme, b), vision))
    if (de < worst.de) worst = { vision, de }
  }
  return worst
}

const ratio = (theme: string, fg: string, bg: string) => contrastRatio(lum(token(theme, fg)), lum(token(theme, bg)))

describe('Color-blind safe theme', () => {
  it('ships a dark and a light palette and a picker entry', () => {
    for (const v of VARIANTS) expect(CSS_THEMES).toContain(v)
    expect(PICKER.map((t) => t.value)).toContain('colorblind')
    expect(THEME_LABEL_KEY.colorblind).toBe('hooks.theme.color_blind_safe')
  })

  it('gives the light user bubble a fill, since its --card is the page colour', () => {
    expect(resolveVar('colorblind-light', '--card')).toBe(resolveVar('colorblind-light', '--bg'))
    expect(ratio('colorblind-light', 'bg-hover', 'bg')).toBeGreaterThan(1.09)
    const css = readFileSync(join(__dirname, '../styles/chat-transcript.css'), 'utf8')
    expect(css).toMatch(/\[data-theme="colorblind-light"\] \.user-bubble\{background-color:var\(--bg-hover\)\}/)
  })

  it('the distance floor fails a palette that is not colour-blind safe (control)', () => {
    // High Contrast pairs a pure green ok with a yellow warn: far apart for
    // typical vision, nearly one colour without M cones.
    const typical = deltaE00(token('highcontrast-dark', 'ok'), token('highcontrast-dark', 'warn'))
    const deutan = deltaE00(
      simulate(token('highcontrast-dark', 'ok'), 'deuteranopia'),
      simulate(token('highcontrast-dark', 'warn'), 'deuteranopia'),
    )
    expect(typical).toBeGreaterThan(MIN_DELTA_E)
    expect(deutan).toBeLessThan(MIN_DELTA_E)
  })

  for (const theme of VARIANTS) {
    describe(theme, () => {
      for (const [a, b] of STATUS_PAIRS) {
        it(`keeps --${a} and --${b} apart under typical vision, protanopia, deuteranopia and tritanopia`, () => {
          const { vision, de } = worstSeparation(theme, a, b)
          expect(de, `${a}/${b} closest under ${vision}: ${de.toFixed(1)} dE00`).toBeGreaterThanOrEqual(MIN_DELTA_E)
        })
      }

      it('keeps --danger apart from body text, so an error never reads as plain text', () => {
        // Under red-green simulation light danger is a dark brown that differs
        // from the near-black body text mainly in lightness. Warn already sits at
        // its 3:1 ceiling on white and danger must stay 20 dE00 darker than it,
        // so the luminance step to --text is held at the widest that leaves
        // room for both: 2.3:1 (the first release had 2.14:1).
        const { vision, de } = worstSeparation(theme, 'danger', 'text')
        expect(de, `danger/text closest under ${vision}: ${de.toFixed(1)} dE00`).toBeGreaterThanOrEqual(MIN_DELTA_E)
        expect(ratio(theme, 'danger', 'text')).toBeGreaterThanOrEqual(2.3)
      })

      it('keeps added and removed diff lines apart under every simulation', () => {
        const { vision, de } = worstSeparation(theme, 'diff-add-text', 'diff-del-text')
        expect(de, `diff add/del closest under ${vision}: ${de.toFixed(1)} dE00`).toBeGreaterThanOrEqual(MIN_DELTA_E)
      })

      it('gives the accent a colour no status token uses', () => {
        const accent = resolveVar(theme, '--accent')?.toLowerCase()
        for (const s of ['ok', 'warn', 'danger', 'info']) {
          expect(resolveVar(theme, `--${s}`)?.toLowerCase(), `--accent equals --${s}`).not.toBe(accent)
        }
      })

      it('keeps every --*-fg at 4.5:1 on its own fill', () => {
        const failures: string[] = []
        for (const t of ['accent', 'ok', 'warn', 'danger', 'info', 'aim', 'muted']) {
          const r = ratio(theme, `${t}-fg`, t)
          if (r < MIN_TEXT) failures.push(`--${t}-fg on --${t}: ${r.toFixed(2)}:1`)
        }
        expect(failures).toEqual([])
      })

      it('keeps text at 4.5:1 on every surface and status hues at 3:1 on the page', () => {
        const failures: string[] = []
        for (const surface of ['bg', 'bg-accent', 'bg-elevated', 'bg-hover', 'card', 'panel', 'panel-strong']) {
          for (const fg of ['text', 'text-strong', 'muted']) {
            const r = ratio(theme, fg, surface)
            if (r < MIN_TEXT) failures.push(`--${fg} on --${surface}: ${r.toFixed(2)}:1`)
          }
        }
        const card = ratio(theme, 'card-fg', 'card')
        if (card < MIN_TEXT) failures.push(`--card-fg on --card: ${card.toFixed(2)}:1`)
        for (const hue of ['ok', 'warn', 'danger', 'info', 'accent']) {
          const r = ratio(theme, hue, 'bg')
          if (r < MIN_UI) failures.push(`--${hue} on --bg: ${r.toFixed(2)}:1`)
        }
        for (const t of ['diff-add-text', 'diff-del-text', 'diff-hunk-text']) {
          const r = ratio(theme, t, 'bg')
          if (r < MIN_TEXT) failures.push(`--${t} on --bg: ${r.toFixed(2)}:1`)
        }
        expect(failures).toEqual([])
      })
    })
  }
})

describe('Color-blind safe picker label', () => {
  const dir = join(__dirname, '../i18n/locales')
  const catalogs = readdirSync(dir)
    .filter((f) => f.endsWith('.json'))
    .map((f) => ({ file: f, json: JSON.parse(readFileSync(join(dir, f), 'utf8')) }))
    .filter(({ json }) => json?.hooks?.theme?.high_contrast !== undefined)

  it('is translated in every catalog that names the other accessibility theme', () => {
    expect(catalogs.length).toBeGreaterThanOrEqual(13)
    const english = catalogs.find((c) => c.file === 'en.manual.json')?.json.hooks.theme.color_blind_safe
    expect(english).toBe('👁️ Color-blind safe')
    for (const { file, json } of catalogs) {
      const label = json.hooks.theme.color_blind_safe
      expect(typeof label, `${file} has no hooks.theme.color_blind_safe`).toBe('string')
      // The pseudolocale wraps the whole value in brackets, so `includes`.
      expect(label.includes('👁️'), `${file}: ${label}`).toBe(true)
      if (file !== 'en.manual.json') expect(label, `${file} copies the English label`).not.toBe(english)
    }
  })

  it('resolves through the catalog in the picker', () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ThemeProvider>{children}</ThemeProvider>
      </QueryClientProvider>
    )
    const { result } = renderHook(() => useTheme(), { wrapper })
    const entry = result.current.allThemes.find((t) => t.value === 'colorblind')
    expect(entry?.label).toBe('👁️ Color-blind safe')
  })
})

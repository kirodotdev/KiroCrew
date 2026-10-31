/**
 * Muted text clears the WCAG 1.4.3 text floor (4.5:1) in every built-in
 * palette (#26268, #26283).
 *
 * Three things were under it. The default dark `--muted` (#7f7f88) read 4.34:1
 * on the dark `--card` (#26268). Thirteen other dark palettes had the same gap:
 * their `--muted` was tuned against `--bg` and read 3.81 to 4.41:1 on their
 * own `--card` (#26283). And text painted `text-muted/60`, which Tailwind
 * renders as `--muted` at 60% over the surface, read about 2.3 to 2.5:1: the
 * credits line (`data-testid="turn-stats"`, #26268) and every other muted
 * caption, label and placeholder that used it (#26283).
 *
 * Muted text also sits on the raised and hovered panels: menus, popovers and
 * dialogs on `--bg-elevated`, a hovered row or tab on `--bg-hover`. Those are
 * lighter than `--card` in a dark palette and darker in a light one, so a
 * `--muted` tuned on `--card` and `--bg` read 2.67 to 4.40:1 there in 33 of the
 * 42 palettes (#26598).
 *
 * This file measures the token itself and the colour muted text actually gets,
 * alpha included, on `--card`, `--bg`, `--bg-elevated` and `--bg-hover` in
 * every palette, and keeps the 60% shade off text.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { contrastRatio } from '../lib/iconContrast'
import { THEMES, resolveVar } from './themePalette'
import { LINES, MIN_TEXT, lum, mix, tokenRgb } from './statusTextTint'

const SURFACES = ['--card', '--bg', '--bg-elevated', '--bg-hover'] as const

const SOURCE = readFileSync(resolve(__dirname, '../pages/chat/AssistantMessage.tsx'), 'utf8')

/** The `text-muted[/N]` class on the credits line, as an alpha (1 when bare). */
function creditsLineAlpha(): number {
  const line = SOURCE.split('\n').find((l) => l.includes('data-testid="turn-stats"'))
  if (!line) throw new Error('no turn-stats line in AssistantMessage.tsx')
  const m = /\btext-muted(?:\/(\d+))?(?=[\s"'`])/.exec(line)
  if (!m) throw new Error(`turn-stats line paints no text-muted colour: ${line.trim()}`)
  return m[1] === undefined ? 1 : Number(m[1]) / 100
}

/** Rendered contrast of `--muted` at `alpha` over a theme's `surface`. */
function mutedRatio(theme: string, surface: string, alpha: number): number {
  const bg = tokenRgb(theme, surface)
  return contrastRatio(lum(mix(tokenRgb(theme, '--muted'), bg, alpha)), lum(bg))
}

/** `text-muted/60`, with any variant prefix (`placeholder:`, `hover:`). */
const MUTED_60 = /(?<![\w-])(?:[\w-]+:)*text-muted\/60(?![\w-])/
/** A translucent `--muted` fill (`bg-muted/15`) and bare `text-muted`. */
const MUTED_TINT = /(?<![\w-])bg-muted\/\d+(?![\w-])/
const TEXT_MUTED = /(?<![\w-])text-muted(?![\w/-])/
/** A lucide icon element (`<Search size={13} …`): a glyph, not text. */
const ICON_LINE = /^\s*<[A-Z]\w*\s+size=\{/

describe('muted text contrast in every built-in palette', () => {
  it('anchors the premise: every palette resolves a measurable --muted', () => {
    expect(THEMES.length).toBeGreaterThan(30)
    for (const t of THEMES) expect(resolveVar(t, '--muted'), t).toMatch(/^#[0-9a-f]{6}$/i)
  })

  for (const t of THEMES) {
    for (const s of SURFACES) {
      it(`${t}: --muted on ${s} reaches ${MIN_TEXT}:1`, () => {
        const ratio = mutedRatio(t, s, 1)
        expect(ratio, `${t} --muted on ${s} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(MIN_TEXT)
      })

      it(`${t}: the credits line as rendered on ${s} reaches ${MIN_TEXT}:1`, () => {
        const ratio = mutedRatio(t, s, creditsLineAlpha())
        expect(ratio, `${t} credits line on ${s} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(MIN_TEXT)
      })
    }
  }

  // `--muted` is also a FILL: the active Debug filter in LogsPage paints
  // `bg-muted text-muted-fg`. Lifting a dark `--muted` darkens a white label on
  // it, and darkening a light one does the same to a black label, so every
  // palette pairs its fill with the `--muted-fg` that clears 4.5:1 on it.
  for (const t of THEMES) {
    it(`${t}: --muted-fg on a --muted fill reaches ${MIN_TEXT}:1`, () => {
      const ratio = contrastRatio(lum(tokenRgb(t, '--muted-fg')), lum(tokenRgb(t, '--muted')))
      expect(ratio, `${t} --muted-fg on --muted = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(MIN_TEXT)
    })
  }

  it('anchors the premise: text-muted/60 renders under 4.5:1 in the default palettes', () => {
    for (const t of ['dark', 'light']) {
      for (const s of SURFACES) expect(mutedRatio(t, s, 0.6), `${t} on ${s}`).toBeLessThan(MIN_TEXT)
    }
  })

  it('paints no text in text-muted/60 (icons only)', () => {
    expect(LINES.length).toBeGreaterThan(1000)
    const offenders = LINES.filter((l) => MUTED_60.test(l.text) && !ICON_LINE.test(l.text)).map((l) => `${l.file}:${l.line}`)
    expect(offenders, 'text-muted/60 text reads ~2.4:1; use text-muted').toEqual([])
  })

  // A `bg-muted/N` pill lays `--muted` over whatever is under it, so muted text
  // on it reads against a colour this file never measures, under 4.5:1 for the
  // default-dark source badge in a hovered row. Badges take the opaque
  // `bg-bg-hover` fill instead, which the loop above measures.
  it('paints no text-muted on a translucent bg-muted fill', () => {
    const offenders = LINES.filter((l) => MUTED_TINT.test(l.text) && TEXT_MUTED.test(l.text)).map((l) => `${l.file}:${l.line}`)
    expect(offenders, 'text-muted on bg-muted/N reads under 4.5:1; use bg-bg-hover').toEqual([])
  })
})

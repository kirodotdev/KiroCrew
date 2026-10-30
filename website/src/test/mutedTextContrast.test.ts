/**
 * Muted meta text under a chat reply clears the WCAG 1.4.3 text floor (4.5:1)
 * in the default palettes (#26268).
 *
 * Two things were under it. The default dark `--muted` (#7f7f88) read 4.34:1
 * on the dark `--card`, so every muted line in dark mode fell short. And the
 * credits line (`data-testid="turn-stats"`) painted `text-muted/60`, which
 * Tailwind renders as `--muted` at 60% over the surface: about 2.4:1 in dark
 * and 2.3:1 in light. This file measures the token itself and the colour the
 * credits line actually gets, alpha included, on `--card` and `--bg`.
 *
 * SCOPE: the default palettes (`:root`/`dark`/`amber-dark` and
 * `light`/`amber-light`). The other built-in palettes tune `--muted` on their
 * own and are not pinned here.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { contrastRatio } from '../lib/iconContrast'
import { resolveVar } from './themePalette'
import { MIN_TEXT, lum, mix, tokenRgb } from './statusTextTint'

const DEFAULT_THEMES = ['dark', 'amber-dark', 'light', 'amber-light'] as const
const SURFACES = ['--card', '--bg'] as const

const SOURCE = readFileSync(resolve(__dirname, '../pages/chat/AssistantMessage.tsx'), 'utf8')

/** The `text-muted[/N]` class on the credits line, as an alpha (1 when bare). */
function creditsLineAlpha(): number {
  const line = SOURCE.split('\n').find((l) => l.includes('data-testid="turn-stats"'))
  if (!line) throw new Error('no turn-stats line in AssistantMessage.tsx')
  const m = /\btext-muted(?:\/(\d+))?(?=[\s"'`])/.exec(line)
  if (!m) throw new Error(`turn-stats line paints no text-muted colour: ${line.trim()}`)
  return m[1] === undefined ? 1 : Number(m[1]) / 100
}

describe('muted meta text contrast in the default palettes', () => {
  it('anchors the premise: the default palettes resolve a measurable --muted', () => {
    for (const t of DEFAULT_THEMES) expect(resolveVar(t, '--muted'), t).toMatch(/^#[0-9a-f]{6}$/i)
  })

  for (const t of DEFAULT_THEMES) {
    for (const s of SURFACES) {
      it(`${t}: --muted on ${s} reaches ${MIN_TEXT}:1`, () => {
        const ratio = contrastRatio(lum(tokenRgb(t, '--muted')), lum(tokenRgb(t, s)))
        expect(ratio, `${t} --muted on ${s} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(MIN_TEXT)
      })

      it(`${t}: the credits line as rendered on ${s} reaches ${MIN_TEXT}:1`, () => {
        const surface = tokenRgb(t, s)
        const text = mix(tokenRgb(t, '--muted'), surface, creditsLineAlpha())
        const ratio = contrastRatio(lum(text), lum(surface))
        expect(ratio, `${t} credits line on ${s} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(MIN_TEXT)
      })
    }
  }
})

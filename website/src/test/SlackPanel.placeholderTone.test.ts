/**
 * Settings ▸ Slack placeholders read as hints, not as saved values.
 *
 * The panel's placeholders are sample IDs (U0123ABC456, E0123ABC456) shaped
 * exactly like the values a user types. The app-wide placeholder colour is
 * `--muted` (index.css base rule), the secondary-text token, and in kiro-light
 * (the default theme) it reads 1.36:1 against typed `--text`, so a sample
 * looked like a saved ID. SlackPanel's root fades its placeholders to a share
 * of `--muted`.
 *
 * This file reads that share from the shipped class and measures it: at least
 * 3:1 on the field in every built-in palette (WCAG 1.4.11 floor), and in the
 * default light and dark palettes clearly further from typed text than before.
 * The two pull against each other: one share cannot give kiro-light 3:1 from
 * text without dropping amoled-midnight-light under 3:1 on the field, so the
 * text floor is 2.25:1 (kiro-light measures 2.46:1 at 75%, up from 1.36:1).
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { contrastRatio } from '../lib/iconContrast'
import { THEMES } from './themePalette'
import { lum, mix, tokenRgb } from './statusTextTint'

const PANEL = readFileSync(resolve(__dirname, '../pages/settings/SlackPanel.tsx'), 'utf8')
const TONE = /\[&_:is\(input,textarea\)::placeholder\]:text-muted\/(\d+)/
const MIN_ON_FIELD = 3
const MIN_FROM_TEXT = 2.25
/** The default colour theme is Kiro (useTheme DEFAULT_COLOR_THEME), plus the
 *  plain light/dark palettes. */
const DEFAULT_THEMES = ['kiro-light', 'kiro-dark', 'light', 'dark']

function share(): number {
  const m = TONE.exec(PANEL)
  expect(m, 'SlackPanel no longer sets its placeholder tone class').not.toBeNull()
  return Number(m![1]) / 100
}

/** The placeholder as painted: `--muted` at `a` alpha over the Input's
 *  `bg-bg-elevated` field (the only field fill in this panel). */
function painted(theme: string, a: number) {
  const field = tokenRgb(theme, '--bg-elevated')
  const ph = mix(tokenRgb(theme, '--muted'), field, a)
  return {
    onField: contrastRatio(lum(ph), lum(field)),
    fromText: contrastRatio(lum(ph), lum(tokenRgb(theme, '--text'))),
  }
}

describe('Slack settings placeholder tone', () => {
  it('fades placeholders below full --muted', () => {
    expect(share()).toBeLessThan(1)
  })

  it(`keeps ${MIN_ON_FIELD}:1 on the field in every built-in palette`, () => {
    const a = share()
    expect(THEMES.length).toBeGreaterThanOrEqual(30)
    const rows = THEMES.map((t) => ({ t, r: painted(t, a).onField })).sort((x, y) => x.r - y.r)
    const table = rows.map((x) => `${x.t}: ${x.r.toFixed(2)}:1`).join('\n')
    expect(rows[0].r, `minimum ${rows[0].r.toFixed(2)}:1 in ${rows[0].t}\n${table}`).toBeGreaterThanOrEqual(MIN_ON_FIELD)
  })

  it.each(DEFAULT_THEMES)('reads clearly lighter than typed text in %s', (theme) => {
    const a = share()
    const now = painted(theme, a).fromText
    const was = painted(theme, 1).fromText
    expect(now, `${theme}: placeholder vs typed text ${now.toFixed(2)}:1`).toBeGreaterThanOrEqual(MIN_FROM_TEXT)
    // And a visible step away from the app-wide --muted placeholder.
    expect(now, `${theme}: ${was.toFixed(2)}:1 before`).toBeGreaterThanOrEqual(was * 1.3)
  })
})

/**
 * Every theme's `--<tier>-fg` must be readable ON `--<tier>`.
 *
 * `bg-danger` + `text-danger-fg` (badges, buttons, and the Agents page meter
 * labels) trusts these pairs blindly — nothing in CSS can notice that a theme
 * paired white text with a pale fill. Two properties are pinned per pair:
 *
 * 1. **4.5:1 floor.** These labels are 12–13px, below WCAG's large-text size,
 *    so AA applies at 4.5:1 rather than 3:1.
 * 2. **No worse than `--text-strong`.** The meter labels previously painted
 *    `--text-strong` over the fill regardless. Reading the token is only an
 *    improvement if the token beats what it replaced — otherwise a theme is
 *    quietly made LESS readable by "fixing" it. This is the assertion that
 *    caught rosepine-light (`#d7827e` + white = 2.84:1, against 5.34:1 for
 *    text-strong) and kiro-light's undeclared `--danger-fg` (4.30:1 inherited
 *    from the dark defaults, since `:root` matches `<html>` in every theme).
 *
 * The CSS is parsed rather than rendered because jsdom resolves no cascade: it
 * would report every var as empty and the whole suite would pass vacuously.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { relativeLuminance, contrastRatio, parseCssColor } from '../lib/iconContrast'

const CSS = readFileSync(resolve(__dirname, '../index.css'), 'utf8')

/** Top-level rules, in document order: `[selector list] { decls }`. */
const RULES: { selectors: string[]; decls: Map<string, string> }[] = []
for (const m of CSS.matchAll(/^([^{}\n][^{}]*)\{([^{}]*)\}/gm)) {
  const decls = new Map<string, string>()
  for (const d of m[2].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) decls.set(d[1], d[2].trim())
  RULES.push({ selectors: m[1].split(',').map((s) => s.trim()), decls })
}

const THEMES = [...new Set([...CSS.matchAll(/\[data-theme="([^"]+)"\]/g)].map((m) => m[1]))].sort()

/**
 * The value a theme actually gets for `prop`.
 *
 * A theme that omits a token does NOT fall back to nothing: the base rule is
 * keyed on `:root`, which matches `<html>` whatever the theme is, so the
 * dark-default value applies. Equal specificity means later wins.
 */
function resolveVar(theme: string, prop: string): string | undefined {
  let value: string | undefined
  for (const rule of RULES) {
    const hit = rule.selectors.some((s) => s === ':root' || s === `[data-theme="${theme}"]`)
    if (hit && rule.decls.has(prop)) value = rule.decls.get(prop)
  }
  return value
}

function lum(css: string | undefined): number | null {
  if (!css) return null
  const c = parseCssColor(css)
  return c ? relativeLuminance(c.r, c.g, c.b) : null
}

const TIERS = ['accent', 'warn', 'danger'] as const
const AA_SMALL_TEXT = 4.5

/*
 * SCOPE: the three tiers the meter bars paint. `index.css` declares four more
 * `-fg` pairs (`ok`, `info`, `aim`, `muted`) with their own consumers, and they
 * are NOT covered here — deliberately, not by oversight. Measured at the time
 * this test was written: extending it would red `amoled-grey-calm-light` ok
 * (3.30:1), `amoled-midnight-light` ok (3.77:1) and `amoled-light` aim (4.13:1),
 * and `muted` fails in 20+ themes (`highcontrast-light` measures 1.32:1). Those
 * are real defects in their own right, but fixing them means editing tokens no
 * meter bar reads, so they belong to their own change rather than riding along
 * with this one.
 */

describe('theme fill/foreground pairs', () => {
  it('covers every theme in the stylesheet', () => {
    // A guard on the guard: a themes list that silently went empty would make
    // every assertion below pass without testing anything.
    expect(THEMES.length).toBeGreaterThanOrEqual(30)
    expect(THEMES).toContain('dracula-light')
    expect(THEMES).toContain('kiro-light')
  })

  for (const theme of THEMES) {
    for (const tier of TIERS) {
      it(`${theme}: --${tier}-fg is readable on --${tier}`, () => {
        const fill = lum(resolveVar(theme, `--${tier}`))
        const fg = lum(resolveVar(theme, `--${tier}-fg`))
        const strong = lum(resolveVar(theme, '--text-strong'))
        // Unparseable means the assertions below cannot mean anything.
        expect(fill, `--${tier} unresolved`).not.toBeNull()
        expect(fg, `--${tier}-fg unresolved`).not.toBeNull()
        expect(strong, '--text-strong unresolved').not.toBeNull()

        const onFill = contrastRatio(fill as number, fg as number)
        const onFillStrong = contrastRatio(fill as number, strong as number)
        expect(onFill).toBeGreaterThanOrEqual(AA_SMALL_TEXT)
        expect(onFill).toBeGreaterThanOrEqual(onFillStrong)
      })
    }
  }
})

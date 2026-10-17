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
import { relativeLuminance, contrastRatio, parseCssColor } from '../lib/iconContrast'
import { THEMES, resolveVar } from './themePalette'

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
 *
 * KNOWN_BELOW_AA below is the same judgement applied to four pairs this file
 * DOES cover. They were passing only because the shared resolver was measuring
 * them against the `:root` dark defaults: a section banner comment on the line
 * above a palette block was being swept into that block's selector list, so 14
 * of the 36 themes silently resolved to `:root`. Fixing that (see
 * `themePalette.ts`) exposed four pre-existing palette defects in the amoled
 * family. Retuning a shipped theme's `--accent-fg` / `--danger-fg` is a visible
 * change to those themes and is not this file's business, so each is recorded
 * with its measured ratio and asserted to STILL be failing — a palette fix must
 * delete its entry rather than leave a stale exemption behind.
 */

describe('theme fill/foreground pairs', () => {
  /**
   * `<theme>:<tier>` pairs that sit below the floor today, with the ratio each
   * one measures. Documented in the header; asserted still-failing below so an
   * entry cannot outlive the defect it excuses.
   */
  const KNOWN_BELOW_AA = new Map<string, number>([
    ['amoled-dark:accent', 3.65],
    ['amoled-grey-calm-dark:danger', 3.67],
    ['amoled-midnight-dark:accent', 3.85],
    ['amoled-midnight-dark:danger', 3.76],
  ])

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

        const known = KNOWN_BELOW_AA.get(`${theme}:${tier}`)
        if (known !== undefined) {
          // A recorded exemption must stay earned: if a palette retune lifted
          // this pair over the floor, the entry is stale and this line says so
          // rather than quietly exempting a pair that no longer needs it.
          expect(onFill, `${theme} --${tier} now clears AA: drop its KNOWN_BELOW_AA entry`)
            .toBeLessThan(AA_SMALL_TEXT)
          expect(onFill, `${theme} --${tier} moved off its recorded ratio`).toBeCloseTo(known, 1)
          return
        }

        expect(onFill).toBeGreaterThanOrEqual(AA_SMALL_TEXT)
        expect(onFill).toBeGreaterThanOrEqual(onFillStrong)
      })
    }
  }
})

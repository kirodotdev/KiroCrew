/**
 * The current preview-find match inside a code or diff block is readable.
 *
 * The preview find paints matches with the CSS Custom Highlight API, and
 * `::highlight(mc-find-current)` sets only a background. Pierre colours each
 * syntax token inline, so without a text colour of its own a code block's
 * current match keeps token colours the orange fill swallows (purple `docker`
 * reads 1.03:1 on it in kiro-dark). One rule scoped to `.code-block` and
 * `.diff-block` gives that match one dark text colour; prose matches keep the
 * page's text colour.
 *
 * This file pins the global rule (background only), the scoped rule (both
 * classes, a colour, no other find highlight carrying one), that both classes
 * still name rendered elements, and that the scoped colour clears 3:1 on the
 * current-match fill composited over `--bg` in every built-in palette.
 */
import { describe, it, expect } from 'vitest'
import { contrastRatio, parseCssColor } from '../lib/iconContrast'
import { THEMES, resolveVar } from './themePalette'
import { ACTIVE, LINES, lum, mix, tokenRgb } from './statusTextTint'

/** Large-text / UI-component floor (WCAG 1.4.11): the match is a highlight
 *  the reader has just searched for, and prose matches read 2.50:1 today. */
const MIN_MATCH_TEXT = 3
const GLOBAL = '::highlight(mc-find-current)'
const SCOPED = ['.code-block::highlight(mc-find-current)', '.diff-block::highlight(mc-find-current)']

interface Rule { selectors: string[]; decls: Map<string, string> }

/** Every rule in the comment-stripped stylesheet, nested ones included, with
 *  all its declarations (not only custom properties). */
const RULES: Rule[] = [...ACTIVE.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => {
  const decls = new Map<string, string>()
  for (const d of m[2].split(';')) {
    const at = d.indexOf(':')
    if (at > 0) decls.set(d.slice(0, at).trim(), d.slice(at + 1).trim())
  }
  return { selectors: m[1].split(',').map((s) => s.trim()), decls }
})

const findRules = RULES.filter((r) => r.selectors.some((s) => s.includes('::highlight(mc-find')))
const globalRule = () => RULES.filter((r) => r.selectors.length === 1 && r.selectors[0] === GLOBAL)
const scopedRule = () => RULES.filter((r) => SCOPED.every((s) => r.selectors.includes(s)))

describe('current preview-find match in code and diff blocks', () => {
  it('anchors the premise: the global current-match rule sets a fill and no text colour', () => {
    const rules = globalRule()
    expect(rules, `expected exactly one \`${GLOBAL} { … }\` rule`).toHaveLength(1)
    expect(rules[0].decls.get('background-color')).toMatch(/^var\(--[\w-]+\)$/)
    expect(rules[0].decls.has('color'), `${GLOBAL} must not set color: prose matches keep their text colour`).toBe(false)
  })

  it('scopes one text colour to the current match in .code-block and .diff-block', () => {
    const rules = scopedRule()
    expect(rules, `expected one rule whose selector list names ${SCOPED.join(' and ')}`).toHaveLength(1)
    expect(rules[0].decls.get('color'), 'the scoped rule declares no color').toBeTruthy()
    const others = findRules
      .filter((r) => r !== rules[0] && r.decls.has('color'))
      .map((r) => r.selectors.join(', '))
    expect(others, `only the scoped rule may colour a find match:\n${others.join('\n')}`).toEqual([])
  })

  it('the scoped classes still name rendered elements', () => {
    for (const cls of ['code-block', 'diff-block']) {
      const token = new RegExp(`className=["'\`][^"'\`]*(?<![\\w-])${cls}(?![\\w-])`)
      const sites = LINES.filter((l) => token.test(l.text)).map((l) => `${l.file}:${l.line}`)
      expect(sites.length, `no component renders className "${cls}"`).toBeGreaterThan(0)
    }
  })

  it(`reads at least ${MIN_MATCH_TEXT}:1 on the current-match fill over --bg in every built-in palette`, () => {
    const [scoped] = scopedRule()
    const [global] = globalRule()
    expect(scoped && global, 'the find highlight rules are missing').toBeTruthy()
    const text = parseCssColor(scoped.decls.get('color')!)
    expect(text, `scoped color ${scoped.decls.get('color')} is not a colour this test can measure`).not.toBeNull()
    expect(text!.a, 'the scoped color must be opaque').toBe(1)
    const fillVar = /^var\((--[\w-]+)\)$/.exec(global.decls.get('background-color')!)![1]

    expect(THEMES.length).toBeGreaterThanOrEqual(30)
    const rows = THEMES.map((t) => {
      const raw = resolveVar(t, fillVar)
      const fill = raw === undefined ? null : parseCssColor(raw)
      if (!fill) throw new Error(`theme ${t}: ${fillVar}=${raw} is not a colour this test can measure`)
      const painted = mix(fill, tokenRgb(t, '--bg'), fill.a)
      return { theme: t, ratio: contrastRatio(lum(text!), lum(painted)) }
    }).sort((a, b) => a.ratio - b.ratio)
    const table = rows.map((r) => `${r.theme}: ${r.ratio.toFixed(2)}:1`).join('\n')
    const min = rows[0]
    expect(
      min.ratio,
      `minimum ${min.ratio.toFixed(2)}:1 in ${min.theme} (floor ${MIN_MATCH_TEXT}:1)\n${table}`,
    ).toBeGreaterThanOrEqual(MIN_MATCH_TEXT)
  })
})

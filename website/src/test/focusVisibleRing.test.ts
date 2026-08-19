/**
 * The keyboard focus ring must stay live, and must stay keyed on
 * `:focus-visible`.
 *
 * Why this is a test rather than a rule someone can read: the global ring is a
 * single declaration in a 2000-line stylesheet, it is invisible to every
 * pointer-driven click-through, and no other test fails when it is gone. The
 * ring can therefore be commented out — while debugging something unrelated,
 * say — and survive indefinitely, leaving keyboard and screen-reader users with
 * no indication of where focus sits (WCAG 2.4.7 Focus Visible, Level AA).
 *
 * The second assertion guards the opposite failure. A ring keyed on `:focus`
 * paints for pointer clicks too, which reads as a visual defect and invites
 * deleting the ring instead of narrowing its selector. `:focus-visible` is the
 * mechanism that separates the two audiences per interaction, so nothing in
 * this stylesheet should style focus through bare `:focus`.
 *
 * Comments are blanked (not removed) before matching, so a commented-out rule
 * cannot satisfy the first assertion while line numbers stay accurate for the
 * second.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

const INDEX_CSS_PATH = join(__dirname, '..', 'index.css')

/** Comment bodies replaced by spaces: inert to matching, line-preserving. */
const ACTIVE_CSS = readFileSync(INDEX_CSS_PATH, 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  (comment) => comment.replace(/[^\n]/g, ' '),
)

describe('keyboard focus ring', () => {
  it('declares a global :focus-visible outline that is not commented out', () => {
    const rule = /(?:^|[};])\s*:focus-visible\s*\{([^}]*)\}/m.exec(ACTIVE_CSS)
    expect(
      rule,
      'no active global `:focus-visible{...}` rule in index.css — if it is ' +
        'commented out, restore it rather than leaving keyboard users without ' +
        'a focus indicator',
    ).not.toBeNull()

    const outline = /outline\s*:\s*([^;}]+)/.exec(rule![1])
    expect(outline, 'the global :focus-visible rule sets no outline').not.toBeNull()
    expect(
      outline![1].trim(),
      'the global :focus-visible outline is switched off',
    ).not.toMatch(/^(none|0)\b/)
  })

  it('styles focus through :focus-visible only, never bare :focus', () => {
    const offenders = ACTIVE_CSS.split('\n')
      .map((line, i) => ({ line, n: i + 1 }))
      .filter(({ line }) => /:focus(?![-\w])/.test(line))
      .map(({ line, n }) => `index.css:${n}: ${line.trim()}`)

    expect(
      offenders,
      'bare `:focus` styles a ring for pointer clicks too; use ' +
        '`:focus-visible` so only keyboard and AT interactions paint one',
    ).toEqual([])
  })
})

/**
 * Session-row COLOUR BAR geometry, guarded against drift in `index.css`.
 *
 * Two properties matter and neither is visible to a jsdom render, because both
 * are computed by layout from a pseudo-element:
 *   1. the bar is FLUSH with the row's left edge (`left:0`) — it identifies the
 *      session at the container's own boundary, not floating inside it,
 *   2. its length is (row height - 2 * corner radius), expressed as a vertical
 *      inset of `var(--radius-md)` — the same token the row's `rounded-md`
 *      resolves to, so the bar's ends track a theme that redefines the radius
 *      instead of stopping at a hardcoded pixel value that no longer matches
 *      the corner.
 *
 * A rule-text guard is the right instrument here: the relationship is the point,
 * and a computed-style test would happily pass on a hardcoded `top:8px` that
 * silently breaks for any theme pack shipping a different radius.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/** Declarations of the first rule whose selector matches, `{}`-block aware.
 *
 *  Comments are stripped FIRST: a `/* … *\/` block immediately above a rule is
 *  captured as part of that rule's selector text by the block regex, so an
 *  un-stripped match never equals the bare selector. */
function declarationsFor(css: string, selector: string): string {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const rules = [...bare.matchAll(/([^{}]*)\{([^{}]*)\}/g)]
  const hit = rules.find(r => r[1].trim() === selector)
  if (!hit) throw new Error(`no rule for selector ${selector}`)
  return hit[2].replace(/\s+/g, '')
}

describe('session row — colour bar geometry', () => {
  const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')

  // Both the plain bar and the bar-plus-gradient decoration draw the same bar,
  // so both must agree about where it sits; a fix applied to one only would show
  // as the bar jumping when a user switches colour mode.
  const selectors = [
    '.session-row.session-colored::before',
    '.session-row.session-colored.session-gradient::before',
  ]

  it.each(selectors)('sits flush against the row\'s left edge — %s', sel => {
    expect(declarationsFor(css, sel)).toContain('left:0')
  })

  it.each(selectors)('insets vertically by the row\'s corner radius token — %s', sel => {
    const decls = declarationsFor(css, sel)
    expect(decls).toContain('top:var(--radius-md)')
    expect(decls).toContain('bottom:var(--radius-md)')
    // A hardcoded pixel inset is the regression this guards against.
    expect(decls).not.toMatch(/top:\d+px/)
    expect(decls).not.toMatch(/bottom:\d+px/)
  })

  it.each(selectors)('renders the bar as a pill — %s', sel => {
    expect(declarationsFor(css, sel)).toMatch(/border-radius:999px/)
  })

  it('resolves --radius-md to a real length in every theme block that sets it', () => {
    // The inset is only meaningful if the token exists; an undefined var would
    // collapse `top` to its initial value and stretch the bar corner to corner.
    const matches = [...css.matchAll(/--radius-md:\s*([^;]+);/g)].map(m => m[1].trim())
    expect(matches.length).toBeGreaterThan(0)
    for (const v of matches) expect(v).toMatch(/^\d+(\.\d+)?(px|rem)$/)
  })
})

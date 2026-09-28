/**
 * Render bounds for the shipped panel template.
 *
 * The store caps published data at 64 KiB, which bounds the INPUT. It does not
 * bound what the template DRAWS: table headers are derived from the union of keys
 * across rows, so rows with disjoint keys produce `rows x union(keys)` cells --
 * quadratic in the payload. A ~55 KiB document of a few thousand uniquely-keyed
 * rows therefore passes every server-side validation and still hangs the tab.
 *
 * These tests execute the real `default.html` script in jsdom against a
 * deliberately SPARSE fixture, so the bound is pinned by behaviour rather than
 * remembered. A grep for the cap constants would pass while the loop that must
 * respect them was rewritten.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const TEMPLATE = join(
  __dirname,
  '../../../src/kiro_crew/agent_panel_templates/default.html',
)

/**
 * The template's inline script.
 *
 * PARSED, not sliced. Reading it by locating `'<script>'` and doing offset
 * arithmetic is both the shape semgrep's `unknown-value-with-script-tag` rule
 * keys on and a worse way to ask the question: `DOMParser` is what the browser
 * itself does with this file, so the element it finds is the element that will
 * actually run.
 */
function templateScript(): string {
  const html = readFileSync(TEMPLATE, 'utf8')
  const parsed = new DOMParser().parseFromString(html, 'text/html')
  const scripts = [...parsed.querySelectorAll('script')]
    .map(s => s.textContent || '')
    .filter(src => src.trim() !== '')

  // Exactly one, so a template that grew a second island cannot have half of it
  // silently untested.
  expect(scripts, 'expected one non-empty inline script in the template').toHaveLength(1)
  return scripts[0]
}

/** Mount the fixture the way the composed document does, then run the template. */
function render(data: unknown): void {
  const holder = document.createElement('script')
  holder.type = 'application/json'
  holder.id = 'kirocrew-panel-data'
  holder.textContent = JSON.stringify(data)

  const root = document.createElement('div')
  root.id = 'kp-root'

  // `replaceChildren`, never `innerHTML`: the repo forbids assigning it anywhere
  // under src/, test code included, and this file of all files should not be the
  // exception -- it exists to check that the template never parses markup either.
  document.body.replaceChildren(holder, root)

  new Function(templateScript())()
}

/**
 * Rows sharing no keys at all -- the worst case for a derived header.
 *
 * Every row contributes a brand-new column, so an uncapped union makes the cell
 * count the PRODUCT of the two axes rather than the sum.
 */
function disjointRows(n: number): Array<Record<string, number>> {
  return Array.from({ length: n }, (_, i) => ({ [`k${i}`]: i }))
}

beforeEach(() => {
  // Not `innerHTML = ''` -- see the note in `render`. `replaceChildren()` with no
  // arguments is the same clear without an assignment the repo rule forbids.
  document.body.replaceChildren()
})

describe('the shipped template bounds what it draws', () => {
  it('does not multiply rows by a derived header', () => {
    render({ rows: disjointRows(4000) })

    const cells = document.querySelectorAll('#kp-root td').length
    // 4000 disjoint rows x 4000 derived columns = 16,000,000 cells uncapped.
    expect(cells).toBeLessThanOrEqual(100 * 20)
    expect(cells).toBeGreaterThan(0)
  })

  it('caps the number of unique columns', () => {
    render({ rows: disjointRows(500) })
    expect(document.querySelectorAll('#kp-root th').length).toBeLessThanOrEqual(20)
  })

  it('caps the number of rows', () => {
    // Rows that SHARE keys, so only the row axis is over the cap.
    const rows = Array.from({ length: 900 }, (_, i) => ({ cycle: i, state: 'idle' }))
    render({ rows })
    // Header row is a <tr> too, hence the +1.
    expect(document.querySelectorAll('#kp-root tr').length).toBeLessThanOrEqual(100 + 1)
  })

  it('says so when it truncates, rather than dropping data silently', () => {
    render({ rows: disjointRows(4000) })

    const notice = document.querySelector('#kp-root .kp-note')?.textContent ?? ''
    expect(notice).toMatch(/\b4000\b/) // the real total, not just the shown count
    expect(notice.toLowerCase()).toContain('rows')
    expect(notice.toLowerCase()).toContain('columns')
  })

  it('leaves a table within both bounds completely alone', () => {
    const rows = [
      { cycle: 1, state: 'idle' },
      { cycle: 2, state: 'busy' },
    ]
    render({ rows })

    expect(document.querySelectorAll('#kp-root tr').length).toBe(3) // header + 2
    expect(document.querySelectorAll('#kp-root td').length).toBe(4)
    // No notice, because nothing was withheld -- a truncation notice on a
    // complete table would be a lie in the other direction.
    expect(document.querySelector('#kp-root .kp-note')).toBeNull()
  })

  it('still renders an absent cell as a visible nil, not an empty one', () => {
    // Sparseness WITHIN the cap must stay legible: this is the property the
    // truncation notice is consistent with.
    render({ rows: [{ a: 1 }, { b: 2 }] })
    expect(document.querySelectorAll('#kp-root .kp-nil').length).toBe(2)
  })
})

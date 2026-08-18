import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * The webhook tokens table is an AUTO-layout table whose six columns cannot
 * reflow below ~560px (the page's own comment concedes it), so Revoke — the
 * last column — starts past the scroll edge on a phone and revoking a
 * credential costs a horizontal scroll behind a hidden scrollbar. The fix pins
 * the trailing cells (header + body) `sticky right-0` on an opaque `bg-bg`
 * (the detail pane's surface — this table sits on the pane, not in a Card),
 * with a seam (1px child div + `right-full` fade) gated on the MEASURED
 * overflow flag — the treatment PR #4097 established, adapted for auto layout
 * where a wrapper-anchored cue cannot know the pinned column's edge. The seam
 * is a CHILD DIV and not `border-l`: under `border-collapse: collapse` a cell
 * border belongs to the collapsed table grid and paints at the cell's layout
 * slot, so it stays behind while the sticky cell travels.
 *
 * Load-bearing parts a later edit could lose separately: the pin + opaque base
 * on BOTH cells, the overflow gate on the seam, the zebra overlay (these rows
 * carry no hover tint, so the overlay mirrors only `.table-striped`'s
 * `--card-hl` on even rows), and the observed content node (auto layout means
 * the rows set scrollWidth, which the scroller's own box never reports).
 *
 * Comments are stripped before matching — the rationale in the page quotes the
 * class names being asserted.
 */
const PAGE = join(__dirname, '..', 'pages', 'WebhooksPage.tsx')

const loadSource = async () => {
  const raw = await readFile(PAGE, 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\{\/\*[\s\S]*?\*\/\}/g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('WebhooksPage tokens table sticky Revoke column', () => {
  it('measures the real scroller and observes the auto-layout table', async () => {
    const src = await loadSource()
    expect(src).toMatch(/<div ref=\{attachTokensScroller\} className="mt-3 -mx-1 px-1 overflow-x-auto">/)
    expect(src).toMatch(/<table ref=\{attachTokensTable\} className="w-full min-w-max text-\[13px\] border-collapse table-striped">/)
  })

  it('pins the trailing header cell on the pane surface with a gated seam', async () => {
    const src = await loadSource()
    const header = src.match(/<th className="([^"]*)">\s*\{tokensTableEdges\.right &&/)
    expect(header, 'the trailing <th> moved or changed shape').toBeTruthy()
    const cls = header![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls, 'the tokens table sits on the pane, not in a Card').toContain('bg-bg')
  })

  it('pins the Revoke body cell with the zebra overlay', async () => {
    const src = await loadSource()
    const cell = src.match(/<td className="([^"]*)">\s*<div aria-hidden className=\{`absolute inset-0 -z-10/)
    expect(cell, 'the Revoke <td> moved or changed shape').toBeTruthy()
    const cls = cell![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-bg')
    // No hover tint on these rows — the overlay mirrors only the zebra.
    const overlay = src.match(/<div aria-hidden className=\{`absolute inset-0 -z-10 ([^`]*)`\} \/>/)
    expect(overlay, 'the zebra overlay is gone from the Revoke cell').toBeTruthy()
    expect(overlay![1]).toContain("i % 2 === 1 ? 'bg-[var(--card-hl)]' : ''")
  })

  it('paints the seam and fade cues only while columns are hidden', async () => {
    const src = await loadSource()
    const seams = src.match(/\{tokensTableEdges\.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" \/>\}/g)
    expect(seams?.length, 'both pinned cells carry the gated 1px seam child (a border-l never travels under border-collapse)').toBe(2)
    const fades = src.match(/\{tokensTableEdges\.right && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-bg to-transparent" \/>\}/g)
    expect(fades?.length, 'both pinned cells hang the gated right-full fade, blending into the pane surface').toBe(2)
  })
})

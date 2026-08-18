import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * The hooks table is an AUTO-layout table whose declared column widths exceed a
 * phone (and a rail-narrowed desktop pane), so Actions — the last column —
 * starts past the scroll edge at rest and Test/Edit/Delete cost a horizontal
 * scroll. The fix pins the Actions cells (header + body) `sticky right-0` on an
 * opaque background, with a seam (1px child div + `right-full` fade) gated on
 * the MEASURED overflow flag — same treatment as the Schedule jobs table,
 * adapted for auto layout where a wrapper-anchored cue cannot know the
 * pinned column's edge. The seam is a CHILD DIV and not `border-l`: under
 * Preflight's `border-collapse: collapse` a cell border belongs to the
 * collapsed table grid and paints at the cell's layout slot, so it stays behind
 * while the sticky cell travels.
 *
 * Load-bearing parts a later edit could lose separately:
 * 1. `sticky right-0` + `bg-card` on BOTH cells (transparent cells show the
 *    scrolling columns through the pin).
 * 2. The overflow gate on the seam (a permanent seam lies on a table that fits).
 * 3. The row-state overlay: even rows mirror `.table-striped`'s `--card-hl`
 *    zebra, odd rows mirror the hover tint via the named row group — losing it
 *    makes the pinned cell ignore zebra/hover while the rest of the row paints.
 * 4. The observed content node: auto layout means the rows set scrollWidth,
 *    which a ResizeObserver on the scroller's own box never reports, so
 *    without it the seam goes stale when the rendered content changes width.
 *
 * Comments are stripped before matching — the rationale in the page quotes the
 * class names being asserted.
 */
const PAGE = join(__dirname, '..', 'pages', 'HooksPage.tsx')

const loadSource = async () => {
  const raw = await readFile(PAGE, 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\{\/\*[\s\S]*?\*\/\}/g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('HooksPage table sticky Actions column', () => {
  it('measures the real scroller', async () => {
    const src = await loadSource()
    expect(src).toMatch(/<div ref=\{attachHooksScroller\} className="overflow-x-auto">/)
  })

  it('pins the Actions header cell with an overflow-gated seam', async () => {
    const src = await loadSource()
    const header = src.match(/<th className="([^"]*)">\s*\{hooksTableEdges\.right &&/)
    expect(header, 'the Actions <th> moved or changed shape').toBeTruthy()
    const cls = header![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
  })

  it('pins the Actions body cell with the row-state overlay and gated seam', async () => {
    const src = await loadSource()
    const cell = src.match(/<td aria-label=\{i18nT\('pages\.hooksPage\.actions'\)\} className="([^"]*)">/)
    expect(cell, 'the Actions <td> moved or changed shape').toBeTruthy()
    const cls = cell![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
    // The row names the group the overlay listens to…
    expect(src).toMatch(/<tr key=\{h\.id\} className=\{`group\/hookrow /)
    // …and the overlay mirrors zebra on even rows, hover on odd rows.
    const overlay = src.match(/<div aria-hidden className=\{`absolute inset-0 -z-10 ([^`]*)`\} \/>/)
    expect(overlay, 'the row-state overlay is gone from the Actions cell').toBeTruthy()
    expect(overlay![1]).toContain("i % 2 === 1 ? 'bg-[var(--card-hl)]' : 'group-hover/hookrow:bg-bg-hover'")
  })

  it('paints the seam and fade cues only while columns are hidden', async () => {
    const src = await loadSource()
    // Scope to each sticky cell's OWN markup: a seam div counted globally
    // could drift outside the cell and still pass, recreating the defect.
    const header = src.match(/<th className="[^"]*sticky[^"]*">([\s\S]*?)<\/th>/)
    const body = src.match(/<td aria-label=\{i18nT\('pages\.hooksPage\.actions'\)\}[\s\S]*?<\/td>/)
    expect(header, 'the sticky Actions <th> moved or changed shape').toBeTruthy()
    expect(body, 'the sticky Actions <td> moved or changed shape').toBeTruthy()
    // The seam is the full literal: a seam that loses top-0/bottom-0 spans
    // only the text line, a distinct visual regression from losing it whole.
    const seamRe = /\{hooksTableEdges\.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" \/>\}/
    expect(header![1], 'the header cell carries its gated 1px seam child (a border-l never travels under border-collapse)').toMatch(seamRe)
    expect(body![0], 'the body cell carries its gated 1px seam child').toMatch(seamRe)
    const headerFade = header![1].match(/\{hooksTableEdges\.right && <div aria-hidden="true" className="([^"]*)" \/>\}/g)?.filter(c => c.includes('right-full')) ?? []
    expect(headerFade.length, 'the header cell hangs its gated right-full fade just left of the pin, auto-layout safe').toBe(1)
    expect(headerFade[0]).toContain('pointer-events-none')
    expect(headerFade[0]).toContain('from-card')
    // The body fade ramps toward the surface it abuts: hover tint on odd rows
    // (zebra outranks hover on even rows, so those keep from-card).
    const bodyFade = body![0].match(/\{hooksTableEdges\.right && <div aria-hidden="true" className=\{`([^`]*)`\} \/>\}/)
    expect(bodyFade, 'the body cell fade is gone or lost its row-state conditional').toBeTruthy()
    expect(bodyFade![1]).toContain('pointer-events-none')
    expect(bodyFade![1]).toContain('right-full')
    expect(bodyFade![1]).toContain('from-card')
    expect(bodyFade![1], 'a hovered odd row repaints to bg-hover; a fade still ramping toward card is a visible band')
      .toContain("i % 2 === 1 ? '' : 'group-hover/hookrow:from-bg-hover'")
  })

  it('observes the table as the content node', async () => {
    const src = await loadSource()
    // Auto layout: the ROWS set scrollWidth — filtering, a locale switch, a
    // webfont load — none of which resize the scroller's own box. The table's
    // border-box IS the overflow driver, so the hook observes it directly.
    expect(src).toMatch(/\[attachHooksScroller, hooksTableEdges, , attachHooksTable\] = useScrollEdges/)
    expect(src).toMatch(/<table ref=\{attachHooksTable\} className="w-full border-collapse table-striped">/)
  })
})

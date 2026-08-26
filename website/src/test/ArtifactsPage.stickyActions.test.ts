import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * BOTH artifact library tables render LibraryTableHead (for the header) and
 * ArtifactRow (for a data row), so the Actions column is defined once and shared.
 * It is an AUTO-layout table whose declared column widths total past a phone
 * (and a rail-narrowed desktop pane), so Actions — the last column — starts past
 * the scroll edge at rest and every open/delete costs a horizontal scroll. The
 * fix pins the Actions cells (header + body) `sticky right-0` on an opaque
 * background, with a seam (1px child div + `right-full` fade) gated on the
 * MEASURED overflow flag (`edgeRight`) — same treatment as the hooks and
 * schedule tables, adapted for auto layout where a wrapper-anchored cue cannot
 * know the pinned column's left edge.
 *
 * The seam is a CHILD DIV and not `border-l`: under Preflight's
 * `border-collapse: collapse` a cell border belongs to the collapsed table grid
 * and paints at the cell's layout slot, so it stays behind while the sticky cell
 * travels.
 *
 * Load-bearing parts a later edit could lose separately:
 * 1. `sticky right-0` + `bg-card` on BOTH cells (transparent cells show the
 *    scrolling columns through the pin).
 * 2. The width contract on the header (`w-[120px]`, counted by the min-width).
 * 3. The overflow gate on the seam (a permanent seam lies on a table that fits).
 * 4. The row-state overlay: it mirrors `.table-striped`'s zebra via the
 *    ancestor `nth-child(even)` variant (the row's REAL DOM position, not a
 *    clean map index, because folder/artifact/lane rows interleave), and layers
 *    the drag-file highlight and hover tint on top — losing it makes the pinned
 *    cell ignore zebra/hover/drag while the rest of the row paints.
 * 5. The observed content node: auto layout means the rows set scrollWidth,
 *    which a ResizeObserver on the scroller's own box never reports, so the
 *    hook must observe the table's border-box directly (both consumers).
 *
 * Comments are stripped before matching — the rationale in the page quotes the
 * class names being asserted.
 */
const PAGE = join(__dirname, '..', 'pages', 'ArtifactsPage.tsx')

const loadSource = async () => {
  const raw = await readFile(PAGE, 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\{\/\*[\s\S]*?\*\/\}/g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

const SEAM = /\{edgeRight && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" \/>\}/
const FADE = /\{edgeRight && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" \/>\}/

describe('ArtifactsPage library tables sticky Actions column', () => {
  it('pins the shared Actions header cell on an opaque background, keeping its width', async () => {
    const src = await loadSource()
    const header = src.match(/<th className=\{`\$\{th\} ([^`]*)`\}>\s*\{edgeRight &&/)
    expect(header, 'the Actions <th> in LibraryTableHead moved or changed shape').toBeTruthy()
    const cls = header![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
    expect(cls, 'w-[120px] is counted by the table min-width arithmetic').toContain('w-[120px]')
  })

  it('gates the header seam and fade on the measured overflow flag', async () => {
    const src = await loadSource()
    const header = src.match(/<th className=\{`\$\{th\}[^`]*sticky[^`]*`\}>([\s\S]*?)<\/th>/)
    expect(header, 'the sticky Actions <th> moved or changed shape').toBeTruthy()
    expect(header![1], 'the header cell carries its gated 1px seam child (a border-l never travels under border-collapse)').toMatch(SEAM)
    expect(header![1], 'the header cell hangs its gated right-full fade just left of the pin').toMatch(FADE)
  })

  it('pins the shared Actions body cell with the row-state overlay', async () => {
    const src = await loadSource()
    const cell = src.match(/<td className="(sticky[^"]*)">\s*<div aria-hidden/)
    expect(cell, 'the Actions <td> in ArtifactRow moved or changed shape').toBeTruthy()
    const cls = cell![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
    // The row names the group the overlay's hover tint listens to.
    expect(src).toMatch(/className=\{`group\/artrow /)
    // The overlay mirrors zebra via the row's REAL DOM position (interleaved
    // rows rule out a clean index) and layers drag-highlight / hover on top.
    const overlay = src.match(/<div aria-hidden className=\{`absolute inset-0 -z-10 ([^`]*)`\} \/>\s*\{edgeRight/)
    expect(overlay, 'the row-state overlay is gone from the Actions cell').toBeTruthy()
    expect(overlay![1], 'the overlay must mirror the .table-striped zebra by DOM position')
      .toContain('[.table-striped_tbody_tr:nth-child(even)_&]:bg-[var(--card-hl)]')
    expect(overlay![1], 'the overlay must layer the drag-file highlight and hover tint')
      .toContain("dropHighlight ? 'bg-accent/10' : 'group-hover/artrow:bg-bg-hover'")
  })

  it('gates the body seam and fade on the measured overflow flag', async () => {
    const src = await loadSource()
    const body = src.match(/<td className="sticky[^"]*">[\s\S]*?<\/td>/)
    expect(body, 'the sticky Actions <td> moved or changed shape').toBeTruthy()
    expect(body![0], 'the body cell carries its gated 1px seam child').toMatch(SEAM)
    expect(body![0], 'the body cell carries its gated right-full fade').toMatch(FADE)
  })

  it('drives the pin from the flag both tables pass down', async () => {
    const src = await loadSource()
    // The header and row take the flag as a prop, so ONE definition serves both
    // the flat library table and the folder-tree table.
    expect(src).toMatch(/edgeRight = false \}: \{ sort: SortState; onSort: \(key: SortKey\) => void; edgeRight\?: boolean \}/)
    expect(src).toMatch(/<LibraryTableHead sort=\{sort\} onSort=\{onSort\} edgeRight=\{edges\.right\} \/>/)
    expect(src).toMatch(/edgeRight=\{edges\.right\}/)
  })

  it('observes each table as the content node, not the scroller box', async () => {
    const src = await loadSource()
    // Auto layout: the ROWS set scrollWidth (filtering, a locale switch, a
    // webfont load), none of which resize the scroller's own box, so both
    // library tables observe the table's border-box directly.
    const consumers = src.match(/const \[attachScroller, edges, , attachTable\] = useScrollEdges<HTMLDivElement>\(\)/g) ?? []
    expect(consumers.length, 'both library tables wire the measured-overflow hook').toBe(2)
    expect(src).toMatch(/<table ref=\{attachTable\} className="w-full border-collapse table-striped">/)
  })
})

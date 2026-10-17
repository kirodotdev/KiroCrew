import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * The MCP servers table is an AUTO-layout table whose content routinely exceeds
 * a phone width (six columns, 180px name floor), so Actions — the last column —
 * starts past the scroll edge at rest and Edit/Uninstall cost a horizontal
 * scroll. The fix pins the Actions cells (header + body) `sticky right-0` on an
 * opaque `bg-card`, with a seam (1px child div + `right-full` fade) gated on
 * the MEASURED overflow flag — the treatment PR #4097 established for the
 * Schedule jobs table, adapted for auto layout where a wrapper-anchored cue
 * cannot know the pinned column's edge. The seam is a CHILD DIV and not
 * `border-l`: under `border-collapse: collapse` a cell border belongs to the
 * collapsed table grid and paints at the cell's layout slot, so it stays
 * behind while the sticky cell travels.
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
const PAGE = join(__dirname, '..', 'pages', 'overview', 'McpTab.tsx')

const loadSource = async () => {
  const raw = await readFile(PAGE, 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\{\/\*[\s\S]*?\*\/\}/g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('McpTab servers table sticky Actions column', () => {
  it('measures the real scroller and observes the auto-layout table', async () => {
    const src = await loadSource()
    expect(src).toMatch(/<div ref=\{attachMcpScroller\} className="overflow-x-auto">/)
    expect(src).toMatch(/<table ref=\{attachMcpTable\} className="w-full border-collapse table-striped">/)
  })

  it('pins the Actions header cell with an overflow-gated seam', async () => {
    const src = await loadSource()
    const header = src.match(/<th className="([^"]*)">\s*\{mcpTableEdges\.right &&/)
    expect(header, 'the Actions <th> moved or changed shape').toBeTruthy()
    const cls = header![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
  })

  it('pins the Actions body cell with the row-state overlay', async () => {
    const src = await loadSource()
    const cell = src.match(/<td className="([^"]*)">\s*<div aria-hidden className=\{`absolute inset-0 -z-10/)
    expect(cell, 'the Actions <td> moved or changed shape').toBeTruthy()
    const cls = cell![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
    // The row names the group the overlay listens to…
    expect(src).toMatch(/<tr key=\{s\.name\} className=\{`group\/mcprow hover:bg-bg-hover /)
    // …and the overlay mirrors zebra on even rows, hover on odd rows.
    const overlay = src.match(/<div aria-hidden className=\{`absolute inset-0 -z-10 transition-colors ([^`]*)`\} \/>/)
    expect(overlay, 'the row-state overlay is gone from the Actions cell').toBeTruthy()
    expect(overlay![1]).toContain("i % 2 === 1 ? 'bg-[var(--card-hl)]' : 'group-hover/mcprow:bg-bg-hover'")
  })

  it('paints the seam and fade cues only while columns are hidden', async () => {
    const src = await loadSource()
    const seamRe = /\{mcpTableEdges\.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" \/>\}/g
    expect(src.match(seamRe)?.length, 'both pinned cells carry the gated 1px seam child (a border-l never travels under border-collapse)').toBe(2)
    // The header fade blends into the card; the body fade ramps toward the
    // surface it abuts — hover tint on odd rows (zebra outranks hover on even
    // rows, so those keep from-card).
    expect(src).toMatch(/\{mcpTableEdges\.right && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" \/>\}/)
    const bodyFade = src.match(/\{mcpTableEdges\.right && <div aria-hidden="true" className=\{`([^`]*)`\} \/>\}/)
    expect(bodyFade, 'the body cell fade is gone or lost its row-state conditional').toBeTruthy()
    expect(bodyFade![1]).toContain('right-full')
    expect(bodyFade![1]).toContain('from-card')
    expect(bodyFade![1]).toContain("i % 2 === 1 ? '' : 'group-hover/mcprow:from-bg-hover'")
  })
})

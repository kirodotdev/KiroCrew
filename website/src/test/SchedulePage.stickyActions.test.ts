import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * The jobs table on SchedulePage is `table-fixed` with a min-width wider than a
 * phone, so Actions — the LAST of ten columns — starts past the scroll edge at
 * narrow widths and every Run/Delete costs a horizontal scroll. The fix pins the
 * Actions cell (header + body) with `sticky right-0` on an opaque background, so
 * row actions stay reachable while the other columns scroll under them.
 *
 * Three parts of that treatment are load-bearing and easy to lose separately:
 *
 * 1. `sticky right-0` on BOTH the header and body cell — dropping either one
 *    leaves half the column scrolling away.
 * 2. An OPAQUE background on the pinned cells. The default cell background is
 *    transparent, so without it the scrolling columns show through the pinned
 *    cell. `bg-card` is the Card surface the table sits on.
 * 3. The row-state overlay. The row's hover/selected tints live on the <tr>,
 *    which the opaque base hides under the pinned cell; the overlay re-applies
 *    the same tokens above the base. Losing it makes the pinned cell ignore
 *    hover and selection while the rest of the row highlights.
 *
 * Sticky changes paint position, not column width, so the `w-[176px]` width
 * declaration must survive alongside it (the table min-width arithmetic counts
 * it).
 *
 * Comments are stripped before matching — the rationale in the page quotes the
 * class names being asserted.
 */
const PAGE = join(__dirname, '..', 'pages', 'SchedulePage.tsx')

const loadSource = async () => {
  const raw = await readFile(PAGE, 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('SchedulePage jobs table sticky Actions column', () => {
  it('pins the Actions header cell on an opaque background, keeping its width', async () => {
    const src = await loadSource()
    const header = src.match(/<TableHead className="([^"]*)">\{i18nT\('pages\.schedulePage\.actions'\)\}/)
    expect(header, 'the Actions TableHead moved or changed shape').toBeTruthy()
    const cls = header![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
    expect(cls, 'w-[176px] is counted by the table min-width arithmetic').toContain('w-[176px]')
  })

  it('pins the Actions body cell on the same opaque background', async () => {
    const src = await loadSource()
    const cell = src.match(/<TableCell className="([^"]*)" onClick=\{e => e\.stopPropagation\(\)\}>\s*<div aria-hidden/)
    expect(cell, 'the Actions TableCell moved or changed shape').toBeTruthy()
    const cls = cell![1]
    expect(cls).toContain('sticky')
    expect(cls).toContain('right-0')
    expect(cls).toContain('bg-card')
  })

  it('paints the seam cue only while the scroller hides columns', async () => {
    const src = await loadSource()
    // The measurement must read the box the sticky cells resolve against — the
    // shadcn Table wrapper is the table's parentElement — through a STABLE ref
    // (an inline arrow detaches/reattaches every render and loops edge-state).
    expect(src).toMatch(/attachJobsScroller\(el\?\.parentElement \?\? null\)/)
    expect(src).toMatch(/<Table className="table-fixed[^"]*" ref=\{attachJobsTable\}>/)
    // The cue is gated on the MEASURED right-overflow flag, never painted
    // unconditionally: a permanent seam lies on a full-width desktop table.
    const cue = src.match(/\{jobsTableEdges\.right && \(\s*<div aria-hidden="true" data-testid="jobs-table-cue-right" className="([^"]*)"/)
    expect(cue, 'the pinned-edge seam cue is gone (or no longer overflow-gated)').toBeTruthy()
    const cls = cue![1]
    expect(cls).toContain('pointer-events-none')
    expect(cls, 'the cue anchors at the pinned column left edge').toContain('right-[176px]')
    expect(cls, 'the cue blends clipped content into the pinned cell surface').toContain('from-card')
  })

  it('re-applies the row hover/selected tints inside the pinned cell', async () => {
    const src = await loadSource()
    // The row must name the group the overlay listens to…
    expect(src).toMatch(/<TableRow key=\{j\.id\} className=\{`group\/jobrow /)
    // …and the overlay must mirror every row-state background: hover plus both
    // selection tints, with the same conditions the <tr> uses.
    const overlay = src.match(/<div aria-hidden className=\{`([^`]*)`\}/)
    expect(overlay, 'the row-state overlay is gone from the Actions cell').toBeTruthy()
    const cls = overlay![1]
    expect(cls).toContain('absolute inset-0')
    expect(cls).toContain('group-hover/jobrow:bg-bg-hover')
    expect(cls).toContain("selected?.id === j.id ? 'bg-accent-subtle' : ''")
    expect(cls).toContain("selectedIds.has(j.id) ? 'bg-accent-subtle/60' : ''")
  })
})

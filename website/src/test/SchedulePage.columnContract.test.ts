import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * The jobs table on SchedulePage is `table-fixed`, so its column widths are a
 * contract the browser does not renegotiate: a column that ends up with no width
 * does not shrink its content, it draws it over the next cell.
 *
 * Message is deliberately the ONE column with no declared width — it absorbs the
 * spare width, because a cron message has no natural length. That only works
 * while the OTHER nine columns cannot eat the spare width themselves, which is
 * what the first suite pins. With the earlier 15%/13%/12% widths the residual
 * was `0.6 x tableWidth - 540px`: zero at the table's own 900px min-width, so at
 * phone widths (and on a 1280px desktop with the nav rail open) the Message
 * chevron and preview rendered on top of the Status badge.
 *
 * The second suite pins the sticky Actions treatment: the table's
 * min-width is wider than a phone, so Actions — the LAST of ten columns —
 * starts past the scroll edge at narrow widths and every Run/Delete costs a
 * horizontal scroll. The fix pins the Actions cell (header + body) with
 * `sticky right-0` on an opaque background, so row actions stay reachable while
 * the other columns scroll under them. Three parts of that treatment are
 * load-bearing and easy to lose separately:
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
 * declaration must survive alongside it (the min-width arithmetic counts it).
 *
 * Comments are stripped before matching — the rationale in the page quotes the
 * class names being asserted, and a negative match would hit the prose.
 */
const PAGE = join(__dirname, '..', 'pages', 'SchedulePage.tsx')

const loadSource = async () => {
  const raw = await readFile(PAGE, 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

/**
 * Chevron (14px) + gap + a readable one-line preview, INCLUDING the cell's own
 * `p-2`: Tailwind's preflight makes every cell `border-box`, so a declared
 * width already contains its padding (verified against the built page — the ten
 * rendered column widths sum to the table width exactly).
 */
const MESSAGE_FLOOR = 176

const loadHeaderRow = async () => {
  const src = await loadSource()
  const start = src.indexOf('<Table className="table-fixed')
  const end = src.indexOf('</TableHeader>', start)
  expect(start, 'the jobs Table opening tag moved or changed shape').toBeGreaterThan(-1)
  expect(end, 'the jobs TableHeader moved or changed shape').toBeGreaterThan(start)
  return { src, header: src.slice(start, end) }
}

describe('SchedulePage jobs table column contract', () => {
  it('declares no percentage column width', async () => {
    const { header } = await loadHeaderRow()
    const pct = header.match(/w-\[\d+(?:\.\d+)?%\]/g) ?? []
    expect(pct, 'a percentage column grows with the table and starves the Message column').toEqual([])
  })

  it('leaves exactly one column — Message — without a width', async () => {
    const { header } = await loadHeaderRow()
    // Every header cell is either a TableHead or a SortableTableHead. Match
    // the OPENING TAG (widths live in the className attribute) plus its
    // immediate text child (which names the column); matching into element
    // children would truncate at the first self-closing child (the Actions
    // seam div, the checkbox input) and silently couple the count to child
    // order.
    const cells = header.match(/<(?:Sortable)?TableHead\b[^>]*>[^<]*/g) ?? []
    expect(cells.length, 'expected the ten jobs columns').toBe(10)
    const unsized = cells.filter(c => !/\bw-\[\d+px\]/.test(c))
    expect(unsized).toHaveLength(1)
    expect(unsized[0]).toContain('schedulePage.message')
  })

  it('reserves the Message floor in the table min-width', async () => {
    const { src, header } = await loadHeaderRow()
    const minW = src.match(/<Table className="table-fixed min-w-\[(\d+)px\]/)
    expect(minW, 'the table lost its min-width, so every column is squeezed').toBeTruthy()
    const declared = [...header.matchAll(/(?<!min-)\bw-\[(\d+)px\]/g)].map(m => Number(m[1]))
    expect(declared).toHaveLength(9)
    const residual = Number(minW![1]) - declared.reduce((sum, w) => sum + w, 0)
    expect(residual, `Message gets ${residual}px at the table's own min-width`)
      .toBeGreaterThanOrEqual(MESSAGE_FLOOR)
  })
})

describe('SchedulePage jobs table sticky Actions column', () => {
  it('pins the Actions header cell on an opaque background, keeping its width', async () => {
    const src = await loadSource()
    const header = src.match(/<TableHead className="([^"]*)">\s*\{jobsTableEdges\.right &&/)
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

  it('seams the pinned edge with a travelling child, gated on measured overflow', async () => {
    const src = await loadSource()
    // The seam is a CHILD DIV of each sticky cell, not `border-l`: under
    // Preflight's `border-collapse: collapse` a cell border belongs to the
    // collapsed table grid and paints at the cell's LAYOUT slot, so it stays
    // behind while the sticky cell travels. A child div rides along, and its
    // default z paints above the body cell's -z-10 row-state overlay.
    // Scope to each sticky cell's OWN markup: a seam div counted globally
    // could drift outside the cells and still pass, recreating the defect.
    const seamRe = /\{jobsTableEdges\.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" \/>\}/
    const header = src.match(/<TableHead className="[^"]*sticky[^"]*">([\s\S]*?)<\/TableHead>/)
    expect(header, 'the sticky Actions TableHead moved or changed shape').toBeTruthy()
    expect(header![1], 'the header cell carries its gated 1px seam child').toMatch(seamRe)
    const body = src.match(/<TableCell className="[^"]*sticky[^"]*" onClick=\{e => e\.stopPropagation\(\)\}>[\s\S]*?<\/TableCell>/)
    expect(body, 'the sticky Actions TableCell moved or changed shape').toBeTruthy()
    expect(body![0], 'the body cell carries its gated 1px seam child').toMatch(seamRe)
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

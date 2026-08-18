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
 * what this file pins. With the earlier 15%/13%/12% widths the residual was
 * `0.6 x tableWidth - 540px`: zero at the table's own 900px min-width, so at
 * phone widths (and on a 1280px desktop with the nav rail open) the Message
 * chevron and preview rendered on top of the Status badge.
 *
 * Comments are stripped before matching — the rationale in the page quotes the
 * class names being asserted, and a negative match would hit the prose.
 */
const PAGE = join(__dirname, '..', 'pages', 'SchedulePage.tsx')

/**
 * Chevron (14px) + gap + a readable one-line preview, INCLUDING the cell's own
 * `p-2`: Tailwind's preflight makes every cell `border-box`, so a declared
 * width already contains its padding (verified against the built page — the ten
 * rendered column widths sum to the table width exactly).
 */
const MESSAGE_FLOOR = 176

const loadHeaderRow = async () => {
  const raw = await readFile(PAGE, 'utf8')
  const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
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
    // Every header cell is either a TableHead or a SortableTableHead.
    const cells = header.match(/<(?:Sortable)?TableHead\b[\s\S]*?(?:\/>|<\/TableHead>)/g) ?? []
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

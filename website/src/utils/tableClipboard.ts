import type { Element as HastElement, ElementContent } from 'hast'

/**
 * Serialize a rendered markdown table back to text a reader can paste
 * somewhere else — a GFM table for a doc or another chat, CSV for a
 * spreadsheet.
 *
 * The source is the HAST node react-markdown hands the `table` override, NOT
 * the DOM. Two reasons. Alignment: remark-gfm records column alignment as an
 * `align` property on each cell, and `spa('th', node)` deliberately does not
 * forward it to the DOM, so the rendered `<th>` has no trace of `:---:`. And
 * chrome: Kiro Crew renders inline code as a click-to-copy chip with an icon
 * and a `role="button"`, so a `textContent` walk would have to know which
 * rendered decorations are content and which are UI. The hast tree is the
 * document as parsed, before any of that.
 *
 * Inline formatting that survives a round trip is restated (`code`, `strong`,
 * `em`, `del`, links); anything else contributes its text. A cell is always a
 * single line — GFM has no multi-line cells, and a stray newline would break
 * the row — so `<br>` and newlines become one space.
 */

type Align = 'left' | 'center' | 'right' | null

const INLINE_MARKERS: Record<string, string> = { strong: '**', b: '**', em: '*', i: '*', del: '~~', s: '~~' }

function cellAlign(cell: HastElement): Align {
  const a = cell.properties?.align
  if (a === 'left' || a === 'center' || a === 'right') return a
  const style = cell.properties?.style
  if (typeof style === 'string') {
    const m = /text-align\s*:\s*(left|center|right)/i.exec(style)
    if (m) return m[1].toLowerCase() as Align
  }
  return null
}

/** Characters that would re-parse as Markdown if a cell's literal text were
 *  emitted verbatim: `__init__` becomes bold, `*x*` italic, `[x]` a link
 *  label, `<b>` raw HTML, `~~x~~` strikethrough, a stray backtick opens a
 *  code span, `&amp;` an entity, `a@b.co` a mailto autolink. The parser
 *  stripped the author's escapes when it built the tree, so they are restored
 *  here on every text node that is NOT inside a code span (code content is
 *  fenced and must stay byte-for-byte). Pipes are escaped separately in
 *  `markdownCell`, where the cell boundary lives. */
const MD_META = /[\\`*_[\]<>~&@]/g

/** GFM's autolink extension turns bare `http://x`, `https://x` and `www.x`
 *  into links with no brackets at all, so literal text of that shape needs a
 *  break in the trigger: an escaped `:` or `.` is still the same character to
 *  the reader and stops the extension from firing. */
const AUTOLINK_SCHEME = /:\/\//g
const AUTOLINK_WWW = /\bwww\./gi

function escapeMarkdownText(text: string): string {
  return text.replace(MD_META, ch => `\\${ch}`).replace(AUTOLINK_SCHEME, '\\://').replace(AUTOLINK_WWW, m => `${m.slice(0, 3)}\\.`)
}

/** A cell is one line in GFM, so whitespace is normalized per TEXT node, where
 *  the code/prose distinction is still known: prose runs collapse to one space
 *  (a wrapped source line is not two spaces to the reader), while inside a code
 *  span only line breaks are replaced -- runs of spaces and tabs are part of
 *  the code and must survive the copy byte-for-byte. */
function inlineText(node: ElementContent, inCode = false): string {
  if (node.type === 'text') {
    return inCode ? node.value.replace(/[\r\n]+/g, ' ') : escapeMarkdownText(node.value.replace(/\s+/g, ' '))
  }
  if (node.type !== 'element') return ''
  const tag = node.tagName.toLowerCase()
  if (tag === 'br') return ' '
  if (tag === 'img') {
    const alt = node.properties?.alt
    return typeof alt === 'string' ? escapeMarkdownText(alt) : ''
  }
  if (tag === 'code') {
    const inner = node.children.map(c => inlineText(c, true)).join('')
    // A backtick inside the code needs a longer fence, exactly as CommonMark
    // specifies for inline code spans. Padding is needed in two cases: a
    // backtick at either end (it would fuse with the fence), and a space at
    // BOTH ends, because CommonMark strips one space from each end of a span
    // that begins and ends with one -- so `` ` a ` `` would re-parse as `a`.
    // The extra pair of spaces is what gets stripped, leaving the original.
    const runs = inner.match(/`+/g) ?? []
    const fence = '`'.repeat(Math.max(0, ...runs.map(r => r.length)) + 1)
    const touchesFence = inner.startsWith('`') || inner.endsWith('`')
    const spaceBounded = inner.length > 0 && inner.trim().length > 0 && inner.startsWith(' ') && inner.endsWith(' ')
    const pad = touchesFence || spaceBounded ? ' ' : ''
    return `${fence}${pad}${inner}${pad}${fence}`
  }
  const inner = node.children.map(c => inlineText(c, inCode)).join('')
  if (tag === 'a') {
    const href = node.properties?.href
    // Parentheses or spaces in the destination would end the link early;
    // CommonMark's angle-bracket form takes such a destination verbatim -- but
    // a literal `<` or `>` inside it would end THAT form, so those two are
    // percent-encoded first (a valid, equivalent URL). A plain URL keeps the
    // plain form so it round-trips byte-for-byte.
    if (typeof href !== 'string' || href.length === 0 || inner.length === 0) return inner
    const dest = href.replace(/[<>]/g, encodeURIComponent)
    return /[()\s]/.test(dest) ? `[${inner}](<${dest}>)` : `[${inner}](${dest})`
  }
  const marker = INLINE_MARKERS[tag]
  if (marker && inner.trim().length > 0) return `${marker}${inner}${marker}`
  return inner
}

/** The text of one cell as GFM sees it: one line (see `inlineText` for the
 *  per-node whitespace rule), pipes escaped so the cell boundary is
 *  unambiguous, surrounding whitespace dropped. */
function markdownCell(cell: HastElement): string {
  return cell.children.map(c => inlineText(c)).join('').trim().replace(/\|/g, '\\|')
}

/** Plain text of one cell -- formatting markers are noise in a spreadsheet.
 *  Unlike the Markdown side, a CSV field CAN hold a line break (RFC 4180 quotes
 *  it), so an author's `<br>` stays a newline here; whitespace inside a text
 *  node collapses to one space, and whitespace inside code is kept. */
function plainCell(cell: HastElement): string {
  const walk = (n: ElementContent, inCode: boolean): string => {
    if (n.type === 'text') return inCode ? n.value : n.value.replace(/\s+/g, ' ')
    if (n.type !== 'element') return ''
    const tag = n.tagName.toLowerCase()
    if (tag === 'br') return '\n'
    if (tag === 'img') { const alt = n.properties?.alt; return typeof alt === 'string' ? alt : '' }
    return n.children.map(c => walk(c, inCode || tag === 'code')).join('')
  }
  return cell.children.map(c => walk(c, false)).join('').split('\n').map(l => l.trim()).join('\n').trim()
}

/** The lowercased tag of an element node, or null for text/comment nodes.
 *  A plain string rather than a type predicate: a predicate's negative branch
 *  would narrow `child` to `never` in the `else if` below, since failing
 *  "is a `<tr>`" is read by TS as "is not an Element at all". */
function tagOf(n: ElementContent): string | null {
  return n.type === 'element' ? n.tagName.toLowerCase() : null
}

/** The table's row groups, in document order. A `<thead>`, `<tbody>` or
 *  `<tfoot>` is one group; a run of `<tr>` written directly under `<table>`
 *  is one implicit group, which is how HTML treats it (the parser wraps such
 *  rows in an anonymous `<tbody>`). Spans never cross a group: a `rowspan`
 *  that reaches past its group's last row is clamped there by the browser,
 *  and `rowspan="0"` means "to the end of this group". */
function rowGroups(table: HastElement): HastElement[][] {
  const groups: HastElement[][] = []
  let bare: HastElement[] = []
  const flushBare = () => { if (bare.length) { groups.push(bare); bare = [] } }
  for (const child of table.children) {
    const tag = tagOf(child)
    if (tag === 'tr') bare.push(child as HastElement)
    else if (tag === 'thead' || tag === 'tbody' || tag === 'tfoot') {
      flushBare()
      const rows = (child as HastElement).children.filter((r): r is HastElement => tagOf(r) === 'tr')
      if (rows.length) groups.push(rows)
    }
  }
  flushBare()
  return groups
}

function rowCells(row: HastElement): HastElement[] {
  return row.children.filter((c): c is HastElement => { const t = tagOf(c); return t === 'th' || t === 'td' })
}

/** The HTML spec's own clamps for the two attributes (the browser applies the
 *  same ones when it lays the table out), so the grid can never be driven
 *  wider than the rendered table by a hostile `colspan="1000000000"`. */
const MAX_SPAN = { colSpan: 1000, rowSpan: 65534 } as const

/** A cell's `colspan` as a positive integer; anything else is 1. hast keeps
 *  the camelCase names react expects (`colSpan`), and the sanitizer admits
 *  both span attributes on raw-HTML cells (`TAG_ATTRS` in MarkdownRenderer). */
function colSpanOf(cell: HastElement): number {
  const v = cell.properties?.colSpan
  const n = typeof v === 'number' ? v : typeof v === 'string' ? parseInt(v, 10) : 1
  return Number.isFinite(n) && n > 1 ? Math.min(Math.floor(n), MAX_SPAN.colSpan) : 1
}

/** A cell's `rowspan`: 1 for anything unusable, `Infinity` for the HTML
 *  `rowspan="0"` ("every remaining row of this row group"), otherwise the
 *  clamped integer. Infinity is safe because a span is only ever consumed
 *  row by row inside `groupGrid`, whose loop is bounded by the group's rows. */
function rowSpanOf(cell: HastElement): number {
  const v = cell.properties?.rowSpan
  const n = typeof v === 'number' ? v : typeof v === 'string' ? parseInt(v, 10) : 1
  if (n === 0) return Number.POSITIVE_INFINITY
  return Number.isFinite(n) && n > 1 ? Math.min(Math.floor(n), MAX_SPAN.rowSpan) : 1
}

/** One row group as a logical grid: one entry per row, one slot per column,
 *  a slot holding the cell that owns it or `null` where a `rowspan` /
 *  `colspan` from an earlier cell covers it. A GFM table cannot express
 *  spans, so the covered slots serialize as empty cells -- the same shape a
 *  spreadsheet gives a merged range. Without this, a row that starts under a
 *  spanning cell would have its cells shifted left into the wrong columns. */
function groupGrid(rows: HastElement[]): (HastElement | null)[][] {
  const grid: (HastElement | null)[][] = []
  // Columns still occupied by a rowspan from above: column -> rows remaining.
  // Scoped to this group, so a span can never leak into the next one.
  const pending = new Map<number, number>()
  for (const row of rows) {
    const cells = rowCells(row)
    if (cells.length === 0 && pending.size === 0) continue
    const slots: (HastElement | null)[] = []
    let col = 0
    const skipOccupied = () => {
      while ((pending.get(col) ?? 0) > 0) { slots[col] = null; pending.set(col, pending.get(col)! - 1); col++ }
    }
    for (const cell of cells) {
      skipOccupied()
      const cols = colSpanOf(cell)
      const down = rowSpanOf(cell)
      slots[col] = cell
      for (let i = 1; i < cols; i++) slots[col + i] = null
      if (down > 1) for (let i = 0; i < cols; i++) pending.set(col + i, down - 1)
      col += cols
    }
    // Columns past the last cell that a rowspan still covers: consume them
    // too, so a short row under a spanning cell keeps the grid rectangular.
    for (const [k, v] of [...pending]) {
      if (k >= col && v > 0) { slots[k] = null; pending.set(k, v - 1) }
    }
    for (const [k, v] of pending) if (v <= 0) pending.delete(k)
    if (slots.length > 0) grid.push(slots)
  }
  return grid
}

/** The whole table as a grid: the row groups' grids concatenated. */
function tableGrid(table: HastElement): (HastElement | null)[][] {
  return rowGroups(table).flatMap(groupGrid)
}

function delimiter(align: Align): string {
  if (align === 'center') return ':---:'
  if (align === 'right') return '---:'
  if (align === 'left') return ':---'
  return '---'
}

/** GFM table text for the hast `<table>` element. Empty string when the table
 *  has no rows with cells (nothing worth putting on the clipboard). The first
 *  row is the header, whatever section it sits in: GFM requires one, and a
 *  raw-HTML table with no `<thead>` still has a first row to promote. */
export function hastTableToMarkdown(table: HastElement): string {
  const rows = tableGrid(table)
  if (rows.length === 0) return ''
  const width = Math.max(...rows.map(r => r.length))
  const pad = <T,>(cells: T[], fill: T) => cells.concat(Array<T>(width - cells.length).fill(fill))
  const line = (cells: string[]) => `| ${pad(cells, '').join(' | ')} |`
  const [header, ...body] = rows
  const out = [
    line(header.map(c => c ? markdownCell(c) : '')),
    line(pad(header.map(c => delimiter(c ? cellAlign(c) : null)), '---')),
  ]
  for (const row of body) out.push(line(row.map(c => c ? markdownCell(c) : '')))
  return out.join('\n')
}

/** A field a spreadsheet would evaluate rather than display: it starts with a
 *  formula trigger (`=`, `+`, `-`, `@`, tab, CR) and is not simply a number.
 *  `-0.57` and `+3%` are values and stay bare; `=WEBSERVICE(...)`, `-2+3` and
 *  `@SUM(A1)` are formulas. */
const FORMULA_LEAD = /^[=+\-@\t\r]/
const PLAIN_NUMBER = /^[+-]?(?:\d[\d,_\s]*)?(?:\.\d+)?\s*%?$/

/** Spreadsheet formula injection defence: a table copied from an untrusted
 *  source (a page the agent fetched, a tool result) must not become a live
 *  `=WEBSERVICE()` or `=HYPERLINK()` the moment it is pasted. A leading
 *  apostrophe is the spreadsheet convention for "this is text" -- Excel,
 *  Sheets and LibreOffice all honour it and hide the mark -- and is applied
 *  only to fields that would otherwise evaluate, so ordinary negative numbers
 *  paste as numbers. */
function neutralizeFormula(value: string): string {
  return FORMULA_LEAD.test(value) && !PLAIN_NUMBER.test(value) ? `'${value}` : value
}

/** RFC 4180 quoting: a field with a comma, quote, or line break is wrapped in
 *  double quotes with inner quotes doubled; everything else is left bare. */
function csvField(value: string): string {
  const v = neutralizeFormula(value)
  return /[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v
}

/** CSV text for the hast `<table>` element, one line per row, header first. */
export function hastTableToCsv(table: HastElement): string {
  const rows = tableGrid(table)
  const width = rows.length ? Math.max(...rows.map(r => r.length)) : 0
  return rows
    .map(cells => cells.concat(Array<HastElement | null>(width - cells.length).fill(null)))
    .map(cells => cells.map(c => c ? csvField(plainCell(c)) : '').join(','))
    .join('\n')
}

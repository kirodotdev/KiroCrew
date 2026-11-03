/**
 * Markdown tables rendered as a real grid, for the doc pane PoC (Obsidian-style).
 *
 * The file keeps plain markdown. Any block that is a valid table (a header row,
 * then a `---` separator) is shown as a grid widget instead of its text: each cell
 * is an input, and every edit rewrites the table's markdown, re-aligned. Keys in a
 * cell: Tab / Shift+Tab next / previous cell (Tab off the last one adds a row),
 * Enter a row below, Up / Down between rows (and out of the table at either edge),
 * Backspace in an empty row deletes it, Escape leaves the table. To hand a table
 * to the conductor, select across it in the text: it is wrapped as a whole.
 */
import { Prec, StateField, type EditorState, type Extension, type Transaction } from '@codemirror/state'
import { Decoration, EditorView, WidgetType, keymap, type DecorationSet } from '@codemirror/view'
import { askRows, requestRowAsk } from './docAskRows'

export const isRow = (text: string) => text.trim().startsWith('|')

function splitRow(text: string): string[] {
  let t = text.trim()
  if (t.startsWith('|')) t = t.slice(1)
  if (t.endsWith('|')) t = t.slice(0, -1)
  return t.split('|').map(c => c.trim())
}

const isSeparator = (cells: string[]) => cells.length > 0 && cells.every(c => /^:?-+:?$/.test(c))

export interface Table {
  from: number
  to: number
  firstLine: number
  /** Every row as written, the separator included at index 1. */
  rows: string[][]
}

/** The table block whose lines include *pos*, when it is a valid table. */
export function tableAt(state: EditorState, pos: number): Table | null {
  const doc = state.doc
  const here = doc.lineAt(pos)
  if (!isRow(here.text)) return null
  let first = here.number
  let last = here.number
  while (first > 1 && isRow(doc.line(first - 1).text)) first--
  while (last < doc.lines && isRow(doc.line(last + 1).text)) last++
  const rows: string[][] = []
  for (let n = first; n <= last; n++) rows.push(splitRow(doc.line(n).text))
  if (rows.length < 2 || !isSeparator(rows[1])) return null
  return { from: doc.line(first).from, to: doc.line(last).to, firstLine: first, rows }
}

/** Pad every row to the widest column and render the aligned lines. */
export function formatRows(rows: string[][]): string[] {
  const cols = Math.max(...rows.map(r => r.length))
  const padded = rows.map(r => [...r, ...Array(cols - r.length).fill('')])
  const widths = Array.from({ length: cols }, (_, c) =>
    Math.max(3, ...padded.filter(r => !isSeparator(r)).map(r => r[c].length)))
  return padded.map(r => {
    if (isSeparator(r)) {
      const cells = r.map((c, i) => {
        const left = c.startsWith(':')
        const right = c.endsWith(':') && c.length > 1
        return (left ? ':' : '') + '-'.repeat(widths[i] - (left ? 1 : 0) - (right ? 1 : 0)) + (right ? ':' : '')
      })
      return `| ${cells.join(' | ')} |`
    }
    return `| ${r.map((c, i) => c.padEnd(widths[i])).join(' | ')} |`
  })
}

const emptyRow = (cols: number) => Array<string>(cols).fill('')

// ---- writing back -----------------------------------------------------------

/** The table a widget's DOM stands for, read fresh from the document. */
function liveTable(view: EditorView, wrapper: HTMLElement): Table | null {
  return tableAt(view.state, view.posAtDOM(wrapper))
}

function write(view: EditorView, table: Table, rows: string[][]) {
  view.dispatch({ changes: { from: table.from, to: table.to, insert: formatRows(rows).join('\n') } })
}

/** Focus cell (row, col) of the table that starts at *from*, text selected. */
function focusCell(view: EditorView, from: number, row: number, col: number) {
  for (const el of view.contentDOM.querySelectorAll<HTMLElement>('.cm-table-widget')) {
    if (view.posAtDOM(el) !== from) continue
    const input = el.querySelector<HTMLInputElement>(`input[data-r="${row}"][data-c="${col}"]`)
    if (input) { input.focus(); input.select() }
    return
  }
}

/** Put the editor's own caret just before or after the table and focus it. */
function leave(view: EditorView, table: Table, dir: 'before' | 'after') {
  const doc = view.state.doc
  if (dir === 'after') {
    if (table.to >= doc.length) {
      view.dispatch({ changes: { from: doc.length, insert: '\n' }, selection: { anchor: doc.length + 1 } })
    } else {
      view.dispatch({ selection: { anchor: table.to + 1 } })
    }
  } else if (table.from === 0) {
    view.dispatch({ changes: { from: 0, insert: '\n' }, selection: { anchor: 0 } })
  } else {
    view.dispatch({ selection: { anchor: table.from - 1 } })
  }
  view.focus()
}

/** Data rows only: the header is 0, body rows follow the separator. */
const visibleRows = (rows: string[][]) => rows.map((_, i) => i).filter(i => i !== 1)

function onCellKey(view: EditorView, wrapper: HTMLElement, e: KeyboardEvent, r: number, c: number) {
  const table = liveTable(view, wrapper)
  if (!table) return
  const rows = table.rows.map(x => [...x])
  const cols = Math.max(...rows.map(x => x.length))
  const order = visibleRows(rows)
  const at = order.indexOf(r)
  if (e.key === 'Tab') {
    e.preventDefault()
    let nr = r
    let nc = c + (e.shiftKey ? -1 : 1)
    if (nc >= cols) { nc = 0; nr = order[at + 1] ?? -1 }
    if (nc < 0) { nc = cols - 1; nr = order[at - 1] ?? r }
    if (nr === -1) {
      rows.push(emptyRow(cols))
      write(view, table, rows)
      nr = rows.length - 1
    }
    focusCell(view, table.from, nr, nc)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    const at2 = r === 0 ? 2 : r + 1
    rows.splice(at2, 0, emptyRow(cols))
    write(view, table, rows)
    focusCell(view, table.from, at2, 0)
  } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault()
    const nr = order[at + (e.key === 'ArrowDown' ? 1 : -1)]
    if (nr === undefined) leave(view, table, e.key === 'ArrowDown' ? 'after' : 'before')
    else focusCell(view, table.from, nr, c)
  } else if (e.key === 'Escape') {
    e.preventDefault()
    leave(view, table, 'after')
  } else if (e.key === 'Backspace' && r > 1 && rows[r].every(x => x === '')) {
    e.preventDefault()
    rows.splice(r, 1)
    write(view, table, rows)
    focusCell(view, table.from, order[at - 1] ?? 0, c)
  }
}

function onCellInput(view: EditorView, wrapper: HTMLElement, input: HTMLInputElement, r: number, c: number) {
  const table = liveTable(view, wrapper)
  if (!table) return
  // A pipe would end the cell; the grid keeps it out of the markdown.
  if (input.value.includes('|')) input.value = input.value.replace(/\|/g, '')
  const rows = table.rows.map(x => [...x])
  while (rows[r].length <= c) rows[r].push('')
  rows[r][c] = input.value.trim()
  write(view, table, rows)
}

function addColumn(view: EditorView, wrapper: HTMLElement) {
  const table = liveTable(view, wrapper)
  if (!table) return
  const cols = Math.max(...table.rows.map(x => x.length))
  const rows = table.rows.map((x, i) => [...x, ...emptyRow(cols - x.length), i === 1 ? '---' : i === 0 ? `Column ${cols + 1}` : ''])
  write(view, table, rows)
  focusCell(view, table.from, 0, cols)
}

function addRow(view: EditorView, wrapper: HTMLElement) {
  const table = liveTable(view, wrapper)
  if (!table) return
  const cols = Math.max(...table.rows.map(x => x.length))
  const rows = [...table.rows.map(x => [...x]), emptyRow(cols)]
  write(view, table, rows)
  focusCell(view, table.from, rows.length - 1, 0)
}

function deleteTable(view: EditorView, wrapper: HTMLElement) {
  const table = liveTable(view, wrapper)
  if (!table) return
  const doc = view.state.doc
  const to = table.to < doc.length ? table.to + 1 : table.to
  view.dispatch({ changes: { from: table.from, to }, selection: { anchor: table.from } })
  view.focus()
}

// ---- the widget -------------------------------------------------------------

/** How long the row handle stays after the pointer leaves the table. */
const HANDLE_HIDE_MS = 400

class TableWidget extends WidgetType {
  /** *picked*: the rows chosen for an open ask, tinted until it is sent or put away. */
  constructor(readonly rows: string[][], readonly picked: number[] = []) { super() }

  eq(other: TableWidget) {
    return JSON.stringify(other.rows) === JSON.stringify(this.rows) && other.picked.join() === this.picked.join()
  }

  toDOM(view: EditorView) {
    const wrapper = document.createElement('div')
    wrapper.className = 'cm-table-widget'
    wrapper.setAttribute('data-testid', 'doc-table')
    this.render(wrapper, view)
    return wrapper
  }

  /** Same table, new text: patch in place so the cell being typed in keeps focus. */
  updateDOM(dom: HTMLElement, view: EditorView) {
    const focused = dom.contains(document.activeElement) ? document.activeElement as HTMLInputElement : null
    const where = focused ? { r: focused.dataset.r, c: focused.dataset.c, s: focused.selectionStart, e: focused.selectionEnd } : null
    const sameShape = dom.dataset.shape === this.shape()
    if (sameShape) {
      for (const tr of dom.querySelectorAll<HTMLElement>('tr[data-r]')) tr.classList.toggle('cm-row-picked', this.picked.includes(Number(tr.dataset.r)))
      for (const input of dom.querySelectorAll<HTMLInputElement>('input[data-r]')) {
        if (input === focused) continue
        const v = this.rows[Number(input.dataset.r)]?.[Number(input.dataset.c)] ?? ''
        if (input.value !== v) input.value = v
      }
      return true
    }
    this.render(dom, view)
    if (where) {
      const input = dom.querySelector<HTMLInputElement>(`input[data-r="${where.r}"][data-c="${where.c}"]`)
      if (input) { input.focus(); input.setSelectionRange(where.s, where.e) }
    }
    return true
  }

  shape() {
    return `${this.rows.length}x${Math.max(...this.rows.map(r => r.length))}`
  }

  render(wrapper: HTMLElement, view: EditorView) {
    wrapper.replaceChildren()
    wrapper.dataset.shape = this.shape()
    const cols = Math.max(...this.rows.map(r => r.length))
    const table = document.createElement('table')
    const head = document.createElement('thead')
    const body = document.createElement('tbody')
    visibleRows(this.rows).forEach(r => {
      const tr = document.createElement('tr')
      tr.dataset.r = String(r)
      if (this.picked.includes(r)) tr.classList.add('cm-row-picked')
      // Read at hover time, once the handle below exists. The header has no handle.
      tr.onmouseenter = () => { if (r > 0) showHandle(tr, r); else handle.hidden = true }
      for (let c = 0; c < cols; c++) {
        const cell = document.createElement(r === 0 ? 'th' : 'td')
        const input = document.createElement('input')
        input.dataset.r = String(r)
        input.dataset.c = String(c)
        input.value = this.rows[r][c] ?? ''
        input.spellcheck = false
        input.setAttribute('aria-label', r === 0 ? `Header ${c + 1}` : `Row ${r - 1}, column ${c + 1}`)
        input.oninput = () => onCellInput(view, wrapper, input, r, c)
        input.onkeydown = e => onCellKey(view, wrapper, e, r, c)
        cell.append(input)
        tr.append(cell)
      }
      ;(r === 0 ? head : body).append(tr)
    })
    table.append(head, body)
    const foot = document.createElement('div')
    foot.className = 'cm-table-foot'
    const button = (label: string, testId: string, run: () => void) => {
      const b = document.createElement('button')
      b.type = 'button'
      b.textContent = label
      b.setAttribute('data-testid', testId)
      b.onclick = run
      foot.append(b)
    }
    button('+ Row', 'table-add-row', () => addRow(view, wrapper))
    button('+ Column', 'table-add-column', () => addColumn(view, wrapper))
    button('Delete table', 'table-delete', () => deleteTable(view, wrapper))
    // The row handle: one ✦ in the margin, beside whichever row the pointer is
    // on, outside the grid so it never takes a cell's place or a keystroke.
    const handle = document.createElement('button')
    handle.type = 'button'
    handle.className = 'cm-row-ask'
    handle.textContent = '✦'
    handle.title = 'Ask the conductor about this row (Shift-click for a range)'
    handle.setAttribute('aria-label', 'Ask the conductor about this row')
    handle.setAttribute('data-testid', 'table-row-ask')
    handle.onmousedown = e => e.preventDefault()
    // Hiding waits a moment: the pointer crosses the margin on its way from the
    // row to the handle, and that crossing must not make the handle vanish.
    let hideTimer: ReturnType<typeof setTimeout> | null = null
    const keep = () => { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null } }
    const hideSoon = () => { keep(); hideTimer = setTimeout(() => { handle.hidden = true }, HANDLE_HIDE_MS) }
    const showHandle = (tr: HTMLElement, r: number) => {
      keep()
      handle.style.top = `${tr.offsetTop + tr.offsetHeight / 2}px`
      handle.dataset.r = String(r)
      handle.hidden = false
    }
    handle.hidden = true
    wrapper.onmouseleave = hideSoon
    handle.onmouseenter = keep
    handle.onclick = e => {
      const r = Number(handle.dataset.r)
      if (r) askAboutRows(view, wrapper, r, e.shiftKey)
    }
    wrapper.append(table, foot, handle)
  }

  // Clicks and keys in the grid belong to its inputs, not the editor.
  ignoreEvent() { return true }
}

/** Open the ask box for row *r* of the table in *wrapper* (with *extend*, the
 *  range from the rows already picked to *r*). The conductor is sent the header
 *  and those rows; the markers wrap the whole table, since one inside a row would
 *  end it. The picked rows are read from the state, not the widget: a widget is
 *  patched in place, so the one that drew this grid can be several picks old. */
function askAboutRows(view: EditorView, wrapper: HTMLElement, r: number, extend: boolean) {
  const table = liveTable(view, wrapper)
  if (!table || r < 2) return
  const current = view.state.field(askRows, false)
  const picked = extend && current?.from === table.from ? current.rows : []
  let rows = [r]
  if (picked.length) {
    const lo = Math.min(r, ...picked)
    const hi = Math.max(r, ...picked)
    rows = []
    for (let i = Math.max(2, lo); i <= hi; i++) rows.push(i)
  }
  const quote = formatRows([table.rows[0], table.rows[1], ...rows.map(i => table.rows[i])]).join('\n')
  view.dispatch({ effects: requestRowAsk.of({ from: table.from, to: table.to, quote, rows }) })
}

function buildTables(state: EditorState): DecorationSet {
  const decos = []
  const picked = state.field(askRows, false)
  const doc = state.doc
  for (let n = 1; n <= doc.lines; n++) {
    const line = doc.line(n)
    if (!isRow(line.text)) continue
    const table = tableAt(state, line.from)
    if (!table) continue
    decos.push(Decoration.replace({ widget: new TableWidget(table.rows, picked?.from === table.from ? picked.rows : []), block: true }).range(table.from, table.to))
    n = table.firstLine + table.rows.length - 1
  }
  return Decoration.set(decos)
}

const tables = StateField.define<DecorationSet>({
  create: buildTables,
  update: (value, tr: Transaction) => (tr.docChanged || tr.effects.length ? buildTables(tr.state) : value),
  provide: f => EditorView.decorations.from(f),
})

/** Up / Down from the text into a table: the caret would otherwise land on the
 *  widget's edge as one step (the table's top), so it steps into the nearest row
 *  instead, the last row from below and the header from above. */
function enterTable(view: EditorView, forward: boolean): boolean {
  const sel = view.state.selection.main
  if (!sel.empty) return false
  const doc = view.state.doc
  const here = tableAt(view.state, sel.head)
  let next = here
  if (!here) {
    const line = doc.lineAt(sel.head)
    const n = line.number + (forward ? 1 : -1)
    if (n < 1 || n > doc.lines) return false
    // A wrapped paragraph moves within itself first.
    const moved = view.moveVertically(sel, forward).head
    if (moved !== sel.head && moved >= line.from && moved <= line.to) return false
    next = tableAt(view.state, doc.line(n).from)
  }
  if (!next) return false
  if (here && !forward) {
    // On the widget's own edge: Up leaves it upwards.
    leave(view, here, 'before')
    return true
  }
  const order = visibleRows(next.rows)
  focusCell(view, next.from, forward ? order[0] : order[order.length - 1], 0)
  return true
}

const tableKeys = Prec.high(keymap.of([
  { key: 'ArrowUp', run: v => enterTable(v, false) },
  { key: 'ArrowDown', run: v => enterTable(v, true) },
]))

export function insertTable(view: EditorView): boolean {
  const { state } = view
  const line = state.doc.lineAt(state.selection.main.head)
  const blank = line.text.trim() === ''
  const from = blank ? line.from : line.to
  const lead = blank ? '' : '\n\n'
  const rows = [['Column 1', 'Column 2', 'Column 3'], ['---', '---', '---'], emptyRow(3), emptyRow(3)]
  const text = formatRows(rows).join('\n')
  const end = blank ? line.to : from
  const tail = end >= state.doc.length ? '\n' : ''
  view.dispatch({ changes: { from, to: end, insert: lead + text + tail } })
  focusCell(view, from + lead.length, 0, 0)
  return true
}

const tableTheme = EditorView.baseTheme({
  // Padding, not margin: CodeMirror measures a block widget by its border box, so
  // an outer margin shifts every line below it and Up / Down land lines away.
  '.cm-table-widget': { padding: '8px 0 12px', position: 'relative' },
  '.cm-table-widget tr.cm-row-picked td': { background: 'color-mix(in srgb, var(--accent) 14%, transparent)' },
  '.cm-row-ask': {
    position: 'absolute', left: '-24px', width: '22px', height: '22px', transform: 'translateY(-50%)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0',
    borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
    color: 'var(--accent)', fontSize: '12px', cursor: 'pointer', boxShadow: '0 1px 4px rgba(0,0,0,.12)',
  },
  '.cm-row-ask:hover': { background: 'var(--bg-hover)' },
  '.cm-row-ask[hidden]': { display: 'none' },
  '.cm-table-widget table': { borderCollapse: 'collapse', width: '100%', tableLayout: 'auto' },
  '.cm-table-widget th, .cm-table-widget td': { border: '1px solid var(--border)', padding: '0' },
  '.cm-table-widget th': { background: 'var(--bg-elevated)' },
  '.cm-table-widget input': {
    width: '100%', boxSizing: 'border-box', border: 'none', outline: 'none', background: 'transparent',
    color: 'var(--text)', font: 'inherit', padding: '6px 10px',
  },
  '.cm-table-widget th input': { fontWeight: '600', color: 'var(--text-strong)' },
  '.cm-table-widget input:focus': { boxShadow: 'inset 0 0 0 2px var(--accent)' },
  '.cm-table-foot': { display: 'flex', gap: '6px', marginTop: '6px', opacity: '0', transition: 'opacity .15s' },
  '.cm-table-widget:hover .cm-table-foot, .cm-table-widget:focus-within .cm-table-foot': { opacity: '1' },
  '.cm-table-foot button': {
    padding: '3px 10px', borderRadius: '6px', border: '1px solid var(--border)',
    background: 'var(--bg-elevated)', color: 'var(--text)', fontSize: '12px', cursor: 'pointer',
  },
  '.cm-table-foot button:hover': { background: 'var(--bg-hover)' },
})

export function tableExtension(): Extension {
  return [tables, tableKeys, tableTheme]
}

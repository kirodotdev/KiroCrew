/**
 * Doc pane tables: a valid markdown table renders as a grid of inputs, and every
 * grid edit rewrites the table's markdown. Driven through a real CodeMirror view.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { insertTable, tableExtension } from '../pages/chat/page/docTables'

let view: EditorView | null = null
afterEach(() => { view?.destroy(); view = null; document.body.replaceChildren() })

function make(doc: string): EditorView {
  view = new EditorView({
    parent: document.body,
    state: EditorState.create({ doc, extensions: [tableExtension()] }),
  })
  return view
}
const cell = (v: EditorView, r: number, c: number) =>
  v.dom.querySelector<HTMLInputElement>(`input[data-r="${r}"][data-c="${c}"]`)!
const type = (input: HTMLInputElement, value: string) => fireEvent.input(input, { target: { value } })
const TABLE = '| a | b |\n| --- | --- |\n| c | d |'

describe('doc tables', () => {
  it('renders a valid table as a grid, and leaves pipe text without a separator alone', () => {
    const v = make(`${TABLE}\n\n| not | a table |`)
    expect(v.dom.querySelectorAll('[data-testid="doc-table"]').length).toBe(1)
    expect(cell(v, 0, 0).value).toBe('a')
    expect(cell(v, 2, 1).value).toBe('d')
  })

  it('typing in a cell rewrites the markdown, re-aligned, and keeps the cell focused', () => {
    const v = make(TABLE)
    const input = cell(v, 2, 0)
    input.focus()
    type(input, 'a longer cell')
    expect(v.state.doc.toString()).toBe('| a             | b   |\n| ------------- | --- |\n| a longer cell | d   |')
    expect(document.activeElement).toBe(cell(v, 2, 0))
  })

  it('Tab moves cell to cell and off the last cell adds a row', () => {
    const v = make(TABLE)
    fireEvent.keyDown(cell(v, 0, 1), { key: 'Tab' })
    expect(document.activeElement).toBe(cell(v, 2, 0))
    fireEvent.keyDown(cell(v, 2, 1), { key: 'Tab' })
    expect(v.state.doc.lines).toBe(4)
    expect(document.activeElement).toBe(cell(v, 3, 0))
  })

  it('Enter adds a row below; Backspace in an empty row deletes it', () => {
    const v = make(TABLE)
    fireEvent.keyDown(cell(v, 0, 0), { key: 'Enter' })
    expect(v.state.doc.line(3).text).toBe('|     |     |')
    expect(document.activeElement).toBe(cell(v, 2, 0))
    fireEvent.keyDown(cell(v, 2, 0), { key: 'Backspace' })
    expect(v.state.doc.toString()).toBe('| a   | b   |\n| --- | --- |\n| c   | d   |')
  })

  it('+ Column and Delete table', () => {
    const v = make(`${TABLE}\nafter`)
    fireEvent.click(v.dom.querySelector('[data-testid="table-add-column"]')!)
    expect(v.state.doc.line(1).text).toBe('| a   | b   | Column 3 |')
    fireEvent.click(v.dom.querySelector('[data-testid="table-delete"]')!)
    expect(v.state.doc.toString()).toBe('after')
  })

  it('Up from below a table enters its last row, Down from above enters its header', () => {
    const v = make(`before\n${TABLE}\nafter`)
    v.dispatch({ selection: { anchor: v.state.doc.length } })
    v.focus()
    fireEvent.keyDown(v.contentDOM, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(cell(v, 2, 0))
    v.dispatch({ selection: { anchor: 2 } })
    v.focus()
    fireEvent.keyDown(v.contentDOM, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(cell(v, 0, 0))
  })

  it('inserts a 3x3 table with the first header cell focused', () => {
    const v = make('intro')
    v.dispatch({ selection: { anchor: 5 } })
    insertTable(v)
    expect(v.state.doc.toString()).toContain('intro\n\n| Column 1 | Column 2 | Column 3 |')
    expect(document.activeElement).toBe(cell(v, 0, 0))
  })
})

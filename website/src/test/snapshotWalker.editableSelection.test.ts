/**
 * The page-side walker (`src/kiro_crew/browser_harness/snapshot_walker.js`)
 * reports whether the focused rich editor holds a non-empty selection
 * (`activeField().selected`). The `type` op clears such a selection with a
 * Delete before inserting, because Word on the web keeps only as many inserted
 * characters as the selection held. Pinned here against the real source.
 *
 * jsdom does not compute `isContentEditable`, so each editor sets it the way
 * Chrome reports it for a `contenteditable` host.
 */
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { readSource } from './readSource'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readSource(join(HARNESS_DIR, 'snapshot_walker.js')).split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

interface Walker {
  activeField: () => { focused: boolean; tag: string; selected?: boolean }
}

const evaluate = (source: string): unknown => new Function(`return (${source})`)()
const evalWalker = (): Walker => evaluate(walkerSource()) as Walker

function editor(text: string): HTMLDivElement {
  const el = document.createElement('div')
  el.setAttribute('contenteditable', 'true')
  el.tabIndex = 0
  Object.defineProperty(el, 'isContentEditable', { value: true })
  el.textContent = text
  document.body.appendChild(el)
  el.focus()
  return el
}

function select(node: Node, start: number, end: number): void {
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, end)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

afterEach(() => {
  window.getSelection()?.removeAllRanges()
  document.body.innerHTML = ''
})

describe('activeField reports a rich editor selection', () => {
  it('is true for a non-empty selection inside the focused editor', () => {
    const el = editor('Line 2 for input timing tests.')
    select(el.firstChild!, 17, 24)
    expect(evalWalker().activeField().selected).toBe(true)
  })

  it('is false at a collapsed caret', () => {
    const el = editor('Line 2 for input timing tests.')
    select(el.firstChild!, 5, 5)
    expect(evalWalker().activeField().selected).toBe(false)
  })

  it('is false when the selection sits outside the focused editor', () => {
    const outside = document.createElement('p')
    outside.textContent = 'elsewhere'
    document.body.appendChild(outside)
    editor('focused editor')
    select(outside.firstChild!, 0, 4)
    expect(evalWalker().activeField().selected).toBe(false)
  })

  it('is false for a text input with a selection', () => {
    const input = document.createElement('input')
    input.value = 'hello world'
    document.body.appendChild(input)
    input.focus()
    input.setSelectionRange(0, 5)
    const active = evalWalker().activeField()
    expect(active.tag).toBe('input')
    expect(active.selected).toBe(false)
  })
})

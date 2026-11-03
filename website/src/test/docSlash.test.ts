/**
 * The doc's "/" menu and empty-line hint, through a real CodeMirror view: the slash
 * is real text, the menu filters on what follows it, Enter inserts the block, and a
 * slash anywhere but an empty line's start is only a slash.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { act } from '@testing-library/react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { EMPTY_LINE_HINT, isSlashOpen, matchCommands, slashExtension } from '../pages/chat/page/docSlash'
import { spaceAskExtension, isSpaceAskOpen } from '../pages/chat/page/docSpaceAsk'
import { tableExtension } from '../pages/chat/page/docTables'
import type { ConductorHost } from '../pages/chat/page/docConductor'

let view: EditorView | null = null
afterEach(() => { act(() => { view?.destroy() }); view = null; document.body.replaceChildren() })

function make(doc: string, at = doc.length): EditorView {
  const host = { current: { send: () => {}, openSession: () => {}, markRead: () => {} } as ConductorHost }
  act(() => {
    view = new EditorView({
      parent: document.body,
      state: EditorState.create({
        doc, selection: { anchor: at },
        extensions: [markdown({ base: markdownLanguage }), tableExtension(), spaceAskExtension(host), slashExtension()],
      }),
    })
  })
  return view!
}

/** Type *text* as the person would: through the editor's input handlers, so "/" can open the menu. */
function type(v: EditorView, text: string) {
  for (const ch of text) {
    act(() => {
      const { from, to } = v.state.selection.main
      const handled = v.state.facet(EditorView.inputHandler).some(h => h(v, from, to, ch, () => v.state.update({ changes: { from, to, insert: ch } })))
      if (!handled) v.dispatch({ changes: { from, to, insert: ch }, selection: { anchor: from + 1 } })
    })
  }
}
const key = (v: EditorView, k: string) => act(() => { v.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })) })
const items = () => [...document.querySelectorAll<HTMLElement>('[data-testid="slash-item"]')].map(e => e.dataset.id)

describe('doc "/" menu', () => {
  it('opens on an empty line with the slash in the text, and filters as you type', () => {
    const v = make('intro\n\n')
    type(v, '/')
    expect(v.state.doc.toString()).toBe('intro\n\n/')
    expect(isSlashOpen(v.state)).toBe(true)
    expect(items()[0]).toBe('ai')
    type(v, 'tab')
    expect(items()).toEqual(['table'])
  })

  it('Enter replaces the "/query" with the chosen block', () => {
    const v = make('intro\n\n')
    type(v, '/h2')
    key(v, 'Enter')
    expect(v.state.doc.toString()).toBe('intro\n\n## ')
    expect(isSlashOpen(v.state)).toBe(false)
    type(v, '/')
    // Not an empty line any more: just a slash.
    expect(isSlashOpen(v.state)).toBe(false)
    expect(v.state.doc.toString()).toBe('intro\n\n## /')
  })

  it('arrows choose; a code block puts the caret inside it; Table inserts a grid', () => {
    const v = make('')
    type(v, '/code')
    key(v, 'Enter')
    expect(v.state.doc.toString()).toBe('```\n\n```')
    expect(v.state.selection.main.head).toBe(4)
    const t = make('x\n\n')
    type(t, '/')
    const down = items().indexOf('table')
    for (let i = 0; i < down; i++) key(t, 'ArrowDown')
    key(t, 'Enter')
    expect(t.state.doc.toString()).toContain('| Column 1 | Column 2 | Column 3 |')
  })

  it('Ask AI opens the Space card', () => {
    const v = make('a paragraph\n\n')
    type(v, '/ask')
    key(v, 'Enter')
    expect(v.state.doc.toString()).toBe('a paragraph\n\n')
    expect(isSpaceAskOpen(v.state)).toBe(true)
  })

  it('a slash mid-text is text; Escape or a query that matches nothing keeps what was typed', () => {
    const v = make('and')
    type(v, '/or')
    expect(isSlashOpen(v.state)).toBe(false)
    expect(v.state.doc.toString()).toBe('and/or')
    const w = make('')
    type(w, '/h')
    key(w, 'Escape')
    expect(isSlashOpen(w.state)).toBe(false)
    expect(w.state.doc.toString()).toBe('/h')
    const x = make('')
    type(x, '/zzz')
    expect(isSlashOpen(x.state)).toBe(false)
    expect(x.state.doc.toString()).toBe('/zzz')
  })

  it('matches keywords as well as labels, labels first', () => {
    expect(matchCommands('check').map(c => c.id)).toEqual(['todo'])
    expect(matchCommands('h')[0].id).toBe('h1')
  })

  it('the caret\'s empty line shows the hint, outside the text', () => {
    const v = make('intro\n\n')
    act(() => { v.focus(); v.dispatch({ selection: { anchor: v.state.doc.length } }) })
    // jsdom has no real focus for contenteditable; skip when the view cannot hold it.
    if (!v.hasFocus) return
    const line = v.contentDOM.querySelector<HTMLElement>('.cm-empty-hint')
    expect(line?.dataset.hint).toBe(EMPTY_LINE_HINT)
    expect(v.state.doc.toString()).toBe('intro\n\n')
  })
})

/**
 * HTML blocks in a doc, through a real CodeMirror view: an `html-embed` fence draws
 * as a sandboxed, transparent frame; Edit HTML shows its source until the caret
 * leaves; the "/" menu starts one; the conductor writes one with `doc-html`.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { act, fireEvent } from '@testing-library/react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { htmlBlocks, htmlExtension, setEmbedTheme } from '../pages/chat/page/docHtml'
import { slashExtension } from '../pages/chat/page/docSlash'
import { spaceAskExtension } from '../pages/chat/page/docSpaceAsk'
import { changeForWrite, htmlBlock, splitReply, type ConductorHost } from '../pages/chat/page/docConductor'

let view: EditorView | null = null
afterEach(() => { act(() => { view?.destroy() }); view = null; document.body.replaceChildren() })

const BLOCK = htmlBlock('<div style="color: var(--accent)">Hello</div>')

function make(doc: string, at = 0) {
  const host = { current: { send: () => {}, openSession: () => {}, markRead: () => {} } as ConductorHost }
  act(() => {
    view = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc, selection: { anchor: at }, extensions: [markdown({ base: markdownLanguage }), spaceAskExtension(host), slashExtension(), htmlExtension()] }),
    })
    view.dispatch({ effects: setEmbedTheme.of({ mode: 'dark', vars: { '--accent': '#9747ff' } }) })
  })
  return view!
}
const frame = (v: EditorView) => v.dom.querySelector<HTMLIFrameElement>('[data-testid="doc-html-frame"]')

describe('doc HTML blocks', () => {
  it('finds each block and draws it as a sandboxed frame with a see-through page', () => {
    const v = make(`intro\n\n${BLOCK}\n\nafter`)
    expect(htmlBlocks(v.state).map(b => b.html)).toEqual(['<div style="color: var(--accent)">Hello</div>'])
    const f = frame(v)!
    expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    const doc = f.getAttribute('srcdoc') ?? ''
    expect(doc).toContain('Hello')
    expect(doc).toContain('background:transparent!important')
    // The fence's text is not shown; the lines around it are.
    expect([...v.contentDOM.querySelectorAll('.cm-line')].map(l => l.textContent)).toEqual(['intro', '', '', 'after'])
  })

  it('Edit HTML shows the source until the caret leaves the block; Delete removes it', () => {
    const v = make(`intro\n\n${BLOCK}\n\nafter`)
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-html-edit"]')!) })
    expect(frame(v)).toBeNull()
    expect(v.contentDOM.textContent).toContain('Hello</div>')
    // happy-dom fires selectionchange inside a focused editor's own update; a real
    // browser does not. Out of focus, the caret move below is the same move.
    act(() => { v.contentDOM.blur() })
    act(() => { v.dispatch({ selection: { anchor: v.state.doc.length } }) })
    expect(frame(v)).not.toBeNull()
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-html-delete"]')!) })
    expect(v.state.doc.toString()).toBe('intro\n\n\nafter')
  })

  it('the "/" menu\'s HTML block starts an empty one, open with the caret inside', () => {
    const v = make('intro\n\n', 7)
    act(() => {
      const { from, to } = v.state.selection.main
      v.state.facet(EditorView.inputHandler).some(h => h(v, from, to, '/', () => v.state.update({ changes: { from, to, insert: '/' } })))
    })
    for (const ch of 'html') act(() => { const at = v.state.selection.main.head; v.dispatch({ changes: { from: at, insert: ch }, selection: { anchor: at + 1 } }) })
    act(() => { v.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })) })
    expect(v.state.doc.toString()).toBe(`intro\n\n${htmlBlock('<div>\n  \n</div>')}`)
    expect(frame(v)).toBeNull()
    expect(v.state.doc.lineAt(v.state.selection.main.head).text).toBe('  ')
  })

  it('the conductor\'s doc-html block becomes an HTML block in the doc, hidden from its prose', () => {
    const { prose, actions } = splitReply('Here is the chart.\n```doc-html after="Q3 numbers"\n<div>chart</div>\n```')
    expect(prose).toBe('Here is the chart.')
    expect(actions?.writes).toEqual([{ mode: 'after', after: 'Q3 numbers', text: htmlBlock('<div>chart</div>') }])
    const appended = splitReply('```doc-html\n<p>x</p>\n```').actions!.writes[0]
    expect(changeForWrite('# Plan\n', appended, () => null)).toEqual({ from: 7, insert: `\n${htmlBlock('<p>x</p>')}\n` })
    // Still streaming in: never shown as prose.
    expect(splitReply('Working on it\n```doc-html\n<div>par').prose).toBe('Working on it')
  })
})

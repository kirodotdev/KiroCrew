/**
 * HTML blocks in a doc (PoC). In the file a block is a fenced code block with the
 * `html-embed` info string, so the doc stays plain markdown:
 *
 *     ```html-embed
 *     <div>…</div>
 *     ```
 *
 * In the editor the whole fence is drawn as the live HTML (`DocHtmlFrame`). "Edit
 * HTML" shows its source in place, and once the caret leaves the source it is drawn
 * again. The conductor writes these with its `doc-html` block; the "/" menu's HTML
 * block starts an empty one, open for editing.
 */
import { createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { StateEffect, StateField, type EditorState, type Extension, type Range } from '@codemirror/state'
import { Decoration, EditorView, WidgetType, type DecorationSet } from '@codemirror/view'
import DocHtmlFrame, { type EmbedTheme } from './DocHtmlFrame'
import { HTML_FENCE } from './docConductor'

export interface HtmlBlock { from: number; to: number; html: string }

const OPEN = new RegExp(`^\`\`\`${HTML_FENCE}\\s*$`)
const CLOSE = /^```\s*$/

/** Every HTML block in the doc: an opening `html-embed` fence and the next closing one. */
export function htmlBlocks(state: EditorState): HtmlBlock[] {
  const doc = state.doc
  const out: HtmlBlock[] = []
  for (let n = 1; n <= doc.lines; n++) {
    if (!OPEN.test(doc.line(n).text)) continue
    let end = n + 1
    while (end <= doc.lines && !CLOSE.test(doc.line(end).text)) end++
    if (end > doc.lines) break
    const from = doc.line(n).from
    const to = doc.line(end).to
    const html = n + 1 <= end - 1 ? doc.sliceString(doc.line(n + 1).from, doc.line(end - 1).to) : ''
    out.push({ from, to, html })
    n = end
  }
  return out
}

/** The theme the frames draw with, pushed in by the pane whenever it changes. */
export const setEmbedTheme = StateEffect.define<EmbedTheme>()
const themeField = StateField.define<EmbedTheme>({
  create: () => ({ mode: 'light', vars: {} }),
  update: (v, tr) => tr.effects.reduce((acc, e) => (e.is(setEmbedTheme) ? e.value : acc), v),
})

/** The block whose source is showing, by where it starts; null for none. */
export const editHtml = StateEffect.define<number | null>()
const editingField = StateField.define<number | null>({
  create: () => null,
  update(v, tr) {
    let next = v === null ? null : tr.changes.mapPos(v, -1)
    for (const e of tr.effects) if (e.is(editHtml)) next = e.value
    if (next === null) return null
    // The source closes again once the caret is out of its block.
    const block = htmlBlocks(tr.state).find(b => b.from === next)
    if (!block) return null
    const head = tr.state.selection.main.head
    return head >= block.from && head <= block.to ? next : null
  },
})

/** Show the source of the block around the caret (after the "/" menu made one). */
export function editHtmlAtCaret(view: EditorView) {
  const head = view.state.selection.main.head
  const block = htmlBlocks(view.state).find(b => head >= b.from && head <= b.to)
  if (block) view.dispatch({ effects: editHtml.of(block.from) })
}

const roots = new WeakMap<HTMLElement, Root>()

class HtmlWidget extends WidgetType {
  constructor(readonly from: number, readonly html: string, readonly theme: EmbedTheme) { super() }
  eq(o: HtmlWidget) { return o.html === this.html && o.theme === this.theme && o.from === this.from }
  toDOM(view: EditorView) {
    const dom = document.createElement('div')
    dom.className = 'cm-html-embed'
    roots.set(dom, createRoot(dom))
    this.render(dom, view)
    return dom
  }
  updateDOM(dom: HTMLElement, view: EditorView) {
    if (!roots.has(dom)) return false
    this.render(dom, view)
    return true
  }
  render(dom: HTMLElement, view: EditorView) {
    const { from } = this
    roots.get(dom)?.render(createElement(DocHtmlFrame, {
      html: this.html,
      theme: this.theme,
      onEdit: () => {
        // The caret goes onto the block's first source line, which keeps it open.
        const line = view.state.doc.lineAt(from)
        const first = Math.min(view.state.doc.length, line.to + 1)
        view.dispatch({ effects: editHtml.of(from), selection: { anchor: first } })
        view.focus()
      },
      onDelete: () => {
        const block = htmlBlocks(view.state).find(b => b.from === from)
        if (!block) return
        const doc = view.state.doc
        const to = block.to < doc.length ? block.to + 1 : block.to
        view.dispatch({ changes: { from: block.from, to } })
        view.focus()
      },
      onHeight: () => view.requestMeasure(),
    }))
  }
  destroy(dom: HTMLElement) {
    const root = roots.get(dom)
    roots.delete(dom)
    if (root) queueMicrotask(() => root.unmount())
  }
  ignoreEvent() { return true }
}

function build(state: EditorState): DecorationSet {
  const editing = state.field(editingField)
  const theme = state.field(themeField)
  const decos: Range<Decoration>[] = []
  for (const b of htmlBlocks(state)) {
    if (b.from === editing) continue
    decos.push(Decoration.replace({ widget: new HtmlWidget(b.from, b.html, theme), block: true }).range(b.from, b.to))
  }
  return Decoration.set(decos)
}

const theme = EditorView.baseTheme({
  // Padding, not margin: a block widget's margin is not measured by the editor.
  '.cm-html-embed': { padding: '6px 0 10px' },
  '[data-he="root"]': { position: 'relative' },
  '[data-he="tools"]': {
    position: 'absolute', top: '4px', right: '4px', display: 'flex', gap: '4px', opacity: '0', transition: 'opacity .15s',
  },
  '[data-he="root"]:hover [data-he="tools"], [data-he="root"]:focus-within [data-he="tools"]': { opacity: '1' },
  '[data-he="tool"]': {
    display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '3px 8px', borderRadius: '6px',
    border: '1px solid var(--border)', background: 'var(--bg-elevated)', color: 'var(--text)', font: 'inherit',
    fontSize: '12px', cursor: 'pointer', boxShadow: '0 1px 4px rgba(0,0,0,.12)',
  },
  '[data-he="tool"]:hover': { background: 'var(--bg-hover)' },
})

export function htmlExtension(): Extension {
  const decos = StateField.define<DecorationSet>({
    create: build,
    update: (v, tr) => (tr.docChanged || tr.selection || tr.effects.some(e => e.is(setEmbedTheme) || e.is(editHtml)) ? build(tr.state) : v),
    provide: f => EditorView.decorations.from(f),
  })
  return [themeField, editingField, decos, theme]
}

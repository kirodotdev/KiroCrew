/**
 * Asking the conductor without the mouse (PoC), after Notion: Space at the start of
 * an empty line opens a full-width ask card on that line, and the block just above
 * is highlighted as what it is about. Up takes in one more block above, Down one
 * fewer (down to none: then it is about the doc, answered at that spot). Enter sends
 * through the same markers a highlight uses; Backspace on an empty card, or Escape,
 * puts it away and the caret is back on the line.
 */
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'
import { ensureSyntaxTree, syntaxTree } from '@codemirror/language'
import { Prec, StateEffect, StateField, type EditorState, type Extension, type Range } from '@codemirror/state'
import { Decoration, EditorView, WidgetType, keymap, type DecorationSet } from '@codemirror/view'
import { askMarkers, newAskId, parseMarkers, wrapAsk, type ConductorHost } from './docConductor'
import SpaceAskCard from './SpaceAskCard'

export interface Block { from: number; to: number }

// A goal's marker line is hidden too, so it is no block to ask about either.
const MARKERS_ONLY = /^(\s*<!-- \/?(?:ask|work):[A-Za-z0-9_.:-]+ -->\s*|\s*<!-- goal(?: g[a-z0-9]{3,16})?(?: every=\d+)?: .* -->\s*)+$/

/** The document's top-level blocks (paragraphs, headings, lists, tables, code) ending before *pos*, nearest first. */
export function blocksAbove(state: EditorState, pos: number): Block[] {
  const tree = ensureSyntaxTree(state, state.doc.length, 200) ?? syntaxTree(state)
  const out: Block[] = []
  const c = tree.topNode.cursor()
  if (!c.firstChild()) return out
  do {
    if (c.to > pos) break
    const text = state.doc.sliceString(c.from, c.to)
    // Hidden markers are not blocks of their own.
    if (!text.trim() || MARKERS_ONLY.test(text)) continue
    out.push({ from: c.from, to: c.to })
  } while (c.nextSibling())
  return out.reverse()
}

/** Whether *pos* is inside a code block, where Space is just a space. */
export function inCode(state: EditorState, pos: number): boolean {
  for (let n: ReturnType<ReturnType<typeof syntaxTree>['resolveInner']> | null = syntaxTree(state).resolveInner(pos, -1); n; n = n.parent) {
    if (n.name === 'FencedCode' || n.name === 'CodeBlock') return true
  }
  return false
}

interface Open { at: number; blocks: number }

const openSpace = StateEffect.define<Open>()
const setBlocks = StateEffect.define<number>()
const closeSpace = StateEffect.define<null>()

const spaceField = StateField.define<Open | null>({
  create: () => null,
  update(v, tr) {
    let next = v && tr.docChanged ? { ...v, at: tr.changes.mapPos(v.at, 1) } : v
    // Clicking or moving back into the text puts the card away.
    if (tr.selection && next) next = null
    for (const e of tr.effects) {
      if (e.is(openSpace)) next = e.value
      else if (e.is(setBlocks) && next) next = { ...next, blocks: e.value }
      else if (e.is(closeSpace)) next = null
    }
    return next
  },
})

export const isSpaceAskOpen = (state: EditorState) => !!state.field(spaceField, false)

/** Whether the caret is where a keyboard start belongs (Space for the ask card,
 *  "/" for the command menu): alone on an empty line, outside code and outside a
 *  highlight's hidden markers. */
export function atEmptyLine(state: EditorState): boolean {
  const sel = state.selection.main
  if (!sel.empty || state.field(spaceField, false)) return false
  if (state.doc.lineAt(sel.head).text !== '' || inCode(state, sel.head)) return false
  return !parseMarkers(state.doc.toString()).some(m => m.kind === 'ask' && m.open.to <= sel.head && sel.head <= m.close.from)
}

/** Open the ask card on the caret's empty line, about the block above. */
export function openSpaceAsk(view: EditorView): boolean {
  const { state } = view
  if (!atEmptyLine(state)) return false
  const line = state.doc.lineAt(state.selection.main.head)
  const max = blocksAbove(state, line.from).length
  view.dispatch({ effects: openSpace.of({ at: line.from, blocks: Math.min(1, max) }) })
  view.dom.querySelector<HTMLTextAreaElement>('[data-testid="space-ask-input"]')?.focus({ preventScroll: true })
  return true
}

const roots = new WeakMap<HTMLElement, Root>()

class SpaceWidget extends WidgetType {
  constructor(readonly blocks: number, readonly max: number, readonly host: { current: ConductorHost }) { super() }
  eq(o: SpaceWidget) { return o.blocks === this.blocks && o.max === this.max }
  toDOM(view: EditorView) {
    const dom = document.createElement('div')
    dom.className = 'cm-space-ask-host'
    roots.set(dom, createRoot(dom))
    // Rendered now, not on React's next tick: the next key (Up, to take in a block)
    // can arrive before a deferred render, and it would land in the document.
    flushSync(() => this.render(dom, view))
    return dom
  }
  // A new count re-renders in place, so what was typed stays.
  updateDOM(dom: HTMLElement, view: EditorView) {
    if (!roots.has(dom)) return false
    this.render(dom, view)
    return true
  }
  render(dom: HTMLElement, view: EditorView) {
    roots.get(dom)?.render(createElement(SpaceAskCard, {
      blocks: this.blocks,
      max: this.max,
      onGrow: () => view.dispatch({ effects: setBlocks.of(Math.min(this.max, this.blocks + 1)) }),
      onShrink: () => view.dispatch({ effects: setBlocks.of(Math.max(0, this.blocks - 1)) }),
      onCancel: () => closeAndReturn(view),
      onSend: (text: string) => sendSpaceAsk(view, this.host, text),
    }))
  }
  destroy(dom: HTMLElement) {
    const root = roots.get(dom)
    roots.delete(dom)
    // Unmounting during CodeMirror's own DOM update would re-enter React.
    if (root) queueMicrotask(() => root.unmount())
  }
  ignoreEvent() { return true }
}

/** Put the card away and the caret back on its line. */
function closeAndReturn(view: EditorView) {
  const open = view.state.field(spaceField, false)
  // Closing first: moving the selection in the same transaction would focus the
  // editor mid-update, from inside the card's own key handler.
  view.dispatch({ effects: closeSpace.of(null) })
  if (open) view.dispatch({ selection: { anchor: view.state.doc.lineAt(Math.min(open.at, view.state.doc.length)).from } })
  view.focus()
}

/** Wrap the chosen blocks (or mark the spot) as an ask and send it. */
export function sendSpaceAsk(view: EditorView, host: { current: ConductorHost }, text: string) {
  const open = view.state.field(spaceField, false)
  if (!open) return
  const picked = blocksAbove(view.state, open.at).slice(0, open.blocks)
  const id = newAskId()
  let quote: string | null
  if (picked.length) {
    quote = wrapAsk(view, picked[picked.length - 1].from, picked[0].to, id)
  } else {
    const [o, c] = askMarkers(id)
    view.dispatch({ changes: { from: view.state.doc.lineAt(open.at).from, insert: `${o}${c}` } })
    quote = ''
  }
  if (quote === null) return
  const line = view.state.doc.lineAt(Math.min(view.state.field(spaceField, false)?.at ?? open.at, view.state.doc.length))
  view.dispatch({ effects: closeSpace.of(null) })
  // The caret goes back to the line it was on, past a spot marker, ready to write.
  view.dispatch({ selection: { anchor: line.to } })
  view.focus()
  host.current.send(id, text, quote, view.state.doc.toString(), false)
}

function decorations(state: EditorState, host: { current: ConductorHost }): DecorationSet {
  const open = state.field(spaceField)
  if (!open) return Decoration.none
  const all = blocksAbove(state, open.at)
  const decos: Range<Decoration>[] = []
  for (const b of all.slice(0, open.blocks)) {
    const first = state.doc.lineAt(b.from).number
    const last = state.doc.lineAt(b.to).number
    // Only the lines with text: blank lines inside a block (code) stay bare.
    const lines: number[] = []
    for (let n = first; n <= last; n++) {
      const t = state.doc.line(n).text
      if (t.trim() && !MARKERS_ONLY.test(t)) lines.push(n)
    }
    lines.forEach((n, i) => {
      const cls = `cm-space-sel${i === 0 ? ' cm-space-sel-first' : ''}${i === lines.length - 1 ? ' cm-space-sel-last' : ''}`
      decos.push(Decoration.line({ class: cls }).range(state.doc.line(n).from))
    })
  }
  const line = state.doc.lineAt(Math.min(open.at, state.doc.length))
  decos.push(Decoration.widget({ widget: new SpaceWidget(open.blocks, all.length, host), block: true, side: 1 }).range(line.to))
  return Decoration.set(decos, true)
}

const theme = EditorView.baseTheme({
  // The chosen blocks, each its own soft tint, as Notion marks them.
  '.cm-space-sel': { backgroundColor: 'color-mix(in srgb, var(--accent) 13%, transparent)' },
  '.cm-space-sel-first': { borderTopLeftRadius: '4px', borderTopRightRadius: '4px' },
  '.cm-space-sel-last': { borderBottomLeftRadius: '4px', borderBottomRightRadius: '4px' },
  '.cm-space-ask-host': { padding: '4px 0 10px' },
  '[data-sa="root"]': {
    border: '1px solid var(--border)', borderRadius: '10px', background: 'var(--bg-elevated)', color: 'var(--text)',
    boxShadow: '0 8px 28px rgba(0,0,0,.14), 0 1px 3px rgba(0,0,0,.08)', padding: '9px 10px 7px 12px',
  },
  '[data-sa="bar"]': { display: 'flex', alignItems: 'flex-start', gap: '10px' },
  '[data-sa="icon"]': { flexShrink: '0', marginTop: '4px', color: 'var(--accent)' },
  '[data-sa="root"] textarea': {
    flex: '1', minWidth: '0', border: 'none', outline: 'none', resize: 'none', background: 'transparent', color: 'var(--text)',
    font: 'inherit', fontSize: '15px', lineHeight: '1.5', padding: '1px 0', maxHeight: '220px', overflowY: 'auto',
  },
  '[data-sa="root"] textarea::placeholder': { color: 'var(--muted)' },
  '[data-sa="send"]': {
    flexShrink: '0', display: 'flex', alignItems: 'center', justifyContent: 'center', width: '26px', height: '26px',
    border: 'none', borderRadius: '999px', background: 'var(--accent)', color: 'var(--bg)', cursor: 'pointer',
  },
  '[data-sa="send"]:disabled': { background: 'var(--border-strong)', color: 'var(--bg)', cursor: 'default' },
  '[data-sa="hint"]': {
    display: 'flex', justifyContent: 'space-between', gap: '12px', marginTop: '5px', paddingLeft: '26px',
    fontSize: '11.5px', color: 'var(--muted)', lineHeight: '18px',
  },
  '[data-sa="keys"] kbd': {
    font: 'inherit', fontSize: '10.5px', padding: '0 4px', margin: '0 1px', borderRadius: '4px', border: '1px solid var(--border)',
  },
})

export function spaceAskExtension(host: { current: ConductorHost }): Extension {
  const decos = StateField.define<DecorationSet>({
    create: s => decorations(s, host),
    update: (v, tr) => (tr.docChanged || tr.effects.some(e => e.is(openSpace) || e.is(setBlocks) || e.is(closeSpace)) ? decorations(tr.state, host) : v),
    provide: f => EditorView.decorations.from(f),
  })
  const open = Prec.high(keymap.of([{ key: 'Space', run: openSpaceAsk }]))
  // While the card is open, its keys mean the same wherever the focus is, so a key
  // that beats the box to its focus does not move the caret instead.
  const whileOpen = (run: (view: EditorView, open: Open) => void) => (view: EditorView) => {
    const o = view.state.field(spaceField)
    if (!o) return false
    run(view, o)
    return true
  }
  const keys = Prec.highest(keymap.of([
    { key: 'ArrowUp', run: whileOpen((v, o) => v.dispatch({ effects: setBlocks.of(Math.min(blocksAbove(v.state, o.at).length, o.blocks + 1)) })) },
    { key: 'ArrowDown', run: whileOpen((v, o) => v.dispatch({ effects: setBlocks.of(Math.max(0, o.blocks - 1)) })) },
    { key: 'Escape', run: whileOpen(v => closeAndReturn(v)) },
    { key: 'Backspace', run: whileOpen(v => closeAndReturn(v)) },
  ]))
  return [spaceField, decos, open, keys, theme]
}

/**
 * The doc's "/" command menu and its empty-line hint (PoC), after Notion.
 *
 * "/" typed at the start of an empty line is a real slash in the text, and it opens
 * a menu of blocks filtered by what follows it ("/tab" leaves Table). Up / Down
 * choose, Enter or Tab replaces the "/query" with that block, Escape closes the menu
 * and keeps what was typed. A query that matches nothing closes it too, so a slash
 * that is just a slash costs nothing; anywhere else ("and/or", a path) "/" is only
 * ever text.
 *
 * The caret's empty line also shows a hint, drawn by CSS and never part of the
 * text: Space for the conductor, "/" for blocks.
 */
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'
import { Prec, StateEffect, StateField, type EditorState, type Extension } from '@codemirror/state'
import {
  Decoration, EditorView, ViewPlugin, keymap, showTooltip, type DecorationSet, type Tooltip, type TooltipView, type ViewUpdate,
} from '@codemirror/view'
import SlashMenu, { type SlashIcon, type SlashItem } from './SlashMenu'
import { atEmptyLine, openSpaceAsk } from './docSpaceAsk'
import { insertTable } from './docTables'
import { openMentionPicker } from './docMentions'
import { editHtmlAtCaret } from './docHtml'
import { htmlBlock } from './docConductor'

/** A new HTML block: an empty element with the caret inside it, ready to write. */
const HTML_TEMPLATE = htmlBlock('<div>\n  \n</div>')
import type { StyleSpec } from './styleSpec'

export const EMPTY_LINE_HINT = 'Press Space for AI, or / for commands'

interface Command extends SlashItem {
  keywords: string
  /** What replaces the "/query": text, with the caret at *caret* within it (default: its end). */
  insert?: string
  caret?: number
  /** Or an action run once the "/query" is gone. */
  then?: (view: EditorView) => void
}

const cmd = (id: string, label: string, hint: string, icon: SlashIcon, keywords: string, rest: Partial<Command>): Command =>
  ({ id, label, hint, icon, keywords, ...rest })

export const COMMANDS: Command[] = [
  cmd('ai', 'Ask AI', 'Space', 'ai', 'ask conductor ai chat', { then: v => { openSpaceAsk(v) } }),
  cmd('text', 'Text', '', 'text', 'paragraph plain', { insert: '' }),
  cmd('h1', 'Heading 1', '#', 'h1', 'title big h1', { insert: '# ' }),
  cmd('h2', 'Heading 2', '##', 'h2', 'subtitle h2', { insert: '## ' }),
  cmd('h3', 'Heading 3', '###', 'h3', 'h3', { insert: '### ' }),
  cmd('bullet', 'Bulleted list', '-', 'bullet', 'bullet list unordered ul', { insert: '- ' }),
  cmd('number', 'Numbered list', '1.', 'number', 'numbered list ordered ol', { insert: '1. ' }),
  cmd('todo', 'To-do list', '[ ]', 'todo', 'todo task checkbox check', { insert: '- [ ] ' }),
  cmd('table', 'Table', '', 'table', 'table grid', { insert: '', then: v => { insertTable(v) } }),
  cmd('html', 'HTML block', '</>', 'html', 'html embed widget chart custom code live', {
    insert: HTML_TEMPLATE, caret: HTML_TEMPLATE.indexOf('\n  \n') + 3, then: v => { editHtmlAtCaret(v) },
  }),
  cmd('link', 'Link to session', '@', 'link', 'link mention session page board doc chat', { insert: '', then: v => { openMentionPicker(v) } }),
  cmd('quote', 'Quote', '>', 'quote', 'quote blockquote', { insert: '> ' }),
  cmd('code', 'Code block', '```', 'code', 'code snippet fence', { insert: '```\n\n```', caret: 4 }),
  cmd('divider', 'Divider', '---', 'divider', 'divider rule line hr separator', { insert: '---\n' }),
]

/** The commands *query* (the text after the slash) leaves, best first. */
export function matchCommands(query: string): Command[] {
  const q = query.trim().toLowerCase()
  if (!q) return COMMANDS
  const starts = COMMANDS.filter(c => c.label.toLowerCase().startsWith(q))
  const rest = COMMANDS.filter(c => !starts.includes(c) && (c.label.toLowerCase().includes(q) || c.keywords.split(' ').some(k => k.startsWith(q))))
  return [...starts, ...rest]
}

interface Open { from: number; index: number; tooltip: Tooltip }

const openSlash = StateEffect.define<number>()
const setIndex = StateEffect.define<number>()
const closeSlash = StateEffect.define<null>()

const query = (state: EditorState, from: number) => state.doc.sliceString(from + 1, state.selection.main.head)

/** The menu is still standing when its slash is still there, alone at its line's
 *  start, with the caret after it on that line and something left to match. */
function stillOpen(state: EditorState, v: Open): boolean {
  if (v.from >= state.doc.length || state.doc.sliceString(v.from, v.from + 1) !== '/') return false
  const line = state.doc.lineAt(v.from)
  const head = state.selection.main
  if (line.from !== v.from || !head.empty || head.head <= v.from || head.head > line.to) return false
  return matchCommands(query(state, v.from)).length > 0
}

const slashField = StateField.define<Open | null>({
  create: () => null,
  update(v, tr) {
    let next = v
    if (next && tr.docChanged) next = { ...next, from: tr.changes.mapPos(next.from, -1), index: 0 }
    for (const e of tr.effects) {
      if (e.is(openSlash)) next = { from: e.value, index: 0, tooltip: menuTooltip(e.value) }
      else if (e.is(setIndex) && next) next = { ...next, index: e.value }
      else if (e.is(closeSlash)) next = null
    }
    return next && stillOpen(tr.state, next) ? next : null
  },
  provide: f => showTooltip.from(f, v => v?.tooltip ?? null),
})

export const isSlashOpen = (state: EditorState) => !!state.field(slashField, false)

/** Replace the "/query" with *command*. *clicked*: picked with the mouse, so the
 *  editor takes the focus back (from the keyboard it never lost it). */
export function runCommand(view: EditorView, command: Command, clicked = false) {
  const open = view.state.field(slashField, false)
  if (!open) return
  const to = view.state.selection.main.head
  const insert = command.insert ?? ''
  view.dispatch({
    changes: { from: open.from, to, insert },
    selection: { anchor: open.from + (command.caret ?? insert.length) },
    effects: closeSlash.of(null),
  })
  if (clicked) view.focus()
  command.then?.(view)
}

const roots = new WeakMap<HTMLElement, Root>()

/** One tooltip for the menu's whole life, so a keystroke updates the list in place. */
function menuTooltip(from: number): Tooltip {
  return {
    pos: from, above: false, arrow: false,
    create: (view): TooltipView => {
      const dom = document.createElement('div')
      dom.className = 'cm-slash-host'
      const root = createRoot(dom)
      roots.set(dom, root)
      const render = () => {
        const open = view.state.field(slashField, false)
        if (!open) return
        const items = matchCommands(query(view.state, open.from))
        root.render(createElement(SlashMenu, {
          items, index: Math.min(open.index, items.length - 1),
          onPick: i => runCommand(view, items[i], true),
          onHover: i => view.dispatch({ effects: setIndex.of(i) }),
        }))
      }
      // Drawn now, not on React's next tick, so the first frame already has its rows.
      flushSync(render)
      return {
        dom,
        update: (u: ViewUpdate) => { if (u.docChanged || u.selectionSet || u.transactions.some(t => t.effects.length)) render() },
        destroy: () => { roots.delete(dom); queueMicrotask(() => root.unmount()) },
      }
    },
  }
}

/** "/" at the start of an empty line: the slash goes in, and the menu opens on it. */
const trigger = EditorView.inputHandler.of((view, from, to, text) => {
  if (text !== '/' || from !== to || !atEmptyLine(view.state)) return false
  view.dispatch({ changes: { from, insert: '/' }, selection: { anchor: from + 1 }, effects: openSlash.of(from) })
  return true
})

const keys = Prec.highest(keymap.of([
  ['ArrowDown', 1], ['ArrowUp', -1],
].map(([key, step]) => ({
  key: key as string,
  run: (view: EditorView) => {
    const open = view.state.field(slashField, false)
    if (!open) return false
    const n = matchCommands(query(view.state, open.from)).length
    view.dispatch({ effects: setIndex.of((open.index + (step as number) + n) % n) })
    return true
  },
})).concat(['Enter', 'Tab'].map(key => ({
  key,
  run: (view: EditorView) => {
    const open = view.state.field(slashField, false)
    if (!open) return false
    const items = matchCommands(query(view.state, open.from))
    runCommand(view, items[Math.min(open.index, items.length - 1)])
    return true
  },
}))).concat([{
  key: 'Escape',
  run: (view: EditorView) => {
    if (!view.state.field(slashField, false)) return false
    view.dispatch({ effects: closeSlash.of(null) })
    return true
  },
}])))

/** The hint on the caret's empty line: a line class whose ::before draws the text. */
const hint = ViewPlugin.fromClass(class {
  decorations: DecorationSet
  constructor(view: EditorView) { this.decorations = this.build(view) }
  update(u: ViewUpdate) {
    if (u.docChanged || u.selectionSet || u.focusChanged || u.transactions.some(t => t.effects.length)) this.decorations = this.build(u.view)
  }
  build(view: EditorView): DecorationSet {
    const { state } = view
    // An empty doc shows its own placeholder instead.
    if (!view.hasFocus || state.doc.length === 0 || isSlashOpen(state) || !atEmptyLine(state)) return Decoration.none
    const line = state.doc.lineAt(state.selection.main.head)
    return Decoration.set([Decoration.line({ class: 'cm-empty-hint', attributes: { 'data-hint': EMPTY_LINE_HINT } }).range(line.from)])
  }
}, { decorations: p => p.decorations })

/**
 * The look of a keyboard menu (the "/" blocks, the "@" sessions), by its `data-sm`
 * parts. Exported so the whiteboard's "@" picker wears the same outside an editor.
 */
export const MENU_STYLE: StyleSpec = {
  '[data-sm="root"]': {
    width: '300px', marginTop: '6px', borderRadius: '10px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
    color: 'var(--text)', boxShadow: '0 12px 32px -6px rgba(0,0,0,.22), 0 1px 3px rgba(0,0,0,.08)', overflow: 'hidden',
  },
  '[data-sm="list"]': { maxHeight: '320px', overflowY: 'auto', padding: '6px' },
  '[data-sm="item"]': {
    display: 'flex', alignItems: 'center', gap: '10px', padding: '5px 8px', borderRadius: '6px', cursor: 'pointer',
    fontSize: '14px', lineHeight: '20px',
  },
  '[data-sm="item"][aria-selected="true"]': { background: 'var(--bg-hover)' },
  '[data-sm="icon"]': {
    display: 'flex', alignItems: 'center', justifyContent: 'center', width: '28px', height: '28px', flexShrink: '0',
    borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--muted-strong)',
  },
  '[data-sm="icon"][data-ai="true"]': { color: 'var(--accent)' },
  '[data-sm="label"]': { flex: '1', minWidth: '0' },
  '[data-sm="hint"]': { fontSize: '12px', color: 'var(--muted)', fontFamily: 'var(--font-mono, ui-monospace, monospace)' },
  '[data-sm="foot"]': {
    padding: '6px 12px', borderTop: '1px solid var(--border)', fontSize: '11.5px', color: 'var(--muted)',
  },
  '[data-sm="foot"] kbd': {
    font: 'inherit', fontSize: '10.5px', padding: '0 4px', margin: '0 1px', borderRadius: '4px', border: '1px solid var(--border)',
  },
}

const theme = EditorView.baseTheme({
  '.cm-empty-hint': { position: 'relative' },
  '.cm-empty-hint::before': {
    content: 'attr(data-hint)', position: 'absolute', left: '0', top: '0', color: 'var(--muted)', opacity: '0.75',
    pointerEvents: 'none', whiteSpace: 'nowrap',
  },
  '.cm-tooltip.cm-slash-host': { border: 'none', background: 'transparent' },
  ...MENU_STYLE,
})

export function slashExtension(): Extension {
  return [slashField, trigger, keys, hint, theme]
}

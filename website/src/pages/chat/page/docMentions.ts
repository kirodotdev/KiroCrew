/**
 * Links to other sessions in a doc (PoC), after Notion's page mentions.
 *
 * In the file a link is plain markdown with a `session:` address,
 * `[Q3 planning](session:<key>)`, so the doc stays readable anywhere. In the editor
 * that text is drawn as one chip: the session's kind icon and its live title (a
 * rename shows everywhere it is linked; the written title is the fallback when the
 * session is gone). The caret steps over a chip as one unit, and a click opens the
 * session.
 *
 * "@" at the start of a word opens a picker of sessions, filtered by what follows
 * it; Enter or Tab links the chosen one, Escape keeps what was typed. The "/" menu's
 * "Link to session" opens the same picker.
 */
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'
import { Prec, RangeSet, StateEffect, StateField, type EditorState, type Extension, type Range } from '@codemirror/state'
import {
  Decoration, EditorView, WidgetType, keymap, showTooltip, type DecorationSet, type Tooltip, type TooltipView, type ViewUpdate,
} from '@codemirror/view'
import { MentionChip, MentionMenu, type SessionRef } from './SessionMention'
import { inCode } from './docSpaceAsk'
import type { StyleSpec } from './styleSpec'

/** A session link in markdown: `[title](session:<key>)`. */
export const SESSION_LINK = /\[([^\]\n]*)\]\(session:([A-Za-z0-9_.:@-]+)\)/g
const LINK = SESSION_LINK
const MAX_ITEMS = 8
const MAX_QUERY = 60

interface MentionData { sessions: SessionRef[]; self: string | null }

/** The sessions a doc can link to, pushed in by the pane whenever they change. */
export const setMentionSessions = StateEffect.define<MentionData>()

const mentionData = StateField.define<MentionData>({
  create: () => ({ sessions: [], self: null }),
  update: (v, tr) => tr.effects.reduce((acc, e) => (e.is(setMentionSessions) ? e.value : acc), v),
})

/** The markdown for a link to *session*: its title, kept free of what would end the link. */
export const mentionMarkdown = (s: Pick<SessionRef, 'key' | 'title'>) => `[${s.title.replace(/[[\]\n]/g, ' ').trim()}](session:${s.key})`

/** The sessions *query* leaves, title prefix matches first, the doc's own session never. */
export function matchSessions(data: MentionData, query: string): SessionRef[] {
  const q = query.trim().toLowerCase()
  const pool = data.sessions.filter(s => s.key !== data.self)
  if (!q) return pool.slice(0, MAX_ITEMS)
  const starts = pool.filter(s => s.title.toLowerCase().startsWith(q))
  const rest = pool.filter(s => !starts.includes(s) && s.title.toLowerCase().includes(q))
  return [...starts, ...rest].slice(0, MAX_ITEMS)
}

// ---- the chips ---------------------------------------------------------------------

const roots = new WeakMap<HTMLElement, Root>()

class MentionWidget extends WidgetType {
  constructor(readonly key: string, readonly written: string, readonly session: SessionRef | null, readonly host: { current: { openSession: (key: string) => void } }) { super() }
  eq(o: MentionWidget) { return o.key === this.key && o.written === this.written && JSON.stringify(o.session) === JSON.stringify(this.session) }
  toDOM() {
    const dom = document.createElement('span')
    dom.className = 'cm-mention'
    dom.setAttribute('data-mention', '')
    dom.setAttribute('data-testid', 'session-mention')
    dom.setAttribute('role', 'link')
    roots.set(dom, createRoot(dom))
    this.paint(dom)
    dom.onmousedown = e => e.preventDefault()
    dom.onclick = () => { if (this.session) this.host.current.openSession(this.key) }
    return dom
  }
  updateDOM(dom: HTMLElement) {
    if (!roots.has(dom)) return false
    this.paint(dom)
    // The handler reads the widget it was made with: keep it pointed at this one.
    dom.onclick = () => { if (this.session) this.host.current.openSession(this.key) }
    return true
  }
  paint(dom: HTMLElement) {
    dom.dataset.key = this.key
    dom.dataset.kind = this.session?.kind ?? 'missing'
    dom.title = this.session ? `Open ${this.session.title}` : 'This session no longer exists'
    dom.setAttribute('aria-label', this.session ? `Open session ${this.session.title}` : `Deleted session ${this.written}`)
    // Drawn now, so a chip is never an empty box for a frame.
    flushSync(() => roots.get(dom)?.render(createElement(MentionChip, { session: this.session, fallback: this.written })))
  }
  destroy(dom: HTMLElement) {
    const root = roots.get(dom)
    roots.delete(dom)
    if (root) queueMicrotask(() => root.unmount())
  }
  ignoreEvent() { return true }
}

function build(state: EditorState, host: { current: { openSession: (key: string) => void } }) {
  const data = state.field(mentionData)
  const byKey = new Map(data.sessions.map(s => [s.key, s]))
  const text = state.doc.toString()
  const decos: Range<Decoration>[] = []
  for (const m of text.matchAll(LINK)) {
    const from = m.index ?? 0
    if (inCode(state, from + 1)) continue
    decos.push(Decoration.replace({ widget: new MentionWidget(m[2], m[1], byKey.get(m[2]) ?? null, host) }).range(from, from + m[0].length))
  }
  return Decoration.set(decos, true)
}

// ---- the "@" picker ------------------------------------------------------------------

interface Open { from: number; index: number; tooltip: Tooltip }

const openPicker = StateEffect.define<number>()
const setIndex = StateEffect.define<number>()
const closePicker = StateEffect.define<null>()

const query = (state: EditorState, from: number) => state.doc.sliceString(from + 1, state.selection.main.head)

function stillOpen(state: EditorState, v: Open): boolean {
  if (state.doc.sliceString(v.from, v.from + 1) !== '@') return false
  const sel = state.selection.main
  const line = state.doc.lineAt(v.from)
  if (!sel.empty || sel.head <= v.from || sel.head > line.to) return false
  const q = query(state, v.from)
  if (q.length > MAX_QUERY) return false
  // Prose that only looks like a mention ("@home") closes the picker by matching nothing.
  return !q || matchSessions(state.field(mentionData), q).length > 0
}

const pickerField = StateField.define<Open | null>({
  create: () => null,
  update(v, tr) {
    let next = v
    if (next && tr.docChanged) next = { ...next, from: tr.changes.mapPos(next.from, -1), index: 0 }
    for (const e of tr.effects) {
      if (e.is(openPicker)) next = { from: e.value, index: 0, tooltip: pickerTooltip(e.value) }
      else if (e.is(setIndex) && next) next = { ...next, index: e.value }
      else if (e.is(closePicker)) next = null
    }
    return next && stillOpen(tr.state, next) ? next : null
  },
  provide: f => showTooltip.from(f, v => v?.tooltip ?? null),
})

export const isMentionOpen = (state: EditorState) => !!state.field(pickerField, false)

function link(view: EditorView, session: SessionRef, clicked = false) {
  const open = view.state.field(pickerField, false)
  if (!open) return
  const insert = `${mentionMarkdown(session)} `
  view.dispatch({
    changes: { from: open.from, to: view.state.selection.main.head, insert },
    selection: { anchor: open.from + insert.length },
    effects: closePicker.of(null),
  })
  if (clicked) view.focus()
}

function pickerTooltip(from: number): Tooltip {
  return {
    pos: from, above: false, arrow: false,
    create: (view): TooltipView => {
      const dom = document.createElement('div')
      dom.className = 'cm-slash-host'
      const root = createRoot(dom)
      const render = () => {
        const open = view.state.field(pickerField, false)
        if (!open) return
        const q = query(view.state, open.from)
        const items = matchSessions(view.state.field(mentionData), q)
        root.render(createElement(MentionMenu, {
          items, query: q, index: Math.min(open.index, Math.max(0, items.length - 1)),
          onPick: i => link(view, items[i], true),
          onHover: i => view.dispatch({ effects: setIndex.of(i) }),
        }))
      }
      flushSync(render)
      return {
        dom,
        update: (u: ViewUpdate) => { if (u.docChanged || u.selectionSet || u.transactions.some(t => t.effects.length)) render() },
        destroy: () => queueMicrotask(() => root.unmount()),
      }
    },
  }
}

/** Put an "@" at the caret and open the picker on it (the "/" menu's "Link to session"). */
export function openMentionPicker(view: EditorView): boolean {
  const at = view.state.selection.main.head
  view.dispatch({ changes: { from: at, insert: '@' }, selection: { anchor: at + 1 }, effects: openPicker.of(at) })
  return true
}

/** "@" at the start of a word (line start, after a space or an opening bracket), outside code. */
const trigger = EditorView.inputHandler.of((view, from, to, text) => {
  if (text !== '@' || from !== to) return false
  const before = from > 0 ? view.state.doc.sliceString(from - 1, from) : ''
  if (before && !/[\s([{]/.test(before)) return false
  if (inCode(view.state, from)) return false
  view.dispatch({ changes: { from, insert: '@' }, selection: { anchor: from + 1 }, effects: openPicker.of(from) })
  return true
})

const keys = Prec.highest(keymap.of([
  ...([['ArrowDown', 1], ['ArrowUp', -1]] as const).map(([key, step]) => ({
    key,
    run: (view: EditorView) => {
      const open = view.state.field(pickerField, false)
      if (!open) return false
      const n = matchSessions(view.state.field(mentionData), query(view.state, open.from)).length
      if (n) view.dispatch({ effects: setIndex.of((open.index + step + n) % n) })
      return true
    },
  })),
  ...['Enter', 'Tab'].map(key => ({
    key,
    run: (view: EditorView) => {
      const open = view.state.field(pickerField, false)
      if (!open) return false
      const items = matchSessions(view.state.field(mentionData), query(view.state, open.from))
      if (!items.length) { view.dispatch({ effects: closePicker.of(null) }); return false }
      link(view, items[Math.min(open.index, items.length - 1)])
      return true
    },
  })),
  {
    key: 'Escape',
    run: (view: EditorView) => {
      if (!view.state.field(pickerField, false)) return false
      view.dispatch({ effects: closePicker.of(null) })
      return true
    },
  },
]))

/**
 * The chip's and the picker's own parts. Exported so the whiteboard, which draws
 * the same chips and picker outside an editor, wears exactly the same.
 */
export const MENTION_STYLE: StyleSpec = {
  // Notion's page mention: the icon, then the title with a quiet underline; the
  // whole chip tints on hover.
  '[data-mention]': {
    // A board chip is a button: it takes the text's font and nothing of its own.
    font: 'inherit', border: 'none', background: 'transparent', textAlign: 'inherit', verticalAlign: 'baseline',
    // Tight on the right: the text's own space follows a chip, and any more
    // reads as a gap before the next word or the full stop.
    display: 'inline-flex', alignItems: 'baseline', gap: '4px', padding: '0 1px 0 2px', margin: '0 -1px 0 0',
    borderRadius: '4px', cursor: 'pointer', color: 'var(--text-strong)', fontWeight: '500',
    transition: 'background-color .12s',
  },
  '[data-mention]:hover': { backgroundColor: 'var(--bg-hover)' },
  '[data-mn="icon"]': { alignSelf: 'center', display: 'inline-flex', color: 'var(--muted-strong)', transform: 'translateY(0.5px)' },
  '[data-mention][data-kind="board"] [data-mn="icon"], [data-mention][data-kind="doc"] [data-mn="icon"]': { color: 'var(--accent)' },
  '[data-mn="title"]': {
    borderBottom: '1px solid color-mix(in srgb, var(--text) 22%, transparent)', lineHeight: '1.3',
  },
  '[data-mention]:hover [data-mn="title"]': { borderBottomColor: 'color-mix(in srgb, var(--text) 50%, transparent)' },
  '[data-mention][data-kind="missing"]': { cursor: 'default', color: 'var(--muted)' },
  '[data-mention][data-kind="missing"] [data-mn="title"]': { textDecoration: 'line-through', borderBottomColor: 'transparent' },
  // A session that is working right now: a small live dot after its title.
  '[data-mn="running"]': {
    alignSelf: 'center', width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent)',
    animation: 'cm-ask-pulse 1.2s ease-in-out infinite',
  },
  '[data-mn="menu-head"]': { padding: '8px 12px 2px', fontSize: '11.5px', color: 'var(--muted)' },
  '[data-mn="kind"]': { fontSize: '11.5px', color: 'var(--muted)' },
  '[data-mn="empty"]': { padding: '8px', fontSize: '13px', color: 'var(--muted)' },
}

const theme = EditorView.baseTheme({
  ...MENTION_STYLE,
  '@media (prefers-reduced-motion: reduce)': { '[data-mn="running"]': { animation: 'none' } },
})

export function mentionExtension(host: { current: { openSession: (key: string) => void } }): Extension {
  const chips = StateField.define<DecorationSet>({
    create: s => build(s, host),
    update: (v, tr) => (tr.docChanged || tr.effects.some(e => e.is(setMentionSessions)) ? build(tr.state, host) : v),
    provide: f => [EditorView.decorations.from(f), EditorView.atomicRanges.of(view => view.state.field(f) as RangeSet<Decoration>)],
  })
  return [mentionData, chips, pickerField, trigger, keys, theme]
}

/**
 * The doc pane's conductor layer (PoC). The document is the only surface: every
 * message to the conductor starts from a highlight, and its answer sits right under
 * the highlight without ever becoming part of the text.
 *
 * - Select text, press "Ask conductor", type, Enter. The selection (trimmed of the
 *   blank space around it) is wrapped in hidden markers (`<!-- ask:ID -->...`, on
 *   their own lines when the selection spans lines or touches a table) and the host
 *   sends the turn.
 * - The highlight's own text is tinted. Under its last line sits the conductor's
 *   answer (`DocAnswer`): a block widget, collapsed to one line by default, that
 *   the caret, arrow keys and Enter pass straight by; only a click opens it, and
 *   the conversation continues there.
 * - A `<!-- work:SESSION -->` section is a worker's: a bar runs down its lines. The
 *   workers an answer started are listed, live, in that answer.
 *
 * Markers are part of the markdown (so they survive reloads and edits), hidden and
 * atomic in the editor. Thread text never enters the document.
 */
import { createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { StateEffect, StateField, type EditorState, type Extension, type Range } from '@codemirror/state'
import {
  Decoration, EditorView, WidgetType, showTooltip, type DecorationSet, type Tooltip,
} from '@codemirror/view'
import { isRow } from './docTables'
import { askRows, closeDraft, requestRowAsk } from './docAskRows'
import DocAnswer from './DocAnswer'
import { goalIdOf, goalThread, headingSections } from './docGoals'
import type { StyleSpec } from './styleSpec'

// ---- markers -----------------------------------------------------------------

export type MarkerKind = 'ask' | 'work'

export interface Marker {
  kind: MarkerKind
  id: string
  open: { from: number; to: number }
  close: { from: number; to: number }
  /** Both markers sit on lines of their own. */
  ownLine: boolean
}

const MARK_RE = /<!-- (\/?)(ask|work):([A-Za-z0-9_.:-]+) -->/g

function aloneOnLine(text: string, from: number, to: number): boolean {
  const start = text.lastIndexOf('\n', from - 1) + 1
  const endAt = text.indexOf('\n', to)
  const end = endAt === -1 ? text.length : endAt
  return text.slice(start, from).trim() === '' && text.slice(to, end).trim() === ''
}

/** Every well-formed marker pair in *text*, in document order. */
export function parseMarkers(text: string): Marker[] {
  const open = new Map<string, { from: number; to: number }>()
  const out: Marker[] = []
  for (const m of text.matchAll(MARK_RE)) {
    const from = m.index ?? 0
    const to = from + m[0].length
    const key = `${m[2]}:${m[3]}`
    if (!m[1]) { open.set(key, { from, to }); continue }
    const o = open.get(key)
    if (!o) continue
    open.delete(key)
    out.push({
      kind: m[2] as MarkerKind, id: m[3], open: o, close: { from, to },
      ownLine: aloneOnLine(text, o.from, o.to) && aloneOnLine(text, from, to),
    })
  }
  return out.sort((a, b) => a.open.from - b.open.from)
}

export const askMarkers = (id: string) => [`<!-- ask:${id} -->`, `<!-- /ask:${id} -->`] as const
export const workMarkers = (key: string) => [`<!-- work:${key} -->`, `<!-- /work:${key} -->`] as const

/** The text between a marker pair, markers excluded. */
export function markerContent(text: string, m: Marker): string {
  return text.slice(m.open.to, m.close.from).replace(/^\n|\n$/g, '')
}

/** [from, to] without the whitespace at either end; null when nothing is left. */
export function trimRange(text: string, from: number, to: number): { from: number; to: number } | null {
  while (from < to && /\s/.test(text[from])) from++
  while (to > from && /\s/.test(text[to - 1])) to--
  return to > from ? { from, to } : null
}

// ---- data the host pushes in ---------------------------------------------------

export interface ThreadMsg { who: 'you' | 'conductor'; text: string }
export type WorkerState = 'working' | 'needs_you' | 'idle'
export interface WorkerView { title: string; state: WorkerState }
export interface ThreadView {
  messages: ThreadMsg[]
  pending: boolean
  unread: boolean
  /** The workers this thread's answers started, with their live state. */
  workers?: (WorkerView & { key: string })[]
  /** What it is about, when the highlight alone does not say (a table row). */
  topic?: string
}
export interface DocData {
  threads: Record<string, ThreadView>
  workers: Record<string, WorkerView>
  /** The page's current conversation, and a counter that moves focus to it. */
  active?: { id: string; focus: number } | null
  /** A goal check is running: a goal with no report of its own yet shows it working. */
  goalsPending?: boolean
}

export interface ConductorHost {
  /** Send one turn about ask *id*. *doc* is the text with the markers in it. */
  send: (id: string, request: string, quote: string, doc: string, followUp: boolean) => void
  openSession: (key: string) => void
  markRead: (id: string) => void
  /** A click opened this conversation: it becomes the page's current one. */
  activate?: (id: string) => void
  /** Threads open in the page's side panel, not inline under their highlight. */
  threadsInSidePanel?: boolean
}

/** What thread *id* is about, as its next turn quotes it: a goal's section as it
 *  stands now, or the text between the ask's markers ("" for an empty spot). */
export function threadQuote(state: EditorState, id: string): string {
  if (id.startsWith('goal-')) {
    const sec = headingSections(state).find(s => s.goal && goalIdOf(s) === id.slice(5))
    return sec ? state.doc.sliceString(state.doc.line(sec.line).from, state.doc.line(sec.endLine).to) : ''
  }
  const doc = state.doc.toString()
  const marker = parseMarkers(doc).find(m => m.kind === 'ask' && m.id === id)
  return marker ? markerContent(doc, marker) : ''
}

export const setDocData = StateEffect.define<DocData>()
const docData = StateField.define<DocData>({
  create: () => ({ threads: {}, workers: {} }),
  update: (v, tr) => tr.effects.reduce((acc, e) => (e.is(setDocData) ? e.value : acc), v),
})

// ---- the ask draft -----------------------------------------------------------------

const openDraft = StateEffect.define<{ from: number; to: number }>()

/** The open ask box: the range its markers will wrap, and, for table rows, the
 *  quote to send in place of the wrapped text (the picked rows, not the table). */
type Draft = { from: number; to: number; quote?: string }

const draft = StateField.define<Draft | null>({
  create: () => null,
  update(v, tr) {
    let next = v && tr.docChanged ? { ...v, from: tr.changes.mapPos(v.from, 1), to: tr.changes.mapPos(v.to, -1) } : v
    // Clicking or moving back into the text puts the box away.
    if (tr.selection && next) next = null
    for (const e of tr.effects) {
      if (e.is(openDraft)) next = e.value
      else if (e.is(requestRowAsk)) next = { from: e.value.from, to: e.value.to, quote: e.value.quote }
      else if (e.is(closeDraft)) next = null
    }
    return next
  },
})

export const newAskId = () => `a${Date.now().toString(36).slice(-4)}${Math.random().toString(36).slice(2, 5)}`

/** Wrap [from, to] in ask markers and return the quoted text, or null for blank text. */
export function wrapAsk(view: EditorView, rawFrom: number, rawTo: number, id: string): string | null {
  const doc = view.state.doc
  // Only the text itself: blank lines around a selection are not part of it.
  const range = trimRange(doc.toString(), rawFrom, rawTo)
  if (!range) return null
  const { from, to } = range
  const first = doc.lineAt(from)
  const last = doc.lineAt(to)
  let blockish = first.number !== last.number
  for (let n = first.number; n <= last.number && !blockish; n++) if (isRow(doc.line(n).text)) blockish = true
  const [o, c] = askMarkers(id)
  if (blockish) {
    // Own lines, around whole lines: a marker inside a table row would end it.
    let a = first.number
    let b = last.number
    while (a > 1 && isRow(doc.line(a).text) && isRow(doc.line(a - 1).text)) a--
    while (b < doc.lines && isRow(doc.line(b).text) && isRow(doc.line(b + 1).text)) b++
    const start = doc.line(a).from
    const end = doc.line(b).to
    const quote = doc.sliceString(start, end)
    view.dispatch({ changes: [{ from: start, insert: `${o}\n` }, { from: end, insert: `\n${c}` }] })
    return quote
  }
  const quote = doc.sliceString(from, to)
  view.dispatch({ changes: [{ from, insert: o }, { from: to, insert: c }] })
  return quote
}

// ---- widgets -----------------------------------------------------------------

class HiddenLine extends WidgetType {
  eq() { return true }
  toDOM() { const d = document.createElement('div'); d.className = 'cm-marker-hidden'; return d }
}

const roots = new WeakMap<HTMLElement, Root>()

/** How far above its widget a goal pin sits, by heading level, so it is level with
 *  the heading's taller line: half the line (font size x 1.7) plus half the pin.
 *  Index 0 is any level with body-size text. */
const GOAL_PIN_LIFT = [27, 38, 33, 30, 27, 27, 27]

/** How far left a goal's pin sits of where other pins do: clear of the section's rail. */
const GOAL_PIN_INSET = 8

/** The answer under a highlight: a React island inside a block widget. */
class AnswerWidget extends WidgetType {
  constructor(
    readonly id: string, readonly thread: ThreadView | undefined, readonly host: { current: ConductorHost },
    readonly active = false, readonly focus = 0, readonly stack = 0, readonly onTable = false,
    /** A heading goal's pin: its heading level (the pin sits level with a taller line)
     *  and the goal id whose section a reply quotes. */
    readonly goal: { level: number; goalId: string } | null = null,
  ) { super() }
  eq(o: AnswerWidget) {
    return o.id === this.id && o.active === this.active && o.focus === this.focus && o.stack === this.stack && o.onTable === this.onTable
      && o.goal?.level === this.goal?.level && JSON.stringify(o.thread) === JSON.stringify(this.thread)
  }
  toDOM(view: EditorView) {
    const dom = document.createElement('div')
    dom.className = 'cm-answer'
    roots.set(dom, createRoot(dom))
    this.render(dom, view)
    return dom
  }
  // Same answer, newer thread: re-render in place, so an open section and a
  // half-written reply survive a new message arriving.
  updateDOM(dom: HTMLElement, view: EditorView) {
    if (!roots.has(dom)) return false
    this.render(dom, view)
    return true
  }
  render(dom: HTMLElement, view: EditorView) {
    const { id, host } = this
    roots.get(dom)?.render(createElement(DocAnswer, {
      thread: this.thread,
      gutter: true,
      stack: this.stack,
      onTable: this.onTable,
      active: this.active,
      focusSignal: this.focus,
      onActivate: () => host.current.activate?.(id),
      onOpen: () => host.current.markRead(id),
      onOpenSession: (key: string) => host.current.openSession(key),
      ...(this.goal ? { lift: GOAL_PIN_LIFT[this.goal.level] ?? GOAL_PIN_LIFT[0], inset: GOAL_PIN_INSET, emptyLabel: 'Not checked yet' } : {}),
      inline: !host.current.threadsInSidePanel,
      onSend: (text: string) => host.current.send(id, text, threadQuote(view.state, id), view.state.doc.toString(), true),
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

// ---- decorations ---------------------------------------------------------------

type AskTint = 'pending' | 'unread' | 'read'

function tint(t: ThreadView | undefined): AskTint {
  if (!t || t.pending) return 'pending'
  return t.unread ? 'unread' : 'read'
}

function build(state: EditorState, host: { current: ConductorHost }): { decos: DecorationSet; atomic: DecorationSet } {
  const text = state.doc.toString()
  const data = state.field(docData)
  const decos: Range<Decoration>[] = []
  const atomic: Range<Decoration>[] = []
  const hide = Decoration.replace({})
  const markers = parseMarkers(text)
  // Pins sit level with the visible line above their answer; several on one line
  // (two asks about one table, say) line up side by side instead of overlapping.
  const hidden = new Set<number>()
  for (const m of markers) {
    if (m.ownLine) hidden.add(state.doc.lineAt(m.open.from).number).add(state.doc.lineAt(m.close.from).number)
  }
  const onLine = new Map<number, number>()
  const pinFor = (m: Marker) => {
    let n = state.doc.lineAt(m.close.from).number
    if (m.ownLine) { n--; while (n > 1 && hidden.has(n)) n-- }
    const i = onLine.get(n) ?? 0
    onLine.set(n, i + 1)
    // Under a table the widget starts below the grid's bottom padding: raise the
    // pin to its last row.
    return { stack: i, onTable: isRow(state.doc.line(n).text) }
  }
  for (const m of markers) {
    const openLine = state.doc.lineAt(m.open.from)
    const closeLine = state.doc.lineAt(m.close.from)
    if (m.kind === 'work') {
      const cls = `cm-work-line cm-work-${data.workers[m.id]?.state ?? 'missing'}`
      if (m.ownLine) {
        decos.push(Decoration.replace({ widget: new HiddenLine(), block: true }).range(openLine.from, openLine.to))
        decos.push(Decoration.replace({ widget: new HiddenLine(), block: true }).range(closeLine.from, closeLine.to))
        atomic.push(hide.range(openLine.from, openLine.to), hide.range(closeLine.from, closeLine.to))
        for (let n = openLine.number + 1; n < closeLine.number; n++) decos.push(Decoration.line({ class: cls }).range(state.doc.line(n).from))
      } else {
        decos.push(hide.range(m.open.from, m.open.to), hide.range(m.close.from, m.close.to))
        atomic.push(hide.range(m.open.from, m.open.to), hide.range(m.close.from, m.close.to))
      }
      continue
    }
    // An ask: its own text tinted, and its answer under its last line.
    const thread = data.threads[m.id]
    const isActive = data.active?.id === m.id
    const pin = pinFor(m)
    const answer = new AnswerWidget(m.id, thread, host, isActive, isActive ? data.active!.focus : 0, pin.stack, pin.onTable)
    const content = trimRange(text, m.open.to, m.close.from)
    if (m.ownLine) {
      decos.push(Decoration.replace({ widget: new HiddenLine(), block: true }).range(openLine.from, openLine.to))
      // The closing marker's line becomes the answer: nothing to type into.
      decos.push(Decoration.replace({ widget: answer, block: true }).range(closeLine.from, closeLine.to))
      atomic.push(hide.range(openLine.from, openLine.to), hide.range(closeLine.from, closeLine.to))
    } else {
      decos.push(hide.range(m.open.from, m.open.to), hide.range(m.close.from, m.close.to))
      atomic.push(hide.range(m.open.from, m.open.to), hide.range(m.close.from, m.close.to))
      decos.push(Decoration.widget({ widget: answer, block: true, side: 1 }).range(state.doc.lineAt(m.close.to).to))
    }
    if (content) decos.push(Decoration.mark({ class: `cm-ask cm-ask-${tint(thread)}${isActive ? ' cm-ask-active' : ''}` }).range(content.from, content.to))
  }
  // A heading with a goal carries that goal's pin: its checks' reports and the
  // person's replies, in the goal's own thread. Drawn by the page, so it is there
  // whether or not the conductor ever draws anything.
  for (const s of headingSections(state)) {
    if (!s.goal) continue
    const goalId = goalIdOf(s)
    const id = goalThread(goalId)
    const isActive = data.active?.id === id
    const i = onLine.get(s.line) ?? 0
    onLine.set(s.line, i + 1)
    // A goal no check has reported on yet still has a pin: quiet, or working while one runs.
    const thread = data.threads[id] ?? { messages: [], pending: !!data.goalsPending, unread: false }
    const widget = new AnswerWidget(id, thread, host, isActive, isActive ? data.active!.focus : 0, i, false, { level: s.level, goalId })
    decos.push(Decoration.widget({ widget, block: true, side: 1 }).range(state.doc.line(s.line).to))
  }
  return { decos: Decoration.set(decos, true), atomic: Decoration.set(atomic, true) }
}

// ---- tooltips: the ask button and the ask box -------------------------------------

function askBox(onSend: (text: string) => void, onCancel: () => void): HTMLTextAreaElement {
  const box = document.createElement('textarea')
  box.rows = 2
  box.placeholder = 'Ask the conductor about this…'
  box.className = 'cm-ask-input'
  box.setAttribute('data-testid', 'ask-input')
  box.onkeydown = e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault()
      const text = box.value.trim()
      if (text) onSend(text)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      onCancel()
    }
  }
  return box
}

/** Where a row ask's box goes: just under the last picked row of the table at *from*. */
function rowAnchor(view: EditorView, from: number, rows: number[]): { left: number; right: number; top: number; bottom: number } {
  for (const el of view.dom.querySelectorAll<HTMLElement>('.cm-table-widget')) {
    let at: number
    try { at = view.posAtDOM(el) } catch { continue }
    if (at !== from) continue
    const table = el.getBoundingClientRect()
    const row = rows.length ? el.querySelector<HTMLElement>(`tr[data-r="${Math.max(...rows)}"]`) : null
    const bottom = (row ?? el).getBoundingClientRect().bottom
    return { left: table.left, right: table.left + 1, top: bottom, bottom }
  }
  const c = view.coordsAtPos(from)
  return c ?? { left: 0, right: 0, top: 0, bottom: 0 }
}

function tooltipFor(state: EditorState, host: { current: ConductorHost }): Tooltip | null {
  const d = state.field(draft)
  if (d) {
    const { from, to, quote: rowQuote } = d
    return {
      pos: to, above: false, arrow: false,
      create: view => {
        const dom = document.createElement('div')
        dom.className = 'cm-ask-pop'
        const box = askBox(text => {
          const id = newAskId()
          const quote = wrapAsk(view, from, to, id)
          // Sent: the text is no longer selected. The caret goes just past the
          // highlight (past its hidden closing marker, so typing there stays out of
          // it) and back into the doc, ready to keep writing.
          const m = parseMarkers(view.state.doc.toString()).find(k => k.kind === 'ask' && k.id === id)
          const after = m ? Math.min(view.state.doc.length, m.ownLine ? m.close.to + 1 : m.close.to) : null
          // Closing the box first: moving the selection in the same transaction
          // focuses the editor mid-update, and the box's blur handler would then
          // dispatch from inside it.
          view.dispatch({ effects: closeDraft.of(null) })
          if (after !== null) view.dispatch({ selection: { anchor: after } })
          view.focus()
          if (quote !== null) host.current.send(id, text, rowQuote ?? quote, view.state.doc.toString(), false)
        }, () => { view.dispatch({ effects: closeDraft.of(null) }); view.focus() })
        const hint = document.createElement('div')
        hint.className = 'cm-ask-hint'
        hint.textContent = 'Enter to send · Esc to cancel'
        dom.append(box, hint)
        if (!rowQuote) return { dom, mount: () => box.focus() }
        // A row ask's range ends inside the table widget, where the editor has no
        // text coordinates: placed by `pos`, the box would be moved off-screen.
        // It sits under the last picked row instead, read from the grid itself.
        const rows = state.field(askRows, false)?.rows ?? []
        return { dom, mount: () => box.focus(), getCoords: () => rowAnchor(view, from, rows) }
      },
    }
  }
  const sel = state.selection.main
  // Blank space is nothing to ask about.
  if (sel.empty || !trimRange(state.doc.toString(), sel.from, sel.to)) return null
  return {
    pos: sel.from, above: true, arrow: false,
    create: view => {
      const dom = document.createElement('div')
      dom.className = 'cm-ask-start'
      const button = document.createElement('button')
      button.type = 'button'
      button.textContent = '✦ Ask conductor'
      button.setAttribute('data-testid', 'ask-conductor')
      button.onmousedown = e => e.preventDefault()
      button.onclick = () => {
        const { from, to } = view.state.selection.main
        if (from !== to) view.dispatch({ effects: openDraft.of({ from, to }) })
      }
      dom.append(button)
      return { dom }
    },
  }
}

// ---- theme -----------------------------------------------------------------------

/**
 * The answer section's look (`DocAnswer`, by its `data-da` parts). Exported so the
 * whiteboard, which hosts the same component outside an editor, wears the same.
 */
export const ANSWER_STYLE: StyleSpec = {
  '[data-da="bar"]': {
    display: 'flex', alignItems: 'center', gap: '8px', width: '100%', minWidth: '0', padding: '3px 8px',
    border: 'none', borderLeft: '2px solid color-mix(in srgb, var(--accent) 45%, transparent)', borderRadius: '0 6px 6px 0',
    background: 'transparent', color: 'var(--muted)', font: 'inherit', fontSize: '12.5px', lineHeight: '20px',
    textAlign: 'left', cursor: 'pointer',
  },
  '[data-da="bar"]:hover': { background: 'var(--bg-hover)', color: 'var(--text)' },
  // A doc's answer, collapsed, is only its pin: the root takes no height, and the
  // pin sits in the left margin, centred on the line above the widget.
  '[data-da="root"][data-gutter="true"]': { position: 'relative' },
  '[data-da="pin"]': {
    position: 'absolute', padding: '0', border: 'none', background: 'transparent', cursor: 'pointer', lineHeight: '0',
  },
  '[data-da="pin-flip"]': { display: 'block', transform: 'scaleX(-1)' },
  '[data-da="preview"]': {
    position: 'absolute', left: '0', zIndex: '5', width: '280px', pointerEvents: 'none',
    padding: '8px 12px', borderRadius: '14px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
    color: 'var(--text)', boxShadow: '0 8px 24px -6px rgba(0,0,0,.25)', lineHeight: '1.45',
  },
  '[data-da="preview-text"]': {
    fontSize: '13px', display: '-webkit-box', WebkitLineClamp: '2', WebkitBoxOrient: 'vertical', overflow: 'hidden',
  },
  '[data-da="preview-meta"]': { display: 'flex', gap: '8px', marginTop: '2px', fontSize: '11px', color: 'var(--muted)' },
  '[data-da="root"][data-open="true"] [data-da="bar"]': { background: 'var(--bg-elevated)', color: 'var(--text)' },
  '[data-da="who"]': { color: 'var(--accent)', fontWeight: '600', flexShrink: '0' },
  '[data-da="summary"]': { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: '1', minWidth: '0' },
  '[data-da="chip"]': {
    flexShrink: '0', padding: '0 7px', borderRadius: '999px', border: '1px solid var(--border)', fontSize: '11px',
  },
  '[data-da="caret"]': { flexShrink: '0', opacity: '0.7' },
  '[data-da="dot"]': { width: '7px', height: '7px', borderRadius: '50%', background: 'var(--border-strong)', flexShrink: '0' },
  '[data-da="dot"][data-state="unread"]': { background: 'var(--accent)' },
  '[data-da="dot"][data-state="pending"], [data-da="dot"][data-state="working"]': {
    background: 'var(--accent)', animation: 'cm-ask-pulse 1.2s ease-in-out infinite',
  },
  '[data-da="dot"][data-state="needs_you"]': { background: '#f59e32' },
  '[data-da="body"]': {
    padding: '10px 12px 12px', borderLeft: '2px solid color-mix(in srgb, var(--accent) 45%, transparent)',
    background: 'var(--bg-elevated)', borderRadius: '0 0 8px 0', color: 'var(--text)', fontSize: '13.5px',
  },
  '[data-da="msg"]': { marginBottom: '10px' },
  '[data-da="msg-who"]': { fontSize: '11px', color: 'var(--muted)', marginBottom: '2px' },
  '[data-da="msg"][data-who="conductor"] [data-da="msg-who"]': { color: 'var(--accent)' },
  '[data-da="text"]': { whiteSpace: 'pre-wrap' },
  '[data-da="md"] p': { margin: '0 0 6px' },
  '[data-da="md"] ul, [data-da="md"] ol': { margin: '0 0 6px', paddingLeft: '20px' },
  '[data-da="md"] code': { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: '0.92em', color: 'var(--accent)' },
  '[data-da="md"] pre': { overflowX: 'auto', padding: '8px', borderRadius: '6px', background: 'var(--bg)' },
  '[data-da="md"] table': { borderCollapse: 'collapse', margin: '4px 0 8px' },
  '[data-da="md"] th, [data-da="md"] td': { border: '1px solid var(--border)', padding: '3px 8px', textAlign: 'left' },
  '[data-da="md"] th': { background: 'var(--bg-hover)' },
  '[data-da="md"] a': { color: 'var(--accent)' },
  '[data-da="waiting"]': { color: 'var(--muted)', fontStyle: 'italic', marginBottom: '8px' },
  '[data-da="workers"]': { display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '10px' },
  '[data-da="worker"]': {
    display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '2px 10px 2px 8px', borderRadius: '999px',
    border: '1px solid var(--border-strong)', background: 'var(--bg)', color: 'var(--text)', font: 'inherit',
    fontSize: '12px', cursor: 'pointer',
  },
  '[data-da="worker"]:hover': { background: 'var(--bg-hover)' },
  '[data-da="worker-state"]': { color: 'var(--muted)' },
  '[data-da="input"], .cm-ask-input': {
    width: '100%', boxSizing: 'border-box', resize: 'none', borderRadius: '6px', padding: '6px 8px',
    border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)', font: 'inherit', outline: 'none',
  },
  '[data-da="input"]:focus, .cm-ask-input:focus': { borderColor: 'var(--accent)' },
  // The compact pin a board uses: a pill, with the thread as a floating card.
  '[data-da="root"][data-compact="true"]': { position: 'relative', display: 'inline-block' },
  '[data-da="root"][data-compact="true"] [data-da="bar"]': {
    width: 'auto', gap: '5px', padding: '1px 9px 1px 7px', border: '1px solid var(--border-strong)', borderRadius: '999px',
    background: 'var(--bg-elevated)', color: 'var(--text)', fontSize: '12px', boxShadow: '0 2px 10px rgba(0,0,0,.25)',
  },
  '[data-da="root"][data-compact="true"] [data-da="summary"]': { flex: 'none' },
  '[data-da="root"][data-compact="true"] [data-da="body"]': {
    position: 'absolute', top: 'calc(100% + 6px)', left: '0', width: '340px', maxHeight: '420px', overflowY: 'auto',
    border: '1px solid var(--border-strong)', borderRadius: '10px', boxShadow: '0 12px 32px rgba(0,0,0,.3)', zIndex: '20',
  },
}

const conductorTheme = EditorView.baseTheme({
  '.cm-marker-hidden': { height: '0' },
  // An ask tints its own text only.
  // A sent highlight is not a selection: it is only underlined, and only while
  // there is something to see (thinking, an unread reply). The tint is reserved
  // for the page's current conversation.
  '.cm-ask': { borderRadius: '3px', textUnderlineOffset: '4px', textDecorationThickness: '1.5px' },
  '.cm-ask-pending': { textDecorationLine: 'underline', textDecorationStyle: 'dashed', textDecorationColor: 'color-mix(in srgb, var(--accent) 70%, transparent)' },
  '.cm-ask-unread': { textDecorationLine: 'underline', textDecorationColor: 'var(--accent)' },
  '.cm-ask-active': { backgroundColor: 'color-mix(in srgb, var(--accent) 14%, transparent)' },
  '@keyframes cm-ask-pulse': { '0%, 100%': { opacity: 1 }, '50%': { opacity: 0.35 } },
  '.cm-work-line': { borderLeft: '3px solid var(--accent)', paddingLeft: '12px !important' },
  '.cm-work-line.cm-work-working': { backgroundColor: 'color-mix(in srgb, var(--accent) 7%, transparent)' },
  '.cm-work-line.cm-work-needs_you': { borderLeftColor: '#f59e32' },
  '.cm-work-line.cm-work-idle, .cm-work-line.cm-work-missing': { borderLeftColor: 'var(--border-strong)' },

  // The answer under a highlight (`DocAnswer`, styled by its `data-da` parts).
  // Collapsed, an answer is only its margin pin and takes no height at all.
  '.cm-answer': { cursor: 'default' },
  '.cm-answer [data-da="root"][data-open="true"]': { padding: '2px 0 6px' },
  ...ANSWER_STYLE,
  '@media (prefers-reduced-motion: reduce)': {
    '[data-da="dot"]': { animation: 'none !important' },
  },

  // The ask button and box over a selection.
  '.cm-tooltip.cm-ask-start, .cm-tooltip.cm-ask-pop': { border: 'none', background: 'transparent' },
  '.cm-ask-start button': {
    padding: '3px 10px', borderRadius: '6px', border: '1px solid var(--border-strong)',
    background: 'var(--bg-elevated)', color: 'var(--text)', fontSize: '12px', cursor: 'pointer',
    boxShadow: '0 4px 14px rgba(0,0,0,.25)', transform: 'translateY(-4px)',
  },
  '.cm-ask-start button:hover': { background: 'var(--bg-hover)' },
  '.cm-ask-pop': {
    width: '340px', maxWidth: '80vw', padding: '10px', borderRadius: '10px', marginTop: '6px',
    border: '1px solid var(--border-strong) !important', background: 'var(--bg-elevated) !important',
    color: 'var(--text)', boxShadow: '0 10px 30px rgba(0,0,0,.3)', fontSize: '13px',
  },
  '.cm-ask-hint': { marginTop: '4px', fontSize: '11px', color: 'var(--muted)' },
})

export function conductorExtension(host: { current: ConductorHost }): Extension {
  const marks = StateField.define<{ decos: DecorationSet; atomic: DecorationSet }>({
    create: s => build(s, host),
    update: (v, tr) => (tr.docChanged || tr.effects.some(e => e.is(setDocData)) ? build(tr.state, host) : v),
    provide: f => [
      EditorView.decorations.from(f, v => v.decos),
      EditorView.atomicRanges.of(view => view.state.field(f).atomic),
    ],
  })
  const tip = StateField.define<Tooltip | null>({
    create: s => tooltipFor(s, host),
    update: (v, tr) => (tr.docChanged || tr.selection || tr.effects.some(e => e.is(openDraft) || e.is(closeDraft) || e.is(requestRowAsk))
      ? tooltipFor(tr.state, host)
      : v),
    provide: f => showTooltip.from(f),
  })
  return [docData, draft, askRows, marks, tip, conductorTheme]
}

// ---- writes: markdown a conductor reply asks the pane to add to the doc ----------------

/** The info string of a doc's HTML block: a fence the editor draws as live HTML. */
export const HTML_FENCE = 'html-embed'
/** *html* as the doc's HTML block, in markdown. */
export const htmlBlock = (html: string) => `\`\`\`${HTML_FENCE}\n${html.replace(/\s+$/, '')}\n\`\`\``

/** One write: add *text* at the end of the doc, or after the line holding *after*. */
export interface DocWrite { mode: 'append' | 'after'; after?: string; text: string }
export interface DocActions { writes: DocWrite[] }

// A fenced block, closed by a fence on a line of its own.
const WRITE_RE = /```(doc-append|doc-insert|doc-html)([^\n]*)\n([\s\S]*?)\n```[ \t]*(?=\n|$)/g
// A block still streaming in (or a retired one): hidden, never applied.
const OPEN_RE = /```(?:doc-(?:append|insert|html|actions)|board-(?:add|edit)|codebase-(?:root|focus)|page-(?:html|replace))[\s\S]*$/
// A whiteboard's additions and edits, a codebase model's root and focus, and a
// dashboard's page: applied by their panes, never shown as prose.
const BOARD_ADD_RE = /```(?:board-(?:add|edit)|codebase-(?:root|focus)|page-(?:html|replace))[^\n]*\n[\s\S]*?\n```[ \t]*(?=\n|$)/g

/** Split a conductor reply into the prose the person reads and the writes it asks for. */
export function splitReply(text: string): { prose: string; actions: DocActions | null } {
  const writes: DocWrite[] = []
  let prose = text.replace(WRITE_RE, (_all, kind: string, info: string, body: string) => {
    if (kind === 'doc-append') writes.push({ mode: 'append', text: body })
    else if (kind === 'doc-html') {
      // Raw HTML from the conductor lands as the doc's own HTML block, at the end
      // or after a line it names.
      const after = /after="([^"]*)"/.exec(info)?.[1]
      const block = htmlBlock(body)
      writes.push(after ? { mode: 'after', after, text: block } : { mode: 'append', text: block })
    } else {
      const after = /after="([^"]*)"/.exec(info)?.[1]
      if (after) writes.push({ mode: 'after', after, text: body })
    }
    return ''
  })
  prose = prose.replace(BOARD_ADD_RE, '').replace(/```doc-actions\s*\n[\s\S]*?```/g, '').replace(OPEN_RE, '').trim()
  return { prose, actions: writes.length ? { writes } : null }
}

/**
 * The change one write makes to *text*, or null when it does not apply: its text
 * is already in the doc (so re-applying, or a second browser, is a no-op), or the
 * line it goes after is gone. It only ever adds; nothing the person wrote moves.
 */
export function changeForWrite(text: string, write: DocWrite, findLine: (text: string, anchor: string) => number | null): { from: number; insert: string } | null {
  const body = write.text.replace(/\s+$/, '')
  if (!body.trim() || text.includes(body.trim())) return null
  if (write.mode === 'append') {
    const lead = text === '' ? '' : text.endsWith('\n\n') ? '' : text.endsWith('\n') ? '\n' : '\n\n'
    return { from: text.length, insert: `${lead}${body}\n` }
  }
  const at = findLine(text, write.after ?? '')
  if (at === null) return null
  const endAt = text.indexOf('\n', at)
  const lineEnd = endAt === -1 ? text.length : endAt
  return { from: lineEnd, insert: `\n\n${body}` }
}

/**
 * Goals on a doc's headings (PoC), after the board's section goals: a heading
 * stands for its section, everything down to the next heading of the same or a
 * higher level (so an `##` section holds its `###` subsections), and a goal set
 * on it covers whatever is inside that section when it is checked.
 *
 * In the file a goal is a hidden marker on the line right after its heading, so it
 * travels with the heading and the doc stays plain markdown:
 *
 *     ## Open tickets
 *     <!-- goal: Keep each ticket's status current -->
 *
 * Every goal on the page is checked at one rate, kept in the doc's front matter
 * (`goal-every`), by one loop on the conductor (`goalLoop`).
 *
 * This module reads and writes those markers and draws them: the marker line is
 * hidden, the heading carries a "Watching" chip (or, on hover, "Set goal"), and a
 * watched section gets a moving dashed rail down its left margin.
 */
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'
import { EditorState, StateEffect, StateField, type Extension, type Range } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin, WidgetType, type DecorationSet, type ViewUpdate } from '@codemirror/view'
import { DEFAULT_GOAL_INTERVAL, type SectionGoal } from './board/boardModel'
import DocGoalChip from './DocGoalChip'
import DocGoalRails, { type Rail } from './DocGoalRails'

export type DocGoal = Pick<SectionGoal, 'text'> & {
  /** What the conductor tags its report on this goal with. A marker written before
   *  goals had ids has none; its heading's text stands in until it is saved again. */
  id?: string
}

export interface HeadingSection {
  /** The heading's line number, its level and its text. */
  line: number
  level: number
  title: string
  /** The goal marker's line number, when it has one. */
  markerLine: number | null
  goal: DocGoal | null
  /** The section's last line: just before the next heading of this level or higher. */
  endLine: number
}

const HEADING = /^(#{1,6})\s+(\S.*)$/
const FENCE = /^```/
// `<!-- goal <id>: text -->`. An older marker had no id, or carried its own
// `every=N`; both still read, and the next save writes the current shape.
const MARKER = /^<!-- goal(?: (g[a-z0-9]{3,16}))?(?: every=\d+)?: (.*) -->$/
const ANY_MARKER = /^<!-- goal(?: g[a-z0-9]{3,16})?(?: every=\d+)?: \S.* -->$/m

/** The goal a marker line holds, or null when the line is no goal marker. */
export function parseGoalMarker(text: string): DocGoal | null {
  const m = MARKER.exec(text.trim())
  if (!m || !m[2].trim()) return null
  return m[1] ? { id: m[1], text: m[2].trim() } : { text: m[2].trim() }
}

export const newGoalId = () => `g${Math.random().toString(36).slice(2, 8)}`

const MARKER_ID = /^(<!-- goal )(g[a-z0-9]{3,16})(?=(?: every=\d+)?: )/

/**
 * A goal is known by the id in its marker, and the id names its pin and thread. A
 * copy of a section brings its marker along, which would leave two headings sharing
 * one goal; the copy (the marker lines this change inserted) takes a new id. A cut
 * and paste, or a drag, leaves the id alone: the original is gone by then.
 */
export const freshGoalIds = EditorState.transactionFilter.of(tr => {
  if (!tr.docChanged) return tr
  const doc = tr.newDoc
  const inserted: { from: number; to: number }[] = []
  tr.changes.iterChangedRanges((_fa, _ta, from, to) => { if (to > from) inserted.push({ from, to }) })
  if (!inserted.some(r => doc.sliceString(r.from, r.to).includes('<!-- goal g'))) return tr
  const marks: { at: number; id: string; added: boolean }[] = []
  for (let n = 1; n <= doc.lines; n++) {
    const line = doc.line(n)
    const m = MARKER_ID.exec(line.text)
    if (m) marks.push({ at: line.from + m[1].length, id: m[2], added: inserted.some(r => line.from < r.to && line.to >= r.from) })
  }
  const taken = new Set(marks.filter(m => !m.added).map(m => m.id))
  const changes: { from: number; to: number; insert: string }[] = []
  for (const m of marks.filter(x => x.added)) {
    if (taken.has(m.id)) {
      let id = newGoalId()
      while (taken.has(id)) id = newGoalId()
      taken.add(id)
      changes.push({ from: m.at, to: m.at + m.id.length, insert: id })
    } else taken.add(m.id)
  }
  return changes.length ? [tr, { changes, sequential: true }] : tr
})

/** *goal* as its marker line: one line, with nothing in it that would end the comment. */
export function goalMarker(goal: DocGoal): string {
  const text = goal.text.replace(/\s+/g, ' ').replace(/--+>?/g, '–').trim()
  return `<!-- goal ${goal.id ?? newGoalId()}: ${text} -->`
}

/** Whether a doc's text holds any heading goal: what the page's loop follows. */
export const docHasGoals = (text: string) => ANY_MARKER.test(text)

/** The id a heading's goal is known by: its own, or for an older marker its heading's text. */
export const goalIdOf = (s: Pick<HeadingSection, 'goal' | 'title'>) =>
  s.goal?.id ?? `h-${s.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40)}`

/** The thread one goal's checks and replies live in: an id a doc ask turn can name
 *  too, so a reply on the goal's pin continues the same conversation. Boards key it
 *  by the section's id. */
export const goalThread = (goalId: string) => `goal-${goalId}`

/** One finished check of a goal: when its report came, and whether the report said
 *  the conductor found or did something (a quiet check begins "Nothing new"). */
export interface GoalCheck { ts?: number; acted: boolean }

/** The checks the chip shows. */
export const GOAL_BEAT_COUNT = 8

/** Whether a goal report says there was nothing to do: the instruction asks for it to begin so. */
export const isQuietReport = (text: string) => /^\W*nothing new\b/i.test(text)

/** The page-wide goal rate, pushed in by the pane. */
export const setGoalState = StateEffect.define<{ every: number }>()
const goalState = StateField.define<{ every: number }>({
  create: () => ({ every: DEFAULT_GOAL_INTERVAL }),
  update: (v, tr) => tr.effects.reduce((acc, e) => (e.is(setGoalState) ? e.value : acc), v),
})

/** Each goal's recent checks by goal id, pushed in by the pane from the transcript. */
export const setGoalChecks = StateEffect.define<Record<string, GoalCheck[]>>()
const goalChecks = StateField.define<Record<string, GoalCheck[]>>({
  create: () => ({}),
  update: (v, tr) => tr.effects.reduce((acc, e) => (e.is(setGoalChecks) ? e.value : acc), v),
})

/** What the goal controls call out to: the page owns the rate, in its front matter. */
export interface GoalHost { setEvery: (minutes: number) => void }

/** Every heading of the doc with its section and goal, in order. Headings inside a
 *  code block (an HTML block's source included) are not headings. */
export function headingSections(state: EditorState): HeadingSection[] {
  const doc = state.doc
  const heads: { line: number; level: number; title: string }[] = []
  let inFence = false
  for (let n = 1; n <= doc.lines; n++) {
    const text = doc.line(n).text
    if (FENCE.test(text)) { inFence = !inFence; continue }
    if (inFence) continue
    const h = HEADING.exec(text)
    if (h) heads.push({ line: n, level: h[1].length, title: h[2].trim() })
  }
  return heads.map((h, i) => {
    const next = heads.slice(i + 1).find(o => o.level <= h.level)
    let endLine = next ? next.line - 1 : doc.lines
    // A section ends at its last line with text: the blank lines before the next
    // heading are spacing, not content.
    while (endLine > h.line && !doc.line(endLine).text.trim()) endLine--
    const after = h.line < doc.lines ? doc.line(h.line + 1).text : ''
    const goal = parseGoalMarker(after)
    return { ...h, markerLine: goal ? h.line + 1 : null, goal, endLine }
  })
}

/** Set, change or clear (*goal* undefined) the goal of the heading on line *line*. */
export function setHeadingGoal(view: EditorView, line: number, goal: DocGoal | undefined) {
  const sec = headingSections(view.state).find(s => s.line === line)
  if (!sec) return
  const doc = view.state.doc
  if (sec.markerLine) {
    const m = doc.line(sec.markerLine)
    // Cleared: the marker goes with its line break; changed: it is rewritten in place,
    // keeping its id, so its conversation stays the same one.
    const kept = goal ? { ...goal, id: sec.goal?.id ?? goal.id } : undefined
    view.dispatch({ changes: kept ? { from: m.from, to: m.to, insert: goalMarker(kept) } : { from: doc.line(sec.line).to, to: m.to } })
  } else if (goal) {
    view.dispatch({ changes: { from: doc.line(sec.line).to, insert: `\n${goalMarker(goal)}` } })
  }
}

// ---- drawing ------------------------------------------------------------------------

class HiddenMarker extends WidgetType {
  eq() { return true }
  toDOM() { const d = document.createElement('div'); d.className = 'cm-goal-marker'; return d }
}

const roots = new WeakMap<HTMLElement, Root>()

/** At the end of a heading: its "Watching" chip, or the hover-only "Set goal". */
class ChipWidget extends WidgetType {
  constructor(
    readonly line: number, readonly goal: DocGoal | null, readonly title: string,
    readonly every: number, readonly host: { current: GoalHost }, readonly checks: GoalCheck[] = [],
  ) { super() }
  eq(o: ChipWidget) {
    return o.line === this.line && o.title === this.title && o.goal?.text === this.goal?.text && o.every === this.every
      && JSON.stringify(o.checks) === JSON.stringify(this.checks)
  }
  toDOM(view: EditorView) {
    const dom = document.createElement('span')
    dom.className = 'cm-goal-chip-host'
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
    const { line } = this
    const { host } = this
    flushSync(() => roots.get(dom)?.render(createElement(DocGoalChip, {
      goal: this.goal,
      title: this.title,
      every: this.every,
      checks: this.checks,
      onGoal: (g: DocGoal | undefined) => setHeadingGoal(view, line, g),
      onEvery: (m: number) => host.current.setEvery(m),
    })))
  }
  destroy(dom: HTMLElement) {
    const root = roots.get(dom)
    roots.delete(dom)
    if (root) queueMicrotask(() => root.unmount())
  }
  ignoreEvent() { return true }
}

function build(state: EditorState, host: { current: GoalHost }): { decos: DecorationSet; atomic: DecorationSet } {
  const { every } = state.field(goalState)
  const checks = state.field(goalChecks)
  const decos: Range<Decoration>[] = []
  const atomic: Range<Decoration>[] = []
  const hide = Decoration.replace({})
  for (const s of headingSections(state)) {
    const head = state.doc.line(s.line)
    decos.push(Decoration.line({ class: 'cm-goal-heading' }).range(head.from))
    decos.push(Decoration.widget({ widget: new ChipWidget(s.line, s.goal, s.title, every, host, s.goal ? checks[goalIdOf(s)] : []), side: 1 }).range(head.to))
    if (s.markerLine) {
      const m = state.doc.line(s.markerLine)
      decos.push(Decoration.replace({ widget: new HiddenMarker(), block: true }).range(m.from, m.to))
      atomic.push(hide.range(m.from, m.to))
    }
  }
  return { decos: Decoration.set(decos, true), atomic: Decoration.set(atomic, true) }
}

/** The rails beside watched sections: one layer in the scroller, placed from the
 *  editor's own line geometry in its measure phase. */
const rails = ViewPlugin.fromClass(class {
  layer: HTMLElement
  root: Root
  constructor(readonly view: EditorView) {
    this.layer = document.createElement('div')
    this.layer.className = 'cm-goal-rails'
    this.layer.setAttribute('aria-hidden', 'true')
    view.scrollDOM.appendChild(this.layer)
    this.root = createRoot(this.layer)
    this.schedule()
  }
  update(u: ViewUpdate) { if (u.docChanged || u.geometryChanged || u.viewportChanged) this.schedule() }
  schedule() {
    this.view.requestMeasure({
      key: this,
      read: view => {
        const left = view.contentDOM.offsetLeft
        return headingSections(view.state).filter(s => s.goal).map((s): Rail => {
          const top = view.lineBlockAt(view.state.doc.line(s.line).from).top
          const bottom = view.lineBlockAt(view.state.doc.line(s.endLine).from).bottom
          return { key: `${s.line}`, top, height: Math.max(0, bottom - top), left }
        })
      },
      write: list => this.root.render(createElement(DocGoalRails, { rails: list })),
    })
  }
  destroy() {
    const root = this.root
    queueMicrotask(() => root.unmount())
    this.layer.remove()
  }
})

const theme = EditorView.baseTheme({
  '.cm-goal-marker': { height: '0' },
  '.cm-goal-rails': { position: 'absolute', inset: '0', pointerEvents: 'none', zIndex: '1' },
  // "Set goal" shows on the heading under the pointer, or the one the caret is on.
  '[data-goal="add"]': { opacity: '0', transition: 'opacity .15s' },
  '.cm-goal-heading:hover [data-goal="add"], .cm-goal-heading:focus-within [data-goal="add"], [data-goal="add"][aria-expanded="true"]': { opacity: '1' },
  // The chip keeps its own small size whatever the heading's level.
  '[data-gc="root"]': { position: 'relative', display: 'inline-block', marginLeft: '10px', verticalAlign: 'middle', lineHeight: '1' },
  '[data-gc="chip"], [data-gc="add"]': {
    display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '3px 9px', borderRadius: '999px', border: 'none',
    font: '500 12px/16px system-ui, -apple-system, "Segoe UI", sans-serif', cursor: 'pointer',
  },
  '[data-gc="chip"]': { background: 'color-mix(in srgb, var(--accent) 14%, transparent)', color: 'var(--accent)' },
  '[data-gc="chip"]:hover': { background: 'color-mix(in srgb, var(--accent) 22%, transparent)' },
  '[data-gc="add"]': { background: 'transparent', color: 'var(--muted)' },
  '[data-gc="add"]:hover, [data-gc="add"][aria-expanded="true"]': { background: 'var(--bg-hover)', color: 'var(--text)' },
  '[data-gc="dot"]': { width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent)', flex: 'none' },
  '[data-gc="beat"]': { display: 'inline-flex', alignItems: 'flex-end', gap: '2px', height: '12px' },
  '[data-gc="beat"] > i': { display: 'block', width: '3px', borderRadius: '1px', background: 'color-mix(in srgb, var(--accent) 35%, transparent)' },
  '[data-gc="beat"] > i[data-acted="true"]': { background: 'var(--accent)' },
  '[data-gc="ago"]': { font: '400 11px/16px system-ui, -apple-system, "Segoe UI", sans-serif', color: 'var(--muted)' },
  '[data-gc="every"]': { opacity: '0.75' },
  '[data-gc="pop"]': {
    position: 'absolute', left: '0', top: 'calc(100% + 8px)', zIndex: '20', width: '330px', display: 'flex', flexDirection: 'column',
    gap: '8px', padding: '12px', borderRadius: '12px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
    color: 'var(--text)', boxShadow: '0 12px 32px -6px rgba(0,0,0,.22), 0 1px 3px rgba(0,0,0,.08)',
    font: '400 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif', textAlign: 'left', whiteSpace: 'normal', cursor: 'default',
  },
  '[data-gc="pop-title"]': { fontWeight: '600', color: 'var(--text-strong)' },
  '[data-gc="pop-help"]': { fontSize: '12px', color: 'var(--muted)' },
  '[data-gc="pop"] textarea': {
    resize: 'none', padding: '8px', borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg)',
    color: 'var(--text)', font: 'inherit', outline: 'none',
  },
  '[data-gc="pop"] textarea:focus': { borderColor: 'var(--accent)' },
  '[data-gc="every-row"]': { display: 'flex', alignItems: 'center', gap: '4px', fontSize: '12px' },
  '[data-gc="every-label"]': { color: 'var(--muted)', marginRight: '2px' },
  '[data-gc="every-opt"]': {
    padding: '3px 8px', borderRadius: '6px', border: '1px solid var(--border)', background: 'transparent', color: 'var(--text)',
    font: 'inherit', cursor: 'pointer',
  },
  '[data-gc="every-opt"][aria-checked="true"]': { background: 'var(--accent)', borderColor: 'var(--accent)', color: 'var(--accent-fg, #fff)' },
  '[data-gc="actions"]': { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '2px' },
})

const NO_HOST = { current: { setEvery: () => {} } as GoalHost }

export function goalExtension(host: { current: GoalHost } = NO_HOST): Extension {
  const field = StateField.define<{ decos: DecorationSet; atomic: DecorationSet }>({
    create: s => build(s, host),
    update: (v, tr) => (tr.docChanged || tr.effects.some(e => e.is(setGoalState) || e.is(setGoalChecks)) ? build(tr.state, host) : v),
    provide: f => [EditorView.decorations.from(f, v => v.decos), EditorView.atomicRanges.of(view => view.state.field(f).atomic)],
  })
  return [goalState, goalChecks, field, freshGoalIds, rails, theme]
}

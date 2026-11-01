/**
 * A dashboard session's object (PoC): one HTML page the conductor writes, plus the
 * asks pinned to its elements. The page is the OBJECT; the conductor's overlay
 * artifact, drawn on top of it, is the STATE.
 *
 * Elements are named by a target string: `el:<data-id>` for an element carrying a
 * `data-id` (the conductor is told to give every meaningful element one), or
 * `css:<selector>` for any other element the person picked.
 */
import type { ChatMessage } from '../../../../types'

export const DASHBOARD_ID_PREFIX = 'da5b0'
export const isDashboardId = (docId: string) => docId.startsWith(DASHBOARD_ID_PREFIX)

export interface PageAsk {
  id: string
  /** The elements asked about; empty for an ask about the whole page. */
  targets: string[]
  /** A few words naming what was asked about, for the chip and the pin. */
  label: string
}

export interface Dashboard {
  html: string
  asks: PageAsk[]
  /** Earlier pages, newest last: what undo goes back to. */
  past: string[]
  /** Pages undone, newest last: what redo brings back. */
  future: string[]
}

const HISTORY_CAP = 25

export const emptyDashboard = (): Dashboard => ({ html: '', asks: [], past: [], future: [] })

export function parseDashboard(text: string): Dashboard {
  try {
    const raw = JSON.parse(text) as Partial<Dashboard>
    const strings = (v: unknown) => (Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string') : [])
    return {
      html: typeof raw.html === 'string' ? raw.html : '',
      asks: Array.isArray(raw.asks)
        ? raw.asks.filter((a): a is PageAsk => !!a && typeof a.id === 'string').map(a => ({ id: a.id, targets: strings(a.targets), label: typeof a.label === 'string' ? a.label : '' }))
        : [],
      past: strings(raw.past).slice(-HISTORY_CAP),
      future: strings(raw.future).slice(-HISTORY_CAP),
    }
  } catch {
    return emptyDashboard()
  }
}

export const serializeDashboard = (d: Dashboard) => JSON.stringify(d)

/** *d* showing *html*, the page it replaces kept for undo. */
export function withPage(d: Dashboard, html: string): Dashboard {
  if (html === d.html) return d
  return { ...d, html, past: [...d.past, d.html].slice(-HISTORY_CAP), future: [] }
}

export function undoPage(d: Dashboard): Dashboard {
  if (!d.past.length) return d
  return { ...d, html: d.past[d.past.length - 1], past: d.past.slice(0, -1), future: [...d.future, d.html] }
}

export function redoPage(d: Dashboard): Dashboard {
  if (!d.future.length) return d
  return { ...d, html: d.future[d.future.length - 1], future: d.future.slice(0, -1), past: [...d.past, d.html] }
}

// ---- targets ------------------------------------------------------------------------

/** The element *target* names in *doc*, or null when it is not on the page. */
export function resolveTarget(doc: Document, target: string): Element | null {
  try {
    if (target.startsWith('el:')) return doc.querySelector(`[data-id="${CSS.escape(target.slice(3).trim())}"]`)
    if (target.startsWith('css:')) return doc.querySelector(target.slice(4))
  } catch { /* a malformed selector names nothing */ }
  return null
}

const parsePage = (html: string) => new DOMParser().parseFromString(html, 'text/html')
const printPage = (doc: Document) => `<!doctype html>\n${doc.documentElement.outerHTML}`

// ---- what a conductor reply does to the page ------------------------------------------

/**
 * One change: the whole page, or one element's replacement. A replacement whose
 * target is gone is skipped, so a reply written against an older page cannot
 * land on the wrong element.
 */
export type PageChange = { kind: 'page'; html: string } | { kind: 'replace'; target: string; html: string }

const CHANGE_RE = /```(page-html|page-replace)([^\n]*)\n([\s\S]*?)\n```[ \t]*(?=\n|$)/g

export function pageChangesIn(text: string): PageChange[] {
  const out: PageChange[] = []
  for (const m of text.matchAll(CHANGE_RE)) {
    if (m[1] === 'page-html') out.push({ kind: 'page', html: m[3] })
    else {
      const target = /target="([^"]+)"/.exec(m[2])?.[1]
      if (target) out.push({ kind: 'replace', target: target.includes(':') ? target : `el:${target}`, html: m[3] })
    }
  }
  return out
}

/** *html* after *changes*, in order. */
export function applyPageChanges(html: string, changes: PageChange[]): string {
  let out = html
  for (const c of changes) {
    if (c.kind === 'page') { out = c.html; continue }
    const doc = parsePage(out)
    const el = resolveTarget(doc, c.target)
    if (!el) continue
    el.outerHTML = c.html
    out = printPage(doc)
  }
  return out
}

/** Every finished reply's page changes, keyed by the reply they came from, in order. */
export function pageChangesFromMessages(messages: ChatMessage[]): { key: string; changes: PageChange[] }[] {
  const out: { key: string; changes: PageChange[] }[] = []
  messages.forEach((m, i) => {
    if (m.role !== 'assistant') return
    const changes = pageChangesIn(m.content ?? '')
    if (changes.length) out.push({ key: String((m.meta as { mid?: unknown } | undefined)?.mid ?? `${m.ts ?? ''}#${i}`), changes })
  })
  return out
}

/** The markup of the elements *targets* name, each cut to a size a turn can carry. */
export function describeTargets(html: string, targets: string[], max = 4000): string {
  if (!targets.length) return '(the whole page)'
  const doc = parsePage(html)
  return targets.map(t => {
    const el = resolveTarget(doc, t)
    if (!el) return `${t}: (no longer on the page)`
    const markup = el.outerHTML
    return `${t}:\n${markup.length > max ? `${markup.slice(0, max)}\n…(cut)` : markup}`
  }).join('\n\n')
}

/** A few words naming an element, for its chip and pin. */
export function labelFor(text: string, fallback: string): string {
  const t = text.replace(/\s+/g, ' ').trim()
  if (!t) return fallback
  return t.length > 40 ? `${t.slice(0, 37)}…` : t
}

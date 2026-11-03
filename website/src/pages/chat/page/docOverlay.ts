/**
 * The conductor's status overlay on a doc (PoC).
 *
 * The conductor decides how to show what is going on: it publishes one HTML
 * artifact (`doc-overlay-<docId>`) whose top-level elements are overlay items, and
 * the pane places them over the document:
 *
 * - `data-anchor="<exact text>"` pins an item to those words. The text is found
 *   again on every edit, so the item follows it as the person writes; an item whose
 *   words are gone, or that names none, sits in a tray at the top right.
 * - `data-place="margin"` (the default) draws it in a column right of the text,
 *   level with the anchored line; `data-place="over"` floats it just above the
 *   anchored words.
 * - `data-open-session="<key>"` on any element opens that session when clicked.
 *
 * The HTML is the conductor's to write, within the rules dashboard pages follow:
 * no scripts, style elements, event handlers, forms, frames or remote resources.
 * Nothing it draws is part of the document's text.
 */
import DOMPurify from 'dompurify'
import { StateEffect, StateField, type Extension } from '@codemirror/state'
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view'
import type { StyleSpec } from './styleSpec'

export interface OverlayItem {
  /** Stable within one overlay version: its position among the items. */
  key: string
  anchor: string | null
  /** Docs place margin / over; a board also places top / right / bottom / corner. */
  place: 'margin' | 'over' | 'top' | 'right' | 'bottom' | 'corner'
  /** Sanitized markup for the item, its own element included. */
  html: string
}

export const overlaySlug = (docId: string) => `doc-overlay-${docId}`

/** The overlay's items, sanitized. Anything that could run, reach out or submit is gone. */
export function parseOverlay(html: string): OverlayItem[] {
  // The dashboard pages' recipe: sanitize to a string, parse it, then a second,
  // independent pass over every attribute that could load or navigate.
  const sanitized = DOMPurify.sanitize(html, {
    FORBID_TAGS: ['style', 'script', 'link', 'meta', 'base', 'iframe', 'frame', 'object', 'embed', 'applet', 'template', 'noscript', 'form', 'input', 'button', 'textarea', 'select', 'animate', 'set', 'img'],
    ALLOW_DATA_ATTR: true,
  })
  const body = new DOMParser().parseFromString(`<body>${sanitized}</body>`, 'text/html').body
  for (const el of body.querySelectorAll('*')) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.localName
      if (name.startsWith('on') || name === 'src' || name === 'srcset' || name === 'href' || name === 'action' || name === 'formaction') {
        el.removeAttributeNode(attr)
      }
    }
  }
  return [...body.children].map((el, i) => {
    const anchor = el.getAttribute('data-anchor')?.trim() || null
    const raw = el.getAttribute('data-place')
    // Docs know margin and over; a board also knows top, right, bottom and corner.
    const place = raw === 'over' || raw === 'top' || raw === 'right' || raw === 'bottom' || raw === 'corner' ? raw : 'margin'
    return { key: String(i), anchor, place, html: el.outerHTML }
  })
}

/** Where *anchor* is in *text*: exact first, then forgiving case and spacing. */
export function findAnchor(text: string, anchor: string): number | null {
  const exact = text.indexOf(anchor)
  if (exact !== -1) return exact
  // Forgive case and runs of whitespace, which the conductor may not copy exactly.
  const lower = text.toLowerCase()
  const parts = anchor.toLowerCase().split(/\s+/).filter(Boolean)
  if (!parts.length) return null
  const at = lower.indexOf(parts[0])
  if (at === -1) return null
  const re = new RegExp(parts.map(p => p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+'))
  const m = re.exec(lower.slice(at))
  return m ? at + m.index : at
}

export const setOverlay = StateEffect.define<OverlayItem[]>()
const overlayItems = StateField.define<OverlayItem[]>({
  create: () => [],
  update: (v, tr) => tr.effects.reduce((acc, e) => (e.is(setOverlay) ? e.value : acc), v),
})

const MARGIN_GAP = 6

/**
 * The convention classes the conductor can use in its overlay. Exported so the
 * whiteboard's overlay, drawn outside an editor, wears exactly the same.
 */
export const OVERLAY_CLASS_STYLE: StyleSpec = {
  '[data-open-session]': { cursor: 'pointer' },
  '.ov-card': {
    padding: '8px 10px', borderRadius: '8px', border: '1px solid var(--border)', background: 'var(--bg-elevated)',
    boxShadow: '0 4px 14px rgba(0,0,0,.18)',
  },
  '.ov-pill': {
    display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '1px 9px', borderRadius: '999px',
    border: '1px solid var(--border-strong)', background: 'var(--bg-elevated)', whiteSpace: 'nowrap',
  },
  '.ov-ok': { color: 'var(--ok, var(--accent))' },
  '.ov-busy': { color: 'var(--accent)' },
  '.ov-warn': { color: 'var(--warn, #f59e32)' },
  '.ov-muted': { color: 'var(--muted)' },
  '.ov-progress': { height: '4px', borderRadius: '999px', background: 'var(--border)', overflow: 'hidden', margin: '4px 0' },
  // A bar that has not finished is work in progress: its fill moves.
  '.ov-progress > span': {
    display: 'block', height: '100%', background: 'var(--accent)', transition: 'width .6s ease',
    backgroundImage: 'linear-gradient(45deg, rgba(255,255,255,.28) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.28) 50%, rgba(255,255,255,.28) 75%, transparent 75%)',
    backgroundSize: '12px 12px', animation: 'ov-stripes .8s linear infinite',
  },
  '.ov-progress.ov-done > span, .ov-done .ov-progress > span': { backgroundImage: 'none', animation: 'none', background: 'var(--ok, #22a355)' },
  // Length unknown: a segment slides along the track.
  '.ov-progress.ov-indeterminate > span': { width: '35% !important', animation: 'ov-slide 1.3s ease-in-out infinite' },
  // A spinner, for an item that is waiting on work with no other indicator.
  '.ov-spinner': {
    display: 'inline-block', width: '11px', height: '11px', borderRadius: '50%', verticalAlign: '-1px',
    border: '2px solid color-mix(in srgb, var(--accent) 25%, transparent)', borderTopColor: 'var(--accent)',
    animation: 'ov-spin .8s linear infinite',
  },
  // A card or pill whose work is running: a light sweeps across it and its edge
  // takes the accent, so live work is told apart from finished work at a glance.
  '.ov-card.ov-running, .ov-card.ov-busy, .ov-pill.ov-running, .ov-pill.ov-busy': {
    borderColor: 'color-mix(in srgb, var(--accent) 55%, var(--border))',
    backgroundImage: 'linear-gradient(100deg, transparent 35%, color-mix(in srgb, var(--accent) 16%, transparent) 50%, transparent 65%)',
    backgroundSize: '250% 100%', backgroundRepeat: 'no-repeat', animation: 'ov-sweep 2.2s ease-in-out infinite',
  },
  // Status indicators for a job: a small pill whose dot says the state.
  '.ov-status': {
    display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '1px 8px 1px 6px', borderRadius: '999px',
    border: '1px solid var(--border-strong)', background: 'var(--bg-elevated)', color: 'var(--text)',
    fontSize: '11.5px', lineHeight: '18px', fontWeight: '500', whiteSpace: 'nowrap', boxShadow: '0 2px 8px rgba(0,0,0,.18)',
  },
  '.ov-status::before': { content: '""', width: '7px', height: '7px', borderRadius: '50%', background: 'var(--muted)', flexShrink: '0' },
  // Running: the dot pulses and sends out a ring, like a live recording light.
  '.ov-status.ov-running, .ov-status.ov-busy': { position: 'relative', borderColor: 'color-mix(in srgb, var(--accent) 45%, var(--border-strong))' },
  '.ov-status.ov-running::before, .ov-status.ov-busy::before': { background: 'var(--accent)', animation: 'ov-pulse 1.2s ease-in-out infinite' },
  '.ov-status.ov-running::after, .ov-status.ov-busy::after': {
    content: '""', position: 'absolute', left: '6px', top: '50%', width: '7px', height: '7px', marginTop: '-3.5px', boxSizing: 'border-box',
    borderRadius: '50%', border: '1.5px solid var(--accent)', animation: 'ov-ping 1.4s cubic-bezier(0,0,.2,1) infinite', pointerEvents: 'none',
  },
  '.ov-status.ov-done::before': { background: 'var(--ok, #22a355)' },
  // Waiting on something: a slow breath, calmer than running.
  '.ov-status.ov-waiting::before': { background: 'var(--muted)', animation: 'ov-breathe 2.6s ease-in-out infinite' },
  '.ov-status.ov-blocked::before': { background: 'var(--warn, #f59e32)' },
  '.ov-status.ov-failed::before': { background: 'var(--danger, #e5484d)' },
  '.ov-status.ov-done': { color: 'var(--ok, #22a355)' },
  '.ov-status.ov-blocked': { color: 'var(--warn, #f59e32)' },
  '.ov-status.ov-failed': { color: 'var(--danger, #e5484d)' },
}

/**
 * The keyframes the classes above animate with, plus the reduced-motion switch.
 * Global CSS rather than part of the spec, so the doc and the board both mount
 * exactly this one copy (keyframe names are global either way).
 */
export const OVERLAY_KEYFRAMES = [
  '@keyframes ov-pulse{0%,100%{opacity:1}50%{opacity:.35}}',
  '@keyframes ov-ping{0%{transform:scale(1);opacity:.9}80%,100%{transform:scale(2.6);opacity:0}}',
  '@keyframes ov-breathe{0%,100%{opacity:.45}50%{opacity:1}}',
  '@keyframes ov-spin{to{transform:rotate(360deg)}}',
  '@keyframes ov-stripes{from{background-position:0 0}to{background-position:12px 0}}',
  '@keyframes ov-slide{0%{transform:translateX(-110%)}100%{transform:translateX(320%)}}',
  '@keyframes ov-sweep{0%{background-position:120% 0}100%{background-position:-120% 0}}',
  // Motion carries state here, so reduced motion keeps the state and drops the
  // movement: the colours and borders above still say "running".
  '@media (prefers-reduced-motion: reduce){[class*="ov-"],[class*="ov-"]::before,[class*="ov-"]::after{animation:none!important;transition:none!important}}',
].join('\n')

interface Placed { item: OverlayItem; where: 'margin' | 'over' | 'tray'; top: number; left: number }

function overlayPlugin(host: { current: { openSession: (key: string) => void } }) {
  return ViewPlugin.fromClass(class {
    layer: HTMLElement
    tray: HTMLElement
    constructor(readonly view: EditorView) {
      this.layer = document.createElement('div')
      this.layer.className = 'cm-overlay'
      this.layer.setAttribute('data-testid', 'doc-overlay')
      this.tray = document.createElement('div')
      this.tray.className = 'cm-overlay-tray'
      this.tray.setAttribute('data-testid', 'doc-overlay-tray')
      this.layer.onclick = e => {
        const el = (e.target as HTMLElement | null)?.closest?.('[data-open-session]') as HTMLElement | null
        const key = el?.getAttribute('data-open-session')
        if (key) { e.preventDefault(); host.current.openSession(key) }
      }
      view.scrollDOM.appendChild(this.layer)
      this.schedule()
    }
    update(u: ViewUpdate) {
      if (u.docChanged || u.geometryChanged || u.viewportChanged || u.transactions.some(t => t.effects.some(e => e.is(setOverlay)))) this.schedule()
    }
    // Positions are read in CodeMirror's measure phase: layout may not be read
    // during an update.
    schedule() {
      this.view.requestMeasure({ key: this, read: view => this.place(view), write: placed => this.draw(placed) })
    }
    place(view: EditorView): Placed[] {
      const text = view.state.doc.toString()
      const box = view.scrollDOM.getBoundingClientRect()
      return view.state.field(overlayItems).map(item => {
        const pos = item.anchor ? findAnchor(text, item.anchor) : null
        if (pos === null) return { item, where: 'tray' as const, top: 0, left: 0 }
        if (item.place === 'margin') return { item, where: 'margin' as const, top: view.lineBlockAt(pos).top, left: 0 }
        // Just above the anchored words, where they are on screen right now. A
        // position that cannot be measured falls back to its line's.
        let coords: { top: number; left: number } | null = null
        try { coords = view.coordsAtPos(pos) } catch { coords = null }
        return {
          item, where: 'over' as const,
          top: coords ? coords.top - box.top + view.scrollDOM.scrollTop : view.lineBlockAt(pos).top,
          left: coords ? coords.left - box.left + view.scrollDOM.scrollLeft : 0,
        }
      })
    }
    draw(placed: Placed[]) {
      const margin: { el: HTMLElement; top: number }[] = []
      this.layer.replaceChildren(this.tray)
      this.tray.replaceChildren()
      for (const p of placed) {
        const el = document.createElement('div')
        el.setAttribute('data-testid', 'overlay-item')
        el.innerHTML = p.item.html
        if (p.where === 'tray') {
          el.className = 'cm-overlay-item cm-overlay-tray-item'
          this.tray.append(el)
          continue
        }
        el.className = `cm-overlay-item cm-overlay-${p.where}`
        el.style.top = `${p.top}px`
        if (p.where === 'over') el.style.left = `${p.left}px`
        this.layer.append(el)
        if (p.where === 'margin') margin.push({ el, top: p.top })
      }
      this.tray.hidden = this.tray.childElementCount === 0
      // Margin items never overlap: each starts below the one above it, and all
      // of them below the tray.
      let floor = this.tray.hidden ? -Infinity : this.tray.offsetHeight + MARGIN_GAP
      for (const m of margin.sort((a, b) => a.top - b.top)) {
        const top = Math.max(m.top, floor)
        m.el.style.top = `${top}px`
        floor = top + m.el.offsetHeight + MARGIN_GAP
      }
    }
    destroy() { this.layer.remove() }
  })
}

const overlayTheme = EditorView.baseTheme({
  '.cm-scroller': { position: 'relative' },
  // An overlay draws over the page, never in the text's way: margin items and the
  // tray float just right of the text column, which keeps its full width.
  '.cm-overlay': { position: 'absolute', inset: '0', pointerEvents: 'none', zIndex: '4' },
  '.cm-overlay-item': {
    position: 'absolute', pointerEvents: 'auto', color: 'var(--text)', fontSize: '12.5px', lineHeight: '1.45',
    overflow: 'hidden', contain: 'layout paint',
  },
  '.cm-overlay-margin': { left: 'calc(100% + 24px)', width: '216px' },
  '.cm-overlay-over': {
    transform: 'translateY(calc(-100% - 2px))', maxWidth: '320px', opacity: '0.88', transition: 'opacity .15s',
  },
  '.cm-overlay-over:hover': { opacity: '1' },
  '.cm-overlay-tray': {
    position: 'absolute', top: '0', left: 'calc(100% + 24px)', width: '216px', display: 'flex', flexDirection: 'column', gap: '6px',
    pointerEvents: 'auto',
  },
  '.cm-overlay-tray-item': { position: 'static' },
  ...OVERLAY_CLASS_STYLE,
})

export function overlayExtension(host: { current: { openSession: (key: string) => void } }): Extension {
  return [overlayItems, overlayPlugin(host), overlayTheme]
}

import { useCallback, useEffect, useRef, useState } from 'react'
import type { MutableRefObject, RefObject } from 'react'
import { ChevronDown, ChevronUp, Search, X } from 'lucide-react'

import { i18nT } from '../../i18n/t'

const MAX_MATCHES = 800

/** CSS Custom Highlight API (Chromium 105+); absent from some TS lib targets. */
interface HighlightRegistryLike {
  set(name: string, highlight: unknown): void
  delete(name: string): void
}

function highlightRegistry(): HighlightRegistryLike | null {
  const css = (globalThis as { CSS?: { highlights?: HighlightRegistryLike } }).CSS
  const Ctor = (globalThis as { Highlight?: unknown }).Highlight
  return css?.highlights && typeof Ctor === 'function' ? css.highlights : null
}

function makeHighlight(...ranges: Range[]): unknown {
  const Ctor = (globalThis as { Highlight?: new (...r: Range[]) => unknown }).Highlight
  return Ctor ? new Ctor(...ranges) : null
}

/** Collect match ranges for `query` across the text nodes under `root`. */
export function findRanges(root: Node | null, query: string): Range[] {
  const out: Range[] = []
  if (!root || !query) return out
  const q = query.toLowerCase()
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let node: Node | null
  while ((node = walker.nextNode())) {
    const text = (node.nodeValue || '').toLowerCase()
    let idx = 0
    while ((idx = text.indexOf(q, idx)) !== -1) {
      const range = new Range()
      range.setStart(node, idx)
      range.setEnd(node, idx + query.length)
      out.push(range)
      idx += query.length
      if (out.length >= MAX_MATCHES) return out
    }
  }
  return out
}

export interface FindState {
  open: boolean
  setOpen: (open: boolean) => void
  query: string
  setQuery: (q: string) => void
  index: number
  total: number
  jump: (delta: number) => void
  inputRef: MutableRefObject<HTMLInputElement | null>
}

/**
 * Find-in-document over the rendered viewer body. Matches are painted with
 * the CSS Custom Highlight API — no DOM mutation, so React-rendered content
 * (markdown, tables, code) is never disturbed. Degrades to count-and-jump
 * only when the API is unavailable.
 */
export function useFindInDocument(
  bodyRef: RefObject<HTMLElement | null>,
  contentKey: string,
  enabled: boolean,
): FindState {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const [total, setTotal] = useState(0)
  const ranges = useRef<Range[]>([])
  const inputRef = useRef<HTMLInputElement | null>(null)

  const scrollTo = (range: Range | undefined) => {
    const el = range?.startContainer.parentElement
    el?.scrollIntoView({ block: 'center' })
  }

  useEffect(() => {
    const reg = highlightRegistry()
    reg?.delete('kc-fe-find')
    reg?.delete('kc-fe-find-active')
    ranges.current = []
    if (!open || !query || !enabled) {
      setTotal(0)
      return
    }
    const timer = setTimeout(() => {
      const found = findRanges(bodyRef.current, query)
      ranges.current = found
      setTotal(found.length)
      setIndex(0)
      if (found.length && reg) {
        reg.set('kc-fe-find', makeHighlight(...found))
        reg.set('kc-fe-find-active', makeHighlight(found[0]))
      }
      if (found.length) scrollTo(found[0])
    }, 140)
    return () => clearTimeout(timer)
  }, [bodyRef, open, query, enabled, contentKey])

  useEffect(() => () => {
    const reg = highlightRegistry()
    reg?.delete('kc-fe-find')
    reg?.delete('kc-fe-find-active')
  }, [])

  const jump = useCallback((delta: number) => {
    const found = ranges.current
    if (!found.length) return
    setIndex((prev) => {
      const next = ((prev + delta) % found.length + found.length) % found.length
      highlightRegistry()?.set('kc-fe-find-active', makeHighlight(found[next]))
      scrollTo(found[next])
      return next
    })
  }, [])

  return { open, setOpen, query, setQuery, index, total, jump, inputRef }
}

export function FindBar({ find, fileName }: { find: FindState; fileName: string }) {
  if (!find.open) return null
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 10px', borderBottom: '1px solid var(--border)', flex: '0 0 auto' }}>
      {/* ::highlight() rules ship with the component so the feature is self-contained. */}
      <style>{'::highlight(kc-fe-find){background:rgba(255,213,79,.5);color:#1a1a1a}::highlight(kc-fe-find-active){background:#ff9838;color:#111}'}</style>
      <Search size={12} style={{ color: 'var(--muted)' }} />
      <input
        ref={find.inputRef}
        value={find.query}
        placeholder={i18nT('apps.fileExplorer.find.find_in_file', { name: fileName })}
        aria-label={i18nT('apps.fileExplorer.find.find_in_file', { name: fileName })}
        onChange={(e) => find.setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); find.jump(e.shiftKey ? -1 : 1) }
          if (e.key === 'Escape') { find.setOpen(false); find.setQuery('') }
        }}
        style={{ flex: 1, maxWidth: 340, background: 'transparent', border: '1px solid var(--border)', borderRadius: 4, padding: '3px 8px', fontSize: 12, color: 'inherit', outline: 'none' }}
        autoFocus
      />
      <span data-testid="fe-find-count" style={{ fontSize: 11, color: 'var(--muted)', minWidth: 52, textAlign: 'center' }}>
        {find.query ? `${find.total ? find.index + 1 : 0} / ${find.total}` : ''}
      </span>
      <button className="mc-fe-iconbtn" onClick={() => find.jump(-1)} aria-label={i18nT('apps.fileExplorer.find.previous_match')}><ChevronUp size={12} /></button>
      <button className="mc-fe-iconbtn" onClick={() => find.jump(1)} aria-label={i18nT('apps.fileExplorer.find.next_match')}><ChevronDown size={12} /></button>
      <button className="mc-fe-iconbtn" onClick={() => { find.setOpen(false); find.setQuery('') }} aria-label={i18nT('apps.fileExplorer.find.close_find')}><X size={12} /></button>
    </div>
  )
}

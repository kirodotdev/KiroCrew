import { useEffect, useMemo, useRef } from 'react'
import type { SearchMatch } from '../hooks/useMessageSearch'
import type { ChatMessage } from '../types'
import { searchableTextMemo } from '../utils/searchableText'

import { i18nT } from '../i18n/t'
interface SearchResultsListProps {
  matches: SearchMatch[]
  currentIdx: number
  messages: ChatMessage[]
  term: string
  caseSensitive: boolean
  /** Jump to a match by its index in `matches`. */
  onJump: (matchIdx: number) => void
}

const CTX_BEFORE = 36
const CTX_AFTER = 64
// Render cap — beyond this the DOM cost of hundreds of rows isn't worth it;
// users narrow the term instead. The find-bar counter still shows the true total.
const MAX_ROWS = 200

// Shared ARIA wiring so the SearchBar input (combobox) can point at this
// listbox and its active option via aria-controls / aria-activedescendant.
export const SEARCH_LISTBOX_ID = 'mc-search-results-listbox'
export const searchOptionId = (matchIdx: number) => `mc-search-opt-${matchIdx}`

interface Snippet {
  matchIdx: number
  prefix: string
  match: string
  suffix: string
  role: string
}

/** Find the char offset of the `occ`-th (0-based) occurrence of `needle` in `hay`. */
function nthIndex(hay: string, needle: string, occ: number): number {
  let pos = 0
  let idx = -1
  for (let k = 0; k <= occ; k++) {
    idx = hay.indexOf(needle, pos)
    if (idx === -1) return -1
    pos = idx + needle.length
  }
  return idx
}

/**
 * Slack/Word-style results panel: one row per match occurrence, each showing a
 * context snippet with the term highlighted. Clicking a row jumps to that match;
 * the active row is highlighted and auto-scrolled into view. Complements the
 * highlight-and-cycle find bar for the "many matches" case — read context and
 * jump precisely instead of blind next/prev cycling.
 */
export default function SearchResultsList({
  matches,
  currentIdx,
  messages,
  term,
  caseSensitive,
  onJump,
}: SearchResultsListProps) {
  const listRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<HTMLButtonElement>(null)

  // When there are more matches than we render, slide the window so it always
  // contains the active match — otherwise navigating past row MAX_ROWS via the
  // find bar would leave currentIdx outside the rendered range (no highlight,
  // no auto-scroll).
  const windowStart = useMemo(() => {
    if (matches.length <= MAX_ROWS) return 0
    const half = Math.floor(MAX_ROWS / 2)
    return Math.max(0, Math.min(currentIdx - half, matches.length - MAX_ROWS))
  }, [matches.length, currentIdx])

  const snippets = useMemo<Snippet[]>(() => {
    if (!term) return []
    const needle = caseSensitive ? term : term.toLowerCase()
    const out: Snippet[] = []
    const end = Math.min(matches.length, windowStart + MAX_ROWS)
    for (let i = windowStart; i < end; i++) {
      const m = matches[i]
      const msg = messages[m.msgIdx]
      const content = msg ? searchableTextMemo(msg) : ''
      const role = msg?.role ?? ''
      const hay = caseSensitive ? content : content.toLowerCase()
      const start = nthIndex(hay, needle, m.occ)
      if (start === -1) {
        out.push({ matchIdx: i, prefix: '', match: term, suffix: '', role })
        continue
      }
      const mEnd = start + term.length
      const sliceStart = Math.max(0, start - CTX_BEFORE)
      const sliceEnd = Math.min(content.length, mEnd + CTX_AFTER)
      // Collapse whitespace/newlines so each snippet stays one tidy line.
      const clean = (s: string) => s.replace(/\s+/g, ' ')
      out.push({
        matchIdx: i,
        prefix: (sliceStart > 0 ? '…' : '') + clean(content.slice(sliceStart, start)),
        match: content.slice(start, mEnd),
        suffix: clean(content.slice(mEnd, sliceEnd)) + (sliceEnd < content.length ? '…' : ''),
        role,
      })
    }
    return out
  }, [matches, messages, term, caseSensitive, windowStart])

  // Keep the active result visible as the user cycles with the find bar.
  useEffect(() => {
    activeRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [currentIdx])

  if (snippets.length === 0) return null

  const roleLabel = (role: string) => (role === 'user' ? 'You' : role === 'assistant' ? 'Assistant' : role)

  return (
    <div
      ref={listRef}
      id={SEARCH_LISTBOX_ID}
      className="h-full overflow-y-auto py-1 text-[13px]"
      role="listbox"
      aria-label={i18nT('components.searchResultsList.search_results')}
    >
      {snippets.map(s => {
        const active = s.matchIdx === currentIdx
        return (
          <button
            key={s.matchIdx}
            ref={active ? activeRef : undefined}
            id={searchOptionId(s.matchIdx)}
            type="button"
            tabIndex={-1}
            onClick={() => onJump(s.matchIdx)}
            role="option"
            aria-selected={active}
            className={`block w-full text-left px-3 py-1.5 border-none cursor-pointer transition-colors ${
              active ? 'bg-accent-subtle ring-1 ring-inset ring-accent' : 'bg-transparent hover:bg-bg-hover'
            }`}
          >
            <span className={`block text-[10px] uppercase tracking-wide mb-0.5 ${active ? 'text-accent font-semibold' : 'text-muted'}`}>{roleLabel(s.role)}{active ? ' · selected' : ''}</span>
            <span className={`block leading-snug ${active ? 'text-text-strong font-medium' : 'text-text'}`}>
              <span className="text-muted">{s.prefix}</span>
              <mark className="bg-accent text-accent-fg rounded-sm px-0.5">{s.match}</mark>
              <span className="text-muted">{s.suffix}</span>
            </span>
          </button>
        )
      })}
      {matches.length > MAX_ROWS && (
        <div className="px-3 py-1.5 text-[11px] text-muted border-t border-border">
          {i18nT('components.searchResultsList.showing')} {snippets.length} {i18nT('components.searchResultsList.of')} {matches.length} {i18nT('components.searchResultsList.matches_around_the_current_result_narrow_your_se')}
        </div>
      )}
    </div>
  )
}

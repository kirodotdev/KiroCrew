import { useEffect, useRef } from 'react'
import { X, ChevronUp, ChevronDown, CaseSensitive } from 'lucide-react'
import type { SearchMatch } from '../hooks/useMessageSearch'
import { platformShortcut } from '../utils/platform'
import { SEARCH_LISTBOX_ID, searchOptionId } from './SearchResultsList'

import { i18nT } from '../i18n/t'
interface SearchBarProps {
  term: string
  setTerm: (t: string) => void
  matches: SearchMatch[]
  currentIdx: number
  next: () => void
  prev: () => void
  close: () => void
  caseSensitive: boolean
  toggleCaseSensitive: () => void
  focusNonce?: number
  /** Jump to a match by index (Home/End jump to first/last). */
  goTo?: (i: number) => void
  /** Render inline (fills width, no floating chrome) for use inside the search pane header. */
  docked?: boolean
}

export default function SearchBar({ term, setTerm, matches, currentIdx, next, prev, close, caseSensitive, toggleCaseSensitive, focusNonce, goTo, docked }: SearchBarProps) {
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  // On every find-shortcut press (incl. when already open), re-focus and
  // select-all so the user can type a fresh query over the existing one.
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.focus()
    el.select()
  }, [focusNonce])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (e.shiftKey) prev()
      else next()
    }
    // ArrowDown/Up move through results from the input so the (absolutely
    // positioned) results panel is keyboard-navigable without moving focus
    // into it; the panel highlights and scrolls the active row.
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      next()
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      prev()
    }
    // Home/End jump to the first/last result — but only when the text cursor is
    // already at the input's start/end, so they still move the cursor within a
    // longer query (the WAI-ARIA combobox convention: Home/End edit the textbox
    // unless focus is in the listbox). At the boundary the cursor move is a
    // no-op anyway, so repurposing them there is lossless.
    if (e.key === 'Home' && goTo && matches.length > 0) {
      const input = e.currentTarget as HTMLInputElement
      if (input.selectionStart === 0 && input.selectionEnd === 0) {
        e.preventDefault()
        goTo(0)
      }
    }
    if (e.key === 'End' && goTo && matches.length > 0) {
      const input = e.currentTarget as HTMLInputElement
      const end = input.value.length
      if (input.selectionStart === end && input.selectionEnd === end) {
        e.preventDefault()
        goTo(matches.length - 1)
      }
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      close()
    }
  }

  return (
    <div className={docked
      ? 'flex items-center gap-1.5 w-full text-[13px]'
      : 'absolute top-14 right-4 z-20 flex items-center gap-1.5 bg-bg-elevated border border-border rounded-lg shadow-md px-2.5 py-1.5 text-[13px]'}>
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded={matches.length > 0}
        aria-controls={SEARCH_LISTBOX_ID}
        aria-autocomplete="list"
        aria-activedescendant={matches.length > 0 ? searchOptionId(currentIdx) : undefined}
        value={term}
        onChange={e => setTerm(e.target.value)}
        onKeyDown={handleKeyDown}
        aria-label={i18nT('components.searchBar.find_in_chat')}
        placeholder={i18nT('components.searchBar.find_in_chat_2')}
        className={`bg-transparent border-none outline-none text-text placeholder:text-muted text-[13px] ${docked ? 'flex-1 min-w-0' : 'w-[180px]'}`}
      />
      <button
        onClick={toggleCaseSensitive}
        className={`p-0.5 rounded cursor-pointer border-none transition-colors ${caseSensitive ? 'bg-accent/20 text-accent' : 'bg-transparent text-muted hover:text-text'}`}
        title={i18nT('components.searchBar.case_sensitive')}
        aria-label={i18nT('components.searchBar.case_sensitive')}
      >
        <CaseSensitive size={15} />
      </button>
      {term && (
        <span className="text-muted text-[12px] whitespace-nowrap tabular-nums">
          {matches.length > 0 ? `${currentIdx + 1} of ${matches.length} results` : i18nT('components.searchBar.no_results')}
        </span>
      )}
      <button onClick={prev} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.searchBar.previous', { mod: platformShortcut('Shift+Enter') })} aria-label={i18nT('components.searchBar.previous_match')}>
        <ChevronUp size={15} />
      </button>
      <button onClick={next} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.searchBar.next', { mod: platformShortcut('Enter') })} aria-label={i18nT('components.searchBar.next_match')}>
        <ChevronDown size={15} />
      </button>
      {!docked && (
        <button onClick={close} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.searchBar.close_esc')} aria-label={i18nT('components.searchBar.close_esc')}>
          <X size={15} />
        </button>
      )}
    </div>
  )
}

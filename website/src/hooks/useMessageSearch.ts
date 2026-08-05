import { useState, useEffect, useCallback } from 'react'
import type { ChatMessage } from '../types'
import { searchableTextMemo } from '../utils/searchableText'

export interface SearchMatch {
  /** Index into the messages[] Redux array. */
  msgIdx: number
  /** 0-based occurrence index within the message at `msgIdx`. */
  occ: number
}

/**
 * In-session message search hook. Must only be called from ChatPage —
 * the keydown listener is scoped to ChatPage's mount/unmount lifecycle.
 */
export function useMessageSearch(messages: ChatMessage[], activeSlot: string | null) {
  const [isOpen, setIsOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [currentIdx, setCurrentIdx] = useState(0)
  const [caseSensitive, setCaseSensitive] = useState(false)
  const [matches, setMatches] = useState<SearchMatch[]>([])
  // Bumped every time the find shortcut fires so the SearchBar can re-focus and
  // select-all — letting the user immediately type a new query over the old one.
  const [focusNonce, setFocusNonce] = useState(0)

  // Debounced match computation (50ms)
  useEffect(() => {
    if (!term) { setMatches([]); return }
    const timer = setTimeout(() => {
      const needle = caseSensitive ? term : term.toLowerCase()
      const result: SearchMatch[] = []
      for (let i = 0; i < messages.length; i++) {
        const m = messages[i]
        if (m.role !== 'user' && m.role !== 'assistant') continue
        // Skip assistant reasoning segments collapsed inside turns
        if (m.role === 'assistant') {
          const next = messages[i + 1]
          if (next && next.role === 'tool') continue
        }
        const haystack = caseSensitive ? searchableTextMemo(m) : searchableTextMemo(m).toLowerCase()
        let pos = 0
        let occ = 0
        while (true) {
          const idx = haystack.indexOf(needle, pos)
          if (idx === -1) break
          result.push({ msgIdx: i, occ })
          occ++
          pos = idx + needle.length
        }
      }
      setMatches(result)
    }, 50)
    return () => clearTimeout(timer)
  }, [messages, term, caseSensitive])

  // Clamp currentIdx when matches change
  useEffect(() => {
    setCurrentIdx(prev => (matches.length === 0 ? 0 : Math.min(prev, matches.length - 1)))
  }, [matches])

  // Reset on session switch
  useEffect(() => {
    setIsOpen(false)
    setTerm('')
    setMatches([])
    setCurrentIdx(0)
  }, [activeSlot])

  const open = useCallback(() => setIsOpen(true), [])
  const close = useCallback(() => {
    setIsOpen(false)
    setTerm('')
    setMatches([])
    setCurrentIdx(0)
  }, [])
  const next = useCallback(() => {
    setCurrentIdx(prev => (matches.length === 0 ? 0 : (prev + 1) % matches.length))
  }, [matches.length])
  const prev = useCallback(() => {
    setCurrentIdx(prev => (matches.length === 0 ? 0 : (prev - 1 + matches.length) % matches.length))
  }, [matches.length])
  // Jump directly to an arbitrary match (Home/End in the find bar, result-row clicks).
  // Clamped to the valid range so a stale index from a shrinking match set
  // can never point past the array.
  const goTo = useCallback((idx: number) => {
    setCurrentIdx(() => (matches.length === 0 ? 0 : Math.max(0, Math.min(idx, matches.length - 1))))
  }, [matches.length])
  const toggleCaseSensitive = useCallback(() => setCaseSensitive(p => !p), [])

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'f') {
        // Yield Cmd/Ctrl+F to app surfaces that own their own in-file find
        // (e.g. the file explorer). Without this, an in-file search hijacks
        // the key and opens the chat message-search pane instead. This
        // document-level handler runs before the file explorer's window-level
        // handler (bubble order), so bailing here lets that handler run.
        // Guard the closest() call: e.target may be Document (no closest()).
        const target = e.target as Element | null
        if (target && typeof target.closest === 'function' && target.closest('.mc-fe-root')) return
        e.preventDefault()
        setIsOpen(true)
        setFocusNonce(n => n + 1)
      }
      if (e.key === 'Escape' && isOpen) {
        close()
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [isOpen, close])

  const current = matches[currentIdx] as SearchMatch | undefined
  const currentMessageIdx = current?.msgIdx ?? -1
  const currentOccurrenceIdx = current?.occ ?? -1

  return {
    isOpen,
    term,
    setTerm,
    matches,
    currentIdx,
    next,
    prev,
    goTo,
    open,
    close,
    caseSensitive,
    toggleCaseSensitive,
    currentMessageIdx,
    currentOccurrenceIdx,
    focusNonce,
  }
}

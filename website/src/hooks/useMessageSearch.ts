import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import type { ChatMessage } from '../types'
import { hasCommandModifier } from '../utils/commandModifier'
import { isMac } from '../utils/platform'
import { focusComposer } from '../pages/chat/composerFocus'
import { useStableMessageKey } from '../chat-core/transcript/useStableMessageKey'
import { CONVERGE_MAX_MS } from '../utils/searchScroll'
import type { RevealLedger } from './SearchHighlightContext'
import { findMatchIds, findReducer, livePosition, messageKeys, NO_FIND, type FindState, type RowIndex } from './messageSearchState'

export interface SearchMatch {
  /** Index into the messages[] Redux array. */
  msgIdx: number
  /** 0-based occurrence index within the message at `msgIdx`. */
  occ: number
}

const NO_ROWS: RowIndex = new Map()
const clearFind = (s: FindState) => findReducer(s, { type: 'clear' })

/**
 * In-session message search hook: ChatPage's, and a Bot chat's (`ChatPane`
 * with `findInChat`). The keydown listener lives as long as the caller is
 * mounted, so only one caller on a page may pass `enabled`; `enabled: false`
 * leaves the shortcut to the browser and closes an open bar.
 *
 * The current match is the match itself, not its place in the list (see
 * messageSearchState): rows arriving around it leave it current and leave the
 * reader where they are. Only a find action hands out a `reveal`, the signal
 * the hosts and rows move the reader by.
 */
export function useMessageSearch(messages: ChatMessage[], activeSlot: string | null, enabled = true) {
  const [isOpen, setIsOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [caseSensitive, setCaseSensitive] = useState(false)
  // One state, with findReducer as the updater rather than useReducer: a
  // 'clear' that changes nothing returns the same state, and only useState
  // skips that render. Each streamed chunk sends one while the bar is closed.
  const [find, setFind] = useState<FindState>(NO_FIND)
  // Bumped every time the find shortcut fires so the SearchBar can re-focus and
  // select-all — letting the user immediately type a new query over the old one.
  const [focusNonce, setFocusNonce] = useState(0)
  // Mirror of isOpen readable from the identity-stable close() below. close()
  // must keep useCallback([]) — ChatPage's handleFileOpen/handleFolderOpen list
  // `search.close` in their dep arrays precisely because it never churns — so
  // it cannot read the isOpen state directly without changing identity. Synced
  // post-commit, which is exact for close(): every caller is an event handler
  // (Escape, the bar's close button, ChatPage's file/folder-open paths) and
  // event handlers only run after the commit that made the bar visible.
  const isOpenRef = useRef(false)
  useEffect(() => { isOpenRef.current = isOpen }, [isOpen])

  // Each message's key while a search is on, and each key's index in the list
  // this render was given: a match is mapped onto its message's index in the
  // same render, so no render points a match at another message. (The Sessions
  // chat's rows draw a deferred snapshot of that list; ChatPage hands them the
  // match's index in it.)
  const keyOf = useStableMessageKey()
  const searching = term !== '' || find.ids.length > 0
  const msgKeys = useMemo(() => (searching ? messageKeys(messages, keyOf) : null), [searching, messages, keyOf])
  const rows = useMemo<RowIndex>(() => (msgKeys ? new Map(msgKeys.map((k, i) => [k, i])) : NO_ROWS), [msgKeys])
  // Read by the identity-stable steps below: a step moves over the matches
  // whose rows the last render held.
  const rowsRef = useRef(rows)
  rowsRef.current = rows

  // Debounced match computation (50ms)
  useEffect(() => {
    // An empty search that stays empty returns the same state, so this sends
    // nothing to render on every `messages` change (each streamed chunk) while
    // the search bar is closed.
    if (!term) { setFind(clearFind); return }
    if (!msgKeys) return
    const timer = setTimeout(() => {
      const ids = findMatchIds(messages, msgKeys, term, caseSensitive)
      setFind(s => findReducer(s, { type: 'found', ids, term, caseSensitive }))
    }, 50)
    return () => clearTimeout(timer)
  }, [messages, msgKeys, term, caseSensitive])

  // Reset on session switch
  useEffect(() => {
    setIsOpen(false)
    setTerm('')
    setFind(clearFind)
  }, [activeSlot])

  const open = useCallback(() => setIsOpen(true), [])
  // Clears the bar without touching focus.
  const reset = useCallback(() => {
    isOpenRef.current = false
    setIsOpen(false)
    setTerm('')
    setFind(clearFind)
  }, [])
  const close = useCallback(() => {
    // Focus is handed back only when the bar was actually open: ChatPage's
    // file/folder-open handlers call close() unconditionally to un-gate the
    // dock, and a close that never dismissed anything must not steal focus.
    const wasOpen = isOpenRef.current
    reset()
    // In the close path itself — not the Escape handler — so the bar's own
    // close button hands typing back to the composer identically. focusComposer
    // already defers a frame, skips touch devices, and no-ops when the composer
    // is unmounted or the session switched, so closing never throws.
    if (wasOpen) focusComposer()
  }, [reset])
  const next = useCallback(() => {
    const rows = rowsRef.current
    setFind(s => findReducer(s, { type: 'step', delta: 1, rows }))
  }, [])
  const prev = useCallback(() => {
    const rows = rowsRef.current
    setFind(s => findReducer(s, { type: 'step', delta: -1, rows }))
  }, [])
  // Jump directly to an arbitrary match (Home/End in the find bar, result-row clicks).
  // Clamped to the valid range so a stale index from a shrinking match set
  // can never point past the array.
  const goTo = useCallback((index: number) => {
    const rows = rowsRef.current
    setFind(s => findReducer(s, { type: 'goto', index, rows }))
  }, [])
  const toggleCaseSensitive = useCallback(() => setCaseSensitive(p => !p), [])

  // The matches whose rows are in the list, in this render's indices. Between a
  // row leaving and the next search (one debounce), a current match whose row
  // left shows as the next one, which no find action asked for.
  const live = useMemo(() => {
    const places: number[] = []
    const matches: SearchMatch[] = []
    find.ids.forEach((id, i) => {
      const msgIdx = rows.get(id.key)
      if (msgIdx === undefined) return
      places.push(i)
      matches.push({ msgIdx, occ: id.occ })
    })
    return { places, matches }
  }, [find.ids, rows])
  const pos = livePosition(find.at, live.places)
  const reveal = pos >= 0 && live.places[pos] === find.at ? find.reveal : 0
  // When the shown find action was handed out. A row may start centering for it
  // only within one converge backstop: a row that unmounted before it finished
  // centering, or a row no host could move to, must not move the reader when
  // the reader's own scroll mounts it later.
  const issuedRef = useRef({ reveal: 0, at: 0 })
  if (reveal !== issuedRef.current.reveal) issuedRef.current = { reveal, at: reveal ? performance.now() : 0 }
  // Which reveals a row has already used, for every row of this search.
  const revealedRef = useRef(0)
  const revealLedger = useMemo<RevealLedger>(() => ({
    pending: n => n > revealedRef.current && n === issuedRef.current.reveal
      && performance.now() - issuedRef.current.at < CONVERGE_MAX_MS,
    consume: n => { if (n > revealedRef.current) revealedRef.current = n },
  }), [])

  // A host that stops offering the find (its chat is covered, or the user moved
  // into a side panel) clears the bar without close()'s focus hand-back: the
  // user's focus is already where they want it, not in the composer.
  useEffect(() => { if (!enabled) reset() }, [enabled, reset])

  // Keyboard shortcuts
  useEffect(() => {
    if (!enabled) return
    const handler = (e: KeyboardEvent) => {
      // `hasCommandModifier` accepts either primary modifier, which is right for
      // every chord except this one on a Mac: Ctrl+F is Cocoa's `forward-char`
      // Emacs binding, live in every text field including the composer, so
      // answering it here would open the find pane instead of moving the caret
      // one character right. On Mac the pane is ⌘F only and bare Ctrl+F falls
      // through unhandled; Windows/Linux keep Ctrl+F, where no such binding
      // exists. (Ctrl+⌘F stays with macOS either way — hasCommandModifier
      // rejects both-modifiers so Toggle Full Screen survives.) Shift is ⌘⇧F /
      // Ctrl+Shift+F, Search all chats; macOS still reports `e.key` as 'f' then,
      // so the Shift state, not the key's case, keeps the two chords apart.
      if (hasCommandModifier(e) && (!isMac || e.metaKey) && !e.shiftKey && e.key === 'f') {
        // Yield to a surface that already answered the chord. In an edit
        // session Pierre binds cmdOrCtrl+f on its own content element and calls
        // preventDefault() without stopPropagation(), so this document-level
        // handler still runs; opening chat search here would stack a
        // transcript-scoped find on top of the editor's own find panel.
        if (e.defaultPrevented) return
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
  }, [isOpen, close, enabled])

  const current = pos >= 0 ? live.matches[pos] : undefined
  const currentMessageIdx = current?.msgIdx ?? -1
  const currentOccurrenceIdx = current?.occ ?? -1
  const currentRowKey = pos >= 0 ? find.ids[live.places[pos]].key : null

  return {
    isOpen,
    term,
    setTerm,
    matches: live.matches,
    currentIdx: Math.max(pos, 0),
    next,
    prev,
    goTo,
    open,
    close,
    caseSensitive,
    toggleCaseSensitive,
    currentMessageIdx,
    currentOccurrenceIdx,
    /** The key of the message that holds the current match, the same across
     *  renders while it stays that message (null with no current match). */
    currentRowKey,
    /** The find action that asked to move the reader to the current match: a
     *  new term or case, a step, Home or End, or a result click. 0 when the
     *  current match became current without one. A host moves the reader once
     *  per value; a row centers its occurrence once per value (`revealLedger`). */
    reveal,
    revealLedger,
    focusNonce,
  }
}

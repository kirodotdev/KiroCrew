import { useState, useEffect, useCallback, useRef } from 'react'
import { hasCommandModifier } from '../utils/commandModifier'
import { isMac } from '../utils/platform'
import { focusComposer } from '../pages/chat/composerFocus'

/**
 * The chat's find shortcut: ⌘F / Ctrl+F opens the chat's history search
 * (`components/chatHistorySearch`), ChatPage's and a Bot chat's (`ChatPane`
 * with `findInChat`). The keydown listener lives as long as the caller is
 * mounted, so only one caller on a page may pass `enabled`; `enabled: false`
 * leaves the shortcut to the browser and closes an open search.
 */
export function useMessageSearch(activeSlot: string | null, enabled = true) {
  const [isOpen, setIsOpen] = useState(false)
  // Bumped every time the find shortcut fires so the search field can re-focus
  // and select-all, letting the user type a new query over the old one.
  const [focusNonce, setFocusNonce] = useState(0)
  // Mirror of isOpen readable from the identity-stable close() below. close()
  // must keep useCallback([]) — ChatPage's handleFileOpen/handleFolderOpen list
  // `search.close` in their dep arrays precisely because it never churns — so
  // it cannot read the isOpen state directly without changing identity. Synced
  // post-commit, which is exact for close(): every caller is an event handler
  // (Escape, the search's Cancel, ChatPage's file/folder-open paths) and
  // event handlers only run after the commit that made the search visible.
  const isOpenRef = useRef(false)
  useEffect(() => { isOpenRef.current = isOpen }, [isOpen])

  // Reset on session switch
  useEffect(() => { setIsOpen(false) }, [activeSlot])

  // The header's Search button and the side panel's + menu open it, so a
  // search already open takes focus again too.
  const open = useCallback(() => {
    setIsOpen(true)
    setFocusNonce(n => n + 1)
  }, [])
  // Closes the search without touching focus.
  const reset = useCallback(() => {
    isOpenRef.current = false
    setIsOpen(false)
  }, [])
  const close = useCallback(() => {
    // Focus is handed back only when the search was actually open: ChatPage's
    // file/folder-open handlers call close() unconditionally to un-gate the
    // dock, and a close that never dismissed anything must not steal focus.
    const wasOpen = isOpenRef.current
    reset()
    // In the close path itself — not the Escape handler — so the search's own
    // Cancel hands typing back to the composer identically. focusComposer
    // already defers a frame, skips touch devices, and no-ops when the composer
    // is unmounted or the session switched, so closing never throws.
    if (wasOpen) focusComposer()
  }, [reset])

  // A host that stops offering the find (its chat is covered, or the user moved
  // into a side panel) closes the search without close()'s focus hand-back: the
  // user's focus is already where they want it, not in the composer.
  useEffect(() => { if (!enabled) reset() }, [enabled, reset])

  // Keyboard shortcuts
  useEffect(() => {
    if (!enabled) return
    const handler = (e: KeyboardEvent) => {
      // `hasCommandModifier` accepts either primary modifier, which is right for
      // every chord except this one on a Mac: Ctrl+F is Cocoa's `forward-char`
      // Emacs binding, live in every text field including the composer, so
      // answering it here would open the history search instead of moving the caret
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
        // The chord toggles: in the search's own focused field it ends the
        // search; anywhere else (the search behind another tab, or the field
        // not focused) it brings the search forward and focuses the field.
        if (isOpen && historySearchFieldFocused()) close()
        else open()
      }
      // A search kept open in a side panel tab the person moved off is not on
      // screen, so an Escape aimed elsewhere leaves it be. An Escape a surface
      // inside it already answered (a Radix menu in the preview closing) is
      // that surface's, as the shortcut branch above yields too.
      if (e.key === 'Escape' && isOpen && !e.defaultPrevented && !historySearchOffScreen()) {
        close()
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [isOpen, open, close, enabled])

  return { isOpen, open, close, focusNonce }
}

/** Marks for the element a host draws the history search into, as a side
 *  panel tab that stays mounted while another tab or a hidden panel covers it.
 *  On screen the element owns Escape (chat-input/escapeOwners.ts), so the
 *  composer's Escape-to-stop and dictation discard yield to the search. Off
 *  screen it owns nothing, and the find shortcut's Escape close stands down. */
export function historySearchHostMarks(onScreen: boolean): Record<string, string> {
  return onScreen ? { 'data-escape-owner': '' } : { 'data-history-search-off-screen': '' }
}

/** True when the focused element is an on-screen history search field. The
 *  selectors are inlined for the strict-i18n gate (see escapeOwners.ts). */
function historySearchFieldFocused(): boolean {
  if (typeof document === 'undefined') return false
  const el = document.activeElement
  return !!el && typeof el.closest === 'function'
    && el.closest('[data-history-search-field]') != null
    && el.closest('[data-history-search-off-screen]') == null
}

/** True when the open history search sits in a host element that is off screen.
 *  The selector is inlined for the strict-i18n gate (see escapeOwners.ts). */
function historySearchOffScreen(): boolean {
  return typeof document !== 'undefined' && document.querySelector('[data-history-search-off-screen]') != null
}

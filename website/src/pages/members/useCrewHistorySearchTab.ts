/**
 * The bot chat's history search as an on-demand tab of the Bots page side
 * panel, docked or as the overlay (full width on a phone).
 *
 * The bot's `ChatPane` owns the search (⌘F opens it, Escape and Cancel close
 * it) and reports each open and close here. While it is open the side panel
 * carries a Search tab whose body is the element the pane draws the search
 * into, so the search sits in the right-hand panel and never covers the
 * transcript. When the search ends with its tab in front, a panel the search
 * opened closes again (on a phone the overlay always closes), and a panel that
 * was already open goes back to the tab that was in front before. Hiding the
 * panel ends the search, so a later open never shows the tab.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

/** The Search tab's id in the side panel strip. */
export const CREW_HISTORY_SEARCH_TAB_ID = 'crew-history-search'
/** The strip's `keepFocusIds`: the Search tab may hold the focus, is never the
 *  default, and is never persisted. */
export const CREW_HISTORY_SEARCH_TAB_IDS: readonly string[] = [CREW_HISTORY_SEARCH_TAB_ID]

interface Options {
  activeTabId: string | null
  setActive: (id: string | null) => void
  /** Focused on close when no tab was focused before the search opened. */
  fallbackTabId: string
  isMobile: boolean
  beside: boolean
  setDockedOpen: (open: boolean) => void
  setOverlayOpen: (open: boolean) => void
  closeReplyThread: () => void
  /** The side panel is open (docked or as the overlay). */
  panelOpen: boolean
  /** The side panel is shown and no reply thread covers its tabs. */
  panelShown: boolean
}

/** Decided when the search opens. */
interface OpenSearch {
  returnTo: string | null
  /** The search opened the panel, so ending it closes the panel too. */
  closePanel: boolean
}

/** The bot pane's own search controls (`ChatPane`'s `onHistorySearchOpener`). */
interface SearchControls { open: () => void; close?: () => void }

export function useCrewHistorySearchTab({ activeTabId, setActive, fallbackTabId, isMobile, beside, setDockedOpen, setOverlayOpen, closeReplyThread, panelOpen, panelShown }: Options) {
  const [open, setOpen] = useState(false)
  // The tab body the pane draws the search into; set by the body's ref.
  const [host, setHost] = useState<HTMLElement | null>(null)
  const openRef = useRef<OpenSearch | null>(null)
  const activeRef = useRef(activeTabId)
  activeRef.current = activeTabId
  const panelOpenRef = useRef(panelOpen)
  panelOpenRef.current = panelOpen
  // Null while no bot chat is mounted: then the header's Search button and
  // the + menu's Search row are not offered.
  const [controls, setControls] = useState<SearchControls | null>(null)
  const setHistorySearchOpener = useCallback((openFn: (() => void) | null, closeFn?: () => void) => {
    setControls(openFn ? { open: openFn, close: closeFn } : null)
  }, [])

  // Called on every ⌘F while the search is open too, so a hidden panel or
  // another focused tab gives the search back.
  const onOpenChange = useCallback((next: boolean) => {
    if (next) {
      closeReplyThread()
      if (!openRef.current) openRef.current = { returnTo: null, closePanel: isMobile || !panelOpenRef.current }
      if (activeRef.current !== CREW_HISTORY_SEARCH_TAB_ID) openRef.current.returnTo = activeRef.current
      setOpen(true)
      setActive(CREW_HISTORY_SEARCH_TAB_ID)
      if (beside) setDockedOpen(true)
      else setOverlayOpen(true)
      return
    }
    const ended = openRef.current
    openRef.current = null
    setOpen(false)
    if (activeRef.current !== CREW_HISTORY_SEARCH_TAB_ID) return
    setActive(ended?.returnTo ?? fallbackTabId)
    if (!ended?.closePanel) return
    if (beside) setDockedOpen(false)
    else setOverlayOpen(false)
  }, [isMobile, setActive, fallbackTabId, beside, setDockedOpen, setOverlayOpen, closeReplyThread])

  // Hiding the panel ends the search.
  const wasPanelOpenRef = useRef(panelOpen)
  useEffect(() => {
    const was = wasPanelOpenRef.current
    wasPanelOpenRef.current = panelOpen
    if (open && was && !panelOpen) controls?.close?.()
  }, [open, panelOpen, controls])

  return {
    historySearchOpen: open,
    // The tab body stays mounted behind another tab or a hidden panel; only
    // while it is on screen does the search own Escape.
    historySearchOnScreen: open && panelShown && activeTabId === CREW_HISTORY_SEARCH_TAB_ID,
    historySearchHost: host,
    setHistorySearchHost: setHost,
    onHistorySearchOpenChange: onOpenChange,
    openHistorySearch: controls?.open,
    closeHistorySearch: controls?.close,
    setHistorySearchOpener,
  }
}

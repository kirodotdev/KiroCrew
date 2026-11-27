/**
 * The session chat's history search as an on-demand tab of the chat's side
 * panel, on desktop and on a phone (where the panel is a full-width overlay).
 *
 * `useMessageSearch` owns the search: ⌘F, the session title menu's Search row
 * and the panel's + menu open it; Escape, Cancel, the tab's close button and
 * ⌘F in its focused field end it. This hook mirrors that state into the strip.
 * Each open, and each ⌘F while the search is open, opens the panel and puts
 * the Search tab in front. When the search ends with its tab in front, a panel
 * the search opened closes again (on a phone the overlay always closes), and a
 * panel that was already open goes back to the tab that was in front before.
 * Closing the panel ends the search, so a later open never shows the tab.
 * The bot chat's twin is `pages/members/useCrewHistorySearchTab.ts`.
 */
import { useEffect, useRef } from 'react'
import { useAppDispatch } from '../../store'
import { closeActivityPanel, openActivityPanel } from '../../store/chatSlice'

/** The Search tab's id in the side panel strip. Not a stored tab: the strip
 *  holds it only as a focus while the search is open, and never persists it. */
export const CHAT_HISTORY_SEARCH_TAB_ID = 'chat-history-search'
/** The strip's `keepFocusIds`, so a pinned-view sync keeps the Search tab in front. */
export const CHAT_HISTORY_SEARCH_TAB_IDS: readonly string[] = [CHAT_HISTORY_SEARCH_TAB_ID]

interface Options {
  search: { isOpen: boolean; focusNonce: number; close: () => void }
  isMobile: boolean
  activityOpen: boolean
  tabsCtl: { tabs: readonly { id: string }[]; activeId: string | null; setActive: (id: string | null) => void }
}

/** Decided when the search opens. */
interface OpenSearch {
  /** The tab in front before the search, focused again when it ends. */
  returnTo: string | null
  /** The search opened the panel, so ending it closes the panel too. */
  closePanel: boolean
}

export function useChatHistorySearchTab({ search, isMobile, activityOpen, tabsCtl }: Options) {
  const dispatch = useAppDispatch()
  const tabOpen = search.isOpen
  const { activeId } = tabsCtl
  const openRef = useRef<OpenSearch | null>(null)
  const activeRef = useRef(activeId)
  activeRef.current = activeId
  const firstTabRef = useRef<string | null>(null)
  firstTabRef.current = tabsCtl.tabs[0]?.id ?? null
  const stateRef = useRef({ activityOpen, isMobile })
  stateRef.current = { activityOpen, isMobile }
  // Read through a ref: `setActive` is bound to the active chat's strip and
  // changes with it, and a chat switch must neither focus the Search tab in
  // the next chat's strip nor open the panel there.
  const setActiveRef = useRef(tabsCtl.setActive)
  setActiveRef.current = tabsCtl.setActive

  useEffect(() => {
    if (!tabOpen) return
    const { activityOpen: panelOpen, isMobile: phone } = stateRef.current
    if (!openRef.current) openRef.current = { returnTo: null, closePanel: phone || !panelOpen }
    if (activeRef.current !== CHAT_HISTORY_SEARCH_TAB_ID) openRef.current.returnTo = activeRef.current
    setActiveRef.current(CHAT_HISTORY_SEARCH_TAB_ID)
    dispatch(openActivityPanel())
  }, [tabOpen, search.focusNonce, dispatch])

  // The search ended. With its tab in front it hands the strip back; a tab
  // another action put in front (a file opened from the transcript) stays.
  // With no search behind it (a reload or a remount restored the focus), only
  // the focus goes back to a real tab.
  useEffect(() => {
    if (tabOpen) return
    const ended = openRef.current
    openRef.current = null
    if (activeId !== CHAT_HISTORY_SEARCH_TAB_ID) return
    setActiveRef.current(ended?.returnTo ?? firstTabRef.current)
    if (ended?.closePanel) dispatch(closeActivityPanel())
  }, [tabOpen, activeId, dispatch])

  // Closing the panel ends the search, so opening the panel again never
  // shows a Search tab left behind.
  const { close } = search
  const wasPanelOpenRef = useRef(activityOpen)
  useEffect(() => {
    const was = wasPanelOpenRef.current
    wasPanelOpenRef.current = activityOpen
    if (tabOpen && was && !activityOpen) close()
  }, [tabOpen, activityOpen, close])

  return {
    /** The strip carries the Search tab. */
    tabOpen,
    /** The Search tab is in front of a shown panel: only then does the search own Escape. */
    onScreen: tabOpen && activityOpen && activeId === CHAT_HISTORY_SEARCH_TAB_ID,
  }
}

import type { MotionValue } from 'motion-dom'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, type MutableRefObject } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { Search } from 'lucide-react'
import type { ChatPanelTrackpadSwipe } from './chatTrackpadSwipe'
import type { ChatPin } from '../../../api/pins'
import type { ChatNavigationData } from '../../../hooks/useChatNavigation'
import type { SidePanelDock } from '../../../hooks/useSidePanelDock'
import type { ChatSlot } from '../../../types'
import type { PullRequestLink } from '../../../utils/pullRequestLinks'
import { shouldMountSidePanel, isSidePanelHidden, SIDE_PANEL_DOCK_TRANSITION, type SidePanelDockMotion } from '../sidePanelMount'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import type { PreviewActions } from '../../../components/chatHistorySearch/ChatHistoryPreview'
import SidePanel, { type SidePanelLeadingTab } from '../SidePanel'
import { lazyRetry } from '../../../components/lazyRetry'
import { i18nT } from '../../../i18n/t'
import { coversPaneProps } from '../../../hooks/paneVisibility'
import { useSidePanelHealth } from '../../../hooks/useSidePanelHealth'
import { historySearchHostMarks } from '../../../hooks/useMessageSearch'
import { CHAT_HISTORY_SEARCH_TAB_ID, useChatHistorySearchTab } from '../useChatHistorySearchTab'
import { useAppDispatch } from '../../../store'
import { closeActivityPanel } from '../../../store/chatSlice'
import { setSidePanelMounted } from '../../../hooks/sidePanelMounted'

// A chunk that fails to load shows its notice in the Search tab, with Try Again.
// No hand-off: the chat composer beside the pane may hold an unsent draft.
const ChatHistorySearch = lazyRetry(() => import('../../../components/chatHistorySearch/ChatHistorySearch'), {
  layout: 'page',
  title: () => i18nT('components.chatHistorySearch.load_failed'),
  scope: 'chat-history-search',
  askAgent: false,
})

interface ChatSidePanelsProps {
  mode?: string
  activeSlot: string | null
  connected: boolean
  isMobile: boolean
  activityOpen: boolean
  insertDashboardReply: (text: string) => void
  addSourceCommentToChat: (text: string) => void
  handleArtifactOpen: (slug: string) => Promise<void>
  handleFileOpen: ReturnType<typeof useChatPageResourcesController>['handleFileOpen']
  handleFileSave: (filePath: string, content: string) => Promise<void>
  handleOpenWorkingTreeDiff: (filePath: string, opts?: { slot?: string | null; }) => Promise<void>
  hasBrowserTab: boolean
  hasLiveAppTab: boolean
  panelIssues: PullRequestLink[]
  panelSources: PullRequestLink[]
  dismissSource: (url: string) => void
  reconcileIssueUrl: (url: string) => void
  reconcileSourceUrl: (url: string) => void
  search: ReturnType<typeof useChatPageResourcesController>['search']
  selectedIssueUrl: string
  selectedSourceUrl: string
  selectIssueUrl: (url: string) => void
  selectSourceUrl: (url: string) => void
  tabsCtl: ReturnType<typeof useChatPageResourcesController>['tabsCtl']
  submitComments: (message: string) => false | Promise<boolean>
  currentSlot: ChatSlot | undefined
  handleAddToContext: (absPath: string, kind: 'file' | 'dir', dropAt?: number | null) => void
  sidePanelDock: SidePanelDock
  sidePanelDockAnim: SidePanelDockMotion
  effectiveSidebarWidth: number
  toggleAct: () => void
  chatNav: ChatNavigationData
  chatPins: ChatPin[]
  chatPinsLoading: boolean
  handleJumpToPin: (messageTs: string, mid?: string) => void
  /** Absent where this chat's composer cannot take it (a remote relay archive, Split View, a crew window): no Put in composer button. */
  handlePutPinInComposer?: (messageTs: string, mid?: string) => boolean
  handleUnpinById: (id: string) => void
  /** Quote and fork for the messages the history search previews. */
  historyActions: PreviewActions
  activeSlotTitle: string | undefined
  activitySlot: HTMLElement | null
  /** The panel is mounted inline, in the page's own row, rather than in the
   *  shell's column (panelInlineHost, lib/touchLayoutForm.ts). A fold never
   *  changes it, so folding never remounts the panel or its frames. */
  panelInline: boolean
  /** Look the shell's `#activity-bar-slot` up again (sessionsDrawer). */
  refreshActivitySlot?: () => void
  /** An embedded chat or a popout: no shell column, no health check. */
  embedded?: boolean
  /** The chat pane, to tell a hidden panel from a hidden page. */
  chatPaneEl?: Element | null
  hasTaskDashboard: boolean
  sidebarOpen: boolean
  sideOverlayPanelRef: MutableRefObject<HTMLDivElement | null>
  sideOverlayPhase: 'open' | 'closed' | 'closing'
  sideOverlayX: MotionValue<number>
  panelFillWidth: number | undefined
  panelMaximized: boolean
  pinSummary: boolean
  trackpad: ChatPanelTrackpadSwipe
}

/** No tween: a target's own `transition` outranks the element's. */
const INSTANT = { duration: 0 }
/** The column during a trackpad drag: its width is the spacer's (the panel is
 *  out of flow), and it leaves at once, already at the width it closed to. */
const DESK_SLIDE_MOTION = {
  animate: { width: 'auto', height: '100%', opacity: 1, transition: INSTANT },
  exit: { opacity: 1, transition: INSTANT },
}

/**
 * The panels beside the chat pane: the inline side panel (phone overlay or embed width reveal), and the side panel portaled into the shell's activity column.
 */
export default function ChatSidePanels({
  mode, activeSlot, connected, isMobile, activityOpen, insertDashboardReply, addSourceCommentToChat, handleArtifactOpen, handleFileOpen, handleFileSave,
  handleOpenWorkingTreeDiff, hasBrowserTab, hasLiveAppTab, panelIssues, panelSources, dismissSource, reconcileIssueUrl, reconcileSourceUrl, search, selectedIssueUrl, selectedSourceUrl, selectIssueUrl,
  selectSourceUrl, tabsCtl, submitComments, currentSlot, handleAddToContext, sidePanelDock, sidePanelDockAnim, effectiveSidebarWidth, toggleAct, chatNav, chatPins, chatPinsLoading, handleJumpToPin, handlePutPinInComposer,
  handleUnpinById, historyActions, activeSlotTitle, activitySlot, panelInline, refreshActivitySlot, embedded, chatPaneEl, hasTaskDashboard, sidebarOpen, sideOverlayPanelRef, sideOverlayPhase, sideOverlayX, panelFillWidth, panelMaximized,
  pinSummary, trackpad,
}: ChatSidePanelsProps) {
  const { deskSlide, deskSpacerW, deskPanelWrapRef } = trackpad
  // The render that hands a settled drag back to the column must not run the
  // column's width reveal: the column already stands at the panel's width.
  const wasDeskSliding = useRef(false)
  const instantPlacement = deskSlide || wasDeskSliding.current
  useEffect(() => { wasDeskSliding.current = deskSlide })
  const dispatch = useAppDispatch()
  const activeTab = tabsCtl.tabs.find(t => t.id === tabsCtl.activeId)
  const browserBehind = hasBrowserTab && activeTab?.kind !== 'browser'
  const hideNative = useCallback(() => { if (activeSlot) void window.browserAPI?.setInactive(activeSlot, true) }, [activeSlot])
  // A panel that fills the chat column (a phone, or a narrow window) hides the
  // composer it just filled, so it closes; docked at the bottom it does not.
  const putPinInComposer = useCallback((messageTs: string, mid?: string) => {
    const done = handlePutPinInComposer?.(messageTs, mid) ?? false
    if (panelFillWidth !== undefined && !(activitySlot && sidePanelDock === 'bottom')) dispatch(closeActivityPanel())
    return done
  }, [handlePutPinInComposer, panelFillWidth, activitySlot, sidePanelDock, dispatch])
  const { retireSlide } = trackpad
  const mountInput = { activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard }
  const historyTab = useChatHistorySearchTab({ search, isMobile, activityOpen, tabsCtl })
  const { close: closeSearch, focusNonce } = search
  // The chat's history search as the panel's leading tab, only while it is
  // open: in the docked panel, or the full-width overlay on a phone. Kept
  // mounted, so a switch to another tab and back keeps the query and the
  // preview; the body owns Escape only while it is in front.
  const leadingTabs = useMemo<SidePanelLeadingTab[] | undefined>(() => (historyTab.tabOpen && activeSlot ? [{
    id: CHAT_HISTORY_SEARCH_TAB_ID,
    title: i18nT('components.chatHistorySearch.title'),
    icon: <Search className="lucide-inline" aria-hidden="true" />,
    keepMounted: true,
    // A phone shows the search full screen with its own Cancel: no tab strip.
    bare: isMobile,
    onClose: closeSearch,
    render: () => (
      <div {...historySearchHostMarks(historyTab.onScreen)} className="h-full min-h-0 flex flex-col" data-testid="chat-history-search-tab">
        <ChatHistorySearch key={activeSlot} slotKey={activeSlot} onClose={closeSearch} focusNonce={focusNonce} escapeOwner={false} {...historyActions} />
      </div>
    ),
  }] : undefined), [historyTab.tabOpen, historyTab.onScreen, activeSlot, closeSearch, focusNonce, historyActions, isMobile])
  // The store says open, the panel must be on screen (useSidePanelHealth).
  useSidePanelHealth({
    enabled: activityOpen && !panelInline && !embedded,
    activeSlot,
    dock: sidePanelDock,
    slot: activitySlot,
    chatPane: () => chatPaneEl ?? null,
    deskSlide,
    nativeSuspect: () => !!window.browserAPI && browserBehind,
    heal: {
      refreshSlot: () => refreshActivitySlot?.(),
      retireSlide,
      hideNative,
      close: () => dispatch(closeActivityPanel()),
    },
  })
  // Whether a SidePanel is mounted, in the shell's column when it has one,
  // else inline. Both hosts render from this one value, and it is published
  // for recordUnfoldedForm (hooks/sidePanelMounted.ts), so the fact and the
  // render cannot drift. An embedded chat or a popout does not publish: one
  // writer, the page's own. It never sits on the chat route, where alone the
  // fold two-pane form applies, so a no-op wake on its own route re-records
  // the same unfolded form and its panel has no hold to keep.
  const sidePanelMounted = activitySlot
    ? shouldMountSidePanel(mountInput) || deskSlide
    : isMobile
      // `hasLiveAppTab` keeps a closed-but-alive panel MOUNTED (display:none
      // below) so its iframe — and the drawing inside it — survives the
      // close, exactly as the width-reveal branch always did.
      ? sideOverlayPhase !== 'closed' || (shouldMountSidePanel(mountInput) && isSidePanelHidden(mountInput))
      : shouldMountSidePanel(mountInput)
  useLayoutEffect(() => {
    if (embedded) return
    setSidePanelMounted(sidePanelMounted)
    return () => setSidePanelMounted(false)
  }, [embedded, sidePanelMounted])
  // A narrow desktop window where the panel fills the chat column needs no mark:
  // it squeezes the transcript to zero width, which the on-screen backstop sees
  // (paneVisibility, #23564), and a bottom-docked panel leaves it on screen.
  return (
    <>
      <AnimatePresence initial={false}>
        {/* Inline side panel — mobile / embed frames where there's no actbar
            grid column. Desktop uses the actbar portal below.

            MOBILE is a fixed overlay sliding in from the RIGHT on the
            compositor (sideOverlayX / animateDrawer), mirroring the sessions
            drawer: the old `width: 0 → auto` reveal is a LAYOUT animation, so
            it re-laid-out the panel AND the squeezed chat pane every frame.
            Mount is held through 'closing' so the slide-out is not cut short.
            Embed frames keep the width reveal — they have no overlay chrome. */}
        {(sidePanelMounted && !activitySlot) && (
          <motion.div
            key="side-panel-inline"
            data-testid="side-panel-inline"
            ref={isMobile ? sideOverlayPanelRef : undefined}
            initial={isMobile ? false : { width: 0 }}
            animate={isMobile ? undefined : { width: 'auto' }}
            exit={isMobile ? undefined : { width: 0 }}
            transition={isMobile ? undefined : { duration: 0.4, ease: [0.32, 0.72, 0, 1] }}
            // Below the 42px app topbar, like the sessions drawer — the panel
            // takes over the CONTENT area, not the shell chrome. The history
            // search in front is the one exception: it covers the topbar too,
            // so only its own field and Cancel are at the top of the screen.
            className={isMobile
              ? historyTab.onScreen
                ? 'fixed top-safe bottom-safe left-safe right-safe z-[60] flex justify-end bg-bg'
                : 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-[47] flex justify-end bg-bg'
              : 'h-full overflow-hidden flex justify-end shrink-0'}
            // On a phone this is an opaque overlay over the whole content area,
            // so while OPEN the transcript under it is not on screen
            // (paneVisibility, #23564); not while sliding out, and the desktop
            // panel sits beside the transcript instead.
            {...(isMobile && sideOverlayPhase === 'open' ? coversPaneProps : {})}
            // Kept mounted for a live app tab: hide instead of unmounting so the
            // iframe (and the drawing inside it) survives a panel close. On
            // mobile the overlay phase already owns mount/hide timing, and the
            // offset is BOUND (not a one-shot read): a drag writes the value
            // directly, so only a live binding paints those frames — the
            // compositor settle still runs through the registered element above.
            style={isMobile
              ? (sideOverlayPhase === 'closed'
                  ? { display: 'none' } // keep-alive: mounted for the iframe, invisible
                  : { x: sideOverlayX })
              : (isSidePanelHidden(mountInput) ? { display: 'none' } : undefined)}
          >
            <SidePanel
              tabsCtl={tabsCtl}
              slot={activeSlot || ''}
              panelHidden={isSidePanelHidden(mountInput)}
              onFileOpen={handleFileOpen}
              onArtifactOpen={handleArtifactOpen}
              onAddToContext={handleAddToContext} onComposerInsert={insertDashboardReply}
              onOpenWorkingTreeDiff={handleOpenWorkingTreeDiff}
              projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
              sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl} onDismissSource={dismissSource}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
              onSubmitComments={submitComments} connected={connected} onFileSave={handleFileSave} onClose={toggleAct}
              pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPin} onPutPinInComposer={handlePutPinInComposer && putPinInComposer} onUnpin={handleUnpinById}
              slotTitle={activeSlotTitle} chatMode={mode} pinSummary={pinSummary}
              expanded={panelMaximized}
              fillWidth={panelFillWidth}
              extraReserveW={!isMobile && sidebarOpen ? effectiveSidebarWidth : 0}
              canDockBottom={false}
              onOpenHistorySearch={activeSlot ? search.open : undefined}
              leadingTabs={leadingTabs}
            />
          </motion.div>
        )}
      </AnimatePresence>
      {/* Full-height tabbed side panel: portaled into the App shell's
          'actbar' grid column so it spans the window top-to-bottom; the header
          row ends at its left edge, shifting the top-bar buttons left.
          The motion wrapper animates the column width 0 -> auto: the actbar
          grid column tracks it frame-by-frame, so the chat pane slides left in
          sync while the panel (right-anchored via justify-end) slides out from
          the window edge — both sides move together instead of snapping. */}
      {activitySlot && createPortal(
        <AnimatePresence initial={false}>
          {sidePanelMounted && (
            <motion.div
              key="side-panel"
              ref={deskPanelWrapRef}
              // A trackpad drag (useChatPanelTrackpadSwipe): the column is a
              // window onto the panel as wide as its visible part, the panel
              // pinned at its left edge, so the panel's edge and the chat's
              // follow the fingers. Swaps in and out with no reveal of its own.
              initial={deskSlide ? false : sidePanelDockAnim.initial}
              animate={deskSlide ? DESK_SLIDE_MOTION.animate : instantPlacement ? { ...sidePanelDockAnim.animate, transition: INSTANT } : sidePanelDockAnim.animate}
              exit={deskSlide ? DESK_SLIDE_MOTION.exit : sidePanelDockAnim.exit}
              transition={SIDE_PANEL_DOCK_TRANSITION}
              className={deskSlide
                // While it slides the panel keeps its size and only moves: its own
                // layer (`will-change`) and containment let the browser move it
                // without repainting or re-laying out its content each frame.
                ? 'h-full relative overflow-hidden flex [&>:first-child]:absolute [&>:first-child]:inset-y-0 [&>:first-child]:left-0 [&>:first-child]:will-change-transform [&>:first-child]:[contain:layout_paint]'
                : sidePanelDock === 'bottom' ? 'w-full overflow-visible flex flex-col justify-end' : 'h-full overflow-visible flex justify-end'}
              style={!deskSlide && isSidePanelHidden(mountInput) ? { display: 'none' } : undefined}
              data-testid="side-panel-column"
              data-sliding={deskSlide ? 'true' : undefined}
              // A native Browser view (useNativeBrowser) is a layer over the
              // panel's last reported rect, and a slide moves the panel without
              // resizing it: hide the view (it is kept, not destroyed) for the
              // whole drag and settle; docking shows it at the new rect.
              data-native-overlay={deskSlide ? '' : undefined}
            >
              <SidePanel
                tabsCtl={tabsCtl}
                slot={activeSlot || ''}
                panelHidden={isSidePanelHidden(mountInput) && !deskSlide}
                onFileOpen={handleFileOpen}
                onArtifactOpen={handleArtifactOpen}
                onAddToContext={handleAddToContext} onComposerInsert={insertDashboardReply}
                onOpenWorkingTreeDiff={handleOpenWorkingTreeDiff}
                projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
                sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl} onDismissSource={dismissSource}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
                onSubmitComments={submitComments} connected={connected} onFileSave={handleFileSave} onClose={toggleAct}
                pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPin} onPutPinInComposer={handlePutPinInComposer && putPinInComposer} onUnpin={handleUnpinById}
                slotTitle={activeSlotTitle} chatMode={mode} pinSummary={pinSummary}
                expanded={panelMaximized}
                fillWidth={panelFillWidth}
                extraReserveW={!isMobile && sidebarOpen ? effectiveSidebarWidth : 0}
                onOpenHistorySearch={activeSlot ? search.open : undefined}
                leadingTabs={leadingTabs}
              />
              {/* After the panel, so the panel keeps its place and never remounts. */}
              {deskSlide && <motion.div data-testid="side-panel-swipe-spacer" className="h-full shrink-0" style={{ width: deskSpacerW }} />}
            </motion.div>
          )}
        </AnimatePresence>,
        activitySlot
      )}
    </>
  )
}

import type { MotionValue } from 'motion-dom'
import type { MutableRefObject } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import type { ChatPin } from '../../../api/pins'
import type { ChatNavigationData } from '../../../hooks/useChatNavigation'
import type { SidePanelDock } from '../../../hooks/useSidePanelDock'
import type { ChatMessage, ChatSlot } from '../../../types'
import type { PullRequestLink } from '../../../utils/pullRequestLinks'
import { shouldMountSidePanel, isSidePanelHidden, SIDE_PANEL_DOCK_TRANSITION, type SidePanelDockMotion } from '../sidePanelMount'
import { searchScopeIsLimited } from '../pagination'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import SearchBar from '../../../components/SearchBar'
import SearchResultsList from '../../../components/SearchResultsList'
import SidePanel from '../SidePanel'
import DetailPanel from '../../../components/DetailPanel'
import { i18nT } from '../../../i18n/t'

interface ChatSidePanelsProps {
  mode?: string
  activeSlot: string | null
  connected: boolean
  isMobile: boolean
  activityOpen: boolean
  cursorIsForActiveSlot: boolean
  messages: ChatMessage[]
  slotHasMore: boolean
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
  handleUnpinById: (id: string) => void
  jumpToSearchResult: (i: number) => void
  activeSlotTitle: string | undefined
  activitySlot: HTMLElement | null
  hasTaskDashboard: boolean
  sidebarOpen: boolean
  sideOverlayPanelRef: MutableRefObject<HTMLDivElement | null>
  sideOverlayPhase: 'open' | 'closed' | 'closing'
  sideOverlayX: MotionValue<number>
  panelFillWidth: number | undefined
  panelMaximized: boolean
  panelReserve: number | undefined
  pinSummary: boolean
}

/**
 * The panels beside the chat pane: the find pane, the inline side panel (phone overlay or embed width reveal), and the side panel portaled into the shell's activity column.
 */
export default function ChatSidePanels({
  mode, activeSlot, connected, isMobile, activityOpen, cursorIsForActiveSlot, messages, slotHasMore, insertDashboardReply, addSourceCommentToChat, handleArtifactOpen, handleFileOpen, handleFileSave,
  handleOpenWorkingTreeDiff, hasBrowserTab, hasLiveAppTab, panelIssues, panelSources, reconcileIssueUrl, reconcileSourceUrl, search, selectedIssueUrl, selectedSourceUrl, selectIssueUrl,
  selectSourceUrl, tabsCtl, submitComments, currentSlot, handleAddToContext, sidePanelDock, sidePanelDockAnim, effectiveSidebarWidth, toggleAct, chatNav, chatPins, chatPinsLoading, handleJumpToPin,
  handleUnpinById, jumpToSearchResult, activeSlotTitle, activitySlot, hasTaskDashboard, sidebarOpen, sideOverlayPanelRef, sideOverlayPhase, sideOverlayX, panelFillWidth, panelMaximized, panelReserve,
  pinSummary,
}: ChatSidePanelsProps) {
  return (
    <>
      {search.isOpen && (
          <DetailPanel
            key="search-panel"
            title={<SearchBar docked term={search.term} setTerm={search.setTerm} matches={search.matches} currentIdx={search.currentIdx} next={search.next} prev={search.prev} close={search.close} caseSensitive={search.caseSensitive} toggleCaseSensitive={search.toggleCaseSensitive} focusNonce={search.focusNonce} goTo={search.goTo} scopeLimited={searchScopeIsLimited({ slotHasMore, cursorIsForActiveSlot })} />}
            onClose={search.close}
            initialWidth={400}
            minWidth={320}
            reserveWidth={panelReserve}
            storageKey="mc-search-width"
            noPadding
          >
            {search.matches.length > 0 ? (
              <SearchResultsList
                matches={search.matches}
                currentIdx={search.currentIdx}
                messages={messages}
                term={search.term}
                caseSensitive={search.caseSensitive}
                onJump={jumpToSearchResult}
              />
            ) : (
              <div className="px-4 py-3 text-[13px] text-muted">{search.term ? i18nT('pages.chatPage.no_results') : i18nT('pages.chatPage.type_to_search_this_conversation')}</div>
            )}
          </DetailPanel>
        )}
      <AnimatePresence initial={false}>
        {/* Inline side panel — mobile / embed frames where there's no actbar
            grid column. Desktop uses the actbar portal below.

            MOBILE is a fixed overlay sliding in from the RIGHT on the
            compositor (sideOverlayX / animateDrawer), mirroring the sessions
            drawer: the old `width: 0 → auto` reveal is a LAYOUT animation, so
            it re-laid-out the panel AND the squeezed chat pane every frame.
            Mount is held through 'closing' so the slide-out is not cut short.
            Embed frames keep the width reveal — they have no overlay chrome. */}
        {(isMobile
          // `hasLiveAppTab` keeps a closed-but-alive panel MOUNTED (display:none
          // below) so its iframe — and the drawing inside it — survives the
          // close, exactly as the width-reveal branch always did.
          ? (sideOverlayPhase !== 'closed'
              || (shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
                  && isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }))) && !activitySlot
          : shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) && !activitySlot) && (
          <motion.div
            key="side-panel-inline"
            ref={isMobile ? sideOverlayPanelRef : undefined}
            initial={isMobile ? false : { width: 0 }}
            animate={isMobile ? undefined : { width: 'auto' }}
            exit={isMobile ? undefined : { width: 0 }}
            transition={isMobile ? undefined : { duration: 0.4, ease: [0.32, 0.72, 0, 1] }}
            // Below the 42px app topbar, like the sessions drawer — the panel
            // takes over the CONTENT area, not the shell chrome.
            className={isMobile
              ? 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-[47] flex justify-end bg-bg'
              : 'h-full overflow-hidden flex justify-end shrink-0'}
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
              : (isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) ? { display: 'none' } : undefined)}
          >
            <SidePanel
              tabsCtl={tabsCtl}
              slot={activeSlot || ''}
              panelHidden={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })}
              onFileOpen={handleFileOpen}
              onArtifactOpen={handleArtifactOpen}
              onAddToContext={handleAddToContext} onComposerInsert={insertDashboardReply}
              onOpenWorkingTreeDiff={handleOpenWorkingTreeDiff}
              projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
              sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
              onSubmitComments={submitComments} connected={connected} onFileSave={handleFileSave} onClose={toggleAct}
              pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPin} onUnpin={handleUnpinById}
              slotTitle={activeSlotTitle} chatMode={mode} pinSummary={pinSummary}
              expanded={panelMaximized}
              fillWidth={panelFillWidth}
              extraReserveW={!isMobile && sidebarOpen ? effectiveSidebarWidth : 0}
              canDockBottom={false}
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
          {shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) && (
            <motion.div
              key="side-panel"
              initial={sidePanelDockAnim.initial}
              animate={sidePanelDockAnim.animate}
              exit={sidePanelDockAnim.exit}
              transition={SIDE_PANEL_DOCK_TRANSITION}
              className={sidePanelDock === 'bottom' ? 'w-full overflow-visible flex flex-col justify-end' : 'h-full overflow-visible flex justify-end'}
              style={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) ? { display: 'none' } : undefined}
            >
              <SidePanel
                tabsCtl={tabsCtl}
                slot={activeSlot || ''}
                panelHidden={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })}
                onFileOpen={handleFileOpen}
                onArtifactOpen={handleArtifactOpen}
                onAddToContext={handleAddToContext} onComposerInsert={insertDashboardReply}
                onOpenWorkingTreeDiff={handleOpenWorkingTreeDiff}
                projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
                sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
                onSubmitComments={submitComments} connected={connected} onFileSave={handleFileSave} onClose={toggleAct}
                pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPin} onUnpin={handleUnpinById}
                slotTitle={activeSlotTitle} chatMode={mode} pinSummary={pinSummary}
                expanded={panelMaximized}
                fillWidth={panelFillWidth}
                extraReserveW={!isMobile && sidebarOpen ? effectiveSidebarWidth : 0}
              />
            </motion.div>
          )}
        </AnimatePresence>,
        activitySlot
      )}
    </>
  )
}

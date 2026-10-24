import type { MotionValue } from 'motion-dom'
import type { Dispatch, SetStateAction, MutableRefObject, RefObject } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import type { KiroCrewAgent } from '../../../components/AgentSelector'
import type { MobileNavRailRenderer } from '../../../components/MobileNavRailContext'
import type { HoverIntent } from '../../../hooks/useHoverIntent'
import type { VisualViewport } from '../../../hooks/useVisualViewport'
import type { AppDispatch } from '../../../store'
import type { ChatSlot, SessionInfo } from '../../../types'
import type { ErrorReport } from '../../../utils/errorReport'
import type { SourceLinkKind } from '../../../utils/pullRequestLinks'
import type { SessionRef } from '../../../utils/sessionRefs'
import type { useSteerDelivery } from './turnActions'
import { DRAWER_UNCOVERED_PX } from './sessionsDrawer'
import MobileTopBar from './MobileTopBar'
import ChatSidebar from '../../ChatSidebar'
import { SIDEBAR_MAX } from '../sidebarWidth'
import OverlayDrawer from '../../../components/OverlayDrawer'
import SessionFlyout, { TOGGLE_RECT } from '../SessionFlyout'
import { PanelLeftLight, PanelLeftSolid } from '../../../components/icons/panels'
import SlotTagPopover from '../../../components/SlotTagPopover'
import { i18nT } from '../../../i18n/t'
import { uiLocation } from '../../../uiLocations/uiLocation'
import { GuideRevealScope } from '../../../guide/GuideRevealScope'

interface SessionsSidebarChromeProps {
  embedMode?: 'chat' | 'sessions'
  mode?: string
  activeSlot: string | null
  connected: boolean
  creatingSlot: boolean
  dispatch: AppDispatch
  filteredSlots: ChatSlot[]
  isMobile: boolean
  keyboardInset: number
  mobileNavRail: MobileNavRailRenderer | null
  surfaceUnreadSlots: string[]
  titleInTopbar: boolean
  topbarSlot: HTMLElement | null
  topbarTrailSlot: HTMLElement | null
  vv: VisualViewport
  activityOpen: boolean
  /** ChatSidebar's `onSelectSlot`: leaves split view and, with "Open Sidebar
   *  Sessions in a Session Tab" on, gives the session its own tab. */
  selectFromSidebar: (key: string) => void
  history: SessionInfo[]
  historyHasMore: boolean
  navigateToEmbeddedSlot: (key: string) => void | Promise<void>
  openEmbeddedCrewWindow: (instanceId: string, key: string) => void | Promise<void>
  splitMode: boolean
  defaultAgent: string
  installedAgents: KiroCrewAgent[]
  sidebarAgents: KiroCrewAgent[]
  /** The picker's unfolded rows, for the session list's same-name memory mark. */
  sidebarChoices: KiroCrewAgent[]
  setActionError: ReturnType<typeof useSteerDelivery>['setActionError']
  showActionError: (message: string, title?: string, report?: ErrorReport) => void
  canStageSessionRef: boolean
  chatPaneEl: HTMLDivElement | null
  stageSessionRef: (ref: SessionRef) => void
  openSlotInNewTab: (key: string, opts?: { background?: boolean; requestSeq?: number }) => void
  ownsSessionTabs: boolean
  activePoppedOut: boolean
  focusActivePopout: (id: string) => void
  openActivePopout: (id: string, title?: string) => void
  splitFeatureEnabled: boolean
  currentSlot: ChatSlot | undefined
  effectiveMode: string | undefined
  title: string
  activeIsSplitAnchor: boolean
  editingTitle: boolean
  enterSplit: (anchor: string | null) => void
  setEditingTitleSlot: Dispatch<SetStateAction<string | null>>
  setSidebarDragging: Dispatch<SetStateAction<boolean>>
  setSidebarWidth: Dispatch<SetStateAction<number>>
  sidebarAutoHidden: MutableRefObject<boolean | null>
  sidebarDragging: boolean
  splitAnchorForActive: string | null
  effectiveSidebarWidth: number
  toggleAct: () => void
  winW: number
  closeSidebar: () => void
  containerH: number
  drawerDragging: boolean
  drawerMounted: boolean
  drawerPanelRef: MutableRefObject<HTMLDivElement | null>
  drawerScrim: MotionValue<number>
  drawerScrimRef: MutableRefObject<HTMLDivElement | null>
  drawerX: MotionValue<number>
  expandFrom: { x: number; y: number; w: number; h: number; } | null
  expandSidebar: (fromFlyout: boolean) => void
  flyout: HoverIntent
  flyoutEligible: boolean
  flyoutSurfaceRef: RefObject<HTMLDivElement>
  flyoutTriggerRef: RefObject<HTMLButtonElement>
  openSidebar: () => void
  revealSourceLink: (slot: string, chip: { url: string; kind: SourceLinkKind; }) => boolean
  sidebarOnScreen: boolean
  sidebarOpen: boolean
  flyoutSwitch: (key: string) => void
  flyoutNew: () => void
  mobileNewSessionItem: { label: string; disabled: boolean; onSelect: () => void; }
  topbarSessionsToggle: JSX.Element
}

/**
 * The sessions chrome around the chat pane: the phone drawer and its scrim, the desktop sidebar toggle and its recents flyout, the sidebar or drawer itself (or the embedded sessions list), the phone top bar, and the tag popover.
 */
export default function SessionsSidebarChrome({
  embedMode, mode, activeSlot, connected, creatingSlot, dispatch, filteredSlots, isMobile, keyboardInset, mobileNavRail, surfaceUnreadSlots, titleInTopbar, topbarSlot, topbarTrailSlot, vv,
  activityOpen, selectFromSidebar, history, historyHasMore, navigateToEmbeddedSlot, openEmbeddedCrewWindow, splitMode, defaultAgent, installedAgents, sidebarAgents, sidebarChoices, setActionError, showActionError,
  canStageSessionRef, chatPaneEl, stageSessionRef, openSlotInNewTab, ownsSessionTabs, activePoppedOut, focusActivePopout, openActivePopout, splitFeatureEnabled, currentSlot, effectiveMode, title,
  activeIsSplitAnchor, editingTitle, enterSplit, setEditingTitleSlot, setSidebarDragging, setSidebarWidth, sidebarAutoHidden, sidebarDragging, splitAnchorForActive, effectiveSidebarWidth, toggleAct,
  winW, closeSidebar, containerH, drawerDragging, drawerMounted, drawerPanelRef, drawerScrim, drawerScrimRef, drawerX, expandFrom, expandSidebar, flyout, flyoutEligible, flyoutSurfaceRef,
  flyoutTriggerRef, openSidebar, revealSourceLink, sidebarOnScreen, sidebarOpen, flyoutSwitch, flyoutNew, mobileNewSessionItem, topbarSessionsToggle,
}: SessionsSidebarChromeProps) {
  return (
    <>
      <AnimatePresence>
        {isMobile && drawerMounted && (
          <motion.div
            key="sessions-backdrop"
            data-testid="sessions-backdrop"
            className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-xs"
            // ^ Frosted, matching every other scrim in the app (navRail.tsx's own
            // mobile nav backdrop is the same three classes). Kept adjacent to
            // `key` — the composer-chrome occlusion guard anchors its z-order
            // regex on that proximity. A full-viewport `backdrop-filter` does
            // re-sample its backdrop on every repaint behind it — which under
            // a streaming message list is every frame — and both alternatives
            // were tried and rejected on how they LOOK: dropping it entirely,
            // and deferring it to the settled state (the blur arriving after
            // the panel had stopped read as a second event).
            ref={drawerScrimRef}
            // The margins inset this fixed box to the VISIBLE band: inset-0 is
            // the whole layout viewport, which a keyboard shrinks on Chromium
            // but not on iOS Safari, so an unmodified scrim keeps its full
            // height there and the drawer's lower half sits behind the keyboard
            // with nothing dimmed under it. Insetting rather than restating the
            // edges is what lets every safe-area class keep owning its own edge,
            // here and on the panel below.
            style={{ opacity: drawerScrim, marginTop: vv.offsetTop, marginBottom: keyboardInset }}
            // Ignored while a drag owns the panel: the release that ends a
            // close gesture lands here as a click, and treating it as a
            // tap-to-dismiss would run a second close over the settle.
            onClick={() => { if (!drawerDragging) closeSidebar() }}
          />
        )}
      </AnimatePresence>
      {/* Sidebar toggle — absolute in the stable container in BOTH states
          (only the icon flips), so collapsing cannot drag it sideways with
          the reflowing content pane. The collapse/expand motion itself is the
          panel deforming into/out of this button's rect (OverlayDrawer morph
          mode, morphTarget below). Desktop, non-embed, with sessions only.
          While collapsed, hovering it opens the recents flyout below; clicking
          hands that flyout's rect to the drawer so the panel grows out of it. */}
      {!isMobile && embedMode !== 'chat' && embedMode !== 'sessions' && filteredSlots.length > 0 && (
        <button
          type="button"
          onClick={() => expandSidebar(flyout.open)}
          {...flyout.triggerProps}
          {...uiLocation('chat.sessions-sidebar-toggle', flyoutTriggerRef)}
          aria-haspopup={flyoutEligible ? 'menu' : undefined}
          aria-expanded={flyoutEligible ? flyout.open : undefined}
          // Geometry mirrored by TOGGLE_RECT (chat/SessionFlyout) — every
          // surface in this interaction grows out of and back into this rect.
          // In split view the grid's top-left pane reserves this column
          // (SessionGridView `leading`); its title row is the same height as
          // the single-chat row, so the toggle keeps this rect there too.
          className="pi-morph absolute top-[9px] left-2 z-[61] w-7 h-7 rounded-md flex items-center justify-center cursor-pointer text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none"
          title={sidebarOpen ? i18nT('pages.chatPage.hide_sessions') : i18nT('pages.chatPage.show_sessions')}
          aria-label={sidebarOpen ? i18nT('pages.chatPage.hide_sessions_sidebar') : i18nT('pages.chatPage.show_sessions_sidebar')}
        >
          {sidebarOpen ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
        </button>
      )}
      <AnimatePresence>
        {flyoutEligible && flyout.open && (
          <SessionFlyout
            key="session-flyout"
            ref={flyoutSurfaceRef}
            slots={filteredSlots}
            activeSlot={activeSlot}
            unreadSlots={surfaceUnreadSlots}
            // The flyout is a session list, which gains nothing past
            // SIDEBAR_MAX; the last reported width may be a board view's.
            panelWidth={Math.min(effectiveSidebarWidth, SIDEBAR_MAX)}
            // The panel's own height (OverlayDrawer carries pb-2), so the
            // flyout can never be taller than the thing it grows into.
            maxHeight={Math.max(0, containerH - 8)}
            connected={connected}
            creating={creatingSlot}
            autoFocus={flyout.openedBy === 'keyboard'}
            onSwitch={flyoutSwitch}
            onNew={flyoutNew}
            onExpand={() => expandSidebar(true)}
            onDismiss={() => { flyout.close(); flyoutTriggerRef.current?.focus() }}
            onMouseEnter={flyout.surfaceProps.onMouseEnter}
            onMouseLeave={flyout.surfaceProps.onMouseLeave}
            onBlur={flyout.surfaceProps.onBlur}
          />
        )}
      </AnimatePresence>
      {embedMode === 'chat' ? null : embedMode === 'sessions' ? (
        <div className="flex-1 min-w-0 h-full overflow-hidden [&_.sidebar-inner]:!w-full [&_.sidebar-inner]:!border-0 [&_.sidebar-inner]:!rounded-none [&_.sidebar-inner]:!shrink [&_.sidebar-inner]:!bg-bg [--folder-row-sticky-bg:var(--bg)] [&_.sidebar-resize-handle]:!hidden">
          <ChatSidebar
            slots={filteredSlots}
            activeSlot={null}
            unreadSlots={surfaceUnreadSlots}
            history={history}
            historyHasMore={historyHasMore}
            defaultAgent={defaultAgent}
            installedAgents={installedAgents}
            rowAgents={sidebarAgents}
            rowChoices={sidebarChoices}
            mode={mode}
            onWidthChange={setSidebarWidth}
            onDragChange={setSidebarDragging}
            fillsHost
            onSelectSlot={navigateToEmbeddedSlot}
            onOpenPeerSession={openEmbeddedCrewWindow}
          />
        </div>
      ) : (
      // The sessions sidebar's collapse owner (desktop) and the phone's
      // sessions drawer owner: a guide's reveal step completes the moment
      // this says open. Outside the drawer, so "closed" is said while the
      // drawer's contents are unmounted.
      <GuideRevealScope id={isMobile ? 'chat.sessions-drawer' : 'chat.sessions-sidebar'} open={sidebarOpen}>
      <OverlayDrawer open={isMobile ? drawerMounted : sidebarOpen} width={isMobile ? Math.max(0, winW - DRAWER_UNCOVERED_PX) : effectiveSidebarWidth} dragging={sidebarDragging} slideX={isMobile ? drawerX : undefined} slideRef={drawerPanelRef}
        // Mobile only: the same visible-band inset the scrim above carries, so the
        // two surfaces move together. Margins rather than restated edges is what
        // matters here: top-safe-offset-[42px] and bottom-safe compose env() insets
        // no script can read, so the panel could not express its own top/height in
        // JS without a calc(env(…)) string. With both edges pinned and height auto
        // the box is over-constrained on the block axis, so the used height is the
        // viewport minus both safe insets AND both margins — the top edge lands at
        // the visible top and the bottom edge just above the keyboard. Both terms
        // are 0 at rest, so the resting panel is unchanged; and where the layout
        // viewport window.innerHeight reports disagrees with the fixed positioning
        // viewport, the bottom margin merely under-insets — today's behaviour.
        //
        // Horizontal channels are untouched: left-safe stays on the className and
        // OverlayDrawer applies width and the x slide AFTER this style, so neither
        // can be overridden from here.
        slideStyle={isMobile ? { marginTop: vv.offsetTop, marginBottom: keyboardInset } : undefined}
        // Top edge: below the shell's bar while this page still draws its own
        // title row under that bar, so the two bars stay visible above the
        // panel; with the row IN the shell's bar (`titleInTopbar`) the panel
        // covers the whole safe-area height like the App nav drawer it
        // replaces on this route -- the rail's brand mark then sits where
        // that drawer's did. Close: scrim tap, swipe, Back -- all unchanged.
        morph={!isMobile} morphTarget={TOGGLE_RECT} expandFrom={expandFrom} contentH={Math.max(0, containerH - 8)} className={isMobile ? `mobile-sessions-overlay fixed ${titleInTopbar ? 'top-safe' : 'top-safe-offset-[42px]'} bottom-safe left-safe z-50 bg-bg-elevated !py-0 rounded-r-xl shadow-lg [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0` : ''}>
        {/* Phone chat page: ONE drawer holds the shell's navigation rail (72px,
            from MobileNavRailContext) beside the sessions pane, Discord-style, so
            the bar above needs no second drawer trigger. The pane keeps its
            `staticRows` content and takes the drawer's remaining width; the
            drawer's own width, slide, scrim and keyboard inset are unchanged.
            Row taps that stay on this page close the drawer (`closeSidebar`);
            rows that leave it `replace` the duplicate history entry
            `pushDrawerEntry` minted, so Back lands on the chat. */}
        {(() => {
        const sessionsPane = (
        <ChatSidebar
          slots={filteredSlots}
          activeSlot={activeSlot}
          unreadSlots={surfaceUnreadSlots}
          history={history}
          historyHasMore={historyHasMore}
          defaultAgent={defaultAgent}
          installedAgents={installedAgents}
          rowAgents={sidebarAgents}
          rowChoices={sidebarChoices}
          mode={mode}
          onWidthChange={setSidebarWidth}
          onDragChange={setSidebarDragging}
          fillsHost={isMobile}
          collapsible={!isMobile}
          staticRows={isMobile}
          onSelectSlot={selectFromSidebar}
          onOpenSlotInNewTab={ownsSessionTabs ? openSlotInNewTab : undefined}
          onOpenSource={revealSourceLink}
          // Only offer the pane as a drop target when a composer exists to show
          // the chip — see canStageSessionRef for why this is a named predicate.
          chatDropTarget={canStageSessionRef ? chatPaneEl : null}
          onDropSessionRef={stageSessionRef}
        />
        )
        return isMobile && mobileNavRail ? (
          <div className="flex h-full min-h-0 w-full" data-testid="mobile-split-drawer">
            {mobileNavRail({ onActivate: closeSidebar })}
            {/* The pane sits on the drawer's elevated surface so it reads apart
                from the rail's `bg-bg-accent` (the sidebar's own shell paints
                `bg-bg`, one step too close to the rail on the light themes). */}
            {/* The drawer's `[&>*]` resets (no card border/radius/margin on the
                sidebar root) reach only its direct child, which is now this
                wrapper -- so the wrapper carries the same resets one level down. */}
            <div className="flex-1 min-w-0 h-full min-h-0 flex flex-col overflow-hidden bg-bg-elevated [&_.sidebar-inner]:!bg-bg-elevated [&_.sidebar-inner]:!w-full [&_.sidebar-inner]:!min-w-0 [&_.sidebar-inner]:!shrink [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0">{sessionsPane}</div>
          </div>
        ) : sessionsPane
        })()}
      </OverlayDrawer>
      </GuideRevealScope>
      )}
      <MobileTopBar
        topbarSlot={topbarSlot}
        topbarTrailSlot={topbarTrailSlot}
        embedMode={embedMode}
        topbarSessionsToggle={topbarSessionsToggle}
        activeSlot={activeSlot}
        splitMode={splitMode}
        splitFeatureEnabled={splitFeatureEnabled}
        title={title}
        editingTitle={editingTitle}
        setEditingTitleSlot={setEditingTitleSlot}
        showActionError={showActionError}
        setActionError={setActionError}
        currentSlot={currentSlot}
        sidebarAutoHidden={sidebarAutoHidden}
        openSidebar={openSidebar}
        effectiveMode={effectiveMode}
        sidebarOnScreen={sidebarOnScreen}
        activePoppedOut={activePoppedOut}
        focusActivePopout={focusActivePopout}
        openActivePopout={openActivePopout}
        activityOpen={activityOpen}
        toggleAct={toggleAct}
        splitAnchorForActive={splitAnchorForActive}
        activeIsSplitAnchor={activeIsSplitAnchor}
        enterSplit={enterSplit}
        mobileNewSessionItem={mobileNewSessionItem}
        dispatch={dispatch}
      />

      {/* Per-slot tag picker — a single connected popover, opened from any session
          menu (sidebar row or header) via the ChatPage-scoped TagPopover context. */}
      <SlotTagPopover />
    </>
  )
}

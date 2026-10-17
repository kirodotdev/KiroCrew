import type { NavigateFunction } from 'react-router-dom'
import { Suspense, lazy, type Dispatch, type SetStateAction } from 'react'
import type { SessionTabsApi } from '../../../hooks/useSessionTabs'
import type { ProviderAdapter } from '../../../providers/types'
import type { AppDispatch } from '../../../store'
import type { ChatMessage } from '../../../types'
import type { ErrorReport } from '../../../utils/errorReport'
import type { HistoryDeleteRefusal } from '../../../utils/historyDeleteRefusal'
import type { ShortNameMatch } from '../../../utils/sessionKeys'
import SnipOverlay from '../../../components/SnipOverlay'
import type { useModelPickerModels } from './sessionPicks'
import type { useSteerDelivery } from './turnActions'
import ChatPaneNotices from './ChatPaneNotices'
import SessionTabStrip from '../../../components/SessionTabStrip'
import { PanelLeftSolid } from '../../../components/icons/panels'
// Eager: holds the window's isolation guards until its chunk arrives.
import CrewWindowGuard from '../crew-window/CrewWindowGuard'
import { i18nT } from '../../../i18n/t'

// Only a crew session mounts the window, so its code loads on first open.
const CrewChatWindow = lazy(() => import('../crew-window/CrewChatWindow'))

interface ChatPaneChromeProps {
  embedded?: boolean
  activeSlot: string | null
  connected: boolean
  dispatch: AppDispatch
  isMobile: boolean
  navigate: NavigateFunction
  provider: ProviderAdapter
  setVoiceRecoverySlot: Dispatch<SetStateAction<string | null>>
  switchSlotGone: { name: string; kind: 'gone' | 'failed'; report?: ErrorReport; } | null
  titleInTopbar: boolean
  undeletableHistory: HistoryDeleteRefusal | null
  unresumableResume: { key: string; title: string; surface: string; reason: 'surface' | 'failed'; } | null
  crewWindow: { instanceId: string; key: string; } | null
  messages: ChatMessage[]
  slotRunning: boolean
  splitMode: boolean
  selectionCapabilitiesQ: ReturnType<typeof useModelPickerModels>['selectionCapabilitiesQ']
  actionError: { title?: string; message: string; preserveOnSwitch?: boolean; report?: ErrorReport; } | null
  setActionError: ReturnType<typeof useSteerDelivery>['setActionError']
  setUploadError: Dispatch<SetStateAction<string>>
  setUploadHint: Dispatch<SetStateAction<string>>
  snipFrame: HTMLCanvasElement | null
  uploadError: string
  uploadHint: string
  handleSnipCancel: () => void
  handleSnipComplete: (file: File) => void
  closeSessionTab: (key: string) => void
  openShortNameMatch: (match: ShortNameMatch) => void
  ownsSessionTabs: boolean
  selectSessionTab: (key: string) => void
  sessionTabs: SessionTabsApi
  setSidError: Dispatch<SetStateAction<string>>
  setSidMatches: Dispatch<SetStateAction<{ name: string; opened: ShortNameMatch; others: ShortNameMatch[]; } | null>>
  sidError: string
  sidMatches: { name: string; opened: ShortNameMatch; others: ShortNameMatch[]; } | null
  splitFeatureEnabled: boolean
  chipDefault: { settingsDefault: string | null; agentPinned: boolean; failed: boolean; }
  dismissPinStatus: () => void
  pinError: string | null
  pinStatus: string | null
  inlineSidePanelShowing: boolean
  openSidebar: () => void
  sidebarOpen: boolean
  COLLAPSED_SESSION_TABS_INSET: number
}

/**
 * The chat pane's top chrome: the crew window cover, the screen-snip overlay, the pane notices, the phone's floating sessions opener, and the open-session tab strip.
 */
export default function ChatPaneChrome({
  embedded, activeSlot, connected, dispatch, isMobile, navigate, provider, setVoiceRecoverySlot, switchSlotGone, titleInTopbar, undeletableHistory, unresumableResume, crewWindow, messages,
  slotRunning, splitMode, selectionCapabilitiesQ, actionError, setActionError, setUploadError, setUploadHint, snipFrame, uploadError, uploadHint, handleSnipCancel, handleSnipComplete, closeSessionTab,
  openShortNameMatch, ownsSessionTabs, selectSessionTab, sessionTabs, setSidError, setSidMatches, sidError, sidMatches, splitFeatureEnabled, chipDefault, dismissPinStatus, pinError, pinStatus,
  inlineSidePanelShowing, openSidebar, sidebarOpen, COLLAPSED_SESSION_TABS_INSET,
}: ChatPaneChromeProps) {
  return (
    <>
      {crewWindow && (
        <div className="absolute inset-0 z-[48] bg-bg" data-crew-cover>
          <Suspense fallback={<CrewWindowGuard onClose={embedded ? () => navigate('/embed/sessions') : undefined} />}>
            <CrewChatWindow key={`${crewWindow.instanceId}:${crewWindow.key}`} target={crewWindow}
              onClose={embedded ? () => navigate('/embed/sessions') : undefined} />
          </Suspense>
        </div>
      )}
      {snipFrame && (
        <SnipOverlay
          frame={snipFrame}
          onComplete={handleSnipComplete}
          onCancel={handleSnipCancel}
          onError={setUploadError}
        />
      )}
      <ChatPaneNotices
        uploadHint={uploadHint}
        setUploadHint={setUploadHint}
        uploadError={uploadError}
        setUploadError={setUploadError}
        sidError={sidError}
        setSidError={setSidError}
        sidMatches={sidMatches}
        setSidMatches={setSidMatches}
        openShortNameMatch={openShortNameMatch}
        activeSlot={activeSlot}
        provider={provider}
        selectionCapabilitiesQ={selectionCapabilitiesQ}
        chipDefault={chipDefault}
        actionError={actionError}
        setActionError={setActionError}
        switchSlotGone={switchSlotGone}
        setVoiceRecoverySlot={setVoiceRecoverySlot}
        pinError={pinError}
        pinStatus={pinStatus}
        dismissPinStatus={dismissPinStatus}
        unresumableResume={unresumableResume}
        undeletableHistory={undeletableHistory}
        dispatch={dispatch}
      />
      {/* Floating sessions opener — mobile only, and only on a chat with
          nothing in it yet (a conversation gets the in-header control
          instead). Suppressed while the inline side panel is showing: it is
          `fixed` at the same top-left corner as the panel's own collapse
          button and, carrying z-10 against that button's auto z-index, paints
          OVER it — leaving no way to close a panel that covers the whole
          screen. It would also be pointing at a chat pane the panel has
          squeezed to zero width. Sessions stay reachable meanwhile via the
          rightward drag (useDrawerSwipe above).

          Suppressed when EMBEDDED for the same reason it is suppressed
          behind the side panel: `fixed` anchors it to the VIEWPORT, not to
          the host's pane, so it lands on whatever the host put in that
          corner -- in Papyrus, on the toolbar's back button, giving two
          overlapping tap targets on the app's primary exit. A host that
          embeds one scoped conversation has no sessions list to open. */}
      {isMobile && !embedded && !sidebarOpen && !inlineSidePanelShowing && !titleInTopbar && !(activeSlot && (messages.length > 0 || slotRunning)) && (
        <div className="fixed top-safe-offset-[42px] left-safe ml-2 z-10">
          <button className="mc-touch-hit p-2 rounded-lg text-muted hover:text-text bg-bg-elevated border border-border shadow-sm cursor-pointer" onClick={openSidebar} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
            {/* Same glyph as the desktop toggle: a control is named by the SURFACE
                it opens, and this opens the sessions panel. Solid rather than
                `PanelLeftLight` because this form only renders while that panel is
                closed. It carries no conversation-mode variant -- mode belongs to
                the conversation, not to the drawer, and the header's own mode
                control already shows it. */}
            <PanelLeftSolid size={18} />
          </button>
        </div>
      )}
      {/* Open-sessions strip. ABOVE the session title row, not inside the
          transcript column: the title row is an absolute overlay anchored to
          that column, so a strip inserted inside it would be painted over.
          Sitting here it pushes the whole column down instead, and the
          transcript (flex: 1) gives up exactly the strip's height.

          Renders nothing below two tabs (see SessionTabStrip), so a user who
          never opens a second tab sees the surface unchanged. Suppressed in
          split view, which does its own tiling and shows every open session
          at once, and on every EMBEDDED host (`ownsSessionTabs`) — the same
          predicate that stops those hosts owning the persisted set, so the
          strip and the set can never disagree about whose surface this is. */}
      {activeSlot && ownsSessionTabs && !(splitMode && splitFeatureEnabled) && (
        // no-drag: on the desktop shell the top strip of the window is the
        // titlebar drag region, and a tab you cannot click is worse than no tab.
        //
        // While the desktop sidebar is collapsed the shell insets the strip
        // past the stationary toggle, gliding on the same 240ms curve as the
        // panel morph and the title row so the tabs do not jump at the start
        // of the slide. The shell owns the strip's bottom divider (the strip
        // draws none of its own): an inset border on the strip's root would
        // stop short of the gutter and leave a notch under the toggle, so
        // the shell draws one continuous hairline across the full width.
        // `empty:hidden` keeps the shell out of the layout when the strip
        // renders nothing (fewer than two tabs), so the divider it owns
        // appears only when the strip does.
        <div
          className="empty:hidden border-b border-border transition-[padding-left] duration-[240ms] [transition-timing-function:cubic-bezier(.32,.72,0,1)]"
          style={{
            WebkitAppRegion: 'no-drag',
            paddingLeft: !sidebarOpen && !isMobile ? COLLAPSED_SESSION_TABS_INSET : 0,
          } as React.CSSProperties}
        >
          <SessionTabStrip
            tabs={sessionTabs.tabs}
            activeKey={activeSlot}
            cue={sessionTabs.cue}
            connected={connected}
            onSelect={selectSessionTab}
            onClose={closeSessionTab}
          />
        </div>
      )}
    </>
  )
}

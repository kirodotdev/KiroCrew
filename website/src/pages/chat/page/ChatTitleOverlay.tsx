import type { MutableRefObject, Dispatch, SetStateAction } from 'react'
import { Undo2, ExternalLink, Columns2 } from 'lucide-react'
import type { SidePanelDock } from '../../../hooks/useSidePanelDock'
import type { AppDispatch } from '../../../store'
import type { ChatSlot } from '../../../types'
import type { ErrorReport } from '../../../utils/errorReport'
import type { PinnedPromptState } from '../../../utils/pinnedPrompt'
import { requestSlotReveal } from '../../../store/chatSlice'
import { ChatHeaderMenu } from '../ChatHeaderMenu'
import type { useSteerDelivery } from './turnActions'
import { SELECTION_INERT_ATTR } from '../useSelectionInertOverlays'
import ChatHeaderChips from '../../../components/ChatHeaderChips'
import { PinnedPrompt } from '..'
import { pinCandidateKey } from '../usePinnedPrompt'
import SessionTitleControl from '../SessionTitleControl'
import { EdgeFade } from '../../../app-sdk/ChatScrollChrome'
import { SidePanelGlyph } from '../../../components/SidePanelGlyph'
import Clickable from '../../../components/Clickable'
import type { useLabelTip } from '../../../components/InstantTip'
import { i18nT } from '../../../i18n/t'
import { uiLocation } from '../../../uiLocations/uiLocation'

interface ChatTitleOverlayProps {
  embedMode?: 'chat' | 'sessions'
  popout?: boolean
  activeSlot: string
  dispatch: AppDispatch
  filteredSlots: ChatSlot[]
  isMobile: boolean
  titleInTopbar: boolean
  activityOpen: boolean
  setActionError: ReturnType<typeof useSteerDelivery>['setActionError']
  showActionError: (message: string, title?: string, report?: ErrorReport) => void
  onPinCollapsedHeight: (h: number) => void
  pinCardRef: MutableRefObject<HTMLDivElement | null>
  pinExpanded: boolean
  pinFoldRef: MutableRefObject<HTMLDivElement | null>
  pinned: PinnedPromptState | null
  scrollToPinnedPrompt: (target: number) => void
  scrollTranscriptBy: (dy: number) => void
  setPinExpanded: Dispatch<SetStateAction<boolean>>
  activePoppedOut: boolean
  focusActivePopout: (id: string) => void
  openActivePopout: (id: string, title?: string) => void
  returnSelfToMain: () => void
  splitFeatureEnabled: boolean
  currentSlot: ChatSlot | undefined
  effectiveMode: string | undefined
  title: string
  activeIsSplitAnchor: boolean
  editingTitle: boolean
  enterSplit: (anchor: string | null) => void
  setEditingTitleSlot: Dispatch<SetStateAction<string | null>>
  setSidebarPinned: Dispatch<SetStateAction<boolean>>
  sidebarAutoHidden: MutableRefObject<boolean | null>
  sidebarPinned: boolean
  sidePanelDock: SidePanelDock
  splitAnchorForActive: string | null
  toggleAct: () => void
  openSidebar: () => void
  sidebarOnScreen: boolean
  sidebarOpen: boolean
  sidePanelWantsMount: boolean
  mobileNewSessionItem: { label: string; disabled: boolean; onSelect: () => void; }
  mobileSessionsToggle: JSX.Element
  popOutTip: ReturnType<typeof useLabelTip>
  activityTip: ReturnType<typeof useLabelTip>
}

/**
 * The session title row over the transcript (header menu, title, pop-out, side panel and split controls, the header fade), the pinned-prompt fold sentinel, and the pinned prompt.
 */
export default function ChatTitleOverlay({
  embedMode, popout, activeSlot, dispatch, filteredSlots, isMobile, titleInTopbar, activityOpen, setActionError, showActionError, onPinCollapsedHeight, pinCardRef, pinExpanded, pinFoldRef, pinned,
  scrollToPinnedPrompt, scrollTranscriptBy, setPinExpanded, activePoppedOut, focusActivePopout, openActivePopout, returnSelfToMain, splitFeatureEnabled, currentSlot, effectiveMode, title,
  activeIsSplitAnchor, editingTitle, enterSplit, setEditingTitleSlot, setSidebarPinned, sidebarAutoHidden, sidebarPinned, sidePanelDock, splitAnchorForActive, toggleAct, openSidebar, sidebarOnScreen,
  sidebarOpen, sidePanelWantsMount, mobileNewSessionItem, mobileSessionsToggle, popOutTip, activityTip,
}: ChatTitleOverlayProps) {
  return (
    <>
      {/* Claude-style title row — absolute overlay, solid top fading to transparent.
          Inset on the right by the 6px scrollbar width (see ::-webkit-scrollbar
          in index.css) so the overlay never paints over the scroller's scrollbar
          track — otherwise the thumb is hidden/un-grabbable when scrolled to top. */}
      {/* z-[45] at rest keeps this row BELOW the mobile drawer scrim
          (z-[46]) so an open sessions drawer dims it. While it hosts the
          rename editor it lifts to z-[47], above the composer status bars
          (z-[46]): those are flex-flow chrome, not overlays, and they rise
          into this band once the transcript scroller collapses to zero —
          what a phone keyboard does — where they painted over the caret.
          Scoping the lift to the edit is safe because opening the drawer
          blurs the input, which commits and closes the editor. */}
      <div className={`absolute top-0 left-0 right-1.5 ${editingTitle ? 'z-[47]' : 'z-[45]'} pointer-events-none`} style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties} {...{ [SELECTION_INERT_ATTR]: '' }}>
        {/* The row's left padding GLIDES between its open (20px) and
            collapsed (60px, clearing the stationary toggle + divider)
            values on the same 320ms curve as the panel — an instant
            class flip here reads as the title jumping sideways at the
            start of the slide. */}
        {/* Phone: the shell's single bar hosts this row (the portal above),
            so the inline copy stands down. The fold sentinel, the pinned
            prompt and the header fade stay: they belong to the transcript
            column, not to the bar. */}
        {!titleInTopbar && (
        <div className={`@container/chathead relative pr-1.5 pt-[9px] pb-2 flex items-center gap-2 bg-bg pointer-events-none transition-[padding-left] duration-[240ms] [transition-timing-function:cubic-bezier(.32,.72,0,1)] ${!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && !sidebarOpen ? 'pl-[60px]' : isMobile ? (embedMode === 'chat' ? 'pl-4' : 'pl-3') : 'pl-5'}`}>
          {/* Divider between toggle and title — ALWAYS mounted and
              absolute (zero width, no flex-gap participation) so it can
              never change the row's layout; it rides the row (title
              side) and only fades. left-[52px] = the collapsed pane's
              view of container x 44 (button 8+28 + 8px gap). */}
          {!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && (
            <span aria-hidden="true" className={`absolute left-[52px] top-[13px] w-px h-5 bg-border transition-opacity ${sidebarOpen ? 'opacity-0 duration-100' : 'opacity-100 duration-150 delay-[90ms]'}`} />
          )}
          {embedMode !== 'chat' && isMobile && mobileSessionsToggle}
          <div className="group/header flex min-w-0 items-stretch gap-0.5 pointer-events-auto">
          <div className="flex items-center rounded-l-md rounded-r-[2px] px-1.5 py-0.5 group-hover/header:bg-bg-hover transition-colors">
          <ChatHeaderMenu
            activeSlot={activeSlot}
            agent={currentSlot?.agent}
            onReveal={activeSlot && embedMode !== 'chat' ? () => {
              // The request rides the store, not a window event: with the
              // drawer collapsed ChatSidebar is unmounted, so an event
              // dispatched here (before the mount that setSidebarPinned
              // schedules commits) had no listener and was dropped —
              // the store entry survives until the sidebar consumes it
              // (#912). Mobile drives its own drawer state. Embed-chat
              // never mounts a sidebar, so the item is not offered there:
              // a stored request would outlive the view and fire on
              // whichever sidebar mounts next.
              sidebarAutoHidden.current = null
              if (isMobile) openSidebar()
              else if (!sidebarPinned) setSidebarPinned(true)
              dispatch(requestSlotReveal(activeSlot))
            } : undefined}
            onRename={activeSlot ? () => setEditingTitleSlot(activeSlot) : undefined}
            mode={effectiveMode}
            sidebarOnScreen={sidebarOnScreen}
            newSessionHere={embedMode !== 'chat' && isMobile ? mobileNewSessionItem : undefined}
          />
          </div>
          {/* Shared with every split-view pane header (#9727). The editor
              flag stays here, pinned to the slot it opened on. */}
          {activeSlot && (
            <SessionTitleControl
              slotKey={activeSlot}
              title={title}
              editing={editingTitle}
              onEditingChange={open => setEditingTitleSlot(open ? activeSlot : null)}
              onError={showActionError}
              onAttempt={() => setActionError(null)}
            />
          )}
          </div>
        <ChatHeaderChips slotKey={activeSlot} />
        {/* Trailing controls grouped under a single ml-auto so multiple
            right-aligned items don't each absorb free space (two ml-auto
            siblings split the gap, parking the split icon mid-header). */}
        {/* focus-caption-reserve: this group owns the window's top-trailing
            corner — where Windows and frameless Linux paint their caption
            controls — whenever the side panel is not holding that edge, i.e.
            while it is closed (the state that renders the reopen toggle
            below) or docked at the bottom. Right-docked and showing, the
            panel is at that edge instead and carries the reserve itself, so
            reserving here too would indent these controls for nothing. */}
        <div className={`ml-auto flex shrink-0 items-center gap-1.5 pointer-events-none${!sidePanelWantsMount || sidePanelDock === 'bottom' ? ' focus-caption-reserve' : ''}`}>
        {/* Pop-out control, promoted to the title bar (menu items remain for
            sidebar parity). Mirrors the split-view pattern to its left: a
            dimmed icon to act, an accent chip when the state is active.
            Inside the popout window itself the same spot carries Return. */}
        {popout ? (
          <Clickable className="flex items-center gap-1 text-muted hover:text-text transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded hover:bg-bg-hover" onClick={returnSelfToMain} title={i18nT('pages.chatPage.return_this_session_to_the_main_window')} aria-label={i18nT('pages.chatPage.return_to_main_window')}>
            <Undo2 size={13} /> {i18nT('pages.chatPage.return')}
          </Clickable>
        ) : !embedMode && activeSlot && (activePoppedOut ? (
          <Clickable className="flex items-center gap-1 text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded" onClick={() => focusActivePopout(activeSlot)} title={i18nT('pages.chatPage.this_session_is_open_in_its_own_window_focus_it')} aria-label={i18nT('pages.chatPage.focus_popped_out_window')}>
            <ExternalLink size={13} /> {i18nT('pages.chatPage.popped_out')}
          </Clickable>
        ) : (
          <Clickable className="flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 text-muted hover:text-text pointer-events-auto" onClick={() => openActivePopout(activeSlot, currentSlot?.title)} aria-label={i18nT('pages.chatPage.pop_out_session_to_its_own_window')} {...popOutTip.handlers}>
            <ExternalLink size={15} />
            {popOutTip.bubble}
          </Clickable>
        ))}
        {/* Activity panel open toggle — relocated here from the top bar
            (item 2.4) so opening the panel no longer narrows the now
            full-width header. Shown only while the panel is closed; the
            panel's own header carries the close button. Never disabled:
            below the mobile breakpoint the panel opens full width, at or
            above it opens beside the chat. There is no width at which
            the button does nothing. */}
        {!embedMode && !popout && !activityOpen && (
          <Clickable
            className="pi-morph flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 pointer-events-auto text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
            onClick={toggleAct}
            aria-label={i18nT('pages.chatPage.open_activity_panel')}
            {...uiLocation('chat.side-panel-open')}
            {...activityTip.handlers}
          >
            <SidePanelGlyph size={15} />
            {activityTip.bubble}
          </Clickable>
        )}
        {!embedMode && splitFeatureEnabled && (splitAnchorForActive && !activeIsSplitAnchor ? (
          <Clickable className="flex items-center gap-1 text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded" onClick={() => enterSplit(splitAnchorForActive)} title={i18nT('pages.chatPage.this_session_is_open_in_a_split_return_to_it')} aria-label={i18nT('pages.chatPage.return_to_split_view')}>
          <Columns2 size={13} /> {i18nT('pages.chatPage.in_split')}
        </Clickable>
        ) : (
          <Clickable className="opacity-40 hover:opacity-100 transition-opacity cursor-pointer pointer-events-auto" onClick={() => enterSplit(activeSlot)} title={i18nT('pages.chatPage.split_view_d')} aria-label={i18nT('pages.chatPage.enter_split_view')}>
          <Columns2 size={14} />
        </Clickable>
        ))}
        </div>
        {/* Header fade — softens content passing up into the opaque title
            row, so it hangs off that row's bottom edge (anchor="below":
            as an in-flow sibling its 24px consumed layout and pushed the
            pinned card that far off the header; out of flow it overlays
            the transcript and the pinned card paints above it). */}
        <EdgeFade side="top" anchor="below" />
        </div>
        )}
        {/* Fold sentinel — zero-height, always mounted. Its top edge is the
            line the pinned prompt sticks to (see updatePinnedPrompt). */}
        <div ref={pinFoldRef} aria-hidden className="h-0" />
        {/* Anchor the phone fade at the fold so its opaque edge stays behind
            the card instead of masking the transcript below it. */}
        {titleInTopbar && (
          <div className="relative h-0">
            <EdgeFade side="top" />
          </div>
        )}
        {pinned && (
          <PinnedPrompt
            text={pinned.text}
            fullText={pinned.full}
            images={pinned.images}
            bodyBeyondPreview={pinned.bodyBeyondPreview}
            pushUp={pinned.push}
            liveH={pinned.liveH}
            maxH={pinned.maxH}
            bannerH={pinned.bannerH}
            expanded={pinExpanded}
            onToggleExpanded={() => setPinExpanded(p => !p)}
            onJump={() => scrollToPinnedPrompt(pinned.idx)}
            cardRef={pinCardRef}
            onCollapsedHeight={onPinCollapsedHeight}
            promptKey={pinCandidateKey(pinned.idx, pinned.ts)}
            scrollTranscriptBy={scrollTranscriptBy}
          />
        )}
      </div>
    </>
  )
}

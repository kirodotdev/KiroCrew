import { Suspense, lazy, type ComponentProps, type Dispatch, type MutableRefObject, type ReactNode, type SetStateAction } from 'react'
import { createPortal } from 'react-dom'
import { Columns2, ExternalLink, EyeOff, MoreHorizontal, VenetianMask } from 'lucide-react'

import { api } from '../../../api/client'
import ErrorBoundary from '../../../components/ErrorBoundary'
import ErrorNotice, { ErrorNoticeMenuItem } from '../../../components/ErrorNotice'
import { PanelRightSolid } from '../../../components/icons/panels'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../../../components/ui/dropdown-menu'
import type { useChatPopouts } from '../../../hooks/useChatPopouts'
import { i18nT } from '../../../i18n/t'
import type { AppDispatch } from '../../../store'
import { requestSlotReveal } from '../../../store/chatSlice'
import { sseSlotTitle } from '../../../store/dashboardSlice'
import type { ChatSlot } from '../../../types'
import { errMessage } from '../../../utils/thunkError'
import { ChatHeaderMenu } from '../ChatPageMessageContent'
import SessionTitleControl from '../SessionTitleControl'

// Lazy for the same reason App.tsx lazy-loads the pill: the update chunk is
// off the app-core budget, and the phone menu needs it only while an update exists.
const MobileUpdateMenuItem = lazy(() => import('../../../components/UpdatePill'))

interface MobileTopBarProps {
  /** The shell's `#mobile-topbar-slot` and `#mobile-topbar-trail-slot`, or null off the phone chat route. */
  topbarSlot: HTMLElement | null
  topbarTrailSlot: HTMLElement | null
  embedMode: 'chat' | 'sessions' | undefined
  topbarSessionsToggle: ReactNode
  activeSlot: string | null
  splitMode: boolean
  splitFeatureEnabled: boolean
  title: string
  editingTitle: boolean
  setEditingTitleSlot: Dispatch<SetStateAction<string | null>>
  showActionError: (message: string, title?: string) => void
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean } | null>>
  currentSlot: ChatSlot | undefined
  /** Pre-expand sidebar state; a user reveal clears it (see ChatPage). */
  sidebarAutoHidden: MutableRefObject<boolean | null>
  openSidebar: () => void
  /** One LLM title generation at a time from the menu item. */
  menuAutoTitleInFlight: MutableRefObject<boolean>
  effectiveMode: string | undefined
  sidebarOnScreen: boolean
  activePoppedOut: boolean
  focusActivePopout: ReturnType<typeof useChatPopouts>['focus']
  openActivePopout: ReturnType<typeof useChatPopouts>['open']
  activityOpen: boolean
  toggleAct: () => void
  splitAnchorForActive: string | null
  activeIsSplitAnchor: boolean
  enterSplit: (anchor: string | null) => void
  /** The session menu's "New chat here" item (ChatPage builds it from the current folder). */
  mobileNewSessionItem: ComponentProps<typeof ChatHeaderMenu>['newSessionHere']
  dispatch: AppDispatch
}

/**
 * The chat page's share of the phone shell's ONE top bar (narrow-viewport.md,
 * "The phone chat page has ONE top bar"): the leading cell's sessions toggle,
 * session title and menu, and the trailing overflow menu. Portaled into the
 * shell's slots; renders nothing where the shell has none.
 */
export default function MobileTopBar({
  topbarSlot,
  topbarTrailSlot,
  embedMode,
  topbarSessionsToggle,
  activeSlot,
  splitMode,
  splitFeatureEnabled,
  title,
  editingTitle,
  setEditingTitleSlot,
  showActionError,
  setActionError,
  currentSlot,
  sidebarAutoHidden,
  openSidebar,
  menuAutoTitleInFlight,
  effectiveMode,
  sidebarOnScreen,
  activePoppedOut,
  focusActivePopout,
  openActivePopout,
  activityOpen,
  toggleAct,
  splitAnchorForActive,
  activeIsSplitAnchor,
  enterSplit,
  mobileNewSessionItem,
  dispatch,
}: MobileTopBarProps) {
  return (
    <>
    {/* Phone: this page's share of the shell's single top bar. Leading cell:
        [sessions toggle][session title][session menu]. The title is first and
        the menu chevron trails it, as on the approved mock — one control the
        eye reads as "the session, and its menu". Rendered whenever the slot
        exists, so the sessions toggle is in the bar even with no session open
        (ChatPage's fixed corner button stands down); the title half needs a
        session and is not drawn in split view, whose grid names each pane. */}
    {topbarSlot && createPortal(
      <>
        {embedMode !== 'chat' && topbarSessionsToggle}
        {activeSlot && !(splitMode && splitFeatureEnabled) && (
          <div className="group/header flex min-w-0 flex-1 items-center gap-0.5" data-testid="mobile-topbar-title">
            {/* ONE control for title + menu: the session menu's trigger carries
                the title text with the chevron flush after its last character
                (the approved mock), so the bar's centre holds two controls --
                the sessions toggle and this -- and the title has one tap
                target, not a rename tap beside a menu tap. Rename lives in the
                menu (`onRename`) and swaps this trigger for the shared title
                editor while it is open; the memory-mode glyphs ride the label
                so an incognito/temporary session is still marked. */}
            {editingTitle ? (
              <SessionTitleControl
                slotKey={activeSlot}
                title={title}
                editing
                onEditingChange={open => setEditingTitleSlot(open ? activeSlot : null)}
                onError={showActionError}
                onAttempt={() => setActionError(null)}
              />
            ) : (
              <ChatHeaderMenu
                activeSlot={activeSlot}
                agent={currentSlot?.agent}
                onReveal={() => {
                  // Same as the inline row's reveal: the phone drives its own
                  // drawer, and the store carries the request (#912).
                  sidebarAutoHidden.current = null
                  openSidebar()
                  dispatch(requestSlotReveal(activeSlot))
                }}
                onRename={() => setEditingTitleSlot(activeSlot)}
                // The phone bar does not render the title row's hover-revealed
                // Auto-title button (an opacity-0 control has no touch home), so
                // the LLM rename is a menu item here. Same endpoint and same
                // store write as SessionTitleControl's button; the Undo window
                // that button offers is a hover-hold affordance and is not
                // offered from a menu -- Rename in the same menu is the way back.
                onAutoTitle={() => {
                  if (menuAutoTitleInFlight.current) return
                  menuAutoTitleInFlight.current = true
                  setActionError(null)
                  const slot = activeSlot
                  api.generateTitle(slot).then(r => {
                    /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */
                    if (r.title) dispatch(sseSlotTitle({ key: slot, title: r.title }))
                  }).catch(e => showActionError(errMessage(e) || i18nT('pages.chatPage.unknown_error'), i18nT('pages.chatPage.could_not_generate_title')))
                    .finally(() => { menuAutoTitleInFlight.current = false })
                }}
                mode={effectiveMode}
                sidebarOnScreen={sidebarOnScreen}
                // Pop out / focus the popped-out window live in the bar's
                // trailing ⋯ menu (below), the phone's window menu; the same
                // row in two adjacent menus read as two different actions.
                omitPopout
                newSessionHere={embedMode !== 'chat' ? mobileNewSessionItem : undefined}
                triggerLabel={
                  <>
                    {currentSlot?.memory_mode === 'incognito' && <EyeOff size={13} className="lucide-inline shrink-0 text-warn" aria-label={i18nT('pages.chatPage.incognito_memory_writes_disabled')} />}
                    {currentSlot?.memory_mode === 'temporary' && <VenetianMask size={13} className="lucide-inline shrink-0 text-aim" aria-label={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')} />}
                    {title}
                  </>
                }
              />
            )}
            {/* No InboundLinkChip here: it is a third button in a two-button
                cell, and a two-way link would make it a third trigger. Its actions are the session
                menu's "Linked surfaces" section (LinkedSurfacesSection). */}
          </div>
        )}
      </>,
      topbarSlot,
    )}
    {/* Trailing cell: the page's overflow menu, after the shell's bell — the
        second of the two controls a row may hold. It carries what the inline
        title row shows as icons: pop-out (or focus the popped-out window),
        the activity panel, and split view. Nothing here is a hard swap: the
        menu opens with DropdownMenu's own animation. */}
    {topbarTrailSlot && activeSlot && !embedMode && createPortal(
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button type="button" className="mc-touch-hit w-8 h-8 rounded-md flex items-center justify-center text-text hover:text-text-strong hover:bg-bg-hover bg-transparent border-none cursor-pointer shrink-0" aria-label={i18nT('pages.chatPage.more_actions')} title={i18nT('pages.chatPage.more_actions')} data-testid="mobile-topbar-more">
            <MoreHorizontal size={18} />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-[220px]">
          {/* A pending update leads the menu (accent row, same lifecycle label
              as the desktop pill: available → downloading N% → ready). The
              phone bar has no cell with room for the pill without making a
              three-control group, so this is its phone home. Renders nothing
              when no update exists. */}
          {/* Local boundary: this is a lazy chunk, and a chunk that fails to
              load after the preload heal declined would otherwise reject up
              to the ROUTE boundary and replace the whole chat page with an
              error card. The fallback is the shared error surface, not
              nothing: this menu is the update's only phone home, so a chunk
              that never loads must SAY so here (`errors-use-error-notice`),
              and the sibling item carries the agent hand-off through the
              menu's roving focus. Settings › About still checks for updates. */}
          <ErrorBoundary
            scope="mobile-update-menu"
            fallback={
              <>
                <ErrorNotice
                  id="mobile-update-menu-error"
                  variant="inline"
                  className="px-2 py-1.5"
                  message={i18nT('pages.chatPage.update_entry_load_failed')}
                />
                <ErrorNoticeMenuItem Item={DropdownMenuItem} message={i18nT('pages.chatPage.update_entry_load_failed')} describedBy="mobile-update-menu-error" />
              </>
            }
          ><Suspense fallback={null}><MobileUpdateMenuItem variant="menu-item" /></Suspense></ErrorBoundary>
          {activePoppedOut ? (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" onSelect={() => focusActivePopout(activeSlot)}>
              <span className="flex items-center gap-2"><ExternalLink size={14} className="shrink-0 text-muted" /><span>{i18nT('pages.chatPage.focus_popped_out_window')}</span></span>
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" onSelect={() => openActivePopout(activeSlot, currentSlot?.title)}>
              <span className="flex items-center gap-2"><ExternalLink size={14} className="shrink-0 text-muted" /><span>{i18nT('pages.chatPage.pop_out_to_window')}</span></span>
            </DropdownMenuItem>
          )}
          {!activityOpen && (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" onSelect={toggleAct}>
              <span className="flex items-center gap-2"><PanelRightSolid size={14} className="shrink-0 text-muted" /><span>{i18nT('pages.chatPage.open_activity_panel')}</span></span>
            </DropdownMenuItem>
          )}
          {splitFeatureEnabled && (splitAnchorForActive && !activeIsSplitAnchor ? (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" onSelect={() => enterSplit(splitAnchorForActive)}>
              <span className="flex items-center gap-2"><Columns2 size={14} className="shrink-0 text-muted" /><span>{i18nT('pages.chatPage.return_to_split_view')}</span></span>
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" onSelect={() => enterSplit(activeSlot)}>
              <span className="flex items-center gap-2"><Columns2 size={14} className="shrink-0 text-muted" /><span>{i18nT('pages.chatPage.enter_split_view')}</span></span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>,
      topbarTrailSlot,
    )}
    </>
  )
}

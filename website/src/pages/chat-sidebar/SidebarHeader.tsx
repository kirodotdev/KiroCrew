/** The sidebar header: the Sessions title, the list menu (dashboards, board
 *  toggle, cleanup, model switch, tags) and the split New button with its create
 *  menu (private chats, import, Crew Members, folders, chat in a folder or on a
 *  crew). */
import { Plus, Monitor, FolderPlus, FolderX, MessageSquarePlus, Folder, ChevronRight, ChevronDown, BrushCleaning, MoreVertical, Tag as TagIcon, Columns3, Loader2, Cpu, Users, Server, Megaphone } from 'lucide-react'
import ErrorNotice, { ErrorNoticeMenuItem } from '../../components/ErrorNotice'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent } from '../../components/ui/dropdown-menu'
import { LIST_HEADER_CLS, LIST_TITLE_CLS } from '../../components/listShell'
import ImportSessionItem from '../../components/ImportSessionItem'
import type { ChatFolder } from '../../types'
import { loadChatConfig, saveChatConfig } from '../chat/ChatSettings'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { PrivateChatRow } from './PrivateChatRow'
import { createTabModeForClick } from './chrome'
import type { SidebarScope } from './scope'
import OfflineMenuReason from '../../components/OfflineMenuReason'
import { offlineMenuRow, offlineProps } from '../../utils/offline'

/** The header row. */
export function renderSidebarHeader(shell: SidebarScope): React.ReactNode {
  const { cleanupOpen, collapsible, connected, createChatInFolder, createChatMutation, createEphemeralChatMutation,
    createRemoteChatMutation, creatingSlot, crewPreview, folderCleanupOpen, folderCompare, folders, isMobile,
    missingLanes, navigate, newChatMenuOpen, onMenuCloseAutoFocus, onOpenSlotInNewTab, openCrewMembers,
    paintedSidebarWidth, rawColumns, refuseCreateOffline, remoteCrewChatPreview, remoteCrewError, remoteCrewErrorId,
    restorePreBoardWidth, seedStateLanesMutation, setCleanupOpen, setFolderCleanupOpen, setFolderModal,
    setBroadcastOpen, setManageTagsOpen, setNewChatMenuOpen, setRemoteCrewError, setSeedError, setToggleSaveFailed,
    tagColumnsEnabled, warmCrews } = shell
  const { setCleanupError, setCleanupExpanded } = shell.sessionCleanup
  const { setBulkModel, setBulkModelError, setBulkModelOpen, setBulkSkipRunning } = shell.bulkModel
  // Narrow-sidebar header responsiveness: below ~256px the full "New chat"
  // label no longer fits next to the label + kebab, so collapse the create
  // button to icon-only; below ~200px also drop the "Sessions" label.
  const compactHeader = paintedSidebarWidth < 256
  const tinyHeader = paintedSidebarWidth < 200

  return (
      <div className={LIST_HEADER_CLS}>
        <div className={`flex items-center gap-1.5 min-w-0 flex-1 ${collapsible && !isMobile ? 'pl-9' : 'pl-1.5'}`}>
          {!tinyHeader && <span className={LIST_TITLE_CLS}>{i18nT('pages.chatSidebar.sessions')}</span>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <DropdownMenu guideScope="menu:sessions.list-menu">
            <DropdownMenuTrigger asChild>
              <button className="mc-touch-hit w-7 h-7 rounded-md border border-border bg-transparent text-muted cursor-pointer flex items-center justify-center hover:border-border-strong hover:text-text transition-all" title={i18nT('pages.chatSidebar.more_options')} aria-label={i18nT('pages.chatSidebar.more_options')} {...uiLocation('sessions.list-menu')}><MoreVertical size={14} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[180px]">
              <DropdownMenuItem onSelect={() => navigate('/session-dashboards')} {...uiLocation('sessions.list-menu.dashboards')}>
                <Monitor size={14} className="text-muted" />
                {i18nT('commandCenter.all_title')}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={seedStateLanesMutation.isPending} onClick={() => {
                if (seedStateLanesMutation.isPending) return
                const isActive = tagColumnsEnabled && rawColumns.length > 0
                const next = !isActive
                const cfg = loadChatConfig()
                // The toggle persists `tagColumnsEnabled` through saveChatConfig,
                // which returns false and rolls back when the write (or its dirty
                // marker) cannot be stored. Surface that failure and STOP before
                // clearing the error, seeding lanes or restoring widths — none of
                // which should happen for a preference that was not saved (GPT 6.1
                // F1, errors-use-error-notice).
                if (!saveChatConfig({ ...cfg, tagColumnsEnabled: next })) {
                  setToggleSaveFailed(true)
                  setSeedError(i18nT('pages.chatSidebar.failed_to_save_board_toggle'))
                  return
                }
                setToggleSaveFailed(false)
                setSeedError('')
                if (!next) {
                  // Leaving board view: give back the width the user chose before
                  // the lanes were auto-widened, rather than stranding a ~900px
                  // sidebar in list view.
                  restorePreBoardWidth()
                }
                // Seed when the board has no lanes and nothing configured worth
                // keeping. Seeding is additive and idempotent, so a repeat click
                // cannot duplicate lanes; the pending guard above only stops a
                // second request racing the first before the cache refreshes.
                if (next && !rawColumns.some(c => c.source === 'state' || c.name || (c.tag_ids || []).length || c.include_untagged)) {
                  seedStateLanesMutation.mutate()
                }
              }} {...uiLocation('sessions.list-menu.view')}>
                <Columns3 size={14} className={tagColumnsEnabled && rawColumns.length > 0 ? 'text-accent' : 'text-muted'} />
                {tagColumnsEnabled && rawColumns.length > 0 ? i18nT('pages.chatSidebar.switch_to_list_view') : i18nT('pages.chatSidebar.switch_to_board_view')}
              </DropdownMenuItem>
              {tagColumnsEnabled && rawColumns.length > 0 && missingLanes.length > 0 && (
                <DropdownMenuItem
                  data-testid="add-state-lanes"
                  {...uiLocation('sessions.list-menu.add-lanes')}
                  disabled={seedStateLanesMutation.isPending}
                  onClick={() => { if (!seedStateLanesMutation.isPending) seedStateLanesMutation.mutate() }}
                >
                  <Columns3 size={14} className="text-muted" />
                  {i18nT('pages.chatSidebar.add_state_lanes')}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onClick={() => { setCleanupOpen(!cleanupOpen); setCleanupExpanded(false); setCleanupError('') }} {...uiLocation('sessions.list-menu.clean-up')}>
                <BrushCleaning size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.clean_up_sessions')}
              </DropdownMenuItem>
              <DropdownMenuItem data-testid="clean-up-empty-folders" onClick={() => setFolderCleanupOpen(!folderCleanupOpen)}>
                <FolderX size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.clean_up_empty_folders_menu')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => { setBulkModelOpen(true); setBulkModel(''); setBulkSkipRunning(true); setBulkModelError('') }} {...uiLocation('sessions.list-menu.switch-model')}>
                <Cpu size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.switch_all_to_model')}
              </DropdownMenuItem>
              <DropdownMenuItem data-testid="broadcast-menu-item" onClick={() => setBroadcastOpen(true)}>
                <Megaphone size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.broadcast_menu')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setManageTagsOpen(o => !o)} {...uiLocation('sessions.list-menu.manage-tags')}>
                <TagIcon size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.manage_tags')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Split create-button: main segment = one-click New chat; caret
           *  opens a menu grouping New folder + New chat in folder (flat
           *  folder flyout). Replaces the old standalone New-folder + New-chat
           *  header buttons. Menu is portaled to the body-level overlay root so the right-side
           *  folder flyout escapes the sidebar's overflow clip. */}
          <div className="relative flex items-center rounded-md bg-accent text-accent-fg overflow-hidden [@media(pointer:coarse)]:overflow-visible shrink-0" data-create-menu>
            <button
              disabled={creatingSlot}
              // Offline the PRESS affordances come off (`active:scale-95`, the
              // hover fill), so a create that will refuse does not animate as
              // accepted. No opacity dim: the accent fill is the wrapper's, so a
              // dim here would fade only the label, and dimming the wrapper would
              // drag down the caret, whose menu still opens offline.
              className={`mc-touch-hit-y flex items-center h-7 rounded-s-md cursor-pointer bg-transparent border-none text-accent-fg transition-all disabled:opacity-70 disabled:cursor-wait disabled:active:scale-100 ${connected ? 'hover:bg-accent-hover active:scale-95' : ''} ${compactHeader ? 'justify-center w-7' : 'gap-1.5 pl-2 pr-2.5 text-[12px] font-semibold'}`}
              // Same gesture contract as a session row: plain click creates and
              // switches; Cmd/Ctrl-click and middle-click create the session as
              // a background TAB and leave the user where they are. Create
              // controls add Cmd/Ctrl+Shift-click, which opens the new session
              // in a new tab and switches to it (createTabModeForClick).
              // Both tab gestures are gated on `onOpenSlotInNewTab` — without a
              // tab strip (embedded hosts) there is nothing to open into, so the
              // modifier is ignored and the click stays an ordinary create.
              // Middle-press autoscroll is cancelled on mousedown, as on rows.
              onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
              onAuxClick={onOpenSlotInNewTab ? (e => {
                if (e.button !== 1 || creatingSlot) return
                e.preventDefault()
                // The same create as the caret menu's New chat row, which dims
                // and refuses offline; refused on both gestures, with the reason.
                if (!connected) { refuseCreateOffline(); return }
                createChatMutation.mutate({ inNewTab: 'background' })
              }) : undefined}
              onClick={e => { if (!connected) { refuseCreateOffline(); return } createChatMutation.mutate({ inNewTab: onOpenSlotInNewTab ? createTabModeForClick(e) : false }) }}
              title={i18nT('pages.chatSidebar.new_chat')}
              aria-label={i18nT('pages.chatSidebar.new_chat_session')}
              // Spread only while offline: this button also carries
              // `disabled={creatingSlot}`, and `aria-disabled="false"` would contradict it.
              {...(connected ? {} : offlineProps(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_chat_session')))}
              aria-busy={creatingSlot}
              {...uiLocation('chat.new-session')}
            >{creatingSlot ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />}{!compactHeader && <span className="whitespace-nowrap">{creatingSlot ? i18nT('pages.chatSidebar.creating') : i18nT('pages.chatSidebar.new')}</span>}</button>
            <span className="w-px h-4 bg-accent-fg opacity-30" aria-hidden="true" />
            <DropdownMenu guideScope="menu:sessions.create-menu" open={newChatMenuOpen} onOpenChange={o => { setNewChatMenuOpen(o); if (!o) setRemoteCrewError('') }}>
              <DropdownMenuTrigger asChild>
                <button
                  className="mc-touch-hit-end flex items-center justify-center w-6 h-7 rounded-e-md cursor-pointer bg-transparent border-none text-accent-fg hover:bg-black/10 active:scale-95 transition-all"
                  title={i18nT('pages.chatSidebar.create')} aria-label={i18nT('pages.chatSidebar.more_create_options')} {...uiLocation('sessions.create-menu')}><ChevronDown size={13} /></button>
              </DropdownMenuTrigger>
              {/* max-w bounds the menu: the mode descriptions below are full
               *  sentences, and without an upper bound a flex item's automatic
               *  min-width lets the longest one stretch the menu across the
               *  session list instead of wrapping. */}
              <DropdownMenuContent align="end" className="min-w-[200px] max-w-[264px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
                {/* The plain chat is what the button's main segment does, but a
                 *  menu that lists every OTHER way to create and omits the
                 *  ordinary one reads as if the other kinds were the only ones
                 *  the caret can make. Listed first so the default stays the
                 *  default. */}
                <DropdownMenuItem data-testid="new-plain-chat" disabled={creatingSlot} {...offlineMenuRow(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_chat'), () => { createChatMutation.mutate({ inNewTab: false }) })}>
                  <MessageSquarePlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat')}
                </DropdownMenuItem>
                {/* The two private modes sit at the top level beside New chat,
                 *  each naming its own action and what it does with memory. They
                 *  used to share a submenu named for a word no other surface uses,
                 *  which at phone width collapsed into a caption that read as a
                 *  button. */}
                <DropdownMenuItem className="items-start" data-testid="new-incognito-chat" disabled={creatingSlot} {...offlineMenuRow(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_incognito_chat'), () => { createEphemeralChatMutation.mutate('incognito') })} {...uiLocation('sessions.create-menu.incognito')}>
                  <PrivateChatRow kind="incognito">{i18nT('pages.chatSidebar.new_incognito_chat')}</PrivateChatRow>
                </DropdownMenuItem>
                <DropdownMenuItem className="items-start" data-testid="new-temporary-chat" disabled={creatingSlot} {...offlineMenuRow(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_temporary_chat'), () => { createEphemeralChatMutation.mutate('temporary') })} {...uiLocation('sessions.create-menu.temporary')}>
                  <PrivateChatRow kind="temporary">{i18nT('pages.chatSidebar.new_temporary_chat')}</PrivateChatRow>
                </DropdownMenuItem>
                {/* Import creates a session too, so it sits with the create rows
                 *  rather than only in a per-session ⋯ menu that has to be opened
                 *  on some unrelated session first. */}
                <ImportSessionItem Item={DropdownMenuItem} />
                {/* Crew Members is a DOOR, not a create action: it navigates to the
                 *  Members page (or, while that page is preview-gated, to the
                 *  Settings card that turns it on — see `openCrewMembers`). It sits
                 *  among the create entries because this menu is where "crew" was
                 *  offered until Crew Mode retired, so it is where a returning user
                 *  looks. Not disabled by `creatingSlot`: it creates nothing.
                 *
                 *  CAPTURED: the Feature Previews "See what it looks like" dialog
                 *  shows the Members page this entry opens. A visible change to
                 *  that page makes the picture stale — re-shoot with
                 *  `scripts/capture-feature-previews.mjs`.
                 *
                 *  Separators on BOTH sides: every other row here creates something and is
                 *  named "New …"; this one navigates and is not. Without the rule a
                 *  reader parsed it as an unnamed create action on every menu open
                 *  (UX review on #9519). It sits between the session rows and the
                 *  folder rows, in a group of its own. */}
                <DropdownMenuSeparator />
                <DropdownMenuItem className="items-start" data-testid="open-crew-members" onClick={openCrewMembers}>
                  <Users size={14} className="text-muted mt-[3px] shrink-0" />
                  <span className="flex min-w-0 flex-col gap-px">
                    <span>{i18nT('pages.chatSidebar.open_crew_members')}</span>
                    <span className="whitespace-normal text-[11px] leading-snug text-muted">{crewPreview ? i18nT('pages.chatSidebar.open_crew_members_desc') : i18nT('pages.chatSidebar.open_crew_members_gated_desc')}</span>
                  </span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem data-testid="new-folder" {...offlineMenuRow(connected, i18nT('utils.offline.create_folders'), i18nT('pages.chatSidebar.new_folder'), () => { setFolderModal({ mode: 'create', parentId: '' }) })} {...uiLocation('sessions.create-menu.new-folder')}>
                  <FolderPlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_folder')}
                </DropdownMenuItem>
                {folders.length > 0 && (() => {
                  const folderRows = (() => {
                    const roots = folders.filter(f => !f.parent_id).sort(folderCompare)
                    const childrenOf = (pid: string) => folders.filter(f => f.parent_id === pid).sort(folderCompare)
                    const items: { f: ChatFolder; depth: number }[] = []
                    const walk = (list: ChatFolder[], depth: number) => { for (const f of list) { items.push({ f, depth }); walk(childrenOf(f.id), depth + 1) } }
                    walk(roots, 0)
                    return items.map(({ f, depth }) => (
                      <DropdownMenuItem key={f.id} style={{ paddingLeft: `${12 + depth * 16}px` }} {...offlineMenuRow(connected, i18nT('utils.offline.create_sessions'), f.name, () => { createChatInFolder(f.id, { focus: true }) })}>
                        <Folder size={14} className={depth === 0 ? 'text-muted' : 'text-muted/60'} /> {f.name}
                      </DropdownMenuItem>
                    ))
                  })()
                  // A flyout has nowhere to open at phone width (Radix pins a
                  // submenu to the trigger's side and only shifts it vertically),
                  // so on a phone the folders are listed inline under a caption.
                  // The caption carries no icon: drawn with the same Folder glyph
                  // as the rows under it, it read as one more tappable row. Text
                  // alone, in the FILTER / SORT BY caption style, marks a heading.
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuSeparator />
                        <DropdownMenuLabel data-testid="new-chat-in-folder-caption" className="text-[11px] uppercase tracking-[.04em]">
                          {i18nT('pages.chatSidebar.new_chat_in_folder')}
                        </DropdownMenuLabel>
                        <div className="max-h-[240px] overflow-y-auto">{folderRows}</div>
                      </>
                    )
                  }
                  return (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger className="data-[disabled]:pointer-events-none data-[disabled]:opacity-50">
                      <Folder size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat_in_folder')}
                      <ChevronRight size={13} className="ml-auto text-muted" />
                    </DropdownMenuSubTrigger>
                    {/* Intentional tighter cap composed via min() with the
                        primitive's available-height var: 300px keeps the folder
                        list submenu compact while preserving the viewport
                        never-clip floor (a bare max-h would override the
                        primitive, since cn()'s tailwind-merge dedupes max-h-*).
                        overflow is left to the primitive. */}
                    <DropdownMenuSubContent className="max-h-[min(300px,var(--radix-dropdown-menu-content-available-height))]">
                      {folderRows}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                  )
                })()}
                {/* "New chat on crew" — the same shape as "New chat in folder"
                 *  above (dynamic rows behind one submenu, listed inline at phone
                 *  width where a Radix flyout has nowhere to open), because it
                 *  answers the same kind of question. The row is absent, not
                 *  disabled, when no crew holds a live tunnel: a disabled row
                 *  would advertise a capability the install may never have. It
                 *  sits AFTER the folder rows so the local ways to create keep
                 *  their position.
                 *
                 *  Preview-gated on its OWN flag (`utils/previewFlags.ts`), not
                 *  the Crew Members page's: the landing is what is unfinished, since the
                 *  created session opens in that crew's pane and the local list
                 *  does not yet show live remote sessions. Toggle lives in
                 *  Settings > Remote Crew. */}
                {remoteCrewChatPreview && warmCrews.length > 0 && (() => {
                  // The picked row swaps its icon for a spinner and its label for
                  // "Starting chat on <crew>…": the create opens a session on the peer
                  // before the local one exists, so it takes seconds, and the menu
                  // stays open (onSelect preventDefault) the whole time, so the row
                  // is where the eye already is.
                  const crewRows = warmCrews.map(c => {
                    const pendingHere = createRemoteChatMutation.isPending && createRemoteChatMutation.variables === c.id
                    return (
                      <DropdownMenuItem key={c.id} data-testid={`new-chat-on-crew-${c.id}`}
                        // The picked row is the progress cue, so it is not disabled
                        // (a disabled row is drawn faded); the guard in onSelect is
                        // what refuses a second pick while the create runs.
                        disabled={createRemoteChatMutation.isPending && !pendingHere}
                        aria-busy={pendingHere || undefined}
                        onSelect={e => {
                          e.preventDefault()
                          if (createRemoteChatMutation.isPending) return
                          createRemoteChatMutation.mutate(c.id)
                        }}>
                        {pendingHere
                          ? <Loader2 size={14} className="text-info animate-spin" aria-hidden="true" data-testid={`new-chat-on-crew-spinner-${c.id}`} />
                          : <Server size={14} className="text-info" />}{' '}
                        {pendingHere ? i18nT('pages.chatSidebar.creating_on_crew', { name: c.name }) : c.name}
                      </DropdownMenuItem>
                    )
                  })
                  // Inline failure reason (version mismatch, tunnel down), shown
                  // through the shared ErrorNotice (website AGENTS.md forbids a
                  // hand-written text-danger div for a rejected mutation). Kept in
                  // the menu because the create leaves nothing behind on failure —
                  // closing would erase the only signal; `onSelect preventDefault`
                  // on the rows keeps a failed create from auto-closing over it.
                  // The sibling menu item is the keyboard-reachable hand-off in
                  // both the mobile inline list and the desktop submenu.
                  const errRow = remoteCrewError
                    ? (
                      <>
                        <div className="px-2 py-1.5">
                          <ErrorNotice
                            id={remoteCrewErrorId}
                            message={remoteCrewError}
                            variant="inline"
                            testId="new-chat-on-crew-error"
                          />
                        </div>
                        <ErrorNoticeMenuItem
                          Item={DropdownMenuItem}
                          message={remoteCrewError}
                          describedBy={remoteCrewErrorId}
                        />
                      </>
                    )
                    : null
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2">
                          <Server size={13} className="text-info" /> {i18nT('pages.chatSidebar.new_chat_on_crew')}
                        </DropdownMenuLabel>
                        <div className="max-h-[240px] overflow-y-auto">{crewRows}{errRow}</div>
                      </>
                    )
                  }
                  return (
                    <DropdownMenuSub>
                      <DropdownMenuSubTrigger data-testid="new-chat-on-crew" className="data-[disabled]:pointer-events-none data-[disabled]:opacity-50">
                        <Server size={14} className="text-info" /> {i18nT('pages.chatSidebar.new_chat_on_crew')}
                        <ChevronRight size={13} className="ml-auto text-muted" />
                      </DropdownMenuSubTrigger>
                      {/* Intentional tighter cap composed via min() with the
                          primitive's available-height var: 300px keeps the crew
                          list submenu compact while preserving the viewport
                          never-clip floor (a bare max-h would override the
                          primitive, since cn()'s tailwind-merge dedupes max-h-*).
                          overflow is left to the primitive. */}
                      <DropdownMenuSubContent className="max-h-[min(300px,var(--radix-dropdown-menu-content-available-height))]">
                        {crewRows}{errRow}
                      </DropdownMenuSubContent>
                    </DropdownMenuSub>
                  )
                })()}
                {/* Last, like every gated menu: the dimmed rows say something is
                    unavailable, this says why. */}
                <OfflineMenuReason testId="new-menu-offline-reason" />
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>
  )
}

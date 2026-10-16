/** The tree lane's folder blocks: each folder's header row (glyph, name, count,
 *  hover cluster), its two menus, its body with the pinned conductor card, the
 *  sorted and dormant rows and the hidden-folder reveal, and the folder-scoped
 *  create-failure notice. Built once per shell render (`createFolderBlocks`). */
import { Fragment } from 'react'
import { X, Eye, EyeOff, FolderPlus, CornerUpRight, MessageSquarePlus, Pencil, Link2, MoreVertical, Settings } from 'lucide-react'
import { FolderBody } from '../../components/FolderBody'
import { FolderRail } from '../../components/FolderRail'
import ErrorNotice from '../../components/ErrorNotice'
import FolderGlyph from '../../components/FolderGlyph'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '../../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator } from '../../components/ui/context-menu'
import { switchSlot } from '../../store/chatSlice'
import { folderOffersHide } from '../../utils/folderVisibility'
import { highlightText } from '../../utils/highlightText'
import { Input } from '../../components/ui'
import FolderMoveSubmenu from '../../components/FolderMoveSubmenu'
import { ChannelBrandIcon, hasChannelBrandIcon } from '../../components/ChannelBrandIcon'
import { DndDroppable } from '../../components/dnd'
import { collectFolderSubtreeIds, folderNameText } from '../../utils/folderTree'
import type { ChatFolder } from '../../types'
import { i18nT } from '../../i18n/t'
import { PrivateChatRow } from './PrivateChatRow'
import { ParentFolderDropTarget, SortableSubfolderBlock } from './dnd/targets'
import type { Slot } from './types'
import { detectFolderConductor } from './folderConductor'
import { DisclosureChevron, FOLDER_BODY_CLS, FOLDER_BODY_OPEN_PADDING, FOLDER_ROW_STICKY_Z, PinnedSessionDivider, isOpenInTabModifierClick } from './chrome'
import type { LaneRows } from './laneRows'
import type { SidebarScope } from './scope'
import OfflineMenuReason from '../../components/OfflineMenuReason'
import { offlineMenuRow, offlineProps } from '../../utils/offline'

/** The folder block renderers for one shell render. */
export function createFolderBlocks(shell: SidebarScope & LaneRows) {
  const { activeDrag, armHold, boardLaneActive, connected, createChatInFolder, deleteFolderWithConfirm,
    disarmHold, dispatch, editName, editScope, editingId, filterHiddenFolders, folderCompare,
    folderConductorPin, folderCreateError, folderDragWithheld, folderEditInputRef, folderNameMatchIds,
    folderReorderable, folderSortMode, folderSubtrees, folders, foldersWithActiveSubtree, heldLane,
    hiddenByContainer, hideEmptyFolderBody, ime, instantCloseId, isActiveRow, isFolderFilteredOut,
    isFolderHidden, listNarrowed, moveFolderTo, onMenuCloseAutoFocus, onOpenSlotInNewTab, onSelectSlot,
    pinned, renameCommit, renderSessionRow, renderStaleSection, revealFlash, revealedContainers,
    searchRanked, setEditName, setEditScope, setEditingId, setFolderCreateError, setFolderModal, setFolderOfflineNotice, sidebarRows,
    slotFilter, slotFolders, splitStale, staleExpanded, startsAutomaticSection, suppressMenuRestoreRef,
    toggleCollapse, toggleFolderFilter, toggleReveal, unreadSet, updateFolderMutation } = shell
  /**
   * The session CONDUCTING a folder, derived from the folder's own rows.
   *
   * Read once, by the folder's menu, to decide whether that menu offers the
   * chat item and to name the session it opens. A folder whose rows resolve no
   * conductor gets `null` and its menu is the menu on main, which is why no
   * other branch in this file had to change: the ordinary folder row is still
   * the only shape this lane draws.
   *
   * The whole row comes back rather than its key, because the item names the
   * session it opens: a label that says only "conductor" leaves a first-time
   * reader with no idea what the click does.
   *
   * Computed per render rather than memoized. `sidebarRows` is a fresh object
   * every render, so a memo keyed on it would recompute anyway and only add a
   * cache to read; the work is one pass over each folder's own rows.
   */
  /**
   * This folder's conductor AS A ROW ON SCREEN, or null. The one place either
   * caller -- the folder menu's item and the pinned card -- asks the question.
   *
   * Two sets, and they are different on purpose. Detection runs over
   * `allRowsIn`, the folder's whole membership: which session opened the others
   * is a fact about the folder, and reading it off the FILTERED lane meant a
   * search or a status hide that concealed the real root promoted whichever row
   * was left on top -- so the menu claimed a worker was "the chat that started
   * this folder's other chats", which is false, and with the preview flag on
   * that wrong card was pinned and tagged.
   *
   * The result is then required to be in `rowsIn`, the rows this folder draws,
   * because both callers put it on screen: offering a row the filter concealed
   * would either open a pane the list says is not there or re-admit a row the
   * person asked not to see. A filter that hides the root hides the FEATURE --
   * no item, no pin -- rather than moving it to a different session.
   */
  const folderConductorOf = (folderId: string): Slot | null => {
    const key = detectFolderConductor(sidebarRows.folderTree.allRowsIn(folderId))
    if (key == null) return null
    return sidebarRows.folderTree.rowsIn(folderId).find(r => r.key === key) ?? null
  }

  const toggleListFolderCollapse = (folder: ChatFolder, from: HTMLElement) => {
    if (folder.collapsed) disarmHold()
    else armHold(folder.id, from.closest<HTMLElement>('[data-folder-drop]'))
    toggleCollapse(folder.id)
  }

  // ── Folder row: matches session-row width (full width minus drawer padding) ──
  // Recursively check if a folder or any descendant contains an unread slot.
  const folderTreeHasUnread = (folderId: string, visited = new Set<string>()): boolean => {
    if (visited.has(folderId)) return false
    visited.add(folderId)
    for (const k of unreadSet) { if (slotFolders[k] === folderId) return true }
    return folders.some(f => f.parent_id === folderId && folderTreeHasUnread(f.id, visited))
  }

  // Inline failure notice for a folder-scoped create, rendered directly under
  // the folder's header row through the shared ErrorNotice surface (AUTOSDE
  // errors-use-error-notice): it carries the role="alert", the design tokens,
  // the dismiss affordance, and the agent hand-off. askAgent is on because the
  // hand-off destroys nothing here — the sidebar holds no unsaved draft (the
  // rename Input commits on blur) and survives the navigation. `columnId`
  // scopes board-view rendering to the column the create was issued from, so
  // a root folder repeated across columns announces ONE alert, under a
  // column-unique test id.
  // True when the tree lane will NOT render the folder's header (and so its
  // per-folder notice mount): the folder or an ancestor is hidden or filtered
  // out, and not currently revealed via the "N hidden folders" peek (whose
  // container key is the parent id, or 'root' at top level). Mirrors the
  // exclusion applied at visibleRootFolders / renderFolderBlock's child
  // filter, so the tree-lane fallback below renders exactly when the scoped
  // mount cannot.
  const folderCreateMountAbsent = (folderId: string): boolean => {
    let cur = folders.find(f => f.id === folderId)
    // A folder that no longer exists (deleted while its create was in flight)
    // has no header anywhere by definition — the strongest mount-absent case.
    if (!cur) return true
    const seen = new Set<string>()
    while (cur) {
      if (seen.has(cur.id)) break
      seen.add(cur.id)
      const revealed = revealedContainers.has(cur.parent_id || 'root')
      if ((isFolderHidden(cur) && !revealed) || isFolderFilteredOut(cur)) return true
      cur = cur.parent_id ? folders.find(f => f.id === cur!.parent_id) : undefined
    }
    return false
  }

  const renderFolderCreateError = (folderId: string, columnId?: string): React.ReactNode => {
    if (!folderCreateError || folderCreateError.folderId !== folderId) return null
    // Ownership: an exact columnId match wins (board columns each render the
    // folder, so scoping prevents N duplicate alerts). Outside board view the
    // single tree mount owns EVERY error for its folder — including one whose
    // columnId outlived its column or its view (user switched back to tree).
    const owns = folderCreateError.columnId === columnId || (columnId === undefined && !boardLaneActive)
    if (!owns) return null
    return (
      <div className="px-2 py-1">
        {/* inline variant with flex-wrap: the sidebar drawer is ~250px wide,
         *  and both stock single-row layouts squeeze the message to a sliver
         *  beside the Ask-agent / dismiss controls. Wrapping lets the message
         *  take the full line and the controls fold under it. */}
        <ErrorNotice
          message={folderCreateError.message}
          title={folderCreateError.title}
          report={folderCreateError.report}
          variant="inline"
          askAgent
          onDismiss={() => setFolderCreateError(null)}
          testId={columnId ? `col-${columnId}-folder-create-error-${folderId}` : `folder-create-error-${folderId}`}
          className="flex-wrap w-full"
        />
        {/* Direct remedy for the stale-directory case: open Folder settings
         *  right here instead of describing a hover-only menu glyph. On its
         *  own line, never as a row peer of the notice's Ask-agent/dismiss
         *  pair (the two-buttons-per-row cap) — same pattern as the
         *  PullRequestPanel remedy link. */}
        {folderCreateError.offerSettings && (
          <div className="mt-0.5">
            <button type="button"
              className="text-[11px] font-medium text-danger/80 hover:text-danger bg-transparent border-none p-0 cursor-pointer underline decoration-danger/30 hover:decoration-danger underline-offset-2"
              data-testid={`folder-create-error-settings-${folderId}`}
              onClick={() => { setFolderModal({ mode: 'edit', folderId }); setFolderCreateError(null) }}>
              {i18nT('components.folderConfigModal.folder_settings')}
            </button>
          </div>
        )}
      </div>
    )
  }

  /**
   * While the search box narrows the list, does this folder's subtree still put
   * ANYTHING on screen? Answered without rendering, because the render cannot
   * answer it: a nested block's `[]` is returned from inside the subfolder
   * wrapper's own render, which runs after `childNodes.push` has already committed
   * the wrapper — so `childNodes.length > 0` reads true for a subtree that draws
   * nothing, and both the drop gate and the header count believed it.
   *
   * That is not cosmetic. Searching "archive" kept `Sydney Property` on screen
   * wearing the count `1` — the `1` being a subfolder that did not render —
   * directly above the note saying no sessions matched. The row named nothing the
   * query asked for and the number contradicted the sentence beneath it.
   *
   * Each clause mirrors one thing `renderFolderBlock` actually draws, so the
   * predicate cannot drift from the render: own surviving sessions, the query
   * having named this folder (`folderNameMatchIds` already covers a matched
   * folder's whole subtree), the create-failure notice this folder owns, the
   * "N hidden folders" peek row filed in this container, and recursively any
   * child folder the tree is willing to draw. `visited` is the same cycle guard
   * `renderFolderBlock` carries, for the same reason: `parent_id` comes off disk.
   */
  const narrowedSubtreeShowsSomething = (folder: ChatFolder, visited = new Set<string>()): boolean => {
    if (visited.has(folder.id)) return false
    visited.add(folder.id)
    if (sidebarRows.folderTree.rowsIn(folder.id).length > 0) return true
    if (folderNameMatchIds?.has(folder.id)) return true
    if (folderCreateError?.folderId === folder.id) return true
    if (hiddenByContainer.get(folder.id)?.length) return true
    return folders.some(f => f.parent_id === folder.id
      && !isFolderHidden(f) && !isFolderFilteredOut(f)
      && narrowedSubtreeShowsSomething(f, visited))
  }

  /**
   * The child folders this container will draw, in the order it draws them — the
   * narrow's verdict included. Sorted, not raw array order: a subfolder's `order`
   * is set by a drag AND by chat_folder_move's before/after, and the cache order
   * reflects neither.
   */
  const drawableChildFolders = (folder: ChatFolder): ChatFolder[] =>
    folders.filter(f => f.parent_id === folder.id
      && !isFolderHidden(f) && !isFolderFilteredOut(f)
      && (!listNarrowed || narrowedSubtreeShowsSomething(f)))
      .sort(folderCompare)

  // The folder's action items, rendered once for BOTH surfaces that open them:
  // the row's ⋯ button (a DropdownMenu) and a right-click on the row (a Radix
  // ContextMenu, which positions itself at the pointer). Radix menu items only
  // work inside their own primitive family, so the family is picked from
  // `variant` -- the same shape SessionActionsMenu and FolderMoveSubmenu use.
  // `data-testid`s carry a `-ctx` suffix in the context variant so a test can
  // tell the two copies apart when both are mounted for one folder.
  const renderFolderMenuItems = (folder: ChatFolder, reparentTargets: readonly ChatFolder[], variant: 'dropdown' | 'context') => {
    const ctx = variant === 'context'
    const Item = ctx ? ContextMenuItem : DropdownMenuItem
    const Separator = ctx ? ContextMenuSeparator : DropdownMenuSeparator
    const tid = (name: string) => `folder-${name}-${folder.id}${ctx ? '-ctx' : ''}`
    const privateChatRows = (
      <>
        {/* Menu create entries take NO open-in-tab gesture (#10575,
         *  scoped out): a menu closes on select, and Radix keyboard
         *  activation synthesizes a modifier-free click, so the
         *  gesture would be mouse-only and undiscoverable. */}
        <Item className="items-start" data-testid={tid('new-incognito')} {...offlineMenuRow(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_incognito_chat'), () => { createChatInFolder(folder.id, { memoryMode: 'incognito' }) })}>
          <PrivateChatRow kind="incognito" />
        </Item>
        <Item className="items-start" data-testid={tid('new-temporary')} {...offlineMenuRow(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_temporary_chat'), () => { createChatInFolder(folder.id, { memoryMode: 'temporary' }) })}>
          <PrivateChatRow kind="temporary" />
        </Item>
      </>
    )
    // The conductor's own chat, reachable from the folder row instead of from an
    // expand-and-scan. The ROW now carries a button for it, beside the ⋯ menu
    // and new-chat: that puts it over the two-controls-per-row cap, which is
    // exceeded deliberately for this one case because the folder row is where a
    // reader is looking when they want that conversation. The excess is bounded
    // to a folder that resolves a conductor; every other row stays capped.
    //
    // This menu entry stays, and is not a fallback for the button. It is the
    // right-click path -- a reader who opens the row's context menu finds the
    // same action there, named identically -- and the keyboard path on a device
    // that reveals the hover cluster only on focus. Both menus render from here,
    // so the ⋯ menu and the right-click menu cannot drift apart.
    //
    // A folder whose rows resolve no conductor gets nothing added, in the menu
    // or on the row: the session cards and the folder body are what they are on
    // main.
    //
    // The label names the FOLDER and the second line says what the chat it
    // opens is to that folder. A label carrying the word "conductor" and
    // nothing else left a first-time reader unable to say what the click opens,
    // and the two other chat affordances on the same row made that worse rather
    // than clearer. The two-line shape is the one the ephemeral-chat items in
    // the header's New menu already use.
    //
    // It names the folder rather than the conductor's own title because the
    // folder is what the reader has in front of them: they opened this menu on
    // that row. The conductor's title is a session name they have not
    // necessarily seen, and reading one in a menu hung off a different name made
    // the item look like it belonged to something else.
    const conductor = folderConductorOf(folder.id)
    return (
      <>
        {conductor && (
          <Item className="items-start" data-testid={tid('conductor-chat')} onClick={() => {
            // The same offline gate every session-row click has (see the row's
            // own `onClick`/`onKeyDown`). Switching slots while the backend is
            // down selects a session nothing can load, so the click would close
            // the menu onto an empty pane with no way back to the row.
            if (!connected) return
            dispatch(switchSlot({ key: conductor.key, announceOnMissing: true }))
            onSelectSlot?.(conductor.key)
          }}>
            <CornerUpRight size={14} className="text-accent mt-[3px] shrink-0" />
            <span className="flex min-w-0 flex-col gap-px">
              <span className="truncate">{i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}</span>
              <span className="whitespace-normal text-[11px] leading-snug text-muted">{i18nT('pages.chatSidebar.chat_with_folder_desc')}</span>
            </span>
          </Item>
        )}
        <Item data-testid={tid('rename')} {...offlineMenuRow(connected, i18nT('utils.offline.rename_folders'), i18nT('pages.chatSidebar.rename'), () => { suppressMenuRestoreRef.current = true; setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) })}><Pencil size={13} /> {i18nT('pages.chatSidebar.rename')}</Item>
        <Item data-testid={tid('new-subfolder')} {...offlineMenuRow(connected, i18nT('utils.offline.create_folders'), i18nT('pages.chatSidebar.new_subfolder'), () => { setFolderModal({ mode: 'create', parentId: folder.id }) })}><FolderPlus size={13} /> {i18nT('pages.chatSidebar.new_subfolder')}</Item>
        {/* The two private modes are listed flat, each naming its own action
         *  and what it does with memory. A grouping caption read as a button
         *  at phone width and named the pair with a word no other surface
         *  uses, so neither a caption nor a submenu stands between the user
         *  and the choice. */}
        <Separator />
        {privateChatRows}
        <Separator />
        {/* Re-parent: move this folder under another folder or back to the
         *  top level. Self + descendants are excluded (cycle guard). */}
        <FolderMoveSubmenu variant={variant} label={i18nT('pages.chatSidebar.move_folder_to')} sortMode={folderSortMode}
          folders={reparentTargets}
          currentFolderId={folder.parent_id || null}
          // Dimmed with its siblings; `moveFolderTo` refuses the pick at the sink.
          offline={!connected}
          offlineTitle={i18nT('utils.offline.gateway_offline_reconnect', { action: i18nT('utils.offline.move_folders') })}
          onPick={pid => moveFolderTo(folder.id, pid)} />
        <Item data-testid={tid('settings')} {...offlineMenuRow(connected, i18nT('utils.offline.change_folder_settings'), i18nT('components.folderConfigModal.folder_settings'), () => { setFolderModal({ mode: 'edit', folderId: folder.id }) })}><Settings size={13} /> {i18nT('components.folderConfigModal.folder_settings')}</Item>
        {/* Hide this folder from the session lists (flat lane + tree).
         *  Same state the filter menu's checkboxes drive, reached from the
         *  folder itself — which is where the user is looking when they
         *  decide a folder is noise. Distinct from "Hide when empty"
         *  below, which is a server-persisted archive affordance. */}
        <Item data-testid={tid('visibility')} onClick={() => { toggleFolderFilter(folder.id) }}>
          {filterHiddenFolders.has(folder.id)
            ? <><Eye size={13} /> {i18nT('pages.chatSidebar.show_folder')}</>
            : <><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_folder')}</>}
        </Item>
        {folderOffersHide(folder, foldersWithActiveSubtree) && (
          <Item data-testid={tid('hide')} {...offlineMenuRow(connected, i18nT('utils.offline.hide_folders'), i18nT('pages.chatSidebar.hide_when_empty'), () => { updateFolderMutation.mutate({ id: folder.id, body: { hidden: true } }) })}><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_when_empty')}</Item>
        )}
        <Separator />
        <Item className="text-danger focus:text-danger" data-testid={tid('delete')} {...offlineMenuRow(connected, i18nT('utils.offline.delete_folders'), i18nT('pages.chatSidebar.delete_folder'), () => { deleteFolderWithConfirm(folder) })}><X size={13} /> {i18nT('pages.chatSidebar.delete_folder')}</Item>
        {/* Last, so its mount on disconnect shifts no row under the pointer. */}
        <OfflineMenuReason testId={ctx ? 'folder-offline-reason-ctx' : 'folder-offline-reason'} />
      </>
    )
  }

  const renderFolderHeader = (folder: ChatFolder, dragHandleProps?: React.HTMLAttributes<HTMLElement>, emptyBody = false, depth = 0) => {
    // Same predicate `renderFolderBlock` renders by, so the number describes what
    // the row can actually show. Counting a hidden-when-empty child made the count
    // and the body disagree: the body skipped it, so no body rendered, while the
    // count still said 1 - and the row then presented as a toggle with nothing to
    // toggle. A folder the user hid is a folder they asked not to see, so it is
    // not part of what this row holds.
    //
    // `drawableChildFolders` carries the narrow's verdict for the same reason: with
    // a search active, a child whose whole subtree draws nothing is not part of
    // what this row holds either, and counting it printed a number the body below
    // could not account for.
    const childFolders = drawableChildFolders(folder)
    // The row model's folder index: a peer row is never in a folder, so a peer key
    // colliding with a local one cannot count a session this machine does not own
    // toward the folder it does.
    const childSlots = sidebarRows.folderTree.rowsIn(folder.id)
    const count = childSlots.length + childFolders.length
    const collapsed = !!folder.collapsed
    // One derivation, not two: `renderFolderBlock` decides the body from the nodes
    // it renders, and this row follows that decision. With the count now built
    // from the same predicate, `count === 0` agrees with it by construction rather
    // than by a guard that had to pick which way to fail.
    const emptyRow = emptyBody
    // `button` when the row toggles something, a plain `span` when it does not.
    const HeaderShell = (emptyRow ? 'span' : 'button') as 'button'
    const hasUnread = folderTreeHasUnread(folder.id)
    const focusListFolderRow = () => {
      document.querySelector<HTMLElement>(`[data-folder-row="${folder.id}"]`)?.focus({ preventScroll: true })
    }
    const draggable = !!dragHandleProps && editingId !== folder.id
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    // Reveal confirmation for THIS folder. The `kind` check is what keeps a
    // session reveal from lighting up a folder whose id equals that slot key.
    const folderFlash = revealFlash?.kind === 'folder' && revealFlash.key === folder.id
      ? (revealFlash.fading ? 'fade' : 'flash')
      : null
    // The row's own chat control, or null for a folder that resolves no
    // conductor. The SAME selector the menu item asks, so the button and the
    // menu entry can never offer different sessions -- and null here means the
    // cluster emits its two controls and nothing else.
    const folderRowConductor = folderConductorOf(folder.id)
    return (
      // Right-click (or long-press) anywhere on the row opens the SAME menu the
      // ⋯ button does, positioned at the pointer. The ⋯ button stays: it is the
      // keyboard-reachable path, and the only one on a device with no secondary
      // button. Rename mode opts out so a right-click in the name input keeps the
      // browser's own edit menu (cut/paste).
      <ContextMenu key={`folder-header-${folder.id}`}>
        <ContextMenuTrigger asChild disabled={editingId === folder.id && editScope === 'list'}>
      <div
        // The reveal target for this folder (command palette Folders tab), and the
        // only marker that identifies a folder ROW. Deliberately not the existing
        // `data-folder-drop`: that one is a drop zone and is rendered once per
        // BOARD COLUMN as well as here, so `querySelector` would return whichever
        // copy sorts first in the DOM — the same ambiguity the session reveal
        // avoids by targeting `data-session-row` instead of `data-slot-key`.
        data-folder-row={folder.id}
        tabIndex={-1}
        // Non-interactive container (role="group"): the row holds a collapse
        // toggle button + action buttons, so it must NOT itself be a button —
        // an interactive element can't legally contain other interactive
        // elements (invalid ARIA), and a folder row is a grouping, not an action.
        role="group"
        aria-label={i18nT('pages.chatSidebar.folder_2', { name: folder.name })}
        // The whole header is the drag-to-reorder handle (pointer listeners only,
        // no role override). 8px activation distance keeps the collapse toggle
        // and action buttons clickable; drag is off while renaming.
        {...(draggable ? dragHandleProps : {})}
        // `pl-[3px]` (3px), with no inline left-pad override. Deliberately LESS than
        // the session rows' pad, so the glyph outdents into the row gutter while the
        // folder name lands on its sessions' text. The pad is NOT free: the guides
        // below fix it, so nothing else may claim it. That is why the unread dot
        // sits inline on the right, not in an absolutely-positioned left gutter
        // (a gutter wide enough for it would break guide 3).
        //
        // With H = this header's box left, D = `FOLDER_BODY_INSET_PX` 2 — the
        // nested body's own left inset, applied by `FolderBody` so its collapse
        // animation does not clip. It is invisible in the class list, which is
        // exactly why four revisions derived this geometry from Tailwind classes
        // and each landed 2px out. It is now a named, exported constant that the
        // alignment test imports and asserts against the rendered padding, so it
        // is no longer a free empirical term.
        // P = this `px-3.5` 14,
        // G = glyph 14, g = `gap-[5px]` 5, M = body `ml-3` 12, B = 1px border,
        // p = body `pl-1` 4, R = session row `pl-3.5` 14 (the same pad in the root
        // lane and inside every folder body):
        //
        //   GUIDE 1  glyph sits ON the connector line        P = D + M
        //   GUIDE 2  name == agent / title / tool-call sub   P + G + g = D + M + B + p + R
        //   GUIDE 3  nested glyph == sibling content column  P = R
        //
        //   14 = 2 + 12      14 + 14 + 5 = 2 + 12 + 1 + 4 + 14      14 = 14
        //
        // Each nesting level costs D + M + B + p = 19px. This is the maintainer's
        // decided geometry (#3905; docs/decisions/2026-10-09-folder-glyph-sits-on-
        // its-connector-line.md). A tighter indent that outdents the glyph off its
        // connector line (#14717) was reverted by that decision: the glyph on the
        // line is the picture, and width for titles is not bought by moving it.
        //
        // All three hold at EVERY depth and in the root lane: the algebra has no
        // per-depth term, so depth 3 nests exactly as depth 2 does.
        //
        // Measured on the built SPA (x in CSS px), NOT derived — a paper estimate
        // of these same numbers was 3px out: root glyph 248, root-lane session
        // content 255; depth 1 glyph 258, name and all three text lines 264;
        // depth 2 name/content 274; depth 3 content 284.
        //
        // Computing these guides from class names without D breaks them (moving
        // the name past the content, or leaving a nested glyph short). Re-measure with
        // `website/scripts/capture-folder-glyph.mjs` under MEASURE=1 — never
        // re-derive on paper.
        // A row with no body does not light up on hover. The highlight is this
        // sidebar's "this row is pressable" signal, and a row that toggles nothing
        // wearing the same one is the whole reason the previous round's dead click
        // read as broken. Its cluster is already visible at rest, so hover has
        // nothing left to reveal here either.
        // Pinned to the top of the lane while any of its folder is on screen: the
        // header is `sticky` inside its own folder block (the drop container that
        // holds header + body), so it rides the top edge until the block's end
        // pushes it off, and the next folder's header takes over. A nested header
        // pins one row lower per depth so the whole ancestor path stays readable,
        // and a shallower header paints above a deeper one as it is pushed out.
        // The opaque surface and the row height live in index.css
        // (`.folder-row-sticky`); `sticky` also serves as the containing block
        // the old `relative` provided for the absolutely-positioned children.
        style={{ top: `calc(var(--folder-row-sticky-h) * ${depth} - var(--folder-row-sticky-inset))`, zIndex: FOLDER_ROW_STICKY_Z - depth }}
        className={`folder-row folder-row-sticky group sticky flex items-center gap-2 px-3.5 py-1.5 rounded-md text-sm text-muted transition-all${emptyRow ? '' : ' hover:text-text hover:bg-bg-hover'} ${draggable ? 'cursor-grab active:cursor-grabbing' : ''}${folderFlash ? ` session-reveal-flash${folderFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}>
        {editingId === folder.id && editScope === 'list' ? (
          <>
            <FolderGlyph color={folder.color} icon={folder.icon} size={14} open={!collapsed} />
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[13px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => { if (renameCommit(folder.id, editName)) requestAnimationFrame(focusListFolderRow) }, onEscape: () => { setEditingId(null); requestAnimationFrame(focusListFolderRow) }, onBlur: () => renameCommit(folder.id, editName) })} />
            <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
          </>
        ) : (
          <>
            {/* The collapse toggle is the real interactive control — a native
             *  <button> (keyboard-operable for free), filling the row so clicking
             *  the folder glyph/name still toggles.  Double-click the name renames. */}
            {/* A folder with no body has nothing to toggle, so on an empty row this
             *  is not a control at all: no button role, no tab stop, no pointer
             *  cursor, no expanded state to announce and no handler. Keeping the
             *  <button> and neutering its handler is the worst of the options - a
             *  focusable control that looks clickable and does nothing. The name
             *  still double-click renames and the row's own cluster still creates
             *  and opens the menu, so the row keeps every action it can honour. */}
            <HeaderShell
              className={`flex items-center gap-[5px] flex-1 min-w-0 bg-transparent border-none text-left text-inherit p-0${emptyRow ? '' : ' cursor-pointer'}`}
              {...(emptyRow
                // Nothing to disclose, but the row is still a DRAG HANDLE while it
                // is draggable, and this shell is the only focusable thing inside
                // it: the sortable `listeners` sit on the row div, which has no tab
                // stop of its own, so keyboard activation reaches them by bubbling
                // from here. Swapping the <button> for a <span> without this took
                // keyboard reordering away from empty folders in the list view -
                // the same hole the board row had, through a different door.
                ? (draggable ? {
                  role: 'button',
                  tabIndex: 0,
                  'aria-label': i18nT('pages.chatSidebar.folder_2', { name: folder.name }),
                  'aria-roledescription': i18nT('pages.chatSidebar.drag_to_reorder'),
                } : {})
                : {
                type: 'button' as const,
                'aria-expanded': !collapsed,
                'aria-label': collapsed ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name }),
                onClick: (e: React.MouseEvent<HTMLElement>) => toggleListFolderCollapse(folder, e.currentTarget),
              })}>
              {/* An inert row's glyph says "inactive" by WEIGHT, not by shape. The
               *  closed shape is this product's "collapsed, click to expand"
               *  affordance, so drawing it on a row that toggles nothing invites
               *  exactly the dead click it was meant to prevent; the open shape
               *  invites no click, and "contents shown below" is not a lie when
               *  there are none. So the shape stays open and the glyph instead goes
               *  dimmer and stops brightening on hover, which every pressable
               *  sibling does. Same icon, same box: the alignment guides that key
               *  off this glyph's geometry are untouched. */}
              {/* `|| emptyRow` is the point, not a tidy-up: a folder that was
               *  collapsed BEFORE it emptied still carries `collapsed: true`, and
               *  binding the glyph to that alone would draw the closed shape on an
               *  inert row - this product's "click to expand" affordance on a row
               *  that cannot expand. An inert row is always drawn open. */}
              <FolderGlyph color={folder.color} icon={folder.icon} size={14} open={!collapsed || emptyRow}
                className={emptyRow ? 'shrink-0 text-muted/40 transition-colors' : undefined}
                testId={`folder-collapse-${folder.id}`} />
              {/* Double-click rename is a mouse-only power shortcut; the accessible
               *  path is the ⋯-menu Rename item, so scope-disable the interaction rule. */}
              {/* The matched letters are marked while the search box narrows the list.
               *  Without it a folder row surfaced by a NAME match carries no cue at
               *  all: the row for "Sydney Property" on a search for "archive" (its
               *  subfolder) is indistinguishable from one whose own name matched, so
               *  the lane reads as arbitrary. The launcher already marks its matched
               *  letters, and this is the same signal in the surface the user was
               *  looking at. `highlightText` returns the plain string when the term
               *  is empty or absent, so ancestor and subtree rows — which have
               *  nothing to mark — are untouched, and that difference is itself the
               *  cue: the marked row is the one that explains the result. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              <span className="flex-1 text-[13px] font-medium text-text truncate text-left" title={i18nT('pages.chatSidebar.double_click_to_rename')} {...offlineProps(connected, i18nT('utils.offline.rename_folders'))} onDoubleClick={e => { e.stopPropagation(); if (!connected) { setFolderOfflineNotice(i18nT('utils.offline.gateway_offline_reconnect', { action: i18nT('utils.offline.rename_folders') })); return } setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}>{highlightText(folderNameText(folder), slotFilter.trim(), false, -1)}</span>
              {/* Channel-owned folder (created by per-channel session filing):
               *  show the channel's brand mark so the folder reads as "these are
               *  the Discord conversations" at a glance. Guarded the same way the
               *  session rows are — a channel with no brand asset shows nothing
               *  rather than ChannelBrandIcon's generic Link2 fallback, which
               *  means "live mirroring" elsewhere in this sidebar. */}
              {folder.channel && hasChannelBrandIcon(folder.channel) && (
                <span className="shrink-0 opacity-80" aria-hidden><ChannelBrandIcon channel={folder.channel} size={11} /></span>
              )}
              {folder.project_dir && <span className="text-[10px] text-accent/60 shrink-0" title={folder.project_dir}><Link2 size={9} /></span>}
              {/* Unread dot on the RIGHT, inline before the count — a state marker
               *  reading after the text, not a gutter marker. The header's left pad
               *  is load-bearing for the alignment guides, so the dot stays where it
               *  does not compete with it. Only when collapsed: an expanded folder's
               *  child rows carry their own markers. */}
              {hasUnread && collapsed && (
                // Carries the same accessible name as a session row's unread
                // marker, and the SAME i18n key: a colour-only dot is invisible to
                // a screen reader and indistinguishable from decoration, and this
                // one sits beside a count where that reads as styling. Like the
                // session row's marker, it is `role="img"` with that label.
                // `--ok` for the same reason as the session row's dot: it is the
                // SAME unread state rolled up, so it reads the same semantic
                // status token rather than the brand accent, matching the
                // `recent` filter and the connection-status dot.
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--ok)' }}
                  role="img"
                  aria-label={i18nT('pages.chatSidebar.agent_finished_your_turn')}
                  title={i18nT('pages.chatSidebar.agent_finished_your_turn')} />
              )}
              <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
            </HeaderShell>
            {folder.default_agent && <span className="text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded-full shrink-0 truncate max-w-[60px]" title={i18nT('pages.chatSidebar.default_agent', { name: folder.default_agent })}>{folder.default_agent}</span>}
          </>
        )}
        {/* An empty folder's row is otherwise a dead end: hiding the body took
          *  away the only control it had, and the closed glyph alone does not say
          *  the row can be opened or created in. So the row's own action cluster
          *  stops hiding on an empty folder — it already holds exactly the two
          *  controls that row needs (create, and the ⋯ menu whose rename/delete
          *  is what an empty folder usually wants), and an empty folder holds no
          *  sessions, so it resolves no conductor and the third control below is
          *  never emitted on it. */}
        {!(editingId === folder.id && editScope === 'list') && (
        <div className={`transition-all flex items-center gap-0.5 rounded-md group-focus-visible:opacity-100 group-has-[:focus-visible]:opacity-100 has-[[data-state=open]]:opacity-100${emptyRow ? ' shrink-0 -my-1' : ' absolute top-1/2 -translate-y-1/2 right-1.5 p-1 bg-card border border-border shadow-sm opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 [@media(hover:none)]:static [@media(hover:none)]:translate-y-0 [@media(hover:none)]:shrink-0 [@media(hover:none)]:-my-1 [@media(hover:none)]:p-0 [@media(hover:none)]:bg-transparent [@media(hover:none)]:border-transparent [@media(hover:none)]:shadow-none'}`}>
          {/* ⋯ menu first, then the primary "new chat" action.  Sibling
           *  <button>s of the collapse toggle (valid ARIA — no nesting). */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-text hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" data-testid={`folder-menu-${folder.id}`} onMouseDown={e => { e.stopPropagation() }}><MoreVertical size={12} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
              {renderFolderMenuItems(folder, reparentTargets, 'dropdown')}
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Same three-gesture contract as the header New button: plain click
           *  creates and switches; Cmd/Ctrl-click and middle-click create the
           *  session as a background TAB. Gated on `onOpenSlotInNewTab` --
           *  embedded hosts have no tab strip, so the modifier is ignored. */}
          <button type="button" data-testid={`folder-new-chat-${folder.id}`} className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} {...offlineProps(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name }))}
            onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
            onAuxClick={onOpenSlotInNewTab ? (e => {
              if (e.button !== 1) return
              e.preventDefault()
              e.stopPropagation()
              createChatInFolder(folder.id, { inNewTab: true })
            }) : undefined}
            onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, { inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) }) }}><MessageSquarePlus size={12} /></button>
          {/* Third control: the conductor's own chat, on the row itself.
           *
           *  This is the one place in the sidebar over the two-controls-per-row
           *  cap (`max-two-buttons-per-row`, `website/AUTOSDE.yaml`), and it is
           *  over it deliberately rather than by oversight: the folder row is
           *  where a reader is looking when they want that conversation, and
           *  sending them through the menu for it was judged the wrong trade.
           *  The excess is BOUNDED to this one case -- a folder that resolves a
           *  conductor -- and the cap governs every other row in the product.
           *
           *  Rendered ONLY when a conductor resolves. A folder with none keeps
           *  the two-control cluster exactly as it is on `main` -- the element
           *  below is simply not emitted, so nothing about that row's geometry
           *  or markup is conditional on this feature.
           *
           *  NOT gated on `folderConductorPin`: this ships always-on, while the
           *  flag holds the pinned LAYOUT, which re-orders a list
           *  people already read; a button on a row is an ingress they can
           *  decline, and holding it would leave the row with no way to the
           *  conversation it is drawn around.
           *
           *  `title` AND `aria-label` carry the same string the menu item shows,
           *  so the action is named identically wherever it is reached and a
           *  screen reader gets it without hover.
           *
           *  The glyph is an ARROW, not a speech bubble. Two bubble glyphs were
           *  tried here and both failed the same way: new-chat's neighbouring
           *  `MessageSquarePlus` is a bubble, so a blind UX read of the hovered
           *  row came back "purple speech bubble with no plus ... I'm not sure,
           *  nothing tells me what it does". `CornerUpRight` shares no outline
           *  with it at 12px, and it points the right way: the conductor sits
           *  ABOVE the chats it opened, so "up and over to that one" is what the
           *  click does. It is deliberately the mirror of the `CornerDownRight`
           *  the session row already draws for a row nested UNDER its creator.
           *
           *  And the glyph does not carry the meaning alone -- that was the rest
           *  of the same finding, since an icon-only control keeps its name in
           *  `title`, which is OS chrome a reader may never see. So the button
           *  shows the label too, the SAME `chat_with_folder` string, truncated:
           *  no new wording and no fourth name for one action. The cluster is an
           *  absolutely-positioned overlay on hover, so the text costs the row
           *  no width at rest; on a touch device, where the cluster is static and
           *  inline and width is scarcest, the label is dropped and the menu
           *  item remains the named path. */}
          {folderRowConductor && (
            <button type="button" data-testid={`folder-conductor-chat-row-${folder.id}`}
              className="group/conductor-chat relative cursor-pointer p-[4px] rounded text-accent hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none"
              title={i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}
              aria-label={i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}
              onMouseDown={e => { e.stopPropagation() }}
              onClick={e => {
                e.stopPropagation()
                // The same body the menu item runs, including the offline gate
                // every session click has: switching to a session nothing can
                // load would leave an empty pane with no way back to the row.
                if (!connected) return
                dispatch(switchSlot({ key: folderRowConductor.key, announceOnMissing: true }))
                onSelectSlot?.(folderRowConductor.key)
              }}>
              <CornerUpRight size={12} className="shrink-0" />
              {/* The name, on screen, in the page.
               *
               *  `title` alone was the finding: it is OS chrome, a reader may
               *  never see it, and no screenshot can show it. An inline label
               *  was tried first and rejected on the rendered pixels -- at this
               *  sidebar's width the string made the hover overlay 136px of a
               *  242px row and covered the folder's own name down to two
               *  letters, so the control explained itself by hiding what it
               *  was pointing at.
               *
               *  So it floats instead: out of flow, under the button, revealed
               *  on hover AND on keyboard focus. It costs the row no width at
               *  any width, it occludes nothing at rest, and it carries the
               *  SAME `chat_with_folder` string as the tooltip and the menu
               *  item, so one action never picks up a second name.
               *
               *  `aria-hidden`, because the button's own `aria-label` already
               *  carries this string and a screen reader must not say it twice.
               *  `pointer-events-none`, so the panel cannot eat the click it
               *  is describing.
               *
               *  `[@media(hover:none)]:hidden` is DECLARATIVE, not behavioural,
               *  and must not be deleted as dead: Tailwind gates `hover:` behind
               *  `@media (hover: hover)`, so on a touch screen the reveal never
               *  fires and the panel is already hidden. What the class adds is
               *  the statement that this is intended -- `hoverVariantPolicy`
               *  scans for anything hidden-until-hover and fails the build
               *  unless the touch state is spelled out on the element or the
               *  site is listed as decoration. It belongs here rather than in
               *  that allowlist because the panel is a label and not a control:
               *  on touch the cluster is static and inline, where width is
               *  scarcest, and the same action is still reached by name through
               *  the folder's menus. */}
              <span aria-hidden="true"
                className="pointer-events-none absolute top-full right-0 mt-1 hidden group-hover/conductor-chat:block group-focus-visible/conductor-chat:block [@media(hover:none)]:hidden whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-[11px] font-medium leading-none text-text shadow-md z-20">
                {i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}
              </span>
            </button>
          )}
        </div>
        )}
        {/* During a session drag, replace an expanded parent's normal presentation
          *  with one explicit destination and extend its hit area 8px into the
          *  first child row. Keeping the overlay INSIDE the sticky header makes
          *  it follow pinned parents without inserting layout; all remaining
          *  child-row pixels and the child body keep their own droppable. A
          *  session already filed directly here gets no no-op target. */}
        {activeDrag?.type === 'session' && !collapsed && childFolders.length > 0
          && slotFolders[activeDrag.id] !== folder.id
          && <ParentFolderDropTarget folder={folder} depth={depth} />}
      </div>
        </ContextMenuTrigger>
        <ContextMenuContent data-testid={`folder-context-menu-${folder.id}`} className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
          {renderFolderMenuItems(folder, reparentTargets, 'context')}
        </ContextMenuContent>
      </ContextMenu>
    )
  }

  // One row announcing the folders this container is hiding, rendered at the
  // BOTTOM of that container's folder list and indented to its depth. Peeking it
  // open renders those folders' real blocks (dimmed), so every normal
  // affordance — including ⋯ → Show folder, the durable undo — still works.
  // `containerKey` is 'root' | 'flat' | parent folder id.
  const renderHiddenReveal = (containerKey: string, hidden: readonly ChatFolder[], depth: number): React.ReactNode => {
    if (hidden.length === 0) return null
    const open = revealedContainers.has(containerKey)
    const n = hidden.length
    return (
      <div key={`hidden-reveal-${containerKey}`} data-testid={`hidden-reveal-${containerKey}`}>
        <button
          type="button"
          onClick={() => toggleReveal(containerKey)}
          aria-expanded={open}
          title={open ? i18nT('pages.chatSidebar.collapse_hidden_folders') : i18nT('pages.chatSidebar.show_hidden_folder', { count: n })}
          data-folder-hidden-reveal=""
          className="w-full flex items-center gap-1.5 py-1 pl-3.5 pr-2 text-left text-[11px] text-muted hover:text-text hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors"
        >
          <DisclosureChevron open={open} size={11} />
          <span>{i18nT('pages.chatSidebar.hidden_folder_count', { count: n })}</span>
        </button>
        {open && (
          <div className="opacity-70">
            {hidden.map(f => (
              <Fragment key={`revealed-${f.id}`}>{renderFolderBlock(f, depth)}</Fragment>
            ))}
          </div>
        )}
      </div>
    )
  }

  const renderFolderBlock = (folder: ChatFolder, depth: number, visited = new Set<string>(), dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed = false): React.ReactNode[] => {
    if (depth > 10 || visited.has(folder.id)) return []
    visited.add(folder.id)
    const allChildSlots = sidebarRows.folderTree.rowsIn(folder.id)
    /**
     * The one row this folder is drawn AROUND, or null.
     *
     * A conducted folder is one conversation that opened the rest, and reading
     * it off the lane meant picking that card out of a time-sorted list where it
     * sits wherever it last spoke, drawn exactly like the sessions it opened. So
     * it is lifted to the top of the folder and marked, and the folder row plus
     * that card read as the one thing they are.
     *
     * Null for an ordinary folder AND whenever the preview flag is off, and
     * `allChildSlots` is then handed on UNTOUCHED -- same array, same order,
     * same nodes. That is the whole fallback: nothing about an ordinary render
     * is conditional on this feature, so the lane tens of thousands of people
     * already read is unchanged for them.
     *
     * `folderConductorOf`, the same selector the folder menu's item asks, so
     * the pin and the item can never name different sessions: it detects over
     * the folder's unfiltered membership and then requires the row to be one
     * this folder draws. Null while a filter conceals the root, which is also
     * why the row it returns is always one of `allChildSlots` below.
     */
    const folderConductorSlot = folderConductorPin ? folderConductorOf(folder.id) : null
    // What the body draws: every row in the folder except the pinned one. The
    // folder HEADER's count still reads `rowsIn` and so still counts the
    // conductor -- it says how many sessions are filed here, and that is still
    // all of them.
    const childSlots = folderConductorSlot == null
      ? allChildSlots
      : allChildSlots.filter(s => s !== folderConductorSlot)
    const childNodes: React.ReactNode[] = []
    // Nested subfolders are sortables, exactly as root folders are: dragging one
    // either re-orders it among its siblings (drop on a sibling's edges or body)
    // or re-parents it (drop on the middle band of another folder's header, or on
    // the root lane to move it to the top level). Both gestures are the ones the
    // root lane already has -- see SortableSubfolderBlock for why a nested row
    // could previously only re-parent. The subtree ids ride along in the drag data
    // so collision detection can exclude self/descendants as targets, and the
    // sibling ids so a reorder cannot resolve into another container.
    //
    // `drawableChildFolders`, not a raw parent_id filter: while the list is
    // narrowed a child whose subtree draws nothing must be skipped HERE, before
    // the push. The nested render returns `[]` from inside the wrapper's own
    // render, which runs long after this push, so a wrapper committed now can
    // never be taken back -- and `childNodes.length` is what the drop gate below
    // and the header count both read.
    //
    // One SortableContext per PARENT, holding exactly that parent's drawable
    // children: `order` is a per-container index, so the ring a drag may move
    // within is one container's children and nothing else. It renders no DOM of
    // its own, so the row geometry the alignment guides pin is untouched.
    const childFolderRows = drawableChildFolders(folder)
    if (childFolderRows.length) {
      const siblingIds = childFolderRows.map(f => f.id)
      childNodes.push(
        <SortableContext key={`subfolder-ring-${folder.id}`} items={siblingIds} strategy={verticalListSortingStrategy}>
          {childFolderRows.map(cf => (
            <SortableSubfolderBlock key={`subfolder-drag-${cf.id}`} folder={cf} reorderable={folderReorderable} dragWithheld={folderDragWithheld}
              depth={depth + 1} visited={visited}
              subtree={[...(folderSubtrees.get(cf.id) ?? collectFolderSubtreeIds(folders, cf.id))]}
              siblings={siblingIds}
              disabled={editingId === cf.id}
              renderFolderBlock={renderFolderBlock} />
          ))}
        </SortableContext>
      )
    }
    // Bottom of THIS container's folder list: announce what the filter is
    // hiding here, at this depth. Sits after the sibling folders and before the
    // new-subfolder input, so it reads as part of the folder list.
    const hiddenHere = hiddenByContainer.get(folder.id)
    if (hiddenHere?.length) childNodes.push(renderHiddenReveal(folder.id, hiddenHere, depth + 1))
    const { fresh: freshChildSlotsRaw, stale: staleChildSlots } = splitStale(childSlots)
    // Stale rows are collapsed into their own section and are stale precisely
    // because nothing is bumping them, so only the live list needs the hold.
    const { rows: freshChildSlots, navScope: treeChildScope, container: treeChildContainer } = heldLane(freshChildSlotsRaw, 'list', `tree:folder:${folder.id}`)
    /**
     * The pinned conductor card: the folder body's FIRST child, before the
     * sibling folders and before the sorted rows.
     *
     * INSIDE `FOLDER_BODY_CLS`, which is load-bearing. An earlier revision drew
     * it outside that div to put it at the folder row's own left edge, and that
     * outdented its text 15px left of the folder name -- breaking the shared
     * edge recorded in
     * docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md,
     * whose guide 2 is that a folder name and the agent label of every session
     * filed inside it sit on one column. Drawn here the card keeps that column:
     * it is a row in the body like any other, so no guide moves and there is no
     * per-depth term to get wrong.
     *
     * So the pin is POSITION ONLY. `childNodes.unshift` below puts it first,
     * which is why it is not handed to the lane: `freshChildSlots` is in lane
     * order (recency, or the search ranking), and pinning by sorting would mean
     * teaching that comparator what a folder is.
     *
     * Same lane scope and hold container as its siblings, so keyboard roving
     * reaches it as the folder's first row. Kept OUT of `heldLane`'s list
     * because that list is the one a hover-hold displaces, and this row's
     * position is the one thing about it that is fixed.
     *
     * The accent bar is drawn OUT OF FLOW -- an absolutely positioned 2px span
     * in a `relative` wrapper -- for the same alignment reason. A `border-l-2`
     * on the wrapper would push the card's content 2px right and break the same
     * guide by 2px instead of 15. Out of flow it costs no width: it sits inside
     * the card's own left pad, so it reads as a bar down the card's left edge
     * while the text stays on the shared column. `inset-y-1` keeps it off the
     * row divider above and below.
     */
    const folderConductorNode = folderConductorSlot && (
      <div key={`folder-conductor-${folder.id}`} data-folder-conductor={folder.id} className="relative">
        <span aria-hidden="true" className="absolute left-0 inset-y-1 w-0.5 rounded-full bg-accent" />
        {renderSessionRow(folderConductorSlot, depth + 1, false, treeChildScope, treeChildScope, treeChildContainer, undefined, true)}
      </div>
    )
    freshChildSlots.forEach((s, i) => {
      const isActive = isActiveRow(s)
      const nextIsActive = isActiveRow(freshChildSlots[i + 1])
      const showDivider = i < freshChildSlots.length - 1 && !isActive && !nextIsActive
        && !startsAutomaticSection(freshChildSlots, i + 1)
      if (startsAutomaticSection(freshChildSlots, i)) {
        childNodes.push(<PinnedSessionDivider key={`pinned-divider-${folder.id}`} />)
      }
      childNodes.push(renderSessionRow(s, depth + 1, showDivider, treeChildScope, treeChildScope, treeChildContainer))
    })
    if (!searchRanked && staleExpanded.has(folder.id)
      && staleChildSlots.length > 0 && freshChildSlots.length > 0
      && pinned.has(freshChildSlots[freshChildSlots.length - 1].key)) {
      childNodes.push(<PinnedSessionDivider key={`pinned-divider-stale-${folder.id}`} />)
    }
    const staleSection = renderStaleSection(folder.id, staleChildSlots, depth + 1, folder.name)
    if (staleSection) childNodes.push(staleSection)
    // FIRST in the body, after everything else has been pushed. One `unshift`
    // rather than a branch at each of the three places below that read
    // `childNodes.length`: the pinned card is a child of this folder like any
    // other, so counting it as one keeps the empty-body, narrowed-away and
    // body-wrapper decisions exactly as they are on main.
    if (folderConductorNode) childNodes.unshift(folderConductorNode)
    // Hide folders with no matching children while the list is narrowed —
    // unless this folder owns the active create-failure notice: a create fired
    // from the folder-picker menu can target a folder the narrow is hiding,
    // and eliding it would make the failure exactly as silent as before #8229.
    //
    // Nor when the folder ITSELF is what the query named. An empty folder whose
    // name matches is still the answer to "where is that folder" — dropping it
    // would mean the one search guaranteed to name it is also the one search that
    // cannot show it. `folderNameMatchIds` covers the matched folder's subtree, so
    // a matched parent keeps its empty children too: they are part of what the
    // query asked to see.
    if (listNarrowed && childNodes.length === 0
      && folderCreateError?.folderId !== folder.id
      && !folderNameMatchIds?.has(folder.id)) return []
    // Wrap children in a bordered container so the folder's extent is visually
    // clear when multiple folders are open. Only wrap when there's content,
    // otherwise the FolderBody would render an empty 1px-tall strip with a line.
    // Opt-in (Settings > Chat > "Hide the body of an empty folder"): a folder with
    // nothing in it renders NO body - not a collapsed one, not an empty one - so
    // it costs one row instead of two and a tree of area folders stops spending
    // most of the sidebar's height on rows holding nothing. Dropping the body
    // rather than collapsing it is why there is no per-folder expansion state:
    // nothing is hidden, so nothing needs re-reaching.
    const emptyBody = hideEmptyFolderBody && childNodes.length === 0
    const wrapped = childNodes.length > 0 ? (
      <div key={`folder-children-${folder.id}`} className={FOLDER_BODY_CLS}>
        <FolderRail name={folder.name} id={folder.id} onToggle={e => toggleListFolderCollapse(folder, e.currentTarget)} />
        {childNodes}
      </div>
    ) : emptyBody || listNarrowed ? null : (
      // Default: the empty-folder affordance stays exactly as it was. A newly
      // created (or emptied) expanded folder would otherwise render nothing,
      // leaving the hover-only create control on the header as the only
      // (invisible-at-rest) way to start a session in it.
      <div key={`folder-children-${folder.id}`} className={FOLDER_BODY_CLS}>
        <button key={`folder-newchat-${folder.id}`} type="button" data-testid={`folder-empty-new-chat-${folder.id}`} data-folder-new-chat=""
          // Same three-gesture contract as the folder header's "+" above.
          onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
          onAuxClick={onOpenSlotInNewTab ? (e => {
            if (e.button !== 1) return
            e.preventDefault()
            createChatInFolder(folder.id, { inNewTab: true })
          }) : undefined}
          onClick={e => createChatInFolder(folder.id, { inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) })}
          title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} {...offlineProps(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name }))}
          className="w-full flex items-center gap-2.5 pl-3.5 pr-3 py-2 rounded-md text-[12px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
          <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={13} className="shrink-0 ml-auto" />
        </button>
      </div>
    )
    // Outer container wraps header + body so the entire folder block is a
    // single drag-drop target. Dropping anywhere inside (header, children,
    // empty space) assigns the dragged session to this folder.
    // Uses a dragEnter counter instead of contains() checks — nested child
    // folders fire enter/leave pairs that balance to zero when the drag
    // moves into a subfolder, so the parent highlight clears correctly.
    return [
      <DndDroppable key={`folder-drop-${folder.id}`} id={`folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
          // `--folder-pin-stack` is how far the pinned headers above this block's
          // rows reach down the lane (this header plus every ancestor's), so a
          // row that keyboard roving or a reveal scrolls into view lands below
          // them instead of behind them (`scroll-margin-top` in index.css).
          <div ref={setNodeRef} data-folder-drop={folder.id} style={{ '--folder-pin-stack': `calc(var(--folder-row-sticky-h) * ${depth + 1})` } as React.CSSProperties} className={`rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}`}>
            {renderFolderHeader(folder, dragHandleProps, emptyBody, depth)}
            {renderFolderCreateError(folder.id)}
            {wrapped && <FolderBody key={`folder-body-${folder.id}`} padding={FOLDER_BODY_OPEN_PADDING} open={!folder.collapsed && !forceCollapsed} instantClose={instantCloseId === folder.id}>{wrapped}</FolderBody>}
          </div>
        )}
      </DndDroppable>,
    ]
  }
  return { renderFolderBlock, renderHiddenReveal, renderFolderCreateError, folderCreateMountAbsent }
}

/** What `createFolderBlocks` hands the lanes. */
export type FolderBlocks = ReturnType<typeof createFolderBlocks>

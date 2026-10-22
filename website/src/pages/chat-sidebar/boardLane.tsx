/** The tag-column board lane: the lane notices, each column with its header,
 *  filter popover and native card drop, the column's folder blocks (the board's
 *  own folder rows, reorderable per column) and the unfile strip. */
import { Fragment } from 'react'
import { OverlayPortal } from '../../components/overlayRoot'
import { Plus, X, EyeOff, FolderPlus, MessageSquarePlus, ChevronRight, MoreVertical, Tag as TagIcon, GripVertical, Server } from 'lucide-react'
import { FolderBody } from '../../components/FolderBody'
import { FolderRail } from '../../components/FolderRail'
import ErrorNotice from '../../components/ErrorNotice'
import FolderGlyph from '../../components/FolderGlyph'
import { DndContext, DragOverlay, MeasuringStrategy } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent } from '../../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent } from '../../components/ui/context-menu'
import { Input } from '../../components/ui'
import { safeSetItem } from '../../utils/safeStorage'
import TagManagerList from '../../components/TagManagerList'
import { DndActiveProbe, DndDroppable } from '../../components/dnd'
import { collectFolderSubtreeIds } from '../../utils/folderTree'
import type { ChatFolder, ChatTag } from '../../types'
import { SESSION_LANES } from '../chat/sessionLane'
import { SessionRowWindowScroller } from '../chat/sessionRowWindow'
import { i18nT } from '../../i18n/t'
import { sidebarCollision } from './dnd/collision'
import { BoardUnfileDropStrip, SortableColumnFolder, FolderDragGhost } from './dnd/targets'
import type { Slot } from './types'
import { FOLDERS_SHELVED_LS_KEY } from './persistence'
import { sessionRowIdentity } from './rowIdentity'
import { BOARD_FOLDER_BODY_CLS, FOLDER_BODY_OPEN_PADDING, PinnedSessionDivider, createTabModeForClick } from './chrome'
import type { LaneRows } from './laneRows'
import type { FolderBlocks } from './folderBlocks'
import type { SidebarScope } from './scope'
import { offlineProps } from '../../utils/offline'

/** The board lane renderer for one shell render. */
export function createBoardLane(shell: SidebarScope & LaneRows & FolderBlocks) {
  const { activeDrag, addColumnAfterMutation, attachStripEdges, boardError, boardFolderCollapsed,
    boardPeerCount, closeColumnPopover, columnEditId, columnMatches, columnPopoverImeLatch, columnPopoverRef,
    connected, createChatInFolder, deleteColumnWithConfirm, derivedHintCol, dndSensors,
    dropSlotMutation, editName, editScope, editingId, flatView, folderCompare, folderCreateError,
    folderCreateMountAbsent, folderDragWithheld, folderEditInputRef, folderCancelEscapeRef, folderReorderable,
    folderSubtrees, folders, handleSidebarDragCancel, handleSidebarDragEnd, handleSidebarDragStart, heldLane,
    hiddenFolderCount, hideEmptyFolderBody, ime, isActiveRow, isFolderFilteredOut, laneSlots, moveByDrag,
    nativeSessionDrag, onMenuCloseAutoFocus, onOpenSlotInNewTab, orderedColumns, popoverPos,
    renameCommit, renderFolderCreateError, renderFolderMenuItems, renderSessionRow, reorderColumnsMutation, reportDndActive,
    revealFlash, rootFolders, setBoardError, setColumnEditId, setDerivedHintCol, setEditName, setEditScope,
    setEditingId, setFilterSortOpen, setFolderModal, setFolderOfflineNotice, setFoldersShelved, sidebarRows, slotFolders,
    startsAutomaticSection, stripEdges, tagById, toggleColumnCollapse,
    updateColumnMutation } = shell
  // Does any descendant (direct or nested) of `folderId` contain a slot from `slots`?
  function descendantMatch(fs: ChatFolder[], folderId: string, slots: Slot[], visited = new Set<string>()): boolean {
    if (visited.has(folderId)) return false // cycle guard
    visited.add(folderId)
    for (const child of fs) {
      if (child.parent_id !== folderId) continue
      if (slots.some(s => sidebarRows.folderOf(s) === child.id)) return true
      if (descendantMatch(fs, child.id, slots, visited)) return true
    }
    return false
  }

  // Render a folder block scoped to a single column: only slots matching the column predicate.
  // Always render the folder header (even with 0 matches) so users can see + drop into it.
  const renderColumnFolder = (folder: ChatFolder, columnId: string, colSlotKeys: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean): React.ReactNode => {
    const childFolders = folders.filter(f => f.parent_id === folder.id).sort(folderCompare)
    const { rows: childSlots, navScope: folderLaneScope, container: folderHoldContainer } = heldLane(sidebarRows.folderTree.rowsIn(folder.id).filter(s => colSlotKeys.has(sessionRowIdentity(s))), columnId, `board:${columnId}:folder:${folder.id}`)
    // A nested folder the person unchecked drops out of the recursion, so neither its
    // header nor anything under it renders. Checking the folder's OWN id is enough:
    // dropping it here takes its descendants with it, the same way the tree's block
    // removal does. Its sessions are already gone from `colSlotKeys`; without this the
    // column would still draw the header of a folder the person asked not to see.
    const deepChildren = childFolders.filter(f => !isFolderFilteredOut(f))
    // Same opt-in as the tree (see the note in renderFolderBlock): only when the
    // setting is on does a column copy holding nothing lose its body, and with it
    // the collapse state it no longer has anything to remember.
    const emptyBody = hideEmptyFolderBody && deepChildren.length === 0 && childSlots.length === 0
    const collapsed = boardFolderCollapsed(columnId, folder)
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    // The board lane's answer to a reveal. A column has no folder HEADER row, so the
    // tree's `folderFlash` has nothing to attach to here and a board reveal used to
    // scroll to the column and then sit there unmarked -- the scroll alone is not the
    // confirmation, since the target is often already on screen and nothing moves.
    // Same state, same classes, attached to the box the reveal actually found.
    const boardFolderFlash = revealFlash?.kind === 'folder' && revealFlash.key === folder.id
      ? (revealFlash.fading ? 'fade' : 'flash')
      : null
    const count = childSlots.length + deepChildren.filter(cf => {
      const cfSlots = sidebarRows.folderTree.rowsIn(cf.id).filter(s => colSlotKeys.has(sessionRowIdentity(s)))
      return cfSlots.length > 0 || descendantMatch(
        folders,
        cf.id,
        laneSlots.filter(s => colSlotKeys.has(sessionRowIdentity(s))),
      )
    }).length
    // Board-view folders become sortable only when a drag handle is supplied
    // (root folders wrapped in SortableColumnFolder). Subfolders render without
    // one, so a board subfolder is not directly draggable at all — neither
    // reordered nor re-parented. That is NOT parity with the list view, which
    // has always given a nested row a drag (re-parent before #10428, reorder as
    // well after it); giving the board lane the same gesture needs a handle this
    // path does not pass down, so it stays a separate piece of work. Disabled
    // while renaming in THIS column (rename is per-column via editScope) so
    // the inline input stays usable.
    const focusBoardFolderRow = () => {
      const col = document.querySelector<HTMLElement>(`[data-testid="col-${columnId}-folder-${folder.id}"]`)
      ;(col?.querySelector<HTMLElement>('[role="button"][tabindex],button') ?? col)?.focus({ preventScroll: true })
    }
    const draggable = !!dragHandleProps && !(editingId === folder.id && editScope === columnId)
    return (
      // Two drop mechanisms coexist on this block, one per drag SOURCE:
      //  • Native HTML5 onDrop (below) — SESSION cards drag natively (they set
      //    dataTransfer text/plain), so a session dropped here is assigned to
      //    this folder via assignToFolder.
      //  • dnd-kit DndDroppable (this wrapper) — FOLDERS drag via the pointer
      //    sensor (SortableColumnFolder), never via native DnD, so their active
      //    data lives in active.data.current, unreadable by onDrop. The
      //    folder-drop droppable is what lets handleSidebarDragEnd re-parent a
      //    folder dropped here (moveFolderTo). The two never collide: a native
      //    drag never fires dnd-kit's onDragEnd and a dnd-kit drag never fires
      //    the DOM drop event. Id is column-scoped because a root folder renders
      //    once per board column and dnd-kit droppable ids must be unique.
      <DndDroppable key={`col-${columnId}-folder-drop-${folder.id}`} id={`col-${columnId}-folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
      // The drag handlers below make this a mouse-only drop target with no
      // keyboard analogue, so scope-disable the static-interaction rule.
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions
      <div ref={setNodeRef}
        data-testid={`col-${columnId}-folder-${folder.id}`}
        data-folder-drop={folder.id}
        // `folder-col` is the board lane's counterpart to the tree's `folder-row`,
        // and it exists for the flash: the reveal outline in index.css is declared
        // COMPOUND (`.folder-row.session-reveal-flash`), so adding the flash class to
        // a box carrying neither row class attaches a class that no rule matches and
        // paints nothing. Naming this box gives the same declaration something to
        // key on here.
        className={`folder-col rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}${boardFolderFlash ? ` session-reveal-flash${boardFolderFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}
        onDragOver={e => { e.preventDefault(); e.stopPropagation(); e.currentTarget.classList.add('ring-1', 'ring-accent') }}
        onDragLeave={e => { e.stopPropagation(); e.currentTarget.classList.remove('ring-1', 'ring-accent') }}
        onDrop={e => {
          e.preventDefault(); e.stopPropagation()
          e.currentTarget.classList.remove('ring-1', 'ring-accent')
          const k = e.dataTransfer.getData('text/plain')
          if (k) moveByDrag(k, folder.id)
        }}
      >
        {/* Only the header opens the folder menu; body rows keep their own
         * menus and the rename input keeps its native editing menu. */}
        <ContextMenu>
          <ContextMenuTrigger asChild disabled={editingId === folder.id && editScope === columnId}>
        {/* Same rule as the tree row: a column copy with no body has nothing to
         *  toggle, so it is not a control - no button role, no tab stop, no
         *  pointer cursor, no expanded state and no handler. It stays draggable,
         *  because reordering a folder is an action an empty folder can honour. */}
        <div
          className={`group relative flex items-center gap-2 pr-2 py-1 rounded-md ${draggable ? 'cursor-grab active:cursor-grabbing' : emptyBody ? 'cursor-default' : 'cursor-pointer'} text-[12px] text-muted transition-all${emptyBody ? '' : ' hover:text-text hover:bg-bg-hover'}`}
          style={{ paddingLeft: '6px' }}
          {...(draggable ? dragHandleProps : {})}
          // The collapse props go AFTER the drag spread, and the order is
          // load-bearing: `dragHandleProps` are dnd-kit's sortable listeners and
          // they carry the keyboard sensor's own `onKeyDown`, so a later drag
          // spread would replace the collapse handler and Enter would start a
          // drag instead of toggling the body.
          {...(emptyBody
            // No body to disclose, but the row is still a DRAG HANDLE while it is
            // draggable: `useSortable` here hands down only `listeners`, never its
            // `attributes`, so this hand-written `tabIndex` is the only thing that
            // lets the keyboard sensor reach the row - drop it and an empty folder
            // can be reordered with a mouse but not with a keyboard. So it keeps a
            // tab stop and says what it is, and drops only the collapse-specific
            // props (`aria-expanded`, the expand/collapse label, both handlers).
            ? (draggable ? {
              role: 'button',
              tabIndex: 0,
              'aria-label': i18nT('pages.chatSidebar.folder_2', { name: folder.name }),
              'aria-roledescription': i18nT('pages.chatSidebar.drag_to_reorder'),
            } : {})
            : {
              role: 'button',
              tabIndex: 0,
              'aria-expanded': !collapsed,
              'aria-label': collapsed ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name }),
              onClick: () => toggleColumnCollapse(columnId, folder),
              // `e.target === e.currentTarget` restricts the Space/Enter toggle to
              // the row itself. Without it the row swallows every Space typed in a
              // focused DESCENDANT - the inline rename input below - because
              // preventDefault() drops the character and the folder collapses
              // instead. Same guard as Clickable and UpdateModal.
              onKeyDown: (e: React.KeyboardEvent) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleColumnCollapse(columnId, folder) } },
            })}
        >
          {/* Dimmer and hover-inert on an empty row - same rule as the tree. */}
          {/* Always open on an inert row - same reason as the tree. */}
          <FolderGlyph color={folder.color} icon={folder.icon} size={11} open={!collapsed || emptyBody}
            className={emptyBody ? 'shrink-0 text-muted/40 transition-colors' : undefined} />
          {editingId === folder.id && editScope === columnId ? (
            /* Inline rename input — board-view parity with renderFolderHeader.
             *  Without this branch the ⋯-menu "Rename" set editingId but no
             *  field ever appeared, so rename silently did nothing here. The
             *  collapse handler is on the OUTER div, so the input's onClick +
             *  onMouseDown stopPropagation are load-bearing (they keep clicking
             *  the field from bubbling to toggleColumnCollapse). Keys are
             *  handled the other way round — the row's onKeyDown ignores events
             *  whose target is not the row — so Space types a space here rather
             *  than collapsing the folder. */
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[12px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => { if (renameCommit(folder.id, editName)) requestAnimationFrame(focusBoardFolderRow) }, onEscape: () => { folderCancelEscapeRef.current = true; folderEditInputRef.current?.blur(); setEditingId(null); requestAnimationFrame(focusBoardFolderRow) }, onBlur: () => { if (folderCancelEscapeRef.current) { folderCancelEscapeRef.current = false; return } renameCommit(folder.id, editName) } })} />
          ) : (
            // Double-click rename is a mouse-only power shortcut; the accessible
            // path is the ⋯-menu Rename item, so scope-disable the interaction rule.
            // eslint-disable-next-line jsx-a11y/no-static-element-interactions
            <span className="flex-1 truncate" title={i18nT('pages.chatSidebar.double_click_to_rename')} {...offlineProps(connected, i18nT('utils.offline.rename_folders'))} onDoubleClick={e => { e.stopPropagation(); if (!connected) { setFolderOfflineNotice(i18nT('utils.offline.gateway_offline_reconnect', { action: i18nT('utils.offline.rename_folders') })); return } setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}>{folder.name}</span>
          )}
          <span className="text-[10px] text-muted shrink-0">{count}</span>
          {/* List-view parity: an empty folder's row keeps its action cluster
            *  visible (see the note in renderFolderHeader). */}
          {!(editingId === folder.id && editScope === columnId) && (
          <span className={`${emptyBody ? '' : 'opacity-0 '}group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-visible:opacity-100 group-has-[:focus-visible]:opacity-100 has-[[data-state=open]]:opacity-100 transition-opacity flex items-center gap-0.5`}>
            {/* ⋯ menu + a primary "new chat in folder" action, mirroring the
             *  list-view folder header (renderFolderHeader) so board view has
             *  the same one-click way to start a session inside a folder. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-menu`} className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" onMouseDown={e => { e.stopPropagation() }} onClick={e => { e.stopPropagation() }} onKeyDown={e => { e.stopPropagation() }}>
                  <MoreVertical size={11} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
                {renderFolderMenuItems(folder, reparentTargets, 'dropdown', columnId)}
              </DropdownMenuContent>
            </DropdownMenu>
            {/* Same three-gesture contract as the header New button; the
             *  existing stopPropagation stays so the header click/drag
             *  handlers never see the press. */}
            <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-new-chat`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} {...offlineProps(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name }))}
              onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, { columnId, inNewTab: onOpenSlotInNewTab ? createTabModeForClick(e) : false }) }}
              onMouseDown={e => { e.stopPropagation(); if (e.button === 1 && onOpenSlotInNewTab) e.preventDefault() }}
              onAuxClick={onOpenSlotInNewTab ? (e => {
                if (e.button !== 1) return
                e.preventDefault()
                e.stopPropagation()
                createChatInFolder(folder.id, { columnId, inNewTab: 'background' })
              }) : undefined}
              onKeyDown={e => { e.stopPropagation() }}>
              <MessageSquarePlus size={11} />
            </button>
          </span>
          )}
        </div>
          </ContextMenuTrigger>
          <ContextMenuContent data-testid={`col-${columnId}-folder-${folder.id}-context-menu`} className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
            {renderFolderMenuItems(folder, reparentTargets, 'context', columnId)}
          </ContextMenuContent>
        </ContextMenu>
        {renderFolderCreateError(folder.id, columnId)}
        {!emptyBody && (
        <FolderBody padding={FOLDER_BODY_OPEN_PADDING} open={!collapsed && !forceCollapsed}>
          {/* `BOARD_FOLDER_BODY_CLS`: the connector beside the rows with the
           *  4px pad that clears the rail's hit strip, as in the list view;
           *  rows inside keep their own `ROW_BOX_CLS` pad. */}
          <div className={BOARD_FOLDER_BODY_CLS}>
            {(deepChildren.length > 0 || childSlots.length > 0) && (
              <FolderRail name={folder.name} id={`${columnId}-${folder.id}`} onToggle={() => toggleColumnCollapse(columnId, folder)} />
            )}
            {/* Default: the empty-folder affordance stays exactly as it was, in
             *  list-view parity (see renderFolderBlock). Reached only when the
             *  setting is OFF - with it on there is no body to put this in. */}
            {deepChildren.length === 0 && childSlots.length === 0 && (
              <button key={`col-${columnId}-newchat-${folder.id}`} type="button" data-testid={`col-${columnId}-folder-${folder.id}-empty-new-chat`} data-folder-new-chat=""
                // Same three-gesture contract as the folder header's "+".
                onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
                onAuxClick={onOpenSlotInNewTab ? (e => {
                  if (e.button !== 1) return
                  e.preventDefault()
                  createChatInFolder(folder.id, { columnId, inNewTab: 'background' })
                }) : undefined}
                onClick={e => createChatInFolder(folder.id, { columnId, inNewTab: onOpenSlotInNewTab ? createTabModeForClick(e) : false })}
                title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} {...offlineProps(connected, i18nT('utils.offline.create_sessions'), i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name }))}
                className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-[11px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
                <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={11} className="shrink-0 ml-auto" />
              </button>
            )}
            {deepChildren.map(cf => renderColumnFolder(cf, columnId, colSlotKeys))}
            {childSlots.map((s, i) => {
              const isActive = isActiveRow(s)
              const nextIsActive = isActiveRow(childSlots[i + 1])
              const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
                && !startsAutomaticSection(childSlots, i + 1)
              // `scope` stays per-folder so the Framer layoutId and the inline
              // rename target remain unique, but the arrow rove is scoped to the
              // COLUMN: a board column's foldered and ungrouped rows are one
              // visible list, so ArrowDown has to cross the folder boundary.
              return (
                <Fragment key={sessionRowIdentity(s)}>
                  {startsAutomaticSection(childSlots, i) && <PinnedSessionDivider />}
                  {renderSessionRow(s, 1, showDivider, `${folderLaneScope}:${folder.id}`, folderLaneScope, folderHoldContainer)}
                </Fragment>
              )
            })}
          </div>
        </FolderBody>
        )}
      </div>
        )}
      </DndDroppable>
    )
  }

  // The board's reading of the list lanes' `draggingFolderedSession`, from the native mirror: a card
  // drags with HTML5 DnD there, so `activeDrag` never carries it. Reveals the
  // unfile strip at the foot of every column.
  const boardDraggingFiledSession = nativeSessionDrag !== null && !!slotFolders[nativeSessionDrag]

  const renderBoardLane = (): React.ReactNode => (
          // Trello-style horizontal column strip. The columns scroll on their own
          // inside the strip, so the lane itself steps below the floating dock
          // (ListDock) rather than scrolling under it; the hidden-folders line and
          // the board error above the strip stay reachable that way.
          <div className="flex-1 min-h-0 flex flex-col pt-[var(--list-dock-h,0.5rem)]">
          {/* Lane-level fallback ownership (exactly one mount ever renders):
           *  - no columnId (New-menu create): no per-column mount exists;
           *  - board-flat: the columnId-scoped mounts are hidden with folders;
           *  - the error's column was deleted: its mount is gone for good;
           *  - the target FOLDER was deleted mid-flight: no mount anywhere.
           *  With folders shown, the column alive and the folder present, the
           *  column's own mount wins and this line is false. */}
          {folderCreateError && (flatView || !folderCreateError.columnId || !orderedColumns.some(c => c.id === folderCreateError.columnId) || folderCreateMountAbsent(folderCreateError.folderId)) && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
          {/* Board writes (delete / reorder / add-after / card drop) report here,
           *  above the strip. Column payloads are server-side, so nothing can be
           *  lost by handing off; the caches were re-synced in the onError. */}
          <ErrorNotice
            title={i18nT('pages.chatSidebar.board_update_failed')}
            message={boardError}
            askAgent
            onDismiss={() => setBoardError('')}
            className="mx-2 mt-2 shrink-0"
            testId="board-error"
          />
          {/* Below the two error notices above, not beside them: this is a
            *  standing fact about the layout, not something that just went
            *  wrong, so it must never push a failure the user has to act on
            *  further down the lane.
            *
            *  The experimental peer-session merge is a FLAT/LIST affordance.
            *  Board columns remain local-slot containers because their tags,
            *  lanes and drop actions are local mutations. Every filtered peer
            *  row is therefore omitted here and represented by this exact count. */}
          {boardPeerCount > 0 && (
            <div className="mx-2 mt-2 px-2 py-1.5 rounded-md bg-info-subtle border border-info/40 text-info-text text-[11px] flex items-center gap-1.5">
              <Server size={11} aria-hidden="true" className="shrink-0" />
              <span className="min-w-0">
                {i18nT('pages.chatSidebar.remote_sessions_not_shown_in_board_view', { count: boardPeerCount })}
              </span>
            </div>
          )}
          {/* The hide's trace in the board lane, as a row rather than as a tint.
            *
            * The other three lanes end a container with a reveal row, which a board
            * cannot copy: a column draws no folder header for such a row to hang from,
            * and a hidden folder is not a property of any one column anyway — its
            * sessions scatter across all of them, so a per-column row would print the
            * same count once per column. So it sits at the LANE level, beside the notice
            * above that reports the other population a board declines to draw.
            *
            * A row and not just the funnel's tint, because the hide is persistent: it
            * lives in localStorage and survives a reload, so a tint and a hover count are
            * all a returning reader has to account for sessions that are simply fewer
            * than they were. The honest reading of that is deletion.
            *
            * It is a button, and it opens the filter menu, because that menu holds the
            * undo. It also UNSHELVES the menu's folder list on the way: that list is the
            * way back and it is gated behind the shelf, so opening the menu over a
            * rolled-up shelf lands the reader on a dense panel with no folders in it and
            * the word on the button promises something that did not happen. The other
            * lanes' row peeks the folders open in place; this is the same gesture as far
            * as a board can carry it. */}
          {hiddenFolderCount > 0 && (
            <button
              type="button"
              onClick={() => {
                setFoldersShelved(false)
                safeSetItem(FOLDERS_SHELVED_LS_KEY, '0')
                setFilterSortOpen(true)
              }}
              title={i18nT('pages.chatSidebar.show_hidden_folders_from_board', { count: hiddenFolderCount })}
              aria-label={i18nT('pages.chatSidebar.show_hidden_folders_from_board', { count: hiddenFolderCount })}
              data-testid="board-hidden-folders"
              data-hidden-folder-count={String(hiddenFolderCount)}
              className="mx-2 mt-2 px-2 py-1.5 rounded-md bg-warn-subtle border border-warn/40 text-warn-text text-[11px] flex items-center gap-1.5 text-left cursor-pointer hover:bg-warn/20 transition-colors"
            >
              <EyeOff size={11} aria-hidden="true" className="shrink-0" />
              <span className="min-w-0 truncate" data-testid="board-hidden-folders-count">
                {i18nT('pages.chatSidebar.hidden_folder_count', { count: hiddenFolderCount })}
              </span>
              {/* The action, in VISIBLE text and not only in the name. A count plus a
                *  glyph tells a sighted pointer-less reader that rows are withheld and
                *  leaves them to guess the row is tappable, which is the hover-only
                *  failure this row exists to end. `show` is the catalog's own word for
                *  this affordance, so the 13 locales already carry it. The chevron is
                *  decorative: the word beside it already says what happens. */}
              <span className="ml-auto shrink-0 inline-flex items-center gap-0.5 underline decoration-dotted underline-offset-2"
                data-testid="board-hidden-folders-action">
                {i18nT('pages.chatSidebar.show')}
                <ChevronRight size={11} aria-hidden="true" className="shrink-0" />
              </span>
            </button>
          )}
          <div className="relative flex-1 min-h-0 flex flex-col">
          <div ref={attachStripEdges} className="flex-1 overflow-x-auto overflow-y-hidden flex gap-2 p-2" data-testid="column-strip">
            {orderedColumns.map((col, colIdx) => {
              // The board pool already leaves out every row the folder filter conceals,
              // and this one population feeds every render site below: the flat-board
              // rows, every folder block's body through `colSlotKeys`, each block's
              // aggregate count, and the "no sessions" notice. The board lane has no
              // reveal row (a column has no folder header for one to hang from), so the
              // way back is re-checking the folder in the filter menu.
              const colSlots = sidebarRows.lanes.board.filter(s => columnMatches(col, s))
              const colTags = col.tag_ids.map(tid => tagById[tid]).filter(Boolean) as ChatTag[]
              const laneDef = col.source === 'state' ? SESSION_LANES.find(l => l.key === col.state_key) : undefined
              // Only a single-status-tag column can accept a card: dropping onto a
              // derived lane has nothing to write (the backend refuses it too).
              const isStatusLane = !laneDef && colTags.length === 1 && !!colTags[0].status
              return (
                // Board column is a drag-and-drop drop zone (column reorder + session
                // card drop); mouse-only drag handlers, so scope-disable the rule.
                // eslint-disable-next-line jsx-a11y/no-static-element-interactions
                <div key={col.id} data-testid={`column-${col.id}`} className="flex flex-col flex-1 min-w-0 bg-card border border-border rounded-md overflow-hidden" style={{ minWidth: orderedColumns.length > 1 ? '220px' : undefined }}
                  // Clear first, on the way down: a folder block or unfile strip
                  // inside the lane takes the card and stops the dragover, so only
                  // a dragover that reaches the lane itself shows the hint again.
                  onDragOverCapture={laneDef ? () => setDerivedHintCol(c => (c === col.id ? null : c)) : undefined}
                  onDragOver={e => {
                    const types = e.dataTransfer.types
                    // Accept column reorder on the entire column surface
                    if (types.includes('application/mc-column')) {
                      e.preventDefault()
                      return
                    }
                    // A derived lane leaves the drop refused (no preventDefault)
                    // and shows why.
                    if (laneDef && types.includes('text/plain')) {
                      setDerivedHintCol(col.id)
                      return
                    }
                    // Accept session-card drop only on status lanes
                    if (isStatusLane && types.includes('text/plain')) {
                      e.preventDefault()
                      e.currentTarget.classList.add('ring-1', 'ring-accent')
                    }
                  }}
                  onDragLeave={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    // Moving onto a child also fires dragleave; only leaving the lane clears.
                    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDerivedHintCol(c => (c === col.id ? null : c))
                  }}
                  onDrop={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    setDerivedHintCol(null)
                    // Column reorder takes priority
                    const draggedCol = e.dataTransfer.getData('application/mc-column')
                    if (draggedCol && draggedCol !== col.id) {
                      e.preventDefault()
                      const ids = orderedColumns.map(c => c.id).filter(id => id !== draggedCol)
                      ids.splice(colIdx, 0, draggedCol)
                      reorderColumnsMutation.mutate(ids)
                      return
                    }
                    if (!isStatusLane) return
                    e.preventDefault()
                    const k = e.dataTransfer.getData('text/plain')
                    if (k) dropSlotMutation.mutate({ slot: k, columnId: col.id })
                  }}>
                  <div className="relative flex items-center gap-1 p-2 border-b border-border bg-bg-elevated">
                    {/* Reorder handle: mouse-only drag source for column reordering. */}
                    {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
                    <span draggable
                      className="cursor-grab text-muted hover:text-text shrink-0"
                      onDragStart={e => { e.dataTransfer.setData('application/mc-column', col.id); e.dataTransfer.effectAllowed = 'move' }}
                      title={i18nT('pages.chatSidebar.drag_to_reorder')}>
                      <GripVertical size={12} />
                    </span>
                    <div className="flex flex-wrap gap-1 items-center flex-1 min-w-0">
                      {laneDef ? (
                        // A lane's identity is its runtime state, so it shows a
                        // fixed name and accent rather than tag chips — there is
                        // no filter behind it for the user to edit.
                        <span className="inline-flex items-center gap-1.5 min-w-0" title={i18nT('pages.chatSidebar.lane_derived_hint')}>
                          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: laneDef.color }} aria-hidden />
                          <span className="text-[11px] font-semibold uppercase tracking-wider truncate" style={{ color: laneDef.color }}>{i18nT(laneDef.labelKey)}</span>
                        </span>
                      ) : colTags.length === 0 ? (
                        <span className="text-[11px] text-muted font-semibold uppercase tracking-wider">{col.name || (col.include_untagged ? i18nT('pages.chatSidebar.untagged_2') : i18nT('pages.chatSidebar.all_sessions'))}</span>
                      ) : (
                        <>
                          {colTags.map(t => (
                            <span key={t.id} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>{t.name}</span>
                          ))}
                          {col.include_untagged && <span className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border border-dashed border-muted text-muted" title={i18nT('pages.chatSidebar.also_shows_untagged_sessions')}>{i18nT('pages.chatSidebar.untagged')}</span>}
                        </>
                      )}
                      {col.name && !laneDef && colTags.length > 0 && <span className="text-[11px] text-muted ml-1">· {col.name}</span>}
                      {/* A bare match-all column beside the lanes shows every
                        * session again, so the counts stop summing and cards
                        * appear twice. Seeding deliberately does not delete it
                        * (it is indistinguishable from a column the user added),
                        * so say what it is and let them decide. */}
                      {!laneDef && colTags.length === 0 && !col.name && !col.include_untagged
                        && orderedColumns.some(c => c.source === 'state') && (
                        <span data-testid={`column-duplicates-hint-${col.id}`} className="text-[10px] text-muted ml-1 truncate">
                          · {i18nT('pages.chatSidebar.lane_legacy_column_hint')}
                        </span>
                      )}
                    </div>
                    <span data-testid={`column-count-${col.id}`} className="text-[11px] text-muted shrink-0">{colSlots.length}</span>
                    <button type="button" data-testid={`column-new-folder-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.new_folder')} aria-label={i18nT('pages.chatSidebar.new_folder')} onClick={() => { setFolderModal({ mode: 'create', parentId: '' }) }}><FolderPlus size={12} /></button>
                    {!laneDef && <button type="button" data-testid={`column-edit-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.filter_manage_tags')} aria-label={i18nT('pages.chatSidebar.filter_manage_tags')} onClick={() => setColumnEditId(columnEditId === col.id ? null : col.id)}><TagIcon size={12} /></button>}
                    <button
                      type="button"
                      data-testid={`column-add-after-${col.id}`}
                      className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px] disabled:cursor-wait disabled:opacity-50"
                      title={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      aria-label={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      disabled={addColumnAfterMutation.isPending}
                      onClick={() => addColumnAfterMutation.mutate(col.id)}
                    ><Plus size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-delete-${col.id}`}
                      className="text-muted hover:text-danger bg-transparent border-none cursor-pointer shrink-0 p-[2px]"
                      title={i18nT('pages.chatSidebar.delete_column')}
                      aria-label={i18nT('pages.chatSidebar.delete_column')}
                      onClick={() => { deleteColumnWithConfirm(col.id) }}
                    ><X size={12} /></button>
                    {/* Overlays the top of the card list, so the cards under the
                      * pointer do not move while it shows. */}
                    {nativeSessionDrag !== null && derivedHintCol === col.id && (
                      <span role="status" aria-live="polite" data-testid={`column-derived-hint-${col.id}`}
                        className="absolute top-full left-0 right-0 z-10 pointer-events-none px-2 py-1 text-[11px] text-muted border-b border-border bg-bg-elevated">
                        {i18nT('pages.chatSidebar.lane_derived_hint')}
                      </span>
                    )}
                  </div>
                  {/* Column filter popover — portaled to the body-level overlay root so the column's
                      overflow-hidden ancestor cannot clip it; viewport-anchored
                      to the edit button via popoverPos. */}
                  {columnEditId === col.id && popoverPos && <OverlayPortal>{
                    /* Non-modal disclosure: role=dialog + a Tab-trap contains keyboard
                       focus, but we deliberately omit aria-modal — the popover has no
                       backdrop and is outside-click-dismissible, so claiming the rest of
                       the page is inert would mislead screen readers. */
                    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- Escape-dismiss and the Tab trap ARE a dialog's documented keyboard operation, and they have to live on the dialog root because the trap reasons about first/last focusable inside it; the onClick only stopPropagation
                    <div ref={columnPopoverRef} role="dialog" aria-label={i18nT('pages.chatSidebar.filter_tags', { name: col.name || 'column' })} tabIndex={-1} data-column-popover={col.id}
                      className="fixed z-[9100] bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px] outline-hidden"
                      style={{ top: popoverPos.top, left: popoverPos.left }}
                      onClick={e => e.stopPropagation()}
                      onKeyDown={e => {
                        if (e.key === 'Escape') { e.stopPropagation(); closeColumnPopover(col.id); return }
                        if (e.key !== 'Tab') return
                        // Trap Tab within the dialog — portal content sits at the end of
                        // <body>, so without this Tab would jump into unrelated page chrome.
                        const root = columnPopoverRef.current
                        if (!root) return
                        const f = Array.from(root.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])'))
                        if (f.length === 0) return
                        const first = f[0], last = f[f.length - 1]
                        const wrapsBackward = e.shiftKey && document.activeElement === first
                        const wrapsForward = !e.shiftKey && document.activeElement === last
                        // A mid-popover Tab is the browser's to move, and not the trap's
                        // to claim. A boundary Tab the IME owns must not cycle focus —
                        // the user is choosing a candidate, not leaving the field —
                        // so `claimKey` (native-event contract in useImeGuard.ts) runs
                        // before the preventDefault() and focus move.
                        if (!wrapsBackward && !wrapsForward) return
                        // `claimSyntheticKey` owns BOTH halves of a decline:
                        // the native event (which document/window listeners
                        // see) and React's own propagation flag (which it
                        // walks when dispatching to component ancestors), so a
                        // declined Tab cannot trigger an ancestor's keyboard
                        // handling.
                        if (!columnPopoverImeLatch.claimSyntheticKey(e)) return
                        e.preventDefault()
                        ;(wrapsBackward ? last : first).focus()
                      }}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">{i18nT('pages.chatSidebar.column_filter')}</span>
                        <button className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0" onClick={() => closeColumnPopover(col.id)} aria-label={i18nT('pages.chatSidebar.close')}><X size={13} /></button>
                      </div>
                      <Input className="w-full py-1 text-[12px] mb-2" placeholder={i18nT('pages.chatSidebar.column_name_optional')} defaultValue={col.name} onBlur={e => { const v = e.target.value.trim(); if (v !== col.name) updateColumnMutation.mutate({ id: col.id, body: { name: v } }) }} />
                      <div className="flex items-center gap-1 mb-2" role="radiogroup" aria-label={i18nT('pages.chatSidebar.match_mode')}>
                        {(['any', 'all', 'none'] as const).map(m => (
                          <button key={m} role="radio" aria-checked={col.mode === m} className={`text-[11px] px-2 py-0.5 rounded cursor-pointer border transition-all ${col.mode === m ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text'}`} onClick={() => updateColumnMutation.mutate({ id: col.id, body: { mode: m } })}>{m}</button>
                        ))}
                      </div>
                      {(() => {
                        // On a column with no tag filter, ticking the box narrows to
                        // untagged-only (the "Untagged" column), so the label and
                        // tooltip say "only". With a tag filter, ticking it ADDS
                        // untagged sessions, so they say "include". Keyed on the
                        // filter fields alone — never the display name — so the
                        // control's wording never depends on what the column is called.
                        const untaggedOnly = !col.tag_ids || col.tag_ids.length === 0
                        const label = untaggedOnly
                          ? i18nT('pages.chatSidebar.untagged_only')
                          : i18nT('pages.chatSidebar.include_untagged_sessions')
                        const title = untaggedOnly
                          ? i18nT('pages.chatSidebar.only_show_sessions_that_have_no_tags')
                          : i18nT('pages.chatSidebar.also_show_sessions_that_have_no_tags_at_all')
                        return (
                      <label htmlFor={`column-include-untagged-${col.id}`} className="flex items-center gap-2 px-1 py-1 mb-2 text-[11px] text-muted cursor-pointer select-none hover:text-text" title={title}>
                        <input
                          type="checkbox"
                          id={`column-include-untagged-${col.id}`}
                          data-testid={`column-include-untagged-${col.id}`}
                          aria-label={label}
                          checked={!!col.include_untagged}
                          onChange={e => updateColumnMutation.mutate({ id: col.id, body: { include_untagged: e.target.checked } })}
                          className="cursor-pointer"
                        />
                        {label}
                      </label>
                        )
                      })()}
                      <TagManagerList
                        mode="column-filter"
                        selectedIds={col.tag_ids}
                        onToggleTag={(_tagId, nextIds) => updateColumnMutation.mutate({ id: col.id, body: { tag_ids: nextIds } })}
                        createTestId={`tag-create-${col.id}`}
                      />
                      <div className="mt-2 flex justify-end">
                        <button className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer" onClick={() => { updateColumnMutation.mutate({ id: col.id, body: { tag_ids: [] } }) }}>{i18nT('pages.chatSidebar.clear_filter')}</button>
                      </div>
                    </div>
                  }</OverlayPortal>}
                  <SessionRowWindowScroller className="flex-1 overflow-y-auto scrollbar-none p-1.5 flex flex-col" style={{ scrollbarWidth: 'none' }}>
                    {/* No onDrop here: folder assignment only changes via folder-header drop
                        (into a folder) and the unfile strip below this scroller (out of one).
                        Cross-column drops are handled by the OUTER column onDrop
                        (which only mutates status tags, keeping folder_id intact). */}
                    {(() => {
                      const colSlotKeys = new Set(colSlots.map(sessionRowIdentity))
                      // Show ALL root folders as drop targets, not only those with matching slots.
                      // Empty folders render with "0" count so users see the structure they built.
                      // Root folders in explicit `order`-field order (the sorted
                      // rootFolders memo, same source as list view). Rendering the
                      // raw cache array here made drops appear to revert: a reorder
                      // only rewrites `order` values (array positions are
                      // unchanged), so an unsorted render ignored the new order.
                      //
                      // Flat view inside the board: the same view-only toggle as
                      // the list — folders stop rendering and every matching
                      // session sits directly in the lane, in filteredSlots
                      // order. Cross-lane card drag (the column onDrop above) is
                      // untouched; only folder rendering (and with it folder
                      // reorder/drop, which need folder headers) goes away.
                      // A folder the person unchecked in the filter menu drops out here
                      // for the same reason the tree drops it: the hide is a statement
                      // about the folder, not about one lane, so every lane that renders
                      // folder blocks answers to it. `isFolderHidden` is deliberately NOT
                      // applied -- a board column renders an empty folder header on
                      // purpose, as something to drop onto.
                      const relevantFolders = flatView ? [] : rootFolders.filter(f => !isFolderFilteredOut(f))
                      const { rows: ungrouped, navScope: colLaneScope, container: colHoldContainer } = heldLane(flatView
                        ? colSlots
                        : colSlots.filter(s => {
                            const folderId = sidebarRows.folderOf(s)
                            return !folderId || !folders.find(f => f.id === folderId)
                          }), col.id, `board:${col.id}:ungrouped`)
                      // In flat view folders never render, so an empty lane is
                      // empty — folder structure alone must not suppress the
                      // "no sessions" notice.
                      const hasAny = colSlots.length > 0 || (!flatView && folders.length > 0)
                      return (
                        <>
                          {/* Folder reorder in board view: one DndContext per
                           *  column (folder ids stay unique within it) + the
                           *  header as drag handle. Reorders flow through the
                           *  same global reorderFolders() as list view, so order
                           *  is consistent across columns. Native session-card
                           *  drop (HTML5 DnD) is untouched — it uses drag events,
                           *  not the pointer sensor. Skipped entirely in flat
                           *  view: no folder headers means nothing to drag, and
                           *  an empty context would still mount sensors and a
                           *  body portal per column for nothing. */}
                          {!flatView && (
                          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision} measuring={{ droppable: { strategy: MeasuringStrategy.Always } }} onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
                            <DndActiveProbe report={reportDndActive} />
                            <SortableContext items={relevantFolders.map(f => f.id)} strategy={verticalListSortingStrategy}>
                              {relevantFolders.map(f => <SortableColumnFolder key={f.id} folder={f} columnId={col.id} colSlotKeys={colSlotKeys} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} reorderable={folderReorderable} dragWithheld={folderDragWithheld} renderColumnFolder={renderColumnFolder} />)}
                            </SortableContext>
                            {/* Compact ghost follows the pointer while a folder drags —
                             *  same visual as the list-view overlay. DragOverlay renders
                             *  null unless THIS column's DndContext has an active drag,
                             *  so per-column overlays never stack. Portaled to
                             *  the body-level overlay root: the sidebar rides inside OverlayDrawer's
                             *  morph clip-path, and a clip-path clips fixed-position
                             *  descendants too, so an in-place overlay is erased the
                             *  moment the ghost strays past the drawer edge. React
                             *  portals preserve context, so the overlay still reads
                             *  THIS column's active drag. */}
                            <OverlayPortal>{
                              <DragOverlay dropAnimation={null}>
                                {activeDrag?.type === 'folder' ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} /> : null}
                              </DragOverlay>
                            }</OverlayPortal>
                          </DndContext>
                          )}
                          {ungrouped.map((s, i) => {
                            const isActive = isActiveRow(s)
                            const nextIsActive = isActiveRow(ungrouped[i + 1])
                            const showDivider = i < ungrouped.length - 1 && !isActive && !nextIsActive
                              && !startsAutomaticSection(ungrouped, i + 1)
                            return (
                              <Fragment key={sessionRowIdentity(s)}>
                                {startsAutomaticSection(ungrouped, i) && <PinnedSessionDivider />}
                                {renderSessionRow(s, 0, showDivider, colLaneScope, colLaneScope, colHoldContainer)}
                              </Fragment>
                            )
                          })}
                          {!hasAny && <div className="text-muted text-[12px] text-center py-4">{i18nT('pages.chatSidebar.no_sessions')}</div>}
                        </>
                      )
                    })()}
                  </SessionRowWindowScroller>
                  {/* The way back out of a folder, at the foot of the column and
                    * outside the scroller so it stays in reach however far the
                    * lane is scrolled. Only while a FILED card is in flight: an
                    * unfiled card has nothing to leave, and a column reorder is
                    * not a card. The strip's handlers stop propagation, so the
                    * column onDrop above never sees this release -- the retag
                    * and the unfile stay two gestures. */}
                  {boardDraggingFiledSession && <BoardUnfileDropStrip columnId={col.id} onUnfile={k => moveByDrag(k, null)} />}
                </div>
              )
            })}
          </div>
          {/* Edge cues: lanes continue past a clipped edge of the strip. Each is
            *  a 24 px band, the session tab strip's width, so the lane controls it
            *  overlaps stay legible and clickable. It fades from the foreground
            *  colour at low alpha, not from the page background as the tab
            *  strip's does: the lanes are cards, and in light theme the card and
            *  the page background are both near white, so a fade from bg over a
            *  card shows nothing. The foreground contrasts with the card in every
            *  theme. It is inset to the strip's padding, the cards' own vertical
            *  extent, so it shades the clipped lane and not the page gutter above
            *  and below it. It is fade-only, as the other scroll strips' cues are: the
            *  strip scrolls natively, and an arrow here would look like the
            *  follow-up bar's scroll buttons while a click on it fell through to the
            *  row underneath. */}
          {stripEdges.left && (
            <div aria-hidden="true" data-testid="column-strip-cue-left" className="pointer-events-none absolute left-0 top-2 bottom-2 w-6 z-10 bg-gradient-to-r from-text-strong/15 to-transparent" />
          )}
          {stripEdges.right && (
            <div aria-hidden="true" data-testid="column-strip-cue-right" className="pointer-events-none absolute right-0 top-2 bottom-2 w-6 z-10 bg-gradient-to-l from-text-strong/15 to-transparent" />
          )}
          </div>
          </div>
  )
  return { renderBoardLane }
}

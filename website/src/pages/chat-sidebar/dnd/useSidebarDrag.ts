/** The drag lifecycle: the start/over/end/cancel handlers, the folder drop writes
 *  (sibling renumber, re-parent) and the move-undo offers they arm. Each haptic tap
 *  fires only past every refusal, so a no-op drop stays silent. */
import { useCallback, type Dispatch, type SetStateAction, useRef, useEffect } from 'react'
import type { DragStartEvent, DragEndEvent, DragOverEvent } from '@dnd-kit/core'
import type { QueryClient } from '@tanstack/react-query'
import type { ChatFolder } from '../../../types'
import { computeSiblingReorder } from '../../../utils/reorderFolders'
import { haptic } from '../../../lib/haptic'
import { api } from '../../../api/client'
import { errMessage } from '../../../utils/thunkError'
import { i18nT } from '../../../i18n/t'
import { collectFolderSubtreeIds } from '../../../utils/folderTree'
import { useMoveSlotToFolder } from '../../../hooks/useMoveSlotToFolder'
import useMoveUndo from '../../../hooks/useMoveUndo'
import type { Slot } from '../types'
import { sessionRefBlockReason } from '../../../utils/sessionRefs'
import { boardColumnFromDroppableId } from '../../../utils/boardFolderCollapse'
import { CHAT_PANE_DROP_TYPE } from './collision'
import type { FolderSortModeRead } from '../../../hooks/useFolderSortMode'
import type { FolderMutations } from '../folders'

/** The two folder drop writes: sibling renumber and re-parent. */
export function useFolderDropOps({ folderReorderable, queryClient, setFolderActionError, updateFolderMutation }: {
  folderReorderable: boolean
  queryClient: QueryClient
  setFolderActionError: Dispatch<SetStateAction<string>>
  updateFolderMutation: FolderMutations['updateFolderMutation']
}) {
  const reorderFolders = useCallback((activeId: string, overId: string) => {
    if (activeId === overId) return
    // A sibling reorder is a write to the STORED positions computed against the
    // DRAWN order, sound only when the mode is known to be custom (see
    // `folderReorderable`). Outside that, the folder rows' droppable side is off,
    // so no pointer drag reaches here with a sibling as `over`; this guard is the
    // belt for the keyboard and scripted paths, and it is silent because the
    // restriction is expected -- the affordance is withdrawn, not a write that
    // failed. Re-parenting by drag is routed before this and still works.
    if (!folderReorderable) return
    // Read latest from cache to avoid stale-closure ordering on rapid successive drags
    const current = queryClient.getQueryData<ChatFolder[]>(['chat-folders']) ?? []
    // Scoped to the dragged folder's own container, not to the root lane: a
    // nested subfolder is reorderable among its siblings too, and `order` is a
    // per-container index either way. The helper refuses a target outside that
    // container, so a cross-container drop reaching here renumbers nothing --
    // that gesture is a re-parent and the collision layer routes it as one.
    const changes = computeSiblingReorder(current, activeId, overId)
    if (!changes.length) return
    // Past every refusal above: rows really renumber, so the drop seats here
    // and not in the caller, which cannot see which releases this helper drops.
    haptic('light')
    // Snapshot the pre-drag order of exactly the rows this drag renumbers, so a
    // rejected write can be rolled back field-scoped rather than by restoring a
    // whole-list snapshot (which would clobber a concurrent rename/move).
    const before = new Map(
      changes.map(c => [c.id, current.find(f => f.id === c.id)?.order]),
    )
    // Optimistic update
    queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old =>
      (old ?? []).map(f => {
        const c = changes.find(ch => ch.id === f.id)
        return c ? { ...f, order: c.order } : f
      })
    )
    // Persist as ONE atomic request. The endpoint applies the whole renumber
    // under the folder-store lock, all-or-none, so a mid-sequence failure
    // leaves the stored order untouched instead of half-applied -- the reason a
    // per-row PATCH loop is wrong here. On failure, roll back only the rows
    // this drag set, and only where the cache still holds its optimistic
    // value, then re-sync from the server.
    api.reorderChatFolders(changes).catch((e) => {
      setFolderActionError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong')))
      queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old =>
        (old ?? []).map(f => {
          if (!before.has(f.id)) return f
          const c = changes.find(ch => ch.id === f.id)
          return c && f.order === c.order ? { ...f, order: before.get(f.id) as number } : f
        })
      )
      queryClient.invalidateQueries({ queryKey: ['chat-folders'] })
    })
  }, [queryClient, folderReorderable, setFolderActionError])
  // Re-parent a folder: move it into `parentId`, or to the top level (null).
  // Client-side guards mirror the server (self/descendant targets rejected)
  // so an invalid pick or drop is a silent no-op instead of a 400 round-trip.
  // `opts.onCommitted` fires once the server has ACKNOWLEDGED the write (the
  // optimistic cache patch is not the same fact) — the drag-move undo offer
  // arms on it. A guarded no-op never acknowledges, so an offer armed over one
  // simply expires unarmed.
  const moveFolderTo = useCallback((folderId: string, parentId: string | null, opts?: { onCommitted?: () => void }) => {
    const current = queryClient.getQueryData<ChatFolder[]>(['chat-folders']) ?? []
    const folder = current.find(f => f.id === folderId)
    if (!folder) return
    const target = parentId ?? ''
    if ((folder.parent_id || '') === target) return
    if (target && collectFolderSubtreeIds(current, folderId).has(target)) return
    updateFolderMutation.mutate({ id: folderId, body: { parent_id: target }, onCommitted: opts?.onCommitted })
  }, [queryClient, updateFolderMutation])
  return { reorderFolders, moveFolderTo }
}

/** Drag moves of a session or folder and the undo offers they arm. */
export function useSidebarMoveUndo({ localSlots, folders, moveFolderTo, queryClient }: {
  localSlots: Slot[]
  folders: ChatFolder[]
  moveFolderTo: (folderId: string, parentId: string | null, opts?: { onCommitted?: (() => void) | undefined; } | undefined) => void
  queryClient: QueryClient
}) {
  // Shared optimistic move (also used by the session-header dropdown and
  // drag-to-folder) — single source of truth for slot→folder assignment. Both
  // the menu "Move to folder" submenus and drag-to-folder route through this.
  const assignToFolder = useMoveSlotToFolder()
  // ── Drag-move undo ────────────────────────────────────────────────────────
  // The offer's whole lifecycle — pending until the server acks, one-way to
  // gone, superseded latched on a third-party placement, plus the 8s deadline
  // and its hover hold — lives in useMoveUndo. This surface supplies only the
  // three things that are specific to sessions: where a slot sits, how to move
  // it, and whether a folder id is still real.
  //
  // Only DRAG-initiated moves arm it. Menu moves ("Move to folder…") pick the
  // destination by name, so there is nothing unnamed to confirm.
  const locateSlotFolder = useCallback((slotKey: string) => {
    const slot = localSlots.find(s => s.key === slotKey)
    // `undefined` = session closed (retire the offer); `null` = unfiled root.
    return slot ? (slot.folder_id || null) : undefined
  }, [localSlots])
  const folderStillExists = useCallback(
    (folderId: string) => folders.some(f => f.id === folderId),
    [folders],
  )
  const {
    offer: dragMove,
    arm: armDragMove,
    undo: undoDragMove,
    dismiss: dismissDragMove,
    bar: undoBar,
  } = useMoveUndo({ locate: locateSlotFolder, apply: assignToFolder, folderExists: folderStillExists })
  // Folder re-parenting gets its own offer: same lifecycle, folder-specific
  // deps (a folder sits under `parent_id`, moves through moveFolderTo). Only
  // the DRAG call sites in handleSidebarDragEnd arm it — the "Move to folder…"
  // picker names its destination, so there is nothing unnamed to confirm.
  // The two offers share ONE visual slot: arming either DISMISSES the other,
  // so a displaced offer is retired rather than hidden — a hidden-but-live
  // offer would resurrect when the winner retires, and its exiting bar would
  // hold a second ⌘Z listener able to undo a move the user no longer sees.
  const locateFolderParent = useCallback((folderId: string) => {
    const f = folders.find(x => x.id === folderId)
    // `undefined` = folder deleted (retire the offer); `null` = top level.
    return f ? (f.parent_id || null) : undefined
  }, [folders])
  const {
    offer: folderMove,
    arm: armFolderMove,
    undo: undoFolderMove,
    dismiss: dismissFolderMove,
    bar: folderUndoBar,
  } = useMoveUndo({ locate: locateFolderParent, apply: moveFolderTo, folderExists: folderStillExists })
  const moveByDrag = useCallback((slotKey: string, folderId: string | null) => {
    const slot = localSlots.find(s => s.key === slotKey)
    const to = folderId || null
    // A drop back onto the session's current folder arms nothing (arm's own
    // no-op check) — and must not dismiss the folder offer for nothing either.
    if ((slot?.folder_id || null) === to) return
    haptic('light')
    const dest = to ? folders.find(f => f.id === to) : undefined
    dismissFolderMove()
    armDragMove({
      itemKey: slotKey,
      fromFolderId: slot?.folder_id || null,
      toFolderId: to,
      toFolderName: dest?.name ?? null,
      toFolderColor: dest?.color,
      itemTitle: slot?.title || slotKey,
    })
  }, [localSlots, folders, armDragMove, dismissFolderMove])
  const moveFolderByDrag = useCallback((folderId: string, parentId: string | null) => {
    // Same guards as moveFolderTo, so a drop it would refuse arms no offer
    // (arm's own no-op check only covers the same-parent case).
    const current = queryClient.getQueryData<ChatFolder[]>(['chat-folders']) ?? []
    const folder = current.find(f => f.id === folderId)
    if (!folder) return
    const target = parentId ?? ''
    if ((folder.parent_id || '') === target) return
    if (target && collectFolderSubtreeIds(current, folderId).has(target)) return
    haptic('light')
    const dest = parentId ? current.find(f => f.id === parentId) : undefined
    dismissDragMove()
    armFolderMove({
      itemKey: folderId,
      fromFolderId: folder.parent_id || null,
      toFolderId: parentId,
      toFolderName: dest?.name ?? null,
      toFolderColor: dest?.color,
      itemTitle: folder.name,
    })
  }, [queryClient, armFolderMove, dismissDragMove])
  return { dragMove, undoDragMove, undoBar, folderMove, undoFolderMove, folderUndoBar, moveByDrag, moveFolderByDrag }
}

/** The DndContext lifecycle handlers shared by every lane. */
export function useSidebarDragHandlers({ releaseHoverPin, setDragFrozen, hideFolderReorderHint, setActiveDrag, activeDrag, dragFrozen, folderReorderable, folderSortRead, showFolderReorderHint, moveFolderByDrag, reorderFolders, searchRanked, pinned, reorderPinned, localSlots, activeSlot, onDropSessionRef, moveByDrag, folders, boardFolderCollapsed, updateFolderMutation, clearBoardCollapse }: {
  releaseHoverPin: () => void
  setDragFrozen: Dispatch<SetStateAction<boolean>>
  hideFolderReorderHint: () => void
  setActiveDrag: Dispatch<SetStateAction<{ type: string; id: string; } | null>>
  activeDrag: { type: string; id: string; } | null
  dragFrozen: boolean
  folderReorderable: boolean
  folderSortRead: FolderSortModeRead
  showFolderReorderHint: () => void
  moveFolderByDrag: (folderId: string, parentId: string | null) => void
  reorderFolders: (activeId: string, overId: string) => void
  searchRanked: Map<string, number> | null
  pinned: Set<string>
  reorderPinned: (activeKey: string, overKey: string) => void
  localSlots: Slot[]
  activeSlot: string | null
  onDropSessionRef: ((ref: { key: string; title: string; messages?: number | undefined; }) => void) | undefined
  moveByDrag: (slotKey: string, folderId: string | null) => void
  folders: ChatFolder[]
  boardFolderCollapsed: (columnId: string, folder: ChatFolder) => boolean
  updateFolderMutation: FolderMutations['updateFolderMutation']
  clearBoardCollapse: (folderId: string, columnId?: string) => void
}) {
  // Unified dnd-kit handlers for the legacy single-lane layout. One DndContext
  // owns both folder reordering (sortable) and session drag-to-assign
  // (draggable rows + droppable folder/root targets); the active item's
  // data.type routes the drop.
  const handleSidebarDragStart = useCallback((e: DragStartEvent) => {
    // Past the sensor's hold/distance constraint: the row is really picked up.
    // Nothing on screen has moved yet under a finger, so the hand is told here.
    haptic('medium')
    // Drop the hold FIRST: the freeze below pins the list dnd-kit's drop math is
    // computed against, and a displaced row would make the render disagree with it.
    releaseHoverPin()
    setDragFrozen(true)
    // A new gesture retires the previous one's hint; its drop will say its own.
    hideFolderReorderHint()
    const d = e.active.data.current as { type?: string; key?: string } | undefined
    if (d?.type === 'session' && d.key) setActiveDrag({ type: 'session', id: d.key })
    else if (d?.type === 'folder') setActiveDrag({ type: 'folder', id: e.active.id as string })
  }, [releaseHoverPin, hideFolderReorderHint, setActiveDrag, setDragFrozen])
  // The one place the drag mirror is torn down: end, cancel, and the
  // reconciler below all go through it so none can leave a piece behind.
  const resetSidebarDrag = useCallback(() => {
    setActiveDrag(null)
    setDragFrozen(false)
    if (dragExpandTimer.current) { clearTimeout(dragExpandTimer.current.timer); dragExpandTimer.current = null }
  }, [setActiveDrag, setDragFrozen])
  // Which DndContexts currently hold an active drag, as reported by their
  // DndActiveProbe. A ref, not state: the probes write it from layout effects
  // and the reconciler reads it from a passive effect in the same commit.
  const dndActiveContexts = useRef(new Set<string>())
  const reportDndActive = useCallback((id: string, active: boolean) => {
    if (active) dndActiveContexts.current.add(id)
    else dndActiveContexts.current.delete(id)
  }, [])
  // Reconcile the mirror with dnd-kit's store after every commit: a live
  // mirror with no context reporting a drag is a gesture whose end dnd-kit
  // never delivered (see DndActiveProbe). Deliberately dependency-free — the
  // store can go idle in a commit that changes neither mirror value, and the
  // check is two reads against a ref.
  useEffect(() => {
    if (activeDrag === null && !dragFrozen) return
    if (dndActiveContexts.current.size > 0) return
    resetSidebarDrag()
  })
  const handleSidebarDragEnd = useCallback((event: DragEndEvent) => {
    resetSidebarDrag()
    // The drop seats only where the sidebar ACTS. The move/reorder helpers tap
    // past their own refusals (same folder, own subtree, unknown target), so a
    // release they drop is felt as nothing, the same as a release over empty
    // space; the one drop handled inline below taps after its own guard.
    const { active, over } = event
    const a = active.data.current as {
      type?: string
      key?: string
      nested?: boolean
      pinned?: boolean
      container?: string
    } | undefined
    const o = over?.data.current as {
      type?: string
      key?: string
      folderId?: string | null
      container?: string
    } | undefined
    // Outside Custom the folder rows' droppable side is off, so a sibling
    // reorder drag resolves to NO target (or, on a scripted path, to a sortable
    // hit that `reorderFolders` refuses) -- either way nothing moves, and the
    // person is told why at the point of the drop. A folder-drop hit is the
    // re-parent gesture, which still works in every mode, so it falls through to
    // the handling below. Only when the mode is KNOWN (a config body is on hand):
    // before the first read, or after one that failed with nothing to fall back
    // on, the withdrawal is the read's, and the read's own banner says so.
    if (a?.type === 'folder' && !folderReorderable && folderSortRead.known && o?.type !== 'folder-drop') {
      showFolderReorderHint()
      return
    }
    if (!over) return
    if (a?.type === 'folder') {
      if (a.nested) {
        // Nested subfolder drag, both gestures. A folder-drop hit is the
        // re-parent: into that folder, or to the top level when dropped on the
        // root lane (folderId null). moveFolderByDrag itself no-ops on the
        // folder's current parent, so a drop resolving to it (easy to hit now
        // that a tall parent's whole block is a reachable target) costs no write.
        if (o?.type === 'folder-drop') {
          moveFolderByDrag(active.id as string, o.folderId ?? null)
          return
        }
        // Otherwise a sortable hit (over.id = a sibling's folder id) = reorder
        // among siblings, the same call the root lane makes. reorderFolders
        // renumbers only the dragged folder's own container and refuses a target
        // outside it, so a stray resolution is a no-op rather than a wrong move.
        reorderFolders(active.id as string, over.id as string)
        return
      }
      // Root folder drag: a folder-drop hit only occurs via the header-band
      // gesture in sidebarCollision = re-parent INTO that folder. A sortable
      // hit (over.id = folder id) is the reorder-among-siblings gesture.
      if (o?.type === 'folder-drop') {
        if (o.folderId) moveFolderByDrag(active.id as string, o.folderId)
        return
      }
      reorderFolders(active.id as string, over.id as string)
      return
    }
    if (a?.type === 'session' && a.key) {
      if (!searchRanked && o?.type === 'pinned-session' && o.key
        && a.pinned === true && a.container === o.container
        && pinned.has(a.key) && pinned.has(o.key)) {
        reorderPinned(a.key, o.key)
        return
      }
      // Drop targets, innermost-first via pointerWithinDeepest:
      //  chat-pane-ref → stage a LINK to this session in the open chat's composer
      //  folder-drop  → assign to that folder (folderId may be null for root lane)
      //  folder       → sortable folder container (whole block) → assign to its id
      if (o?.type === CHAT_PANE_DROP_TYPE) {
        const src = localSlots.find(x => x.key === a.key)
        // Re-decide at drop time rather than trusting the drag-start snapshot:
        // the refusal must not depend on the affordance having been rendered,
        // and memory_mode can change mid-drag. Same function the zone uses.
        if (sessionRefBlockReason({ key: a.key, activeSlot, memoryMode: src?.memory_mode })) return
        haptic('light')
        onDropSessionRef?.({
          key: a.key,
          title: src?.title && src.title !== src.key ? src.title : a.key,
          messages: src?.messages,
        })
        return
      }
      if (o?.type === 'folder-drop') moveByDrag(a.key, o.folderId ?? null)
      else if (o?.type === 'folder') moveByDrag(a.key, over.id as string)
    }
  }, [resetSidebarDrag, reorderFolders, reorderPinned, searchRanked, pinned, moveByDrag, moveFolderByDrag, localSlots, activeSlot, onDropSessionRef, folderReorderable, folderSortRead.known, showFolderReorderHint])
  const handleSidebarDragCancel = resetSidebarDrag
  // Auto-expand collapsed folders when a dragged item hovers over them for 500ms.
  const dragExpandTimer = useRef<{ id: string; timer: ReturnType<typeof setTimeout> } | null>(null)
  const handleSidebarDragOver = useCallback((event: DragOverEvent) => {
    const over = event.over
    const overData = over?.data.current as { type?: string; folderId?: string | null } | undefined
    const targetFolderId = overData?.type === 'folder-drop' ? overData.folderId : null
    // If hovering a collapsed folder, blink ring twice then expand. In a board
    // column, "collapsed" is that column's effective state (server flag +
    // column override), and the expansion must clear the column's override —
    // the server flag alone can read expanded while the hovered copy is
    // collapsed by its override, which would leave the drop target shut.
    if (targetFolderId) {
      const overColumnId = over ? boardColumnFromDroppableId(String(over.id)) : null
      const f = folders.find(x => x.id === targetFolderId)
      const effectiveCollapsed = f ? (overColumnId ? boardFolderCollapsed(overColumnId, f) : !!f.collapsed) : false
      const expandTarget = () => {
        if (f?.collapsed) updateFolderMutation.mutate({ id: targetFolderId, body: { collapsed: false } })
        if (overColumnId) {
          clearBoardCollapse(targetFolderId, overColumnId)
        }
      }
      if (effectiveCollapsed) {
        if (dragExpandTimer.current?.id !== targetFolderId) {
          if (dragExpandTimer.current) clearTimeout(dragExpandTimer.current.timer)
          dragExpandTimer.current = {
            id: targetFolderId,
            timer: setTimeout(() => {
              // Blink the folder ring twice before expanding
              const el = document.querySelector(`[data-folder-drop="${targetFolderId}"]`) as HTMLElement | null
              if (el) {
                const ring = 'inset 0 0 0 2px var(--accent)'
                const dim = () => { el.style.boxShadow = ring; el.style.opacity = '0.4' }
                const bright = () => { el.style.boxShadow = ring; el.style.opacity = '1' }
                bright(); setTimeout(dim, 100); setTimeout(bright, 200); setTimeout(dim, 300)
                setTimeout(() => {
                  el.style.boxShadow = ''; el.style.opacity = ''
                  expandTarget()
                  dragExpandTimer.current = null
                }, 450)
              } else {
                expandTarget()
                dragExpandTimer.current = null
              }
            }, 500),
          }
        }
        return
      }
    }
    // Moved away from the folder or it's already expanded — clear timer
    if (dragExpandTimer.current) {
      clearTimeout(dragExpandTimer.current.timer)
      dragExpandTimer.current = null
    }
  }, [folders, updateFolderMutation, boardFolderCollapsed, clearBoardCollapse])
  return {
    handleSidebarDragStart, reportDndActive, handleSidebarDragEnd, handleSidebarDragCancel,
    handleSidebarDragOver,
  }
}

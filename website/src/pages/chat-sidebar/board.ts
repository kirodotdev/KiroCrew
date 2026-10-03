/** The tag-column board: the columns query and its config flags, the column filter
 *  popover, column writes, state-lane seeding with its one-time auto-widen, per-column
 *  folder collapse, and column membership. */
import { useQuery, useMutation, type QueryClient } from '@tanstack/react-query'
import { useState, useEffect, useMemo, useRef, useCallback, type Dispatch, type SetStateAction, type MutableRefObject } from 'react'
import { LAYOUT } from '../../components/layout'
import { SIDEBAR_MAX, SIDEBAR_MIN } from '../chat/sidebarWidth'
import type { TagColumn, TagColumnMode, ChatFolder } from '../../types'
import { api } from '../../api/client'
import { loadChatConfig } from '../chat/ChatSettings'
import { useDocumentImeLatch } from '../../hooks/useImeGuard'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import { SESSION_LANES, inferLane } from '../chat/sessionLane'
import { normalizeRunSessionKey } from '../../apps/workflows/runModel'
import type { Slot } from './types'
import { loadBoardFolderCollapse, boardCollapseKey, persistBoardOverride, clearFolderOverrides, persistClearFolderOverrides } from '../../utils/boardFolderCollapse'

/** Board column geometry, mirrored from the column strip's own classes:
 *  `min-w-[220px]` per column, `gap-2` between them, `p-2` around the strip. */
const BOARD_COL_MIN_W = 220
const BOARD_COL_GAP = 8
const BOARD_STRIP_PAD = 16
/** Leave this much for the chat pane when widening the sidebar for a board, so
 *  a wide board never squeezes the conversation out of the window. */
const BOARD_CHAT_RESERVE = 520

/** How wide the sidebar must be for `count` board columns to fit without
 *  horizontal scrolling — clamped to the sidebar's own ceiling and to what the
 *  viewport can spare once the nav rail and a usable chat pane are subtracted.
 *  Returns the CURRENT width when nothing wider is available, so the caller can
 *  only ever widen. On a narrow window the strip keeps a little horizontal
 *  scroll rather than burying the conversation: four 220px lanes and a readable
 *  chat pane genuinely do not both fit below roughly 1700px.
 */
export function boardSidebarWidth(count: number, current: number, viewport: number): number {
  if (count <= 0) return current
  const wanted = count * BOARD_COL_MIN_W + (count - 1) * BOARD_COL_GAP + BOARD_STRIP_PAD
  const spare = viewport - LAYOUT.NAV_WIDTH - BOARD_CHAT_RESERVE
  const ceiling = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, spare))
  return Math.max(current, Math.min(wanted, ceiling))
}

/** The board columns query and the two chat-config flags it rides with. */
export function useBoardColumns() {
  // Sidebar column layout (flat list; empty = legacy single-lane UX)
  const {
    data: rawColumns = [],
    isFetched: tagColumnsSettled,
    isError: columnsFailed,
    error: columnsError,
    refetch: refetchColumns,
  } = useQuery<TagColumn[]>({ queryKey: ['tag-columns'], queryFn: () => api.tagColumns() })
  const [tagColumnsEnabled, setTagColumnsEnabled] = useState(() => loadChatConfig().tagColumnsEnabled)
  // Opt-in: off, an empty folder keeps its labelled "New chat in <name>" row
  // exactly as before. On, it has no body at all and its row stops presenting as
  // a control. Read through the same `mc-config-changed` listener as the flag
  // above so toggling it in Settings reshapes the open sidebar immediately.
  const [hideEmptyFolderBody, setHideEmptyFolderBody] = useState(() => loadChatConfig().hideEmptyFolderBody)
  useEffect(() => {
    const onChange = () => {
      const cfg = loadChatConfig()
      setTagColumnsEnabled(cfg.tagColumnsEnabled)
      setHideEmptyFolderBody(cfg.hideEmptyFolderBody)
    }
    window.addEventListener('mc-config-changed', onChange)
    return () => window.removeEventListener('mc-config-changed', onChange)
  }, [])
  // When feature is disabled, treat it as zero columns → sidebar falls back to legacy layout.
  // Derive the effective column list inside the memo so its identity only changes
  // when the stable inputs (rawColumns / tagColumnsEnabled) change, not every render.
  const orderedColumns = useMemo(() => {
    const columns: TagColumn[] = tagColumnsEnabled ? rawColumns : []
    return [...columns].sort((a, b) => a.order - b.order)
  }, [rawColumns, tagColumnsEnabled])
  return {
    rawColumns, tagColumnsSettled, columnsFailed, columnsError, refetchColumns, tagColumnsEnabled,
    hideEmptyFolderBody, orderedColumns,
  }
}

/** Position, focus and outside-click dismissal of the portaled column filter popover. */
export function useColumnPopover() {
  const [columnEditId, setColumnEditId] = useState<string | null>(null)  // column whose popover is open
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(null)
  // The column-filter popover is portaled to <body>, so it is outside the trigger's
  // DOM tab-order and never receives focus on open. columnPopoverRef + the effect
  // below move focus into it, and closeColumnPopover returns focus to the trigger —
  // together with the onKeyDown (Escape + Tab-trap) on the popover, this makes the
  // portaled overlay fully keyboard-operable.
  const columnPopoverRef = useRef<HTMLDivElement>(null)
  // Shared IME latch for the popover's Tab trap: a Tab that lands during an
  // IME composition (or its post-`compositionend` window) is choosing a
  // candidate, not leaving the field, so the trap must decline it instead of
  // yanking focus and aborting the composition (`useDialogFocusTrap` is the
  // reference consumer of the same seam).
  const columnPopoverImeLatch = useDocumentImeLatch(columnEditId !== null)
  const closeColumnPopover = useCallback((colId: string) => {
    setColumnEditId(null)
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-testid="column-edit-${colId}"]`)?.focus())
  }, [setColumnEditId])
  // Anchor the popover to the edit button's bounding rect so it stays put even
  // though it renders in a portal outside the (overflow-hidden) column ancestor.
  useEffect(() => {
    if (!columnEditId) { setPopoverPos(null); return }
    const updatePos = () => {
      const btn = document.querySelector<HTMLElement>(`[data-testid="column-edit-${columnEditId}"]`)
      if (!btn) return
      const r = btn.getBoundingClientRect()
      setPopoverPos({ top: r.bottom + 4, left: r.left })
    }
    updatePos()
    window.addEventListener('resize', updatePos)
    window.addEventListener('scroll', updatePos, true)
    return () => {
      window.removeEventListener('resize', updatePos)
      window.removeEventListener('scroll', updatePos, true)
    }
  }, [columnEditId])
  // Close column-filter popover on outside click
  useEffect(() => {
    if (!columnEditId) return
    const handler = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (!t) return
      if (t.closest(`[data-column-popover="${columnEditId}"]`)) return
      if (t.closest(`[data-testid="column-edit-${columnEditId}"]`)) return
      setColumnEditId(null)
    }
    // Defer one tick so the same click that opened the popover doesn't immediately close it
    const id = setTimeout(() => document.addEventListener('mousedown', handler), 0)
    return () => { clearTimeout(id); document.removeEventListener('mousedown', handler) }
  }, [columnEditId, setColumnEditId])
  // Move focus into the portaled column-filter popover once it is positioned. We
  // focus the dialog container itself (tabIndex=-1) — not its first control — so the
  // screen reader announces the dialog and Tab then walks its fields in order; this
  // avoids landing on the Close button (first in DOM) or stealing focus into a text field.
  useEffect(() => {
    if (!columnEditId || !popoverPos) return
    // Focus only on initial open. popoverPos gets a fresh object on every
    // resize/scroll reflow, re-running this effect — so bail if focus is already
    // inside the popover (e.g. the user is typing in the rename input) to avoid
    // yanking it back to the container.
    if (columnPopoverRef.current?.contains(document.activeElement)) return
    const raf = requestAnimationFrame(() => columnPopoverRef.current?.focus())
    return () => cancelAnimationFrame(raf)
  }, [columnEditId, popoverPos])
  return { columnEditId, setColumnEditId, popoverPos, columnPopoverRef, columnPopoverImeLatch, closeColumnPopover }
}

/** Column writes and the state-lane seeding. */
export function useBoardColumnMutations({ queryClient, setBoardError, orderedColumns, rawColumns, paintedWidthRef, widenForBoard, setSeedError }: {
  queryClient: QueryClient
  setBoardError: Dispatch<SetStateAction<string>>
  orderedColumns: TagColumn[]
  rawColumns: TagColumn[]
  /** The width the sidebar is painted at, which a window narrower than the
   *  saved width holds below it (see sidebarPaintWidth). */
  paintedWidthRef: MutableRefObject<number>
  widenForBoard: (next: number) => void
  setSeedError: Dispatch<SetStateAction<string>>
}) {
  /** One reader for every board write: the rejections are ApiErrors or plain
   *  Errors, and the strip's banner shows whichever message they carry. */
  const updateColumnMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; tag_ids?: string[]; mode?: TagColumnMode; order?: number; include_untagged?: boolean } }) => api.updateTagColumn(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
    // The server rejects a tag_ids payload naming an unknown tag (400
    // invalid_column_payload) instead of silently dropping it — e.g. the tag
    // was deleted from another window while this popover's cache was stale.
    // Re-sync both caches so the popover redraws from reality (the stale tag
    // disappears) rather than leaving a selection that looks applied but isn't.
    onError: (e) => {
      setBoardError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong')))
      queryClient.invalidateQueries({ queryKey: ['chat-tags'] })
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
    },
  })
  const deleteColumnMutation = useMutation({
    mutationFn: (id: string) => api.deleteTagColumn(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
    onError: (e) => setBoardError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong'))),
  })
  const reorderColumnsMutation = useMutation({
    mutationFn: (ids: string[]) => api.reorderTagColumns(ids),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
    onError: (e) => setBoardError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong'))),
  })
  const addColumnAfterMutation = useMutation({
    mutationFn: async (afterColId: string) => {
      const created = await api.createTagColumn({ name: '', tag_ids: [], mode: 'any' })
      const ids = orderedColumns.map(c => c.id)
      const idx = ids.indexOf(afterColId)
      ids.splice(idx + 1, 0, created.id)
      const uniqIds: string[] = []
      for (const id of ids) { if (!uniqIds.includes(id)) uniqIds.push(id) }
      await api.reorderTagColumns(uniqIds)
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
    // The create may have landed before the reorder failed: re-sync so the new
    // lane shows where the server put it rather than nowhere.
    onError: (e) => {
      setBoardError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong')))
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
    },
  })
  const dropSlotMutation = useMutation({
    mutationFn: ({ slot, columnId }: { slot: string; columnId: string }) => api.dropSlotToColumn(slot, columnId),
    // The moved row lands in its new lane on the next authoritative sseSlots
    // push; this handler does not eagerly reflect the tag change. The previous
    // dead-key `invalidateQueries(['chat-slots'])` was a no-op (no query is
    // registered on that key; slot.tags lives in the Redux dashboard slice). An
    // eager client-side patch here -- and surfacing the endpoint's 200-level
    // {ok:false} refusal -- needs the same per-field reconciliation contract as
    // the bulk-model site; both belong to the whole-list applySlots
    // reducer-contract work in #11149, not this rename-recovery fix.
    onSuccess: () => {},
    onError: (e) => setBoardError((errMessage(e) || i18nT('components.errorBoundary.something_went_wrong'))),
  })
  /** Lanes the board does not have yet. Drives the seeding write and the menu
   *  affordance, so the offer to add lanes appears exactly when there is
   *  something to add — including after a partial failure left the set
   *  incomplete, which is what makes "click again to finish" a real recovery
   *  path rather than a claim.
   *
   *  This is an AFFORDANCE, not the uniqueness rule. It reads a cached column
   *  list, so two dashboards can both compute the same missing lane; the backend
   *  decides uniqueness by `state_key` under its write lock and returns the
   *  existing lane instead of creating a second one. */
  const missingLanes = useMemo(() => {
    const present = new Set(rawColumns.filter(c => c.source === 'state').map(c => c.state_key))
    return SESSION_LANES.filter(lane => !present.has(lane.key))
  }, [rawColumns])

  /** Add the four derived state lanes to the board.
   *
   *  Purely ADDITIVE and IDEMPOTENT: it creates only the lanes that are missing
   *  and never deletes a column. That is the invariant, not an implementation
   *  detail — an additive action that also disposes of persisted rows has to
   *  guess which ones are disposable, and the unnamed match-all shape the view
   *  toggle once created is byte-identical to a bare column the user added
   *  themselves via "Add column after". No predicate can separate them, so the
   *  only safe answer is to delete neither.
   *
   *  Consequences of the invariant, all of them deliberate:
   *  - There is no two-write ordering to get wrong, so a mid-flight failure
   *    leaves fewer lanes rather than a board stripped of its columns; clicking
   *    again completes the set because creation is keyed on what is missing.
   *  - A pre-existing bare column survives and sits beside the lanes, showing
   *    every session. It is one click to remove and is not ours to delete.
   *  - Re-running is harmless, which is what makes the pending-guard on the
   *    menu items a second line of defence rather than the only one.
   */
  const seedStateLanesMutation = useMutation({
    mutationFn: async () => {
      for (const lane of missingLanes) {
        await api.createTagColumn({ source: 'state', state_key: lane.key, tag_ids: [], mode: 'any' })
      }
      return rawColumns.length + missingLanes.length
    },
    onSuccess: (columnCount: number) => {
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
      // A board is a horizontal strip inside a 260px-default sidebar, so lanes
      // that do not fit are reachable only by discovering the resize handle.
      // Widen once to fit them; never shrink, so a width the user chose stands.
      // Compare against the painted width, what the lanes actually get: a
      // saved width wider than this window allows is clipped on screen, so it
      // says nothing about whether the lanes fit.
      const painted = paintedWidthRef.current
      const next = boardSidebarWidth(columnCount, painted, window.innerWidth)
      // Remembering what the user had (the saved width, not the painted one)
      // is the width owner's job (widenForBoard).
      if (next !== painted) widenForBoard(next)
    },
    onError: (err) => {
      // Without this the toggle has already flipped to board view and nothing
      // renders: no board, no message, no way to tell it failed from an empty
      // one. Report it and hand back list view when nothing was created.
      setSeedError(err instanceof Error ? err.message : String(err))
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
    },
  })
  return {
    updateColumnMutation, deleteColumnMutation, reorderColumnsMutation, addColumnAfterMutation,
    dropSlotMutation, missingLanes, seedStateLanesMutation,
  }
}

/** The column writes, typed once for the owners that call them. */
export type BoardColumnMutations = ReturnType<typeof useBoardColumnMutations>

/** Whether a slot belongs in a column. */
export function useColumnMatches({ subagentCounts, subagentApprovalCounts, workflowActiveSet, automationRunningSet }: {
  subagentCounts: Record<string, number>
  subagentApprovalCounts: Record<string, number>
  workflowActiveSet: Set<string>
  automationRunningSet: Set<string>
}) {
  // Filter predicate for a single column. Takes the whole slot, not just its
  // tags: a state column's membership is derived from live runtime fields, and
  // a lane needs the same extras the row status chain uses (a parent whose
  // sub-agent is blocked owes an approval even though the parent is idle).
  const columnMatches = useCallback((col: TagColumn, slot: Slot): boolean => {
    if (col.source === 'state') {
      if (!col.state_key) return false
      // Clamped against the running count exactly as the row status chain does:
      // an approval count above the live agent count is stale, and unclamped it
      // would pin an otherwise-idle session to Needs Approval indefinitely.
      const running = subagentCounts[slot.key] || 0
      // `slot` here is the raw payload, whose `running` covers only the slot's
      // own turn. A dynamic workflow and a goal loop are both live work that
      // outlive that flag, and the row status chain already reads them from the
      // store — so the lane must too, or a session renders a workflow spinner
      // while sitting in Idle.
      return inferLane(slot, {
        subagentAwaiting: Math.min(subagentApprovalCounts[slot.key] || 0, running),
        workflowActive: workflowActiveSet.has(normalizeRunSessionKey(slot.key)),
        goalLoopActive: automationRunningSet.has(slot.key),
        detailedSubagentsRunning: running > 0,
      }) === col.state_key
    }
    const slotTags = slot.tags || []
    // "include untagged" OR'd on top of any tag filter
    if (col.include_untagged && slotTags.length === 0) return true
    if (!col.tag_ids || col.tag_ids.length === 0) return true
    const set = new Set(slotTags)
    if (col.mode === 'all') return col.tag_ids.every(t => set.has(t))
    if (col.mode === 'none') return !col.tag_ids.some(t => set.has(t))
    return col.tag_ids.some(t => set.has(t))  // 'any'
  }, [subagentApprovalCounts, subagentCounts, automationRunningSet, workflowActiveSet])
  return { columnMatches }
}

/** Per-column folder collapse overrides over the server flag. */
export function useBoardFolderCollapse() {
  // Board-view collapse is per (column, folder): the same root folders render
  // once per column, and the shared server flag would collapse a folder in
  // every column at once. Overrides are client-local (localStorage) and layer
  // over the server flag, which stays the default for untouched columns and
  // the sole state for the list view.
  const [boardCollapse, setBoardCollapse] = useState<Map<string, boolean>>(loadBoardFolderCollapse)
  const boardFolderCollapsed = useCallback((columnId: string, folder: ChatFolder): boolean => {
    return boardCollapse.get(boardCollapseKey(columnId, folder.id)) ?? !!folder.collapsed
  }, [boardCollapse])
  const toggleColumnCollapse = useCallback((columnId: string, folder: ChatFolder) => {
    setBoardCollapse(prev => {
      const next = new Map(prev)
      const value = !(prev.get(boardCollapseKey(columnId, folder.id)) ?? !!folder.collapsed)
      next.set(boardCollapseKey(columnId, folder.id), value)
      // Delta write: another tab's overrides must survive this tab's toggle.
      persistBoardOverride(columnId, folder.id, value)
      return next
    })
  }, [])
  /** Drop the overrides that hold `folderId` shut (in every column, or only in
   *  `columnId`), in state and in storage together. */
  const clearBoardCollapse = useCallback((folderId: string, columnId?: string) => {
    setBoardCollapse(prev => clearFolderOverrides(prev, folderId, columnId))
    persistClearFolderOverrides(folderId, columnId)
  }, [])
  return { clearBoardCollapse, boardFolderCollapsed, toggleColumnCollapse }
}

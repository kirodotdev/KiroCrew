/**
 * The Bots panel's starred rows: listed first, in the left dock's order, and
 * moved among themselves.
 *
 * The star is the pin. `starredFirst` draws every starred Bot ahead of the
 * other rows in `star_order` (`dockMateOrder`, the dock's own rule) under
 * either sort, and the other rows keep the panel's held order below them. A
 * starred row is dragged on a wide screen (`StarredReorder`,
 * `useStarredRowDrag`) or moved with Alt+ArrowUp / Alt+ArrowDown
 * (`useRosterStarOrder().step`), among the starred rows of its own group. A
 * move writes through the dock's write path (`useStarOrderReorder`), so the
 * dock and the panel show one order.
 */
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { DndContext, DragOverlay, closestCenter, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { useSortable } from '@dnd-kit/sortable'
import { type MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import { OverlayPortal } from '../../components/overlayRoot'
import { useDndSensors } from '../../hooks/useDndSensors'
import { useStarOrderReorder } from '../../hooks/useStarOrderReorder'
import { dockMateOrder } from '../../lib/dockMates'

/** The display order: every starred row of `roster` first, in the dock's
 *  order, then the other rows in `ordered`'s order. `roster` is in the
 *  server's order, which is what the dock breaks a `star_order` tie by. */
export function starredFirst(
  ordered: readonly MemberRosterRow[],
  roster: readonly MemberRosterRow[],
): MemberRosterRow[] {
  return [...dockMateOrder(roster), ...ordered.filter((m) => !m.starred)]
}

/** The panel's moves, made over the whole starred list in the dock's order. */
export function useRosterStarOrder(roster: readonly MemberRosterRow[]) {
  const { saving, move, error, reset } = useStarOrderReorder()
  const starred = useMemo(() => dockMateOrder(roster), [roster])
  /** Put `name` in `over`'s place, as a drop of one on the other does. */
  const moveTo = useCallback(
    (name: string, over: string) =>
      move(starred, starred.findIndex((m) => m.name === name), starred.findIndex((m) => m.name === over)),
    [move, starred],
  )
  // A row that moves down is re-inserted into the list, which drops its focus.
  // Until the move has saved, a commit that finds focus on nothing puts it
  // back on the moved row, so the next Alt+Arrow moves the same row.
  const refocus = useRef<string | null>(null)
  useLayoutEffect(() => {
    const name = refocus.current
    if (!name) return
    const focused = document.activeElement
    if (!focused || focused === document.body) {
      Array.from(document.querySelectorAll<HTMLElement>('[data-member-row]'))
        .find((el) => el.dataset.memberRow === name)
        ?.focus()
    }
    if (!saving()) refocus.current = null
  })
  /** Alt+Arrow: move `name` one place among `peers`, the starred rows its group draws. */
  const step = useCallback(
    (name: string, delta: -1 | 1, peers: readonly string[]) => {
      const at = peers.indexOf(name)
      const over = at < 0 ? undefined : peers[at + delta]
      if (over !== undefined && moveTo(name, over)) refocus.current = name
    },
    [moveTo],
  )
  return { starred, saving, moveTo, step, error, reset }
}

export type RosterStarOrder = ReturnType<typeof useRosterStarOrder>

/** The drag surface over the roster's groups. Each group wraps its rows in a
 *  `SortableContext` of its starred names; a drop puts the dragged row in the
 *  target row's place in the whole starred list. */
export function StarredReorder({ starOrder, children }: { starOrder: RosterStarOrder; children: ReactNode }) {
  const { starred, saving, moveTo } = starOrder
  const sensors = useDndSensors({ distance: 8 })
  const [dragged, setDragged] = useState<MemberRosterRow | null>(null)
  const onDragStart = useCallback(
    (e: DragStartEvent) => {
      if (saving()) return
      setDragged(starred.find((m) => m.name === e.active.id) ?? null)
    },
    [saving, starred],
  )
  const onDragEnd = useCallback(
    (e: DragEndEvent) => {
      setDragged(null)
      const { active, over } = e
      if (!over || active.id === over.id) return
      // A row of another group: each group draws its own rows, so the move
      // could only put the dragged row somewhere in its own group that nobody
      // pointed at.
      if (active.data.current?.sortable?.containerId !== over.data.current?.sortable?.containerId) return
      moveTo(String(active.id), String(over.id))
    },
    [moveTo],
  )
  const onDragCancel = useCallback(() => setDragged(null), [])
  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={onDragCancel}>
      {children}
      {/* Portaled like the Sessions sidebar's ghost: a fixed element is still
          clipped by a clipped or filtered ancestor. */}
      <OverlayPortal>
        <DragOverlay dropAnimation={null}>
          {dragged ? (
            <div data-testid="member-reorder-ghost" className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text shadow-lg max-w-[240px] truncate pointer-events-none">
              {crewDisplayName(dragged)}
            </div>
          ) : null}
        </DragOverlay>
      </OverlayPortal>
    </DndContext>
  )
}

/** One roster row's part in a drag, off for an unstarred row and on a phone,
 *  as a drag source and as a drop target both (a bare `disabled: true` would
 *  keep the row a drop target, so a drop past the starred rows would land on
 *  an unstarred one and change nothing). Rows never shift while one is
 *  dragged: the drop moves each row once, with its own layout motion
 *  (`MemberRow`), and `insertion` marks where the dragged row lands: after this
 *  row when it comes from above, before it when it comes from below, which is
 *  where `starOrderWrites` puts it. */
export function useStarredRowDrag(name: string, enabled: boolean) {
  const { setNodeRef, listeners, isDragging, isOver, activeIndex, index } = useSortable({
    id: name,
    disabled: { draggable: !enabled, droppable: !enabled },
  })
  const insertion: 'before' | 'after' | null =
    isOver && !isDragging && activeIndex >= 0 && index >= 0 ? (activeIndex < index ? 'after' : 'before') : null
  return { setNodeRef, listeners, isDragging, insertion }
}

/**
 * Dropping a session or a Mate onto the rail pins it there.
 *
 * Two drag systems feed this. Mate rows drag natively (HTML5), so the rail's
 * scroller takes their drop itself (`useRailNativeDrop`). Session rows drag
 * inside the chat sidebar's own dnd-kit context, which no rail listener can
 * see; for those the sidebar portals `RailPinDropZone` over the rail while a
 * session drag is live, the same way it portals the chat-pane zone, and pins on
 * drop. Either way the drop POINT picks the slot (`railDropTarget`).
 */
import { useCallback, useState, type DragEvent, type MouseEvent as ReactMouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { useDroppable } from '@dnd-kit/core'

import { insertPin, RAIL_PIN_MIME, updateRail } from '../../lib/railLayout'
import { i18nT } from '../../i18n/t'

/** Droppable `type` of the rail zone inside the sidebar's DndContext. */
export const RAIL_PIN_DROP_TYPE = 'rail-pin'
/** Id of the element the zone portals into: the rail's group scroller. */
export const RAIL_PIN_DROP_HOST_ID = 'rail-pin-drop-host'

/**
 * Which group a point is in and which visible row it lands before, read from
 * the rail's DOM (`data-rail-group` / `data-rail-item`). A point above the first
 * group joins the first, below the last joins the last; within a group it
 * lands before the first row whose middle is below it, else at the end.
 */
export function railDropTarget(y: number, root: ParentNode = document): { groupId: string; beforeId: string | null } | null {
  const groups = [...root.querySelectorAll<HTMLElement>('[data-rail-group]')]
  if (groups.length === 0) return null
  const hit = groups.find(g => { const r = g.getBoundingClientRect(); return y >= r.top && y <= r.bottom })
    ?? (y < groups[0].getBoundingClientRect().top ? groups[0] : groups[groups.length - 1])
  const rows = [...hit.querySelectorAll<HTMLElement>('[data-rail-item]')]
  const before = rows.find(r => { const b = r.getBoundingClientRect(); return b.top + b.height / 2 > y })
  return { groupId: hit.dataset.railGroup ?? '', beforeId: before?.dataset.railItem ?? null }
}

/** Pin `id` at the rail slot under `y`. False when the rail is not on screen. */
export function pinAtPoint(id: string, y: number): boolean {
  const target = railDropTarget(y)
  if (!target) return false
  return updateRail(l => insertPin(l, id, target.groupId, target.beforeId))
}

/** The final pointer y of a dnd-kit drag: where it started plus how far it moved. */
export function dndDropY(activatorEvent: Event | null, deltaY: number): number | null {
  const e = activatorEvent as { clientY?: number; touches?: { clientY: number }[] } | null
  const start = typeof e?.clientY === 'number' ? e.clientY : e?.touches?.[0]?.clientY
  return typeof start === 'number' ? start + deltaY : null
}

/** The zone the sidebar portals over the rail during a session drag. */
function RailPinDropZoneInner() {
  const { setNodeRef, isOver } = useDroppable({ id: 'rail-pin', data: { type: RAIL_PIN_DROP_TYPE } })
  return (
    <div
      ref={setNodeRef}
      // dnd-kit hit-tests measured rects, so the zone needs no pointer events and
      // the rail under it stays usable.
      className={`absolute inset-0 pointer-events-none rounded-lg border-2 border-dashed transition-colors z-10 ${isOver ? 'border-accent bg-accent/10' : 'border-border'}`}
      data-testid="rail-pin-drop-zone"
      aria-hidden="true"
    >
      {isOver && <span className="sr-only">{i18nT('nav.rail_drop_to_pin')}</span>}
    </div>
  )
}

export function RailPinDropZone() {
  const host = typeof document !== 'undefined' ? document.getElementById(RAIL_PIN_DROP_HOST_ID) : null
  return host ? createPortal(<RailPinDropZoneInner />, host) : null
}

/** Native-drop handlers for the rail scroller, plus whether a pin drag is over it. */
export function useRailNativeDrop() {
  const [over, setOver] = useState(false)
  const accepts = (e: DragEvent) => e.dataTransfer.types.includes(RAIL_PIN_MIME)
  const onDragOver = useCallback((e: DragEvent) => {
    if (!accepts(e)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    setOver(true)
  }, [])
  const onDragLeave = useCallback((e: DragEvent) => {
    // Leaving into a child is not leaving the rail.
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return
    setOver(false)
  }, [])
  const onDrop = useCallback((e: DragEvent) => {
    setOver(false)
    if (!accepts(e)) return
    e.preventDefault()
    const id = e.dataTransfer.getData(RAIL_PIN_MIME)
    if (id) pinAtPoint(id, e.clientY)
  }, [])
  return { over, handlers: { onDragOver, onDragLeave, onDrop } }
}

/**
 * A right-click on rail chrome outside the groups (empty space, the Settings
 * row, padding) re-fires on the groups' drop host, which is inside the rail
 * menu's trigger, so it opens the rail menu at the pointer in its empty-space
 * form (New group, Customize rail...). Clicks inside the groups, and in a text
 * field, are left alone.
 */
export function forwardRailContextMenu(e: ReactMouseEvent) {
  const host = document.getElementById(RAIL_PIN_DROP_HOST_ID)
  const target = e.target as Element | null
  if (!host || (target && (host.contains(target) || target.closest('input, textarea, [contenteditable="true"]')))) return
  e.preventDefault()
  host.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: e.clientX, clientY: e.clientY }))
}

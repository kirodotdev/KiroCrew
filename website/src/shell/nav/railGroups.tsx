import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { DndContext, closestCenter, DragOverlay, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'

import { useDndSensors } from '../../hooks/useDndSensors'
import { haptic } from '../../lib/haptic'
import {
  applyVisibleOrder, commitRail, isEntityPin, readRailLayout, removePin, resolveRailLayout, setRailLabel, updateRail,
  useRailLayout, writeRailLayout, type RailGroup,
} from '../../lib/railLayout'
import { SortableAppNavRow } from './appRail'
import { RailContextMenu, groupTitle, type RailItemMeta } from './railCustomize'
import { RailCustomizeModal, type RailPinChange } from './railCustomizeModal'
import { OPEN_RAIL_CUSTOMIZE_EVENT } from './railSettingsFlyout'
import { RAIL_PIN_DROP_HOST_ID, useRailNativeDrop } from './railPinDrop'
import { RailRenameInput } from './railRename'

/** Sortable id of a group's header. Prefixed so it can never equal a nav id. */
const HEADER_PREFIX = 'rail-group:'
const headerId = (groupId: string) => HEADER_PREFIX + groupId
const isHeaderId = (id: string) => id.startsWith(HEADER_PREFIX)

/**
 * Rebuild groups from the flat sortable order: each header opens its group,
 * and a row that lands above the first header joins the first group. Headers
 * never move (they are not draggable), so the group order is unchanged.
 */
export function regroupFlat(flat: readonly string[], groups: readonly RailGroup[]): RailGroup[] {
  const out: RailGroup[] = groups.map(g => ({ ...g, items: [] }))
  let current = -1
  const orphans: string[] = []
  for (const id of flat) {
    if (isHeaderId(id)) { current = out.findIndex(g => headerId(g.id) === id); continue }
    if (current < 0) orphans.push(id)
    else out[current].items.push(id)
  }
  if (out.length) out[0].items.unshift(...orphans)
  return out
}

/** A group header: a divider when collapsed, a title when expanded. It is a
 *  droppable slot in the flat list (so a row can be dropped at a group's top)
 *  but never draggable itself. */
function GroupHeader({ group, first, collapsed }: { group: RailGroup; first: boolean; collapsed: boolean }) {
  const { setNodeRef } = useSortable({ id: headerId(group.id), disabled: { draggable: true, droppable: false } })
  const title = groupTitle(group)
  if (collapsed) {
    return (
      <div ref={setNodeRef} role="presentation" className={first ? 'h-0' : 'py-1.5 px-2'}>
        {!first && <div aria-hidden="true" className="h-px bg-border" />}
      </div>
    )
  }
  return (
    <div ref={setNodeRef} className={`nav-section flex items-center pl-3 pr-1 pb-1 ${first ? 'pt-1' : 'pt-3'}`}>
      {/* Clipped by nowrap/overflow in a longer locale, so `title` keeps the full string. */}
      <span title={title} className="text-[13px] font-medium text-muted whitespace-nowrap overflow-hidden">{title}</span>
    </div>
  )
}

/**
 * The layout-driven part of the rail: every group in the user's rail layout,
 * with its rows drag-reorderable within a group and across groups. `available`
 * is every id the rail can currently draw; `renderItem` draws one of them.
 */
export function RailGroups({ available, renderItem, collapsed, defaultGroup, meta, unpinnedApps = [], isApp = () => false, setAppPinned }: {
  available: readonly string[]
  /** Draw one row. `label` is the user's rail-only name for it, when set. */
  renderItem: (id: string, label?: string) => ReactNode
  collapsed: boolean
  /** The group an id the layout never listed joins (e.g. a newly installed app). */
  defaultGroup?: (id: string) => string
  /** Label + icon of any id, including ones not on the rail (for the modal). */
  meta: (id: string) => RailItemMeta | null
  /** Apps unpinned through the Library, which owns app pinning. */
  unpinnedApps?: readonly string[]
  /** True for an installed-app id: its pin state lives in the Library's set, not the layout. */
  isApp?: (id: string) => boolean
  setAppPinned?: (id: string, pinned: boolean) => void
}) {
  const layout = useRailLayout()
  const groups = useMemo(() => resolveRailLayout(layout, available, defaultGroup), [layout, available, defaultGroup])
  const flat = useMemo(() => groups.flatMap(g => [headerId(g.id), ...g.items]), [groups])
  // Same 8px mouse threshold as the Apps list: a plain click must reach navigation.
  const sensors = useDndSensors({ distance: 8 })
  const [activeId, setActiveId] = useState<string | null>(null)
  const [customizing, setCustomizing] = useState(false)
  // The Settings flyout opens the same modal from outside this component.
  useEffect(() => {
    const open = () => setCustomizing(true)
    window.addEventListener(OPEN_RAIL_CUSTOMIZE_EVENT, open)
    return () => window.removeEventListener(OPEN_RAIL_CUSTOMIZE_EVENT, open)
  }, [])
  // Unpin: apps go through the Library's set (the rail drops them via
  // `sortedAppGroup`); everything else is hidden in the layout itself.
  const unpin = useCallback((id: string) => {
    if (isApp(id)) setAppPinned?.(id, false)
    // A pinned session or Mate has no shipped slot to remember: it just goes.
    else if (isEntityPin(id)) updateRail(l => removePin(l, id))
    else commitRail(groups.map(g => ({ ...g, items: g.items.filter(x => x !== id) })), { add: id })
  }, [groups, isApp, setAppPinned])
  // The customize modal's drop: a rearrangement plus at most one row moved
  // onto or off its "Not on the rail" shelf.
  const commitFromModal = useCallback((next: RailGroup[], change?: RailPinChange) => {
    if (change && 'unpin' in change) {
      const id = change.unpin
      if (isApp(id)) { setAppPinned?.(id, false); commitRail(next) }
      else if (isEntityPin(id)) updateRail(l => removePin(applyVisibleOrder(l, next), id))
      else commitRail(next, { add: id })
      return
    }
    if (change && 'pin' in change) {
      if (isApp(change.pin)) setAppPinned?.(change.pin, true)
      commitRail(next, { remove: change.pin })
      return
    }
    commitRail(next)
  }, [isApp, setAppPinned])
  const hidden = useMemo(() => {
    const avail = new Set(available)
    return [...layout.hidden.filter(id => avail.has(id)), ...unpinnedApps]
  }, [layout.hidden, available, unpinnedApps])

  const [renaming, setRenaming] = useState<string | null>(null)
  const labelOf = useCallback((id: string) => layout.labels?.[id], [layout.labels])
  const saveName = useCallback((id: string, name: string | null) => {
    setRenaming(null)
    updateRail(l => setRailLabel(l, id, name))
  }, [])
  const nativeDrop = useRailNativeDrop()
  // Stable: the modal memoizes on it, and its rows show rail-only names.
  const modalMeta = useCallback((id: string) => { const m = meta(id); const l = labelOf(id); return m && l ? { ...m, label: l } : m }, [meta, labelOf])

  const onDragStart = useCallback((e: DragStartEvent) => { haptic('medium'); setActiveId(String(e.active.id)) }, [])
  const onDragCancel = useCallback(() => setActiveId(null), [])
  const onDragEnd = useCallback((e: DragEndEvent) => {
    setActiveId(null)
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = flat.indexOf(String(active.id))
    let to = flat.indexOf(String(over.id))
    if (from < 0 || to < 0) return
    // Dropping onto the FIRST header means "top of the first group", which in
    // the flat list is just below that header.
    if (to === 0) to = 1
    haptic('light')
    const next = regroupFlat(arrayMove(flat, from, to), groups)
    // Merge into a fresh read: another tab may have written since this render.
    writeRailLayout(applyVisibleOrder(readRailLayout(), next))
  }, [flat, groups])

  return (<>
    <RailContextMenu
      groups={groups}
      onUnpin={unpin}
      onCustomize={() => setCustomizing(true)}
      onRename={setRenaming}
      onResetName={id => saveName(id, null)}
      isRenamed={id => !!labelOf(id)}
    >
    {/* Drop host: sessions dragged from the chat sidebar land on the zone the
        sidebar portals in here; Mates drag natively and drop on this element. */}
    <div id={RAIL_PIN_DROP_HOST_ID} className="relative pb-6" {...nativeDrop.handlers}>
    {nativeDrop.over && <div aria-hidden="true" className="absolute inset-0 pointer-events-none rounded-lg border-2 border-dashed border-accent bg-accent/10 z-10" />}
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={onDragCancel}>
      <SortableContext items={flat} strategy={verticalListSortingStrategy}>
        {groups.map((g, gi) => (
          <div key={g.id} role="group" aria-label={groupTitle(g)} className="flex flex-col gap-0.5" data-rail-group={g.id}>
            <GroupHeader group={g} first={gi === 0} collapsed={collapsed} />
            {g.items.map(id => (
              <SortableAppNavRow key={id} id={id}>
                <div data-rail-item={id} className="relative">
                  {renderItem(id, labelOf(id))}
                  {renaming === id && (
                    <RailRenameInput
                      initial={labelOf(id) ?? meta(id)?.label ?? ''}
                      collapsed={collapsed}
                      onSave={name => saveName(id, name)}
                      onCancel={() => setRenaming(null)}
                    />
                  )}
                </div>
              </SortableAppNavRow>
            ))}
          </div>
        ))}
      </SortableContext>
      <DragOverlay>{activeId ? renderItem(activeId, labelOf(activeId)) : null}</DragOverlay>
    </DndContext>
    </div>
    </RailContextMenu>
    <RailCustomizeModal
      open={customizing}
      onClose={() => setCustomizing(false)}
      groups={groups}
      hidden={hidden}
      meta={modalMeta}
      onCommit={commitFromModal}
    />
  </>)
}

/** Sortable id of a group header, for tests that build a flat order. */
export { headerId as railGroupHeaderId }

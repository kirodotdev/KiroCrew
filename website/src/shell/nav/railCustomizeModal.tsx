import { useEffect, useMemo, useRef, useState } from 'react'
import { DndContext, DragOverlay, closestCenter, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { GripVertical, Plus, X } from 'lucide-react'

import Modal from '../../components/Modal'
import { Btn } from '../../components/ui'
import { useDndSensors } from '../../hooks/useDndSensors'
import { i18nT } from '../../i18n/t'
import { addGroup, commitRail, moveGroup, removeGroup, renameGroup, resetRailLayout, type RailGroup } from '../../lib/railLayout'
import { groupTitle, type RailItemMeta } from './railCustomize'

/** The shelf: rows not on the rail. A pseudo-group, always last, never a real group. */
const SHELF = '__shelf'
const HEAD = 'head:'
const headId = (groupId: string) => HEAD + groupId
const isHead = (id: string) => id.startsWith(HEAD)

/** A pin-state change one drag makes: a row dropped onto or lifted off the shelf. */
export type RailPinChange = { pin: string } | { unpin: string }

/**
 * Rebuild groups + shelf from the flat sortable order (headers open sections).
 * A row above the first header joins the first group.
 */
export function regroupWithShelf(flat: readonly string[], groups: readonly RailGroup[]): { groups: RailGroup[]; shelf: string[] } {
  const out = groups.map(g => ({ ...g, items: [] as string[] }))
  const shelf: string[] = []
  let current: string[] | null = null
  const orphans: string[] = []
  for (const id of flat) {
    if (isHead(id)) {
      const gid = id.slice(HEAD.length)
      current = gid === SHELF ? shelf : out.find(g => g.id === gid)?.items ?? null
      continue
    }
    ;(current ?? orphans).push(id)
  }
  if (out.length) out[0].items.unshift(...orphans)
  return { groups: out, shelf }
}

/** A group title you just type into; saves on blur / Enter, Escape reverts. */
function GroupTitle({ group, groups, autoFocus }: { group: RailGroup; groups: readonly RailGroup[]; autoFocus: boolean }) {
  const [value, setValue] = useState(group.title ?? '')
  useEffect(() => setValue(group.title ?? ''), [group.title])
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { if (autoFocus) ref.current?.focus() }, [autoFocus])
  // Escape blurs too; this keeps that blur from saving the text it reverted.
  const reverting = useRef(false)
  const save = () => {
    if (reverting.current) { reverting.current = false; return }
    if (value.trim() !== (group.title ?? '')) commitRail(renameGroup(groups, group.id, value))
  }
  return (
    <input
      ref={ref}
      value={value}
      placeholder={groupTitle({ ...group, title: undefined })}
      aria-label={i18nT('nav.rail_group_name')}
      onChange={e => setValue(e.target.value)}
      onBlur={save}
      // Typing must not reach the drag sensors on the header.
      onPointerDown={e => e.stopPropagation()}
      onKeyDown={e => {
        e.stopPropagation()
        if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur() }
        else if (e.key === 'Escape') { e.preventDefault(); reverting.current = true; setValue(group.title ?? ''); e.currentTarget.blur() }
      }}
      maxLength={60}
      className="flex-1 min-w-0 bg-transparent border-none outline-none rounded px-1 -mx-1 text-[13px] font-medium text-muted placeholder:text-muted focus:text-text focus:bg-bg-hover"
    />
  )
}

function SectionHeader({ id, group, groups, newId, canRemove }: {
  id: string
  group: RailGroup | null
  groups: readonly RailGroup[]
  newId: string | null
  canRemove: boolean
}) {
  // Real group headers drag (to reorder groups); the shelf's never does.
  const { setNodeRef, attributes, listeners, transform, transition, isDragging } = useSortable({ id, disabled: { draggable: !group, droppable: false } })
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={`group/head flex items-center gap-1 px-2 py-1.5 border-b border-border ${group ? 'bg-bg-hover' : 'border-dashed'} ${isDragging ? 'opacity-40' : ''}`}
    >
      {group ? (
        <>
          <span {...attributes} {...listeners} aria-label={i18nT('nav.rail_drag_group')} className="text-muted opacity-0 group-hover/head:opacity-100 cursor-grab touch-none" ><GripVertical size={14} /></span>
          <GroupTitle group={group} groups={groups} autoFocus={newId === group.id} />
          {canRemove && (
            <button
              type="button"
              onClick={() => commitRail(removeGroup(groups, group.id))}
              aria-label={i18nT('nav.rail_remove_group')}
              title={i18nT('nav.rail_remove_group')}
              className="w-6 h-6 flex items-center justify-center rounded bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover opacity-0 group-hover/head:opacity-100 focus-visible:opacity-100"
            >
              <X size={13} />
            </button>
          )}
        </>
      ) : (
        <span className="px-1 text-[13px] font-medium text-muted">{i18nT('nav.rail_not_on_rail')}</span>
      )}
    </div>
  )
}

function RowView({ m, dim }: { m: RailItemMeta; dim?: boolean }) {
  return (
    <div className={`flex items-center gap-2.5 px-3 py-2 text-sm ${dim ? 'text-muted' : 'text-text'}`}>
      <span className="w-4 h-4 flex items-center justify-center shrink-0 text-muted">{m.icon}</span>
      <span className="flex-1 min-w-0 truncate">{m.label}</span>
    </div>
  )
}

function Row({ id, m, onShelf, onUnpin }: { id: string; m: RailItemMeta; onShelf: boolean; onUnpin: () => void }) {
  const { setNodeRef, attributes, listeners, transform, transition, isDragging } = useSortable({ id })
  return (
    <div
      ref={setNodeRef}
      {...attributes}
      {...listeners}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={`group/row relative cursor-grab touch-none hover:bg-bg-hover ${isDragging ? 'opacity-40' : ''}`}
      data-rail-customize-item={id}
    >
      <RowView m={m} dim={onShelf} />
      {!onShelf && (
        <button
          type="button"
          onClick={onUnpin}
          // A press on the × is a click, never the start of a row drag.
          onPointerDown={e => e.stopPropagation()}
          onKeyDown={e => e.stopPropagation()}
          aria-label={`${i18nT('nav.rail_unpin')}: ${m.label}`}
          title={i18nT('nav.rail_unpin')}
          className="absolute right-1 top-1/2 -translate-y-1/2 w-6 h-6 flex items-center justify-center rounded bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100"
        >
          <X size={13} />
        </button>
      )}
    </div>
  )
}

/**
 * The rail config modal, shaped like the rail itself: type a group's title in
 * place, drag rows within and across groups, drag a group by its handle, and
 * drag a row onto "Not on the rail" to unpin it (or off it to pin it back).
 * Every drop writes straight through, so the rail behind the modal follows.
 */
export function RailCustomizeModal({ open, onClose, groups, hidden, meta, onCommit }: {
  open: boolean
  onClose: () => void
  groups: readonly RailGroup[]
  /** Ids not currently on the rail that can be pinned back. */
  hidden: readonly string[]
  meta: (id: string) => RailItemMeta | null
  /** Persist a rearrangement, with the one pin-state change it made, if any. */
  onCommit: (next: RailGroup[], change?: RailPinChange) => void
}) {
  const sensors = useDndSensors({ distance: 4 })
  const [activeId, setActiveId] = useState<string | null>(null)
  const [newId, setNewId] = useState<string | null>(null)
  const shown = useMemo(() => groups.map(g => ({ ...g, items: g.items.filter(id => meta(id)) })), [groups, meta])
  const shelf = useMemo(() => hidden.filter(id => meta(id)), [hidden, meta])
  const flat = useMemo(
    () => [...shown.flatMap(g => [headId(g.id), ...g.items]), headId(SHELF), ...shelf],
    [shown, shelf],
  )

  const onDragStart = (e: DragStartEvent) => setActiveId(String(e.active.id))
  const onDragEnd = (e: DragEndEvent) => {
    setActiveId(null)
    const a = String(e.active.id)
    const o = e.over ? String(e.over.id) : null
    if (!o || a === o) return
    if (isHead(a)) {
      // A group header: move the group to the slot of the group it was dropped in.
      const groupOf = (id: string) => (isHead(id) ? id.slice(HEAD.length) : shown.find(g => g.items.includes(id))?.id ?? SHELF)
      const from = shown.findIndex(g => g.id === a.slice(HEAD.length))
      const to = shown.findIndex(g => g.id === groupOf(o))
      if (from < 0 || to < 0 || from === to) return
      let next = [...groups]
      const step = to > from ? 1 : -1
      for (let i = from; i !== to; i += step) next = moveGroup(next, next[i].id, step as 1 | -1)
      commitRail(next)
      return
    }
    const fromIdx = flat.indexOf(a)
    let toIdx = flat.indexOf(o)
    if (fromIdx < 0 || toIdx < 0) return
    if (toIdx === 0) toIdx = 1 // onto the first header = top of the first group
    const { groups: next, shelf: nextShelf } = regroupWithShelf(arrayMove(flat, fromIdx, toIdx), shown)
    const wasShelved = shelf.includes(a)
    const isShelved = nextShelf.includes(a)
    // Keep each group's unavailable ids: `shown` filtered them only for drawing.
    const merged = next.map(g => {
      const original = groups.find(x => x.id === g.id)
      const kept = (original?.items ?? []).filter(id => !meta(id))
      return { ...g, items: [...g.items, ...kept] }
    })
    onCommit(merged, wasShelved === isShelved ? undefined : isShelved ? { unpin: a } : { pin: a })
  }

  // The row's × : the same commit a drop onto the shelf makes.
  const unpinRow = (id: string) => {
    const next = groups.map(g => ({ ...g, items: g.items.filter(x => x !== id) }))
    onCommit(next, { unpin: id })
  }

  const activeMeta = activeId && !isHead(activeId) ? meta(activeId) : null
  const activeGroup = activeId && isHead(activeId) ? shown.find(g => headId(g.id) === activeId) : null

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={i18nT('nav.rail_customize_title')}
      maxWidth={420}
      footer={(
        <div className="flex w-full items-center justify-between">
          <Btn onClick={resetRailLayout}>{i18nT('nav.rail_reset')}</Btn>
          <Btn primary onClick={onClose}>{i18nT('nav.rail_done')}</Btn>
        </div>
      )}
    >
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={() => setActiveId(null)}>
        <SortableContext items={flat} strategy={verticalListSortingStrategy}>
          {/* One sortable order, drawn as a card per section: dnd-kit sorts by
              the flat id list, so the wrappers are purely visual. */}
          <div className="flex flex-col gap-3">
            {[...shown.map(g => ({ gid: g.id, g, items: g.items })), { gid: SHELF, g: null, items: shelf }].map(({ gid, g, items }) => (
              <div key={gid} className="flex flex-col gap-3">
                {gid === SHELF && (
                  <button
                    type="button"
                    onClick={() => { const r = addGroup(groups, ''); commitRail(r.groups); setNewId(r.groupId) }}
                    className="self-start flex items-center gap-1.5 px-1 py-1 text-[13px] text-muted bg-transparent border-none cursor-pointer hover:text-text"
                  >
                    <Plus size={14} />{i18nT('nav.rail_new_group')}
                  </button>
                )}
                <section className={`rounded-lg border border-border overflow-hidden ${g ? 'bg-bg-elevated' : 'border-dashed bg-transparent'}`}>
                  <SectionHeader id={headId(gid)} group={g} groups={groups} newId={newId} canRemove={groups.length > 1} />
                  <div className="flex flex-col divide-y divide-border min-h-[8px]">
                    {items.map(id => {
                      const m = meta(id)
                      return m ? <Row key={id} id={id} m={m} onShelf={!g} onUnpin={() => unpinRow(id)} /> : null
                    })}
                  </div>
                </section>
              </div>
            ))}
          </div>
        </SortableContext>
        <DragOverlay>
          {activeMeta ? <div className="shadow-lg rounded-md border border-border bg-bg-elevated"><RowView m={activeMeta} /></div> : null}
          {activeGroup ? <div className="shadow-lg rounded-md bg-bg-elevated px-2 py-1 text-[13px] font-medium text-text">{groupTitle(activeGroup)}</div> : null}
        </DragOverlay>
      </DndContext>
    </Modal>
  )
}

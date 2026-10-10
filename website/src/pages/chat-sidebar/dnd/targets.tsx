/** Drop targets, sortable wrappers and drag previews rendered inside the sidebar's
 *  DndContext. */
import { useDroppable } from '@dnd-kit/core'
import { useRef, useState, useCallback, useLayoutEffect } from 'react'
import { EyeOff, Repeat, MessagesSquare } from 'lucide-react'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import type { SessionRefBlockReason } from '../../../utils/sessionRefs'
import { i18nT } from '../../../i18n/t'
import { CHAT_PANE_DROP_TYPE } from './collision'
import type { ChatFolder } from '../../../types'
import FolderGlyph from '../../../components/FolderGlyph'
import type { Slot } from '../types'

/**
 * Full-pane drop affordance for "drag a session into the open chat".
 *
 * The HIT AREA is the whole pane — it is ~10x the composer's area and a shorter
 * travel from the session list, and a release over the transcript that silently
 * did nothing would read as a broken feature rather than a near-miss. But the
 * CUE is anchored on the composer, because that is where the chip actually
 * lands; a label floating mid-transcript taught the wrong mental model (that the
 * session drops into the conversation itself).
 *
 * Rendered only while a session drag is live, so it never sits invisibly over
 * the transcript at rest. `pointer-events-none` is safe *and* required: dnd-kit
 * resolves collisions from measured rects, not DOM hit-testing, so the zone
 * still receives the drop while the chat underneath stays fully interactive.
 *
 * When the dragged session may not be referenced the zone renders a refusal state
 * instead of an invitation, and the two refusals are NOT interchangeable:
 * incognito/temporary is a guard stated plainly, while dropping a session onto
 * its own pane is a harmless mis-aim answered with a recursive joke rather than a
 * warning. Explaining the block beats silently ignoring the drop — and the drop
 * handler refuses independently, so this is the visible half of a guard that does
 * not depend on the UI being reached.
 */
export function ChatPaneDropZone({ refusal }: { refusal: SessionRefBlockReason | null }) {
  const refused = refusal !== null
  const { setNodeRef, isOver } = useDroppable({ id: 'chat-pane-ref', data: { type: CHAT_PANE_DROP_TYPE } })
  const zoneRef = useRef<HTMLDivElement | null>(null)
  /** The composer's box in zone-local coordinates (plus the zone's own height, so
   *  the pill's offset is plain arithmetic rather than a `calc()` string — a CSS
   *  template literal here is exactly the shape the i18n gate flags). */
  const [target, setTarget] = useState<
    { left: number; top: number; width: number; height: number; zoneH: number } | null
  >(null)
  const attach = useCallback((el: HTMLDivElement | null) => {
    zoneRef.current = el
    setNodeRef(el)
  }, [setNodeRef])
  // Measured ONCE at mount rather than hardcoded as an offset from the bottom:
  // the composer band's height moves with the attachment strip, the session-ref
  // strip, and the approval bar, so any constant would drift. The zone exists
  // only for the duration of one drag and the pointer is held down throughout,
  // so a single read cannot go stale.
  useLayoutEffect(() => {
    const el = zoneRef.current
    const composer = el?.parentElement?.querySelector('[data-testid="input-wrapper"]')
    if (!el || !composer) return
    const z = el.getBoundingClientRect()
    const c = composer.getBoundingClientRect()
    setTarget({
      left: c.left - z.left,
      top: c.top - z.top,
      width: c.width,
      height: c.height,
      zoneH: z.height,
    })
  }, [])
  const active = isOver && !refused
  // Two refusals, told apart deliberately. 'private' is a GUARD — the user asked
  // for something the product will not do, so it keeps the warn tone. 'self' is
  // not a guard at all: dropping a session onto its own pane is a no-op the user
  // reached by aiming badly, and dressing a harmless gesture in warning colour
  // teaches them they broke something. It gets the resting neutral tone and a
  // joke that IS the explanation — the sentence recurses the way the drop would.
  const tone = refusal === 'private'
    ? 'border-warn bg-bg-elevated/90 text-warn'
    : refusal === 'self'
      ? 'border-border bg-bg-elevated/90 text-muted'
      : active
        ? 'border-accent bg-bg-elevated/90 text-accent ring-2 ring-accent'
        : 'border-border bg-bg-elevated/90 text-muted'
  const pill = (
    <div className={`inline-flex items-center gap-2 rounded-lg border border-dashed px-3 py-2 text-[12px] shadow-lg backdrop-blur-xs ${tone}`}>
      {refusal === 'private'
        ? <EyeOff size={14} className="shrink-0" />
        : refusal === 'self'
          ? <Repeat size={14} className="shrink-0" />
          : <MessagesSquare size={14} className="shrink-0" />}
      <span>
        {refusal === 'private'
          ? i18nT('pages.chatSidebar.private_session_cannot_be_referenced')
          : refusal === 'self'
            ? i18nT('pages.chatSidebar.session_dropped_into_itself')
            : i18nT('pages.chatSidebar.drop_to_reference_session')}
      </span>
    </div>
  )
  return (
    <div
      ref={attach}
      data-testid="chat-pane-drop-zone"
      data-refused={refusal ?? undefined}
      aria-hidden="true"
      className={`absolute inset-0 z-30 pointer-events-none transition-colors ${
        active ? 'bg-accent/[0.06]' : isOver && refusal === 'private' ? 'bg-warn/[0.06]' : 'bg-transparent'
      }`}
    >
      {target ? (
        <>
          {/* Outline the destination itself, matching the treatment the existing
              file drop puts on the composer, so both drags land the same way.
              Suppressed when refused: outlining a destination while the label
              says the drop is not allowed contradicts itself — a refusal has no
              destination. The pill still sits over the composer, because that is
              the context of what was refused. */}
          {!refused && (
            <div
              data-testid="chat-pane-drop-target"
              className={`absolute rounded-2xl border-2 border-dashed transition-colors ${
                active ? 'border-accent' : 'border-border-strong'
              }`}
              style={{ left: target.left, top: target.top, width: target.width, height: target.height }}
            />
          )}
          {/* Pill sits directly above the composer, pointing at where the chip
              will appear. 10px of air between the two. */}
          <div
            className="absolute flex justify-center"
            style={{ left: target.left, width: target.width, bottom: target.zoneH - target.top + 10 }}
          >
            {pill}
          </div>
        </>
      ) : (
        // Measurement unavailable (no composer on screen — e.g. an empty state).
        // Fall back to a centered pill rather than rendering no affordance at all.
        <div className="absolute inset-0 flex items-center justify-center">{pill}</div>
      )}
    </div>
  )
}

/** Dashed always-reachable drop target shown in the root lane while dragging
 *  a foldered item — the explicit escape hatch out of a folder. Shared by
 *  session drags and nested-folder drags so the affordance (and wording)
 *  stays identical for both. */
export function RootDropHint() {
  const { setNodeRef, isOver } = useDroppable({ id: 'root-unnest-hint', data: { type: 'folder-drop', folderId: null } })
  return (
    <div ref={setNodeRef} className={`m-1 min-h-[72px] flex items-center justify-center rounded-md border border-dashed transition-all ${isOver ? 'border-accent bg-accent/10 ring-2 ring-accent text-accent' : 'border-border text-muted'}`}>
      <span className="text-[12px]">{i18nT('pages.chatSidebar.drop_here_to_remove_from_folder')}</span>
    </div>
  )
}

/** The board column's escape hatch out of a folder: `RootDropHint`'s wording
 *  and look as a strip at the foot of a column, shown only while a FILED session
 *  card is in flight, and wired to the same unfile offer -- so the undo bar is
 *  the list view's too. Native HTML5 DnD rather than a droppable, because that is
 *  how a board card drags (the column's DndContext carries only folder
 *  reorders), which is also why the root targets above never exist in a board.
 *
 *  A target of its own and not a drop on the column body: on a status lane the
 *  body's release RETAGS the card, and one release must do one thing -- a filed
 *  card stays retaggable without leaving its folder. Every handler stops
 *  propagation so the column's drop never also sees the release; `dragover`
 *  accepts only a session card (text/plain) and leaves anything else to the
 *  column. The label is `pointer-events-none` so the pointer crossing onto it
 *  does not fire a `dragleave` on the strip and flicker the ring. */
export function BoardUnfileDropStrip({ columnId, onUnfile }: { columnId: string; onUnfile: (slotKey: string) => void }) {
  const [over, setOver] = useState(false)
  return (
    // Mouse-only drop target with no keyboard analogue, like the folder header's
    // native drop; the row's own menu is the keyboard path out of a folder.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      data-testid={`column-unfile-drop-${columnId}`}
      className={`shrink-0 m-1.5 min-h-[36px] px-2 flex items-center justify-center text-center rounded-md border border-dashed transition-all ${over ? 'border-accent bg-accent/10 ring-2 ring-accent text-accent' : 'border-border text-muted'}`}
      onDragOver={e => {
        if (!e.dataTransfer.types.includes('text/plain')) return
        e.preventDefault()
        e.stopPropagation()
        e.dataTransfer.dropEffect = 'move'
        setOver(true)
      }}
      onDragLeave={e => { e.stopPropagation(); setOver(false) }}
      onDrop={e => {
        // Same gate as `dragover`: a release that is not a session card (a
        // column reorder, which the column surface accepts) is the column's.
        if (!e.dataTransfer.types.includes('text/plain')) return
        e.preventDefault()
        e.stopPropagation()
        setOver(false)
        const k = e.dataTransfer.getData('text/plain')
        if (k) onUnfile(k)
      }}
    >
      <span className="text-[12px] pointer-events-none">{i18nT('pages.chatSidebar.drop_here_to_remove_from_folder')}</span>
    </div>
  )
}

export function SortableFolderBlock({ folder, subtree, siblings, reorderable, dragWithheld, renderFolderBlock }: { folder: ChatFolder; subtree?: readonly string[]; siblings?: readonly string[]; reorderable: boolean; dragWithheld: boolean; renderFolderBlock: (f: ChatFolder, depth: number, visited?: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean) => React.ReactNode[] }) {
  // Outside the custom folder order the row stays DRAGGABLE (the nest band on a
  // folder header still re-parents) but stops being a reorder TARGET: with its
  // droppable off, dnd-kit never resolves a sibling as `over`, so no slot opens
  // and nothing displaces -- the affordance is gone rather than refused after
  // the fact. See `folderReorderable`. Before the FIRST read of the mode
  // (`dragWithheld`) both sides are off: the reorder cannot be honoured, nothing
  // on screen could yet say why a lift died at the drop, and the mode arrives
  // with the read -- so for that moment the row is simply not grabbable (dnd-kit
  // hands it no listeners, and the header draws no grab cursor).
  const { listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id, data: { type: 'folder', subtree, siblings }, disabled: dragWithheld ? { draggable: true, droppable: true } : reorderable ? undefined : { droppable: true } })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1, position: 'relative' as const }
  // The whole folder header is the drag handle (pointer + touch): dragging the
  // row reorders the folder — no grip, consistent with session-card drag. Only
  // pointer listeners are forwarded (not attributes) so the header keeps
  // its inner collapse/action buttons valid. The MouseSensor activation
  // distance lets clicks through, and the TouchSensor's press-and-hold delay
  // lets touch swipes pan the list. setNodeRef stays on the block for sortable
  // positioning. While dragging, the body is force-collapsed so the source
  // shrinks to a single row — the drop-target gap (and the DragOverlay ghost)
  // stay compact.
  return (
    <div ref={setNodeRef} style={style} className="relative" data-folder-sortable={folder.id}>
      {renderFolderBlock(folder, 0, undefined, listeners as unknown as React.HTMLAttributes<HTMLElement>, isDragging)}
    </div>
  )
}

/**
 * Sortable wrapper for a NESTED subfolder row — the same wrapper
 * `SortableFolderBlock` is for a root row, one level down.
 *
 * A nested row was a bare draggable until #10428, which made its ONLY possible
 * outcome a re-parent: with no sortable id it registered no reorder target and
 * appeared in no sibling ring, so a person could be shown an order an agent had
 * set with `chat_folder_move`'s `before` / `after` and had no way to change it.
 * Registering it here closes that, and it closes it by reusing the root path
 * rather than adding a second one: the gesture, the collision band, the
 * anchor request and the endpoint are all the ones root rows already use.
 *
 * `siblings` is the ring this row may move within, which is what keeps the two
 * levels from bleeding into each other. Every folder row is now a `folder`
 * droppable, so without it a drag's closest-center fallback could resolve to a
 * row in a different container — a reorder gesture that moves nothing,
 * which reads as the drag having been ignored.
 *
 * `disabled` while renaming, matching the bare-draggable behaviour it replaces:
 * a drag started on a text input would steal the caret.
 */
export function SortableSubfolderBlock({ folder, depth, visited, subtree, siblings, disabled, reorderable, dragWithheld, renderFolderBlock }: {
  folder: ChatFolder
  depth: number
  visited: ReadonlySet<string>
  subtree?: readonly string[]
  siblings?: readonly string[]
  disabled?: boolean
  reorderable: boolean
  dragWithheld: boolean
  renderFolderBlock: (f: ChatFolder, depth: number, visited?: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean) => React.ReactNode[]
}) {
  // Two reasons, one per side: renaming turns the DRAGGABLE off (a drag started
  // on the text input would steal the caret -- dnd-kit's boolean `true` only ever
  // disabled that side), and a non-custom folder order turns the DROPPABLE off,
  // the same way SortableFolderBlock does for a root row. Spelled per side so the
  // two compose: a subfolder mid-rename is no reorder target outside Custom either.
  // Before the first read of the mode both sides are off (see the root wrapper).
  const { listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: folder.id,
    disabled: { draggable: !!disabled || dragWithheld, droppable: !reorderable || dragWithheld },
    data: { type: 'folder', nested: true, subtree, siblings },
  })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1 }
  return (
    <div ref={setNodeRef} style={style} data-subfolder-sortable={folder.id}>
      {/* A CLONE of the ancestor path, never the caller's own set. This render
       *  is deferred and re-invoked (StrictMode, `isDragging` flips), and
       *  `renderFolderBlock` MUTATES the set it is handed — so sharing it would
       *  make the second invocation hit the cycle guard, render the subfolder as
       *  `[]`, and the folder would vanish mid-drag. */}
      {renderFolderBlock(folder, depth, new Set(visited), listeners as unknown as React.HTMLAttributes<HTMLElement>, isDragging)}
    </div>
  )
}

/** Sortable wrapper for a board/column-view folder — the board sibling of
 *  SortableFolderBlock. Each column owns its own DndContext, so the bare folder
 *  id is a unique sortable id within that column even though every column
 *  renders the same root folders. Only pointer listeners are forwarded (the
 *  folder header becomes the drag handle); setNodeRef wraps the whole block for
 *  sortable positioning — identical to the list-view pattern. Reorders route
 *  through the same global reorderFolders() path, so order stays consistent
 *  across every column and the list view. */
export function SortableColumnFolder({ folder, columnId, colSlotKeys, subtree, reorderable, dragWithheld, renderColumnFolder }: {
  folder: ChatFolder
  columnId: string
  colSlotKeys: Set<string>
  subtree?: readonly string[]
  reorderable: boolean
  dragWithheld: boolean
  renderColumnFolder: (f: ChatFolder, columnId: string, colSlotKeys: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean) => React.ReactNode
}) {
  // `subtree` mirrors the list-view SortableFolderBlock: sidebarCollision reads it
  // to exclude the dragged folder's own descendants from the nest drop targets, so
  // a folder can never be dropped into itself or a child (moveFolderTo guards this
  // too, but excluding them up front keeps the highlight honest). `reorderable`
  // mirrors it too: outside the custom order the column's folders are no reorder
  // targets.
  const { listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id, data: { type: 'folder', subtree }, disabled: dragWithheld ? { draggable: true, droppable: true } : reorderable ? undefined : { droppable: true } })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1, position: 'relative' as const }
  // While dragging, the body is force-collapsed so the source shrinks to a
  // single row — the drop-target gap (and the DragOverlay ghost) stay compact,
  // matching the list-view drag feel.
  return (
    <div ref={setNodeRef} style={style} data-col-folder-sortable={folder.id}>
      {renderColumnFolder(folder, columnId, colSlotKeys, listeners as unknown as React.HTMLAttributes<HTMLElement>, isDragging)}
    </div>
  )
}

/** Compact drag-preview ghost for a folder, rendered inside a DragOverlay.
 *  Shared by the list-view overlay and each board-column overlay so the drag
 *  visual is identical in both layouts. */
export function FolderDragGhost({ folder }: { folder?: ChatFolder }) {
  return (
    <div data-testid="folder-drag-ghost" className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text shadow-lg max-w-[240px] truncate pointer-events-none flex items-center gap-2">
      <FolderGlyph color={folder?.color} icon={folder?.icon} size={14} />{folder?.name ?? i18nT('pages.chatSidebar.folder')}
    </div>
  )
}

/** Compact drag-preview ghost for a session row, rendered inside a DragOverlay.
 *  Shared by the folder-tree and flat-lane overlays. Falls back to the slot key
 *  when the session carries no distinct title. */
export function SessionDragGhost({ slot, fallbackLabel }: { slot?: Slot; fallbackLabel: string }) {
  const label = slot?.title && slot.title !== slot.key ? slot.title : (slot?.key ?? fallbackLabel)
  return (
    <div data-testid="session-drag-ghost" className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text shadow-lg max-w-[240px] truncate pointer-events-none">{label}</div>
  )
}

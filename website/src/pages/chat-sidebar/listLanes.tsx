/** The three list lanes: the conductor lane (sessions nested under the one that
 *  opened them), the flat lane (date segments, no folders) and the folder tree,
 *  with the drag overlay and the chat-pane drop target the flat and tree lanes
 *  share. */
import { Fragment } from 'react'
import { createPortal } from 'react-dom'
import { OverlayPortal } from '../../components/overlayRoot'
import { motion } from 'framer-motion'
import { DndContext, DragOverlay, MeasuringStrategy, type Modifier } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { sessionRefBlockReason } from '../../utils/sessionRefs'
import { ancestorsOf, closedCreatorCitation, descendantsOf, orphanCitation } from '../../lib/sessionLineage'
import { LIST_BODY_CLS } from '../../components/listShell'
import { DndActiveProbe, DndDroppable } from '../../components/dnd'
import { collectFolderSubtreeIds } from '../../utils/folderTree'
import { SessionRowWindowContext } from '../chat/sessionRowWindow'
import { slotActivityTs } from '../chat/sessionOrder'
import { i18nT } from '../../i18n/t'
import { sidebarCollision } from './dnd/collision'
import { ChatPaneDropZone, RootDropHint, SortableFolderBlock, FolderDragGhost, SessionDragGhost } from './dnd/targets'
import type { Slot } from './types'
import { isPeerRow, sessionRowIdentity } from './rowIdentity'
import { citedCreatorOf } from './conductor'
import { COLLAPSE_FLOOR_ATTR } from './collapseFloor'
import { PinnedSessionDivider, dateSegment } from './chrome'
import type { LaneRows } from './laneRows'
import type { FolderBlocks } from './folderBlocks'
import type { SidebarScope } from './scope'
import { KindSectionHeader, sectionHeaders, sectionRankOf, sectionRows } from './kindSections'

/** Move only the visual SESSION preview away from a pointer-owned destination
 * cue. Collision detection continues to use the real pointer coordinates; this
 * modifier belongs on DragOverlay, never DndContext. Keyboard drags keep the
 * preview aligned with their synthetic position. A pointer drag is moved only
 * while a `folder-drop` destination is under it — a folder block, a parent cue,
 * the root lane or the un-nest hint, each of which paints its cue in the lane
 * under the pointer; the chat pane anchors its cue to the composer, so there the
 * preview stays where dnd-kit put it. While a folder is targeted, prefer the
 * open space beside the sidebar so both the parent cue and the child row stay
 * readable; a narrow viewport with no side room moves the ghost below both rows
 * instead, stopping at the same gutter the side placements keep so it cannot
 * leave the viewport. */
const offsetSessionDragGhost: Modifier = ({
  transform, activatorEvent, activeNodeRect, overlayNodeRect, over, windowRect,
}) => {
  if (typeof KeyboardEvent !== 'undefined' && activatorEvent instanceof KeyboardEvent) return transform
  const overData = over?.data.current as { type?: string } | undefined
  if (overData?.type !== 'folder-drop' || !activeNodeRect || !overlayNodeRect || !windowRect) return transform
  const sideGap = 12
  const currentLeft = overlayNodeRect.left + transform.x
  const rightOffset = activeNodeRect.width + sideGap
  if (currentLeft + rightOffset + overlayNodeRect.width <= windowRect.right - sideGap) {
    return { ...transform, x: transform.x + rightOffset, y: transform.y + 8 }
  }
  const leftOffset = -(overlayNodeRect.width + sideGap)
  if (currentLeft + leftOffset >= windowRect.left + sideGap) {
    return { ...transform, x: transform.x + leftOffset, y: transform.y + 8 }
  }
  // Below both rows, but never past the bottom gutter. The overlay sits at its
  // initial rect plus the transform, so the room under it is measured from
  // that absolute bottom edge; a ghost already past the gutter is pulled back.
  const roomBelow = windowRect.bottom - sideGap - (overlayNodeRect.top + transform.y + overlayNodeRect.height)
  return { ...transform, x: transform.x + sideGap, y: transform.y + Math.min(96, roomBelow) }
}
const SESSION_DRAG_GHOST_MODIFIERS = [offsetSessionDragGhost]

/** The list lane renderers for one shell render. */
export function createListLanes(shell: SidebarScope & LaneRows & FolderBlocks) {
  const { activeDrag, activeSlot, allHiddenFolders, chatDropTarget, citedCreatorExists, conductorExpanded,
    conductorMatching, conductorRows, dndSensors, flatSlots, folderCreateError, folderCreateMountAbsent,
    folderDragWithheld, folderNameMatchIds, folderReorderable, folderSubtrees, folders,
    handleSidebarDragCancel, handleSidebarDragEnd, handleSidebarDragOver, handleSidebarDragStart,
    heldDisplacedRef, heldLane, hiddenByContainer, hoverPinRef, isActiveRow, kindSections, laneRowWindow, laneScrollMemory,
    laneSlots, lineage, listNarrowed, localGroupHeader, localSlots, onDropSessionRef,
    renderCrewGroups, renderFolderBlock, renderFolderCreateError, renderHiddenReveal,
    renderOlderSessionsHint, renderSessionRow, renderStaleSection, reportDndActive, rootFolderIds,
    rootFolders, rowAnimEnabled, searchRanked, setLaneScrollEl, shownCrewGroups, shownCrewRowCount,
    sidebarRows, slotFilter, slotFolders, sortKey, splitStale, staleExpanded, startsAutomaticSection,
    subagentApprovalCounts, ungroupedSlots, unreadSet, visibleRootFolders, pinnedBandCaption } = shell
  // True while actively dragging a session that currently lives in a folder.
  // Used to reveal the empty-state drop placeholder inside the "No folder"
  // group so there's always a reachable ungroup target.
  const draggingFolderedSession = activeDrag?.type === 'session' && !!slotFolders[activeDrag.id]
  // WHY the session being dragged may not be referenced into the open chat, or
  // null when it may be. Carries the reason rather than a boolean because the two
  // refusals read differently to the user (a privacy guard vs a self-drop no-op).
  // Drives the drop zone's refusal state; the drop handler re-decides with the
  // same function.
  const draggingRefRefusal = activeDrag?.type === 'session'
    ? sessionRefBlockReason({
      key: activeDrag.id,
      activeSlot,
      memoryMode: localSlots.find(x => x.key === activeDrag.id)?.memory_mode,
    })
    : null
  // True while dragging a folder that currently has a parent — the only case
  // where "drop on the root lane to move to top level" applies.
  const draggingNestedFolder = activeDrag?.type === 'folder' && !!folders.find(f => f.id === activeDrag.id)?.parent_id
  // Droppable rects are normally snapshotted once at drag-start, but these
  // lanes ANIMATE during drags (the dragged folder's body collapses over 150ms;
  // hovered collapsed folders auto-expand; the chat-pane zone mounts mid-drag),
  // so the snapshot goes stale and drop targets diverge from the cursor. While a
  // drag is live, poll re-measurement (dnd-kit's numeric `frequency`
  // self-reschedules a measure loop) so rects track the animating layout. Idle
  // sessions keep the plain strategy — no background measuring.
  const dndMeasuring = activeDrag
    ? { droppable: { strategy: MeasuringStrategy.Always, frequency: 100 } }
    : { droppable: { strategy: MeasuringStrategy.Always } }
  /** The follow-the-cursor preview for whatever is being dragged. */
  const dragGhost = activeDrag
    ? activeDrag.type === 'folder'
      ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} />
      : <SessionDragGhost slot={localSlots.find(x => x.key === activeDrag.id)} fallbackLabel={activeDrag.id} />
    : null
  /**
   * The drag preview is PORTALED to the body-level overlay root.
   *
   * dnd-kit positions the overlay `fixed`, which normally escapes ancestor
   * overflow — but the sidebar rides inside OverlayDrawer's morph `clip-path`,
   * and a clip-path clips every descendant including fixed ones. Rendered in
   * place, the ghost therefore vanished the instant the cursor crossed out of
   * the sidebar and into the chat pane, i.e. for the whole second half of the
   * one gesture that aims there. Portaling keeps it visible until release; it
   * stays inside the DndContext because React portals preserve context.
   */
  const dragOverlay = <OverlayPortal>{
    <DragOverlay dropAnimation={null} modifiers={activeDrag?.type === 'session' ? SESSION_DRAG_GHOST_MODIFIERS : undefined}>{dragGhost}</DragOverlay>
  }</OverlayPortal>

  const renderConductorLane = (): React.ReactNode => (
          // Conductor lane: every session nested under the session that OPENED it.
          //
          // The row is the EXISTING session card, unchanged — agent label, time,
          // title, preview, PR chips, loop status, tags, needs-you. The lane adds
          // three things and nothing else: indentation per depth, a chevron with a
          // child count on a card that has children, and that subtree's aggregated
          // badges while the card is collapsed. No compact row design: a nested
          // session is the same object as a top-level one and reads the same.
          //
          // Search flattens it, the way the flat lane does: a query must reach every
          // match, so a match three levels down inside a collapsed conductor cannot be
          // hidden behind a chevron the user would have to guess at.
          //
          // DnD is off, like the flat lane's row order: position here is a function of
          // who opened whom, so there is nothing a drop inside the lane could land on.
          <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
          <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="conductor-view-lane">
            {folderCreateError && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
            {localGroupHeader}
            {(() => {
              const tree = lineage
              if (!tree) return null
              const searching = slotFilter.trim() !== ''
              // Keyed by identity, exactly as `lineage` is: a raw-key map would let a
              // federated peer row overwrite the local row it collides with, so one
              // session would vanish and the other would render twice.
              const byKey = new Map(conductorRows.map(s => [sessionRowIdentity(s), s] as const))
              // The rows this lane may show: every match, plus each ancestor a match
              // needs to hang from. An ancestor is on screen as CONTEXT -- the filter
              // did not admit it -- so it renders dimmed and still carries its chevron.
              // Without it a filtered-out conductor's workers each resolved no parent
              // and scattered to the top level, which is what switching on Unread did.
              const kept = new Set<string>()
              for (const id of conductorMatching) {
                if (!byKey.has(id)) continue
                kept.add(id)
                for (const up of ancestorsOf(id, tree.parentOf)) kept.add(up)
              }
              const keptKids = (key: string) =>
                (tree.children.get(key) ?? []).filter(k => kept.has(k))
              const rootHeaderAt = new Map<number, ReturnType<typeof sectionHeaders>[number]>()
              const rows: Array<{ id: string; slot: Slot; depth: number; childCount: number; expanded: boolean; orphanOf: string | null; citesParent?: string | null; anchorOnly: boolean; aggregate: { needsYou: number; running: number } | null }> = []

              /** Does this row want the user? The same two signals the row itself
               *  renders as a dot or a subtitle, so a collapsed conductor's badge and
               *  its children's badges can never disagree. */
              // Both predicates are derived from the state the CHILD ROWS themselves
              // render from, because the collapsed aggregate claims to be the same fact
              // at a coarser zoom: a count that disagrees with the glyphs it stands for
              // is worse than no count. `runningSet` is the widened signal -- own turn,
              // a live workflow, or a loop -- so a workflow-active child is counted the
              // way its own row is drawn, and a subagent awaiting approval is an ask
              // even though the slot itself carries no `pending_approval`.
              const isRunning = sidebarRows.isRunning
              // The approval counts and the unread set are LOCAL state keyed by
              // local slot key; a peer row's raw key can spell a local one, so
              // only a local row reads them.
              const wantsUser = (s: Slot) =>
                !!(
                  s.pending_approval
                  || s.needs_input
                  || (!isPeerRow(s) && (subagentApprovalCounts[s.key] || 0) > 0)
                  || (!isPeerRow(s) && unreadSet.has(s.key) && !isRunning(s))
                )

              const emit = (key: string, depth: number) => {
                const slot = byKey.get(key)
                if (!slot) return
                const kids = keptKids(key)
                const expanded = conductorExpanded.has(key)
                const subtree = kids.length > 0 && !expanded
                  ? descendantsOf(key, tree.children).filter(k => kept.has(k))
                  : []
                // Which of the two citation glyphs this row earns. `orphanCitation` only
                // knows the row was placed under nothing; whether that is because the
                // creator closed or because the folder filter conceals it is decided
                // against the unfiltered population. A creator that is still there is
                // open and running, so saying it closed would be false.
                //
                // `closedCreatorCitation` is the row the backend re-parented: it IS
                // placed, under the nearest ancestor still open, and its own creator
                // closed. Same fact as an orphan's, so the same glyph, and the backend
                // said so outright -- which is why `ancestor` settles the reading here
                // rather than the population being consulted for it.
                const placedUnder = tree.parentOf.get(key) ?? null
                const cited = orphanCitation(slot, placedUnder)
                  ?? closedCreatorCitation(slot, placedUnder)
                const creator = citedCreatorOf(slot)
                const creatorStillOpen = cited != null && slot.parent?.ancestor !== true
                  && creator !== null
                  && (citedCreatorExists.get(creator.origin)?.has(creator.key) ?? false)
                rows.push({
                  id: key,
                  slot,
                  depth,
                  childCount: kids.length,
                  expanded,
                  orphanOf: creatorStillOpen ? null : cited,
                  citesParent: creatorStillOpen ? cited : null,
                  anchorOnly: !conductorMatching.has(key),
                  // Only a COLLAPSED conductor aggregates: while it is open its
                  // children show their own badges, and showing both would count the
                  // same session twice on one screen.
                  aggregate: subtree.length > 0
                    ? {
                      needsYou: subtree.filter(k => { const c = byKey.get(k); return c ? wantsUser(c) : false }).length,
                      running: subtree.filter(k => { const c = byKey.get(k); return c ? isRunning(c) : false }).length,
                    }
                    : null,
                })
                if (!expanded) return
                for (const kid of kids) emit(kid, depth + 1)
              }

              if (searching) {
                // Flattened: every match at depth 0, in the lane's order, with no
                // chevrons. Matches the flat lane's answer to the same question.
                for (const s of flatSlots) {
                  // The cited creator rides along even though the lane is not nesting:
                  // flattened, a child is otherwise indistinguishable from a root.
                  rows.push({ id: sessionRowIdentity(s), slot: s, depth: 0, childCount: 0, expanded: false, orphanOf: null, citesParent: s.parent?.slot ?? null, anchorOnly: false, aggregate: null })
                }
              } else {
                const roots = tree.roots.filter(key => kept.has(key))
                // "Group by kind" splits the top-level rows; a nested row stays under
                // the session that opened it. The lane order puts pinned roots first,
                // and they stay first, unsplit, as in every other lane.
                const rootSlots = roots.map(key => byKey.get(key)).filter((x): x is Slot => !!x)
                const ordered = sectionRows(rootSlots, sidebarRows.isPinned, kindSections)
                const headers = sectionHeaders(ordered, sidebarRows.isPinned, kindSections)
                ordered.forEach((slot, i) => {
                  const kind = headers[i]
                  if (kind) rootHeaderAt.set(rows.length, kind)
                  emit(sessionRowIdentity(slot), 0)
                })
              }

              return rows.map((row, i) => {
                const next = i < rows.length - 1 ? rows[i + 1] : null
                const isActive = isActiveRow(row.slot)
                const showDivider = next != null && !isActive && !isActiveRow(next.slot) && !rootHeaderAt.get(i + 1)
                const kindHeader = rootHeaderAt.get(i)
                return (
                  <Fragment key={row.id}>
                    {kindHeader && <KindSectionHeader kind={kindHeader} />}
                    {/* NO WRAPPER. The row is rendered exactly as the flat lane renders
                     *  it, and the lane's three additions are handed INTO it (see
                     *  `ConductorRowView`, and the chevron's `toggleConductor` action).
                     *  Wrapping the card in a flex column with the chevron and counts
                     *  beside it narrowed the card: its title truncated early, its own
                     *  divider stopped short of the row, and the counts took the column
                     *  the top line keeps for the time. */}
                    {renderSessionRow(row.slot, row.depth, showDivider, 'conductor', 'conductor', 'conductor', {
                      depth: row.depth,
                      childCount: row.childCount,
                      expanded: row.expanded,
                      aggregate: row.aggregate,
                      orphanOf: row.orphanOf,
                      citesParent: row.citesParent ?? null,
                      anchorOnly: row.anchorOnly,
                    })}
                  </Fragment>
                )
              })
            })()}
            {flatSlots.length === 0 && shownCrewRowCount === 0 && (
              <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
            )}
            {flatSlots.length > 0 && lineage != null && lineage.children.size === 0 && allHiddenFolders.length === 0 && (
              // Not an error state: the crew log may be off, or nothing has opened
              // anything yet. The lane still shows every session -- it just has no
              // nesting to show, and says so instead of looking broken.
              //
              // Withheld while this lane is concealing a folder, because then the note
              // cannot be read as intended: the rows above it are live sessions, and the
              // reveal row immediately below already says how many folders are hidden,
              // which is the actual reason there is no nesting left to draw.
              <div className="px-3 py-2 text-[11px] text-muted select-none" data-testid="conductor-lane-empty-note">
                {i18nT('pages.chatSidebar.no_conductor_sessions_yet')}
              </div>
            )}
            {renderHiddenReveal('conductor', allHiddenFolders, 0)}
            {renderOlderSessionsHint('conductor')}
            {renderCrewGroups()}
          </motion.div>
          </SessionRowWindowContext.Provider>
  )
  const renderFlatLane = (): React.ReactNode => (
          // Flat view: every chat exploded out of its folder into one lane.
          // Removes only the folder rendering hierarchy — sort, pin priority,
          // filters, and search all apply as usual (filteredSlots). No folder
          // tree. This lane only renders when NO tag columns exist: with a
          // board configured, flat view applies inside each column instead
          // (see the column body), so the board never silently disappears.
          // Inactive without folders (the toggle is hidden then too), so a
          // persisted flat preference can never strand the user.
          //
          // Its DndContext carries EXACTLY ONE target: the chat pane. No
          // SortableContext and no folder droppables are registered, so
          // dragging a session into the open chat works here just as it does in
          // the tree, while row order stays a pure function of the sort key —
          // there is nothing for a drop inside the lane to land on. (Order is
          // the reason: a flat lane spans every folder, so a manual position
          // would have no place to be stored.) `sidebarCollision` also keeps the
          // pane out of its closest-edge fallback, so a release inside the
          // sidebar resolves to no target rather than snapping to the pane.
          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
            measuring={dndMeasuring}
            onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
            <DndActiveProbe report={reportDndActive} />
            {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
              && createPortal(
                <ChatPaneDropZone refusal={draggingRefRefusal} />,
                chatDropTarget,
              )}
            <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
            <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="flat-view-lane">
              {/* Flat view renders no folder headers, so the per-folder mount
               *  points for the create-failure notice never exist here — yet
               *  the New menu still offers "New chat in folder". Render the
               *  notice at the top of the lane so a failed folder create is
               *  never console-only in this layout. */}
              {folderCreateError && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
              {localGroupHeader}
              {/* Caption header before the pinned band when a property filter narrows the list. */}
              {pinnedBandCaption && flatSlots.length > 0 && sidebarRows.isPinned(flatSlots[0]) && (
                <PinnedSessionDivider caption={pinnedBandCaption} />
              )}
              {(() => {
                // Date segments (Today / Yesterday / Last 7 Days / …) between
                // rows — resurrects the 9bb0f71 active-list pattern: only for
                // date sorts (segments mislead on name/created order, same
                // guard as the history pane), and pinned rows render first
                // without segments since pinning overrides date order.
                const isDateSort = sortKey === 'date-desc' || sortKey === 'date-asc'
                // Hoisted above the hold so the pixel anchor can count header heights.
                const baseSeg = (s: Slot) => (isDateSort && !sidebarRows.isPinned(s) ? dateSegment(slotActivityTs(s)) : '')
                // "Group by kind": the unpinned rows by section, each in lane order.
                const flatOrdered = sectionRows(flatSlots, sidebarRows.isPinned, kindSections)
                const { rows: flatRows, navScope: flatLaneScope, container: flatHoldContainer } = heldLane(flatOrdered, 'flat', 'flat', baseSeg, sectionRankOf(sidebarRows.isPinned, kindSections))
                const flatHeaders = sectionHeaders(flatRows, sidebarRows.isPinned, kindSections)
                // Reads the flag holdHovered just set for THIS lane, so the header
                // rule and the hold cannot disagree about the row being displaced.
                const heldKey = heldDisplacedRef.current ? hoverPinRef.current?.key : undefined
                // Unconditional exclusion would drop the header of a bucket whose
                // sole row — or the lane's top row — is merely being hovered.
                const segOf = (s: Slot) => {
                  const seg = baseSeg(s)
                  return seg && s.key === heldKey ? '' : seg
                }
                let prevSeg = ''
                return flatRows.map((s, i) => {
                  const kindHeader = flatHeaders[i]
                  // A new section starts its own date segments.
                  if (kindHeader) prevSeg = ''
                  const seg = segOf(s)
                  const showHeader = seg !== '' && seg !== prevSeg
                  if (seg) prevSeg = seg
                  const next = i < flatRows.length - 1 ? flatRows[i + 1] : null
                  const nextIsActive = isActiveRow(next)
                  const isActive = isActiveRow(s)
                  // No divider before a segment header — the header separates.
                  const nextSeg = next ? segOf(next) : seg
                  const showDivider = next != null && !isActive && !nextIsActive && nextSeg === seg
                    && !startsAutomaticSection(flatOrdered, i + 1) && !flatHeaders[i + 1]
                  return (
                    <Fragment key={sessionRowIdentity(s)}>
                      {startsAutomaticSection(flatOrdered, i) && <PinnedSessionDivider />}
                      {kindHeader && <KindSectionHeader kind={kindHeader} />}
                      {showHeader && (
                        <div data-date-header data-testid="date-segment-header" className="px-3 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {renderSessionRow(s, 0, showDivider, flatLaneScope, flatLaneScope, flatHoldContainer)}
                    </Fragment>
                  )
                })
              })()}
              {flatSlots.length === 0 && shownCrewRowCount === 0 && (
                <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
              )}
              {/* Flat view has no containers to anchor to — every hide, top-level
               *  or nested, collapses into this one row at the bottom of the lane. */}
              {renderHiddenReveal('flat', allHiddenFolders, 0)}
              {renderOlderSessionsHint('flat')}
              {renderCrewGroups()}
            </motion.div>
            </SessionRowWindowContext.Provider>
            {dragOverlay}
          </DndContext>
  )
  const renderTreeLane = (): React.ReactNode => (
          // Legacy single-lane layout (identical to pre-columns behavior)
          // Scrollbar hidden (scrollbar-none + inline scrollbarWidth covers
          // Firefox, modern WebKit, and Safari <16) to match the app rail in
          // App.tsx: on macOS with "always show scrollbars" this lane is
          // permanently scrollable, so the 6px track was a fixed stripe down
          // the sidebar rather than a transient hint. Scrolling itself is
          // untouched — wheel, trackpad, keyboard, and drag-autoscroll all
          // still work, and the list's own overflow is still the affordance.
          <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
          <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="tree-view-lane">
            {/* Scroll floor for collapseFloor.ts: lets a folder collapsed near
             *  the end of the list stay where its pinned header was instead of
             *  the list sliding down. Zero height and first in the lane, so
             *  nothing above it can move it; its hidden child is placed only
             *  during a collapse. */}
            <div {...{ [COLLAPSE_FLOOR_ATTR]: '' }} aria-hidden="true" className="relative h-0 shrink-0">
              <div className="absolute left-0 w-px h-px hidden pointer-events-none" />
            </div>
            {/* Tree-lane fallback, completing the set (flat and board lanes
             *  carry the same): a create into a folder the folder-filter or
             *  hide feature excludes never renders that folder's header, so
             *  its scoped notice mount does not exist. Renders exactly when
             *  the scoped mount cannot (folderCreateMountAbsent). */}
            {folderCreateError && folderCreateMountAbsent(folderCreateError.folderId) && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
            {/* One DndContext owns folder reorder (sortable) + session drag-to-
             *  assign (draggable rows + droppable folder/root targets). */}
            <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
              measuring={dndMeasuring}
              onDragStart={handleSidebarDragStart} onDragOver={handleSidebarDragOver} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
              <DndActiveProbe report={reportDndActive} />
              {/* "Drag a session into the open chat" target. Portaled into
               *  ChatPage's pane so it covers the WHOLE conversation area (not
               *  just the composer), while staying inside this DndContext —
               *  React portals preserve context, and useDroppable measures the
               *  node where it actually renders. Mounted only during a session
               *  drag, and only when a pane and a handler exist. */}
              {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
                && createPortal(
                  <ChatPaneDropZone refusal={draggingRefRefusal} />,
                  chatDropTarget,
                )}
              {/* Root lane is the fallback drop target: dropping a session on
               *  empty space (not over a folder) ungroups it (folderId: null). */}
              <DndDroppable id="root-lane" data={{ type: 'folder-drop', folderId: null }}>
                {({ setNodeRef }) => (
                  <div ref={setNodeRef} className="flex flex-col flex-1 min-h-0">
                    {localGroupHeader}
                    <SortableContext items={rootFolderIds} strategy={verticalListSortingStrategy}>
                      {visibleRootFolders.map(f => <SortableFolderBlock key={f.id} folder={f} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} siblings={rootFolderIds} reorderable={folderReorderable} dragWithheld={folderDragWithheld} renderFolderBlock={renderFolderBlock} />)}
                    </SortableContext>
                    {/* Bottom of the ROOT folder list. For a top-level hide this
                     *  is the sidebar's own bottom, which is exactly the "single
                     *  footer row" shape — the nested case is what needs depth. */}
                    {renderHiddenReveal('root', hiddenByContainer.get('root') ?? [], 0)}
                    {/* Every folder block and the ungrouped bucket read
                        laneSlots, so an empty one means nothing can render
                        below — say so rather than leaving a blank lane.
                        A folder-NAME search is the case where the plain wording
                        lies: the matched folder rows are rendered directly above
                        this line, so "No sessions match" alone reads as a
                        contradiction ("no conversations matched, even though two
                        folders did"). `folderNameMatchIds` is non-null only when
                        at least one folder name matched, which is exactly when
                        those rows are on screen, so it is the condition — not a
                        proxy for it.
                        The wording deliberately does NOT point at "the folders
                        above": the tree keeps a folder that merely holds a
                        subfolder, so a row that matched nothing can sit in that
                        list, and a sentence claiming it matched is the same
                        contradiction with the roles reversed. It claims only that
                        a folder name matched; the marks say which row. */}
                    {laneSlots.length === 0 && shownCrewRowCount === 0 && listNarrowed && (
                      <div className="px-3 py-4 text-[12px] text-muted">{i18nT(folderNameMatchIds ? 'pages.chatSidebar.no_sessions_match_folders' : 'pages.chatSidebar.no_sessions_match')}</div>
                    )}
                    {/* Ungrouped sessions live in a headerless droppable bucket
                     *  (folderId: null) that fills the remaining height below the
                     *  folders, so the whole empty lower area is a drop target —
                     *  dropping a session here ungroups it. The ring only lights up
                     *  while dragging a foldered session (when ungrouping applies). */}
                    {(rootFolders.length > 0 || ungroupedSlots.length > 0 || shownCrewGroups.length > 0) && (
                      <DndDroppable id="root-group" data={{ type: 'folder-drop', folderId: null }}>
                        {({ setNodeRef: setRootGroupRef, isOver }) => (
                          <div ref={setRootGroupRef} className={`flex flex-col flex-1 min-h-0 rounded-md transition-all ${isOver && (draggingFolderedSession || draggingNestedFolder) ? 'ring-1 ring-accent' : ''}`}>
                            {/* Explicit un-nest target while dragging a subfolder —
                             *  same escape hatch (and wording) as the session zone
                             *  below, always reachable even when the root lane has
                             *  no empty space. */}
                            {draggingNestedFolder && <RootDropHint />}
                            {(() => {
                              const { fresh: freshRootRaw, stale: staleRoot } = splitStale(ungroupedSlots)
                              const { rows: freshRoot, navScope: treeRootScope, container: treeRootContainer } = heldLane(sectionRows(freshRootRaw, sidebarRows.isPinned, kindSections), 'list', 'tree:root', undefined, sectionRankOf(sidebarRows.isPinned, kindSections))
                              const rootHeaders = sectionHeaders(freshRoot, sidebarRows.isPinned, kindSections)
                              return (
                                <>
                                  {/* Caption header before the pinned band when a property filter narrows the list. */}
                                  {pinnedBandCaption && freshRoot.length > 0 && sidebarRows.isPinned(freshRoot[0]) && (
                                    <PinnedSessionDivider caption={pinnedBandCaption} />
                                  )}
                                  {freshRoot.map((s, i) => {
                                    const nextIsActive = isActiveRow(freshRoot[i + 1])
                                    const isActive = isActiveRow(s)
                                    const showDivider = i < freshRoot.length - 1 && !isActive && !nextIsActive
                                      && !startsAutomaticSection(freshRoot, i + 1) && !rootHeaders[i + 1]
                                    const kindHeader = rootHeaders[i]
                                    return (
                                      <Fragment key={sessionRowIdentity(s)}>
                                        {startsAutomaticSection(freshRoot, i) && <PinnedSessionDivider />}
                                        {kindHeader && <KindSectionHeader kind={kindHeader} />}
                                        {renderSessionRow(s, 0, showDivider, treeRootScope, treeRootScope, treeRootContainer)}
                                      </Fragment>
                                    )
                                  })}
                                  {!searchRanked && staleExpanded.has('root')
                                    && staleRoot.length > 0 && freshRoot.length > 0
                                    && sidebarRows.isPinned(freshRoot[freshRoot.length - 1]) && <PinnedSessionDivider />}
                                  {renderStaleSection('root', staleRoot, 0)}
                                  {/* After the dormant expander, so it stays the
                                   *  lane's last line even when rows are folded. */}
                                  {renderOlderSessionsHint('root')}
                                  {renderCrewGroups()}
                                </>
                              )
                            })()}
                            {ungroupedSlots.length === 0 && draggingFolderedSession && <RootDropHint />}
                          </div>
                        )}
                      </DndDroppable>
                    )}
                  </div>
                )}
              </DndDroppable>
              {dragOverlay}
            </DndContext>
          </motion.div>
          </SessionRowWindowContext.Provider>
  )
  return { renderConductorLane, renderFlatLane, renderTreeLane }
}

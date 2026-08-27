import React from 'react'
import {
  closestCenter,
  pointerWithin,
  useDraggable,
  useDroppable,
  type Collision,
  type CollisionDetection,
  type DroppableContainer,
} from '@dnd-kit/core'

/**
 * Shared dnd-kit plumbing for folder-tree surfaces (chat sidebar, artifact
 * library), so both reuse the exact same wrappers instead of duplicating them.
 */

/** Re-rank collisions leaf-first by REAL DOM containment: a candidate whose
 *  node strictly contains another candidate's node ranks behind every
 *  candidate it contains, all the way down a containment chain (leaves, then
 *  their immediate containers, then theirs). Every candidate that contains
 *  no other candidate — including one UNRELATED to the rest — lands in the
 *  first peel, so unrelated candidates keep their incoming order only
 *  relative to EACH OTHER, and a caller that must keep an unrelated zone
 *  from outranking containers pre-filters it (sidebarCollision consults the
 *  chat-pane zone in a separate step for exactly this reason). Absolute DOM
 *  depth is never a priority: only a proven contains() relation reorders.
 *  Iterative leaf peeling keeps each round's group internally in incoming
 *  order, so the result is deterministic and transitivity-safe (contains()
 *  is a partial order; no pairwise sort comparator is involved). */
function leafFirst(collisions: Collision[]): Collision[] {
  if (collisions.length < 2) return collisions
  const node = (c: Collision): HTMLElement | null =>
    ((c.data?.droppableContainer as DroppableContainer | undefined)?.node?.current as HTMLElement | null) ?? null
  const out: Collision[] = []
  let remaining = [...collisions]
  while (remaining.length) {
    const nodes = remaining.map(node)
    const containsAnother = remaining.map((_, i) =>
      nodes[i] !== null && nodes.some((other, j) => j !== i && other !== null && nodes[i] !== other && nodes[i]!.contains(other)),
    )
    const leaves = remaining.filter((_, i) => !containsAnother[i])
    // Every remaining candidate containing another is impossible in a tree,
    // but fail safe rather than loop forever on pathological input.
    if (leaves.length === 0) { out.push(...remaining); break }
    out.push(...leaves)
    remaining = remaining.filter((_, i) => containsAnother[i])
  }
  return out
}

/**
 * `pointerWithin`, re-ranked so a containing droppable never beats one nested
 * inside it in the DOM.
 *
 * dnd-kit's pointerWithin ranks hits by average pointer→corner distance — a
 * box-size proxy for "innermost" that INVERTS on a folder tree inside a
 * scrollable lane: droppable rects are unclipped border boxes, so an expanded
 * folder block can be far TALLER than the scroll viewport while its ancestor
 * lane's box stays viewport-sized. The ancestor's smaller box then out-ranks
 * the folder the pointer is visually on, and a drop meant to file an item
 * into the folder resolves to the lane (unfiling it) with no highlight ever
 * shown on the folder. Nesting in the actual node tree — not box size — is
 * what "innermost" means here, so containment relations re-rank leaf-first;
 * unrelated overlaps keep pointerWithin's own corner-distance order.
 */
export const pointerWithinDeepest: CollisionDetection = (args) => {
  return leafFirst(pointerWithin(args))
}

/** Two edge distances within this many px count as the same edge; DOM
 *  containment then decides (leaf-first). Covers the sub-pixel gap between an
 *  ancestor lane's border box and its first child's (both "start" at the same
 *  visual line). */
const EDGE_TIE_EPSILON_PX = 1

/** Distance from a point to the nearest point of a rect (0 when inside). */
function pointToRectDistance(p: { x: number; y: number }, r: { top: number; bottom: number; left: number; right: number }): number {
  const dx = Math.max(r.left - p.x, 0, p.x - r.right)
  const dy = Math.max(r.top - p.y, 0, p.y - r.bottom)
  return Math.hypot(dx, dy)
}

/**
 * Near-miss fallback: rank droppables by distance from the POINTER to the
 * nearest EDGE of each rect (0 when inside), not to the rect's center.
 *
 * closestCenter penalizes tall rects: a pointer a fraction of a px outside a
 * tall expanded folder is "half the folder's height" from its center, so a
 * small nearby sibling wins and the drop lands in the wrong folder. Edge
 * distance matches what the user sees — the box their pointer is touching.
 * Within the leading near-tie group (EDGE_TIE_EPSILON_PX), DOM containment
 * re-ranks leaf-first, mirroring pointerWithinDeepest; unrelated near-ties
 * keep their distance order. A drag without pointer coordinates (keyboard or
 * synthetic activation) degrades to closestCenter rather than to no target.
 */
export const closestEdge: CollisionDetection = (args) => {
  const { droppableContainers, droppableRects, pointerCoordinates } = args
  if (!pointerCoordinates) return closestCenter(args)
  const collisions: Collision[] = []
  for (const container of droppableContainers) {
    const rect = droppableRects.get(container.id)
    if (!rect) continue
    collisions.push({ id: container.id, data: { droppableContainer: container, value: pointToRectDistance(pointerCoordinates, rect) } })
  }
  collisions.sort((a, b) => (a.data?.value ?? 0) - (b.data?.value ?? 0))
  if (collisions.length < 2) return collisions
  // Only the leading same-edge group is re-ranked; farther candidates keep
  // pure distance order.
  const best = collisions[0].data?.value ?? 0
  let cut = collisions.findIndex(c => (c.data?.value ?? 0) - best > EDGE_TIE_EPSILON_PX)
  if (cut === -1) cut = collisions.length
  return [...leafFirst(collisions.slice(0, cut)), ...collisions.slice(cut)]
}

/**
 * Collision strategy for surfaces that mix folder reordering with item
 * drag-to-folder assignment in one DndContext:
 *  - Dragging a folder: restrict collisions to folder containers so
 *    `over.id` resolves to a folder and sorting animates cleanly.
 *  - Dragging an item: prefer the innermost droppable under the pointer
 *    (folder/root drop target), falling back to closestCenter.
 */
export const folderAwareCollision: CollisionDetection = (args) => {
  const activeType = (args.active?.data?.current as { type?: string } | undefined)?.type
  if (activeType === 'folder') {
    const folderContainers = args.droppableContainers.filter(
      c => (c.data?.current as { type?: string } | undefined)?.type === 'folder'
    )
    return closestCenter({ ...args, droppableContainers: folderContainers })
  }
  const within = pointerWithin(args)
  return within.length ? within : closestCenter(args)
}

/** Render-prop wrapper exposing a dnd-kit draggable to inline JSX without
 *  defining a component per row (which would remount on every render). */
export function DndDraggable({ id, data, disabled, children }: {
  id: string
  data: Record<string, unknown>
  disabled?: boolean
  children: (p: { setNodeRef: (el: HTMLElement | null) => void; listeners: ReturnType<typeof useDraggable>['listeners']; attributes: ReturnType<typeof useDraggable>['attributes']; isDragging: boolean }) => React.ReactNode
}) {
  const { setNodeRef, listeners, attributes, isDragging } = useDraggable({ id, data, disabled })
  return <>{children({ setNodeRef, listeners, attributes, isDragging })}</>
}

/** Render-prop wrapper exposing a dnd-kit droppable to inline JSX. */
export function DndDroppable({ id, data, children }: {
  id: string
  data: Record<string, unknown>
  children: (p: { setNodeRef: (el: HTMLElement | null) => void; isOver: boolean }) => React.ReactNode
}) {
  const { setNodeRef, isOver } = useDroppable({ id, data })
  return <>{children({ setNodeRef, isOver })}</>
}

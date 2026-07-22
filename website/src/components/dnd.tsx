import React from 'react'
import {
  closestCenter,
  pointerWithin,
  useDraggable,
  useDroppable,
  type CollisionDetection,
} from '@dnd-kit/core'

/**
 * Shared dnd-kit plumbing for folder-tree surfaces (chat sidebar, artifact
 * library). Extracted from ChatSidebar so the artifact views reuse
 * the exact same wrappers instead of duplicating them.
 */

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

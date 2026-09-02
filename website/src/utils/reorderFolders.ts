import { arrayMove } from '@dnd-kit/sortable'
import type { ChatFolder } from '../types'

/**
 * Compute new order values after a drag-and-drop reorder.
 * Returns only folders whose order changed (for minimal PATCH calls).
 */
export function computeReorderedFolders(
  folders: ChatFolder[],
  activeId: string,
  overId: string,
): { id: string; order: number }[] {
  if (activeId === overId) return []
  const sorted = [...folders].sort((a, b) => a.order - b.order)
  const oldIndex = sorted.findIndex(f => f.id === activeId)
  const newIndex = sorted.findIndex(f => f.id === overId)
  if (oldIndex === -1 || newIndex === -1) return []
  const reordered = arrayMove(sorted, oldIndex, newIndex)
  const changes: { id: string; order: number }[] = []
  reordered.forEach((f, idx) => {
    if (f.order !== idx) changes.push({ id: f.id, order: idx })
  })
  return changes
}

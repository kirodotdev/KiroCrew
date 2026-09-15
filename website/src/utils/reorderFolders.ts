import { arrayMove } from '@dnd-kit/sortable'
import type { ChatFolder } from '../types'
import { bySidebarOrder } from './folderTree'

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
  // The same comparator the sidebar draws with, not an order-only sort: this
  // baseline decides which index each folder moves FROM, so a sort that differs
  // from the rendered sequence computes the move the person did not make.
  const sorted = [...folders].sort(bySidebarOrder)
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

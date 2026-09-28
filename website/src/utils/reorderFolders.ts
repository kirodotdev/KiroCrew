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

/**
 * The container a folder is drawn in: its `parent_id`, or the root lane.
 *
 * A `parent_id` naming a folder that is not in the list resolves to the root
 * lane, because that is where `orderFoldersWithPaths` draws such a row — its
 * `childrenOf` applies the same fallback. Scoping an orphan to the id it points
 * at would put it alone in a container nothing renders, so the drag that lands
 * on a root sibling would compute no move at all.
 */
const folderContainer = (f: ChatFolder, known: ReadonlySet<string>): string => {
  const pid = typeof f.parent_id === 'string' ? f.parent_id : ''
  return pid && known.has(pid) ? pid : ''
}

/**
 * Compute new `order` values for a drag among the dragged folder's SIBLINGS.
 *
 * `order` is a per-container index, not a global one: the sidebar sorts each
 * parent's children with `bySidebarOrder` independently, so a nested group
 * numbers itself 0..n exactly as the root lane does. That is why the renumber
 * has to be scoped — running it over the whole folder list would interleave
 * containers and hand every row an index from a sequence nobody draws.
 *
 * Only the container the ACTIVE folder already sits in is renumbered, and a drop
 * whose target is in a different container renumbers nothing: `overId` is absent
 * from the sibling list, so `computeReorderedFolders`' own `newIndex === -1`
 * refusal returns no changes. That gesture is a re-parent, the caller routes it to
 * the move path, and renumbering against a list the target is absent from would
 * otherwise shuffle the active folder's own siblings for a move that never
 * happened. There is deliberately no second membership check in front of that
 * refusal: no input exists that it alone decides, so it would read as a guard
 * while deciding nothing, and a mutation could remove it with every test still
 * green.
 */
export function computeSiblingReorder(
  folders: ChatFolder[],
  activeId: string,
  overId: string,
): { id: string; order: number }[] {
  if (activeId === overId) return []
  const known = new Set(folders.map(f => f.id))
  const active = folders.find(f => f.id === activeId)
  if (!active) return []
  const container = folderContainer(active, known)
  const siblings = folders.filter(f => folderContainer(f, known) === container)
  // Every entry is an `{id, order}` pair and nothing else. `order` is a
  // per-container index, and the reorder endpoint deliberately never reparents,
  // so the request's SHAPE is what makes it auditable as a reorder: a body whose
  // entries named a parent would be indistinguishable from a move.
  return computeReorderedFolders(siblings, activeId, overId)
}

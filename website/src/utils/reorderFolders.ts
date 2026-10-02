import type { ChatFolder } from '../types'
import { bySidebarOrder } from './folderTree'
import { planPosition } from './folderRank'

/**
 * The container a folder is drawn in: its `parent_id`, or the root lane.
 *
 * A `parent_id` naming a folder that is not in the list resolves to the root
 * lane, because that is where `orderFoldersWithPaths` draws such a row — its
 * `childrenOf` applies the same fallback, and so does the gateway's
 * `section_siblings`. Scoping an orphan to the id it points at would put it
 * alone in a container nothing renders, so a drag that lands on a root sibling
 * would compute no move at all.
 */
const folderContainer = (f: ChatFolder, known: ReadonlySet<string>): string => {
  const pid = typeof f.parent_id === 'string' ? f.parent_id : ''
  return pid && known.has(pid) ? pid : ''
}

/** What a sibling drag sends and what it draws while the server answers. */
export interface SiblingMove {
  /** The dragged folder. */
  id: string
  /** The PATCH body: the sibling the folder lands next to, and on which side. */
  anchor: { before: string } | { after: string }
  /** Every rank the gateway will write for this move, for the optimistic draw:
   *  the moved folder's own, plus any sibling a section re-spread touches. */
  ranks: Map<string, string>
}

/**
 * The move a drag of `activeId` onto `overId` makes among the dragged folder's
 * SIBLINGS, or `null` when it makes none.
 *
 * The baseline is the order the sidebar draws (`bySidebarOrder`), not a
 * rank-only sort: it decides which index the folder moves from, so a sort that
 * differs from the rendered sequence computes a move the person did not make.
 * Dragging down lands the folder AFTER `over`, dragging up lands it BEFORE, which
 * is where dnd-kit's `arrayMove` puts it.
 *
 * Only the container the ACTIVE folder already sits in is considered. A drop on
 * a folder in a different container returns `null`: that gesture is a re-parent
 * and the caller routes it to the move path.
 *
 * The request carries only the anchor; the gateway picks the rank. `ranks` is
 * the same plan computed here (`planPosition` mirrors the gateway's), so the
 * sidebar can draw the drop before the write returns.
 */
export function computeSiblingMove(
  folders: ChatFolder[],
  activeId: string,
  overId: string,
): SiblingMove | null {
  if (activeId === overId) return null
  const known = new Set(folders.map(f => f.id))
  const active = folders.find(f => f.id === activeId)
  if (!active) return null
  const container = folderContainer(active, known)
  const sorted = folders.filter(f => folderContainer(f, known) === container).sort(bySidebarOrder)
  const oldIndex = sorted.findIndex(f => f.id === activeId)
  const newIndex = sorted.findIndex(f => f.id === overId)
  if (oldIndex === -1 || newIndex === -1) return null
  const anchor = newIndex > oldIndex ? { after: overId } : { before: overId }
  // `newIndex` is also the landing slot in the list without the moved folder:
  // moving down, every row between shifts up one; moving up, none before it does.
  const siblings = sorted.filter(f => f.id !== activeId)
  const { rank, respread } = planPosition(siblings, newIndex)
  const ranks = new Map(respread)
  ranks.set(activeId, rank)
  return { id: activeId, anchor, ranks }
}

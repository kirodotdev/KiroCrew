import type { ChatFolder } from '../types'

/**
 * Folder IDs that hold at least one ACTIVE slot, directly or via any descendant
 * folder. `directFolderIds` is the set of folder IDs that have a slot filed
 * directly in them; membership is propagated up each folder's parent chain so a
 * parent counts as "active" when any descendant holds a session.
 */
export function computeActiveSubtree(
  folders: ChatFolder[],
  directFolderIds: Iterable<string>,
): Set<string> {
  const byId = new Map(folders.map(f => [f.id, f]))
  const result = new Set<string>()
  for (const start of directFolderIds) {
    let fid: string | undefined = start
    while (fid && !result.has(fid)) {
      result.add(fid)
      fid = byId.get(fid)?.parent_id || undefined
    }
  }
  return result
}

/**
 * A folder drops out of the active-sessions list only when the user hid it AND
 * its subtree currently has no active session. Re-engaging a session clears
 * `hidden` server-side, so the steady-state rule is `!hidden || hasActive`.
 */
export function folderIsHidden(folder: ChatFolder, activeSubtree: Set<string>): boolean {
  return !!folder.hidden && !activeSubtree.has(folder.id)
}

/**
 * Whether the folder menu offers "Hide when empty". Only when the folder has no
 * active session in its subtree (nothing to hide otherwise) AND at least one
 * archived session that can later revive it. A folder with zero sessions
 * (A=0, H=0) offers Delete only — hiding it would orphan it since nothing could
 * bring it back.
 */
export function folderOffersHide(folder: ChatFolder, activeSubtree: Set<string>): boolean {
  return !activeSubtree.has(folder.id) && (folder.history_count ?? 0) > 0
}

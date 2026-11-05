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

/**
 * Folder IDs covered by a folder pin: the pinned folders themselves and every
 * folder under one. A pin on a folder is a promise about its whole subtree —
 * "these sessions stay listed" — so a session filed in a pinned folder's
 * subfolder is as covered as one filed in the folder itself. Each folder's
 * parent chain is walked once; a `parent_id` loop in a hand-edited
 * folders.json terminates instead of freezing the tab.
 */
export function computePinnedSubtree(folders: readonly ChatFolder[]): Set<string> {
  const byId = new Map(folders.map(f => [f.id, f]))
  const covered = new Set<string>()
  for (const f of folders) {
    let cur: ChatFolder | undefined = f
    const visited = new Set<string>()
    while (cur && !visited.has(cur.id)) {
      visited.add(cur.id)
      if (cur.pinned) { covered.add(f.id); break }
      cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
    }
  }
  return covered
}

/**
 * Folder IDs that CONTAIN a pinned folder: the parent chain above each pinned
 * folder, up to the root. These are not covered by the pin (their own sessions
 * still answer to the chips), but they are the containers the pinned folder
 * renders inside, so the folder hide steps over them while the pin holds.
 * Cycle-guarded like `computePinnedSubtree`.
 */
export function computePinnedAncestors(folders: readonly ChatFolder[]): Set<string> {
  const byId = new Map(folders.map(f => [f.id, f]))
  const ancestors = new Set<string>()
  for (const f of folders) {
    if (!f.pinned) continue
    const visited = new Set<string>([f.id])
    let cur = f.parent_id ? byId.get(f.parent_id) : undefined
    while (cur && !visited.has(cur.id)) {
      visited.add(cur.id)
      ancestors.add(cur.id)
      cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
    }
  }
  return ancestors
}

/**
 * For each folder above a pin, the pinned folder its inert controls name: the
 * first by `compare` when it holds several. Keys match `computePinnedAncestors`.
 */
export function computeFirstPinnedDescendant(
  folders: readonly ChatFolder[],
  compare: (a: ChatFolder, b: ChatFolder) => number,
): Map<string, ChatFolder> {
  const byId = new Map(folders.map(f => [f.id, f]))
  const first = new Map<string, ChatFolder>()
  for (const p of folders) {
    if (!p.pinned) continue
    const visited = new Set<string>([p.id])
    let cur = p.parent_id ? byId.get(p.parent_id) : undefined
    while (cur && !visited.has(cur.id)) {
      visited.add(cur.id)
      const prev = first.get(cur.id)
      if (!prev || compare(p, prev) < 0) first.set(cur.id, p)
      cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
    }
  }
  return first
}

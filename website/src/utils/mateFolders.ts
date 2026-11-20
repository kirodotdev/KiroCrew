import type { ChatFolder } from '../types'

/** Whether *folder* is a mate's home or sits inside one. Such a folder is managed
 *  by the gateway: the person can open and collapse it, nothing else (no rename,
 *  move, delete, new chat, new folder, drop, or moving a chat in or out).
 *  Mirrors `mate_folders.home_owner_of`. */
export function isMateManaged(folder: ChatFolder | undefined, folders: readonly ChatFolder[]): boolean {
  const byId = new Map(folders.map(f => [f.id, f]))
  const seen = new Set<string>()
  let cursor: ChatFolder | undefined = folder
  while (cursor && !seen.has(cursor.id)) {
    if (cursor.mate_home && !cursor.parent_id) return true
    seen.add(cursor.id)
    cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined
  }
  return false
}

/** The folders the person's Sessions list draws: every folder, except a Bot's
 *  home and the folders inside it when none of them holds a session of
 *  *listedSlots* (the rows that list already shows, after `crewModeView`). A
 *  Bot whose sessions leave the list would otherwise leave an empty row with a
 *  `0` count, which reads as lost sessions; a Bot whose sessions are listed
 *  keeps its rows. Other readers (Bot page, pickers) keep the full list. */
export function foldersForSessionList(
  folders: ChatFolder[],
  listedSlots: readonly { folder_id?: string | null }[],
): ChatFolder[] {
  if (!folders.some(f => f.mate_home)) return folders
  const byId = new Map(folders.map(f => [f.id, f]))
  // Every folder that holds a listed slot, itself or below it.
  const held = new Set<string>()
  for (const s of listedSlots) {
    for (let id = s.folder_id || undefined; id && !held.has(id); id = byId.get(id)?.parent_id || undefined) held.add(id)
  }
  // The same walk as `isMateManaged`, sharing one map; `seen` stops a parent cycle.
  const dropped = new Set<string>()
  for (const f of folders) {
    const seen = new Set<string>()
    let cur: ChatFolder | undefined = f
    while (cur && !seen.has(cur.id) && !(cur.mate_home && !cur.parent_id)) {
      seen.add(cur.id)
      cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
    }
    if (cur?.mate_home && !cur.parent_id && !held.has(cur.id)) dropped.add(f.id)
  }
  return dropped.size ? folders.filter(f => !dropped.has(f.id)) : folders
}

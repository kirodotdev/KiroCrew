import type { ChatFolder } from '../types'

/** Whether *folder* is a mate's home or sits inside one. Such a folder is managed
 *  by the gateway: the person can open and collapse it, nothing else (no rename,
 *  move, new chat, new folder, drop, or moving a chat in or out), except delete
 *  an empty folder inside the home (`isEmptyBotSubfolder`).
 *  Mirrors `mate_folders.home_owner_of`. */
export function isMateManaged(folder: ChatFolder | undefined, folders: readonly ChatFolder[]): boolean {
  return !!mateHomeOf(folder, folders)
}

/** The mate's home that holds *folder* (the home itself included), or undefined. */
export function mateHomeOf(folder: ChatFolder | undefined, folders: readonly ChatFolder[]): ChatFolder | undefined {
  const byId = new Map(folders.map(f => [f.id, f]))
  const seen = new Set<string>()
  let cursor: ChatFolder | undefined = folder
  while (cursor && !seen.has(cursor.id)) {
    if (cursor.mate_home && !cursor.parent_id) return cursor
    seen.add(cursor.id)
    cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined
  }
  return undefined
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
  // The same walk as `mateHomeOf`, sharing one map; `seen` stops a parent cycle.
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

/** Whether the person may delete *folder*: a folder INSIDE a Bot's home (never
 *  the home) that holds no session (*holdsSession*), no archived session
 *  (`history_count`) and no child folder. Its menu then offers Delete folder
 *  and nothing else. The server re-checks every rule at delete time. */
export function isEmptyBotSubfolder(
  folder: ChatFolder | undefined,
  folders: readonly ChatFolder[],
  holdsSession: boolean,
): boolean {
  if (!folder || !folder.parent_id || !isMateManaged(folder, folders)) return false
  if (holdsSession || (folder.history_count ?? 0) > 0) return false
  return !folders.some(f => f.parent_id === folder.id)
}

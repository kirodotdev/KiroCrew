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

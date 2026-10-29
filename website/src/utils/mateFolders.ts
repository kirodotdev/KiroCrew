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

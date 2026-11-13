import type { ChatFolder } from '../types'
import { isMateManaged } from './mateFolders'

/** The color a session's chat pane takes from its folder: the folder's own
 *  palette color, else the nearest colored ancestor's, so a subfolder reads as
 *  part of its project. Null when the session is unfiled, no folder up the
 *  chain has a color, or the folder is a mate's (managed by the gateway, so
 *  the person's palette does not apply). Folder nesting has no depth limit, so
 *  the walk is bounded only by the `seen` set, which also ends a hand-edited
 *  `parent_id` loop. */
export function resolveFolderTintColor(folders: readonly ChatFolder[], folderId: string | null | undefined): string | null {
  if (!folderId) return null
  const byId = new Map(folders.map(f => [f.id, f]))
  const start = byId.get(folderId)
  if (!start || isMateManaged(start, folders)) return null
  const seen = new Set<string>()
  let cursor: ChatFolder | undefined = start
  while (cursor && !seen.has(cursor.id)) {
    if (cursor.color) return cursor.color
    seen.add(cursor.id)
    cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined
  }
  return null
}

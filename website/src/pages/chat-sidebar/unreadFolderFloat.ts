/** The "Move folders with unread chats to the top" view: a comparator over the
 *  folder sort mode that draws a folder holding an unread chat (directly or in
 *  any subfolder) before its siblings, newest unread first. Display only: it
 *  never writes a folder's stored `order`, so turning it off draws the mode's
 *  own order again exactly. */
import { slotActivityTs } from '../chat/sessionOrder'
import type { ChatFolder } from '../../types'
import type { Slot } from './types'

type FolderCompare = (a: ChatFolder, b: ChatFolder) => number

/** Folder id -> newest activity (epoch ms, 0 when unstamped) of an unread chat in
 *  that folder's subtree. A folder with no unread chat has no entry. */
export function unreadFolderLatest(folders: readonly ChatFolder[], slots: readonly Slot[],
  slotFolders: Readonly<Record<string, string>>, unread: ReadonlySet<string>): Map<string, number> {
  const byId = new Map(folders.map(f => [f.id, f]))
  const latest = new Map<string, number>()
  for (const s of slots) {
    if (!unread.has(s.key) || !slotFolders[s.key]) continue
    const ts = Date.parse(slotActivityTs(s) ?? '') || 0
    // Up the ancestor chain, cycle-guarded: a hand-edited folders.json can loop.
    const seen = new Set<string>()
    for (let f = byId.get(slotFolders[s.key]); f && !seen.has(f.id); f = f.parent_id ? byId.get(f.parent_id) : undefined) {
      seen.add(f.id)
      latest.set(f.id, Math.max(latest.get(f.id) ?? 0, ts))
    }
  }
  return latest
}

/** `base` with folders holding an unread chat first, newest unread first; ties
 *  and every other folder keep `base`'s order. */
export const unreadFirstComparator = (base: FolderCompare, latest: ReadonlyMap<string, number>): FolderCompare =>
  (a, b) => {
    const ta = latest.get(a.id) ?? -1
    const tb = latest.get(b.id) ?? -1
    return tb - ta || base(a, b)
  }

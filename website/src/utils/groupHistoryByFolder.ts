import type { ChatFolder } from '../types'

/** Stable key for the Unfiled bucket (no folder_id, or a deleted folder). */
export const UNFILED_GROUP_KEY = '__unfiled__'

export interface HistoryFolderGroup<T> {
  /** Stable React key + collapse-state key. `UNFILED_GROUP_KEY` for the Unfiled bucket. */
  readonly key: string
  /** The folder for this group, or `undefined` for the Unfiled bucket. */
  readonly folder?: ChatFolder
  readonly rows: readonly T[]
}

/**
 * Bucket history search results by the folder each session was filed in.
 *
 * Groups follow sidebar folder order — a depth-first walk over root folders
 * (each level sorted by `order`) — so the grouping mirrors what the user sees
 * in the active list. The Unfiled bucket (a row with no `folder_id`, or one
 * pointing at a deleted folder) always comes last. Row order within each bucket
 * is preserved, so callers passing relevance-ranked results keep that ranking.
 */
export function groupHistoryByFolder<T extends { folder_id?: string }>(
  rows: readonly T[],
  folders: readonly ChatFolder[],
): HistoryFolderGroup<T>[] {
  const folderById = new Map(folders.map(f => [f.id, f]))
  const orderIndex = new Map<string, number>()
  let oi = 0
  const walk = (fs: ChatFolder[]) => {
    for (const f of fs) {
      orderIndex.set(f.id, oi++)
      walk(folders.filter(c => c.parent_id === f.id).sort((a, b) => a.order - b.order))
    }
  }
  walk(folders.filter(f => !f.parent_id).sort((a, b) => a.order - b.order))

  const buckets = new Map<string, T[]>()
  for (const row of rows) {
    const fid = row.folder_id && folderById.has(row.folder_id) ? row.folder_id : UNFILED_GROUP_KEY
    const bucket = buckets.get(fid)
    if (bucket) bucket.push(row)
    else buckets.set(fid, [row])
  }

  const keys = [...buckets.keys()]
    .filter(k => k !== UNFILED_GROUP_KEY)
    .sort((a, b) => (orderIndex.get(a) ?? Number.MAX_SAFE_INTEGER) - (orderIndex.get(b) ?? Number.MAX_SAFE_INTEGER))
  if (buckets.has(UNFILED_GROUP_KEY)) keys.push(UNFILED_GROUP_KEY)

  return keys.map(k => ({
    key: k,
    folder: k === UNFILED_GROUP_KEY ? undefined : folderById.get(k),
    rows: buckets.get(k)!,
  }))
}

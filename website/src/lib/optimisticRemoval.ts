/**
 * Undo ONE optimistic row removal, and nothing else.
 *
 * A delete that hides its row before the server answers must put that row back
 * when the server refuses. Restoring a whole-list snapshot taken in `onMutate`
 * does that, but it also undoes every LATER optimistic change to the same list
 * that landed while this request was in flight: a second delete's row comes
 * back, a refetch that already moved on is rewound. The chat sidebar's folder
 * update rolls back field by field for the same reason (chat-sidebar/folders.ts).
 *
 * `takeRow` records the row and where it sat; `putRowBack` re-inserts that row
 * alone, at its old position (clamped to the current length), and only when the
 * list does not already hold it. A list that is not cached stays uncached.
 */
export interface RemovedRow<T> {
  row: T
  index: number
}

export function takeRow<T>(list: readonly T[] | undefined, isRow: (item: T) => boolean): RemovedRow<T> | undefined {
  const index = list?.findIndex(isRow) ?? -1
  return list && index >= 0 ? { row: list[index], index } : undefined
}

export function putRowBack<T>(list: T[] | undefined, removed: RemovedRow<T> | undefined, isRow: (item: T) => boolean): T[] | undefined {
  if (!list || !removed || list.some(isRow)) return list
  const at = Math.min(removed.index, list.length)
  return [...list.slice(0, at), removed.row, ...list.slice(at)]
}

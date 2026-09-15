/**
 * COPY one slot's entry in a draft bucket onto the slot replacing it.
 *
 * A mode switch creates a replacement slot and retires the old one. The buckets are keyed by
 * slot, so without this the unsent work is orphaned under a key nothing reads: the composer
 * remounts against the new key and no store answers for the old one.
 *
 * A copy rather than a move, and the caller drops the original only once the deletion has
 * SUCCEEDED — a failed delete leaves the old slot alive and still holding its own work.
 *
 * Absence is preserved rather than normalised. Writing `bucket[to] = undefined` would put an
 * empty entry under the new key, and an empty-but-present draft reads as unsent work to the
 * close guard — so a slot that never had files would start by refusing its own close.
 */
export function copySlotEntry<T>(bucket: Record<string, T>, from: string, to: string): boolean {
  if (!from || !to || from === to) return false
  if (!(from in bucket)) return false
  bucket[to] = bucket[from]
  return true
}

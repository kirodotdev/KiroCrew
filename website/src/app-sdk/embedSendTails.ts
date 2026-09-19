/**
 * ChatEmbed's transcript-tail records, one per send, retired independently.
 *
 * A send that was refused or whose delivery could not be confirmed leaves a
 * row at the transcript's tail (an `error` or a `notice`) and, for a composer
 * submit, hands its text back into the composer. Several such records can be
 * live at once: the hand-back buffer (`embedSendRecovery`) keeps every receipt
 * that landed after its embed unmounted, and the next embed for the slot
 * drains them all. Each record therefore carries its OWN `sendId`, and proof
 * of delivery -- the polled user row that echoes that id -- retires that one
 * record only. Keeping a single "last outcome" here would let a later
 * recovery overwrite an earlier one's id, so the earlier send's delivered text
 * could never be taken back and would sit in the composer inviting a
 * duplicate turn.
 *
 * Taking the text back is chain-aware. Restores merge into the composer one
 * after another, so retiring the FIRST of two means rebuilding the composer
 * from that record's `before` with every later restore replayed on top -- and
 * only when the composer still holds exactly what the chain produced and
 * nothing was typed between the restores. Any edit since leaves the text
 * alone: withdrawing text the user has touched would re-create the silent
 * loss this surface exists to prevent.
 *
 * The replay uses the CALLER's merge (the `merge` parameter), never its own:
 * ChatEmbed's composer is a single-line `<input>`, so its merge flattens the
 * shared rule's paragraph break to a space, and a replay that merged with the
 * raw rule would write the newline back into a field that cannot hold it.
 * One rule, owned by the composer, applied to first restore and replay alike.
 */

/** How a composer merges a handed-back text into its current value -- the
 *  same function it used for the original restore, so replay and restore can
 *  never disagree on what the composer holds. */
export type RestoreMerge = (before: string, text: string) => string

export interface SendTail {
  role: 'error' | 'notice'
  content: string
  /** The client-minted id stamped on the wire (`meta.sendId`). */
  sendId: string
  /** The text this record handed back to the composer and the composer value
   *  just before and after that restore, so proven delivery can take exactly
   *  this text back out. Absent for a chip send, which never consumed the
   *  draft. */
  restore?: { text: string; before: string; after: string }
}

export interface RetireResult {
  tails: SendTail[]
  /** The composer value with the retired record's text taken back out, or
   *  `null` when the text must stay (the record restored nothing, or the
   *  composer no longer holds exactly what the restore chain produced). */
  draft: string | null
}

/** Retire the record with `sendId`, returning the remaining records and the
 *  composer value to write back, if any. Unknown ids leave everything as is. */
export function retireSendTail(tails: SendTail[], sendId: string, draft: string, merge: RestoreMerge): RetireResult {
  const idx = tails.findIndex(t => t.sendId === sendId)
  if (idx < 0) return { tails, draft: null }
  const retired = tails[idx]
  const rest = tails.filter((_, i) => i !== idx)
  if (!retired.restore) return { tails: rest, draft: null }

  // Every later restore, in order. The chain is intact only when each one
  // started from exactly what the previous one produced (nothing typed in
  // between) and the composer still holds the last one's result.
  const later = tails.slice(idx + 1).filter(t => t.restore)
  let expected = retired.restore.after
  for (const t of later) {
    if (t.restore!.before !== expected) return { tails: rest, draft: null }
    expected = t.restore!.after
  }
  if (draft !== expected) return { tails: rest, draft: null }

  // Replay the later restores on top of the retired record's `before`, and
  // record each one's new before/after so a later retirement sees the chain
  // as it now stands.
  let value = retired.restore.before
  const replayed = new Map<string, SendTail>()
  for (const t of later) {
    const after = merge(value, t.restore!.text)
    replayed.set(t.sendId, { ...t, restore: { text: t.restore!.text, before: value, after } })
    value = after
  }
  return { tails: rest.map(t => replayed.get(t.sendId) ?? t), draft: value }
}

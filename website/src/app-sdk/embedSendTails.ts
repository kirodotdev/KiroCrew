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
 * The replay uses the CALLER's merge (the `merge` parameter), never its own,
 * so a composer that merges differently from the shared rule is replayed with
 * the same function it restored with.
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
  /** The attachment paths this record handed back to the composer, so proven
   *  delivery can unstage them instead of inviting a second send. */
  restoreFiles?: string[]
  /** Set only for a send a host `onSend` carried (#21686). That send posts on
   *  the host's own endpoint; unless the host forwards `meta.sendId` (#22061),
   *  no row can ever carry the id: without this the notice could never retire, and a message
   *  that did land stayed "not confirmed" with its text back in the composer. */
  hostProof?: HostSendProof
  /** Set when a text match settled this notice ({@link sendTailProof} `'text'`):
   *  it is no longer judged. The record stays, so the restore chain a later
   *  id-proven retirement replays still holds this send's text. */
  settled?: boolean
  /** A settled record with nothing to explain (a chip send kept no text). */
  hidden?: boolean
}

/** What proves a host `onSend` send landed: a user row with exactly the sent
 *  text that sits AFTER the newest row the poll showed at send time. Position,
 *  not "absent from the page at send time": the page is bounded, so "Load
 *  earlier" can bring in an OLD row with the same text that was never on the
 *  page, and taking that as proof would clear an undelivered draft. */
export interface HostSendProof {
  text: string
  /** {@link rowKey} of the anchor row at send time (see {@link hostProofBaseline});
   *  `null` only for a transcript the server confirmed empty. */
  anchor: string | null
  /** The slot the send went to. Only that slot's rows can prove it: an embed
   *  re-propped to another slot keeps its tails, and that slot's rows say
   *  nothing about this send. */
  slot: string
}

type PolledRow = { role: string; content: string; ts?: string; meta?: Record<string, unknown> }

/** Identity of a polled row: its server `mid`, else its timestamp, else its text. */
export const rowKey = (m: PolledRow): string =>
  (m.meta?.mid as string | undefined) ?? m.ts ?? m.content

/** Whether a polled user row is THIS send's own, by identity. A plain row
 *  carries `meta.sendId`; a row the drain merged from several queued sends
 *  (`merge_queued_messages`) names every send it stands for in `meta.sendIds`,
 *  and membership there is the same proof. Anything else -- no id, another
 *  id, an older backend that drops `meta` -- is not. */
export function isOwnUserRow(m: PolledRow, sendId: string): boolean {
  if (m.role !== 'user' || !m.meta) return false
  const many = m.meta.sendIds
  return m.meta.sendId === sendId || (Array.isArray(many) && many.includes(sendId))
}

/** The {@link HostSendProof} baseline for a send made while the poll shows
 *  `page`. The anchor is the newest USER row (a durable row whose identity a
 *  reply streaming in after it cannot change), else the page's first row.
 *  Rows "Load earlier" brings in land before either, so they can never prove
 *  the send. `null` when there is NO baseline -- no page has loaded yet, or an
 *  empty page with older history behind it -- so the send gets no text proof
 *  and its notice keeps the restored text: an old same-text row in the first
 *  read would otherwise read as delivery. */
export function hostProofBaseline(
  page: { messages?: ReadonlyArray<PolledRow>; has_more?: boolean } | undefined,
): Pick<HostSendProof, 'anchor'> | null {
  if (!page) return null
  const rows = page.messages ?? []
  let pick: PolledRow | undefined
  for (let i = rows.length - 1; i >= 0 && !pick; i--) if (rows[i].role === 'user') pick = rows[i]
  pick ??= rows[0]
  if (!pick) return page.has_more ? null : { anchor: null }
  // A row with neither `mid` nor `ts` is keyed by its text, which a later
  // same-text row shares, so it cannot mark a position: no text proof then.
  if (!pick.meta?.mid && !pick.ts) return null
  return { anchor: rowKey(pick) }
}

/** Whether a settled calm line is still true: its kept text is still in the
 *  composer. Read at render time, so clearing or editing the text away later
 *  makes the line go away instead of going stale. */
export function calmLineHolds(tail: Pick<SendTail, 'restore'>, draft: string): boolean {
  const text = tail.restore?.text.trim()
  return !!text && draft.includes(text)
}

/** What `messages` -- the server rows of `slot` -- say about the tail's send.
 *
 *  - `'id'`: a row carries this send's own `sendId` ({@link isOwnUserRow}).
 *    That is proof of THIS send, so the caller may also take its restored
 *    text back out of the composer.
 *  - `'text'`: a host send (no id on the wire) whose text arrived as a user
 *    row after the anchor row, in the slot it was sent to. Text cannot say
 *    WHICH send landed (an earlier same-text send, another sender, a repeated
 *    "continue"), so this only clears the notice and never touches the
 *    composer: a wrong match then costs a hidden notice, never lost text.
 *  - `null`: nothing proves it. With no anchor on the page (newer rows pushed
 *    it out, a rewind removed it, no page had loaded) the notice stays. */
export function sendTailProof(
  tail: Pick<SendTail, 'sendId' | 'hostProof'>,
  messages: ReadonlyArray<PolledRow>,
  slot: string,
): 'id' | 'text' | null {
  if (messages.some(m => isOwnUserRow(m, tail.sendId))) return 'id'
  const host = tail.hostProof
  if (!host || host.slot !== slot) return null
  let from = 0
  if (host.anchor != null) {
    const at = messages.map(rowKey).lastIndexOf(host.anchor)
    if (at < 0) return null
    from = at + 1
  }
  // Compared trimmed, as isPendingAcked does: a host endpoint may strip the
  // text it stores, and an expanded paste can end in whitespace.
  const sent = host.text.trim()
  return messages.slice(from).some(m => m.role === 'user' && m.content.trim() === sent) ? 'text' : null
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

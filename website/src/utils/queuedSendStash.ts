/** The queued-send stashes, in a module of their own.
 *
 *  Deliberately NOT in `useQueuedMessageActions`: the slot-detail hydration path in `chatSlice`
 *  has to adopt a pre-send record when the `queue_push` broadcast was missed, and importing the
 *  hook module from the slice closes an import cycle (the hook imports the slice's action
 *  creators) which leaves `chatReducer` undefined at module-init. No React, no store imports --
 *  so every writer and reader can share the one store. */

import { restoreQueuedContent } from './fileTokens'

/** Pre-serialization composer state of a send the server QUEUED, written by the
 *  host's send path when the `queued: true` receipt names the entry. */
export interface QueuedSendRecord {
  /** The text exactly as the user typed it. */
  raw: string
  /** The staged file paths at send time. */
  files: string[]
  /** The exact POSTed LLM-facing text — the edit guard: an entry edited after
   *  send keeps its queue id but fails this equality, so an edited card falls
   *  to the parser instead of clobbering the edit with pre-edit state. */
  sent: string
  /** The text THIS tab submitted for an edit that has not settled yet. */
  pendingEdit?: string
  /** The correlation id that edit was submitted under. The broadcast echoes it, and NOTHING else
   *  distinguishes a concurrent editor's frame from this tab's own -- both arrive as `{queue_id,
   *  content}` -- so an echo that does not name this id is another tab's and must not settle here. */
  pendingEditId?: string
  /** This tab's edit RESOLVED as a failure. The pending fields stay so a delayed echo can still
   *  settle; this says a remote edit may now retire the record. */
  editRolledBack?: boolean
  /** The display text a settle re-pointed this record to. The server's `edited` flag is PERMANENT,
   *  so this is what tells a record written FOR that edit from one that predates it. */
  settledFor?: string
  /** WHICH edit `settledFor` describes. The display form is redacted, so it cannot be an identity. */
  settledEditId?: string
  // The highest server revision known APPLIED to this entry. An id names an edit but cannot ORDER
  // two, which is what tells a superseded edit from an echo that merely arrived first.
  appliedEditRev?: number
}

/** Queued-send stash, keyed by the `queue_id` the send receipt returns (the
 *  same id `queue_push` broadcasts and the card's cancel button carries).
 *  Queue identity is the ONLY sound key: the serialization is not injective
 *  (image @-tokens are erased from the LLM-facing text), so content-keyed
 *  records can collide across different captions, duplicate sends, and other
 *  tabs. Module-level so every host's send path (ChatPage, ChatPane) writes
 *  the one store this hook's cancel consumes — the same one-owner reasoning
 *  as the hook itself (#5891). Deliberately unevicted: an entry dies on the
 *  cancel that consumes it, and evicting a live entry would degrade that
 *  card's cancel to the parser fallback; orphans from normal delivery are
 *  three small strings bounded by queued sends per tab session. */
export const queuedSendStash = new Map<string, QueuedSendRecord>()

/** Record the highest server revision applied to an entry. Monotonic: a frame arriving out of order
 *  must not lower it, or a later success would be judged against a revision already replaced. */
export function noteAppliedEditRev(queueId: string | undefined, rev: unknown): void {
  if (!queueId || typeof rev !== 'number') return
  const rec = queuedSendStash.get(queueId)
  if (!rec) return
  const prior = typeof rec.appliedEditRev === 'number' ? rec.appliedEditRev : 0
  if (rev <= prior) return
  queuedSendStash.set(queueId, { ...rec, appliedEditRev: rev })
}

/** Whether an edit STRICTLY newer than `rev` has already been applied.
 *
 *  A server sending no revision answers false: the pending-marker comparison this replaces is what
 *  discarded a success it should have kept, so an unknown order must not inherit that behaviour. */
export function editSupersededByNewerRev(queueId: string | undefined, rev: unknown): boolean {
  if (!queueId || typeof rev !== 'number') return false
  const rec = queuedSendStash.get(queueId)
  const applied = rec && typeof rec.appliedEditRev === 'number' ? rec.appliedEditRev : 0
  return applied > rev
}

/** The same records keyed by `sendId` and written BEFORE the POST, because the queue id only
 *  arrives with the receipt. A send whose 2xx body is unreadable never learns its queue id, and
 *  a BUSY send has no optimistic row either, so `appendQueuedMessage` finds no raw text to carry
 *  and a later cancel would restore only the server's redacted copy, dropping the attachments. */
export const preSendStash = new Map<string, QueuedSendRecord>()

/** Write a pre-send record. Deliberately UNEVICTED, on the same reasoning as `queuedSendStash`.
 *
 *  A record only stays here while its outcome can still produce a `queue_push`, because settling one
 *  REMOVES it -- `retirePreSendStash` on a dispatch or refusal, `adoptPreSendStash` on the push. So
 *  every entry present is by construction still live, and any cap could only ever drop a live one:
 *  the later cancel would then restore the server's redacted copy without the attachments, which is
 *  the exact loss this stash exists to prevent and which nothing downstream can reconstruct.
 *  What is left over is sends whose outcome never arrived at all -- three small strings and a file
 *  list each, bounded by one tab session, the same residue the queue-keyed store already accepts. */
export function stashPreSend(sendId: string, rec: QueuedSendRecord): void {
  preSendStash.set(sendId, rec)
}

/** Retire a record once the send's outcome rules out a `queue_push` naming it: an immediate
 *  dispatch or a refusal. A queued acceptance does NOT qualify — its push is still to come. */
export function retirePreSendStash(sendId: string | undefined): void {
  if (sendId) preSendStash.delete(sendId)
}

/** Move a pre-send record onto the queue id the server assigned, once `queue_push` names both.
 *
 *  `content` is the BROADCAST text, and it becomes the record's `sent`: the cancel guard compares
 *  `sent` against the card's own content, which the server may have REDACTED (image @-tokens are
 *  erased). Keeping the sender's raw text there failed that guard, so cancel fell to the parser and
 *  restored the masked copy WITHOUT the attachments — the loss this stash exists to prevent. */
export function adoptPreSendStash(sendId: string | undefined, queueId: string | undefined, content?: string): void {
  if (!sendId || !queueId) return
  const rec = preSendStash.get(sendId)
  const existing = queuedSendStash.get(queueId)
  if (!rec && !existing) return
  if (rec) preSendStash.delete(sendId)
  // The receipt's copy of {raw, files} is written from the sending surface and stays authoritative;
  // only the BROADCAST content settles `sent`, so a receipt that landed first is UPDATED, not skipped.
  const base = existing ?? rec as QueuedSendRecord
  queuedSendStash.set(queueId, typeof content === 'string' ? { ...base, sent: content } : base)
}

/** Write the receipt-path record, PRESERVING one `queue_push` already adopted.
 *
 *  `queue_push` can win the race against its own HTTP receipt. The adopted record's `sent` is the
 *  server's BROADCAST content, which is what the cancel guard compares against the card; the
 *  receipt's is the sender's un-redacted copy. Overwriting therefore broke cancel on a redacted
 *  entry, dropping the attachments the parser fallback cannot recover. */
export function stashQueuedSend(queueId: string | undefined, rec: QueuedSendRecord): void {
  if (!queueId || queuedSendStash.has(queueId)) return
  queuedSendStash.set(queueId, rec)
}

/** Record the text this tab submitted, BEFORE the POST. It is kept on the record rather than the row
 *  because a rollback clears the row's `editPending` while a committed edit can still echo later. */
export function noteLocalQueueEdit(
  queueId: string | undefined,
  submitted: string,
  editId: string,
): void {
  if (!queueId) return
  const rec = queuedSendStash.get(queueId)
  if (rec) queuedSendStash.set(queueId, { ...rec, pendingEdit: submitted, pendingEditId: editId })
}

/** Settle this tab's own edit RESPONSE. The pending text goes, so a later remote edit can retire the
 *  record; only the id stays, which is all a delayed echo of this same edit needs to be recognised. */
export function settleLocalEditResponse(
  queueId: string | undefined,
  submitted: string,
  display: string,
): void {
  if (!queueId) return
  const before = queuedSendStash.get(queueId)
  restashEditedSend(queueId, submitted, display)
  const after = queuedSendStash.get(queueId)
  if (!after) return
  const { pendingEdit: _settled, ...rest } = after
  // Carry the id forward: it is what a later queue snapshot correlates against, and the display
  // string cannot serve because redaction makes it collide across clients.
  queuedSendStash.set(queueId, before?.pendingEditId ? { ...rest, settledEditId: before.pendingEditId } : rest)
}

/** Settle an edit an echo announces, from the identity this tab kept. Reports whether it ANSWERED the
 *  frame, so one it does not recognise falls through to retirement. */
export function settleQueueEditEcho(
  queueId: string | undefined,
  display: string,
  editId: unknown,
): boolean {
  if (!queueId) return false
  const rec = queuedSendStash.get(queueId)
  if (!rec) return false
  // Correlated, not merely "a local edit is pending": a concurrent editor's frame arrives in the
  // same shape, and settling on it rebound the record to text the server never took.
  if (typeof editId !== 'string' || !editId || editId !== rec.pendingEditId) return false
  // The response may have settled this already, in which case the echo only has its id to spend.
  if (typeof rec.pendingEdit === 'string') restashEditedSend(queueId, rec.pendingEdit, display)
  const after = queuedSendStash.get(queueId)
  // SPEND the identity: it answers exactly one echo, so leaving it set made every later remote edit
  // unretirable, and a stale record then answered a cancel with this tab's own text.
  if (after) {
    const { pendingEdit: _spent, pendingEditId: _spentId, editRolledBack: _spentRb, ...rest } = after
    // Spending the identity still leaves what it PROVED: this record describes the edit `editId`
    // named, which is what a later snapshot correlates against instead of the redacted display.
    queuedSendStash.set(queueId, { ...rest, settledEditId: editId })
  }
  return true
}

/** Retire the record for an entry the server marks EDITED, unless it is the one a settle wrote for
 *  exactly that text. The flag is permanent, so retiring blindly destroyed current recovery data. */
export function retireEditedQueueRecord(
  queueId: string | undefined,
  content: string,
  entryEditId?: unknown,
): void {
  if (!queueId) return
  const rec = queuedSendStash.get(queueId)
  if (!rec) return
  // An edit of ours still in flight has nothing settled yet, so it never predates the edit.
  if (typeof rec.pendingEdit === 'string') return
  // The entry's own edit id is the only sound correlation: `settledFor` holds the REDACTED
  // display, and redaction erases image markers, so another client's edit can render it too.
  if (typeof entryEditId === 'string' && entryEditId) {
    if (rec.settledEditId === entryEditId) return
    queuedSendStash.delete(queueId)
    return
  }
  // No id on the entry (a server that predates it): fall back to the display comparison.
  if (rec.settledFor === content) return
  queuedSendStash.delete(queueId)
}

/** Retire a record a DIFFERENT client's edit made stale: its text and attachments are not this
 *  entry's any more, and `sent` alone cannot refuse them when the edit echoes the redacted form. */
/** Records that this tab's edit request FAILED. Deliberately does NOT clear `pendingEdit` or
 *  `pendingEditId`: a delayed echo of the failed request still arrives, and settling it is what
 *  recovers an attachment path the parser cannot rebuild. */
export function rollbackLocalQueueEdit(queueId: string | undefined): void {
  if (!queueId) return
  const rec = queuedSendStash.get(queueId)
  if (!rec) return
  queuedSendStash.set(queueId, { ...rec, editRolledBack: true })
}

export function retireRemoteQueueEdit(queueId: string | undefined): void {
  if (!queueId) return
  const rec = queuedSendStash.get(queueId)
  if (!rec) return
  // An unsettled edit of this tab's own is the one thing that may keep the record: anything else
  // reaching here describes an entry whose text is no longer the one this record was written for.
  // A ROLLED-BACK edit is not unsettled -- it failed -- so it may not keep the record alive either,
  // which is what left a stale record to answer a later cancel with pre-edit text.
  if (typeof rec.pendingEdit !== 'string' || rec.editRolledBack === true) queuedSendStash.delete(queueId)
}

/** Whether `refs` mentions exactly this path, rather than merely containing it. A plain substring
 *  test kept `/tmp/report.pdf` alive on the strength of `/tmp/report.pdf.bak`, restoring a removed
 *  attachment onto a resend. A real mention ends the path: end of text, or a non-path character. */
function mentionsPath(refs: string, path: string): boolean {
  if (!path) return false
  let from = 0
  for (;;) {
    const at = refs.indexOf(path, from)
    if (at < 0) return false
    const before = at > 0 ? refs[at - 1] : undefined
    const after = refs[at + path.length]
    // Both ends must be a boundary: an absolute path is a contiguous SUFFIX of any path holding it,
    // so checking only the trailing side accepts a longer path that merely ends the same way.
    const opens = before === undefined || /[\s"'`,;:([{<]/.test(before)
    const closes = after === undefined || /[\s"'`,;:)\]}>]/.test(after)
    if (opens && closes) return true
    from = at + 1
  }
}

/** Re-point an adopted record at the EDITED entry. Dropping it instead sent a later cancel to the
 *  parser, which cannot recover an attachment path containing a space. */
export function restashEditedSend(
  queueId: string | undefined,
  submitted: string,
  display: string,
): void {
  if (!queueId) return
  const rec = queuedSendStash.get(queueId)
  if (!rec) return
  // An edit that DROPPED a marker prunes that attachment server-side, so carrying `files` through
  // unchanged re-staged a removed file on cancel and a blind retry would resend it.
  const refs = `${submitted}\n${display}`
  const declared = new Set([
    ...restoreQueuedContent(submitted).files,
    ...restoreQueuedContent(display).files,
  ])
  // Both texts are consulted so redaction cannot look like a REMOVED attachment. Containment also
  // covers what the parser will not CLAIM: its `(\S+)` capture truncates a path holding a space.
  const files = rec.files.filter(p => declared.has(p) || mentionsPath(refs, p))
  // `raw` is what the user SUBMITTED, `sent` what the card SHOWS -- redaction rewrites the display
  // form, so collapsing the two restored the server's text into the composer instead of the user's.
  queuedSendStash.set(queueId, { ...rec, raw: submitted, sent: display, files, settledFor: display })
}

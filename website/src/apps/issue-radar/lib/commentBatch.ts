// Timeline comments: the pending batch and the reply helpers behind TimelineComposer.
//
// A maintainer can post a comment straight away, or hold several comments and
// replies as a PENDING batch and submit them together, the way a forge's own
// "Start a review" works. The batch lives in localStorage, one key per item, so a
// reload or a pane switch does not throw away prose that has not been sent yet.
//
// A reply goes INTO a thread when the comment carries a `thread_id` (GitHub inline
// review comments, every GitLab note, Azure DevOps PR threads). Without one, for
// example a GitHub conversation comment, the reply is a new comment that quotes
// the original, which reads the same on every provider.

import type { CommentBatchItem, CommentBatchResponse, TimelineEvent } from '../api'

/** Matches the backend's `MAX_BATCH_ITEMS`. */
export const MAX_PENDING = 25

export type CommentTarget = 'issue' | 'pull'

/** What a Reply button hands the composer. */
export interface ReplyTarget {
  author: string | null
  body: string
  thread_id: string | null
}

/** One held comment. `id` is local, only for React keys and removal. */
export interface PendingComment extends CommentBatchItem {
  id: string
  /** Who the reply answers, shown on the pending row. Null for a top-level comment. */
  reply_to: string | null
  /** The timeline comment this answers (see `anchorKeyOf`), so the pending reply
   * shows under it and Edit reopens its inline box there. Null for a top-level
   * comment; absent on batches stored before inline replies. */
  anchor?: string | null
  /** A submit sent it but could not tell whether the forge took it. Kept so its
   * text is not lost, and left out of every submit until the user re-adds it
   * after checking the timeline, or dismisses it. */
  unconfirmed?: boolean
}

/** A stable key for one timeline comment: the forge's comment id, with its kind
 * and thread because Azure DevOps numbers comments per thread. Two comments by the
 * same author in the same second would share any key built from author and time,
 * so those are only the fallback for an event that came without an id. */
export function anchorKeyOf(ev: TimelineEvent): string {
  if (ev.id !== undefined && ev.id !== null && ev.id !== '') {
    return ['id', ev.kind, ev.thread_id ?? '', String(ev.id)].join('|')
  }
  return [ev.kind, ev.actor ?? '', ev.created_at, ev.thread_id ?? ''].join('|')
}

export function replyTargetOf(ev: TimelineEvent): ReplyTarget {
  return { author: ev.actor, body: ev.body ?? '', thread_id: ev.thread_id ?? null }
}

const PARAGRAPH_BREAK = '\n\n'

/** The text a quote-reply starts with: a mention, then the original as a block
 * quote. Long comments are cut, because a reply quotes the point it answers, not the
 * whole essay. */
export function quoteReply(target: ReplyTarget, maxLines = 6): string {
  const lines = target.body.trim().split('\n')
  const kept = lines.slice(0, maxLines)
  if (lines.length > maxLines) kept.push('…')
  const quoted = kept.map((line) => (line ? `> ${line}` : '>')).join('\n')
  // Markdown structure, not prose: the mention and the quote are separate paragraphs,
  // and the trailing blank line leaves the cursor below the quote.
  const parts = target.author ? [`@${target.author}`, quoted] : [quoted]
  return [...parts, ''].join(PARAGRAPH_BREAK)
}

export function pendingStorageKey(
  scopeKey: string, target: CommentTarget, number: number,
): string {
  return `issue-radar:pending-comments:${scopeKey}:${target}:${number}`
}

function isPending(value: unknown): value is PendingComment {
  if (!value || typeof value !== 'object') return false
  const v = value as Record<string, unknown>
  return typeof v.id === 'string'
    && typeof v.body === 'string'
    && (v.thread_id === null || typeof v.thread_id === 'string')
    && (v.reply_to === null || typeof v.reply_to === 'string')
    && (v.anchor === undefined || v.anchor === null || typeof v.anchor === 'string')
    && (v.unconfirmed === undefined || typeof v.unconfirmed === 'boolean')
}

/** The stored batch, `[]` when nothing is stored, or `null` when storage cannot be
 * read at all. A corrupt or foreign entry reads as `[]` rather than throwing: the
 * composer must still open. */
function readStored(key: string): PendingComment[] | null {
  let raw: string | null
  try {
    raw = localStorage.getItem(key)
  } catch {
    return null
  }
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter(isPending).slice(0, MAX_PENDING) : []
  } catch {
    return []
  }
}

/** The stored batch, or `[]`. */
export function loadPending(key: string): PendingComment[] {
  return readStored(key) ?? []
}

/** Whether the batch reached storage. */
export function savePending(key: string, items: PendingComment[]): boolean {
  try {
    if (items.length) localStorage.setItem(key, JSON.stringify(items))
    else localStorage.removeItem(key)
    return true
  } catch {
    // Storage full or disabled: the batch still works for this page's lifetime.
    return false
  }
}

// The page's own copy of every item's batch, and the ids of the pending comments
// each item's in-flight submit is sending. Both outlive a pane's mount, because the
// user can leave an item and come back before its submit lands.
//
// Storage is the authority, because another tab may have changed the batch since
// this page last read it. The copy is the fallback for when storage cannot be read,
// or did not take this page's last write: storage is then empty or stale, and
// reconciling against it would drop the comments the forge did not take.
const latestBatches = new Map<string, PendingComment[]>()
const unsaved = new Set<string>()
const inFlight = new Map<string, ReadonlySet<string>>()
// Pending comments open in a row editor, kept apart from the batch submit so the
// two never clear each other's lock.
const held = new Map<string, Set<string>>()

/** The item's batch: the stored one, unless storage cannot be trusted for it. */
export function readBatch(key: string): PendingComment[] {
  const stored = unsaved.has(key) ? null : readStored(key)
  if (stored !== null) {
    latestBatches.set(key, stored)
    return stored
  }
  return latestBatches.get(key) ?? []
}

/** Record the item's batch in this page's copy and in storage. */
export function writeBatch(key: string, items: PendingComment[]): void {
  latestBatches.set(key, items)
  if (savePending(key, items)) unsaved.delete(key)
  else unsaved.add(key)
}

/** Whether the item's batch is held only in this page, because storage did not
 * take the last write: it is lost on reload, so the pane says so. */
export function isUnsaved(key: string): boolean {
  return unsaved.has(key) && (latestBatches.get(key)?.length ?? 0) > 0
}

export function isSubmitting(key: string): boolean {
  return inFlight.has(key)
}

/** Whether the pending comment `id` is part of the item's in-flight submit. The
 * forge may already have it. */
export function isInFlight(key: string, id: string): boolean {
  return inFlight.get(key)?.has(id) ?? false
}

/** Whether the pending comment `id` is open in a row editor, in any pane. */
export function isHeld(key: string, id: string): boolean {
  return held.get(key)?.has(id) ?? false
}

/** Whether the pending comment `id` may not be edited, removed, replaced or
 * submitted right now: a submit is sending it, or a row editor has it open. */
export function isLocked(key: string, id: string): boolean {
  return isInFlight(key, id) || isHeld(key, id)
}

/** Mark the pending comment `id` as open in a row editor, or as released. */
export function setHeld(key: string, id: string, holding: boolean): void {
  const ids = held.get(key) ?? new Set<string>()
  if (holding) ids.add(id)
  else ids.delete(id)
  if (ids.size) held.set(key, ids)
  else held.delete(key)
}

/** The item's Web Locks manager, or null where there is none (an insecure origin,
 * jsdom): the property can exist and be null, so the check is on the method. */
function webLocks(): LockManager | null {
  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks
  return typeof locks?.request === 'function' ? locks : null
}

/** Run one read-modify-write of the item's batch holding a lock every tab of this
 * origin shares, so a write in one tab never lands between another tab's read and
 * its write. Without Web Locks it runs at once, which within one tab is already
 * atomic: the read and the write are one synchronous step. */
export function withBatchWrite(key: string, write: () => void): Promise<void> | void {
  const locks = webLocks()
  if (!locks) {
    write()
    return
  }
  // A synchronous callback: request() resolves to what the callback returns, and
  // an async one would make that a promise of a promise.
  return locks.request(`issue-radar-batch:${key}`, () => { write() })
}

// The last submit error of each item, kept for the page's lifetime: the pane that
// submitted may be closed before the answer arrives, and the next pane opened on
// the item must still say what happened.
const batchErrors = new Map<string, string>()

export function getBatchError(key: string): string | null {
  return batchErrors.get(key) ?? null
}

export function setBatchErrorFor(key: string, message: string | null): void {
  if (message) batchErrors.set(key, message)
  else batchErrors.delete(key)
}

/** Run `task` holding the item's submit lock, shared by every tab of this origin,
 * so two tabs never send the same batch. `task` must read the batch itself, after
 * the lock is held: what another tab submitted meanwhile is gone by then. Resolves
 * false, without running `task`, when another tab is submitting this item. A
 * browser without Web Locks runs `task` under this page's own lock only. */
export async function withSubmitLock(key: string, task: () => Promise<void>): Promise<boolean> {
  const locks = webLocks()
  if (!locks) {
    await task()
    return true
  }
  return locks.request(`issue-radar-submit:${key}`, { ifAvailable: true }, async (lock) => {
    if (!lock) return false
    await task()
    return true
  })
}

/** Mark the item's submit as sending `ids`, or as settled with `null`. */
export function setSubmitting(key: string, ids: readonly string[] | null): void {
  if (ids) inFlight.set(key, new Set(ids))
  else inFlight.delete(key)
}

/** Tests only: forget the page-lifetime state between cases. */
export function resetBatchState(): void {
  latestBatches.clear()
  unsaved.clear()
  inFlight.clear()
  held.clear()
  batchErrors.clear()
}

let counter = 0
/** An id no other tab can also mint: the batch is shared across tabs, and two
 * tabs adding in the same millisecond must not produce the same id. */
export function newPendingId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // No Web Crypto (an insecure origin): random enough that two tabs never meet.
  counter += 1
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}-${counter}`
}

/** The request items for a batch, in order. */
export function toBatchItems(items: PendingComment[]): CommentBatchItem[] {
  return items.map(({ body, thread_id }) => ({ body, thread_id }))
}

/** The pending comments still to send after a submit of `sent`: everything in
 * `current` except the sent items the server reported as posted. Posted ones must
 * never be resent, because they are already on the forge. Matching by id rather
 * than by position keeps anything added to `current` while the submit was in
 * flight, and an item whose text or thread changed since it was sent is kept too:
 * what the forge took is the old text, so the new one has not been sent. */
export function remainingAfter(
  current: PendingComment[],
  sent: PendingComment[],
  result: Pick<CommentBatchResponse, 'posted' | 'unknown'>,
): PendingComment[] {
  const byIndex = (indexes: readonly number[]) => {
    const map = new Map<string, PendingComment>()
    for (const index of indexes) {
      const item = sent[index]
      if (item) map.set(item.id, item)
    }
    return map
  }
  const posted = byIndex(result.posted.map((p) => p.index))
  const unknown = byIndex(result.unknown ?? [])
  const sentIds = new Set(sent.map((p) => p.id))
  const unchanged = (was: PendingComment, item: PendingComment) =>
    was.body === item.body && was.thread_id === item.thread_id
  return current.flatMap((item) => {
    const was = posted.get(item.id)
    if (was && unchanged(was, item)) return []
    // The forge may already have it: kept, so its text is not lost, but marked so
    // a submit does not send it again until the user says to.
    const maybe = unknown.get(item.id)
    if (maybe && unchanged(maybe, item)) return [{ ...item, unconfirmed: true }]
    // Sent but neither posted nor unknown: the answer shows it did not post, so
    // the mark it carried while in flight comes off.
    return [sentIds.has(item.id) ? { ...item, unconfirmed: false } : item]
  })
}

/** Whether storage still holds one of `sent` without its unconfirmed mark, so a
 * reload would offer it for sending again. */
export function unmarkedInStorage(key: string, sent: PendingComment[]): boolean {
  const ids = new Set(sent.map((p) => p.id))
  return loadPending(key).some((p) => ids.has(p.id) && !p.unconfirmed)
}

/** Every item of `sent` still in `current`, with its unconfirmed mark taken off:
 * what a submit does when the gateway answers that nothing was posted. */
export function clearUnconfirmed(current: PendingComment[], sent: PendingComment[]): PendingComment[] {
  const ids = new Set(sent.map((p) => p.id))
  return current.map((item) => (ids.has(item.id) ? { ...item, unconfirmed: false } : item))
}

/** Every item of `sent` still in `current`, marked unconfirmed: what a submit
 * stores before its request leaves, and keeps when no answer tells it otherwise. */
export function markUnconfirmed(current: PendingComment[], sent: PendingComment[]): PendingComment[] {
  const ids = new Set(sent.map((p) => p.id))
  return current.map((item) => (ids.has(item.id) ? { ...item, unconfirmed: true } : item))
}

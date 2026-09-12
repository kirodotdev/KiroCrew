/**
 * Slot-keyed hand-back buffer for ChatEmbed send receipts.
 *
 * ChatEmbed's recovery state (the failure / unconfirmed row and the text it
 * hands back to the composer) is component-local: the draft lives in
 * `useComposerDraft`, the tail row in a `useState`. A send is asynchronous, and
 * a host app can unmount the embed while one is in flight -- switching specs,
 * opening a new one, closing a drawer. When the receipt then lands, writing it
 * into the dead instance loses the only copy of a REFUSED send outright (the
 * server never took it, so no poll will ever show it) and drops the
 * "unconfirmed" warning for a late one.
 *
 * So a receipt that finds its embed gone (or re-propped to another slot) is
 * parked HERE, keyed by the APP the embed belongs to and the slot the send
 * belonged to, and the next ChatEmbed that app mounts for that slot drains
 * it -- the same shape SideChat uses through the
 * store (`SideState.sendStatus`, with the text in the per-slot draft store), kept module-local
 * because the app-sdk embed deliberately owns no store slice of its own.
 *
 * A slot can have MORE than one receipt parked: the in-flight guard is
 * per-instance (`sendMutation.isPending` resets on a fresh mount), so an embed
 * remounted for the same slot can send again while the first instance's send
 * is still out, and both can come back refused. Every parked receipt is kept,
 * in arrival order, and the draining embed applies each -- the handed-back
 * texts merge into the composer one after another (nothing overwrites), and
 * the transcript tail shows every outcome as its own row, each keeping its own
 * `sendId` so its own proof of delivery retires it (`embedSendTails`). If an embed for the slot is
 * already mounted when a receipt lands (the user navigated away and back
 * within the deadline), the subscriber is notified and drains immediately, so
 * nothing waits for a remount that already happened.
 *
 * The app is part of the key because two apps can embed the same slot (the
 * permission scope is per app, the slot is shared). A receipt parked by app A
 * carries A's user's text; app B mounting an embed for the same slot must not
 * drain it -- B would render A's composer text and delete A's only copy.
 */

export interface PendingEmbedRecovery {
  /** The transcript-tail row to show: a failure, or an unconfirmed notice. */
  tail: {
    role: 'error' | 'notice'
    content: string
    sendId: string
  }
  /** The composer text to hand back, as typed. Absent for a chip send, which
   *  never consumed the draft. */
  restoreText?: string
}

/** The parking address: one app's embeds for one slot. `app` is the manifest
 *  name (`useAppInfo().name`), stable across provider remounts. NUL cannot
 *  appear in either part, so the join is unambiguous. */
export interface EmbedRecoveryScope {
  app: string
  slot: string
}

const keyOf = ({ app, slot }: EmbedRecoveryScope): string => [app, slot].join('\u0000')

const pending = new Map<string, PendingEmbedRecovery[]>()
const listeners = new Map<string, Set<() => void>>()

/** Park a receipt for a scope whose embed is not mounted (or not showing that
 *  slot). Appends -- never replaces -- so overlapping sends each keep their
 *  copy. Notifies a mounted subscriber for the scope, if any. */
export function stashEmbedRecovery(scope: EmbedRecoveryScope, rec: PendingEmbedRecovery): void {
  const key = keyOf(scope)
  const queue = pending.get(key)
  if (queue) queue.push(rec)
  else pending.set(key, [rec])
  const subs = listeners.get(key)
  if (subs) for (const cb of subs) cb()
}

/** Take (and clear) every parked receipt for a scope, oldest first. */
export function drainEmbedRecovery(scope: EmbedRecoveryScope): PendingEmbedRecovery[] {
  const key = keyOf(scope)
  const queue = pending.get(key)
  if (!queue) return []
  pending.delete(key)
  return queue
}

/** Be told when a receipt is parked for `scope` while subscribed. Returns the
 *  unsubscribe. */
export function subscribeEmbedRecovery(scope: EmbedRecoveryScope, cb: () => void): () => void {
  const key = keyOf(scope)
  let subs = listeners.get(key)
  if (!subs) { subs = new Set(); listeners.set(key, subs) }
  subs.add(cb)
  return () => {
    subs.delete(cb)
    if (subs.size === 0) listeners.delete(key)
  }
}

/** Test seam: forget every parked receipt and subscriber. */
export function resetEmbedRecoveryForTests(): void {
  pending.clear()
  listeners.clear()
}

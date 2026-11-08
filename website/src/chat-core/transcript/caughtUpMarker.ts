/**
 * The caught-up marker: the newest message a reader had on screen the last
 * time they were at the bottom of a transcript, kept per slot so the next
 * entry can land them on it instead of on the live end.
 *
 * The persisted reading position (`ScrollAnchorCache`) answers "where was the
 * reader when they left mid-transcript". It is CLEARED at the bottom on
 * purpose, because "no anchor" is what makes the next entry open at the live
 * end. That leaves the common crewmate case unserved: the reader was caught
 * up, left, the crewmate kept talking, and on return the transcript opens at
 * the newest row with everything in between above the fold. This marker is
 * the missing fact -- the last row they SAW -- and `findUnreadRun` turns it
 * into the boundary the host lands on and draws its divider above.
 *
 * Storage only; which host saves and lands is the host's (ChatPane's) call.
 * Falls back to no-op without localStorage; a malformed blob reads as "no
 * marker", never a throw. The key is the raw slot key so `utils/storageGc.ts`
 * reaps it with the slot's other per-session entries.
 */
import type { ChatMessage } from '../../types'

/** localStorage key prefix -- a storage identifier, never rendered. Listed in
 *  `SESSION_PREFIXES` (`utils/storageGc.ts`) so a gone slot's marker is swept. */
export const SEEN_TAIL_KEY_PREFIX = 'vc_seen_'

/** The identity a marker is stored under: the server-minted `meta.mid` when
 *  the row carries one, else the row's `ts`. The mid ALONE, not paired with
 *  `ts`: a send the reader made keeps its client `ts` after its confirmation
 *  stamps the mid (a dashboard user row is not echoed), while the hydrated copy
 *  on the next visit carries the server's `ts` -- the mid is the one identity
 *  both sides share. Never the client-side `clientTs`, which a reload does not
 *  round-trip. `null` for a row with no identity at all. */
export function durableMessageId(m: ChatMessage): string | null {
  const mid = m.meta?.mid
  if (typeof mid === 'string' && mid) return `m:${mid}`
  return m.ts || null
}

/** Whether `m` is a row the client is still holding provisionally: an
 *  optimistic send awaiting its echo, a queued send drawn as a bubble, or a
 *  row still streaming. Their `ts` is the client's, and the server's copy
 *  lands under another one, so a marker saved from them would never match
 *  again. */
export function isProvisionalRow(m: ChatMessage): boolean {
  const meta = m.meta
  return m.role === 'streaming' || m.role === 'queued'
    || meta?.optimistic === true || meta?.queuedBubble === true || typeof meta?.queueId === 'string'
}

/** The newest message a marker may be saved from: the last row that is not
 *  provisional and carries a durable id (a client-only row such as a local
 *  send-failure notice has no `ts`, and must not stand in the way of the row
 *  before it). `null` when no row qualifies. */
export function newestDurableMessage(messages: readonly ChatMessage[]): ChatMessage | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (!isProvisionalRow(messages[i]) && durableMessageId(messages[i])) return messages[i]
  }
  return null
}

function getStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null
    return window.localStorage
  } catch {
    return null
  }
}

/** Remember `messageId` as the newest row the reader saw at the bottom of
 *  `slotKey`. Best-effort; a write that changes nothing is skipped. */
export function saveSeenTail(slotKey: string, messageId: string): void {
  const storage = getStorage()
  if (!storage || !slotKey || !messageId) return
  try {
    const key = `${SEEN_TAIL_KEY_PREFIX}${slotKey}`
    if (storage.getItem(key) === messageId) return
    storage.setItem(key, messageId)
  } catch {
    // Quota or private mode: losing the marker only means the next entry
    // takes the default placement, the live end.
  }
}

/** The marker saved for `slotKey`, or null when there is none. */
export function loadSeenTail(slotKey: string): string | null {
  const storage = getStorage()
  if (!storage || !slotKey) return null
  try {
    const v = storage.getItem(`${SEEN_TAIL_KEY_PREFIX}${slotKey}`)
    return typeof v === 'string' && v.length > 0 ? v : null
  } catch {
    return null
  }
}

/** The run of messages the reader has not seen: where it starts and how long
 *  it is. */
export interface UnreadRun {
  /** `durableMessageId` of the first unseen message -- the row the divider
   *  sits above and the landing puts at the top of the viewport. */
  startId: string
  /** How many messages follow the marker. */
  count: number
}

/**
 * Resolve the saved marker against the transcript on hand.
 *
 * `null` means "nothing to land on": no marker (a first visit), a marker whose
 * row is gone (a truncated or reset transcript -- never guess a boundary from a
 * row that is not there), or a marker that IS the newest row (caught up, the
 * default bottom placement stands). The first unseen message must itself carry
 * a durable id, or there is nothing for the divider to attach to.
 */
/** Whether the marker's row is among `messages` at all. `false` with a bounded
 *  page on hand means the row may still be in OLDER history, not that it is
 *  gone: the caller pages back before it concludes anything. */
export function markerOnHand(messages: readonly ChatMessage[], seenTail: string | null): boolean {
  if (!seenTail) return true
  for (let i = messages.length - 1; i >= 0; i--) {
    if (!isProvisionalRow(messages[i]) && durableMessageId(messages[i]) === seenTail) return true
  }
  return false
}

export function findUnreadRun(messages: readonly ChatMessage[], seenTail: string | null): UnreadRun | null {
  if (!seenTail || messages.length === 0) return null
  // Over the rows that have a server identity only: the reader's own queued or
  // optimistic send is theirs, not unseen, and its id is renamed when the
  // server's copy lands -- a run starting on it would vanish mid-visit.
  const rows = messages.filter((m) => !isProvisionalRow(m))
  let at = -1
  // Newest first: the marker is the tail of some recent visit, so it sits
  // near the end of the list, and a long transcript is not walked from the top.
  for (let i = rows.length - 1; i >= 0; i--) {
    if (durableMessageId(rows[i]) === seenTail) { at = i; break }
  }
  if (at < 0 || at >= rows.length - 1) return null
  const startId = durableMessageId(rows[at + 1])
  if (!startId) return null
  return { startId, count: rows.length - 1 - at }
}

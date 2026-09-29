/** Pure calculations over one transcript array: message identity (the durable
 *  client id, the server `meta.mid`, the one-shot `sendId`), redelivery and
 *  duplicate detection, echo reconciliation, streaming finalization, the
 *  chunk-seq floor a snapshot vouches for, and the bounded-page identity rules
 *  every slot-detail reducer cuts and merges by. Nothing here touches chat
 *  state; callers pass the arrays in. */
import type { ChatMessage } from '../../types'
import { jsonEqual } from '../../utils/structuralEqual'
import { secureRandomId } from '../../utils/secureId'

/** Durable client-side identity for a message born WITHOUT a `ts` that will be
 *  mutated across dispatches (streaming/thinking accumulation). ChatPage keys
 *  rows by `meta.clientTs ?? ts` and falls back to a WeakMap id minted per
 *  message OBJECT — but Immer replaces the object on every `content +=` commit,
 *  so without a stamped identity a ts-less accumulating message would mint a
 *  NEW id (→ new React key → full row remount) on every chunk flush. That
 *  remount would reset useSmoothStream's reveal cursor (text snapping in whole
 *  chunks) and restart every CSS/Framer animation in the row
 *  (widget-placeholder dots flashing in unison). Stamping the identity once at
 *  append survives Immer's structural sharing for the message's whole life,
 *  including the streaming→assistant finalization that later sets a server
 *  `ts`. (This is the "durable id stamped in the reducer at append" that
 *  ChatPage's stableMsgKey comment points at.)
 *
 *  Uses a cryptographically-strong UUID (via secureRandomId) so message identity
 *  is exact and collision-free — no timestamp heuristics, no sequence numbers.
 *  The field is `meta.clientTs` for backward compatibility with existing
 *  renderers and the mergePreservedClientTs rehydration path. */
export const mintMsgId = (): string => `msg-${secureRandomId()}`

/** Stamp a stable `meta.clientTs` on a message that has no server `ts` and no
 *  pre-existing client id. This makes every ts-less message carry a durable
 *  identity from birth, surviving Immer structural sharing, refetch/rehydration,
 *  and list replacement — closing the identity gap for error/system/permission
 *  messages that were previously only stable via WeakMap (object identity). */
export const ensureMsgId = (msg: ChatMessage): ChatMessage => {
  if (msg.ts || (msg.meta as Record<string, unknown> | undefined)?.clientTs) return msg
  msg.meta = { ...(msg.meta || {}), clientTs: mintMsgId() }
  return msg
}

/** True when a WS chat frame is a REDELIVERY of a row the transcript already
 *  holds, so applying it again would render the same message twice — or, in the
 *  `assistant` branch, overwrite a live stream with stale text.
 *
 *  Identity is the server-minted row id `meta.mid` (`_ChatSlot.append`), and
 *  nothing else. The backend stamps it once per row and every door the row can
 *  arrive through carries it: the slot-detail HTTP rebuild, the live
 *  `chat_message` broadcast, and the JSONL round trip (persisted with `meta`,
 *  restored with it), so the two copies of one row are recognisably one row.
 *
 *  What this replaces, and why: a (`ts`, role, content) tuple cannot express
 *  this. A coarse OS clock stamps two rows appended in the same tick identically
 *  (the collision `mergePreservedClientTs` pass 1 already guards against) and two
 *  byte-identical messages are legitimate — a Slack channel window can replay
 *  exactly that pair. So a tuple either misses a redelivery (a duplicate bubble)
 *  or matches two distinct rows (a message silently disappears), and no tuning
 *  removes the ambiguity. An explicit id does.
 *
 *  A frame with NO `mid` is never treated as a duplicate: rows a client mints
 *  locally (streaming, thinking, optimistic bubbles) have no server identity yet,
 *  and channel-replayed rows genuinely carry no `meta` at all (`ConversationLog`
 *  writes only role/content/ts/source_* for those). Declining to dedup renders a
 *  duplicate at worst; guessing would drop a real message.
 *
 *  Called from ONE chokepoint per path, placed so it dominates every branch that
 *  creates OR mutates a row — the `tool` insert, the `assistant` reconcile (which
 *  overwrites the trailing `streaming` row, so a late redelivery of an old frame
 *  would clobber a NEW segment's live content), the `user` echo reconcile, and
 *  the generic push. A guard sitting after any of those is a guard some frame
 *  slips past.
 *
 *  Scans from the tail — a redelivery is almost always the newest row — but
 *  scans the whole list, since a replayed frame can be older. */
export function isRedeliveredMessage(
  msgs: Array<{ meta?: Record<string, unknown> }>,
  meta?: Record<string, unknown>,
): boolean {
  const mid = meta?.mid
  if (typeof mid !== 'string' || !mid) return false
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].meta?.mid === mid) return true
  }
  return false
}

/** Remove duplicate messages that share the same delivery identity —
 *  `meta.mid` plus `role` plus `ts`.
 *
 *  On non-streaming channels (e.g. Weixin/iLink), the slot's turn-complete
 *  broadcast and a concurrent `refreshSlot` HTTP fetch can race — each
 *  delivering the same assistant row with the same server-minted `mid` — and
 *  the merge helpers (`mergePreservedClientTs`, `mergePreservedThinking`) do
 *  not collapse rows by `mid` because their contracts are narrower (timestamp
 *  preservation, reasoning re-injection). This final pass keeps the LAST
 *  occurrence of each identity (the freshest merge outcome) and drops earlier
 *  duplicates, making the dedup idempotent and safe on already-clean arrays.
 *
 *  Identity is deliberately mid AND role AND ts, not mid alone: the same row
 *  delivered through both doors carries an identical role and server `ts`, so
 *  the legitimate race duplicates still collapse — while a DISTINCT row that
 *  illegitimately reuses a mid (e.g. a crafted `meta.mid` in a POST /api/chat
 *  body, which `_ChatSlot.append` preserves rather than re-minting) is
 *  appended at a different time and therefore never hides an earlier
 *  legitimate transcript row. */
export function deduplicateByMid(msgs: ChatMessage[]): ChatMessage[] {
  const seen = new Set<string>()
  // Walk backwards so the LAST (newest) occurrence wins.
  const result: ChatMessage[] = []
  for (let i = msgs.length - 1; i >= 0; i--) {
    const mid = msgs[i].meta?.mid
    if (typeof mid === 'string' && mid) {
      // JSON-array key rather than a delimiter-joined template: no delimiter
      // can collide with field content, and no string literal trips the
      // zero-tolerance i18n added-lines gate on this internal identity key.
      const key = JSON.stringify([mid, msgs[i].role, msgs[i].ts ?? null])
      if (seen.has(key)) continue
      seen.add(key)
    }
    result.push(msgs[i])
  }
  result.reverse()
  return result
}

/** Tail window (rows) for a backward `sendId` scan. Shared by the echo
 *  reconcile and the response-confirm path so the two cannot drift into
 *  disagreeing about which bubbles are still addressable by their send id. */
export const RECONCILE_WINDOW = 50

/** Reconcile a server echo (carrying both `sendId` and `mid`) against the
 *  optimistic user bubble that was appended client-side at send time.
 *
 *  Scans the bounded tail for an exact `sendId` match, including past newer
 *  steers that may have been appended before this echo arrived.
 *  A matching optimistic steer can have raced onto a new turn; the ordinary
 *  user echo then also clears its provisional steer flag.
 *  On match: updates ts/meta and clears the `optimistic` flag. Keep `sendId`
 *  so a pending HTTP request can still recognize delivery if its receipt
 *  times out or the connection resets after this echo.
 *
 *  Returns `true` if reconciliation succeeded (caller should `return` to skip
 *  the push), `false` if no match was found (caller falls through to push).
 *
 *  FIX for #3898: the prior inline scan used an unconditional `break` after the
 *  first non-matching user message, preventing reconciliation of pipelined sends
 *  (user A then user B — echo for A could never reach past B). Now uses
 *  `continue` to keep scanning. */
export function reconcileOptimisticEcho(
  msgs: ChatMessage[],
  echoSendId: string,
  meta: Record<string, unknown>,
  ts?: string,
): boolean {
  const reconcileFloor = Math.max(0, msgs.length - RECONCILE_WINDOW)
  for (let i = msgs.length - 1; i >= reconcileFloor; i--) {
    const m = msgs[i]
    if (m.role !== 'user') continue
    if (m.meta?.sendId === echoSendId) {
      // Keep the rendered row's identity when the server supplies its timestamp.
      if (ts && m.ts && ts !== m.ts) {
        m.meta = { ...(m.meta || {}), clientTs: m.meta?.clientTs ?? m.ts }
      }
      if (ts) m.ts = ts
      m.meta = { ...(m.meta || {}), ...meta }
      delete (m.meta as Record<string, unknown>).optimistic
      // The echo is the delivery proof a late receipt never gave; the deadline
      // mark `markSendUnconfirmed` stamped falls with the flag.
      delete (m.meta as Record<string, unknown>).deliveryUnconfirmed
      if (!meta.steer) delete (m.meta as Record<string, unknown>).steer
      return true
    }
    // #3898 fix: continue scanning past non-matching user messages so
    // pipelined sends (multiple optimistic bubbles) can all be reconciled.
  }
  return false
}

/** Finalize the most recent live `streaming` message in place (streaming →
 *  assistant), or drop it entirely when its content is a trivial placeholder
 *  the model emits before tool calls ("...", "…", "---", ". . .", etc.).
 *  Only patterns EXCLUSIVELY composed of 2+ repeated punctuation/whitespace
 *  chars are dropped — never single characters, which could be the start of
 *  legitimate content (list markers, etc.).
 *
 *  Shared by the two segment-finalize paths (active `sseChatMessage` and
 *  background `applyMessageToArray`) AND the steer insertion paths: a mid-turn
 *  steer bubble must never be pushed BELOW a live streaming message, or the
 *  chunk reducer (which scans backwards for the last `streaming` role) keeps
 *  streaming the rest of the segment into the stranded bubble ABOVE the steer
 *  card — the "streaming marker stuck at the steer point" bug. Freezing first
 *  means pre-steer text stays above the bubble and the next chunk opens a
 *  fresh streaming message below it. */
export const finalizeTrailingStreaming = (msgs: ChatMessage[]) => {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'streaming') {
      const raw = msgs[i].content
      const isPlaceholder = !raw || (/^[\s.\-…·•–—]{2,}$/.test(raw) && /[.\-…·•–—]/.test(raw)) || raw === '…'
      if (isPlaceholder) {
        msgs.splice(i, 1)
      } else {
        msgs[i].role = 'assistant'
        msgs[i].rawText = msgs[i].content
      }
      break
    }
  }
}

/** Field-for-field equality over every `ChatMessage` field a consumer can render. */
function sameMessage(a: ChatMessage, b: ChatMessage): boolean {
  if (a === b) return true
  return a.role === b.role && a.content === b.content && a.cls === b.cls
    && a.ts === b.ts && a.rawText === b.rawText && a.kind === b.kind
    && a.variant_idx === b.variant_idx && a._toolCount === b._toolCount
    && jsonEqual(a.variants, b.variants) && jsonEqual(a.meta, b.meta)
}

/** True when `next` renders identically to `prev`, so a reducer can leave
 *  `state.messages` untouched and every consumer keeps its existing reference. */
export function sameTranscript(prev: ChatMessage[], next: ChatMessage[]): boolean {
  if (prev === next) return true
  if (prev.length !== next.length) return false
  for (let i = 0; i < prev.length; i++) if (!sameMessage(prev[i], next[i])) return false
  return true
}

/** The chunk-seq floor a slot snapshot vouches for: the `seq` the server folded
 *  onto the snapshot's trailing `streaming` row (chat_utils._prepare_messages),
 *  i.e. the newest chunk whose text that snapshot already contains. A live
 *  `chat_chunk` with a seq at or below it is a replay of text the snapshot
 *  holds and must be dropped, not appended — the duplicated leading fragment
 *  seen after a reconnect. Returns `undefined` for a snapshot without one (an
 *  older gateway, or no stream in flight), which leaves the guard as it was. */
export const snapshotChunkSeq = (messages: ChatMessage[]): number | undefined => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'streaming') return typeof m.seq === 'number' ? m.seq : undefined
  }
  return undefined
}

/** The generation a snapshot's trailing streaming row was numbered by (the
 *  `gen` the server folds beside `seq`); `undefined` for an older gateway. */
export const snapshotChunkGen = (messages: ChatMessage[]): string | undefined => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'streaming') return typeof m.gen === 'string' ? m.gen : undefined
  }
  return undefined
}

/** The seq floor to order an incoming chunk or snapshot against, given the
 *  generation it carries. Seqs are a per-slot counter that continues across
 *  turns but restarts with the gateway process, so a floor this generation
 *  cannot vouch for says nothing about it: the floor is dropped (`undefined`)
 *  rather than compared, which is what stops a pre-restart floor from
 *  swallowing the new process's early chunks.
 *
 *  A floor with NO generation (`floorGen === undefined`) counts as unvouched
 *  too, not as a match. It is what an older gateway's seq-only frames leave
 *  behind, so after an upgrade or a reconnect the first generation-stamped chunk
 *  arrives numbered from a restarted counter and sits below that stale floor —
 *  treating "no generation" as compatible dropped the new process's reply text
 *  and left every later snapshot reconciling against a floor from a dead
 *  process.
 *
 *  An incoming frame with no generation keeps the floor: that is the same older
 *  gateway still running, where seqs are the only ordering available. */
export const floorForGen = (floor: number | undefined, floorGen: string | undefined, gen: string | undefined): number | undefined =>
  gen !== undefined && gen !== floorGen ? undefined : floor

/** Raise a chunk-seq floor to what a snapshot vouches for; never lower it. A
 *  live frame may already have moved the floor past a snapshot taken earlier,
 *  and lowering it would let the frames between the two be applied again. */
export const raiseChunkSeq = (current: number | undefined, fromSnapshot: number | undefined): number | undefined => {
  if (fromSnapshot === undefined) return current
  return current === undefined || fromSnapshot > current ? fromSnapshot : current
}

/** Every identity a transcript row carries, for recognising two copies as one.
 *
 *  Server rows carry `meta.mid`, stamped once per row by the backend. A row the
 *  user just sent may not have been echoed yet, so it carries only the one-shot
 *  `meta.sendId` the send generated -- and the backend stores the client meta
 *  opaquely before stamping its own id, so the server's copy of that row carries
 *  BOTH. Returning both is what makes the pre-echo window matchable: the local
 *  copy is known only by `sendId` while the server copy is also known by `mid`,
 *  so preferring one id would compare the two rows on keys that cannot agree.
 *
 *  Prefixed so the two id spaces cannot collide. */
export function rowIdentities(m: ChatMessage): string[] {
  const meta = m.meta as Record<string, unknown> | undefined
  const ids: string[] = []
  const mid = meta?.mid
  if (typeof mid === 'string' && mid) ids.push(`mid:${mid}`)
  const sendId = meta?.sendId
  if (typeof sendId === 'string' && sendId) ids.push(`send:${sendId}`)
  return ids
}

/** Rows of `tail` that `page` does not already carry, by identity.
 *
 *  A row with NO identity is kept: dropping a local row on the strength of a
 *  guess is the failure this exists to prevent, and the same "decline, not
 *  guess" rule the warm merge's cut already follows. */
export function tailNotInPage(tail: ChatMessage[], page: ChatMessage[]): ChatMessage[] {
  const seen = new Set<string>()
  for (const m of page) for (const id of rowIdentities(m)) seen.add(id)
  return tail.filter(m => !rowIdentities(m).some(id => seen.has(id)))
}

/** Epoch ms for a transcript `ts`, or `null` when it cannot be read.
 *
 *  One transcript can carry both offset-aware and naive rows — current builds
 *  write an offset, older ones left a bare local-time value. So the raw strings
 *  order by their TEXT rather than by instant: `17:00:00+09:00` is 08:00Z, yet
 *  sorts after `12:00:00Z`. The server parses before ordering for exactly this
 *  reason, and a client-side string compare would disagree with it.
 *
 *  `null` means decline, not guess — the same rule `rowIdentities` and
 *  `tailNotInPage` follow when a row carries no identity. */
export function tsEpoch(ts: string | undefined): number | null {
  if (!ts) return null
  const ms = Date.parse(ts)
  return Number.isNaN(ms) ? null : ms
}

/** THE parser for a transcript `ts` that may be a numeric epoch-SECONDS string
 *  or an ISO string. Returns epoch MILLISECONDS, or `null` when the value
 *  cannot be read — the same "decline, not guess" contract `tsEpoch` follows.
 *
 *  This is the single spelling of "seconds-or-ISO"; callers that need another
 *  unit or a non-null sort default convert at the call site rather than
 *  re-parsing (three hand-rolled copies had already diverged on
 *  numeric-seconds input — #6004). `tsEpoch` above stays deliberately
 *  `Date.parse`-only: its prior/warm boundary callers have never accepted a
 *  numeric-seconds guess, and widening them would change merge behavior.
 *
 *  Exported for the unit test that pins this contract. */
export function transcriptTsMs(ts: string | undefined): number | null {
  if (!ts) return null
  const n = Number(ts)
  if (Number.isFinite(n)) return n * 1000
  const ms = Date.parse(ts)
  return Number.isNaN(ms) ? null : ms
}

/** Occurrences of each usable `meta.mid` in a row list.
 *
 *  `meta` on an inbound message is CALLER-supplied and an id is minted only when
 *  one is ABSENT, so a client can post the same `mid` twice and an id is NOT a
 *  unique key. Counting is what lets a caller tell "this id names the row I mean"
 *  from "this id names SOME row" -- the distinction a membership test cannot make.
 */
export function midOccurrences(rows: Array<{ meta?: Record<string, unknown> }>): Map<string, number> {
  const counts = new Map<string, number>()
  for (const row of rows) {
    const mid = row.meta?.mid
    if (typeof mid === 'string' && mid.length > 0) counts.set(mid, (counts.get(mid) ?? 0) + 1)
  }
  return counts
}

/** Does this id name exactly ONE row on each side, and are those two rows the same row?
 *
 *  The one identity rule every bounded-page comparison in the chat store runs on. An id
 *  that names two rows is a predicate, not a reference: acting on it cuts or
 *  substitutes at whichever occurrence happened to be found first.
 *
 *  `requireTs` exists because DECLINING costs different things at the two call
 *  sites, and the safe default is therefore different:
 *
 *    - `refreshSlot`'s pre-fulfil check pays ONE ROUND TRIP for a decline (it
 *      refetches unbounded), so it can afford the strict form and passes
 *      `requireTs: true`: both rows must carry a `ts` and it must match. That is
 *      what rules out two genuinely different rows sharing a caller-supplied id.
 *    - `olderHeadAbovePage` pays the KEPT HEAD for a decline -- the scrollback it
 *      exists to protect. Demanding a `ts` there would drop the head for legacy
 *      rows that carry none, turning a guard into the very data loss it guards
 *      against. So the default only requires that the two rows do not CONTRADICT
 *      each other: a missing `ts` on either side is not evidence of a mismatch.
 */
export function idAnchorsOneRow(
  id: unknown,
  view: Array<{ ts?: string; meta?: Record<string, unknown> }>,
  page: Array<{ ts?: string; meta?: Record<string, unknown> }>,
  viewCounts: Map<string, number>,
  pageCounts: Map<string, number>,
  opts?: { requireTs?: boolean },
): boolean {
  if (typeof id !== 'string' || id.length === 0) return false
  if (viewCounts.get(id) !== 1 || pageCounts.get(id) !== 1) return false
  const inView = view.find(m => m.meta?.mid === id)
  const inPage = page.find(m => m.meta?.mid === id)
  if (!inView || !inPage) return false
  const bothTimestamped = typeof inView.ts === 'string' && inView.ts.length > 0
    && typeof inPage.ts === 'string' && inPage.ts.length > 0
  if (opts?.requireTs && !bothTimestamped) return false
  return bothTimestamped ? inView.ts === inPage.ts : true
}

/** The prior rows that sit ABOVE a bounded page's first row, plus the index the
 *  cut fell at (-1 when there is none).
 *
 *  A bounded page replacing the array wholesale deletes scrollback under a
 *  reader, so every reducer consuming a `fetchSlotDetail` page routes its cut
 *  through here rather than re-deriving it. Re-deriving is exactly how the two
 *  paths diverged: `warmSlotCache` kept the head while `switchSlot` discarded
 *  it, collapsing a paged-in window to the newest page on switch-away-and-back.
 *
 *  Identity is `meta.mid`, and it must name exactly ONE row on each side --
 *  `idAnchorsOneRow`, the same invariant `refreshSlot` and the slot-detail
 *  handler's overlay run on. A bare `findIndex` cut at the FIRST row carrying the
 *  id, and `meta.mid` is caller-supplied (an id is minted only when absent), so a
 *  client posting one twice made the cut land on the wrong occurrence and drop the
 *  history above it. An ambiguous id therefore declines exactly like a missing one:
 *  `cutIdx -1`, empty head, which every caller already handles.
 *
 *  No mid means decline (an empty head), never guess. Callers hold `thinking` rows
 *  out: reasoning is broadcast-only, carries no identity, and is re-placed by
 *  `mergePreservedThinking` afterwards.
 */
export function olderHeadAbovePage(
  prior: ChatMessage[],
  page: ChatMessage[],
): { cutIdx: number; olderHead: ChatMessage[] } {
  const pageOldestMid = page[0]?.meta?.mid
  const anchored = idAnchorsOneRow(
    pageOldestMid, prior, page, midOccurrences(prior), midOccurrences(page),
  )
  const cutIdx = anchored
    ? prior.findIndex(m => m.meta?.mid === pageOldestMid)
    : -1
  return { cutIdx, olderHead: cutIdx > 0 ? prior.slice(0, cutIdx) : [] }
}

/** Roles the server never writes to history. `permission` is in the backend's own
 *  `_TRANSIENT_ROLES`; `queued`/`streaming`/`thinking` exist only in this client.
 *  `error`/`mcp_oauth` are deliberately absent -- those ARE persisted.
 *
 *  One set, because two consumers ask the same question for opposite reasons and a
 *  second copy would let them drift: `serverRowCount` counts durable rows to shift
 *  a paging OFFSET, and `hasUnidentifiedDurableRow` looks for a durable row the
 *  bound cannot see. A role missing from one copy would silently mis-shift a cursor
 *  in the first and silently drop scrollback in the second. */
const CLIENT_ONLY_ROLES: ReadonlySet<string> = new Set(['queued', 'streaming', 'thinking', 'permission'])

/** Does this row survive in the server's transcript?
 *
 *  Typed on the ROLE alone rather than on `ChatMessage`, so the coverage comparison
 *  below can ask the same question of its own narrower row shape. One predicate is the
 *  point: a second copy of this list is how a caller ends up agreeing with three of the
 *  four roles. A row carrying no role at all reads as durable, which is the direction
 *  that keeps a genuine hole observable. */
export function isDurableRow(m: { role?: string }): boolean {
  return !CLIENT_ONLY_ROLES.has(m.role ?? '')
}

/** How many rows of a kept older head came from SERVER history, for shifting the
 *  paging cursor. The cursor is a row OFFSET, so client-only rows must not count
 *  toward it. Callers already strip `thinking`, so including it in the shared set
 *  costs nothing here and keeps one definition of durable. */
export function serverRowCount(rows: ChatMessage[]): number {
  return rows.filter(isDurableRow).length
}

/** Is there a row the server PERSISTS but that carries no `meta.mid`?
 *
 *  Such a row is invisible to a `mid`-counted bound and unreachable to a
 *  `mid`-keyed cut: it cannot be counted into the limit, and it cannot be anchored
 *  into a kept head. It is nonetheless on screen. Legacy history written before
 *  `mid` existed is the real case. */
export function hasUnidentifiedDurableRow(rows: ChatMessage[]): boolean {
  return rows.some(m => isDurableRow(m) && !(typeof m.meta?.mid === 'string' && m.meta.mid.length > 0))
}

/** Carry the client-stamped `meta.clientTs` from the current messages onto the
 *  server copies returned by a slot-detail reload (the refreshSlot fired on
 *  chat_done). A message STREAMED this session is born with only
 *  `meta.clientTs` (a minted bornKey, no server `ts`); the reloaded server copy
 *  has an authoritative `ts` but NO `clientTs`. The renderer keys virtual rows
 *  by `clientTs ?? ts`, so without this the row's key flips bornKey → serverTs
 *  on the reload, remounting the row and DROPPING its measured height in the
 *  virtualizer's HeightCache — a visible scroll jump on every turn (the "reload
 *  the whole history, scroll bar keeps moving up, can't reach the bottom"
 *  report).
 *
 *  Matching is two-pass so a duplicate-content row can never steal a live
 *  identity (forward-first content matching would let an OLDER duplicate
 *  consume the newest stamp, flipping two rows' keys instead of zero):
 *    1. Durable identities — a stamp that already carries a server `ts` (it was
 *       reloaded before) matches its incoming copy by EXACT `ts`. Collision-proof.
 *    2. Freshly-streamed identities — a stamp with NO `ts` (born this session,
 *       not yet reloaded) has nothing to match on, but its server copy is the
 *       NEWEST message of that role, so pair newest-first: walk the ts-less
 *       stamps from the transcript tail and scan `incoming` in REVERSE for the
 *       first unused (normalized-role, trimmed-content) match. 'streaming' is
 *       normalized to 'assistant' since finalization flips the role.
 *  Returns `incoming` unchanged (reference-equal) when nothing needs carrying. */
export function mergePreservedClientTs<M extends { role: string; content: string; ts?: string; meta?: Record<string, unknown> }>(
  existing: M[],
  incoming: M[],
): M[] {
  const norm = (r: string): string => (r === 'streaming' ? 'assistant' : r)
  const stamped = existing.filter(m => typeof m.meta?.clientTs === 'string')
  if (!stamped.length) return incoming
  const carried = new Array<string | undefined>(incoming.length)
  const usedIncoming = new Set<number>()
  let changed = false

  // Pass 1: durable (already-reloaded) stamps — same server `ts` AND matching
  // (normalized-role, trimmed-content). A `ts` is NOT unique (a coarse OS clock
  // can stamp two fast tool-delimited rows with the same tick) and is NOT
  // role-specific (a tool row can share the assistant's tick), so keying on ts
  // alone would (a) collapse two distinct same-ts identities or (b) hand a
  // stamp to the wrong row (e.g. an unstamped tool row ahead of the stamped
  // assistant). Bucket the stamps per ts and consume the first bucket entry
  // that also matches role+content, so each identity lands on its own row.
  const byTs = new Map<string, { ct: string; role: string; content: string }[]>()
  for (const s of stamped) {
    if (typeof s.ts === 'string' && s.ts) {
      const e = { ct: s.meta!.clientTs as string, role: norm(s.role), content: s.content.trimEnd() }
      const q = byTs.get(s.ts)
      if (q) q.push(e)
      else byTs.set(s.ts, [e])
    }
  }
  if (byTs.size) {
    for (let i = 0; i < incoming.length; i++) {
      const item = incoming[i]
      if (item.meta?.clientTs) continue
      if (!(typeof item.ts === 'string' && item.ts)) continue
      const q = byTs.get(item.ts)
      if (!q || !q.length) continue
      const irole = norm(item.role)
      const icontent = item.content.trimEnd()
      const qi = q.findIndex(e => e.role === irole && e.content === icontent)
      if (qi >= 0) { carried[i] = q[qi].ct; q.splice(qi, 1); usedIncoming.add(i); changed = true }
    }
  }

  // Pass 2: freshly-streamed (ts-less) stamps — pair newest-first from the tail.
  // Exclude still-'streaming' stamps (a partial in-progress row has no server
  // copy yet, so a content match could only hit an older duplicate) and
  // 'thinking' stamps (client-only, never present in the server payload — and
  // re-inserted separately by mergePreservedThinking), which also keeps this
  // pass from scanning one dead thinking stamp per turn.
  const tsLess = stamped.filter(
    s => !(typeof s.ts === 'string' && s.ts) && s.role !== 'streaming' && s.role !== 'thinking',
  )
  for (let p = tsLess.length - 1; p >= 0; p--) {
    const s = tsLess[p]
    for (let i = incoming.length - 1; i >= 0; i--) {
      if (usedIncoming.has(i)) continue
      const item = incoming[i]
      if (item.meta?.clientTs) continue
      if (norm(s.role) === norm(item.role) && s.content.trimEnd() === item.content.trimEnd()) {
        carried[i] = s.meta!.clientTs as string
        usedIncoming.add(i)
        changed = true
        break
      }
    }
  }

  if (!changed) return incoming
  return incoming.map((item, i) =>
    carried[i] !== undefined
      ? { ...item, meta: { ...(item.meta || {}), clientTs: carried[i] as string } }
      : item,
  )
}

/** Pure calculations over one transcript array: message identity (the durable
 *  client id, the server `meta.mid`, the one-shot `sendId`), redelivery and
 *  duplicate detection, echo reconciliation, streaming finalization, the
 *  chunk-seq floor a snapshot vouches for, and the bounded-page identity rules
 *  every slot-detail reducer cuts and merges by. Nothing here touches chat
 *  state; callers pass the arrays in. */
import type { ChatMessage } from '../../types'
import { isStopEvent } from '../../lib/stopEvent'
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

/** Carry a settled `mcp_oauth` banner forward over a stale snapshot copy that
 *  would revive its retired Authorize link (#20833).
 *
 *  The backend upserts an `mcp_oauth` banner in place by its server `meta.mid`,
 *  keeping the same `role` and `ts`: a newer request supersedes it
 *  (`_supersede_open_mcp_oauth_banners` drops `oauth_url`, sets `superseded`),
 *  and the read-time gate expires a dead-child one (`oauth_url: ""`, `expired`).
 *  A reconnect / slot switch then fetches a BOUNDED snapshot taken BEFORE that
 *  transition, still carrying the OPEN copy (a live `oauth_url`, no terminal
 *  flag) under the SAME `(mid, role, ts)`. The slot-detail merge appends that
 *  stale page copy AFTER the live settled row, so `deduplicateByMid` — which
 *  keeps the LAST occurrence — revives the dead Authorize button, which walks
 *  the user to a loopback callback port that can redeem nothing.
 *
 *  This carries each settled prior banner's content/cls/meta forward onto any
 *  merged copy of the same mid that is still open, BEFORE `deduplicateByMid`
 *  collapses the pair, so the surviving row keeps the terminal state the user
 *  already saw. Keyed by `mid` only — the backend's own upsert key.
 *
 *  A sibling of `#20818`'s stop-event rule applied at the same two merge sites
 *  (`slotRefresh.ts`, `slotSwitch.ts`); deliberately scoped to `mcp_oauth` and
 *  independent of it. Returns `merged` unchanged (same reference) when nothing
 *  is carried, so a caller can skip the write.
 */
export function preserveSettledMcpOauth(existing: ChatMessage[], merged: ChatMessage[]): ChatMessage[] {
  const oauthMid = (m: ChatMessage): string | undefined => {
    if (m.role !== 'mcp_oauth') return undefined
    const mid = m.meta?.mid
    return typeof mid === 'string' && mid ? mid : undefined
  }
  // Terminal states the backend stamps on a retired banner. An open banner
  // carries none of them (and still carries a live `oauth_url`).
  const isSettled = (m: ChatMessage): boolean => {
    const meta = m.meta as { superseded?: unknown; expired?: unknown; completed?: unknown; failed?: unknown } | undefined
    return !!(meta?.superseded || meta?.expired || meta?.completed || meta?.failed)
  }

  const settledPrior = new Map<string, ChatMessage>()
  for (const m of existing) {
    const id = oauthMid(m)
    if (id && isSettled(m)) settledPrior.set(id, m)
  }
  if (!settledPrior.size) return merged

  let changed = false
  const result: ChatMessage[] = []
  for (const m of merged) {
    const id = oauthMid(m)
    if (id !== undefined && !isSettled(m) && settledPrior.has(id)) {
      // Carry the settled prior row's rendered state forward in place. The
      // prior meta is AUTHORITATIVE, not merged under the open copy's: the
      // supersede/expire transition DROPS `oauth_url`, so spreading the open
      // copy's meta on top would keep its dead link and only add the terminal
      // flag — a hybrid row that still renders the revived Authorize button.
      // Identity (`mid`, `role`, `ts`) already matches, so replacing content /
      // cls / meta wholesale keeps the row the same row for `deduplicateByMid`.
      const prior = settledPrior.get(id)!
      result.push({ ...m, content: prior.content, cls: prior.cls, meta: { ...(prior.meta || {}) } })
      changed = true
      continue
    }
    result.push(m)
  }
  return changed ? result : merged
}

/** Carry a settled `card` or `steer` row forward over a stale snapshot copy
 *  that would REGRESS it to an earlier lifecycle state (#20833).
 *
 *  Besides `stop_event` (#20818) and `mcp_oauth` (#20904), two more durable
 *  row types are upserted IN PLACE by the backend keeping the same
 *  `(mid, role, ts)`, so the same stale-snapshot merge can revert them:
 *
 *   - a `card` row (`chat_cards._patch_row` -> `slot.update_message`): its
 *     `meta.card.status` advances a LIVE status to a terminal one. A snapshot
 *     captured while it was still live reverts a finished card to "running".
 *   - a steer row (`role: "user"`, `meta.steer`): `steer_queue`'s
 *     `_mark_steer_row_state` -> `slot.update_message` advances
 *     `meta.steerState` `written` -> `consumed`/`requeued`. A snapshot captured
 *     while it was still `written` revives the "pending steer" badge on a steer
 *     the turn already took.
 *
 *  Both carry a server-minted `meta.mid` that the slot-detail snapshot keeps
 *  (its meta is rebuilt from the JSON `cls`, which holds `mid` for every role),
 *  so the live settled copy and the stale page copy share `(mid, role, ts)` and
 *  `deduplicateByMid` keeps the LAST (positionally newest) occurrence — the
 *  stale page copy, since the merge seats `[...olderHead(live), ...page]`.
 *
 *  Like `preserveSettledMcpOauth` (and unlike the stop rule), this runs BEFORE
 *  `deduplicateByMid`: it carries the settled prior row's `content`/`cls`/`meta`
 *  onto any still-earlier merged copy of the same `(role, mid, ts)`, so the
 *  surviving dedup row keeps the terminal state the user already saw. Keyed by
 *  `(role, mid, ts)` — the full row identity `deduplicateByMid` uses, so only
 *  true copies of one row overwrite each other (not two distinct rows that
 *  happen to reuse a `mid`). A row with no settled prior
 *  copy, and every other role, pass through untouched; returns `merged`
 *  unchanged (same reference) when nothing is carried, so a caller can skip the
 *  write.
 *
 *  FAIL-SAFE on which statuses anchor a carry-forward: a card status is only a
 *  carry-forward anchor if it is terminal AND cannot legally move back to a
 *  live status. TWO finished statuses can, so both are EXCLUDED, or the fix
 *  would hide the re-opened row behind the cached terminal copy:
 *    - `failed` — `change_cards.py` documents `failed --retry--> pending`.
 *    - `completed` — `guide_runs.py replay()` ("Show again") takes a completed
 *      guide back to `offered`, patching the same transcript row in place.
 *  Every remaining finished status (`applied`/`partial` -> only `undone`, which
 *  is not live; `cancelled`; `expired`; `undone`) has no edge back to a live
 *  status and anchors safely. A status in neither the live nor the anchor set
 *  (an unknown/future surface) is only ever carried OVER, never carried
 *  forward — we never fabricate a settle we cannot name. The sets below are the
 *  backend's own `_CHANGE_*`/`_GUIDE_*` sets (`chat_cards.py`), minus the two
 *  reversible statuses.
 *
 *  Deliberately scoped to these two in-place-upsert types; it is the sibling of
 *  the stop/mcp_oauth rules, not a general merge rewrite. */
export function preserveSettledUpsertRows(existing: ChatMessage[], merged: ChatMessage[]): ChatMessage[] {
  // LIVE statuses a card row may hold — the backend's `_CHANGE_LIVE` +
  // `_GUIDE_LIVE` (`chat_cards.py`). A merged row in this set is "earlier" and
  // may be carried over by a settled prior copy of the same id.
  const CARD_LIVE = new Set(['pending', 'applying', 'offered', 'active', 'target_missing'])
  // Carry-forward ANCHORS: terminal statuses with NO legal edge back to a live
  // status. This is `_CHANGE_FINISHED` + `_GUIDE_FINISHED` minus the two that
  // can legally return to live, which must NOT be carried forward (a fresh
  // snapshot showing the re-opened row would otherwise be reverted):
  //   - `failed`: `change_cards.py` documents `failed --retry--> pending`.
  //   - `completed`: `guide_runs.py replay()` ("Show again") takes a completed
  //     guide back to `offered`, patching the same transcript row in place.
  // The remaining finished statuses (`applied`/`partial` -> only `undone`,
  // which is not a live status; `cancelled`/`expired`; `undone`) have no edge
  // back to a live status, so they anchor safely.
  const CARD_ANCHOR = new Set(['applied', 'partial', 'cancelled', 'expired', 'undone'])
  // A steer row advances `written` -> `consumed`/`requeued` and never back
  // (`steer_queue.py`: the only two transitions; a requeued steer runs later as
  // its own NEW row, not a rewrite of this one), so both are safe anchors.
  const STEER_SETTLED = new Set(['consumed', 'requeued'])

  // The `(role, mid, ts)` upsert key for a covered row, or undefined if the row
  // is not one this pass guards. Steer rows ride the `user` role, so they are
  // distinguished by `meta.steer` rather than role alone.
  //
  // `ts` is part of the key on purpose, matching `deduplicateByMid`'s own
  // `(mid, role, ts)`: the backend's in-place upsert keeps `ts` constant, so the
  // settled prior and the stale copy of ONE row share it, while two DISTINCT
  // rows that reuse a `mid` (a crafted `meta.mid` in a POST /api/chat body, which
  // `_ChatSlot.append` preserves rather than re-minting) are appended at
  // different times and so get different `ts`. Keying on `(role, mid)` alone
  // would let such a distinct row's content be overwritten by an unrelated
  // same-mid row that `deduplicateByMid` then keeps — hiding the second row's
  // text. Keyed to the full row identity, only true copies of one row can
  // overwrite each other.
  const upsertKey = (m: ChatMessage): string | undefined => {
    const mid = m.meta?.mid
    if (typeof mid !== 'string' || !mid) return undefined
    // JSON-array key rather than a delimiter-joined string template, for the
    // same two reasons `deduplicateByMid` uses one: no delimiter can collide
    // with field content, and no string literal trips the zero-tolerance i18n
    // added-lines gate on this internal identity key.
    if (m.role === 'card') return JSON.stringify(['card', mid, m.ts ?? null])
    if (m.role === 'user' && (m.meta as { steer?: unknown } | undefined)?.steer) return JSON.stringify(['steer', mid, m.ts ?? null])
    return undefined
  }

  // Whether a covered row is a carry-forward ANCHOR: a terminal status that
  // cannot legally move back to a live one. For a card that is the CARD_ANCHOR
  // set (finished minus the retryable `failed`); a live, `failed`, or unknown
  // status does not anchor a carry-forward.
  const isSettled = (m: ChatMessage): boolean => {
    if (m.role === 'card') {
      const status = (m.meta as { card?: { status?: unknown } } | undefined)?.card?.status
      return typeof status === 'string' && CARD_ANCHOR.has(status)
    }
    const steerState = (m.meta as { steerState?: unknown } | undefined)?.steerState
    return typeof steerState === 'string' && STEER_SETTLED.has(steerState)
  }

  // A merged row is a carry-OVER candidate only when it is genuinely EARLIER in
  // the lifecycle, not merely "not an anchor": a card whose status is unknown
  // to us (neither live nor anchor), or a retryable `failed`, is left exactly
  // as the page sent it rather than overwritten from a prior anchored row.
  const isEarlier = (m: ChatMessage): boolean => {
    if (m.role === 'card') {
      const status = (m.meta as { card?: { status?: unknown } } | undefined)?.card?.status
      return typeof status === 'string' && CARD_LIVE.has(status)
    }
    const steerState = (m.meta as { steerState?: unknown } | undefined)?.steerState
    return steerState === 'written'
  }

  const settledPrior = new Map<string, ChatMessage>()
  for (const m of existing) {
    const key = upsertKey(m)
    if (key && isSettled(m)) settledPrior.set(key, m)
  }
  if (!settledPrior.size) return merged

  let changed = false
  const result: ChatMessage[] = []
  for (const m of merged) {
    const key = upsertKey(m)
    if (key !== undefined && isEarlier(m) && settledPrior.has(key)) {
      // Carry the settled prior row's rendered state forward in place. Identity
      // (`role`, `mid`, `ts`) already matches, so replacing content/cls/meta
      // wholesale keeps the row the same row for `deduplicateByMid`. The prior
      // meta is authoritative: a partial merge could leave a hybrid row still
      // advertising the earlier state.
      const prior = settledPrior.get(key)!
      result.push({ ...m, content: prior.content, cls: prior.cls, meta: { ...(prior.meta || {}) } })
      changed = true
      continue
    }
    result.push(m)
  }
  return changed ? result : merged
}

/** Keep a merged transcript's stop_event cards from REGRESSING to `stopping`.
 *
 *  The backend upserts a stop card BY `meta.id`: the press opens it at
 *  `stopping`, then the same id is re-broadcast settled (`stopped`,
 *  `stop_failed_reset`, `stop_declined_compacting`). The live frame appliers
 *  (`applyActiveFrame`) honour that by replacing in place on id — but the
 *  switch/refresh MERGE does not. It rebuilds the transcript from the server
 *  snapshot (`mergePreservedClientTs` returns the incoming rows as the base)
 *  and dedups only by `meta.mid`, while a server snapshot's stop_event row
 *  carries no `mid` (`_prepare_messages` keeps `mid` for permission rows only).
 *  So a snapshot captured while the stop was still `stopping`, landing after
 *  the tab already settled the card, replaces the settled live row with its
 *  stale `stopping` copy — leaving the card stuck pulsing at `stopping` until a
 *  reload (#20728).
 *
 *  The transition is monotonic (a stop never un-settles), so the SETTLED state
 *  always wins. For each merged stop_event row still at `stopping`, look up the
 *  same `meta.id` in the PRIOR transcript (`existing`, the live state the tab
 *  already held): if that copy is settled, carry its settled `meta`/content
 *  onto the merged row. A row with no settled prior copy, and every non-stop
 *  row, pass through untouched.
 *
 *  Scope is deliberately the stale-snapshot regression only: a backend that
 *  recorded two SEPARATE rows for one stop id is a backend-writer concern, not
 *  one this merge pass papers over. */
export function preserveSettledStopState(existing: ChatMessage[], merged: ChatMessage[]): ChatMessage[] {
  // Reuse the one stop-event predicate (`lib/stopEvent.ts`) rather than
  // re-checking half of it; the id is the backend's upsert key.
  const stopId = (m: ChatMessage): string | undefined => {
    if (!isStopEvent(m)) return undefined
    const id = (m.meta as { id?: unknown } | undefined)?.id
    return typeof id === 'string' && id ? id : undefined
  }
  const isStopping = (m: ChatMessage): boolean =>
    (m.meta as { state?: string } | undefined)?.state === 'stopping'

  // The settled prior row for each stop id the tab already held.
  const settledPrior = new Map<string, ChatMessage>()
  for (const m of existing) {
    const id = stopId(m)
    if (id && !isStopping(m)) settledPrior.set(id, m)
  }
  if (!settledPrior.size) return merged

  let changed = false
  const result: ChatMessage[] = []
  for (const m of merged) {
    const id = stopId(m)
    if (id !== undefined && isStopping(m) && settledPrior.has(id)) {
      // Carry the settled prior row's state forward in place.
      const prior = settledPrior.get(id)!
      result.push({ ...m, content: prior.content, cls: prior.cls, meta: { ...(m.meta || {}), ...(prior.meta || {}) } })
      changed = true
      continue
    }
    result.push(m)
  }
  return changed ? result : merged
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
    if (m.role === 'queued' && m.meta?.responsiveSend && m.meta?.sendId === echoSendId) {
      // A crewmate send that waits behind a running turn has its row written
      // at send time (responsive_chat.write_row_at_send), after its queue entry
      // already took the bubble's place. The row takes that same place and key,
      // so the message neither moves nor is re-keyed; its queue entry, still
      // listed until the drain, draws nothing beside it (withQueuedAsBubbles).
      const { optimistic: _o, deliveryUnconfirmed: _d, responsiveSend: _r, queueId: _q, ...kept } = m.meta
      msgs[i] = {
        role: 'user', content: m.content, cls: 'msg msg-u', ts: ts ?? m.ts,
        meta: { ...kept, ...meta, clientTs: (m.meta.clientTs as string | undefined) || m.ts },
      }
      return true
    }
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
const finalizeStreamingAt = (msgs: ChatMessage[], index: number) => {
  const raw = msgs[index].content
  const isPlaceholder = !raw || (/^[\s.\-…·•–—]{2,}$/.test(raw) && /[.\-…·•–—]/.test(raw)) || raw === '…'
  if (isPlaceholder) {
    msgs.splice(index, 1)
  } else {
    msgs[index].role = 'assistant'
    msgs[index].rawText = msgs[index].content
  }
}

export const finalizeTrailingStreaming = (msgs: ChatMessage[]) => {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'streaming') {
      finalizeStreamingAt(msgs, i)
      break
    }
  }
}

/** Finalize every live segment in a copied stale history page. A stop card can
 * split one reply into multiple folded streaming rows, and none may remain as
 * an append target for a successor turn. */
export const finalizeAllStreaming = (msgs: ChatMessage[]) => {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'streaming') finalizeStreamingAt(msgs, i)
  }
}

/** Finalize the live segments a stale history page contributed to `merged`
 * (the page's own row objects), each on a copy, and leave every other row as
 * it is: a newer local reply the merge kept beside the page is not the page's
 * to finalize. Returns a new array; `page` and its rows are not written. */
export const finalizePageStreaming = (merged: ChatMessage[], page: readonly ChatMessage[]): ChatMessage[] => {
  const fromPage = new Set(page)
  const out = merged.slice()
  for (let i = out.length - 1; i >= 0; i--) {
    if (out[i].role !== 'streaming' || !fromPage.has(out[i])) continue
    out[i] = { ...out[i] }
    finalizeStreamingAt(out, i)
  }
  return out
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

/** `next`, with each row that is structurally equal to a row of `prior` replaced
 *  by that prior row, or `prior` itself when every row matches in order.
 *
 *  A reducer that rebuilds a transcript from a fetched page gives every row a
 *  new object even when the page repeats what the store already holds. The turn
 *  grouper and the memoised row components compare rows by reference, so that
 *  rebuild re-renders every mounted turn. Handing back the prior objects keeps
 *  those bail-outs.
 *
 *  Equality is `jsonEqual` over the whole row, not `sameMessage`'s field list:
 *  a reused row stands in for the new one, so a field the list does not name
 *  (`seq`, `gen`, the decision strips) must match too. Candidates pair by
 *  `meta.mid`, else by role and ts, and each prior row is used at most once, so
 *  the result never holds one object twice.
 *
 *  `prior` must be plain store rows (the reducer's `original()` of the array),
 *  and nothing may mutate the result's rows in place afterwards: a reused row
 *  is frozen store state. */
export function reuseUnchangedRows(prior: readonly ChatMessage[], next: ChatMessage[]): ChatMessage[] {
  if (prior === next || prior.length === 0) return next
  // A JSON-array key, as `deduplicateByMid` uses: a mid key and a role+ts key
  // differ in length, so they cannot collide.
  const pairKey = (m: ChatMessage): string => {
    const mid = m.meta?.mid
    return typeof mid === 'string' && mid ? JSON.stringify([mid]) : JSON.stringify([m.role, m.ts ?? null])
  }
  const candidates = new Map<string, ChatMessage[]>()
  for (const m of prior) {
    const k = pairKey(m)
    const list = candidates.get(k)
    if (list) list.push(m)
    else candidates.set(k, [m])
  }
  let allPrior = prior.length === next.length
  const out = next.map((m, i) => {
    const list = candidates.get(pairKey(m))
    const at = list ? list.findIndex(p => jsonEqual(p, m)) : -1
    if (!list || at < 0) {
      allPrior = false
      return m
    }
    const [reused] = list.splice(at, 1)
    if (reused !== prior[i]) allPrior = false
    return reused
  })
  return allPrior ? (prior as ChatMessage[]) : out
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
 *      walks the window older), so it can afford the strict form and passes
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

/** Roles the server never writes to history. `permission` and `elicitation` are in
 *  the backend's own `_TRANSIENT_ROLES`; `queued`/`streaming`/`thinking` exist only
 *  in this client.
 *  `error`/`mcp_oauth` are deliberately absent -- those ARE persisted.
 *
 *  One set, because two consumers ask the same question for opposite reasons and a
 *  second copy would let them drift: `serverRowCount` counts durable rows to shift
 *  a paging OFFSET, and `hasUnidentifiedDurableRow` looks for a durable row the
 *  bound cannot see. A role missing from one copy would silently mis-shift a cursor
 *  in the first and silently drop scrollback in the second. */
const CLIENT_ONLY_ROLES: ReadonlySet<string> = new Set(['queued', 'streaming', 'thinking', 'permission', 'elicitation'])

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

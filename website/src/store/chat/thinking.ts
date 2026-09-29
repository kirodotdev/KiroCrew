/** Client-only reasoning (`thinking`) rows across a server refresh. The backend
 *  never persists reasoning, so every reducer that replaces a transcript from
 *  the server re-seats the blocks it can anchor and parks the rest until their
 *  anchor pages in. */
import { transcriptTsMs } from './transcript'

/**
 * True when a `user` row ends the turn before it.
 *
 * A steered message is injected INTO the running turn, so a CONFIRMED steer is
 * not a boundary. An OPTIMISTIC steer bubble (`meta.optimistic`, set at dispatch
 * and cleared when the server's `steer_push` echo reconciles it) IS treated as a
 * boundary, because it may not be a steer at all: the backend's steer branch is
 * gated on `slot.running or slot._in_stage_execution`, so text sent while
 * `chat_done` is still in flight takes the NEW TURN path instead, and no echo
 * ever arrives to clear the flag. Exempting that row would splice the new turn's
 * reasoning onto the previous turn's block — corrupting content rather than
 * merely misplacing it.
 *
 * `selectSlotPendingApproval`'s scan deliberately does NOT use this: it exempts
 * every steer row, optimistic included, because the distinction can only hide or
 * show an approval bar there, never corrupt one.
 */
export const isTurnBoundaryUser = (m: { role: string; meta?: Record<string, unknown> }): boolean =>
  m.role === 'user' && !(m.meta?.steer && !m.meta?.optimistic)

/** Rows a frame appends BELOW the turn's body rather than as part of it: an
 *  approval request, a queued bubble, an error card, an OAuth banner, a stop
 *  event. They are not turn progress, so they must not close a reasoning burst
 *  the model is still emitting.
 *
 *  The list is deliberately a DENY list, not an allow list of progress roles:
 *  anything unlisted counts as progress, so a role added later splits one burst
 *  into two (cosmetic) instead of merging two bursts into one — the defect the
 *  per-burst accumulation exists to prevent. It gates only the extend-vs-open
 *  decision, never a row's position. */
const OUT_OF_BAND_ROLES = new Set(['permission', 'queued', 'error', 'mcp_oauth'])
export const isOutOfBandRow = (m: { role: string; kind?: string }): boolean =>
  OUT_OF_BAND_ROLES.has(m.role) || m.kind === 'stop_event'

/** What a preserved reasoning block re-attaches to on the server-refreshed list:
 *  a tool call addressed by its server-minted id, or a run of answer text
 *  addressed by its content PLUS — when the anchor row carried them — its
 *  server `ts` and its row `mid`. Text alone is not an identity: two turns
 *  can produce byte-identical answers ("Done."), and a text-only match lets
 *  the OLDER block steal the newer answer row while the newer block is
 *  dropped as covered. A ts-carrying anchor therefore matches only the row
 *  with the same server `ts`; a ts-less anchor (a freshly streamed answer
 *  not yet reloaded) falls back to text-only matching — and is
 *  `confirmed=false` anyway, so a miss can never drop it. A recorded `mid`
 *  separates a regenerated answer from the text it superseded. */
export type ThinkingAnchor =
  | { tool: string; text?: undefined; ts?: undefined; mid?: undefined }
  | { tool?: undefined; text: string; ts?: string; mid?: string }

/** The server-minted row id, or undefined for a row the client minted locally. */
const rowMid = (m: { meta?: Record<string, unknown> }): string | undefined =>
  typeof m.meta?.mid === 'string' && m.meta.mid ? m.meta.mid : undefined

/** A regenerated answer repeats the superseded text at the same ordinal, so only the
 *  row id separates them -- but a locally-minted row has none, so a recorded id may
 *  refute a match and must never be required for one. */
const anchorMidOk = (a: ThinkingAnchor, row: { meta?: Record<string, unknown> }): boolean => {
  if (a.tool !== undefined || a.mid === undefined) return true
  const m = rowMid(row)
  return m === undefined || m === a.mid
}

/** A reasoning block waiting for its anchor. `occ` is which occurrence of a repeated
 *  answer text it belongs to; `occTotal` is how many there were when that was measured,
 *  so a list that has since gained more is detectable rather than silently mismatched.
 *  Both absent on a record parked by a build before they existed. */
export type ParkedThinking<M> = { msg: M; anchor: ThinkingAnchor; occ?: number; occTotal?: number }

/** Re-insert client-only reasoning (`thinking`) messages into a server-refreshed
 *  message list. The backend never persists reasoning, so a refresh (e.g. the
 *  one fired on chat_done) would otherwise drop the thinking block the instant a
 *  turn finishes. Each preserved block is anchored to the first row that followed
 *  it in the old list and re-inserted just before that row again. Returns
 *  `incoming` unchanged (reference-equal) when there is nothing to preserve.
 *
 *  A block with NO recorded anchor because nothing followed it in the old list
 *  (the live turn's in-flight reasoning) is appended at the tail: the tail IS
 *  its position. A block whose anchor scan was CUT SHORT by a turn-boundary
 *  user row (a turn stopped mid-reasoning, or one that emitted no tool call
 *  and no answer text) is also anchorless, but its turn is OVER — it is kept
 *  at the tail only while the pure server page does not cover that boundary
 *  row; once it does, the block is dropped like a covered anchored miss
 *  (#5815), since keeping it stranded one permanent chip per stopped turn
 *  below unrelated newer turns, re-appended on every refresh. A block whose
 *  anchor MISSES its lookup is dropped or kept by where the anchor sits
 *  relative to the region the PURE server page (`coverageSource`) actually
 *  covers:
 *
 *  - **Inside the covered region** (at or before the last `existing` row that
 *    `incoming` recognizably contains), with a server-confirmed anchor (a
 *    server-minted tool id, or answer text carrying a server `ts`): the
 *    snapshot covers that span of history yet does not contain the anchor —
 *    the block's position is gone (a bounded page: `switchSlot` on an idle
 *    slot fetches only `OLDER_PAGE_LIMIT` rows, while preserved blocks span
 *    the whole tab lifetime). DROP it. Appending those used to stack every
 *    out-of-window block from hours of turns as a wall of bare "Thinking"
 *    chips at the transcript tail, re-appended on every later refresh
 *    (#5798). Dropping matches what a full page reload does anyway —
 *    reasoning is client-only and never survives one.
 *  - **Past the covered region** (the anchor row is newer than everything the
 *    snapshot knows): a racing mid-turn refresh (WS reconnect) snapshots the
 *    server, then a tool frame or more streamed text lands BEFORE the fetch
 *    fulfills — the anchor is absent from `incoming` because the snapshot is
 *    older than it, not because history dropped it. KEEP the block (tail
 *    append; the live turn is the tail). The same applies to an anchor that
 *    was never server-confirmed (a `streaming` row, or a text row without a
 *    server `ts`) — its text can grow past what any snapshot holds.
 *
 *  Coverage is measured conservatively: the last `existing` row whose identity
 *  (tool id / `mid` / role+`ts` / role+text) appears in `incoming`. When
 *  nothing matches, nothing is dropped — declining to guess loses at worst a
 *  misplaced chip, while guessing wrong deletes live reasoning.
 *
 *  The anchor is the FOLLOWING TOOL CALL's `tool_call_id` when there is one, and
 *  the following answer text only otherwise. A tool id is the sharper key and
 *  the only one that scales: `_tool_meta` mints it server-side and persists it on
 *  the tool row, so it survives into history and reads back identically on a
 *  historical replay. Answer content does not scale, because a turn that reasons
 *  before each of N tool calls emits no text at those boundaries — the backend's
 *  segment flush is gated on pending text (`chat_runner._flush_segment`, called
 *  under `if not in_tool_group and assistant_text`) — so history holds ONE
 *  assistant row for all N bursts. Anchoring every burst on that single row let
 *  exactly one land and parked the other N-1 at the tail, below the answer and
 *  its footer, as a column of collapsed rows that read as duplicates (#4218).
 *
 *  Bursts and anchors are 1:1 under the tool rule: burst k is followed by tool k,
 *  and the final burst by the answer. An auto-approved call emits two tool rows
 *  sharing one id (🔧 pre-approval + ✅ post-approval, see
 *  `applyToolOutputToMessages`); `used` makes the first win, which is the earlier
 *  row and so the correct side of the pair.
 *
 *  A block this function decides has no position here is handed to
 *  `orphanSink` when the caller supplied one, rather than discarded:
 *  `state.messages` is its only copy, so a later page that loads its anchor
 *  can re-seat it. With no sink it is dropped as before. Either way it
 *  leaves the rendered list, so the #5798 tail wall stays cured. */
export function mergePreservedThinking<M extends { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }>(
  existing: M[],
  incoming: M[],
  coverageSource: M[] = incoming,
  windowComplete = true,
  orphanSink?: Array<ParkedThinking<M>>,
): M[] {
  const toolAnchorId = (m: M): string => {
    if (m.role !== 'tool') return ''
    const id = m.meta?.tool_call_id
    return typeof id === 'string' ? id : ''
  }
  // Conservative row identity for the coverage cut: the STRONGEST available
  // class only — tool id, else server-minted `mid`, else role+ts, else
  // role+trimmed text. Never stacked among those classes: a strong-identity
  // row must not also match on a weaker key, or a duplicate-content sibling
  // (two `🔧 bash` calls with distinct tool ids) lets an OLDER incoming row
  // text-match a NEWER existing row and falsely extend coverage past a
  // post-snapshot anchor — which would drop live reasoning. (`send:${sendId}`
  // is the one deliberate exception; see below.)
  //
  // Coverage evidence comes ONLY from `coverageSource` — the PURE fetched
  // server page, before the reducer re-attaches any client-preserved rows
  // (live `permission` cards, the finalized `lastLocal` reply on a
  // switchSlot). A re-attached row matching its own copy in `existing` would
  // vouch for a span of history the snapshot never actually covered —
  // advancing the cut past a post-snapshot tool anchor and dropping its live
  // reasoning. Provenance, not role, is the boundary: every row in the pure
  // page is server-persisted by construction, so no role filtering is needed
  // and persisted roles beyond the common five (inject, subagent, …) count
  // toward coverage instead of silently shortening it.
  //
  // Used only to locate the newest `existing` row the snapshot still
  // contains — never to dedupe rows — so a residual text collision among
  // identity-less rows can only make coverage read longer, and only among
  // rows that carry no stronger key.
  //
  // `send:${sendId}` is the ONE key that rides ALONGSIDE the strongest class
  // rather than being ranked in it. A UNIQUE send id is not a weak key — a
  // client-minted one-shot id two rows can share only by being the same send
  // (the same convention `rowIdentities` returns both halves of). It MUST
  // stack, because the two copies of a pre-echo send have different strongest
  // keys by construction: the local optimistic bubble carries only `sendId`
  // while its persisted counterpart carries a server `mid` — ranked
  // strongest-only they could never match, and the covered bubble would read
  // as uncovered (#6075). A DUPLICATED id is excluded outright
  // (`dupSendIds`): an id repeated within one list names two different sends,
  // and letting it match would extend the coverage cut past a live
  // post-snapshot anchor on the strength of the WRONG row — deleting live
  // reasoning, the exact failure the never-stacked rule exists to prevent.
  // The pre-echo pair is one occurrence in EACH list, so duplication is
  // counted per list, never across the two. Only user rows emit the key:
  // that is the only role a send id legitimately lives on, and honoring it
  // elsewhere would let a mislabeled row vouch for a bubble.
  const dupSendIds = new Set<string>()
  const countDupSendIds = (list: M[]): void => {
    const seen = new Set<string>()
    for (const m of list) {
      if (m.role !== 'user') continue
      const sid = m.meta?.sendId
      if (typeof sid !== 'string' || !sid) continue
      if (seen.has(sid)) dupSendIds.add(sid)
      else seen.add(sid)
    }
  }
  countDupSendIds(coverageSource)
  countDupSendIds(existing)
  const coverageIds = (m: M): string[] => {
    const ids: string[] = []
    const tid = toolAnchorId(m)
    const mid = m.meta?.mid
    if (tid) ids.push(`tool:${tid}`)
    else if (typeof mid === 'string' && mid) ids.push(`mid:${mid}`)
    else if (m.ts) ids.push(`ts:${m.role}:${m.ts}`)
    else if (m.content) ids.push(`txt:${m.role}:${m.content.trimEnd()}`)
    if (m.role === 'user') {
      const sid = m.meta?.sendId
      if (typeof sid === 'string' && sid && !dupSendIds.has(sid)) ids.push(`send:${sid}`)
    }
    return ids
  }
  const preserved: Array<{ msg: M; anchor: ThinkingAnchor | null; anchorIdx: number; confirmed: boolean; boundaryIdx: number; skip: number }> = []
  // Which backend path each covered send took, keyed by its client-minted
  // `sendId` (#6075). Read where the anchor scan below breaks at an optimistic
  // STEER bubble: a persisted NON-steer row carrying the bubble's id proves the
  // steer POST raced `chat_done` onto the new-turn path (the bubble IS a turn
  // boundary), a persisted STEER row proves acceptance into the running turn
  // (not a boundary at all). Built from `coverageSource` only — the same
  // provenance rule the coverage cut follows — so a re-attached client row can
  // never vouch for itself. A `null` entry is a tombstone: the page holds MORE
  // THAN ONE row with that id, so the id names no single path and resolves
  // nothing (decline, not guess — ids are minted unique, so a duplicate is
  // either a client defect or an adversarial echo, and both must fail safe).
  const steerBySendId = new Map<string, boolean | null>()
  for (const m of coverageSource) {
    if (m.role !== 'user') continue
    const sid = m.meta?.sendId
    if (typeof sid !== 'string' || !sid) continue
    steerBySendId.set(sid, steerBySendId.has(sid) ? null : !!m.meta?.steer)
  }
  // How many rows already repeated this text, so a duplicated anchor resolves to the
  // block's OWN turn rather than to the first match.
  const priorText = new Map<string, number>()
  // The same count over the WHOLE list, recorded with a parked block so a later list
  // that gained occurrences invalidates the ordinal instead of misusing it.
  const existingTotal = new Map<string, number>()
  for (const m of existing) {
    if (m.role !== 'assistant' && m.role !== 'streaming') continue
    const t = m.content.trimEnd()
    existingTotal.set(t, (existingTotal.get(t) ?? 0) + 1)
  }
  for (let i = 0; i < existing.length; i++) {
    const m = existing[i]
    if (m.role === 'assistant' || m.role === 'streaming') {
      const t = m.content.trimEnd()
      priorText.set(t, (priorText.get(t) ?? 0) + 1)
    }
    if (m.role !== 'thinking' || !m.content) continue
    let anchor: ThinkingAnchor | null = null
    let anchorIdx = -1
    let confirmed = false
    let boundaryIdx = -1
    for (let j = i + 1; j < existing.length; j++) {
      const cand = existing[j]
      const tid = toolAnchorId(cand)
      if (tid) { anchor = { tool: tid }; anchorIdx = j; confirmed = true; break }
      if (cand.role === 'assistant' || cand.role === 'streaming') {
        anchor = { text: cand.content.trimEnd(), ts: cand.role === 'assistant' ? cand.ts : undefined, mid: rowMid(cand) }
        anchorIdx = j
        // A `streaming` row's text is still growing, and a text row without a
        // server `ts` has no persisted counterpart yet — either way a racing
        // refresh can miss this anchor without the block being stale, so only
        // a server-confirmed anchor makes a lookup miss mean "drop".
        confirmed = cand.role === 'assistant' && !!cand.ts
        break
      }
      // A confirmed steer does not end this block's turn, so the row after it is
      // still its anchor. Breaking here instead would record a turn boundary for
      // a block whose turn is NOT over — misplacing it at the tail, and (once
      // the page covers the steer row) dropping reasoning that has a real
      // anchor further down.
      //
      // Record WHICH row ended the scan: an anchorless block with a recorded
      // boundary belongs to a FINISHED turn (stopped mid-reasoning, or a
      // reasoning-only turn that emitted no tool call and no text), not to the
      // live tail, and the tail-keep below uses that to decide whether the
      // block's turn is inside the covered region and therefore over (#5815).
      //
      // An OPTIMISTIC bubble of ANY kind breaks the scan (it may be a new turn,
      // and reading past it could splice that turn's reasoning onto this block)
      // and by default records no boundary and authorizes no drop. The
      // predicate is `optimistic` alone, NOT `steer && optimistic`: a plain
      // send is stamped optimistic too (keyed on its `sendId`, see
      // `appendMessage`), and it is just as ambiguous. If the client's idle
      // state was stale the server takes its QUEUE path — persisting no `user`
      // row for that text at all — while the turn keeps emitting rows; a
      // refresh covering one of those later rows would then put this
      // unpersisted bubble INSIDE the covered region and drop the live turn's
      // reasoning above it. A steer bubble is ambiguous for its own reason:
      // accepted into the running turn (its `steer_push` echo pending, real
      // anchor one reconciliation away) or raced `chat_done` onto the new-turn
      // path. Every attempt to resolve either ambiguity from TEXT identity
      // proved unsound in review (duplicate-text turns, missed echoes, pages
      // reaching past the bounded cache window), so text never resolves it.
      //
      // ID identity does (#6075) — for STEER bubbles only. A steer bubble
      // minted with a `sendId` names its persisted counterpart outright: the
      // covered page holding a NON-steer row with that id proves the new-turn
      // path — the bubble is a real turn boundary, recorded so the finished
      // turn's chip drops instead of stranding at the tail — while a STEER row
      // with that id proves acceptance, so the scan continues past it exactly
      // as it would past a confirmed steer (the block's real anchor lies
      // further down). A bubble whose id the page does not contain — or
      // contains MORE THAN ONCE (the `null` tombstone) — keeps the
      // decline-to-guess default: break, no boundary, no drop.
      //
      // A PLAIN optimistic send is deliberately NOT resolved this way, even
      // though it carries a `sendId` too: for a non-steer send, "a persisted
      // row with this id exists" does not prove "the turn above this bubble is
      // over" — a durable-queue ingress (the retired Crew Mode was one) can persist the user row and
      // starts no turn at all — so recording a boundary there re-opens the
      // over-drop class the text heuristics were retired for. For a steer
      // bubble the inference is sound precisely because the row's own `steer`
      // flag names which backend path consumed the send.
      if (isTurnBoundaryUser(cand)) {
        if (!cand.meta?.optimistic) { boundaryIdx = j; break }
        if (cand.meta?.steer) {
          const sid = cand.meta?.sendId
          const steered =
            typeof sid === 'string' && sid && !dupSendIds.has(sid) ? steerBySendId.get(sid) : undefined
          if (steered === true) continue
          if (steered === false) boundaryIdx = j
        }
        break
      }
    }
    preserved.push({ msg: m, anchor, anchorIdx, confirmed, boundaryIdx, skip: anchor?.text !== undefined ? (priorText.get(anchor.text) ?? 0) : 0 })
  }
  if (!preserved.length) return incoming
  // Coverage cut: index of the last `existing` row whose identity the PURE
  // server page contains. Anchors past this index are newer than the snapshot
  // (a tool frame / streamed text that landed after the fetch was taken) — a
  // lookup miss for those says the snapshot is old, not that history dropped
  // them.
  const incomingIds = new Set<string>()
  for (const m of coverageSource) for (const id of coverageIds(m)) incomingIds.add(id)
  let coveredIdx = -1
  for (let i = existing.length - 1; i >= 0; i--) {
    if (coverageIds(existing[i]).some(id => incomingIds.has(id))) { coveredIdx = i; break }
  }
  // No-overlap fallback: a page sharing NO identity with `existing` is either
  // an unrelated racing snapshot (keep everything) or a transcript that moved
  // entirely PAST the stale cache (a long-disconnected session that advanced
  // beyond the page size) — where keeping everything re-creates the #5798
  // wall and the appended blocks go permanently anchorless. Server timestamps
  // disambiguate: a confirmed anchor whose own server ts is OLDER than the
  // oldest row of the pure page belongs to evicted history — droppable. The
  // fallback arms ONLY when EVERY pure-page row carries a readable ts: a
  // single ts-less or unparseable row means the page's true oldest instant is
  // unknown, and a min over the readable subset could overstate it and drop an
  // anchor the page actually reaches back past. Decline, not guess.
  let oldestPageMs: number | null = null
  if (coveredIdx < 0 && coverageSource.length > 0) {
    for (const m of coverageSource) {
      const ms = transcriptTsMs(m.ts)
      if (ms === null) { oldestPageMs = null; break }
      if (oldestPageMs === null || ms < oldestPageMs) oldestPageMs = ms
    }
  }
  // Counting occurrences cannot catch this: when the real anchor is off-window the
  // duplicate that makes a text match wrong is the only one loaded. Tool ids are safe.
  const ambiguous = (a: ThinkingAnchor | null): boolean =>
    !windowComplete && a?.text !== undefined
  const used = new Set<number>()
  const result: M[] = []
  const seenText = new Map<string, number>()
  for (const item of incoming) {
    const tid = toolAnchorId(item)
    const isText = item.role === 'assistant' || item.role === 'streaming'
    if (tid || isText) {
      const c = isText ? item.content.trimEnd() : ''
      let occ = 0
      if (isText) { occ = seenText.get(c) ?? 0; seenText.set(c, occ + 1) }
      for (let p = 0; p < preserved.length; p++) {
        if (used.has(p)) continue
        const a = preserved[p].anchor
        if (!a) continue
        // A text anchor that recorded a server `ts` matches only the row with
        // that exact `ts` — text alone lets an OLDER duplicate-answer block
        // ("Done.") steal the newer answer row while the newer block is
        // dropped as covered. A ts-less anchor (freshly streamed, unreloaded)
        // keeps text-only matching; it is unconfirmed, so a miss never drops.
        // An identity key (`ts` or `mid`) names the row outright, so it licenses the match
        // past the ambiguity and ordinal guards, which exist only because text cannot.
        const midHit = isText && a.tool === undefined && a.mid !== undefined && rowMid(item) === a.mid
        const tsHit = a.tool === undefined && a.ts !== undefined && a.ts === item.ts
        if (!midHit && !tsHit && ambiguous(a)) continue
        const textMatches = a.tool === undefined && a.text === c
          && (a.ts === undefined || a.ts === item.ts)
          && anchorMidOk(a, item)
          && (midHit || tsHit || preserved[p].skip === occ)
        if (tid ? a.tool === tid : textMatches) {
          result.push({ ...preserved[p].msg }); used.add(p); break
        }
      }
    }
    result.push(item)
  }
  for (let p = 0; p < preserved.length; p++) {
    // The tail keeps: truly anchorless blocks (nothing followed them AT ALL —
    // the live turn's in-flight reasoning, whose tail IS its position), blocks
    // whose anchor row was never server-confirmed (a racing mid-turn refresh
    // can miss those without the block being stale), and blocks whose anchor
    // sits PAST the coverage cut (newer than everything the snapshot contains
    // — the snapshot is old, not the block).
    //
    // Two shapes are droppable via coverage, both meaning "this block's turn
    // is over and the snapshot covers it, yet holds no position for the
    // block":
    //  - a server-confirmed anchor INSIDE the covered region that missed its
    //    lookup (bounded page / rewritten history) — dropping it rather than
    //    stranding it at the tail below unrelated turns is #5798;
    //  - an anchorless block whose scan was TERMINATED by a turn-boundary
    //    user row inside the covered region (the turn was stopped
    //    mid-reasoning, or emitted no tool call and no answer text). The
    //    boundary row is a persisted user message, so the snapshot covering
    //    it proves the server's full account of that finished turn — which
    //    contains no reasoning (reasoning is client-only). Keeping the block
    //    teleported it to the transcript tail, below unrelated turns, and
    //    re-appended it there on every later refresh — one permanent stray
    //    chip per stopped turn (#5815). Dropping matches a page reload.
    //    A boundary past the cut (or unresolved, coveredIdx < 0 without a
    //    server-identity eviction proof) keeps the block: the snapshot may
    //    simply predate it.
    if (used.has(p)) continue
    const { msg, anchor, anchorIdx, confirmed, boundaryIdx, skip } = preserved[p]
    const posIdx = anchor !== null ? anchorIdx : boundaryIdx
    const posRow = posIdx >= 0 ? existing[posIdx] : undefined
    const insideCoverage = posIdx >= 0 && posIdx <= coveredIdx
    // The eviction fallback compares the POSITION row's own `ts` against the
    // page's oldest instant, so it is only sound when that `ts` is
    // server-minted. An anchor qualifies by `confirmed` (a server tool id, or
    // an assistant row carrying a server `ts`). A BOUNDARY does not: a plain
    // turn-boundary `user` row is the composer's optimistic bubble, appended
    // locally with `new Date().toISOString()` and only a client `sendId` —
    // the server-minted `mid` arrives with the echo (ChatPage's send path).
    // A browser clock running behind the server would read that bubble as
    // older than every page row and evict LIVE reasoning. So a boundary may
    // use the fallback only once it carries `mid`; without it, `insideCoverage`
    // is the only route to a drop, which is over-keep — the safe direction.
    const posTsIsServer = anchor !== null || typeof posRow?.meta?.mid === 'string'
    const posMs = posRow && posTsIsServer ? transcriptTsMs(posRow.ts) : null
    const evicted = coveredIdx < 0 && oldestPageMs !== null && posMs !== null && posMs < oldestPageMs
    const droppable = (anchor !== null ? confirmed : boundaryIdx >= 0) && (insideCoverage || evicted)
    if (droppable) {
      // Parking retains what a later page can re-seat, so only an ANCHORED block earns
      // it: a #5815 boundary drop names no row to match and would never leave the sink.
      if (anchor !== null) {
        // The occurrence names which of two identical answers is this block's turn.
        const total = anchor.text !== undefined ? (existingTotal.get(anchor.text) ?? 0) : 0
        orphanSink?.push({ msg, anchor, occ: skip, occTotal: total })
      }
      continue
    }
    result.push({ ...msg })
  }
  return result
}

/** Re-insert parked reasoning blocks whose anchoring row is now loaded.
 *  Returns the new list plus the blocks still waiting for their anchor.
 *
 *  `windowComplete` is required rather than defaulted so the compiler names every
 *  call site — the hazard `hydrateQueuedBubbles` records at the three near-identical
 *  slot-detail reducers.
 *
 *  A TEXT-ONLY anchor needs a complete window: while it is incomplete the genuine
 *  anchor may sit above it, so the one loaded row carrying that text belongs to a
 *  different turn. Once complete, a REPEATED text is resolved by the occurrence
 *  recorded at park time, and a count that GREW invalidates that ordinal. An exact
 *  `mid` or server `ts` bypasses both: text AND ts is strictly more evidence than
 *  text AND ordinal, and withholding it hid the block permanently. Tool ids are
 *  1:1 with bursts (#4578) and need none of this. */
export function reinsertThinkingOrphans<M extends { role: string; content: string; ts?: string; meta?: Record<string, unknown> }>(
  list: M[],
  parked: Array<ParkedThinking<M>>,
  windowComplete: boolean,
): { list: M[]; remaining: Array<ParkedThinking<M>> } {
  if (!parked.length) return { list, remaining: parked }
  const used = new Set<number>()
  const out: M[] = []
  const textFreq = new Map<string, number>()
  for (const item of list) {
    if (item.role !== 'assistant' && item.role !== 'streaming') continue
    const t = item.content.trimEnd()
    textFreq.set(t, (textFreq.get(t) ?? 0) + 1)
  }
  const seenText = new Map<string, number>()
  for (const item of list) {
    const tid = item.role === 'tool' && typeof item.meta?.tool_call_id === 'string' ? item.meta.tool_call_id : ''
    const isText = item.role === 'assistant' || item.role === 'streaming'
    if (tid || isText) {
      const c = isText ? item.content.trimEnd() : ''
      let occ = 0
      if (isText) { occ = seenText.get(c) ?? 0; seenText.set(c, occ + 1) }
      for (let p = 0; p < parked.length; p++) {
        if (used.has(p)) continue
        const rec = parked[p]
        const a = rec.anchor
        // The guards below exist only because TEXT cannot name a turn; an exact row id or
        // server `ts` can -- either must bypass them, or the block hides for good.
        const midHit = isText && a.tool === undefined && a.mid !== undefined && rowMid(item) === a.mid
        const tsHit = isText && a.tool === undefined && a.ts !== undefined && a.ts === item.ts
        if (!midHit && !tsHit) {
          if (!windowComplete && a.tool === undefined) continue
          const freq = a.tool === undefined ? (textFreq.get(a.text) ?? 0) : 0
          // A recorded occurrence identifies the turn at ANY count, so a set that shrank to one
          // is still checked; only GROWTH invalidates it, since removals here are tail-only.
          if (rec.occ !== undefined ? (freq > (rec.occTotal ?? 0) || rec.occ !== occ) : freq > 1) continue
        }
        if (tid ? a.tool === tid : a.tool === undefined && a.text === c && anchorMidOk(a, item)) { out.push({ ...rec.msg }); used.add(p); break }
      }
    }
    out.push(item)
  }
  const unmatched = parked.filter((_, p) => !used.has(p))
  // An unmatched record stays parked: appending seats reasoning AFTER the newest
  // reply, and a tail-ordered transcript is worse than a block that stays hidden.
  if (!used.size) return { list, remaining: parked }
  return { list: out, remaining: unmatched }
}

import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { whenScrollQuiet } from '../lib/scrollQuiet'
import { api } from '../api/client'
import { devLog, inspectorOn } from '../dev/scrollInspector'
import { addSlotOptimistic, updateSlot, removeSlotOptimistic, markSlotRead, fetchSlots, slotSurfaceKey, sseSlots, sseConnected } from './dashboardSlice'
import { resolveDefaultColor } from '../utils/sessionColors'
import { isChatPageSurface } from '../utils/channelOrigin'
import { isSystemNoticeKind } from '../lib/systemNotice'
import { isStopEvent } from '../lib/stopEvent'
import { normalizeRunSessionKey } from '../apps/workflows/runModel'
import { gcSessionStorage } from '../utils/storageGc'
import type { RootState } from './index'
import type { ChatMessage, ChatSlot, SessionInfo, SubagentActivity, ToolActivity, WorkflowRunSummary } from '../types'
import { SOFT_STOP_DEBOUNCE_MS, SPAWN_LAUNCH_MARKER } from '../pages/chat/types'
import { mergePreservedPastes } from '../utils/pasteTokens'
import { safeSetItem } from '../utils/safeStorage'
import { errMessage, isMissingSlotError, type StatusRejection } from '../utils/thunkError'
import { parseErrorCode } from '../utils/errorReport'
import { jsonEqual } from '../utils/structuralEqual'
import type { McpAppRenderPayload } from '../lib/mcpAppSrcdoc'
import { i18nT } from '../i18n/t'
import { secureRandomId } from '../utils/secureId'
import { mergeIntoDraft } from '../utils/chatDrafts'
import { isRejectedDecision } from '../utils/approvalDecision'

const SKIP_ROLES = new Set(['chunk', 'done'])
const filterMessages = (msgs: ChatMessage[]) => msgs.filter(m => !SKIP_ROLES.has(m.role))

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
const mintMsgId = (): string => `msg-${secureRandomId()}`

/** Stamp a stable `meta.clientTs` on a message that has no server `ts` and no
 *  pre-existing client id. This makes every ts-less message carry a durable
 *  identity from birth, surviving Immer structural sharing, refetch/rehydration,
 *  and list replacement — closing the identity gap for error/system/permission
 *  messages that were previously only stable via WeakMap (object identity). */
const ensureMsgId = (msg: ChatMessage): ChatMessage => {
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
function isRedeliveredMessage(
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
function deduplicateByMid(msgs: ChatMessage[]): ChatMessage[] {
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
const RECONCILE_WINDOW = 50

/** Reconcile a server echo (carrying both `sendId` and `mid`) against the
 *  optimistic user bubble that was appended client-side at send time.
 *
 *  Scans backward over non-steer user messages looking for a `sendId` match.
 *  On match: updates ts/meta, clears the `optimistic` flag, and strips the
 *  one-shot `sendId` from persisted meta (it served its correlation purpose).
 *
 *  Returns `true` if reconciliation succeeded (caller should `return` to skip
 *  the push), `false` if no match was found (caller falls through to push).
 *
 *  FIX for #3898: the prior inline scan used an unconditional `break` after the
 *  first non-matching user message, preventing reconciliation of pipelined sends
 *  (user A then user B — echo for A could never reach past B). Now uses
 *  `continue` to keep scanning. */
function reconcileOptimisticEcho(
  msgs: ChatMessage[],
  echoSendId: string,
  meta: Record<string, unknown>,
  ts?: string,
): boolean {
  const reconcileFloor = Math.max(0, msgs.length - RECONCILE_WINDOW)
  for (let i = msgs.length - 1; i >= reconcileFloor; i--) {
    const m = msgs[i]
    if (m.role !== 'user') continue
    if (m.meta?.steer) break // steer boundary — stop scanning
    if (m.meta?.sendId === echoSendId) {
      if (ts) m.ts = ts
      m.meta = { ...(m.meta || {}), ...meta }
      // The sendId is a one-shot wire correlation ID — strip it from the
      // persisted meta now that reconciliation succeeded (#3898 item 2).
      delete (m.meta as Record<string, unknown>).sendId
      delete (m.meta as Record<string, unknown>).optimistic
      return true
    }
    // #3898 fix: continue scanning past non-matching user messages so
    // pipelined sends (multiple optimistic bubbles) can all be reconciled.
  }
  return false
}

/** Frame roles that retire a slot's pending STATELESS question card.
 *
 *  Deliberately NARROWER than "every role that starts a turn". The card's
 *  contract is "the user's answer arrives as the next message", and the roles
 *  here are the ones where that answer channel is genuinely gone:
 *
 *  - `user` — the human spoke (composer answer, or something else entirely);
 *    either way the next-message channel was consumed by its owner.
 *  - `nudge` — an auto-nudge cycle deliberately moved the session on past the
 *    question; the loop's instruction, not the answer, became the next turn.
 *
 *  `inject` (cron notifications, recovery resumes) and `subagent` (completion
 *  events) also start turns, but they interleave with a question the agent may
 *  STILL be waiting on: an agent that spawns work, asks the user a question,
 *  and ends its turn will absorb completion events while the question remains
 *  genuinely open — clearing the card on those frames would delete the user's
 *  only UI for answering a live question. If a session moves on for real, its
 *  next user/nudge frame still retires the card. Extending coverage is a data
 *  edit here, not a code change (per Design Review on PR #2131). */
const QUESTION_RETIRING_ROLES = new Set(['user', 'nudge'])

/** Drop a slot's pending STATELESS question card (no ``ask_id``) when a
 *  turn-consuming frame lands on that slot.
 *
 *  A stateless card's contract is "the user's answer arrives as the next
 *  message" (the agent ended its turn on it — `post_question_card`, no
 *  server-side wait). So the frame that STARTS the slot's next turn consumes
 *  the card's answer channel and makes it stale. Without this, a monitored
 *  session that asked a question and was then nudged onward parks the card
 *  above the composer FOREVER — it invites an answer no turn is waiting for.
 *
 *  Server-owned cards (with `ask_id`) are exempt: their lifecycle is the
 *  `question_card_resolved` broadcast (answered / timed out / cancelled /
 *  slot stop), and a blocked wait can legitimately outlive a mid-turn steer
 *  frame — clearing on it would strand the blocked tool call with no card.
 *
 *  Shared by the two hand-synced frame appliers (active `sseChatMessage` and
 *  background `applyNonActiveFrame`) so the paths cannot drift; both call it
 *  AFTER their redelivery guard so a replayed old frame cannot wipe a new
 *  card. */
const dropStaleStatelessQuestion = (state: ChatState, slot: string, role: string): void => {
  if (!QUESTION_RETIRING_ROLES.has(role)) return
  const card = state.pendingQuestions?.[safeKey(slot)]
  if (card && !card.ask_id) {
    // Never destroy work in progress: a non-empty custom answer lives only in
    // the card's component state (QuestionCard publishes emptiness flips via
    // setQuestionDraft), so deleting the entry here would unmount the card and
    // silently discard the user's half-typed answer — precisely on monitored
    // sessions, where nudge frames land at unpredictable times. The card stays
    // until the draft is cleared, answered, or manually dismissed; staleness
    // resumes on the next turn-consuming frame after that.
    if (card.draftActive) return
    delete state.pendingQuestions[safeKey(slot)]
  }
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
const finalizeTrailingStreaming = (msgs: ChatMessage[]) => {
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

/** The three keys that can pollute `Object.prototype` when used to index a
 *  plain-object map (`obj[key] = ...`). Slot ids, subagent ids, run ids, and
 *  session keys all flow in from WebSocket action payloads; a crafted payload
 *  carrying `__proto__` / `constructor` / `prototype` would otherwise mutate the
 *  shared prototype through the per-slot state maps in this slice. */
/** True if `key` would pollute the prototype chain if used to index a plain
 *  object. Every reducer that indexes a `Record<string, …>` state map by an
 *  externally-supplied key rejects such a key up front (early return) — an
 *  explicit guard the CodeQL prototype-pollution query recognizes as a barrier.
 *  It is the single fail-closed chokepoint; a dropped frame for a hostile key is
 *  the correct outcome (no legitimate slot/subagent/run id is `__proto__`).
 *  Written as explicit `===` comparisons (not a Set lookup) so static analysis
 *  can model it as a sanitizing guard. */
const isUnsafeKey = (key: string): boolean =>
  key === '__proto__' || key === 'constructor' || key === 'prototype'

/** Defense-in-depth companion to the early-return guards: reroutes a poisoned
 *  key to an inert own-property so any write that slips past a guard still can't
 *  reach the prototype. Real keys pass through unchanged. */
const safeKey = (key: string): string => (isUnsafeKey(key) ? `unsafe-key:${key}` : key)

/** Composite key for `state.mcpApps`: `<session>\u001F<tool_call_id>`. The
 *  session scope prevents cross-slot render collisions and makes per-slot
 *  eviction a prefix scan (the payloads carry multi-MB app HTML, so they must
 *  not outlive their slot). \u001F (unit separator) cannot appear in either
 *  component. */
const MCP_APP_KEY_SEP = '\u001F'

export const mcpAppKey = (sessionKey: string, toolCallId: string): string =>
  `${sessionKey}${MCP_APP_KEY_SEP}${toolCallId}`

/** Max MCP App render payloads retained per slot (each carries multi-MB HTML);
 *  oldest are evicted past this bound. */
const MCP_APPS_PER_SLOT_MAX = 24

/** Drop every MCP App render payload belonging to `sessionKey` (slot deleted
 *  or its conversation cleared — the tool rows the apps hang off are gone). */
const evictMcpApps = (state: { mcpApps: Record<string, McpAppRenderPayload> }, sessionKey: string): void => {
  const prefix = `${sessionKey}${MCP_APP_KEY_SEP}`
  // `?? {}` for the same reason every sibling enumeration here carries it: a
  // preloaded state need not define every per-slot map, and teardown is now
  // reachable from three writers rather than one.
  for (const k of Object.keys(state.mcpApps ?? {})) {
    if (k.startsWith(prefix)) delete state.mcpApps[k]
  }
}

/** Chat state keyed by a slot.
 *
 *  Single owner of what is keyed per slot, read by every teardown path, so a new
 *  per-slot map registered here is reached by all of them.
 *
 *  Spelling is mixed rather than uniform: several of these are written with the
 *  bare key at some call sites and through `safeKey()` — which rewrites
 *  prototype-polluting names — at others, so teardown removes both spellings
 *  instead of assuming a clean split. `subagents` (keyed `dashboard:<slot>`) and
 *  `workflowRuns` (keyed by run id) are absent because a slot key never matches
 *  their entries; `mcpApps` carries the slot as a key PREFIX and is handled by
 *  `evictMcpApps`. */
const slotKeyedMaps = (state: ChatState) => [
  state.slotMessages, state.slotActivity, state.slotRun, state.slotHydrated,
  state.slotSide, state.slotSideClosed, state.slotStatusDetail,
  state.slotContextPct, state.slotContextTokens, state.stopPressedAt,
  state.followups, state.folderSuggestions,
  state.pendingQuestions, state.subagentQueued, state.goalLoops,
  // A surviving pane marker makes a recreated slot's hydrate early-return into
  // nothing, so these must die with the transcript they describe. The retained
  // server count belongs with them: kept past an eviction it would read as a
  // fall against a recreated slot's first fetch and drop a legitimate tail.
  state.slotPaneHasMore, state.slotPaneBounded, state.slotServerTotal,
  state.slotServerTotalSeq,
  state.thinkingOrphans,
].filter(Boolean)

/** Every slot key that still has residue anywhere in chat state.
 *
 *  A reconcile can only evict a slot it visits, so this has to cover the same
 *  surfaces `evictSlotState` clears — including the two that are not plain
 *  slot-keyed maps: `mcpApps`, whose keys carry the slot as a prefix, and
 *  `slotHistory`, where a slot can outlive every map entry. */
const slotKeysWithResidue = (state: ChatState): Set<string> => new Set([
  ...slotKeyedMaps(state).flatMap(m => Object.keys(m)),
  ...Object.keys(state.mcpApps ?? {}).map(k => k.split(MCP_APP_KEY_SEP)[0]),
  ...(state.slotHistory ?? []),
])

/** Drop every trace of one slot from chat state.
 *
 *  A local delete and a reconcile against the authoritative slot list both end
 *  here, so the two cannot disagree about what a departing slot leaves behind.
 *  Both spellings are removed: `safeKey` is identity for ordinary slot names and
 *  a no-op on an already-rewritten key, so one pass covers a caller holding
 *  either form. */
/** Evict every slot carrying residue that the authoritative list does not name.
 *  Both authoritative writers (`sseSlots`, `fetchSlots.fulfilled`) reconcile
 *  through here, so neither can drift from the other. The active slot is never
 *  pruned: its live `messages`/optimistic state must not be dropped out from
 *  under the open pane. */
const reconcileSlotResidue = (state: ChatState, payload: readonly { key: string }[]): void => {
  const live = new Set(payload.map(s => s.key))
  if (state.activeSlot) live.add(state.activeSlot)
  // A live slot is protected under either spelling, since some writers store it
  // rewritten by safeKey().
  for (const key of [...live]) live.add(safeKey(key))
  for (const key of slotKeysWithResidue(state)) {
    if (live.has(key)) continue
    evictSlotState(state, key)
  }
}

const evictSlotState = (state: ChatState, slotKey: string): void => {
  const spellings = [slotKey, safeKey(slotKey)]
  for (const m of slotKeyedMaps(state)) {
    for (const spelling of spellings) delete m[spelling]
  }
  evictMcpApps(state, slotKey)
  state.slotHistory = (state.slotHistory ?? []).filter(k => k !== slotKey)
  // An evicted slot cannot serve as the failed-switch fallback either: an
  // authoritative snapshot said it is gone, and restoring it would re-create
  // exactly the dead-slot selection the origin exists to unwind (#6309).
  if (state.slotSwitchOrigin && spellings.includes(state.slotSwitchOrigin.key)) state.slotSwitchOrigin = null
}

/** Read one slot's pending question card, or null.
 *
 *  A bare `map[slot]` lookup is not safe even with guarded writes: for
 *  `__proto__` or `constructor` it returns an INHERITED value that is truthy but
 *  carries no `questions`, so the card renders and crashes. Guarding the key and
 *  requiring an own property makes the read fail closed. Exported so the single-
 *  chat view and the grid panes share one definition. */
export const pendingQuestionFor = (
  map: ChatState['pendingQuestions'] | undefined,
  slot: string | null | undefined,
): ChatState['pendingQuestions'][string] | null => {
  if (!slot || !map || isUnsafeKey(slot)) return null
  return Object.prototype.hasOwnProperty.call(map, slot) ? map[slot] : null
}

/** Capture a slot's pending STATELESS card's per-delivery identity for
 *  send-time capture (the `expected` value of retireStatelessQuestion). Call
 *  SYNCHRONOUSLY at the send path's ENTRY — before its first await — so the
 *  capture is the card the user saw when they hit send. Captured any later,
 *  an await gap lets the card-submit flow clear the card (capture reads null
 *  and the retire is skipped) or a newer card land (capture reads an
 *  identity this send never answered, and success would retire it) — either
 *  way the identity guard compares against the wrong baseline. Shared by the
 *  two send sites (ChatPage.send / ChatPane.doSend) so their capture logic
 *  cannot drift. Returns null when no stateless card is pending (or the
 *  entry predates identity minting): dispatch nothing then. */
export const captureStatelessCard = (
  map: ChatState['pendingQuestions'] | undefined,
  slot: string | null | undefined,
): string | null => {
  const c = pendingQuestionFor(map, slot)
  return c && !c.ask_id ? c.cardId ?? null : null
}

/** Capture a slot's pending BLOCKING card's `ask_id` for send-time capture, the
 *  `ask_id` counterpart to captureStatelessCard, with the same
 *  synchronously-at-send-entry contract and for the same reason.
 *
 *  A blocking card cannot be retired in the store the way a stateless one is:
 *  an agent is parked on its HTTP request, so deleting the entry alone leaves
 *  that agent waiting out its whole window with nothing on screen. Sending a
 *  composer message instead of using the card therefore has to resolve it
 *  through the answer endpoint, which is why the send path needs the id rather
 *  than just "a card was pending".
 *
 *  Returns null while the card holds an ANSWER IN PROGRESS — a typed custom
 *  answer or a pending option selection — because resolving it unmounts the card
 *  and that work lives only in the component. The same invariant the stateless
 *  path keeps (dropStaleStatelessQuestion), for the same reason: a send must not
 *  silently destroy something the user is part-way through. The agent then stays
 *  blocked, but the card is still on screen with the draft intact, so the user
 *  keeps both affordances for releasing it. A card the user never touched has no
 *  draft and is resolved normally. */
export const capturePendingAskId = (
  map: ChatState['pendingQuestions'] | undefined,
  slot: string | null | undefined,
): string | null => {
  const c = pendingQuestionFor(map, slot)
  if (c?.draftActive) return null
  return c?.ask_id ?? null
}

/** Whether a send's acceptance should resolve the blocking card captured at its
 *  entry. Shared by the two send sites so the rule cannot drift between them.
 *
 *  `queued` counts, which is the difference from the stateless card's rule and
 *  the whole point of this helper: a queued message cannot pop until the turn
 *  ends, and the turn cannot end while the agent is blocked on the card, so
 *  deferring to queue_pop would hold the two against each other for the entire
 *  ask window. A rejected send resolves nothing — the card is the user's only
 *  way to answer, and the session never moved on. */
export const shouldResolveAskOnSend = (
  accepted: { ok?: boolean; queued?: boolean } | null | undefined,
  askAtSend: string | null,
): boolean => !!askAtSend && !!(accepted?.ok || accepted?.queued)

/** One queued-message entry as normalized by `fetchSlotDetail` from the backend
 *  slot-detail `queue` field. */
type SlotQueueItem = { content: string; queueId: string; ts: string }

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
function sameTranscript(prev: ChatMessage[], next: ChatMessage[]): boolean {
  if (prev === next) return true
  if (prev.length !== next.length) return false
  for (let i = 0; i < prev.length; i++) if (!sameMessage(prev[i], next[i])) return false
  return true
}

/** SINGLE hydration path for the slot-detail `queue` field — the one place that
 *  turns backend queue entries into `queued` message bubbles. Every reducer that
 *  consumes a `fetchSlotDetail` payload (`switchSlot`, `warmSlotCache`,
 *  `refreshSlot`) routes through here so the hydration cannot be hand-copied and
 *  drift apart. Hand-copying it risks dropping queued messages: if `switchSlot`
 *  and `warmSlotCache` each mirror the same literal, a field added to one is
 *  silently forgotten in the other. Centralizing it means a new slot-detail
 *  payload field is added once and consumed everywhere.
 *
 *  Existing `queued` bubbles are stripped first so re-hydration is idempotent —
 *  a `queue_push` WS event may have appended a bubble during the HTTP fetch, and
 *  the server `queue` field is the canonical set. Returns a NEW array; queued
 *  bubbles are always appended last (after history), matching prior behavior. */
function hydrateQueuedBubbles(
  list: ChatMessage[],
  queue: SlotQueueItem[] | undefined,
): ChatMessage[] {
  const base = list.filter((m) => m.role !== 'queued')
  for (const { content, queueId, ts } of queue ?? []) {
    base.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId } })
  }
  return base
}

/** Single-sourced "N chunk(s) missed" degradation marker. Shared by the reducer's
 *  defensive non-batched path and the useWebSocket flush buffer (the live path)
 *  so the marker text and gap arithmetic cannot drift between the two copies.
 *  Returns '' when the seqs are adjacent (no gap). */
export const missedChunkMarker = (prevSeq: number, curSeq: number): string => {
  const missed = curSeq - prevSeq - 1
  return missed > 0 ? `\n[${missed} chunk(s) missed]\n` : ''
}

/** Per-slot activity-panel open/closed state, persisted to localStorage so the
 *  panel's open/closed choice survives a full page reload — keeping it
 *  consistent with the tab strip, which already persists per-slot
 *  (mc-panel-tabs:<slot>).
 *  Mirrors the dashboardSlice pattern: seed initialState.slotActivity from this
 *  map, write on every activityOpen change. */
const ACTIVITY_OPEN_PREFIX = 'mc-activity-open:'          // one key per slot
/** Read every persisted per-slot activityOpen flag (mc-activity-open:<slot>). */
const loadActivityOpenMap = (): Record<string, boolean> => {
  const out: Record<string, boolean> = {}
  if (typeof localStorage === 'undefined') return out
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (!k || !k.startsWith(ACTIVITY_OPEN_PREFIX)) continue
      const slot = k.slice(ACTIVITY_OPEN_PREFIX.length)
      if (slot) out[slot] = localStorage.getItem(k) === 'true'
    }
  } catch { /* enumerating storage can throw in locked-down envs */ }
  return out
}
const persistActivityOpen = (slot: string | null, open: boolean): void => {
  if (!slot) return
  safeSetItem(ACTIVITY_OPEN_PREFIX + slot, String(open))
}
/** Seed the per-slot activity buckets from the persisted open map so the first
 *  switchSlot on cold load restores each chat's panel open/closed state (the
 *  bucket's toolLog/subagents are runtime-only and start empty). */
const seedSlotActivity = (): ChatState['slotActivity'] =>
  Object.fromEntries(
    Object.entries(loadActivityOpenMap()).map(([k, open]) => [k, { toolLog: [], subagents: {}, activityOpen: open }]),
  )

type SlotState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'

/** Live progress entry for a dynamic-workflow run. Folded from workflow_run_event
 *  WS messages so the chat can show status while a run executes. */
export interface WorkflowRunProgress {
  run_id: string
  name: string
  phase: string
  lastLog: string
  status: 'running' | 'finished' | 'failed' | 'cancelled'
  error?: string
  sessionKey?: string
}

/** The statuses a run has ENDED in. Spelled once so the reducer, the reconcile
 *  and any surface deciding "is this still live?" cannot drift apart — and so an
 *  unrecognised status from a newer backend reads as "not terminal / unknown"
 *  rather than accidentally matching. */
export const WORKFLOW_TERMINAL_STATUSES = ['finished', 'failed', 'cancelled'] as const

export function isTerminalWorkflowStatus(status: string | undefined | null): boolean {
  return !!status && (WORKFLOW_TERMINAL_STATUSES as readonly string[]).includes(status)
}

/** Coerce one workflow wire field to the string `WorkflowRunProgress` declares.
 *
 *  Every text field on a run is AGENT-AUTHORED: a workflow script calls
 *  `ctx.phase(123)` or logs a dict, and that value rides the event stream and the
 *  runs API unchanged. The rendering path slices these (`(run.phase || '').slice`)
 *  so a number reaching the store throws inside render — the chat goes blank, not
 *  just this row. The type annotations claimed `string` without anything enforcing
 *  it, so this is the enforcement, applied at BOTH writers into the slice (the
 *  live `sseWorkflowEvent` and the `reconcileWorkflowRuns` read) rather than at one
 *  of them: the two paths carry the same values and a guard on only the newer one
 *  leaves the same crash reachable through the older.
 *
 *  A non-string is dropped rather than stringified: `String({})` renders
 *  "[object Object]" in the chat, which is worse than the field being absent. */
const workflowText = (value: unknown): string => (typeof value === 'string' ? value : '')

export interface SideMessage {
  role: 'user' | 'assistant'
  content: string
  ts: string
  run_id?: string
  is_error?: boolean
  /** Injected into a turn that was already running, not asked from idle. */
  steer?: boolean
  /** Shown before the server confirmed it, so it can be found again by identity.
   *  Position is not usable: an in-flight turn's frames interleave, and both the
   *  reconcile and the rollback used to guess this row was simply the last one. */
  optimistic?: boolean
}

/** One side question held behind an in-flight side turn. */
export interface SideQueueEntry {
  id: string
  content: string
  ts: string
  /** Set when this card is a steer the backend could not confirm and requeued.
   *  The card's id is brand new, so this is the only handle the submitting client
   *  has to recognise its own question — the broadcast content is redacted. */
  steerId?: string
  /** This client typed the content, so it is unredacted. A scrubbed broadcast edit cannot
   *  overwrite it — see the edit branch of `sseSideQueue`. */
  raw?: boolean
}

export interface SideState {
  messages: SideMessage[]
  lastRunId?: string
  pending?: boolean
  streaming?: boolean
  /** Questions queued behind the running turn, oldest first. */
  queue?: SideQueueEntry[]
  /** Text a cancel released, waiting for the panel to put it in the composer.
   *  Set by whichever convergence path lands first; cleared once consumed, so a
   *  lost HTTP response cannot mean lost text and neither path double-applies. */
  releasedText?: string
  /** Queue ids that have reached a TERMINAL state (drained or cancelled).
   *  A submit's HTTP callback can run after the frame that removed its entry,
   *  and re-pushing then shows a card the server no longer has — one that 404s
   *  on cancel. The server cannot rule this out for us: its `still_queued`
   *  answer is already stale by the time the callback runs. Bounded, because
   *  only the recent past can still be raced. */
  removedQueueIds?: string[]
  openedAtTurnCount: number
  createdAt: string
}

/**
 * One agent-authored follow-up suggestion.
 *
 * `prompt` is the expanded, self-contained handoff instruction — it is what
 * gets pre-filled into a composer; `title`/`description` are display only.
 * `branch` is an optional git branch name for the worktree route; when absent
 * the card derives one from the title. Server-side, every string here has
 * already been length-capped, sanitized, and credential/URL-redacted
 * (`SUGGEST_FOLLOWUP_SCHEMA` + `_redact_followup_item`), and `branch` is
 * regex-gated — but it is still LLM-authored text, so render it as text and
 * never as markup.
 */
export interface FollowupItem {
  title: string
  description: string
  prompt: string
  branch?: string
}

interface ChatState {
  activeSlot: string | null
  messages: ChatMessage[]
  slotRunning: boolean
  slotStopping: boolean
  slotState: SlotState
  slotStatusDetail: Record<string, { kind: string; text: string; ts: number; toolName?: string; toolCallId?: string }>
  slotHasMore: boolean
  slotOldestIndex: number
  /** Slot the cursor above describes. A switch moves activeSlot first, so
   *  without this the cursor silently reads as the new chat's. */
  slotCursorKey: string | null
  /** requestId of the switchSlot fetch in flight, else null. While set, that
   *  switch owns the cursor: a background settle must not re-key it, and
   *  clearing the transcript must not install a cursor over it. */
  slotSwitchRequestId: string | null
  /** Slot the in-flight switch targets; it only installs a cursor for that one. */
  slotSwitchTarget: string | null
  /** Pre-switch selection, recorded by `switchSlot.pending` so `rejected` can
   *  restore it when the target turns out to be GONE (404). `pending` mutates
   *  four things atomically -- `activeSlot`, the outgoing slot's activity, its
   *  message page, and the MRU -- and the large majority of dispatch sites
   *  never `.unwrap()`, so the unwind must live here, where the pre-switch
   *  state is still in hand (#6309; caller-side compensation re-derived three
   *  distinct bugs on #6260). When the outgoing view is itself PROVISIONAL
   *  (its own switch never settled), `pending` keeps the previous settled
   *  origin instead of recording the half-loaded key, so a rapid A→B→C chain
   *  whose C fails falls back to A, not to a B that never finished loading.
   *  `cursor` is the outgoing slot's paging cursor when it described that slot
   *  at capture time, else null (no valid cursor existed, so a restore
   *  honestly leaves paging un-keyed rather than guessing); `olderError` rides
   *  along so the origin's top-of-transcript retry bar survives the round trip.
   *  MAINTENANCE: this snapshot is a manual enumeration of the live-pane
   *  fields. A new per-pane field must join BOTH halves of the pair -- capture
   *  here (or seed its per-slot map, as `slotRun` does) in `pending`, restore
   *  in `rejected` -- or it silently leaks across a failed switch. */
  slotSwitchOrigin: {
    key: string
    cursor: { hasMore: boolean; nextBefore: number; olderError: boolean } | null
    /** The active run mirror at capture time, restored verbatim. Kept CURRENT
     *  by the non-active run writers themselves (`syncOriginRun` at every
     *  `slotRun` state write), so a transition mid-flight lands in the
     *  snapshot as an event rather than being inferred afterwards -- inference
     *  by comparing `slotRun` cannot distinguish a same-value round trip (a
     *  queued turn completing writes idle over idle) from "never moved", and
     *  restoring the stale snapshot on that path resurrects a finished turn's
     *  busy composer. `running` is carried separately from `state`: a
     *  running-but-not-yet-streaming turn legitimately reads state 'idle'
     *  while running is true, so deriving one from the other drops it. */
    run: { state: SlotState; running: boolean; stopping: boolean }
  } | null
  loadingOlder: boolean
  /** Last older-history fetch was rejected; surfaced on the top-of-transcript bar. */
  slotOlderError: boolean
  lastChunkSeq: number | undefined
  _wsChunkedDuringFetch: boolean
  /** How many `chat_message` frames were dropped as redeliveries (see
   *  `isRedeliveredMessage`), across every slot, for the life of this tab.
   *
   *  Diagnostic, not product state: nothing renders it. It exists because the
   *  dedup makes at-least-once delivery INVISIBLE — the duplicate bubbles were
   *  the only user-facing signal that something upstream re-emits frames after a
   *  restart, and that source is still unidentified. A non-zero count here is
   *  that signal, and it survives in a Redux state dump rather than in console
   *  scrollback. Steady state on a healthy gateway is 0. */
  _redeliveredFramesDropped: number
  history: SessionInfo[]
  historyHasMore: boolean
  historyOffset: number
  /** The last resume that did not land the user in a session they can use, or
   *  null. This is the ONE post-resolve check for every resume entry point
   *  (#5925): the predicates live in `resumeFromHistory`'s own cases, so a
   *  caller does not have to re-derive "did this resume actually work" to give
   *  feedback -- and the two command-palette providers, which are plain modules
   *  with no component of their own, get feedback they could not render
   *  themselves.
   *
   *  `reason` separates the two ways a resume disappoints, because they need
   *  different sentences: `surface` means it succeeded but landed on a surface
   *  the chat page cannot display, `failed` means it did not succeed at all
   *  (a rejected request, or a fulfilled payload that says `ok: false`). Before
   *  the `failed` half existed, the rarer path was the very dead click this
   *  field was added to kill.
   *
   *  Raw facts, not a sentence: the render site localizes the surface label
   *  from `key`, which only it knows how to read. Lifecycle matches the
   *  per-component notice #3640 shipped -- cleared on dismiss or on the next
   *  resume attempt. */
  unresumableResume: { key: string; title: string; surface: string; reason: 'surface' | 'failed' } | null
  /** requestId of the most recent `resumeFromHistory.pending`. Latest-click-
   *  wins for the notice above: rapid clicks each start a resume, and an
   *  EARLIER one resolving after a LATER one must not narrate a row the user
   *  has already moved past. Supersedes the sidebar's component-local
   *  sequence ref, which could only order ITS OWN clicks -- a palette resume
   *  racing a sidebar resume was unordered before. */
  lastResumeRequestId: string | null
  pendingInput: string | null
  /** Transient feedback for agent-rebind failures shared by the picker and
   *  global cycle shortcuts. The App shell owns rendering and expiry. */
  agentSwitchNotice: { message: string } | null
  // True while a createSlot POST is in flight. Lets every New Chat entry
  // point show a pending state so the UI never looks dead on click.
  creatingSlot: boolean
  slotContextPct: Record<string, number>
  // Real token counts behind the context ring (from the adapter usage_update),
  // keyed by slot. Used for the ring tooltip so "44%" shows its absolute
  // "used / window" tokens and can't be misread (e.g. 44% of 200k, not 1M).
  /** Per-slot absolute context token counts from the adapter's usage_update, so
   *  the ring tooltip can show "used / window" rather than a bare percentage.
   *  `used` is OPTIONAL: a reading seeded from a cold session's stored snapshot
   *  knows the window but not a measured used-count, and both consumers render
   *  an absent `used` as an approximation (a `~` prefix, derived from pct)
   *  rather than asserting a precise figure. */
  slotContextTokens: Record<string, { used?: number; window: number }>
  voicePlaying: boolean
  voiceAudio: string | null  // base64 stitched MP3 for replay
  subagents: Record<string, SubagentActivity>
  /** Aggregate "waiting to start" count per slot — agents accepted but queued
   *  behind the concurrency cap / stagger gate (no individual card yet). Keyed
   *  by slot name so it survives active-slot switches without the subagents
   *  map's active/non-active split. Populated by `subagent_queued` WS events. */
  subagentQueued: Record<string, number>
  /** Live goal-loop (auto-nudge) progress per slot, keyed by the BARE slot key
   *  the sidebar renders — `binding_key_for` strips the `dashboard:` prefix, so
   *  these match `Slot.key` directly. Channel loops (`slack:`/`discord:` keys)
   *  land here too and simply match no sidebar row.
   *  Only ACTIVE loops are held: a loop that hit `max_cycles` stays in the
   *  service registry with `active=false`, and a stopped loop must not keep
   *  showing progress, so presence in this map IS "looping".
   *  Cold-seeded from `GET /api/autonudge`, then kept live by `autonudge_state`
   *  WS events — the service emits one per fired cycle (autonudge.py
   *  `_emit("fired", …)` right after the `cycle_count` bump), which is what
   *  makes the counter tick without rebroadcasting the whole slots list. */
  goalLoops: Record<string, { cycle_count: number; max_cycles: number }>
  /** Agent id the user picked from the chip — the Activity Subagents tab
   *  scrolls to, expands, and auto-loads this card (1-click transcript). */
  selectedSubagentId: string | null
  toolLog: ToolActivity[]
  /** Live dynamic-workflow runs keyed by run_id. Populated from
   *  `workflow_run_event` WS broadcasts; consumed by WorkflowProgressBar. */
  workflowRuns: Record<string, WorkflowRunProgress>
  activityOpen: boolean
  activityTab: 'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'links' | 'side' | 'artifacts'
  /** Monotonic counter bumped ONLY by `openActivityToTab` — i.e. only when
   *  something deliberately asks for a view (a slash command, a sub-agent /
   *  workflow card, a keyboard shortcut). The side panel's tab strip owns which
   *  tab is focused and persists that per chat, so a consumer must distinguish a
   *  genuine request from `activityTab` merely taking a new VALUE: switching
   *  chats restores the incoming chat's cached tab (defaulting to Files), and
   *  treating that as a request would force-focus Files or the last requested
   *  view over the tab the user actually left the chat on. */
  activityTabRequest: number
  /** Pending "reveal in sidebar" request from the session header menu, or
   *  null. State, not a window event, on purpose: the sidebar is unmounted
   *  while the drawer is collapsed (and under preview expand mode / on mobile), and
   *  a one-shot CustomEvent dispatched before the listener mounts is silently
   *  dropped — there is no replay. Held here, the request survives until the
   *  sidebar consumes and clears it in an effect that also runs on mount
   *  (issue #912). */
  revealRequest: { key: string; nonce: number } | null
  /** Never-reset counter feeding `revealRequest.nonce`, so revealing the same
   *  session twice produces two distinct requests (a key-only request would
   *  make the second reveal indistinguishable from the first). Monotonic
   *  across clears. */
  revealNonce: number
  /** Tool call to highlight & auto-expand inline. Set by openActivityToTool;
   *  consumed (cleared) once the matching ToolCallLine has expanded itself. */
  focusToolCallId: string | null
  /** MCP Apps (SEP-1865) render payloads keyed by tool_call_id. Populated from
   *  `mcp_app_render` WS broadcasts; consumed by ToolCallLine → McpAppFrame.
   *  tool_call_ids are globally unique (ACP-issued), so a flat map is safe
   *  across slots. */
  mcpApps: Record<string, McpAppRenderPayload>
  slotActivity: Record<string, { toolLog: ToolActivity[]; subagents: Record<string, SubagentActivity>; activityTab?: 'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'links' | 'side' | 'artifacts'; activityOpen?: boolean }>
  slotSide: Record<string, SideState>
  slotSideClosed: Record<string, boolean>
  slotMessages: Record<string, ChatMessage[]>
  /** Fresh `has_more` for a BACKGROUND pane, written by every bounded warm.
   *  The pane's own query is staleTime:Infinity, so its has_more freezes at
   *  mount while a later warm can truncate the cache past the bound. */
  slotPaneHasMore: Record<string, boolean>
  /** Row count of a bounded pane hydrate, so the unbounded refetch a starting
   *  turn issues can supersede it and still keep the rows it never fetched.
   *  Absent once superseded, so the upgrade happens at most once per slot. */
  slotPaneBounded: Record<string, number>
  /** The server's own message count for a slot, as of the last slot-detail fetch.
   *
   *  This exists to tell two indistinguishable populations apart at the warm
   *  merge. Both are rows this pane holds after the anchor that the fetched page
   *  omits, and neither position, `meta.mid`, `ts` nor the optimistic flag
   *  separates them:
   *
   *    - a row that arrived by live stream after the page was built -- the server
   *      HAS it, so its count did not fall, and the row must be kept;
   *    - a row another client rewound or regenerated away -- the server no longer
   *      has it, so its count FELL, and the row must not be put back on screen.
   *
   *  A fall in this count is therefore the truncation signal. It is retained per
   *  slot because a single response cannot show a delta, and it is written by
   *  every slot-detail reducer so the value a warm compares against is the last
   *  one actually observed rather than a stale figure from an earlier pane.
   *
   *  Residual, stated rather than implied: a rewind followed by enough new turns
   *  to restore the count before this pane is warmed again reads as unchanged, so
   *  that interleaving is not covered. Absent a retained count there is nothing
   *  to compare and the merge declines to discriminate, keeping the rescue. */
  slotServerTotal: Record<string, number>
  /** Dispatch order of the warm whose response set `slotServerTotal`. Present
   *  only when that count came from a warm carrying one, so an absent entry
   *  means the ordering is unknown and the merge must not act on it. */
  slotServerTotalSeq: Record<string, number>
  /** Reasoning blocks whose anchoring row is above the loaded window, per slot.
   *  Client-only, so this is their only copy until the anchor pages back in. */
  thinkingOrphans: Record<string, Array<ParkedThinking<ChatMessage>>>
  /** Path B: per-slot live stream state so a non-active pane shows its own
   *  streaming/tool/idle indicator (mirrors slotActivity for tool events). */
  slotRun: Record<string, { state: SlotState; lastChunkSeq?: number }>
  /** Path B: per-slot one-time hydration guard so the server history is
   *  prepended exactly once even if a WS frame seeds slotMessages first. */
  slotHydrated: Record<string, boolean>
  slotLoading: boolean
  slotHistory: string[]
  /** Whether a non-empty slots frame has arrived. Distinguishes a reconnect's
   *  empty frame, which must not tear anything down, from a genuinely empty
   *  list, which must. */
  slotsSnapshotSeen: boolean
  stopPressedAt: Record<string, number | null>
  /** Pending ask_question cards keyed by slot. Keyed (rather than a single
   *  card) so concurrent ask_question calls from two slots cannot evict each
   *  other — the losing agent would block until its timeout. */
  pendingQuestions: Record<string, { slot: string; ask_id?: string; questions: Array<{ question: string; header?: string; options: Array<{ label: string; description?: string }>; multiSelect?: boolean }>; cardId?: string; serverCardId?: string; draftActive?: boolean }>
  // Agent-authored follow-up suggestions (suggest_followup MCP tool), rendered
  // as a card above the composer. Keyed BY SLOT: a single global card let a
  // suggestion arriving in session B silently evict session A's unacted-on card,
  // contradicting the documented per-session behaviour.
  //
  // `ts` is the broadcast timestamp, used to avoid clearing a card that arrived
  // while a slower action (worktree create) was still in flight.
  //
  // Ephemeral: this lives only in frontend state, so a full page reload drops it.
  // Deliberately NOT cleared by clearSlotState — a suggestion is not tied to an
  // in-flight turn, so tabbing away and back should still show it. Rendering is
  // gated on the active slot's own key, so a retained card can never surface
  // under the wrong session.
  followups: Record<string, { items: FollowupItem[]; ts: number }>
  // Post-titling "file this in <folder>?" offer, keyed by slot for the same
  // reason `followups` is: a card must never be evicted by, or surface under,
  // another session.
  //
  // Every string here is the user's own stored folder data — the backend model
  // call returns an INDEX into a folder list, never text — so nothing rendered
  // from this is model-generated (see chat_folder_suggest.py).
  //
  // Ephemeral like `followups`: frontend-only, dropped by a reload. The backend
  // offers at most one card per slot for the lifetime of that slot, so a
  // dismissed or lost card is never re-offered.
  //
  // `turns` counts the user sends that have happened since the card arrived, so
  // an unanswered card ages out instead of sitting above the composer for the
  // rest of the session (see FOLDER_SUGGESTION_MAX_TURNS). Ignoring a suggestion
  // IS an answer — the user who keeps typing has declined by conduct — and the
  // backend needs no telling because it never re-offers this slot anyway.
  folderSuggestions: Record<string, { folderId: string; folderName: string; breadcrumb: string; ts: number; turns: number }>
  // Slot with a locally-started turn awaiting server confirmation. While set,
  // the slots-sync ignores a server running=false for it (the snapshot may
  // predate the send). Cleared on server confirmation or turn end.
  pendingTurnSlot: string | null
}

const MAX_RETIRED_QUEUE_IDS = 50

/** User sends a folder-suggestion card survives before it ages out on its own.
 *
 *  The card is an offer, not a task: a user who keeps typing past it has already
 *  answered by conduct, and a permanent card in the composer band is a standing
 *  cost paid by every session the model guessed wrong about. Three is the count
 *  where the offer is still plausibly in view (the user may be mid-thought when
 *  it lands, so one turn is too eager) without becoming furniture.
 *
 *  Counted by an explicit `ageFolderSuggestion` dispatch from the ONE surface
 *  that renders the card (ChatPage's composer band, active slot), and only
 *  after the server confirmed the send was delivered. So: a failed send never
 *  counts (delivery unconfirmed), a send from a surface that does not show the
 *  card (ChatPane companion/embed panes, Slack, cron) never counts (nothing
 *  rendered, nothing dispatched), and a replacement card that landed while the
 *  send was in flight is not aged (ts-pinned to the generation the user saw). */
export const FOLDER_SUGGESTION_MAX_TURNS = 3

const initialState: ChatState = {
  activeSlot: null,
  messages: [],
  slotRunning: false,
  slotStopping: false,
  slotState: 'idle',
  slotStatusDetail: {},
  slotHasMore: false,
  slotOldestIndex: 0,
  slotCursorKey: null,
  slotSwitchRequestId: null,
  slotSwitchTarget: null,
  slotSwitchOrigin: null,
  loadingOlder: false,
  slotOlderError: false,
  lastChunkSeq: undefined,
  _wsChunkedDuringFetch: false,
  _redeliveredFramesDropped: 0,
  history: [],
  historyHasMore: false,
  historyOffset: 0,
  unresumableResume: null,
  lastResumeRequestId: null,
  pendingInput: null,
  agentSwitchNotice: null,
  creatingSlot: false,
  slotContextPct: {},
  slotContextTokens: {},
  voicePlaying: false,
  voiceAudio: null,
  subagents: {},
  subagentQueued: {},
  goalLoops: {},
  selectedSubagentId: null,
  toolLog: [],
  workflowRuns: {},
  activityOpen: false,
  activityTab: 'changes' as const,
  activityTabRequest: 0,
  revealRequest: null,
  revealNonce: 0,
  focusToolCallId: null,
  mcpApps: {},
  slotActivity: seedSlotActivity(),
  slotMessages: {},
  slotPaneHasMore: {},
  slotPaneBounded: {},
  slotServerTotal: {},
  slotServerTotalSeq: {},
  thinkingOrphans: {},
  slotRun: {},
  slotHydrated: {},
  slotLoading: false,
  slotSide: {},
  slotSideClosed: {},
  slotHistory: [],
  slotsSnapshotSeen: false,
  pendingQuestions: {},
  followups: {},
  folderSuggestions: {},
  stopPressedAt: {},
  pendingTurnSlot: null,
}

function pushHistory(history: string[], key: string): string[] {
  const deduped = history.filter(k => k !== key)
  deduped.push(key)
  return deduped.length > 50 ? deduped.slice(-50) : deduped
}

/** Mirror a NON-ACTIVE slot's run transition into the failed-switch origin
 *  snapshot, when that slot is the origin. The snapshot is captured by
 *  `switchSlot.pending` and applied verbatim by `rejected`'s restore; without
 *  this event-time write, a turn that settles mid-switch through a SAME-VALUE
 *  round trip (idle -> [runs and completes] -> idle for a queued turn) is
 *  indistinguishable from "never moved" after the fact, and the restore would
 *  resurrect the stale busy state (#6364 review). Every `slotRun` state
 *  writer for non-active slots routes through here, so the snapshot ages the
 *  same way the per-slot entry does. */
function syncOriginRun(state: ChatState, slot: string, runState: SlotState): void {
  const o = state.slotSwitchOrigin
  if (!o || safeKey(o.key) !== safeKey(slot)) return
  o.run = { state: runState, running: runState !== 'idle', stopping: runState === 'stopping' }
}

/** Load a slot's cached activity-panel state (or the empty defaults) into the
 *  live view. Shared by `switchSlot.pending` (entering the target) and
 *  `switchSlot.rejected` (falling back to the origin when the target is gone,
 *  #6309), so the two entry paths cannot drift apart. */
function loadSlotActivity(state: ChatState, key: string): void {
  const cached = state.slotActivity[key]
  state.toolLog = cached?.toolLog ?? []
  state.subagents = cached?.subagents ?? {}
  // Inline expansion replaced the old 'tools' tab, and 'files' is no
  // longer one of this viewer's tabs (the file browser is its own pinned
  // panel now, and this viewer hosts 'links' instead). Any of those
  // legacy cached values fall back to 'changes'.
  const legacyTab = (t: unknown) => t === 'tools' || t === 'nav' || t === 'files'
  state.activityTab = (cached?.activityTab && !legacyTab(cached.activityTab)) ? cached.activityTab : 'changes'
  // Panel open/closed is per-chat; a chat we've never opened defaults to closed.
  state.activityOpen = cached?.activityOpen ?? false
}

/**
 * Path B (native session grid): apply a WS chat frame for a NON-active slot
 * into the per-slot store so a pane rendering that slot streams live. The
 * ACTIVE-slot path in sseChatMessage is intentionally left byte-identical
 * (zero blast radius on the main chat); this mirrors the slotActivity tool
 * pattern already used for tool/subagent events on non-active slots.
 */
function applyNonActiveFrame(
  state: ChatState,
  p: { slot: string; role: string; content: string; ts?: string; seq?: number; cls?: string; meta?: Record<string, unknown>; kind?: string; batched?: boolean },
) {
  const { slot, role, content, ts, seq, cls, meta, kind, batched } = p
  if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
  const msgs = (state.slotMessages[safeKey(slot)] ??= [])
  const run = (state.slotRun[safeKey(slot)] ??= { state: 'idle' })
  const sa = (state.slotActivity[safeKey(slot)] ??= { toolLog: [], subagents: {} })
  const toolLog = sa.toolLog

  const effectiveKind = kind ?? (meta?.kind as string | undefined)
  if (effectiveKind === 'stop_event') {
    const id = (meta?.id as string) ?? ''
    const idx = id ? msgs.findIndex(m => m.meta?.id === id) : -1
    const msg: ChatMessage = ensureMsgId({ role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' })
    if (idx >= 0) msgs[idx] = msg
    else msgs.push(msg)
    return
  }
  if (role === '_segment') {
    finalizeTrailingStreaming(msgs)
    return
  }
  if (role === 'chunk') {
    run.state = 'streaming'
    syncOriginRun(state, slot, 'streaming')
    // Drop only the EMPTY thinking placeholder (mirror the active
    // sseChatMessage path at chatSlice ~998), keeping content-bearing reasoning
    // blocks so a background pane's hydrated reasoning isn't silently deleted by
    // the next streamed chunk.
    if (msgs.some(m => m.role === 'thinking' && !m.content)) {
      const filtered = msgs.filter(m => !(m.role === 'thinking' && !m.content))
      msgs.length = 0
      msgs.push(...filtered)
    }
    const last = toolLog[toolLog.length - 1]
    if (last?.type === 'reasoning') last.text += content
    else {
      toolLog.push({ type: 'reasoning', text: content, ts: Date.now() })
      // Cap the non-active slot's tool log (mirrors the sseToolActivity cap)
      // so a long background-pane turn can't grow slotActivity without bound.
      if (toolLog.length > 100) toolLog.splice(0, toolLog.length - 100)
    }
    let streamIdx = -1
    for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'streaming') { streamIdx = i; break } }
    if (streamIdx >= 0) {
      const msg = msgs[streamIdx]
      // Share missedChunkMarker with the active path so the two cannot drift.
      // Skip on batched frames: the live WS flush buffer already owns gap
      // detection across the chunks it merges and inlines the marker into the
      // batch content, and it dispatches each batch carrying only the batch's
      // LAST seq. Comparing consecutive batches' last-seqs here would treat the
      // batch size as a gap and fabricate a false "[N chunk(s) missed]" marker
      // on every multi-chunk background-pane batch. Mirror the active path,
      // which guards the identical branch with `!batched`.
      if (!batched && seq !== undefined && run.lastChunkSeq !== undefined) {
        msg.content += missedChunkMarker(run.lastChunkSeq, seq)
      }
      msg.content += content
      msg.rawText = msg.content
    } else {
      msgs.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content, meta: { clientTs: mintMsgId() } })
    }
    if (seq !== undefined) run.lastChunkSeq = seq
    return
  }
  if (role === '_done') {
    run.state = 'idle'
    run.lastChunkSeq = undefined
    syncOriginRun(state, slot, 'idle')
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') { msgs[i].role = 'assistant'; msgs[i].rawText = msgs[i].content; break }
    }
    return
  }
  if (role === 'compacting') { run.state = 'compacting'; syncOriginRun(state, slot, 'compacting'); return }
  // Permission rows carry request_id/tool_input inside `cls` (JSON); lift it
  // here — BEFORE the guard — so the identity comparison sees the same
  // `tool_call_id` the stored row has.
  let effectiveMeta = meta
  if (role === 'permission' && !meta?.approval_id && cls) {
    try {
      const parsed = JSON.parse(cls)
      if (parsed.request_id) {
        effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
      }
    } catch { /* not JSON cls, ignore */ }
  }
  // Idempotent append — ONE chokepoint that dominates every branch below, which
  // is the point: each of those branches creates or mutates a row and returns,
  // so a guard placed after any of them is a guard some frame slips past.
  if (isRedeliveredMessage(msgs, effectiveMeta)) { state._redeliveredFramesDropped += 1; return }
  // A turn-consuming frame makes a pending stateless question card stale —
  // placed after the redelivery guard so a replayed frame cannot clear a
  // live card (see dropStaleStatelessQuestion).
  dropStaleStatelessQuestion(state, slot, role)
  if (role === 'tool') {
    run.state = 'tool_running'
    syncOriginRun(state, slot, 'tool_running')
    let insertIdx = msgs.length
    if (insertIdx > 0 && msgs[insertIdx - 1]?.role === 'streaming') insertIdx--
    msgs.splice(insertIdx, 0, ensureMsgId({ role, content, cls: cls || '', ts, meta }))
    return
  }
  if (role === 'thinking') {
    if (!msgs.some(m => m.role === 'thinking')) msgs.push({ role: 'thinking', content: '', cls: '', meta: { clientTs: mintMsgId() } })
    return
  }
  if (role === 'assistant') {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') {
        msgs[i].role = 'assistant'; msgs[i].content = content; if (ts) msgs[i].ts = ts
        // Carry the frame's meta — crucially `mid`, this row's server identity.
        // The row was minted client-side by the first `chunk` and has none until
        // now; without it a later redelivery of THIS frame is unrecognisable and
        // would overwrite whatever is streaming at that moment.
        if (meta) msgs[i].meta = { ...(msgs[i].meta || {}), ...meta }
        return
      }
    }
  }
  if (role === 'user') {
    // A steered message does not start a new turn — skip the "stale permissions"
    // cleanup so the approval bar remains visible and answerable (#1667).
    if (!meta?.steer) {
      sa.toolLog = []
      for (const m of msgs) {
        if (m.role === 'permission' && !m.meta?.resolved) { if (m.meta) m.meta.resolved = 'rejected'; else m.meta = { resolved: 'rejected' } }
      }
    }
    // Reconcile the optimistic user bubble (appendSlotMessage) rather than
    // pushing a 2nd identical one when the server echoes the user frame (#2845).
    // Uses shared helper that scans past non-matching pipelined sends (#3898).
    const echoSendId = meta?.sendId as string | undefined
    if (echoSendId && meta?.mid) {
      if (reconcileOptimisticEcho(msgs, echoSendId, meta as Record<string, unknown>, ts)) return
    } else if (meta?.mid) {
      // Fallback: no sendId on the echo — use tail content match for paths
      // that don't generate a sendId (split-pane, queued promotions).
      const last = msgs[msgs.length - 1]
      if (last?.role === 'user' && last.content === content && !last.meta?.mid) {
        if (ts) last.ts = ts
        if (meta) last.meta = { ...(last.meta || {}), ...meta }
        return
      }
    }
  }
  msgs.push(ensureMsgId({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind }))
}

/** Path B selectors: read a slot's messages / stream-state, falling back to the
 *  global active mirror when the slot IS the currently-active one. */
const EMPTY_MESSAGES: ChatMessage[] = []
export const selectSlotMessages = (state: RootState, slot: string): ChatMessage[] =>
  slot === state.chat.activeSlot ? state.chat.messages : (state.chat.slotMessages[slot] ?? EMPTY_MESSAGES)
export const selectSlotStreamState = (state: RootState, slot: string): SlotState =>
  slot === state.chat.activeSlot ? state.chat.slotState : (state.chat.slotRun[slot]?.state ?? 'idle')

const EMPTY_TOOLLOG: ToolActivity[] = []
/** Per-slot tool log, falling back to the global active mirror. */
export const selectSlotToolLog = (state: RootState, slot: string | null): ToolActivity[] =>
  slot && slot !== state.chat.activeSlot ? (state.chat.slotActivity[slot]?.toolLog ?? EMPTY_TOOLLOG) : state.chat.toolLog
const EMPTY_SUBAGENTS: Record<string, SubagentActivity> = {}
/** Per-slot subagent map, falling back to the global active mirror — the
 *  read-only selector twin of the internal `getSlotSubs`. Exists so the
 *  Activity panel can subscribe to this itself instead of having ChatPage hold
 *  the subscription and pass it down: ChatPage renders on every streamed token,
 *  and `sseSubagentBatchChunks` bumps this reference per sub-agent chunk, so a
 *  ChatPage-level subscription re-rendered the whole page for a panel that is
 *  closed by default. */
export const selectSlotSubagents = (state: RootState, slot: string | null): Record<string, SubagentActivity> =>
  slot && slot !== state.chat.activeSlot ? (state.chat.slotActivity[slot]?.subagents ?? EMPTY_SUBAGENTS) : state.chat.subagents
/** Per-slot pending tool-approval (unresolved permission after the slot's last
 *  user message) — slot-aware version of ChatInput's old selectPendingApproval,
 *  so each grid pane's approval bar reflects ITS slot, not the global active one. */
export const selectSlotPendingApproval = (state: RootState, slot: string | null): ChatMessage | null => {
  const msgs = slot ? selectSlotMessages(state, slot) : state.chat.messages
  // Find the last NON-steer user message — steered messages don't start a new
  // turn, so they must not hide a pending approval bar (#1667).
  let lastUserIdx = -1
  for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'user' && !msgs[i].meta?.steer) { lastUserIdx = i; break } }
  for (let i = msgs.length - 1; i > lastUserIdx; i--) {
    const m = msgs[i]
    if (m.role === 'permission' && !m.meta?.resolved && m.meta?.approval_id) return m
  }
  return null
}

export const fetchHistory = createAsyncThunk(
  'chat/fetchHistory',
  async (append: boolean, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    const offset = append ? state.historyOffset : 0
    // Older sessions is the complement of the open tabs listed above it, so the
    // server drops anything a live slot already holds. Excluded server-side
    // because `historyOffset` advances by the row count received: dropping rows
    // here would desynchronise the offset and skip or repeat rows on the next page.
    const d = await api.sessions(30, offset, false, true)
    return { sessions: (d.sessions || d) as SessionInfo[], hasMore: d.has_more || false, offset, append }
  },
)

/** Rows for the initial slot-open page and each older-history page. One size
 *  for both keeps the scrollback walk uniform: the first page a slot opens
 *  with is simply page one of the same pagination `loadOlderMessages` runs. */
export const OLDER_PAGE_LIMIT = 100

/** Page size for walking BACK through history (loadOlderMessages).
 *
 * Equal to OLDER_PAGE_LIMIT: a load is a load, and the reader cannot tell which
 * door issued it. The larger page this used to carry was justified by amortizing
 * round trips across a walk that ran to the START of history ("a 13-page walk
 * becomes 5") -- but the walk is now bounded to a few pages per expression of
 * intent, so it cannot reach the start on one gesture no matter how big its page
 * is, and the amortization has nothing left to amortize. What the big page did
 * instead was multiply the cost of a single flick: measured on a phone, one
 * gesture pulled 706 rows / ~260,000px of transcript with the reader's finger
 * nowhere near the screen, which reads as history loading without end.
 *
 * The unit is worth stating because it is the trap: this counts MESSAGES, while
 * a reader consumes SCREENS. Measured on the reporter's device a display row is
 * 0.6-1.2 viewports, so ~3 messages fill a screen -- one page of 100 is already
 * some 30 screens of reading. A page that looks modest in messages is enormous
 * in the unit the reader actually experiences. */
export const OLDER_WALK_PAGE_LIMIT = OLDER_PAGE_LIMIT

/** The handler's own ceiling (`min(int(limit), 500)` in chat_handlers). Asking
 *  for more is silently clamped, so a caller that needs to KNOW whether its
 *  window covered the cache has to compare against this, not against what it
 *  asked for. */
export const SLOT_DETAIL_MAX_LIMIT = 500

/**
 * Rows to request when switching to a slot, or `undefined` for the unbounded
 * shape.
 *
 * A switch used to go UNBOUNDED for any slot with rows already painted, and the
 * comment beside it carried its own measurement: 6.2MB/~1s unbounded against
 * 0.7MB/57ms bounded. So the FIRST visit to a session was the fast one and every
 * return to it paid for the whole chained transcript — on a 43MB session that is
 * the reported "switching chats got slow and janky", and it got worse as more
 * history became reachable.
 *
 * The reason for going unbounded was real but narrower than the rule: a bounded
 * page is a WINDOW, and if the server grew past it the window could sit entirely
 * newer than the cache, leaving a hole in the middle of the transcript. That is
 * a question of COVERAGE, not of boundedness — and coverage is VERIFIED after the
 * response (`slotCoverageShortfall` asks which cached rows the window does not
 * contain), so it does not have to be pre-purchased with a larger window.
 *
 * Which matters because the window extends BACKWARD from the newest row: every
 * row of headroom is a row of OLDER history nobody asked for. Buying a page of
 * margin therefore grew the transcript upward by a page on every revisit, and
 * since the next revisit measures the cache it just grew, it ratcheted — one page
 * per switch until the handler ceiling. Reported from a phone as history loading
 * itself on every session switch, from a reader parked at the live end, with no
 * gesture and no spinner (this path never sets `loadingOlder`, so it is invisible
 * to every guard on the automatic older-history doors).
 *
 * So ask for exactly what this tab already holds — never fewer than one page —
 * and let the coverage check pay for the rare case instead.
 *
 * A STREAMING slot is not an exception to that, though it used to be. The
 * carve-out rested on the same pre-purchase argument the paragraph above
 * retires: unseen growth can push a window clear of a small cache. That is the
 * hole the coverage check verifies for, and it verifies it for a streaming
 * response exactly as it does for a settled one — so the streaming exemption was
 * the retired argument surviving in the one branch that did not get revisited.
 *
 * What it cost is the whole point of bounding: a slot mid-turn is the most likely
 * slot a reader switches away from and back to, so the exemption applied the
 * unbounded shape to the commonest switch there is. Measured on a phone as one
 * switch into a streaming session turning 303 loaded messages into 6,265 (7,303
 * raw rows, ~293,000px of transcript) with no gesture, no spinner, and no paging
 * door involved -- and, because the whole transcript is replaced at once, the
 * reader's saved position with it.
 *
 * Order matters here: bounding this is only safe once a streaming response can
 * leave a comparable baseline behind (`retainServerTotal`). Without one the
 * coverage check cannot prove overlap, and every streaming switch would take the
 * unbounded RETRY instead — the same payload, one round-trip later.
 */
export function slotSwitchFetchLimit(input: {
  cached: number
  pageLimit?: number
  maxLimit?: number
}): number | undefined {
  const pageLimit = input.pageLimit ?? OLDER_PAGE_LIMIT
  const maxLimit = input.maxLimit ?? SLOT_DETAIL_MAX_LIMIT
  if (input.cached <= 0) return pageLimit
  return Math.min(maxLimit, Math.max(pageLimit, input.cached))
}

/** The fields coverage needs off a transcript row. Structural rather than the full
 *  `ChatMessage`, so the contract is readable and testable without a whole message. */
export type CoverageRow = {
  ts?: string
  role?: string
  content?: unknown
  meta?: { mid?: unknown }
}

/** A row's identity for the coverage test, in the same vocabulary `deduplicateByMid`
 *  uses:
 *  the server-minted `meta.mid` with `role` and the instant, since a mid can be
 *  supplied by a caller and is not trustworthy alone. A row with no mid falls back to
 *  its content, which is what the transcript itself renders and the only thing left to
 *  compare.
 *
 *  A mid is matched ALONE, without role or timestamp beside it. Every other field on a
 *  row is mutable while the mid is not: a `ts` is overwritten from the optimistic client
 *  value to the server's authoritative one (`sseChatMessage` stashes the old one as
 *  `meta.clientTs` precisely because it changes), a role flips `streaming` -> `assistant`
 *  on finalization, and content grows from partial to final. Pairing any of them with a
 *  stable id defeats the id: the same row read twice reports as two rows, and the
 *  resulting false shortfall reloads the whole transcript -- after nothing more exotic
 *  than sending a message and switching slots. `deduplicateByMid` does pair mid with role
 *  and ts, for a different job: it COLLAPSES rows in the rendered transcript, so it must
 *  not let a crafted mid hide a legitimate row. Coverage cannot be fooled that way
 *  because it COUNTS -- two cached rows carrying one mid still need two window rows
 *  carrying it -- so the discrimination that dedup needs costs coverage nothing to drop.
 *
 *  Without a mid the fallback keys on the INSTANT rather than the raw `ts`: the
 *  seconds-or-ISO union means one row can be spelled two ways, and an identity that
 *  changed with the spelling would call the same row two rows.
 *
 *  Two rows can still be genuinely indistinguishable -- the same role, instant and text,
 *  with no mid on either. Coverage counts them rather than deduplicating them, so a
 *  cache holding two and a window holding one reports the one that would be lost.
 *
 *  A JSON array rather than a delimiter-joined string: no delimiter can collide with
 *  field content, and no string literal here trips the zero-tolerance i18n gate. */
function coverageRowIdentity(r: CoverageRow): string {
  const mid = r.meta?.mid
  if (typeof mid === 'string' && mid) return JSON.stringify([mid])
  return JSON.stringify([null, r.role ?? null, transcriptTsMs(r.ts), String(r.content ?? '')])
}

/**
 * How many rows the tab already holds would be LOST if the bounded window replaced
 * them -- the multiset of cached rows the window does not contain.
 *
 * This is the definition, not a proxy for it. The totals cannot answer the question:
 * a tab holding one page of a long transcript and a tab whose slot grew past its cache
 * look identical as counts (`cached` small, `serverTotal` large), so a count comparison
 * has to assume the worst whenever it has no earlier total to subtract -- which is every
 * FIRST visit to a slot. That assumption read an entire transcript to close a gap that
 * was not there: measured on a phone as 110 loaded messages becoming 2,645 with a server
 * total of 2,644, on a slot whose window already covered its cache exactly.
 *
 * Ordering cannot answer it either, and three rounds of boundary defects came from
 * trying: comparing timestamps as strings, then treating a row tied with the window's
 * oldest as covered, then collapsing two identical rows into one. Each is a different
 * way for "is this row in that set" to be inferred from "is this row older than that
 * row" -- so the fix is to ASK the real question. A row is covered when the window
 * holds a matching row, and each window row can cover only ONE cached row, which is
 * what makes duplicates count.
 *
 * A row the server does not KEEP is skipped on both sides, read through the shared
 * `isDurableRow`. `CLIENT_ONLY_ROLES` -- `queued`, `streaming`, `thinking`,
 * `permission` -- exist only in this client, so the server's window cannot contain one
 * however wide it is asked to be. Counting one as missing is therefore not a hole that
 * a bigger read closes: it is a shortfall that never goes away, so EVERY switch into a
 * slot holding a queued message or a permission card refetches the whole transcript.
 * A narrower test came first here -- skip a row whose `ts` cannot be read -- which
 * happened to catch `streaming` and missed the other three, and the same file already
 * carried the right predicate two consumers deep.
 *
 * The unreadable-`ts` skip stays, for its own reason rather than that one: a row that
 * cannot be placed in time has no whole identity key to match on. An unstamped row is
 * also a live TAIL row, which a newest-N window necessarily reaches, so skipping it
 * cannot hide a hole above it.
 *
 * The one case that declines outright is a window with NO comparable row at all:
 * nothing to compare against, and such a window replacing a populated cache is the
 * shrink this guard is for.
 */
export function slotCoverageShortfall(input: {
  cached: readonly CoverageRow[]
  window: readonly CoverageRow[]
}): number {
  const { cached, window: win } = input
  // Rows this comparison can say anything about at all. Two independent reasons a row
  // is excluded, and they are NOT the same question:
  //   `isDurableRow` -- can the server's window contain this row even in principle?
  //   a readable `ts`  -- can the row be placed, so its identity key is whole?
  const comparable = (r: CoverageRow) => isDurableRow(r) && transcriptTsMs(r.ts) !== null
  const held = cached.filter(comparable)
  if (held.length === 0) return 0
  const floor = win.filter(comparable)
  // Nothing to compare against: an unplaceable window replacing a populated cache is
  // the shrink this guard is for. Counted over the COMPARABLE cache, not the whole of
  // it, or a slot holding only client-only rows against an empty window reports a
  // shortfall it cannot lose.
  if (floor.length === 0) return held.length
  // The window as a MULTISET: a row present twice can cover two cached rows, and a row
  // present once can only cover one.
  const have = new Map<string, number>()
  for (const r of floor) {
    const k = coverageRowIdentity(r)
    have.set(k, (have.get(k) ?? 0) + 1)
  }
  let outside = 0
  for (const r of held) {
    const k = coverageRowIdentity(r)
    const n = have.get(k) ?? 0
    if (n > 0) have.set(k, n - 1)
    else outside += 1
  }
  return outside
}



// Aborts the in-flight older-history fetch, or null when none is running.
// Module-level because switchSlot must reach a fetch it did not start.
let _abortLoadOlder: (() => void) | null = null

/**
 * True for a rejection that means "this paging attempt was cancelled or refused",
 * as opposed to "this request failed". A caller must not read either as evidence
 * that the history it wanted is unreachable.
 *
 * `AbortError` is a superseded fetch (the user switched chat). `ConditionError`
 * is Redux Toolkit refusing the dispatch outright — a page is already loading,
 * or the cursor belongs to a chat the user has left.
 *
 * Keys on `name` and deliberately does NOT use `instanceof`: `unwrap()`
 * rethrows Redux Toolkit's serialized error, a plain `{name, message, stack}`
 * object. The `instanceof DOMException` / `instanceof Error` form used
 * elsewhere in this codebase is always false here, so it would silently never
 * match.
 */
/**
 * Replaces the paging cursor as ONE unit: how far back history goes, the offset
 * to ask for next, and the slot both describe. These three must move together --
 * writing the offset without re-keying leaves paging refusing forever, and
 * re-keying without the offset pages the wrong chat at the wrong place.
 */
function setPagingCursor(state: ChatState, hasMore: boolean, nextBefore: number): void {
  // A switch installs a cursor only for the slot it targets, so a writer that
  // activated a different slot must write: nothing else will.
  if (state.slotSwitchRequestId !== null && state.slotSwitchTarget === state.activeSlot) return
  state.slotHasMore = hasMore
  state.slotOldestIndex = hasMore ? nextBefore : 0
  state.slotCursorKey = state.activeSlot
  // One global flag describes a per-slot fetch, so a re-base clears it here: the
  // next slot must not inherit the previous slot's red retry state.
  state.slotOlderError = false
}

export function isSupersededPagingRejection(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const name = (err as { name?: unknown }).name
  return name === 'AbortError' || name === 'ConditionError'
}

/** Messages a background pane hydrates. Bounds both pane hydrate paths: the
 *  pane's own query and `warmSlotCache`, so `has_more` matches what it holds. */
export const PANE_HYDRATE_LIMIT = 50

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
function rowIdentities(m: ChatMessage): string[] {
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
function tailNotInPage(tail: ChatMessage[], page: ChatMessage[]): ChatMessage[] {
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
function tsEpoch(ts: string | undefined): number | null {
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

/** The ONE writer of a slot's pane transcript and its "has older history" marker.
 *
 *  The two must describe the SAME array. A `true` beside a complete transcript
 *  renders an earlier-messages row that fetches nothing; a `false` beside a
 *  bounded page hides history the pane really is missing. Four reducers fill
 *  this array, and enforcing the pair at each one separately is what let a path
 *  ship writing the array and neither flag.
 *
 *  `hasMore` of `undefined` means "this write does not describe the marker" —
 *  the array is a merge of a bounded page onto retained older rows, so the
 *  page's own flag is not true of the result. The existing marker is left alone
 *  rather than guessed at.
 *
 *  Both maps are keyed through `safeKey`, so a poisoned key cannot land the
 *  array and the flag on different entries.
 *
 *  `boundedLen` is how many leading rows of `messages` came from a bounded page,
 *  and it is an INDEX INTO the array being written -- so replacing the array
 *  invalidates it. Every write therefore sets it or clears it, decided on this
 *  call's own argument rather than on what the key already holds. Leaving that to
 *  callers is what let three writers replace the array behind a stale index. */
function writeSlotPage(
  state: ChatState,
  key: string,
  messages: ChatMessage[],
  hasMore: boolean | undefined,
  boundedLen?: number,
): void {
  const k = safeKey(key)
  state.slotMessages[k] = messages
  if (!state.slotPaneBounded) state.slotPaneBounded = {}
  if (boundedLen === undefined) delete state.slotPaneBounded[k]
  else state.slotPaneBounded[k] = boundedLen
  if (hasMore === undefined) return
  if (!state.slotPaneHasMore) state.slotPaneHasMore = {}
  state.slotPaneHasMore[k] = hasMore
}

/** Occurrences of each usable `meta.mid` in a row list.
 *
 *  `meta` on an inbound message is CALLER-supplied and an id is minted only when
 *  one is ABSENT, so a client can post the same `mid` twice and an id is NOT a
 *  unique key. Counting is what lets a caller tell "this id names the row I mean"
 *  from "this id names SOME row" -- the distinction a membership test cannot make.
 */
function midOccurrences(rows: Array<{ meta?: Record<string, unknown> }>): Map<string, number> {
  const counts = new Map<string, number>()
  for (const row of rows) {
    const mid = row.meta?.mid
    if (typeof mid === 'string' && mid.length > 0) counts.set(mid, (counts.get(mid) ?? 0) + 1)
  }
  return counts
}

/** Does this id name exactly ONE row on each side, and are those two rows the same row?
 *
 *  The one identity rule every bounded-page comparison in this file runs on. An id
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
function idAnchorsOneRow(
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
function olderHeadAbovePage(
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
function isDurableRow(m: { role?: string }): boolean {
  return !CLIENT_ONLY_ROLES.has(m.role ?? '')
}

/** How many rows of a kept older head came from SERVER history, for shifting the
 *  paging cursor. The cursor is a row OFFSET, so client-only rows must not count
 *  toward it. Callers already strip `thinking`, so including it in the shared set
 *  costs nothing here and keeps one definition of durable. */
function serverRowCount(rows: ChatMessage[]): number {
  return rows.filter(isDurableRow).length
}

/** Is there a row the server PERSISTS but that carries no `meta.mid`?
 *
 *  Such a row is invisible to a `mid`-counted bound and unreachable to a
 *  `mid`-keyed cut: it cannot be counted into the limit, and it cannot be anchored
 *  into a kept head. It is nonetheless on screen. Legacy history written before
 *  `mid` existed is the real case. */
function hasUnidentifiedDurableRow(rows: ChatMessage[]): boolean {
  return rows.some(m => isDurableRow(m) && !(typeof m.meta?.mid === 'string' && m.meta.mid.length > 0))
}

/** The `(hasMore, cursor)` pair to install after keeping an older head above a
 *  bounded page. Lives here so a second head-keeping reducer cannot re-derive it.
 *  The cursor is a row OFFSET, so a kept head shifts it down by the head's own
 *  server-row count -- and a `Math.max(0, ...)` clamp conflates two OPPOSITE ends:
 *  - EXACT (`headRows === nextBefore`): the head covers `[0, nextBefore)`, so
 *    everything older is held and `hasMore` must go FALSE. True at cursor 0
 *    advertises history behind an offset `loadOlderMessages` refuses
 *    (`slotOldestIndex <= 0`) -- a PERMANENT dead click, not the one-shot one an
 *    unshifted cursor costs.
 *  - DISAGREEMENT (`headRows > nextBefore`): completeness is NOT proven, so
 *    flipping `hasMore` false would STRAND real history. Fall back to the page's
 *    own cursor -- the one-shot dead click, which self-heals on the next page.
 */
function pagingCursorAfterKeptHead(
  hasMore: boolean,
  nextBefore: number,
  headRows: number,
): { hasMore: boolean; nextBefore: number } {
  if (headRows <= 0) return { hasMore, nextBefore }
  // Completeness proven: nothing older remains to fetch.
  if (headRows === nextBefore) return { hasMore: false, nextBefore: 0 }
  // Counts disagree, so decline to claim completeness rather than strand rows.
  if (headRows > nextBefore) return { hasMore, nextBefore }
  return { hasMore, nextBefore: nextBefore - headRows }
}

/** SINGLE writer for the retained per-slot server count, so the three reducers
 *  that consume a slot-detail payload cannot drift apart on it. A warm reads this
 *  to tell a truncated row from one the page was merely built too early to carry,
 *  which only works if whichever fetch ran last left its count behind. A count of
 *  0 is written like any other: the server reporting an empty slot is a fact, and
 *  treating it as absent would read a later non-zero count as growth.
 *
 *  A running count is refused only when the read was UNBOUNDED, which is where the
 *  incomparability actually lives: the unbounded branch counts raw rows, so a
 *  streaming response is inflated by rows that collapse at turn end, and retaining
 *  it makes the next warm read that ordinary collapse as a truncation and suppress
 *  the rescue, dropping a live row. A BOUNDED read is collapsed by the handler
 *  before it slices (`_collapse_wire_rows`), so its count is already in the same
 *  units as a settled one and refusing it buys nothing.
 *
 *  Refusing every running count -- which is what this did -- manufactured the
 *  absence it was trying to avoid guessing from. A slot that streams for most of
 *  its life then has NO baseline at all, and the switch's coverage check treats an
 *  absent baseline as unproven overlap and refetches the whole transcript: measured
 *  on a phone as one switch turning 305 loaded messages into 6,203, with the tab
 *  eventually killed. So the narrow refusal is not an optimization -- declining a
 *  comparable count is what produced the guess.
 *
 *  `boundedRead` absent still refuses while running, so a caller that cannot say
 *  keeps the conservative answer. */
function retainServerTotal(state: ChatState, key: string, total: number | undefined, running?: boolean, seq?: number, boundedRead?: boolean): void {
  if (running && !boundedRead) return
  if (typeof total !== 'number' || !Number.isFinite(total)) return
  if (!state.slotServerTotal) state.slotServerTotal = {}
  if (!state.slotServerTotalSeq) state.slotServerTotalSeq = {}
  const priorSeq = state.slotServerTotalSeq[safeKey(key)]
  // An older response must not lower the baseline a newer one already set, or
  // the next warm compares against a count that was never the newest view.
  if (typeof seq === 'number' && typeof priorSeq === 'number' && seq < priorSeq) return
  state.slotServerTotal[safeKey(key)] = total
  // Only an ORDERED response moves the order: clearing it on an unordered write
  // erased the field the staleness check reads, so a late warm read as a truncation.
  if (typeof seq === 'number') state.slotServerTotalSeq[safeKey(key)] = seq
}

async function fetchSlotDetail(key: string, limit?: number) {
  // A limit takes the handler's most-recent-N slice. `undefined` keeps the
  // unbounded shape, which a STREAMING warm/switch fetch still takes
  // (deliberate, though the handler collapses before slicing). refreshSlot
  // replaces the active transcript in place, so it cannot take a FIXED bound
  // (that would shrink history the user already paged in) — it passes a
  // COUNT-MATCHED one instead, see REFRESH_LIMIT_CEILING. Omit the arg when
  // unbounded to keep the one-arg shape.
  const d = await (limit === undefined ? api.chatSlotDetail(key) : api.chatSlotDetail(key, limit))
  type QueueItem = string | { content: string; id: string }
  return { key, boundedRead: limit !== undefined, nextBefore: d.next_before || 0, messages: filterMessages(d.messages || []), running: d.running || false, stopping: d.stopping || false, hasMore: d.has_more || false, total: d.total || 0, queue: ((d.queue || []) as QueueItem[]).map((q: QueueItem) => typeof q === 'string' ? { content: q, queueId: crypto.randomUUID(), ts: new Date().toISOString() } : { content: q.content, queueId: q.id, ts: new Date().toISOString() }), context: d.context_pct != null ? { pct: d.context_pct, used: d.context_used_tokens ?? undefined, window: d.context_window_tokens ?? undefined } : undefined }
}

/** SINGLE hydration path for the slot-detail context-meter fields — the one
 *  place that seeds `slotContextPct`/`slotContextTokens` from HTTP. Every
 *  reducer consuming a `fetchSlotDetail` payload routes through here, for the
 *  same reason `hydrateQueuedBubbles` exists: three near-identical reducers
 *  hand-copying the same literal is how a field gets added to one and forgotten
 *  in the others.
 *
 *  Why it exists at all: `context_usage` WS frames are turn-scoped, so a
 *  session reopened in a fresh tab has no entry and the bar renders empty until
 *  the user sends a message.
 *
 *  A stale reading (recovered from the snapshot file because the session's ACP
 *  process is gone) arrives with `used` absent, because no process measured a
 *  count for it — the server omits it rather than relying on this client to
 *  drop it. The tooltip's existing `~` path is how that gets said out loud. The
 *  window is likewise often absent — kiro-cli reports a percentage far more
 *  often than absolute token counts — in which case no token entry is written
 *  at all and the meter keeps using its model-derived window.
 *
 *  Seeds ONLY when the slot has no entry yet. The backend broadcasts over WS
 *  before the HTTP response lands, so a turn's frame can arrive mid-fetch —
 *  an unconditional write would clobber measured live numbers with the older
 *  snapshot this request was built from. Absent-only is monotonic: it can fill
 *  a gap, never overwrite. */
function seedContextUsage(
  state: ChatState,
  key: string,
  context: { pct: number; used?: number; window?: number } | undefined,
): void {
  if (!context) return
  const k = safeKey(key)
  if (state.slotContextPct[k] !== undefined || state.slotContextTokens[k] !== undefined) return
  state.slotContextPct[k] = context.pct
  if (context.window) state.slotContextTokens[k] = { used: context.used, window: context.window }
}

/** `switchSlot`'s argument. The plain-string spelling is the overwhelmingly
 *  common one; the object form exists for the ONE caller class that must NOT
 *  have a 404 unwound: a switch into a slot the caller just created (e.g. the
 *  error handoff), where a 404 is a create/fetch race on a slot that exists
 *  and the seeded composer must stay visible. Handling it as a per-call option
 *  keeps the decision inside the reducer's atomic unwind instead of a caller
 *  patching half the state back afterwards -- the exact #6260 failure class
 *  this fix removes. */
export type SwitchSlotArg = string | { key: string; keepTargetOnMissing?: boolean }

/** The slot key of a `switchSlot` argument, in either spelling. Non-object
 *  values pass through untouched: a hand-rolled test dispatch can omit
 *  `meta.arg` entirely (see the fulfilled reducer's requestId note), and the
 *  reducers' pre-existing tolerance of that must survive this indirection. */
const switchSlotKey = (arg: SwitchSlotArg): string => typeof arg === 'object' && arg !== null ? arg.key : arg

export const switchSlot = createAsyncThunk<
  Awaited<ReturnType<typeof fetchSlotDetail>>,
  SwitchSlotArg,
  { rejectValue: StatusRejection }
>(
  'chat/switchSlot',
  async (arg, { dispatch, getState, rejectWithValue }) => {
    const key = switchSlotKey(arg)
    // Safe unconditionally: this fetch resets the pane's messages and cursor, so
    // any older page still in flight is superseded even when the key is unchanged.
    _abortLoadOlder?.()
    dispatch(markSlotRead(key))
    // Bounded to the page size so opening a long session costs one page, not the
    // whole chained transcript; `loadOlderMessages` walks back from the cursor
    // this fetch returns. Unbounded while the slot is streaming, for the same
    // reason warmSlotCache and ChatPane's hydrate are -- deliberately, not because a
    // bound would cut raw rows: the handler collapses chunk runs BEFORE it slices.
    // `slotRun` and not `selectSlotStreamState`: switchSlot.pending has already
    // assigned `activeSlot = key` by the time this body runs, so that selector
    // would always take its active-slot branch and report `slotState`, which
    // still describes the OUTGOING slot. `slotRun` is keyed per slot, so it
    // answers for the incoming one. Guarded because a partial preloaded state
    // can omit `slotRun` entirely, and throwing here would skip the fetch.
    try {
      // EVERY switch is bounded, including into a slot mid-turn: ask for what
      // this tab already holds (never fewer than one page) and let the coverage
      // check below prove the window overlaps the cache. A bounded page is a
      // WINDOW and unseen growth can push it clear of a small cache, but that is
      // verified after the response rather than pre-purchased with a wider one --
      // see slotSwitchFetchLimit, and the shrink contract in
      // chatSlice.boundedRefetchShrink.test.ts that the pair has to satisfy.
      // Measured 6.2MB/~1s unbounded against 0.7MB/57ms bounded.
      const state = (getState() as { chat: ChatState }).chat
      const cachedRows = state.slotMessages?.[safeKey(key)] ?? []
      const cached = cachedRows.length
      const limit = slotSwitchFetchLimit({ cached })
      const first = await fetchSlotDetail(key, limit)
      // Coverage, MEASURED from the rows the window returned against the rows this
      // tab already holds. The older count-based check had to assume a hole whenever
      // it had no earlier server total to subtract -- true on every first visit to a
      // slot -- and closed that assumed hole with an UNBOUNDED read, which is how a
      // 110-message tab became 2,645 (the whole transcript) on a slot whose window
      // already covered its cache exactly. See slotCoverageShortfall.
      const shortfall = slotCoverageShortfall({ cached: cachedRows, window: first.messages })
      if (shortfall > 0) {
        // Named in the inspector because this is the one path that can multiply the
        // loaded transcript in a single step with no paging door involved. Reaching it
        // now means a hole was OBSERVED between the cache and the window, not merely
        // assumed for want of an earlier total.
        if (inspectorOn()) {
          devLog('SWITCH', `unbounded short=${shortfall} lim=${limit ?? '-'} cached=${cached} total=${first.total ?? '?'}`)
        }
        // Unbounded deliberately: the hole's width is server rows this tab never saw,
        // so a locally-sized window cannot be proven to reach the cache, and this path
        // REPLACES rather than merges. Carry the bounded read's count forward -- it is
        // the only one of the two in settled units, and returning only the retry threw
        // away the baseline the next switch needs.
        const wide = await fetchSlotDetail(key)
        return { ...wide, comparableTotal: first.total }
      }
      return first
    } catch (e) {
      // A thrown error crosses the thunk boundary as `miniSerializeError(e)`,
      // which keeps string fields only -- `ApiError.status` (a number) never
      // reaches the consumer, which left `isMissingSlotError` matching prose
      // (#6199). Reject with a structured payload instead: `unwrap()` throws a
      // `rejectWithValue` payload verbatim, status intact. The check is
      // STRUCTURAL rather than `instanceof ApiError` because store tests
      // replace the `../api/client` module wholesale, and an `instanceof`
      // against a class the mock does not export throws inside this very
      // handler (see utils/agentSwitchFeedback.ts for the precedent).
      const status = (e as { status?: unknown } | null)?.status
      if (typeof status === 'number') return rejectWithValue({ status, message: errMessage(e) })
      throw e
    }
  },
)

/** Re-fetch messages for a slot without changing activeSlot. Only applies if still active. */
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
const isTurnBoundaryUser = (m: { role: string; meta?: Record<string, unknown> }): boolean =>
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
const isOutOfBandRow = (m: { role: string; kind?: string }): boolean =>
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
type ThinkingAnchor =
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
type ParkedThinking<M> = { msg: M; anchor: ThinkingAnchor; occ?: number; occTotal?: number }

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
function mergePreservedThinking<M extends { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }>(
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
      // over" — crew mode persists the user row as a durable queue entry and
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
 *  call site — the hazard recorded above at the three near-identical reducers.
 *
 *  A TEXT-ONLY anchor needs a complete window: while it is incomplete the genuine
 *  anchor may sit above it, so the one loaded row carrying that text belongs to a
 *  different turn. Once complete, a REPEATED text is resolved by the occurrence
 *  recorded at park time, and a count that GREW invalidates that ordinal. An exact
 *  `mid` or server `ts` bypasses both: text AND ts is strictly more evidence than
 *  text AND ordinal, and withholding it hid the block permanently. Tool ids are
 *  1:1 with bursts (#4578) and need none of this. */
function reinsertThinkingOrphans<M extends { role: string; content: string; ts?: string; meta?: Record<string, unknown> }>(
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
function mergePreservedClientTs<M extends { role: string; content: string; ts?: string; meta?: Record<string, unknown> }>(
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

/** Upper bound on the count-matched `refreshSlot` limit, matching the ceiling
 *  the slot-detail handler clamps `limit` to. A request above it comes back
 *  SHORT of what was asked for, which for an in-place replacement means the
 *  view SHRINKS — so a transcript paged back past this keeps the unbounded
 *  shape rather than truncate. */
export const REFRESH_LIMIT_CEILING = 500

export const refreshSlot = createAsyncThunk(
  'chat/refreshSlot',
  async (key: string, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (state.activeSlot !== key) return null
    // COUNT-MATCHED bound, not a fixed one. The recurring refresh (reconnect,
    // chat_done, variant switch) no longer pulls the whole chained transcript
    // every time — but because it REPLACES `messages` wholesale, a fixed
    // bound would delete scrollback the user paged in. Asking for at least as
    // many rows as the view already HOLDS is bounded and cannot shrink it, since
    // the handler's slice is the most-recent-N. PANE_HYDRATE_LIMIT is the FLOOR
    // (a floor cannot truncate) so a near-empty slot still asks for a sensible
    // page instead of one row.
    //
    // An EMPTY view is the one case that stays unbounded: there is no count to
    // match, so any number here would be the fixed bound this design rejects,
    // and this refresh is then the client's only read of a transcript it holds
    // nothing of (a reconnect after `clearMessages`, a refresh racing slot
    // activation). Bounding it would install a window nothing asked for.
    // Only rows the SERVER transcript carries can be counted against a limit the
    // HANDLER applies to server rows. `state.messages` also holds client-only rows
    // -- a `thinking` block, a `permission` card, a `queued` bubble -- and counting
    // those inflates the request past the view's own span, which drags the page
    // BELOW the view's oldest row. `meta.mid` is the server's own per-row stamp,
    // the same server-row notion `serverRowCount` and the reducer's
    // `priorServerRows` are built on.
    const view = state.messages
    /* `isDurableRow` is load-bearing here, not decoration. A client-only row can
     * carry a `mid` too, and counting one inflates `held` -- which does not merely
     * over-request, it makes `want === held` hold when the DURABLE span is smaller,
     * silently bypassing the floor guard below whose whole argument is that at
     * `want === held` a page of `held` rows cannot hide an unidentified row. The
     * limit reaches a handler that slices DISK, and disk has no client-only rows, so
     * only durable ones may be counted against it. */
    const serverRows = view.filter(
      m => isDurableRow(m) && typeof m.meta?.mid === 'string' && m.meta.mid.length > 0,
    )
    const held = serverRows.length
    const want = Math.max(held, PANE_HYDRATE_LIMIT)
    /* The FLOOR is the one over-request, and mixed history is where it bites.
     *
     * At `want === held` the page cannot strand a durable row: `spansView` only
     * passes when all `held` identified rows are INSIDE a page of exactly `held`
     * rows, which leaves no room for an unidentified row to be the page's oldest --
     * and `overlapsView` already requires the page's oldest row to anchor. So the
     * count-matched request is safe whatever the identities are.
     *
     * `want > held` breaks that arithmetic. With few identified rows the floor pulls
     * a page of 50 that holds all of them (so `spansView` passes) plus older
     * UNIDENTIFIED rows -- and the page's oldest row is then one the `mid`-keyed cut
     * cannot anchor, so the reducer keeps no head while the view holds rows above it.
     * They would be in no page and no head, which is the scrollback loss this bound
     * exists to avoid. Legacy rows written before the backend stamped `mid` are the
     * real case.
     *
     * So the floor declines on a window it cannot fully identify. Modern transcripts
     * -- every live session, which is the recurring cost #4690 is about -- keep the
     * bound, because their rows all carry a `mid`. */
    const floorOverRequests = want > held
    const bounded =
      held > 0 &&
      want <= REFRESH_LIMIT_CEILING &&
      !(floorOverRequests && hasUnidentifiedDurableRow(view))
    if (!bounded) return fetchSlotDetail(key)
    const page = await fetchSlotDetail(key, want)
    /* Is this page safe to hand a reducer that REPLACES the transcript with it?
     * It is, on any one of three counts -- and each is a different relationship
     * between the page's range and the view's, not a restatement:
     *
     *   1. the page reaches the START of history (`!hasMore`), so it covers the
     *      view whatever the identities are;
     *   2. the page CONTAINS the view's oldest row, so it spans everything the
     *      view holds -- the floor's over-request lands here, and a superset can
     *      lose nothing;
     *   3. the page's own oldest row is IN the view, so the ranges overlap and
     *      `olderHeadAbovePage` can cut a head to keep above it.
     *
     * None of the three: the server gained at least `held` rows during the gap, so
     * page and view are FULLY DISJOINT and the reducer -- correctly declining to
     * guess a cut it has no identity for -- would drop every loaded row. Refetch
     * unbounded there. One extra round trip in exactly the case a slice cannot be
     * stitched, which keeps the alternative off the table: splicing a disjoint
     * page onto the view publishes a transcript with a silent hole in it.
     */
    /* Counts, not membership. A `Set.has` / `Array.some` answers "SOME row carries
     * this id", and a caller-repeated `meta.mid` makes that true while pointing at
     * a DIFFERENT occurrence than the one meant -- so the page reads as safe and
     * the reducer then cuts at the wrong row and drops visible history. Both tests
     * below therefore go through the one anchor invariant. */
    /* Validate against the view as it is NOW, not the snapshot the limit was sized
     * from. `view` was read before the await, and `loadOlderMessages` can resolve
     * inside it: the rows it prepends are exactly the scrollback a wrong decision
     * strands, and they can also make an anchor that was unique in the old view
     * AMBIGUOUS in the new one. Judging a page against a view that no longer exists
     * is how it gets accepted and then cut wrong.
     *
     * The limit itself is not re-derived -- the request is already in flight and a
     * page that is now too small simply fails the checks below and refetches
     * unbounded, which is the safe direction. Re-reading is only about the DECISION.
     *
     * A slot switch during the await makes the whole answer moot, so it declines the
     * same way the pre-fetch check does. */
    const after = (getState() as { chat: ChatState }).chat
    if (after.activeSlot !== key) return null
    const viewNow = after.messages
    const serverRowsNow = viewNow.filter(
      m => isDurableRow(m) && typeof m.meta?.mid === 'string' && m.meta.mid.length > 0,
    )
    const viewCounts = midOccurrences(viewNow)
    const pageCounts = midOccurrences(page.messages)
    const anchors = (id: unknown): boolean =>
      idAnchorsOneRow(id, viewNow, page.messages, viewCounts, pageCounts, { requireTs: true })
    /* `serverRowsNow` can be empty even though the pre-fetch `held` was positive --
     * a `clearMessages` landing in the await empties the view -- so the oldest-row
     * anchor is guarded rather than indexed blind. */
    const spansView = serverRowsNow.length > 0 && anchors(serverRowsNow[0].meta?.mid)
    const overlapsView = anchors(page.messages[0]?.meta?.mid)
    return !page.hasMore || spansView || overlapsView ? page : fetchSlotDetail(key)
  },
)

/** Warm the per-slot message cache for a *background* slot once its turn
 *  finishes, so switching to it renders the completed answer instantly from
 *  cache instead of waiting for the on-switch fetch round-trip. Guarded to
 *  non-active slots; the fulfilled reducer writes only slotMessages[key] and
 *  never touches the active `messages`, so a background completion can't churn
 *  the view the user is currently looking at. Session-grid panes also rely on
 *  this to reconcile a background pane's optimistic/streamed/echoed messages to
 *  the server's canonical history at end-of-turn (replaces the earlier
 *  reconcileSlot thunk, which did the same job). */
let warmSeqCounter = 0
const nextWarmSeq = (): number => ++warmSeqCounter

export const warmSlotCache = createAsyncThunk(
  'chat/warmSlotCache',
  async (key: string, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (state.activeSlot === key) return null
    // Unbounded while streaming is deliberate, not a raw-row guard: the handler
    // collapses chunk runs BEFORE computing total and slicing, even mid-stream.
    const streaming = (state.slotRun[key]?.state ?? 'idle') !== 'idle'
    // Captured BEFORE the fetch: two warms for one slot resolve in any order,
    // and the later-dispatched response is the newer view of the transcript.
    const warmSeq = nextWarmSeq()
    // `switchSlot.pending` paints the active view from this cache, and a window can miss
    // a small cache entirely once the server has grown, so refetch any of it whole.
    const cached = state.slotMessages?.[safeKey(key)]?.length ?? 0
    return { ...(await fetchSlotDetail(key, streaming || cached > 0 ? undefined : PANE_HYDRATE_LIMIT)), warmSeq }
  },
)

export const createSlot = createAsyncThunk<
  ChatSlot,
  { agent?: string; model?: string; mode?: string; memory_mode?: string; clean_mode?: boolean; folder_id?: string | null; title?: string; color_index?: number | null; color_hex?: string | null; project?: string | null; activate?: boolean; instanceId?: string } | string | undefined,
  { fulfilledMeta: { originActiveSlot: string | null; activate: boolean } }
>(
  'chat/createSlot',
  async (opts, { dispatch, getState, fulfillWithValue }) => {
    const agent = typeof opts === 'string' ? opts : opts?.agent
    const model = typeof opts === 'string' ? undefined : opts?.model
    const mode = typeof opts === 'string' ? undefined : opts?.mode
    const memory_mode = typeof opts === 'string' ? undefined : opts?.memory_mode
    const clean_mode = typeof opts === 'string' ? undefined : opts?.clean_mode
    const folderId = typeof opts === 'string' ? undefined : opts?.folder_id
    // Title at BIRTH, for the same reason folder membership rides this payload:
    // the server pins it (locking the background auto-titler out) and the create
    // broadcast already carries it, where a follow-up rename paints a generated
    // title first and can fail silently, leaving the caller's name unset.
    const title = typeof opts === 'string' ? undefined : opts?.title
    const explicitColor = typeof opts === 'string' ? undefined : opts?.color_index
    const explicitHex = typeof opts === 'string' ? undefined : opts?.color_hex
    const project = typeof opts === 'string' ? undefined : opts?.project
    // Bind the new session to a connected crew for EXECUTION. Sent at birth, not
    // patched on afterwards: the backend has to open the peer's slot before it
    // creates the local one, so a failure leaves nothing behind — patching later
    // would put a session in the sidebar that looks ready and refuses every send.
    const instanceId = typeof opts === 'string' ? undefined : opts?.instanceId
    // `activate: false` creates the session WITHOUT stealing focus, so a caller
    // that must finish setting the slot up (e.g. scoping it to a worktree) can
    // do so before the user is able to type into it. Defaults to true — every
    // existing caller keeps the create-and-focus behaviour.
    const activate = typeof opts === 'string' ? true : opts?.activate !== false
    // Capture the active slot BEFORE the (potentially slow) create round-trip.
    // The fulfilled reducer compares this against the active slot at resolution
    // time: if the user switched to a different session while the create was
    // pending (e.g. New Chat spun on "Creating" under memory pressure and they
    // moved to another tab), the new slot must NOT hijack the view.
    const originActiveSlot = (getState() as RootState).chat.activeSlot
    const slot = await api.createChatSlot(undefined, agent, model, mode, memory_mode, title, clean_mode, undefined, folderId || undefined, instanceId)
    const dashState = (getState() as RootState).dashboard
    // An explicit color (e.g. carried from a slot being recreated on a
    // mode switch) wins; otherwise fall back to the default-color policy.
    // A carried custom hex outranks both: the fields are mutually exclusive
    // (setting the hex clears the index server-side), so a custom-colored
    // session must NOT fall through to the palette policy on recreation.
    if (explicitHex != null) {
      slot.color_hex = explicitHex
      // A CARRIED color must land before the caller deletes the source slot
      // (create-first-then-delete): swallowing this failure would destroy the
      // only copy of the user's custom color. Await it and, on failure, remove
      // the half-configured slot and rethrow — the caller then returns without
      // deleting the original, so the colored session survives. Same contract
      // as the background project carry below. The default-color policy branch
      // stays fire-and-forget: nothing is lost if a default fails to apply.
      try {
        await api.setSlotColorHex(slot.key, explicitHex)
      } catch (err) {
        await api.deleteChatSlot(slot.key).catch(() => {})
        throw err
      }
    } else {
      const ci = explicitColor != null ? explicitColor : resolveDefaultColor(dashState.sessionDefaultColor, dashState.slots.length)
      if (ci != null) {
        slot.color_index = ci
        if (explicitColor != null) {
          try {
            await api.setSlotColor(slot.key, ci)
          } catch (err) {
            await api.deleteChatSlot(slot.key).catch(() => {})
            throw err
          }
        } else {
          api.setSlotColor(slot.key, ci).catch(() => {})
        }
      }
    }
    // Folder membership rides the create payload above, so the server files the
    // slot before it broadcasts it. A follow-up PATCH would be too late to
    // matter: the slots frame announcing this slot is emitted before the create
    // response arrives here, so an unfiled slot would render at the top level
    // first and visibly jump into its folder.
    // Carry the project directory. The create endpoint ignores `project` and
    // defaults it to the workspace dir, so a recreated slot would otherwise
    // lose its project — re-apply it via the dedicated endpoint. (We do NOT
    // re-issue setSlotAgent here: that endpoint resets the project back to the
    // workspace default, which would clobber this carry. Agent rides the
    // create payload instead.)
    if (project) {
      slot.project = project
      // Await the scope on BOTH paths before publishing the slot. Publishing
      // via addSlotOptimistic makes the slot selectable (and, when activated,
      // keys the agents-roster fetch to this optimistic project), so anything
      // that observes the slot before the server records the project runs
      // against the DEFAULT checkout: a turn would execute in the wrong
      // directory, and a roster fetch racing the POST would cache a
      // global-only roster under the new (slot, project) identity and never
      // refetch (the later slots frame carries the same project string). If
      // the scope fails, delete the session server-side rather than publish
      // an unscoped one.
      try {
        await api.chatSlotProject(slot.key, project)
      } catch (err) {
        await api.deleteChatSlot(slot.key).catch(() => {})
        throw err
      }
    }
    dispatch(addSlotOptimistic(slot))
    // Carry the origin slot in the action meta (fulfillWithValue) rather than on
    // the payload, so it can never leak into the persisted slot object. The
    // fulfilled reducer reads action.meta.originActiveSlot to decide whether
    // activating the new slot is safe.
    return fulfillWithValue(slot, { originActiveSlot, activate })
  },
)

export const deleteSlot = createAsyncThunk(
  'chat/deleteSlot',
  async (key: string, { dispatch, getState }) => {
    const root = getState() as RootState
    const deletedSlot = root.dashboard.slots.find(s => s.key === key)
    // Use the surface key (forward-compat alias for `mode`) so a future
    // backend that emits a distinct `slot.surface` keeps "switch to a peer
    // session" pinned to the same nav destination.
    const deletedSurface = deletedSlot ? slotSurfaceKey(deletedSlot) : ''
    // Navigate before removeSlotOptimistic to prevent a useEffect race: the
    // active slot must already name a surviving peer by the time this slot
    // leaves the list.
    //
    // What that ordering constrains is the STATE transitions, not the I/O.
    // `switchSlot.pending` assigns `activeSlot` synchronously as it is
    // dispatched, so the invariant above holds from that call — not from the
    // moment its history fetch resolves. That fetch is unbounded (the peer's
    // whole transcript, megabytes on a long session), so it is carried as a
    // promise rather than awaited here: blocking on it would hold the dismissed
    // tab on screen for the length of an unrelated conversation's load, which
    // reads as a dead close control. The peer paints from the `slotMessages`
    // cache when it has one, or from `slotLoading` behind the already-removed
    // tab when it does not.
    let navigation: Promise<unknown> | undefined
    if (root.chat.activeSlot === key) {
      const sameSurface = new Set(root.dashboard.slots.filter(s => slotSurfaceKey(s) === deletedSurface).map(s => s.key))
      const prev = root.chat.slotHistory.filter(k => k !== key && sameSurface.has(k)).pop()
        || root.dashboard.slots.filter(s => s.key !== key && sameSurface.has(s.key)).map(s => s.key)[0]
      dispatch({ type: 'chat/setActiveSlot', payload: null })
      if (prev) {
        navigation = dispatch(switchSlot(prev)).unwrap().catch(() => dispatch({ type: 'chat/clearSlotState' }))
      } else {
        dispatch({ type: 'chat/clearSlotState' })
      }
    }
    dispatch(removeSlotOptimistic(key))
    try {
      await api.deleteChatSlot(key)
      gcSessionStorage(key)
    } catch {
      dispatch(fetchSlots())
      throw new Error('save failed')
    } finally {
      // Settle the peer navigation before this thunk reports back, on the
      // failure path too. Callers that await it treat resolution as "the
      // dismissal is done" and then read the store (an app agent tearing its
      // session down, the create-first-then-delete mode switch), so resolving
      // mid-fetch would hand them a half-loaded peer. Rejection is already
      // absorbed by the `.catch` above, so this cannot throw and cannot mask
      // the error being propagated.
      await navigation
    }
    return key
  },
)

export const resumeFromHistory = createAsyncThunk(
  'chat/resumeFromHistory',
  async ({ key, title }: { key: string; title: string }, { dispatch }) => {
    const d = await api.resumeChatSlot(key, title)
    if (d.ok) {
      dispatch(addSlotOptimistic({ key: d.key, title: title || d.key, messages: 0, running: false, memory_mode: d.memory_mode, mode: d.mode, surface: d.surface ?? d.mode, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }))
      dispatch(updateSlot({ key: d.key, mode: d.mode, surface: d.surface ?? d.mode }))
    }
    // Without a cursor this response cannot be paged, so do not advertise more:
    // a zero cursor beside hasMore renders an affordance that loads nothing.
    const cursor = typeof d.next_before === 'number' ? d.next_before : null
    // `surface` (falling back to `mode`) is returned so a caller resuming from
    // a surface that cannot display every slot (ChatPage's unified view only
    // shows default/orchestrator/crew, see isChatPageSurface) can tell a
    // silently-unusable resume apart from a genuinely failed one (#3624) --
    // the request succeeds either way, so `ok` alone cannot distinguish them.
    return { ok: d.ok, key: d.key, surface: d.surface ?? d.mode, nextBefore: cursor ?? 0, messages: filterMessages(d.messages || []), hasMore: cursor !== null && (d.has_more || false), total: d.total || 0 }
  },
)

export const forkSlot = createAsyncThunk(
  'chat/forkSlot',
  async (
    { slot, atIndex, messageId, prompt, mode, direction }: { slot: string; atIndex?: number; messageId?: string; prompt?: string; mode?: string; direction?: 'head' | 'tail' },
    { dispatch, rejectWithValue },
  ) => {
    let d
    try {
      d = messageId
        ? await api.forkChatSlot(slot, atIndex, prompt, mode, direction, messageId)
        : await api.forkChatSlot(slot, atIndex, prompt, mode, direction)
    } catch (e) {
      // Same boundary as fetchSlotDetail above: `miniSerializeError` drops the
      // backend `code`, and the check is STRUCTURAL for the reason documented there.
      const status = (e as { status?: unknown } | null)?.status
      const body = (e as { body?: unknown } | null)?.body
      const raw = typeof body === 'string' ? body : undefined
      if (typeof status === 'number') {
        return rejectWithValue({
          status,
          message: errMessage(e),
          code: parseErrorCode(raw),
        })
      }
      throw e
    }
    if (d.ok) {
      dispatch(addSlotOptimistic({ key: d.key, title: d.title || d.key, messages: d.messages || 0, running: false, folder_id: d.folder_id }))
    }
    return d
  },
)

export const deleteHistorySession = createAsyncThunk(
  'chat/deleteHistorySession',
  async (key: string) => { await api.deleteSession(key); return key },
)

/** Abort any in-flight older-page fetch. Wired to transcript MOTION: the
 *  settle gates guard the DISPATCH moment, but a page dispatched during a
 *  reading pause lands 1-2s later — mid-fling on a phone, where the prepend
 *  compensation fights the momentum curve (reproduced on the momentum rig as
 *  ±3000px content jumps during coast). Aborting on motion means a landing
 *  can only ever commit while the scroller is still; the walk re-dispatches
 *  when stillness returns. An abort rejection carries no payload, so the
 *  rejected reducer sets no error flag. */
export function abortActiveOlderFetch(): void {
  _abortLoadOlder?.()
}

export const loadOlderMessages = createAsyncThunk(
  'chat/loadOlder',
  async (_, { getState, rejectWithValue }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!state.activeSlot || !state.slotHasMore) return null
    if (state.slotOldestIndex <= 0) return null
    const slot = state.activeSlot
    const controller = new AbortController()
    const abort = () => controller.abort()
    _abortLoadOlder = abort
    try {
      // Landing size is a LAYOUT BURST: on a phone (slow CPU, slow network)
      // a 300-row landing is a long task during which the anchor
      // compensation paints late and the reader visibly loses their place
      // ('突然加载一大堆就不在原来的位置'). Narrow viewports take smaller,
      // cheaper landings; the walk simply takes more of them.
      const isNarrow = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
        && window.matchMedia('(max-width: 640px)').matches
      const walkLimit = isNarrow ? OLDER_PAGE_LIMIT : OLDER_WALK_PAGE_LIMIT
      const d = await api.chatSlotDetail(slot, walkLimit, state.slotOldestIndex, controller.signal)
      // LANDING BUFFER: the fetch overlaps the reader's gesture, but the
      // MUTATION must not -- splicing rows mid-glide races the pre-paint
      // anchor machinery against the gesture's own pixel-addressed window
      // recompute (phone rig: kilopixel per-landing jumps whose anchor
      // consume mis-bound and stood down). Hold the payload until the
      // scroller has been quiet for a beat; bounded, so a reader who never
      // pauses still gets the page (see scrollQuiet.ts).
      await whenScrollQuiet(controller.signal)
      if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
      return { slot, nextBefore: d.next_before || 0, messages: filterMessages(d.messages || []), hasMore: d.has_more || false, total: d.total || 0 }
    } catch (e) {
      // Rethrow a cancellation so the reducer can tell it from a real failure;
      // a genuine failure names its slot, because a switch may have moved on.
      if (isSupersededPagingRejection(e)) throw e
      return rejectWithValue({ slot })
    } finally {
      // Only clear our own handle: a newer fetch may already have replaced it.
      if (_abortLoadOlder === abort) _abortLoadOlder = null
    }
  },
  {
    // `loadingOlder` must be read HERE: `pending` sets it before the creator runs.
    // The cursor check blocks paging mid-switch, when it still describes the old chat.
    condition: (_, { getState }) => {
      const state = (getState() as { chat: ChatState }).chat
      if (state.loadingOlder) return false
      return state.slotCursorKey === state.activeSlot
    },
  },
)

export const requestStop = createAsyncThunk(
  'chat/requestStop',
  async ({ slotId, force }: { slotId: string; force: boolean }, { getState, dispatch }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!force) {
      const lastPress = state.stopPressedAt[slotId] ?? 0
      if (Date.now() - lastPress < SOFT_STOP_DEBOUNCE_MS) return
    }
    dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: Date.now() }))
    try {
      if (force) {
        await api.stopChatSlotForce(slotId)
      } else {
        await api.stopChatSlot(slotId)
      }
    } catch {
      dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: 0 }))
    }
  },
)

/** Get subagents map for a slot (read-only lookup) */
function getSlotSubs(state: ChatState, slot: string) {
  return slot !== state.activeSlot ? state.slotActivity[slot]?.subagents : state.subagents
}

/**
 * Attach a tool result's output to the tool MESSAGE meta for every message
 * carrying `tid`, in both the live list and the per-slot cache.
 *
 * All matching messages, not just the newest: an auto-approved tool produces
 * TWO tool messages sharing one tool_call_id (🔧 pre-approval + ✅
 * post-approval) and the server patches both, so stopping at the first would
 * leave the pair disagreeing about the same call.
 */
function applyToolOutputToMessages(
  state: ChatState,
  slot: string,
  tid: string,
  output: string,
): void {
  if (isUnsafeKey(slot)) return
  const patch = (msgs: ChatMessage[] | undefined): void => {
    if (!Array.isArray(msgs)) return
    for (const m of msgs) {
      if (m.role !== 'tool') continue
      const meta = m.meta as Record<string, unknown> | undefined
      if (!meta || meta.tool_call_id !== tid) continue
      m.meta = { ...meta, output }
    }
  }
  if (slot === state.activeSlot) patch(state.messages)
  // The cache can hold the SAME array reference as state.messages (switchSlot
  // caches by reference), so this may be a second pass over one list — the
  // patch is idempotent, and skipping it would strand a genuinely separate
  // cached copy with no output. `safeKey` mirrors hydrateSlotMessages: the
  // early return above already rejects unsafe keys, this is the codebase's
  // defense-in-depth companion.
  patch(state.slotMessages[safeKey(slot)])
}

/** Central, fail-closed accessor for a single subagent entry by wire-supplied
 *  id. Applies the `isUnsafeKey` prototype-pollution guard once, here, so no
 *  reducer that indexes the subagents map by an external id has to remember the
 *  incantation — forgetting is impossible at the call site. A hostile
 *  `__proto__`/`constructor`/`prototype` id resolves to `undefined` (frame
 *  dropped) rather than to `Object.prototype`. */
function getSlotSub(state: ChatState, slot: string, id: string): SubagentActivity | undefined {
  if (isUnsafeKey(id)) return undefined
  return getSlotSubs(state, slot)?.[id]
}

/**
 * Live "sub-agents running" signal for a slot, derived from the
 * subagent_spawn/tool/done WS events (the only real-time source — see the
 * ChatSidebar countActive note: dashboardSlice fields only refresh on a full
 * slots push). Counts pending/running/tool as active, mirroring ChatSidebar.
 */
export const selectSlotSubagentsActive = (state: RootState, slot: string): boolean => {
  const subs = getSlotSubs(state.chat, slot)
  if (!subs) return false
  for (const a of Object.values(subs)) {
    if (a.status === 'running' || a.status === 'tool' || a.status === 'pending') return true
  }
  return false
}

// Shared subagent-counting helpers — single implementations for both sidebar and aggregate selectors.

/** Counts active subagents (running + tool + pending) in a subagent map. */
const countActiveSubagents = (m?: Record<string, SubagentActivity>) => {
  if (!m) return 0
  let n = 0
  for (const a of Object.values(m)) {
    if (a.status === 'running' || a.status === 'tool' || a.status === 'pending') n++
  }
  return n
}

/**
 * Predicate: subagent is blocked awaiting a spawn approval.
 *
 * Exported because the RENDERERS need the same question answered, and every
 * component that re-derived it from `status` alone got a different answer: the
 * wave chip and the launch card both counted a parked run as running (#7318).
 * `approval_id` is the load-bearing half — `sseSubagentPending` is the only
 * writer of `'pending'` and always sets it, so its absence means a card built
 * some other way and must not be claimed as blocked on the user.
 */
export const isAwaitingSpawnApproval = (a: SubagentActivity) =>
  a.status === 'pending' && !!a.approval_id

/** Counts subagents pending spawn approval in a subagent map. */
const countPendingApprovals = (m?: Record<string, SubagentActivity>) => {
  if (!m) return 0
  let n = 0
  for (const a of Object.values(m)) {
    if (isAwaitingSpawnApproval(a)) n++
  }
  return n
}

// Stable empty result so the selector is referentially stable (with shallowEqual)
// when a slot has no pending spawn approvals — avoids needless re-renders.
const _EMPTY_PENDING_SPAWNS: SubagentActivity[] = []

/**
 * Pending sub-agent SPAWN approvals for a slot — sub-agents queued to run but
 * blocked on the user's approval (status 'pending' + an approval_id).
 *
 * The backend broadcasts a spawn approval as a WS `approval` event with
 * id `spawn:<agent_id>`; useWebSocket routes it into `sseSubagentPending`, so
 * it only ever renders as a pending card in the side panel's Subagents tab —
 * there is NO inline chat prompt and NO notification. This selector lets the
 * composer surface a top-level "awaiting approval" banner so the user knows an
 * action is required without hunting through the side panel. Use with
 * `shallowEqual`.
 */
export const selectSlotPendingSpawnApprovals = (state: RootState, slot: string | null): SubagentActivity[] => {
  if (!slot) return _EMPTY_PENDING_SPAWNS
  const subs = getSlotSubs(state.chat, slot)
  if (!subs) return _EMPTY_PENDING_SPAWNS
  const out = Object.values(subs).filter(isAwaitingSpawnApproval)
  return out.length ? out : _EMPTY_PENDING_SPAWNS
}

/**
 * Total sub-agents in flight across EVERY slot — started (running/tool/pending)
 * plus accepted-but-queued. Drives the Sessions rail activity dot, which is the
 * only cross-page signal that a background chat has agents working: the chip
 * above the composer covers the viewed slot only, and the sidebar subtitle is
 * invisible from any other page.
 *
 * Memoized (`createSelector`) because the surface registry invokes activity
 * selectors on every dispatch.
 */
export const selectSubagentActivityCount = createSelector(
  [
    (state: RootState) => state.chat.activeSlot,
    (state: RootState) => state.chat.subagents,
    (state: RootState) => state.chat.slotActivity,
    (state: RootState) => state.chat.subagentQueued,
  ],
  (activeSlot, activeSubs, slotActivity, queued) => {
    let total = activeSlot ? countActiveSubagents(activeSubs) : 0
    for (const [slot, act] of Object.entries(slotActivity ?? {})) {
      // On switchSlot the active slot's map is aliased into both
      // state.subagents and slotActivity[active].subagents (same reference),
      // so this guard is what prevents double-counting it.
      if (slot === activeSlot) continue
      total += countActiveSubagents(act.subagents)
    }
    for (const q of Object.values(queued ?? {})) total += q > 0 ? q : 0
    return total
  },
)

/** Per-slot subagent counts for sidebar. Reuses shared counting helpers above. */

/** Total active subagents per slot (running + tool + pending). */
export const selectSidebarSubagentCounts = createSelector(
  [
    (state: RootState) => state.chat.activeSlot,
    (state: RootState) => state.chat.subagents,
    (state: RootState) => state.chat.slotActivity,
    (state: RootState) => state.chat.subagentQueued,
  ],
  (activeSlot, activeSubs, slotActivity, queued) => {
    const counts: Record<string, number> = {}
    if (activeSlot) {
      const n = countActiveSubagents(activeSubs)
      if (n > 0) counts[activeSlot] = n
    }
    for (const [slot, act] of Object.entries(slotActivity ?? {})) {
      // Load-bearing: active slot's map is aliased in both places; skip to avoid double-count.
      if (slot === activeSlot) continue
      const n = countActiveSubagents(act.subagents)
      if (n > 0) counts[slot] = n
    }
    // Fold in queued counts.
    for (const [slot, q] of Object.entries(queued ?? {})) {
      if (q > 0) counts[slot] = (counts[slot] || 0) + q
    }
    return counts
  },
)

/** Subagents pending approval per slot (status=pending + has approval_id). */
export const selectSidebarApprovalCounts = createSelector(
  [
    (state: RootState) => state.chat.activeSlot,
    (state: RootState) => state.chat.subagents,
    (state: RootState) => state.chat.slotActivity,
  ],
  (activeSlot, activeSubs, slotActivity) => {
    const approvalCounts: Record<string, number> = {}
    if (activeSlot) {
      const p = countPendingApprovals(activeSubs)
      if (p > 0) approvalCounts[activeSlot] = p
    }
    for (const [slot, act] of Object.entries(slotActivity ?? {})) {
      // Same aliasing guard as countActive above: the active slot's map is the
      // same object in both places, so skipping it here avoids double-counting.
      if (slot === activeSlot) continue
      const p = countPendingApprovals(act.subagents)
      if (p > 0) approvalCounts[slot] = p
    }
    return approvalCounts
  },
)

/** Live dynamic-workflow activity per originating session, keyed by the
 *  NORMALIZED session key (`normalizeRunSessionKey`), so a slot looks itself
 *  up with the same normalization `runBelongsToSlot` applies — this replaces
 *  a per-slot scan over every run. Values carry the count of running runs
 *  plus the RAW name/phase of the first matching run in insertion order;
 *  they are agent-authored wire strings, so rendering sanitizes at the edge.
 *  Memoized on `workflowRuns` identity, which lets a session row read its one
 *  key with `shallowEqual` and ignore every other run's events. */
export const selectSidebarWorkflowActive = createSelector(
  [(state: RootState) => state.chat.workflowRuns],
  (workflowRuns) => {
    // Null prototype: the accumulator is indexed by a normalized session key
    // from the wire, and on a `{}` literal a key like "__proto__" would READ
    // Object.prototype as a truthy existing entry and then mutate it —
    // corrupting every object in the page. Object.create(null) makes such a
    // key an ordinary own property. (Same threat model as the goalLoops
    // safeKey normalization.)
    const active: Record<string, { count: number; name: string; phase: string }> = Object.create(null)
    for (const r of Object.values(workflowRuns ?? {})) {
      // A run with NO sessionKey is UI-launched (no chat link) and belongs to
      // no slot — the same exclusion runBelongsToSlot encodes.
      if (r.status !== 'running' || !r.sessionKey) continue
      const key = normalizeRunSessionKey(r.sessionKey)
      const cur = active[key]
      if (cur) cur.count += 1
      else active[key] = { count: 1, name: r.name || r.run_id, phase: r.phase || '' }
    }
    return active
  },
)

/** Just the keys of `selectSidebarWorkflowActive` — the sidebar shell's
 *  presence signal (the In-progress filter and the board's state lanes need
 *  "which sessions have a live run", never the label). Subscribed with
 *  `shallowEqual`, it re-renders the shell only when the SET of
 *  workflow-active sessions changes, not on every phase/progress event. */
export const selectSidebarWorkflowActiveKeys = createSelector(
  [selectSidebarWorkflowActive],
  (active) => Object.keys(active),
)

/** Keys of sessions with an active goal loop — the same presence-only
 *  contract as `selectSidebarWorkflowActiveKeys`: a mid-loop cycle-count bump
 *  rewrites the map value but leaves this key set (and so, under
 *  `shallowEqual`, the subscriber) untouched. `Object.keys` returns own keys
 *  only, so membership tests over the result are inherently own-property —
 *  the `safeKey` prototype-pollution caveat on direct map reads does not
 *  apply here. */
export const selectGoalLoopKeys = createSelector(
  [(state: RootState) => state.chat.goalLoops],
  (goalLoops) => Object.keys(goalLoops ?? {}),
)

/**
 * Single source of truth for "is this slot's composer busy" — the signal that
 * queues the next message (busy affordance) and skips the optimistic user
 * bubble (the backend returns a "queued" message instead, so an optimistic
 * bubble would render a duplicate). Busy = main turn running OR background
 * sub-agents running, with two redundant sub-agent signals OR'd
 * (conservative): the live WS-derived signal (real-time, self-heals on
 * sub-agent crash via the reaper's done event) and the slots-stream snapshot
 * field (covers the first frames after reload/reconnect before WS events
 * replay). Used by ChatPage (main route) and ChatPane (split view) — keep both
 * routes on this selector so the rule cannot drift.
 */
export const selectComposerBusy = (state: RootState, slot: string | null): boolean => {
  if (!slot) return state.chat.slotRunning
  if (selectSlotStreamState(state, slot) !== 'idle') return true
  if (slot === state.chat.activeSlot && state.chat.slotRunning) return true
  if (selectSlotSubagentsActive(state, slot)) return true
  const dashSlot = state.dashboard.slots.find((sl) => sl.key === slot)
  // A running autopilot plan keeps the composer "busy" so a mid-plan message
  // queues (chip card) instead of rendering an optimistic bubble that would
  // duplicate the backend's queued message. slot.running reads False between
  // stages, so orchestrating is the durable signal here.
  return !!(dashSlot?.subagents_running || dashSlot?.orchestrating)
}

/** Roles the continue scans walk past: they are not the conversation's floor.
 *  Mirrors `_is_interrupted` / `_has_conversation` in
 *  `src/kiro_crew/dashboard/chat_handlers.py`, which likewise only read
 *  `user` / `assistant` / `error` rows. Keep them in sync — these predicates
 *  decide whether to OFFER Continue and what to call it, those decide whether to
 *  authorize it and what to tell the model. */
const CONTINUE_SCAN_SKIP = new Set(['queued', 'tool_call', 'tool_result', 'inject', 'subagent', 'permission', 'nudge'])

/**
 * True when the active slot can be handed back to the agent — i.e. Continue is
 * worth offering on an empty composer.
 *
 * The rule is simply "the slot is idle and has a conversation under it". It is
 * NOT limited to turns that visibly died, because a transcript cannot reliably
 * show that they did: a force-quit or force-exit runs no cleanup, so no error
 * row is ever written and a killed turn reads exactly like a finished one (see
 * ``_has_conversation`` in `src/kiro_crew/dashboard/chat_handlers.py`, which
 * authorizes the press under the slot lock). Offering it on every idle slot
 * covers those invisible interruptions, and doubles as a plain "keep going"
 * nudge — the one thing an empty composer's dead send button could never do.
 *
 * Everything that makes a continuation UNSAFE still returns false: a live turn,
 * a stop in flight, an optimistic local turn, a mid-plan autopilot slot, a
 * running subagent, or a queued message the runner is about to pick up itself.
 *
 * Computed locally on purpose: `messages`, `slotRunning`, `slotStopping` and the
 * queue are all already in this store, so no server field is needed to decide
 * what to SHOW. The server re-checks under the slot lock when the button is
 * actually pressed — this view is a lagging WS snapshot, so it cannot be the
 * authority for dispatching a turn.
 *
 * An empty transcript returns false, which keeps a brand-new chat's send button
 * disabled exactly as it is today.
 */
/** Project directory of the ACTIVE chat session's slot, or undefined when no
 *  session is selected or the selected session has no project set. Used by the
 *  bottom terminal panel so a freshly opened terminal starts in the selected
 *  session's working tree instead of the server default. */
export const selectActiveSlotProject = (state: RootState): string | undefined => {
  const key = state.chat.activeSlot
  if (!key) return undefined
  return state.dashboard.slots.find((sl) => sl.key === key)?.project || undefined
}

export const selectContinuable = (state: RootState): boolean => {
  const c = state.chat
  if (c.slotRunning || c.slotStopping || c.pendingTurnSlot) return false
  // An autopilot plan reads `running` False BETWEEN stages while still mid-plan,
  // so `running` alone would offer Continue on a slot the server refuses with
  // `slot_orchestrating`. Mirrors the same guard in `api_chat_slot_continue`.
  const dashSlot = state.dashboard.slots.find((sl) => sl.key === c.activeSlot)
  if (dashSlot?.orchestrating || dashSlot?.subagents_running) return false
  const msgs = c.messages
  if (!msgs.length) return false
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    // A pending queued message means the backend is about to run the thread on
    // its own — offering Continue would double-fire the turn.
    if (m.role === 'queued') return false
    if (CONTINUE_SCAN_SKIP.has(m.role)) continue
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      // System notices (compaction, session reload) are assistant-role status
      // messages, not the floor.
      if (m.role === 'assistant' && isSystemNoticeKind((m.meta as { kind?: string } | undefined)?.kind)) continue
      return true
    }
  }
  return false
}

/**
 * True when the transcript SHOWS the last turn ending without the assistant
 * handing the floor back — the user's row is last, or an `error` row trails the
 * assistant's.
 *
 * Gates the composer's Resume button (composed with `selectContinuable` in
 * ChatPage) and selects the continuation body handed to the model. Mirrors
 * `_is_interrupted` in `src/kiro_crew/dashboard/chat_handlers.py` — the two must
 * agree, or the button promises one thing and the agent is told another.
 *
 * A false result means "nothing in the transcript proves an interruption", never
 * "the turn definitely finished": the force-quit case leaves no evidence.
 */
export const selectTurnInterrupted = (state: RootState): boolean => {
  const msgs = state.chat.messages
  let sawTrailingError = false
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    // A deliberate Stop ENDS the turn; it does not interrupt it. This must be
    // tested before the user/assistant check, because pressing Stop before the
    // reply produced any text leaves `[user, stop_event]` — shape-identical to
    // "the gateway died before anything came back", which is what this scan
    // would otherwise read it as. Without this branch the same visible action
    // (pressing Stop) offered Resume or not depending purely on whether a
    // segment had flushed first, i.e. on invisible timing the user cannot
    // predict. The user chose to stop; the floor is theirs, so the composer
    // shows Send. Reached only for the NEWEST turn's terminator — an older stop
    // card deeper in history is never scanned, because a later user/assistant
    // row returns first.
    if (isStopEvent(m)) return false
    if (m.role === 'error') { sawTrailingError = true; continue }
    if (CONTINUE_SCAN_SKIP.has(m.role)) continue
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      if (m.role === 'assistant' && isSystemNoticeKind((m.meta as { kind?: string } | undefined)?.kind)) continue
      return m.role === 'user' ? true : sawTrailingError
    }
  }
  return false
}

/** Monotonic tick, so an observation can be ordered against a request already in flight.
 *  A bare boolean cannot: it could have been set by an earlier, unrelated edit. */
let queueEditBroadcastSeq = 0
/** Nested per slot rather than keyed on a joined string: queue ids are unique only within
 *  their own sidecar, and a joined key would need a separator literal. */
const queueEditBroadcasts = new Map<string, Map<string, number>>()

function noteQueueEditBroadcast(slot: string, queueId: string): void {
  let perSlot = queueEditBroadcasts.get(slot)
  if (!perSlot) {
    perSlot = new Map<string, number>()
    queueEditBroadcasts.set(slot, perSlot)
  }
  perSlot.set(queueId, ++queueEditBroadcastSeq)
}

/** The tick at which the server was last seen broadcasting an edit for this card, or 0.
 *  Client-local: it is evidence about a request, not state worth persisting or syncing. */
export function queueEditBroadcastAt(slot: string, queueId: string): number {
  return queueEditBroadcasts.get(slot)?.get(queueId) ?? 0
}

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    setActiveSlot(state, action: PayloadAction<string | null>) { state.activeSlot = action.payload; state.slotState = 'idle'; state.pendingTurnSlot = null },
    clearSlotState(state) { state.messages = []; state.toolLog = []; state.subagents = {}; state.activityTab = 'changes'; state.slotRunning = false; state.slotStopping = false; state.slotState = 'idle'; setPagingCursor(state, false, 0); state.loadingOlder = false; state.lastChunkSeq = undefined; state._wsChunkedDuringFetch = false; state.slotStatusDetail = {}; state.voicePlaying = false; state.voiceAudio = null; if (state.activeSlot) delete state.pendingQuestions?.[state.activeSlot]; state.pendingTurnSlot = null },
    setPendingInput(state, action: PayloadAction<string | null>) { state.pendingInput = action.payload },
    setAgentSwitchNotice(state, action: PayloadAction<string | null>) {
      // Always create a fresh value so repeating the same refusal restarts the
      // App shell's expiry effect instead of inheriting the previous timer.
      state.agentSwitchNotice = action.payload === null ? null : { message: action.payload }
    },
    /** Dismiss the unresumable-surface notice (#5925). Deliberately does NOT
     *  clear `lastResumeRequestId`: that ordering token belongs to the resume
     *  in flight, and forgetting it would let an older resume's late answer
     *  re-open a notice the user just closed. */
    clearUnresumableResume(state) { state.unresumableResume = null },
    setQuestionCard(state, action: PayloadAction<{ slot: string; ask_id?: string; card_id?: string; questions: ChatState['pendingQuestions'][string]['questions']; fresh?: boolean }>) {
      // Defensive init: existing test fixtures build partial preloaded state
      // without this key.
      if (!state.pendingQuestions) state.pendingQuestions = {}
      // Same fail-closed chokepoint as the neighbouring slot-keyed reducers: the
      // slot arrives over the websocket, and `__proto__`/`constructor` would
      // otherwise make a READ return an inherited value that is truthy but has
      // no `questions`, crashing QuestionCard on render.
      if (isUnsafeKey(action.payload.slot)) return
      const key = safeKey(action.payload.slot)
      const prev = state.pendingQuestions[key]
      if (prev && !action.payload.fresh) {
        // Payload comparison, not reference: a websocket reconnect re-dispatches
        // the SAME still-pending card with a freshly parsed questions array
        // (syncPendingQuestions). That is not a new ask — keep the existing
        // entry (and its cardId) so the mounted card is not churned. Only the
        // NON-fresh path may coalesce: a live `question_card` broadcast sets
        // `fresh`, because a genuinely new ask that happens to repeat a prior
        // question must get its own delivery identity — coalescing it would
        // let a stale send completion for the old card retire the new one.
        const same = prev.ask_id === action.payload.ask_id &&
          JSON.stringify(prev.questions) === JSON.stringify(action.payload.questions)
        if (same) return
      }
      state.pendingQuestions[key] = {
        slot: action.payload.slot,
        ask_id: action.payload.ask_id,
        questions: action.payload.questions,
        // Per-delivery identity, minted once per entry. This — not the
        // payload — is what send-time captures compare against, so two
        // deliveries of an identical question are still distinguishable.
        cardId: `card-${secureRandomId()}`,
        // The SERVER's identity for this ask, carried on the broadcast. Distinct
        // from `cardId` above, which is minted here per delivery: only the
        // server's own id can name the record the dismiss route retires, so a
        // dismissal that lands after a newer card replaced this one is refused
        // instead of clearing the new card's status. Absent for a blocking card
        // (its `ask_id` is that identity) and for a payload that predates it.
        serverCardId: action.payload.card_id,
        // A fresh, structurally IDENTICAL replacement keeps the mounted
        // component (PendingQuestionCard keys the component by payload, not
        // cardId), so the user's local draft survives the swap — but a plain
        // replacement here would reset `draftActive` and let the next
        // turn-consuming frame silently destroy that surviving draft. Carry
        // the flag over exactly for that case. A DIFFERENT payload remounts
        // the component (local draft state is genuinely gone), so starting
        // clean there is correct.
        draftActive:
          prev !== undefined &&
          prev.draftActive === true &&
          prev.ask_id === action.payload.ask_id &&
          JSON.stringify(prev.questions) === JSON.stringify(action.payload.questions)
            ? true
            : undefined,
      }
    },
    /** Confirmed-delivery retirement of the sender's OWN answer to a
     *  stateless card. The composer's user frame is never echoed back over
     *  the wire (slot.append skips the broadcast for `user` rows the sender
     *  already rendered optimistically), so the frame appliers can never
     *  retire the card for the device that sent the answer — the send path
     *  must do it. Dispatched by the send call sites ONLY when the server
     *  accepted the message for immediate dispatch (`ok`): retiring on the
     *  optimistic append would delete the card on a FAILED send (offline,
     *  5xx), and retiring on `queued` would delete it while the queued
     *  message is still cancellable — a QUEUED answer retires at its
     *  `queue_pop` instead (see removeQueuedMessage), the moment it actually
     *  becomes the slot's next turn.
     *
     *  `expected` is the per-delivery `cardId` of the card that was pending
     *  WHEN THE SEND STARTED (captureStatelessCard at the send path's
     *  entry). A slow POST response can race a new card into the slot —
     *  including one repeating the identical question, which payload
     *  comparison cannot distinguish — and an unqualified retirement would
     *  delete that live card. Identity comparison makes any stale
     *  completion a no-op. */
    retireStatelessQuestion(state, action: PayloadAction<{ slot: string; expected: string }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const card = state.pendingQuestions?.[safeKey(action.payload.slot)]
      if (!card || card.ask_id) return
      if (card.cardId !== action.payload.expected) return
      delete state.pendingQuestions[safeKey(action.payload.slot)]
    },
    clearQuestionCard(state, action: PayloadAction<{ slot: string }>) {
      if (isUnsafeKey(action.payload.slot)) return
      delete state.pendingQuestions?.[safeKey(action.payload.slot)]
    },
    /** Publish whether the slot's pending card has a non-empty custom answer
     *  in progress. The draft text itself lives in QuestionCard's component
     *  state; the reducer only needs the boolean so `dropStaleStatelessQuestion`
     *  can refuse to unmount a card whose typed answer would be destroyed.
     *  No-op when no card is pending (a late flip after resolution). */
    setQuestionDraft(state, action: PayloadAction<{ slot: string; active: boolean }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const card = state.pendingQuestions?.[safeKey(action.payload.slot)]
      if (card) card.draftActive = action.payload.active
    },
    /** Clear the card the backend just retired, matched by IDENTITY.
     *
     *  `ask_id` names a blocking round-trip; `card_id` names a stateless card
     *  (compared against the server identity the card was delivered with).
     *  Matching by identity rather than by slot is what stops a stale retirement
     *  — for a question already replaced by a newer one — from clearing a live
     *  card the user is part-way through.
     *
     *  A STATELESS card with a draft in progress survives, for the same reason
     *  `dropStaleStatelessQuestion` spares it: the typed answer lives only in the
     *  card's component state, so unmounting discards it — and a retirement
     *  arrives at an unpredictable moment (a nudge frame on a monitored session
     *  retires the record while the user is still typing). The card is already
     *  answerable as a plain message, and dismissing it after the server dropped
     *  the record is treated as success. A BLOCKING ask is not spared: its future
     *  is already settled, so the card cannot be answered at all. */
    resolveQuestionCard(state, action: PayloadAction<{ ask_id?: string; card_id?: string }>) {
      const { ask_id: askId, card_id: cardId } = action.payload
      if (!askId && !cardId) return
      for (const [slotKey, card] of Object.entries(state.pendingQuestions ?? {})) {
        const hit = askId ? card?.ask_id === askId : card?.serverCardId === cardId
        if (!hit) continue
        if (!askId && card?.draftActive) continue
        delete state.pendingQuestions[slotKey]
      }
    },
    setFollowupCard(state, action: PayloadAction<{ slot: string; items: FollowupItem[]; ts?: number }>) {
      const { slot, items, ts } = action.payload
      if (!slot || !items?.length) return
      if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
      // Defensive: a partial preloaded slice (tests, older persisted state) can
      // arrive without this key.
      if (!state.followups) state.followups = {}
      state.followups[slot] = { items, ts: ts ?? Date.now() / 1000 }
    },
    // `ts` guards the async case: "Start in new worktree" clears the card only
    // after its request resolves, and a NEWER card may have arrived for the same
    // slot meanwhile. Passing the ts the action started with means the newer card
    // survives instead of being clobbered by the older action's completion.
    clearFollowupCard(state, action: PayloadAction<{ slot: string; ts?: number }>) {
      const { slot, ts } = action.payload
      if (isUnsafeKey(slot)) return
      const card = state.followups?.[slot]
      if (!card) return
      if (ts != null && card.ts !== ts) return
      delete state.followups[slot]
    },
    // Skip ONE suggestion without discarding the others. The card disappears
    // only once its last item is gone, so skipping the first of three does not
    // silently throw away the other two.
    dismissFollowupItem(state, action: PayloadAction<{ slot: string; index: number; ts?: number }>) {
      const { slot, index, ts } = action.payload
      if (isUnsafeKey(slot)) return
      const card = state.followups?.[slot]
      if (!card) return
      // Same staleness guard as `clearFollowupCard`: a replacement card can land
      // between render and click, and an unqualified dismiss would delete that
      // index from a card the user has not seen.
      if (ts != null && card.ts !== ts) return
      const items = card.items.filter((_, i) => i !== index)
      if (items.length) state.followups[slot] = { ...card, items }
      else delete state.followups[slot]
    },
    setFolderSuggestion(state, action: PayloadAction<{ slot: string; folderId: string; folderName: string; breadcrumb: string; ts?: number }>) {
      const { slot, folderId, folderName, breadcrumb, ts } = action.payload
      if (!slot || !folderId || !folderName) return
      if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
      // Defensive: a partial preloaded slice (tests, older persisted state) can
      // arrive without this key.
      if (!state.folderSuggestions) state.folderSuggestions = {}
      state.folderSuggestions[slot] = { folderId, folderName, breadcrumb, ts: ts ?? Date.now() / 1000, turns: 0 }
    },
    // Both answers land here — accepting the move and declining it clear the same
    // way, because the backend keeps no state to resolve and offers at most one
    // card per slot either way. `ts` guards the async case the way
    // `clearFollowupCard` does: the accept path clears after its move request is
    // dispatched, so a card that arrived meanwhile must survive.
    clearFolderSuggestion(state, action: PayloadAction<{ slot: string; ts?: number }>) {
      const { slot, ts } = action.payload
      if (isUnsafeKey(slot)) return
      const card = state.folderSuggestions?.[slot]
      if (!card) return
      if (ts != null && card.ts !== ts) return
      delete state.folderSuggestions[slot]
    },
    sseContextUsage(state, action: PayloadAction<{ slot: string; pct: number; used_tokens?: number; window_tokens?: number; reset?: boolean }>) {
      const { slot, pct, used_tokens, window_tokens, reset } = action.payload
      if (isUnsafeKey(slot)) return
      state.slotContextPct[safeKey(slot)] = pct
      if (window_tokens && window_tokens > 0) {
        state.slotContextTokens[safeKey(slot)] = { used: used_tokens ?? 0, window: window_tokens }
      } else if (reset) {
        // Model switch / compaction / session reset: the stored counts belong
        // to a window that no longer describes the session. Deleting re-enables
        // the model-derived fallback (provider.getContextWindow(slot.model)).
        // A frame WITHOUT `reset` never deletes — it only fills or replaces — so
        // the backend sets `reset` whenever it has no real counts to send,
        // clearing stale counts instead of leaving them beside a fresh pct.
        delete state.slotContextTokens[safeKey(slot)]
      }
    },
    appendMessage(state, action: PayloadAction<ChatMessage>) {
      // Finalize-on-steer: a mid-turn steer bubble (ChatPage steer(), meta.steer)
      // must freeze the live streaming message BEFORE it is pushed, or the
      // chunk reducer keeps appending the rest of the segment into the stranded
      // streaming message ABOVE the bubble (stuck streaming marker at the steer
      // point). The backend cuts the segment at the same boundary (see
      // _run_chat's steer segment cut), so the frozen order matches the
      // persisted transcript and the chat_done refresh doesn't reorder it.
      const m = action.payload
      // Retiring the slot's stateless question card on this OPTIMISTIC append
      // is deliberately NOT done here: the send can still fail (offline, 5xx),
      // and the card must survive a failed send. The send path dispatches
      // retireStatelessQuestion after the server confirms delivery.
      if (m.role === 'user' && m.meta?.steer) finalizeTrailingStreaming(state.messages)
      // Non-steer user bubbles carry a `sendId` in meta (set by ChatPage at
      // send time) that serves as both the optimistic marker and the correlation
      // ID for reconciliation. The `optimistic` flag is kept as a simple boolean
      // so the reconcile scan knows this bubble is pending confirmation.
      if (m.role === 'user' && !m.meta?.steer && m.meta?.sendId) {
        m.meta = { ...(m.meta || {}), optimistic: true }
      }
      state.messages.push(ensureMsgId(m))
    },
    /** Optimistically append a message to a specific slot's store — global
     *  `messages` when it's the active slot, else `slotMessages[slot]`. Lets a
     *  grid pane show a just-sent user message immediately in the right place. */
    appendSlotMessage(state, action: PayloadAction<{ slot: string; message: ChatMessage }>) {
      const { slot, message } = action.payload
      if (isUnsafeKey(slot)) return
      // Same reasoning as appendMessage: no card retirement on an optimistic
      // append — the pane's send path dispatches retireStatelessQuestion once
      // the server confirms delivery.
      const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[safeKey(slot)] ??= [])
      // Reconcile a steer echo (server 'steer_push', meta.steer, no optimistic
      // flag) against the optimistic bubble that steer() added client-side
      // (meta.optimistic). Update it in place rather than pushing a duplicate
      // user message — mirrors the user-frame reconcile in applyMessageToArray.
      //
      // The optimistic bubble is NOT necessarily the last message: a steer is
      // by definition sent mid-turn, so streaming/thinking/tool messages keep
      // landing between the optimistic append and the WS echo. A tail-only
      // check loses that race and renders a duplicate "Steered into the
      // running turn" card. Resolution (#6075) pairs strictly by id CLASS:
      // an echo carrying a `sendId` matches by id ONLY, and an ID-LESS echo
      // pairs only with ID-LESS bubbles. The gateway serves this SPA bundle,
      // so client and gateway do not skew: an id-less echo does not mean "an
      // old gateway stripped the id" — it means the POST carried none (a
      // scene-interaction steer, a non-minting caller), i.e. a DIFFERENT send
      // whose echo can never name this tab's id-bearing bubble. Consuming
      // across classes shows the wrong message twice over: the id-bearing
      // bubble adopts the foreign echo's text, and its own later exact-id
      // echo is then suppressed by the redelivery guard. An unmatched echo
      // inserts instead — over-insert is the recoverable direction. A
      // NON-optimistic user row already carrying an id-bearing echo's id
      // means the row was ALREADY installed (the chat_done refresh can
      // replace the bubble with the persisted row before a delayed echo is
      // processed) — that echo is a redelivery and inserts nothing. Within
      // the id-less pairing, prefer exactly matching content, else the most
      // recent id-less optimistic STEER bubble (a plain optimistic user
      // message with coincidentally identical text must never be consumed;
      // server-side redaction can alter the echoed content, so an exact match
      // isn't guaranteed).
      if (message.role === 'user' && message.meta?.steer && !message.meta?.optimistic) {
        const echoSid = typeof message.meta?.sendId === 'string' && message.meta.sendId ? message.meta.sendId : ''
        const floor = Math.max(0, msgs.length - 50)
        let target: ChatMessage | undefined
        let fallback: ChatMessage | undefined
        for (let i = msgs.length - 1; i >= floor; i--) {
          const m = msgs[i]
          if (m.role !== 'user') continue
          const rowSid = typeof m.meta?.sendId === 'string' && m.meta.sendId ? m.meta.sendId : ''
          if (echoSid && rowSid === echoSid && !m.meta?.optimistic) return
          if (!m.meta?.optimistic || !m.meta?.steer) continue
          if (echoSid) {
            // Id-bearing echo: the match is exact or there is no match.
            if (rowSid === echoSid) { target = m; break }
            continue
          }
          // Id-less echo: an id-bearing bubble belongs to a send whose own
          // exact-id echo is still coming — never consume it here.
          if (rowSid) continue
          if (message.content && m.content === message.content) { target = m; break }
          if (!fallback) fallback = m
        }
        const bubble = target ?? fallback
        if (bubble) {
          if (message.content) bubble.content = message.content
          // Preserve the optimistic (client-generated) ts as meta.clientTs
          // BEFORE overwriting with the server ts. The chat renderer keys
          // rows by `meta.clientTs ?? ts`; without this stash the ts change
          // would change the React key, remounting the bubble and replaying
          // the one-shot steer entrance animation (visible flicker).
          if (message.ts && bubble.ts && message.ts !== bubble.ts) {
            bubble.meta = { ...(bubble.meta || {}), clientTs: bubble.ts }
          }
          if (message.ts) bubble.ts = message.ts
          bubble.meta = { ...(bubble.meta || {}), ...(message.meta || {}) }
          delete (bubble.meta as Record<string, unknown>).optimistic
          return
        }
        // No optimistic bubble to reconcile — this tab did not initiate the
        // steer (another tab / a scene-interaction steer). Finalize-on-steer
        // before pushing, same as appendMessage: inserting the bubble below a
        // live streaming message strands the streaming marker above it. Only
        // done on the insert path — after a reconcile a NEW post-steer
        // streaming message may already be live below the bubble, and freezing
        // it here would wrongly finalize the in-flight stream.
        finalizeTrailingStreaming(msgs)
      }
      // Optimistic steer bubble from a pane-scoped composer: same freeze as the
      // appendMessage (active-slot) path.
      if (message.role === 'user' && message.meta?.steer && message.meta?.optimistic) {
        finalizeTrailingStreaming(msgs)
      }
      // Mark non-steer user bubbles as optimistic so the sseChatMessage
      // reconcile can distinguish them from channel-replayed messages (#2845).
      if (message.role === 'user' && !message.meta?.steer && message.meta?.sendId) {
        message.meta = { ...(message.meta || {}), optimistic: true }
      }
      msgs.push(ensureMsgId(message))
    },
    updateStreamingMessage(state, action: PayloadAction<string>) {
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'streaming') { last.content = action.payload }
      else { state.messages.push({ role: 'streaming', content: action.payload, cls: 'msg msg-a', meta: { clientTs: mintMsgId() } }) }
    },
    finalizeAssistant(state, action: PayloadAction<string | { content: string; ts?: string }>) {
      const payload = typeof action.payload === 'string' ? { content: action.payload } : action.payload
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'streaming') { last.role = 'assistant'; last.content = payload.content; if (payload.ts) last.ts = payload.ts }
      else { state.messages.push({ role: 'assistant', content: payload.content, cls: 'msg msg-a', ts: payload.ts }) }
    },
    removeThinking(state) { state.messages = state.messages.filter(m => m.role !== 'thinking') },
    /** Retire a bubble's "pending confirmation" state once the send's own HTTP
     *  response accepted it (`ok` or `queued`).
     *
     *  This is the PRIMARY confirmation path, not a fallback. The `chat_message`
     *  echo that `reconcileOptimisticEcho` waits for is only broadcast for rows
     *  the composer did NOT render — a message typed in a channel and replayed
     *  into the slot (`channel_slots`, the sole `broadcast_user=True` caller).
     *  `DashboardState.append` suppresses it for every dashboard send by design,
     *  precisely BECAUSE the composer already rendered the bubble, so waiting on
     *  it left every composer bubble optimistic forever and the 30s sweep flagged
     *  all of them (#4131).
     *
     *  Clears only the pending-confirmation flags and deliberately KEEPS
     *  `sendId`: a channel-linked slot can still deliver a later echo, and
     *  `reconcileOptimisticEcho` needs that id to update this row in place
     *  instead of pushing a duplicate bubble.
     *
     *  Scans BOTH arrays rather than resolving the slot's own: `appendMessage`
     *  pushes into the active `messages` while `appendSlotMessage` may have used
     *  `slotMessages[slot]`, and the user can switch sessions while the POST is
     *  in flight. `sendId` is unique per send, so scanning both cannot mis-hit. */
    confirmOptimisticSend(state, action: PayloadAction<{ slot: string; sendId: string; mid?: string }>) {
      const { slot, sendId, mid } = action.payload
      if (isUnsafeKey(slot)) return
      const confirm = (msgs: ChatMessage[] | undefined): boolean => {
        if (!msgs) return false
        const floor = Math.max(0, msgs.length - RECONCILE_WINDOW)
        for (let i = msgs.length - 1; i >= floor; i--) {
          const m = msgs[i]
          if (m.role !== 'user' || m.meta?.sendId !== sendId) continue
          const meta = { ...(m.meta || {}) }
          delete meta.optimistic
          // Stamp the server-minted row id the receipt carried back. The bubble
          // was appended client-side with only a `sendId` (no server identity),
          // and no `chat_message` echo carries the `mid` for a dashboard send,
          // so this is the only point it can land before the chat_done refresh.
          // The message-pin control is gated on `meta.mid`, so without it the
          // just-sent message cannot be pinned for the whole turn. Only set when
          // the row has none yet — never overwrite a `mid` a refresh already
          // reconciled (identity must not change once assigned).
          if (mid && !meta.mid) meta.mid = mid
          m.meta = meta
          return true
        }
        return false
      }
      if (!confirm(state.messages)) confirm(state.slotMessages[safeKey(slot)])
    },
    /** Resolve an optimistic steer bubble against the steer POST's own receipt.
     *
     *  `meta.steer` draws the "Steered into the running turn" badge, so it is an
     *  affirmative claim, and only `steered: true` makes it true. `queued: true`
     *  means the text sits in the slot queue, and EVERY arm reporting it has
     *  already broadcast a `queue_push` — including the turn teardown, which is
     *  why the requeued arm does not re-broadcast. That card owns the text, so
     *  the bubble is REMOVED or the same message renders twice. A receipt with
     *  neither flag raced `chat_done` onto a new turn: only the flag drops. No
     *  receipt in time: the caller takes the `queued` (drop) arm -- see below.
     *
     *  Both modes need the bubble still `optimistic` — once an echo or
     *  `confirmOptimisticSend` cleared that, the server owns the row. Scans BOTH
     *  arrays as `confirmOptimisticSend` does; `sendId` is unique per send. */
    resolveOptimisticSteer(state, action: PayloadAction<{ slot: string; sendId: string; outcome: 'queued' | 'turn' }>) {
      const { slot, sendId, outcome } = action.payload
      if (isUnsafeKey(slot)) return
      const resolve = (msgs: ChatMessage[] | undefined): boolean => {
        if (!msgs) return false
        const floor = Math.max(0, msgs.length - RECONCILE_WINDOW)
        for (let i = msgs.length - 1; i >= floor; i--) {
          const m = msgs[i]
          if (m.role !== 'user' || m.meta?.sendId !== sendId) continue
          if (!m.meta?.steer || !m.meta?.optimistic) return true
          // The drop arm. Also taken for a steer whose receipt never came (the
          // transport's deadline aborted the POST and the text went back to the
          // composer): a bubble left standing would read as delivered, and a
          // late `steer_push` echo that does arrive re-creates the row from the
          // server's copy (reconcileOptimisticEcho appends when no row carries
          // the sendId).
          if (outcome === 'queued') { msgs.splice(i, 1); return true }
          const meta = { ...(m.meta || {}) }
          delete meta.steer
          m.meta = meta
          return true
        }
        return false
      }
      if (!resolve(state.messages)) resolve(state.slotMessages[safeKey(slot)])
    },
    /** Age the slot's folder-suggestion card by one delivered user send, and
     *  drop it once it has had its run (> FOLDER_SUGGESTION_MAX_TURNS).
     *
     *  Deliberately its OWN action, dispatched ONLY by the render site that
     *  showed the card (ChatPage's composer band, active slot) after the server
     *  confirmed the send was delivered — never baked into a shared send
     *  reducer. The two review rounds that shaped this: counting the optimistic
     *  `startLocalTurn` let FAILED sends burn the one-shot offer, and counting
     *  `confirmOptimisticSend` let surfaces that confirm sends WITHOUT rendering
     *  the card (ChatPane in artifact companion chats, sidebar panes, settings
     *  embeds) expire a card the user never saw. Tying aging to an explicit
     *  dispatch from the renderer makes every "send from a surface that does not
     *  show the card" variant unreachable by construction.
     *
     *  `ts` guards the in-flight-replacement race the way `clearFolderSuggestion`
     *  does: the POST that earns this dispatch was sent while ONE card
     *  generation was visible, and a replacement arriving before the response
     *  must not inherit its age. */
    ageFolderSuggestion(state, action: PayloadAction<{ slot: string; ts?: number }>) {
      const { slot, ts } = action.payload
      if (isUnsafeKey(slot)) return
      const suggestion = state.folderSuggestions?.[slot]
      if (!suggestion) return
      if (ts != null && suggestion.ts !== ts) return
      suggestion.turns = (suggestion.turns ?? 0) + 1
      if (suggestion.turns > FOLDER_SUGGESTION_MAX_TURNS) delete state.folderSuggestions[slot]
    },
    removeByApprovalId(state, action: PayloadAction<string>) { state.messages = state.messages.filter(m => m.meta?.approval_id !== action.payload) },
    resolveByApprovalId(state, action: PayloadAction<{ id: string; decision?: string }>) {
      const decision = action.payload.decision || 'approved'
      let m = state.messages.find(m => m.meta?.approval_id === action.payload.id)
      if (!m) {
        for (const arr of Object.values(state.slotMessages)) {
          const f = arr.find(x => x.meta?.approval_id === action.payload.id)
          if (f) { m = f; break }
        }
      }
      if (m?.meta) m.meta.resolved = decision
      // If rejected, mark the matching toolLog entry so the pill can show a rejection icon.
      // Every rejection token counts: a reject-once that missed this would leave
      // the pill unmarked, and ToolCallLine then reads its 🚫 sibling as an
      // auto-deny and paints a human refusal as a policy block.
      const toolCallId = m?.meta?.tool_call_id as string | undefined
      if (isRejectedDecision(decision) && toolCallId) {
        const log = state.toolLog
        for (let i = log.length - 1; i >= 0; i--) {
          if (log[i].type === 'tool' && log[i].tool_call_id === toolCallId) {
            log[i].rejected = true; break
          }
        }
      }
    },
    /** Mark all unresolved permission messages as resolved (e.g. when stop is pressed). */
    clearPendingPermissions(state) {
      for (const m of state.messages) {
        if (m.role === 'permission' && !m.meta?.resolved) {
          if (m.meta) m.meta.resolved = 'rejected'
          else m.meta = { resolved: 'rejected' }
        }
      }
      // Mark all incomplete toolLog entries as rejected so pills show the right icon
      for (const e of state.toolLog) {
        if (e.type === 'tool' && e.output == null && !e.rejected) e.rejected = true
      }
    },
    setSlotRunning(state, action: PayloadAction<boolean>) {
      state.slotRunning = action.payload
      if (!action.payload) state.pendingTurnSlot = null
    },
    /** Optimistically start a turn for `slot` after a local send. Marks it
     *  pending so the slots-sync won't clobber running=true before the server
     *  catches up. Only the active slot drives the visible footer. */
    startLocalTurn(state, action: PayloadAction<string>) {
      const slot = action.payload
      state.pendingTurnSlot = slot
      if (slot === state.activeSlot) state.slotRunning = true
    },
    /** The inverse of `startLocalTurn` for a send that did NOT start a turn
     *  (refused, or never left). Slot-keyed like its counterpart: only the slot
     *  the send was for loses its pending mark, and only when that slot is the
     *  active one does the visible footer change -- a failure that lands after
     *  the user switched to a RUNNING session must not clear that session's
     *  running state (which `setSlotRunning(false)` would). */
    endLocalTurn(state, action: PayloadAction<string>) {
      const slot = action.payload
      if (state.pendingTurnSlot === slot) state.pendingTurnSlot = null
      if (slot === state.activeSlot) state.slotRunning = false
    },
    /** Reconcile the active slot's running state from a WS slots broadcast.
     *  running=true is always trusted (also catches Slack/cron-initiated turns);
     *  running=false is ignored while a local turn is pending confirmation, since
     *  the snapshot may predate the send. Turn end is owned by _done/refreshSlot. */
    syncSlotRunningFromServer(state, action: PayloadAction<{ slot: string; running: boolean; stopping: boolean }>) {
      const { slot, running, stopping } = action.payload
      if (slot !== state.activeSlot) return
      if (running) {
        state.slotRunning = true
        state.slotStopping = stopping
        state.pendingTurnSlot = null
      } else if (state.pendingTurnSlot !== slot) {
        state.slotRunning = false
        state.slotStopping = stopping
      }
      // Pending turn: ignore both fields so a leftover stopping=true from a
      // prior turn can't falsely show a "stopping" state on the new turn.
    },
    setSlotStopping(state, action: PayloadAction<boolean>) { state.slotStopping = action.payload },
    setStopPressedAt(state, action: PayloadAction<{ slotId: string; ts: number }>) {
      if (isUnsafeKey(action.payload.slotId)) return
      state.stopPressedAt[safeKey(action.payload.slotId)] = action.payload.ts
    },
    setSlotState(state, action: PayloadAction<SlotState>) { state.slotState = action.payload },
    /** Replace a slot's live status line wholesale. A `tool` phase may carry the
     *  `toolCallId` it describes so a later refinement of the SAME call can be
     *  merged into it (see the `tool_call` case in useWebSocket) without a
     *  refinement of one call inheriting a sibling's purpose when tools run in
     *  parallel. */
    setSlotStatusDetail(state, action: PayloadAction<{ slot: string; kind: string; text: string; ts: number; toolName?: string; toolCallId?: string }>) {
      const { slot, ...detail } = action.payload
      if (isUnsafeKey(slot)) return
      state.slotStatusDetail[safeKey(slot)] = detail
    },
    clearMessages(state) { state.messages = []; setPagingCursor(state, false, 0); state.voiceAudio = null; state.voicePlaying = false; if (state.activeSlot) delete state.thinkingOrphans?.[safeKey(state.activeSlot)]; if (state.activeSlot) evictMcpApps(state, state.activeSlot); if (state.activeSlot) writeSlotPage(state, state.activeSlot, [], false) },
    /** A server-confirmed clear for a slot that is NOT the active view. The
     *  active-slot case routes through `clearMessages`; this one exists so a
     *  background slot's cached page cannot outlive its authoritative clear --
     *  the failed-switch restore re-hydrates from that cache, and a grid pane
     *  reads it directly, so a survivor resurrects a transcript the backend
     *  already discarded (#6364 review). */
    clearSlotCache(state, action: PayloadAction<string>) {
      const slot = action.payload
      if (isUnsafeKey(slot)) return
      writeSlotPage(state, slot, [], false)
      delete state.thinkingOrphans?.[safeKey(slot)]
      evictMcpApps(state, slot)
    },
    truncateAfterIndex(state, action: PayloadAction<number>) { state.messages = state.messages.slice(0, action.payload) },
    replaceMessages(state, action: PayloadAction<ChatMessage[]>) { state.messages = action.payload },
    /** Path B: seed a non-active slot's message history into the per-slot store
     *  (one-time hydrate on pane mount). Prepends the server history BEFORE any
     *  frames that already arrived live: applyNonActiveFrame seeds slotMessages
     *  via `??= []` on the first WS frame, so `cur` can be non-empty before this
     *  hydrate fetch resolves. A dedicated `slotHydrated` flag makes it fire
     *  exactly once, so a racing frame can't make us silently drop history.
     *
     *  One exception to "exactly once": a pane that mounts idle fetches a BOUNDED
     *  page, and the slot can start a turn before that page lands. The pane then
     *  refetches unbounded, and a flat one-shot would discard the wider result and
     *  strand the pane on 50 rows. So a bounded page may be superseded once by an
     *  unbounded one. The reverse is refused, and a superseded slot cannot upgrade
     *  again, so this cannot loop.
     *  No-op for the active slot (its mirror is already live). */
    hydrateSlotMessages(state, action: PayloadAction<{ slot: string; messages: ChatMessage[]; hasMore?: boolean; bounded?: boolean; total?: number; running?: boolean }>) {
      const { slot, messages, hasMore, bounded, total, running } = action.payload
      if (isUnsafeKey(slot)) return
      if (slot === state.activeSlot) return
      const k = safeKey(slot)
      // Only retainer that can seed a BACKGROUND slot -- the others sit behind an
      // activeSlot guard. Accept paths only: a declined page is not evidence.
      if (state.slotHydrated?.[slot]) {
        // Keep the rows the bounded page never fetched: it was written as
        // [page, ...priorRows], so everything past its length is a live tail.
        const boundedLen = state.slotPaneBounded?.[k]
        if (bounded || boundedLen === undefined) return
        const prior = state.slotMessages[k] ?? []
        // The wider page is a fresh server snapshot, so it can already carry rows
        // that tail holds -- a just-sent row persists before its send is acked.
        const tail = tailNotInPage(prior.slice(boundedLen), messages)
        // Reasoning is broadcast-only so the wider page never carries it back.
        // Scoped to the REPLACED region: `tail` already keeps the live tail's own.
        writeSlotPage(state, slot, mergePreservedThinking(prior.slice(0, boundedLen), [...messages, ...tail], messages), hasMore)
        retainServerTotal(state, slot, total, running)
        return
      }
      const cur = state.slotMessages[slot] ?? []
      if (!state.slotHydrated) state.slotHydrated = {}
      state.slotHydrated[k] = true
      // Only a page write records a marker, so its presence means `cur` is a
      // loaded transcript, and prepending a bounded tail onto that reorders it.
      if (state.slotPaneHasMore?.[k] !== undefined) return
      // Seeded frames are NEWER rows appended after the page, so the page's
      // has-more still describes what precedes it; dropping it hid the marker.
      writeSlotPage(state, slot, [...messages, ...cur], hasMore, bounded ? messages.length : undefined)
      retainServerTotal(state, slot, total, running)
    },
    setVoicePlaying(state, action: PayloadAction<boolean>) { state.voicePlaying = action.payload },
    setVoiceAudio(state, action: PayloadAction<string | null>) { state.voiceAudio = action.payload },
    toggleActivity(state) { state.activityOpen = !state.activityOpen; if (!state.activityOpen) state.focusToolCallId = null; persistActivityOpen(state.activeSlot, state.activityOpen) },
    openActivityPanel(state) { state.activityOpen = true; persistActivityOpen(state.activeSlot, true) },
    openActivityToTab(state, action: PayloadAction<'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'links' | 'side' | 'artifacts'>) { state.activityOpen = true; state.activityTab = action.payload; state.activityTabRequest += 1; state.focusToolCallId = null; persistActivityOpen(state.activeSlot, true) },
    /** Tool details expand inline in the chat. This action signals the matching
     *  ToolCallLine pill to auto-expand and scroll into view. */
    openActivityToTool(state, action: PayloadAction<string>) { state.focusToolCallId = action.payload },
    /** Clear after the matching pill has consumed the focus signal, so the same trigger
     *  doesn't re-fire on subsequent re-renders. */
    clearFocusToolCallId(state) { state.focusToolCallId = null },
    /** Ask the sidebar to reveal a session row (expand collapsed ancestor
     *  folders, scroll it into view, flash it). Consumed and cleared by
     *  ChatSidebar once it is mounted and ready — see `revealRequest`. */
    requestSlotReveal(state, action: PayloadAction<string>) { state.revealNonce += 1; state.revealRequest = { key: action.payload, nonce: state.revealNonce } },
    clearSlotReveal(state) { state.revealRequest = null },
    /** Drop the previous connection's ephemeral subagent view before the gateway
     *  replays its authoritative running/done snapshot. Without this reset, an
     *  empty replay leaves agents from a restarted gateway visible indefinitely.
     *  Pending spawn-approval cards are preserved: the subscribe_subagents replay
     *  only re-emits native + managed running/done agents, so a card still
     *  awaiting approval has no backend SubagentInfo to hydrate it and would be
     *  lost (its approve/reject UI along with it) on a mid-approval reconnect. */
    clearSubagentsForSnapshot(state) {
      const keepPending = (subs: Record<string, SubagentActivity> | undefined): Record<string, SubagentActivity> => {
        const kept: Record<string, SubagentActivity> = {}
        if (subs) for (const [id, a] of Object.entries(subs)) if (a.status === 'pending') kept[id] = a
        return kept
      }
      state.subagents = keepPending(state.subagents)
      for (const activity of Object.values(state.slotActivity)) activity.subagents = keepPending(activity.subagents)
      // Queued counts are advisory and re-emitted on the next drain — reset to
      // avoid showing a stale "waiting" count for a wave that finished during
      // the disconnect (under-count self-heals on the next drain frame).
      state.subagentQueued = {}
    },
    /** Aggregate "waiting to start" count for a slot. Agents queued behind the
     *  concurrency cap / stagger gate have no individual card; this count lets
     *  the chip appear immediately on spawn and show how many are pending
     *  start (issues: late chip, flicker, invisible queue). */
    sseSubagentQueued(state, action: PayloadAction<{ slot: string; queued: number }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const n = Math.max(0, Math.floor(Number(action.payload.queued) || 0))
      // Tolerate a store built from partial preloaded state (test fixtures and
      // any consumer that predates this key): indexing an absent map throws and
      // would drop the queue update entirely.
      state.subagentQueued ??= {}
      if (n === 0) delete state.subagentQueued[safeKey(action.payload.slot)]
      else state.subagentQueued[safeKey(action.payload.slot)] = n
    },
    /** Replace the whole goal-loop map from a cold `GET /api/autonudge` seed.
     *  A full replace (not a merge) is correct here: the response is the
     *  service's complete registry, so a loop this client still holds but the
     *  server no longer reports has ended and must disappear. */
    setGoalLoops(state, action: PayloadAction<{ slot: string; active: boolean; cycle_count: number; max_cycles: number }[]>) {
      const next: Record<string, { cycle_count: number; max_cycles: number }> = {}
      for (const loop of action.payload) {
        if (!loop.active || isUnsafeKey(loop.slot)) continue
        next[safeKey(loop.slot)] = {
          cycle_count: Math.max(0, Math.floor(Number(loop.cycle_count) || 0)),
          max_cycles: Math.max(0, Math.floor(Number(loop.max_cycles) || 0)),
        }
      }
      state.goalLoops = next
    },
    /** Upsert (or drop) one loop from an `autonudge_state` WS event. */
    sseGoalLoop(state, action: PayloadAction<{ slot: string; active: boolean; cycle_count: number; max_cycles: number }>) {
      const { slot, active } = action.payload
      if (isUnsafeKey(slot)) return
      // Same partial-preloaded-state tolerance as subagentQueued above.
      state.goalLoops ??= {}
      if (!active) { delete state.goalLoops[safeKey(slot)]; return }
      state.goalLoops[safeKey(slot)] = {
        cycle_count: Math.max(0, Math.floor(Number(action.payload.cycle_count) || 0)),
        max_cycles: Math.max(0, Math.floor(Number(action.payload.max_cycles) || 0)),
      }
    },
    sseSubagentPending(state, action: PayloadAction<{ slot: string; id: string; task: string; approval_id: string }>) {
      if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
      const entry: SubagentActivity = {
        id: action.payload.id, task: action.payload.task, agent: '',
        status: 'pending', streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
        approval_id: action.payload.approval_id,
      }
      if (action.payload.slot !== state.activeSlot) {
        const c = state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }
        c.subagents[safeKey(action.payload.id)] = entry
        return
      }
      state.subagents[safeKey(action.payload.id)] = entry
    },
    markSubagentApproving(state, action: PayloadAction<{ id: string; approving: boolean }>) {
      if (isUnsafeKey(action.payload.id)) return
      const a = state.subagents[action.payload.id]
      if (a) { a.approving = action.payload.approving; return }
      for (const sa of Object.values(state.slotActivity)) {
        const b = sa.subagents[action.payload.id]
        if (b) { b.approving = action.payload.approving; return }
      }
    },
    sseSubagentSpawn(state, action: PayloadAction<{ slot: string; id: string; task: string; agent: string; model?: string; requested_model?: string; child_session?: string }>) {
      if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[action.payload.id]
      if (existing?.status === 'pending') {
        existing.status = 'running'
        existing.agent = action.payload.agent || existing.agent || 'kirocrew'
        // Only overwrite a known model with another known one — never clobber a
        // resolved id back to '' if a later frame omits it.
        if (action.payload.model) existing.model = action.payload.model
        // Same guard for requestedModel: only set when the frame carries a value.
        if (action.payload.requested_model) existing.requestedModel = action.payload.requested_model
        if (action.payload.child_session) existing.childSession = action.payload.child_session
        // The spawn event carries the authoritative task text (the pending
        // card's task is derived from the approval title, which may be empty
        // or just "spawn_run") — always prefer the spawn payload's task.
        if (action.payload.task) existing.task = action.payload.task
        return
      }
      subs[safeKey(action.payload.id)] = {
        id: action.payload.id, task: action.payload.task, agent: action.payload.agent || 'kirocrew',
        model: action.payload.model || '',
        requestedModel: action.payload.requested_model || existing?.requestedModel || undefined,
        childSession: action.payload.child_session || undefined,
        status: 'running', streaming: existing?.streaming || '', lastTool: '', startedAt: existing?.startedAt || Date.now(), elapsed: 0,
        toolCount: 0, stalled: false,
      }
    },
    sseSubagentTool(state, action: PayloadAction<{ slot: string; id: string; tool: string; turns?: number; tool_count?: number }>) {
      const { slot, id } = action.payload
      // Prototype-pollution guard is centralized in getSlotSub.
      const a = getSlotSub(state, slot, id)
      if (a) {
        a.lastTool = action.payload.tool; a.status = 'tool'
        if (typeof action.payload.tool_count === 'number') a.toolCount = action.payload.tool_count
        a.stalled = false
        a.idleSecs = undefined
        a.stalledAt = undefined
        a.retrying = false
      }
    },
    sseSubagentRetrying(state, action: PayloadAction<{ slot: string; id: string; attempt?: number }>) {
      // Fired for both transient-backend retries (subagent_retrying) and the
      // one-shot cancel auto-continue (subagent_recovering): the agent is
      // still alive and recovering — show ⟳ instead of letting it look hung.
      const { slot, id } = action.payload
      if (id === '__proto__' || id === 'constructor' || id === 'prototype') return
      const a = getSlotSubs(state, slot)?.[id]
      if (a) { a.retrying = true; a.stalled = false; a.idleSecs = undefined; a.stalledAt = undefined }
    },
    sseSubagentStalled(state, action: PayloadAction<{ slot: string; id: string; stalled: boolean; idle_secs?: number }>) {
      const { slot, id } = action.payload
      // Prototype-pollution guard is centralized in getSlotSub.
      const a = getSlotSub(state, slot, id)
      if (!a) return
      a.stalled = action.payload.stalled
      // Keep the idle span with the flag it justifies, and clear it on the
      // un-stall frame so a resumed agent cannot keep showing a stale
      // "no activity for Ns" from its previous quiet stretch.
      // `stalledAt` is the receipt instant: the backend emits `idle_secs` only on
      // the transition, so the row advances the figure from here rather than
      // freezing it next to a live elapsed counter.
      a.idleSecs = action.payload.stalled ? action.payload.idle_secs : undefined
      a.stalledAt = action.payload.stalled ? Date.now() : undefined
    },
    /** One coalesced ~1s frame carrying the latest delta per agent (scale
     *  plumbing — replaces per-event tool/stalled/retrying frames when many
     *  agents run). Field presence decides what to apply; latest wins. */
    sseSubagentBatchUpdate(state, action: PayloadAction<{ updates: { id: string; slot: string; tool?: string; tool_count?: number; stalled?: boolean; idle_secs?: number; attempt?: number }[] }>) {
      for (const u of action.payload.updates || []) {
        const a = getSlotSub(state, u.slot, u.id)
        if (!a) continue
        // Order matters: retrying (attempt) applies FIRST so a tool field in
        // the same merged entry — meaning work resumed — clears it last.
        if (typeof u.attempt === 'number') { a.retrying = true; a.stalled = false; a.idleSecs = undefined; a.stalledAt = undefined }
        if (typeof u.tool === 'string' && u.tool) { a.lastTool = u.tool; if (a.status === 'running') a.status = 'tool'; a.retrying = false }
        if (typeof u.tool_count === 'number') a.toolCount = u.tool_count
        if (typeof u.stalled === 'boolean') {
          a.stalled = u.stalled
          // Mirror the per-event frame: the idle span lives and dies with the
          // flag, so a coalesced un-stall cannot leave a stale idle figure.
          a.idleSecs = u.stalled ? u.idle_secs : undefined
          a.stalledAt = u.stalled ? Date.now() : undefined
        }
      }
    },
    /** One coalesced ~1s frame of concatenated streaming text per agent. */
    sseSubagentBatchChunks(state, action: PayloadAction<{ chunks: { id: string; slot: string; text: string }[] }>) {
      for (const c of action.payload.chunks || []) {
        const a = getSlotSub(state, c.slot, c.id)
        if (!a) continue
        a.retrying = false
        a.streaming += c.text
        if (a.streaming.length > 50_000) {
          a.streaming = i18nT('store.chatSlice.truncated') + '\n' + a.streaming.slice(-40_000)
        }
      }
    },
    /** Chip row click → the Activity tab scrolls to/expands this agent. */
    selectSubagent(state, action: PayloadAction<string | null>) {
      state.selectedSubagentId = action.payload
    },
    /** "Dismiss done": drop terminal cards for a slot (backend clear is the
     *  caller's job via DELETE /api/spawn; this trims the local view). */
    clearTerminalSubagents(state, action: PayloadAction<{ slot: string }>) {
      const slot = action.payload.slot
      if (isUnsafeKey(slot)) return
      const subs = slot !== state.activeSlot
        ? state.slotActivity[safeKey(slot)]?.subagents
        : state.subagents
      if (!subs) return
      for (const id of Object.keys(subs)) {
        const st = subs[id]?.status
        if (st === 'done' || st === 'error' || st === 'stopped') delete subs[id]
      }
    },
    sseSubagentDone(state, action: PayloadAction<{ slot: string; id: string; elapsed: number; error?: string; stopped?: boolean; outcome?: 'completed' | 'failed' | 'stopped'; task?: string; agent?: string; model?: string; requested_model?: string; child_session?: string; result?: string }>) {
      if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      let a = subs[action.payload.id]
      if (!a) {
        // Cross-slot fallback: the card may live under a different slot key.
        if (state.subagents[action.payload.id]) a = state.subagents[action.payload.id]
        else {
          for (const sa of Object.values(state.slotActivity)) {
            if (sa.subagents[action.payload.id]) { a = sa.subagents[action.payload.id]; break }
          }
        }
      }
      const isNative = action.payload.id.startsWith('native:')
      // Canonical terminal classification: `outcome` is the single source
      // (spec: docs/system-specs/modules/subagent.md). `stopped`/`error`
      // derivation is kept ONLY as a fallback for old payloads that predate
      // the field (reconnect replays from a pre-upgrade gateway).
      const doneStatus: 'stopped' | 'error' | 'done' =
        action.payload.outcome === 'stopped' ? 'stopped'
          : action.payload.outcome === 'failed' ? 'error'
            : action.payload.outcome === 'completed' ? 'done'
              : action.payload.stopped ? 'stopped' : (action.payload.error ? 'error' : 'done')
      if (a) {
        a.status = doneStatus
        a.retrying = false
        a.elapsed = action.payload.elapsed
        a.error = doneStatus === 'stopped' ? undefined : action.payload.error
        a.streaming = ''
        if (action.payload.task && !a.task) a.task = action.payload.task
        if (action.payload.agent && !a.agent) a.agent = action.payload.agent
        // The done frame carries the authoritative served model (the CC path
        // has resolved it by completion). Prefer a known value, but never
        // clobber a prior known id back to '' if this frame omits it.
        if (action.payload.model) a.model = action.payload.model
        // Carry the requested pin so a reconnect that rebuilds a completed card
        // (clearSubagentsForSnapshot drops it, then subagent_done rehydrates it)
        // keeps the live-downgrade amber chip. Never clobber a known value to ''.
        if (action.payload.requested_model) a.requestedModel = action.payload.requested_model
        if (action.payload.child_session && !a.childSession) a.childSession = action.payload.child_session
        if (isNative && action.payload.result !== undefined) a.result = action.payload.result
      }
      else {
        subs[action.payload.id] = {
          id: action.payload.id,
          task: action.payload.task || '',
          agent: action.payload.agent || 'kirocrew',
          model: action.payload.model || '',
          requestedModel: action.payload.requested_model || undefined,
          childSession: action.payload.child_session || undefined,
          status: doneStatus,
          streaming: '',
          lastTool: '',
          startedAt: Date.now() - action.payload.elapsed * 1000,
          elapsed: action.payload.elapsed,
          error: doneStatus === 'stopped' ? undefined : action.payload.error,
          result: isNative ? action.payload.result : undefined,
        }
      }
    },
    sseSideResult(state, action: PayloadAction<{ slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; is_error?: boolean; final?: boolean; steer?: boolean }>) {
      const { slot, run_id, role, content, ts, is_error, final, steer } = action.payload
      if (isUnsafeKey(slot)) return
      const tsIso = typeof ts === 'number' ? new Date(ts * 1000).toISOString() : new Date().toISOString()
      // A steer is an echo of the conversation being closed, never a request to re-open it,
      // so it is dropped for a tombstoned slot. Checked BEFORE the re-open branch below:
      // a steer carries `role === 'user'`, so that branch would clear the tombstone and
      // make the late-frame guard unreachable, filing the old steer into the next
      // conversation.
      if (steer && state.slotSideClosed[slot]) return
      // Intentional re-open (new user frame) clears the closed sentinel
      if (role === 'user' && state.slotSideClosed[slot]) {
        delete state.slotSideClosed[slot]
      }
      // Block late assistant chunks after sideClose
      if (!state.slotSide[slot] && state.slotSideClosed[slot]) return
      if (!state.slotSide[slot]) {
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: tsIso }
      }
      const side: SideState = state.slotSide[slot]
      if (role === 'user') {
        if (steer) {
          // A steer joins a turn whose answer is already streaming. Land the chip
          // ABOVE that answer: the terminal frame replaces the whole assistant
          // text, so it must still match that row — putting the user bubble after
          // it would strand the reply and make the terminal frame append the full
          // text a second time.
          //
          // Locate the row by run, not by position. The steer RPC can settle after
          // the NEXT queued turn has already started, so this run's answer may no
          // longer be the tail; appending then would file an older steer below a
          // newer turn and scramble the transcript.
          const entry: SideMessage = { role: 'user', content, ts: tsIso, run_id, steer: true }
          let answerIdx = -1
          for (let i = side.messages.length - 1; i >= 0; i--) {
            const row = side.messages[i]
            if (row.role === 'assistant' && row.run_id === run_id) {
              answerIdx = i
              break
            }
          }
          if (answerIdx >= 0) {
            side.messages.splice(answerIdx, 0, entry)
          } else {
            // No answer for this run yet — the chip legitimately precedes it.
            side.messages.push(entry)
          }
          // Deliberately touches NEITHER pending/streaming NOR lastRunId. A steer
          // frame can arrive after its turn's terminal frame, or after a later turn
          // has begun; reviving busy state strands the panel (no later frame would
          // clear it) and rewriting lastRunId regresses run identity to a turn that
          // already ended. A steer never STARTS a turn, so it owns neither.
          return
        }
        // Reconcile with the optimistic bubble appended in sideOptimisticAppend,
        // found by its MARKER rather than by position. This frame can arrive after
        // the in-flight turn has already streamed assistant text, so the bubble is
        // often no longer the tail — and a positional check then pushes a second
        // bubble for the same question.
        const pendingIdx = side.messages.findIndex(
          m => m.optimistic && m.role === 'user' && m.content === content,
        )
        if (pendingIdx >= 0) {
          const row = side.messages[pendingIdx]
          row.run_id = run_id
          row.ts = tsIso
          delete row.optimistic
        } else {
          side.messages.push({ role: 'user', content, ts: tsIso, run_id })
        }
        side.lastRunId = run_id
        side.pending = true
        side.streaming = true
        return
      }
      side.pending = false
      side.streaming = !final
      if (is_error) {
        side.messages.push({ role: 'assistant', content, ts: tsIso, run_id, is_error: true })
        side.lastRunId = run_id
        return
      }
      const last = side.messages[side.messages.length - 1]
      if (last?.role === 'assistant' && last.run_id === run_id && !last.is_error) {
        if (content === last.content) return
        last.content = content.startsWith(last.content) ? content : last.content + content
        last.ts = tsIso
        return
      }
      side.messages.push({ role: 'assistant', content, ts: tsIso, run_id })
      side.lastRunId = run_id
    },
    sseSideQueue(state, action: PayloadAction<{ slot: string; action: 'push' | 'edit' | 'cancel' | 'drain'; queue_id: string; content?: string; ts?: number; front?: boolean; steer_id?: string; raw?: boolean; suppressRelease?: boolean }>) {
      const { slot, action: kind, queue_id, content, ts, front, steer_id, raw, suppressRelease } = action.payload
      if (isUnsafeKey(slot)) return
      // A queue mutation is never a reason to resurrect a closed side.
      if (!state.slotSide[slot]) {
        if (kind !== 'push' || state.slotSideClosed[slot]) return
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: new Date().toISOString() }
      }
      const side: SideState = state.slotSide[slot]
      if (!side.queue) side.queue = []
      const at = side.queue.findIndex(e => e.id === queue_id)
      if (kind === 'push') {
        // Already drained or cancelled: this push lost the race to the frame that
        // retired it, so materialising a card would show a phantom.
        if (side.removedQueueIds?.includes(queue_id)) return
        const tsIso = typeof ts === 'number' ? new Date(ts * 1000).toISOString() : new Date().toISOString()
        // Replay-safe: a redelivered push must not double the card. It must also
        // not REWRITE it — broadcasts are redacted on the wire, so a late duplicate
        // push carries a scrubbed rendering of text already stored raw from the
        // HTTP response, and overwriting corrupts what a later cancel restores.
        // Content changes arrive as `edit`, never as a second `push`, so ignoring
        // the duplicate's content loses nothing.
        if (at >= 0) return
        // `front` mirrors the backend's own head-insert (a requeued steer, or an
        // entry whose dispatch failed). Appending it instead would show a
        // different next question than the backend will actually run.
        // `steer_id`, when present, says this card is a steer the backend could not
        // confirm and requeued. Kept on the entry because the card's id is new to
        // the client, so this is its only handle for matching the raw text it holds.
        else if (front) side.queue.unshift({ id: queue_id, content: content ?? '', ts: tsIso, ...(steer_id ? { steerId: steer_id } : {}), ...(raw ? { raw: true } : {}) })
        else side.queue.push({ id: queue_id, content: content ?? '', ts: tsIso, ...(steer_id ? { steerId: steer_id } : {}), ...(raw ? { raw: true } : {}) })
        return
      }
      if (at < 0) return
      if (kind === 'edit') {
        // A broadcast edit arrives scrubbed (`ws.py` redacts before sending) and carries no
        // `raw` marker. Applying it over content this client typed would replace the
        // question with `[REDACTED: credential]`, which every reader of the card — a
        // WS-driven cancel, or an HTTP cancel whose cached copy was evicted — would then
        // release into the composer. Raw content is therefore a one-way ratchet.
        if (raw) {
          side.queue[at].content = content ?? side.queue[at].content
          side.queue[at].raw = true
        } else if (!side.queue[at].raw) {
          side.queue[at].content = content ?? side.queue[at].content
        } else {
          // Swallowed on purpose — but this frame is the only proof the server applied an
          // edit to a card this client owns. Recorded so an editor whose HTTP response never
          // arrived can tell "the edit landed" from "the edit failed": restoring the text in
          // the first case leaves the question both queued and in the composer.
          noteQueueEditBroadcast(slot, queue_id)
        }
      }
      else {
        // A cancel releases the entry's text: it is gone from the queue and gone
        // from the server, so the composer is the only place left to hold it.
        // Stashed here rather than restored by the caller because BOTH
        // convergence paths land in this reducer — the HTTP response and the
        // `chat.side_queue` frame — and a lost HTTP response must not mean lost
        // text. The panel drains and clears it, so it releases exactly once.
        if (kind === 'cancel') {
          // Prefer the card's OWN content over the frame's. Broadcast payloads are
          // redacted on the wire (`ws.py` scrubs credentials before sending), while
          // the card was populated from the raw text the user typed via the HTTP
          // response. Taking the frame first handed the composer a permanently
          // redacted question — the user would have to retype the secret, or not
          // notice and send `[REDACTED: credential]` as their prompt.
          //
          // The frame remains the fallback: if its push never populated a card
          // (HTTP lost and only the WS frame arrived) a redacted release still
          // beats losing the question entirely.
          // `raw` marks content the SUBMITTING client vouches for as unredacted. It
          // outranks the card because the card can itself be a redacted broadcast: the
          // edit endpoint broadcasts through the scrubber, and the edit action sets
          // content unconditionally, so an edited credential-bearing card holds the
          // scrubbed copy. Card next (it is raw whenever an HTTP response populated it),
          // frame last so a lost HTTP response still beats losing the question.
          const released = (raw ? content : '') || side.queue[at].content || content || ''
          // ACCUMULATE, never assign. Two cancellations can both settle before the
          // panel's effect consumes this field, and an assignment would drop the
          // first one's text for good — the exact loss this whole feature exists to
          // prevent. The panel merges the accumulated value into the composer as a
          // unit and clears it, so the user edits both questions rather than
          // silently losing one.
          // Another tab's cancel: drop the card here and normally leave the question to the
          // tab that cancelled, so one cancellation does not paste the same question into
          // every open dashboard.
          //
          // EXCEPT when this tab holds the unredacted copy. `raw` means the content came from
          // what the user typed here, and the cancelling tab only ever has the scrubbed
          // broadcast — so staying quiet would drop the only good copy of the question and
          // leave a redacted one behind. Owning the text outranks owning the click.
          const ownsRawCopy = side.queue[at].raw === true
          if (released && (!suppressRelease || ownsRawCopy)) {
            side.releasedText = mergeIntoDraft(side.releasedText, released)
          }
        }
        side.queue.splice(at, 1)
        // Retire the id so a slower HTTP callback cannot bring it back.
        const retired = side.removedQueueIds ?? []
        retired.push(queue_id)
        // Only the recent past can still be raced by an in-flight request, so a
        // small window is enough and keeps this from growing without bound.
        side.removedQueueIds = retired.slice(-MAX_RETIRED_QUEUE_IDS)
      }
    },
    sideReleaseConsumed(state, action: PayloadAction<{ slot: string; consumed: string }>) {
      const { slot, consumed } = action.payload
      const side = state.slotSide[slot]
      if (!side) return
      const current = side.releasedText ?? ''
      // Compare-and-clear, never a blind delete. A cancel can append to this
      // buffer between the consumer's render and its effect, and deleting the
      // whole field then discards text the consumer never saw. Keep whatever was
      // appended after the snapshot it actually drained.
      if (current === consumed || !current.startsWith(consumed)) {
        delete side.releasedText
        return
      }
      side.releasedText = current.slice(consumed.length).replace(/^\s+/, '')
    },
    sideClose(state, action: PayloadAction<string>) {
      delete state.slotSide[action.payload]
      if (isUnsafeKey(action.payload)) return
      state.slotSideClosed[safeKey(action.payload)] = true
    },
    sideOptimisticAppend(state, action: PayloadAction<{ slot: string; message: SideMessage }>) {
      const { slot, message } = action.payload
      if (isUnsafeKey(slot)) return
      if (state.slotSideClosed[slot]) delete state.slotSideClosed[slot]
      if (!state.slotSide[slot]) {
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: message.ts }
      }
      const side = state.slotSide[slot]
      side.messages.push({ ...message, optimistic: true })
      side.pending = true
    },
    sideOptimisticRollback(state, action: PayloadAction<string>) {
      const side = state.slotSide[action.payload]
      if (!side) return
      // By marker, not position: popping "whatever is last" removed a real frame
      // once an in-flight turn's assistant text had landed on top of the bubble.
      const idx = side.messages.findIndex(m => m.optimistic && m.role === 'user')
      if (idx >= 0) side.messages.splice(idx, 1)
      side.pending = false
    },
    sseSubagentSnapshot(state, action: PayloadAction<{ id: string; slot: string; task: string; agent: string; model?: string; requested_model?: string; child_session?: string; streaming: string; last_tool: string; started: number; tool_count?: number; stalled?: boolean; idle_secs?: number }>) {
      const d = action.payload
      // A snapshot without an owning slot is an orphan, not evidence that it
      // belongs to whichever chat this browser happens to show. Popout windows
      // cold-subscribe to the complete replay after activating their own slot;
      // treating `slot: ''` as the active map made every such window adopt all
      // unresolved-parent agents. Fail closed: ownerless runs remain available
      // through the global spawn inventory, but never appear inside a chat.
      if (!d.slot || isUnsafeKey(d.slot) || isUnsafeKey(d.id)) return
      const subs = d.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(d.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[d.id]
      // Live events can interleave with replay because subscription starts before
      // snapshots are sent. Never let a stale running snapshot demote a terminal card.
      if (existing?.status === 'done' || existing?.status === 'error') return
      const stalled = d.stalled ?? false
      subs[safeKey(d.id)] = {
        id: d.id, task: d.task, agent: d.agent || 'kirocrew',
        // Prefer the snapshot's model; fall back to any id a live frame already
        // set, so a reconnect that omits it does not blank the pill.
        model: d.model || existing?.model || '',
        // Same guard for requestedModel: prefer frame value, fall back to existing.
        requestedModel: d.requested_model || existing?.requestedModel || undefined,
        childSession: d.child_session || existing?.childSession || undefined,
        status: d.last_tool ? 'tool' : 'running', streaming: d.streaming, lastTool: d.last_tool,
        startedAt: d.started * 1000, elapsed: 0,
        toolCount: d.tool_count ?? 0, stalled,
        // Same pairing rule as sseSubagentStalled: the idle span lives and dies
        // with the flag it justifies, so a non-stalled snapshot can never carry
        // one. `stalledAt` is the receipt instant — the row advances the figure
        // from here rather than freezing it beside a live elapsed counter.
        // Both stay undefined when the gateway omits `idle_secs`, which keeps
        // the plain "no activity" fallback reachable for an older gateway.
        idleSecs: stalled ? d.idle_secs : undefined,
        stalledAt: stalled && typeof d.idle_secs === 'number' ? Date.now() : undefined,
        // Snapshot rebuilds the entry from scratch, so a `retrying` flag a live
        // subagent_retrying/subagent_recovering frame set just before this
        // replay landed would be dropped — the ⟳ recovering cue would vanish
        // until the next live frame. Carry it forward for the same reason the
        // model pill does above: a reconnect must not blank live-only state.
        // A snapshot never turns retrying ON (it has no attempt field); it only
        // preserves what a live frame already set.
        retrying: existing?.retrying,
        approval_id: existing?.approval_id, approving: existing?.approving,
      }
    },
    /** Fold a single dynamic-workflow run event into workflowRuns. */
    sseWorkflowEvent(state, action: PayloadAction<{ run_id: string; session_key?: string; seq?: number; ts?: number; type: string; data?: Record<string, unknown> }>) {
      const { run_id, type, data, session_key } = action.payload
      if (isUnsafeKey(run_id)) return
      if (!run_id) return
      const d = (data || {}) as Record<string, unknown>
      const cur = state.workflowRuns[run_id] ?? {
        run_id, name: '', phase: '', lastLog: '', status: 'running' as const,
      }
      if (session_key && !cur.sessionKey) cur.sessionKey = session_key
      switch (type) {
        case 'run_started':
          cur.name = workflowText(d.name) || cur.name || run_id
          cur.status = 'running'
          break
        case 'phase_started':
          cur.phase = workflowText(d.title) || cur.phase
          break
        case 'log': {
          const msg = workflowText(d.message)
          if (msg) cur.lastLog = msg
          break
        }
        case 'run_finished':
          cur.status = 'finished'
          break
        case 'run_failed':
          cur.status = 'failed'
          cur.error = workflowText(d.error) || cur.error
          break
        case 'run_cancelled':
          cur.status = 'cancelled'
          break
        default:
          break
      }
      state.workflowRuns[safeKey(run_id)] = cur
    },
    clearWorkflowRun(state, action: PayloadAction<string>) {
      delete state.workflowRuns[action.payload]
    },
    /** Fold the AUTHORITATIVE run list (`GET /api/workflows/runs`) into
     *  `workflowRuns`, correcting rows the live event stream could not.
     *
     *  `workflow_run_event` frames are one-shot and never replayed, so a client
     *  that was closed, asleep, or disconnected when a run ended holds an entry
     *  frozen at `running` forever: the spinner keeps spinning, the phase and log
     *  lines keep rendering as live, and the terminal-linger cleanup — which only
     *  tracks entries that have reached a terminal status — never arms to drop it.
     *  A gateway restart is the same case from the other side: the registry marks
     *  a run that was still running as failed (interrupted), and only this read
     *  carries that to a tab that stayed open across the restart.
     *
     *  The merge is deliberately MONOTONIC, because the snapshot is a point-in-time
     *  read that races the live stream (frames can land while the request is in
     *  flight) and a workflow status only ever moves one way, running → terminal:
     *   - a local entry already TERMINAL is never touched — the snapshot cannot be
     *     newer than the frame that ended it, so "re-opening" it could only undo
     *     truth the client already has;
     *   - a running local entry is only ever advanced to terminal, never rewound;
     *   - progress fields (`phase`, `lastLog`) are filled only when EMPTY, since a
     *     live frame's value is newer than any value this response carries;
     *   - a row absent locally is SEEDED only while it is still running — that is
     *     the reload / late-join case (nothing else seeds this slice, so a run
     *     started before the tab opened is otherwise invisible). A terminal row is
     *     never resurrected: the run is over and re-adding it would show a wall of
     *     ✓ rows above the composer on every reconnect.
     *   - an unrecognised status is not evidence and is skipped entirely, so a
     *     future backend state cannot silently clear a spinner or seed a row.
     *
     *  A failed request must NOT reach here at all: an absent list means the
     *  authority could not be read, not that no runs exist. Callers pass only a
     *  real `runs` array (see `syncWorkflowRuns` in useWebSocket).
     *
     *  Absence from a SUCCESSFUL response is likewise not evidence: the registry
     *  evicts old runs (200 by default), so a long-lived entry can legitimately
     *  drop out of the list. Such an entry is left alone rather than guessed at.
     */
    reconcileWorkflowRuns(state, action: PayloadAction<WorkflowRunSummary[]>) {
      for (const row of action.payload ?? []) {
        const runId = row?.run_id
        if (typeof runId !== 'string' || !runId || isUnsafeKey(runId)) continue
        const status = row.status
        const terminal = isTerminalWorkflowStatus(status)
        if (!terminal && status !== 'running') continue  // unknown status: no evidence
        const key = safeKey(runId)
        const cur = state.workflowRuns[key]
        if (!cur) {
          if (terminal) continue  // over and gone — never resurrect
          state.workflowRuns[key] = {
            run_id: runId,
            name: workflowText(row.name) || runId,
            phase: workflowText(row.phase),
            lastLog: workflowText(row.last_log),
            status: 'running',
            sessionKey: workflowText(row.session_key) || undefined,
          }
          continue
        }
        if (cur.status !== 'running') continue  // terminal locally: one-way, done
        if (!cur.name) cur.name = workflowText(row.name) || cur.name
        if (!cur.sessionKey && workflowText(row.session_key)) cur.sessionKey = workflowText(row.session_key)
        if (!terminal) {
          // Still running per the authority — the live stream owns progress, so
          // only fill what this client never received.
          if (!cur.phase && workflowText(row.phase)) cur.phase = workflowText(row.phase)
          if (!cur.lastLog && workflowText(row.last_log)) cur.lastLog = workflowText(row.last_log)
          continue
        }
        cur.status = status as WorkflowRunProgress['status']
        if (workflowText(row.error)) cur.error = workflowText(row.error)
      }
    },
    sseChatMessageUpdate(state, action: PayloadAction<{ slot: string; tool_call_id?: string; ts?: string; content?: string; meta?: Record<string, unknown> }>) {
      const { slot, tool_call_id: tcid, ts, content, meta } = action.payload
      if (!slot) return

      if (tcid) {
        const updateByTcid = (msgs: ChatMessage[]) => {
          for (let i = msgs.length - 1; i >= 0; i--) {
            const m = msgs[i]
            const mMeta = m.meta as Record<string, unknown> | undefined
            if (m.role === 'tool' && mMeta?.tool_call_id === tcid) {
              if (content !== undefined) m.content = content
              if (meta) m.meta = { ...(mMeta || {}), ...meta }
              break
            }
          }
        }
        if (slot === state.activeSlot) updateByTcid(state.messages)
        const cached = state.slotMessages[slot]
        if (cached) updateByTcid(cached)
      } else if (ts) {
        const apply = (msgs: ChatMessage[]) => {
          const idx = msgs.findIndex(m => m.ts === ts)
          if (idx < 0) return
          const target = msgs[idx]
          if (meta) target.meta = { ...(target.meta || {}), ...meta }
          if (content !== undefined) target.content = content
        }
        if (slot === state.activeSlot) apply(state.messages)
        const cached = state.slotMessages[slot]
        if (cached) apply(cached)
      }
    },
    sseToolActivity(state, action: PayloadAction<{ slot: string; tool: string; kind: string; purpose: string; input_preview: string; auto?: boolean; tool_call_id?: string; is_update?: boolean; is_shell?: boolean }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).toolLog
        : state.toolLog
      // claude-agent-acp emits an initial tool_call with empty rawInput followed
      // by tool_call_update notifications carrying the populated payload. The
      // backend sets is_update:true on the second-phase event so we merge into
      // the existing entry by tool_call_id. We gate strictly on is_update to
      // avoid silently merging a replayed initial event (e.g. WebSocket
      // reconnect) into an unrelated tool with a colliding id.
      const tcid = action.payload.tool_call_id
      if (tcid && action.payload.is_update) {
        const existing = log.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
        if (existing) {
          if (action.payload.tool) existing.text = action.payload.tool
          if (action.payload.purpose) existing.purpose = action.payload.purpose
          if (action.payload.input_preview) existing.input = action.payload.input_preview
          if (action.payload.kind) existing.kind = action.payload.kind
          if (action.payload.is_shell !== undefined) existing.is_shell = action.payload.is_shell
          // Update ts for recency sorting but NEVER overwrite executionStartedAt
          // — the elapsed timer must reflect real wall time since the tool began.
          existing.ts = Date.now()
          return
        }
      }
      log.push({ type: 'tool', text: action.payload.tool, purpose: action.payload.purpose, input: action.payload.input_preview, kind: action.payload.kind, ts: Date.now(), auto: action.payload.auto, tool_call_id: action.payload.tool_call_id, is_shell: action.payload.is_shell })
      if (log.length > 100) log.splice(0, log.length - 100)
    },
    sseActivityEvent(state, action: PayloadAction<{ slot: string; kind: string; text: string; approval_id?: string; approval_type?: string }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).toolLog
        : state.toolLog
      if (action.payload.kind === 'approval_resolved') {
        const id = action.payload.approval_id
        const entry = log.find(e => e.type === 'approval' && e.approval_id === id)
        if (entry) entry.type = 'approval_resolved'
        // Resolve against the OWNING slot's message array — active slot uses
        // state.messages, a background slot its slotMessages entry. Reading only
        // state.messages would miss a background-slot approval, so its tool
        // timer would never get the post-approval anchor and would inflate by
        // the whole approval wait after switching back to that slot.
        const msgs = action.payload.slot !== state.activeSlot
          ? (state.slotMessages[safeKey(action.payload.slot)] ?? [])
          : state.messages
        const msg = msgs.findLast(m => m.role === 'permission' && (m.meta as Record<string,unknown>)?.approval_id === id)
        if (msg && !(msg.meta as Record<string,unknown>).resolved) (msg.meta as Record<string,unknown>).resolved = 'approved'
        // Stamp execution_started_at on the EXACT tool entry linked to this
        // approval via the permission message's tool_call_id. This persists in
        // Redux and survives component remounts, preventing the elapsed timer
        // from inflating by the approval wait time.
        const tcid = (msg?.meta as Record<string, unknown>)?.tool_call_id as string | undefined
        if (tcid) {
          const toolEntry = log.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
          if (toolEntry && !toolEntry.execution_started_at) toolEntry.execution_started_at = Date.now()
        }
        return
      }
      const entry: ToolActivity = { type: action.payload.kind, text: action.payload.text, ts: Date.now() }
      if (action.payload.approval_id) entry.approval_id = action.payload.approval_id
      if (action.payload.approval_type) entry.approval_type = action.payload.approval_type
      log.push(entry)
    },
    sseToolResult(state, action: PayloadAction<{ slot: string; output: string; tool_call_id?: string }>) {
      const tid = action.payload.tool_call_id
      // Land the output on the tool MESSAGE's meta as well as the tool log, for
      // the one consumer that reads scrollback rather than the tool log: the
      // inline SubagentRunCard detects a spawn_run launch by parsing
      // "Spawned N subagent(s)." out of `meta.output`. Without this the card
      // sees nothing until the slot is refetched, since `chatSlotDetail` would
      // be the only source carrying this field — a reload-only artifact. Mirrors
      // the server, which writes the same redacted string to the same field
      // (chat_runner.py EVENT_TOOL_RESULT), so live and reloaded state agree.
      //
      // Restricted to launch results on purpose. `toolLog` is capped at 100
      // entries but `state.messages` is not, and a single output can reach the
      // server's 1 MB cap, so copying EVERY tool result here would let one long
      // autonomous turn grow the heap without bound.
      //
      // Runs BEFORE the tool-log lookup below, which returns early for a slot
      // that has no toolLog yet — a background slot's scrollback still needs
      // the output.
      //
      // Only with an explicit tool_call_id: the id-less fallback below is safe
      // for the tool log (positional, single-writer) but would attach output
      // to an arbitrary tool bubble in scrollback. The server applies the same
      // condition (`if _tcid:`), so skipping is parity, not a gap.
      if (tid && action.payload.output.includes(SPAWN_LAUNCH_MARKER)) {
        applyToolOutputToMessages(state, action.payload.slot, tid, action.payload.output)
      }
      const log = action.payload.slot !== state.activeSlot
        ? state.slotActivity[action.payload.slot]?.toolLog
        : state.toolLog
      if (!log) return
      // Prefer an exact tool_call_id match when a tid is supplied. Only if no
      // entry carries that id do we fall back to the most-recent id-less tool
      // entry. A single-pass `... || !log[i].tool_call_id` clause would let a
      // supplied tid latch onto an unrelated id-less tool sitting later in the
      // log, attaching the output to the wrong tool bubble.
      let target = -1
      if (tid) {
        for (let i = log.length - 1; i >= 0; i--) {
          if (log[i].type === 'tool' && log[i].tool_call_id === tid) { target = i; break }
        }
      }
      if (target === -1) {
        for (let i = log.length - 1; i >= 0; i--) {
          if (log[i].type === 'tool' && (!tid || !log[i].tool_call_id)) { target = i; break }
        }
      }
      if (target >= 0) log[target].output = action.payload.output
    },
    /** Store an MCP App (SEP-1865) render payload, keyed by BOTH its session
     *  and tool_call_id (see mcpAppKey): the session scope means an ACP
     *  tool-call-id reuse across slots can never cross-render another
     *  session's app (or its live callback capability), and per-slot eviction
     *  (payloads are multi-MB) is a simple prefix scan. */
    sseMcpAppRender(state, action: PayloadAction<McpAppRenderPayload>) {
      const p = action.payload
      if (!p?.tool_call_id || isUnsafeKey(p.tool_call_id)) return
      if (!p.session_key || isUnsafeKey(p.session_key)) return
      state.mcpApps[mcpAppKey(p.session_key, p.tool_call_id)] = p
      // Bound per-slot retention: payloads carry multi-MB app HTML, so a
      // long-lived slot that renders many apps must not grow unbounded. Keys
      // enumerate in insertion order, so the oldest slot entries are dropped
      // first once the cap is exceeded.
      const prefix = `${p.session_key}\u001F`
      const slotKeys = Object.keys(state.mcpApps).filter((k) => k.startsWith(prefix))
      for (let i = 0; i < slotKeys.length - MCP_APPS_PER_SLOT_MAX; i++) {
        delete state.mcpApps[slotKeys[i]]
      }
    },
    /** Handle chat messages pushed via global SSE/WS (works after refresh). */
    /** Accumulate streamed model reasoning (`chat_thinking` WS event) into a
     *  content-bearing `thinking`-role message — ONE BLOCK PER REASONING BURST.
     *  A turn that reasons, calls a tool, then reasons again therefore renders
     *  two blocks, each above the step it explains. Scanning back to the turn
     *  boundary instead appends every later burst into the FIRST burst's block,
     *  so a multi-tool turn collapsed all of its reasoning under the opening one.
     *
     *  Placement is anchored on the turn's open `streaming` row, located
     *  directly rather than inferred from the array tail: a turn's visible text
     *  accumulates into ONE row that stays open across tool calls (the backend
     *  flushes each segment without broadcasting), and reasoning belongs ABOVE
     *  it, exactly as the `tool` branch inserts ahead of it. The tail is NOT the
     *  end of the turn — an approval row, a queued bubble, a stop event, an
     *  error card and a `file` card are all appended BELOW that open row, so
     *  measuring from `length` would drop the block beneath the answer it
     *  explains. With no open row (reasoning before any text) the block appends,
     *  which is also correct: it lands after whatever opened the turn.
     *
     *  A CONFIRMED steer is injected into the running turn, so reasoning after
     *  it continues the burst it interrupted; an unconfirmed (optimistic) steer
     *  is a raced real turn and does close the burst — see isTurnBoundaryUser.
     *
     *  A `tool` row BELOW that open text row means the rows are already out of
     *  emission order: the `tool` branch steps back over a trailing `streaming`
     *  row but not over an approval row, so an approval-gated call lands beneath
     *  the text. The tool is then the turn's latest step, so the burst that
     *  preceded it is closed and the new one belongs after it — appending is the
     *  only placement that satisfies both, and it is what stops a post-tool
     *  burst being concatenated into the pre-tool block. */
    sseThinkingChunk(state, action: PayloadAction<{ slot: string; content: string }>) {
      const { slot, content } = action.payload
      if (slot !== state.activeSlot || !content) return
      let at = state.messages.length
      for (let i = state.messages.length - 1; i >= 0; i--) {
        if (state.messages[i].role === 'streaming') { at = i; break }
      }
      for (let i = at; i < state.messages.length; i++) {
        if (state.messages[i].role === 'tool') { at = state.messages.length; break }
      }
      // Extend the burst the model is still emitting: an out-of-band row (an
      // approval, a queued bubble) and a confirmed steer both interrupt it
      // without ending it, so look through them for the open block.
      let prev = at
      while (prev > 0) {
        const m = state.messages[prev - 1]
        if (isOutOfBandRow(m) || (m.role === 'user' && !isTurnBoundaryUser(m))) { prev--; continue }
        break
      }
      const open = prev > 0 ? state.messages[prev - 1] : undefined
      if (open?.role === 'thinking') { open.content += content; return }
      state.messages.splice(at, 0, { role: 'thinking', content, cls: '', meta: { clientTs: mintMsgId() } })
    },
    sseChatMessage(state, action: PayloadAction<{ slot: string; role: string; content: string; ts?: string; seq?: number; cls?: string; meta?: Record<string, unknown>; kind?: string; batched?: boolean }>) {
      const { slot, role, content, ts, seq, cls, meta, kind, batched } = action.payload
      if (slot !== state.activeSlot) { applyNonActiveFrame(state, action.payload); return }
      // stop_event — replace in place by id, or insert new
      const effectiveKind = kind ?? (meta?.kind as string | undefined)
      if (effectiveKind === 'stop_event') {
        const id = (meta?.id as string) ?? ''
        const idx = id ? state.messages.findIndex(m => m.meta?.id === id) : -1
        const msg: ChatMessage = ensureMsgId({ role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' })
        if (idx >= 0) { state.messages[idx] = msg } else { state.messages.push(msg) }
        return
      }
      // WS segment — finalize streaming into assistant without resetting sequence or slot state
      if (role === '_segment') {
        finalizeTrailingStreaming(state.messages)
        return
      }
      // WS chunk — accumulate into streaming message, preserve rawText
      if (role === 'chunk') {
        state.slotState = 'streaming'
        state._wsChunkedDuringFetch = true
        // Drop only the empty "Thinking…" placeholder; keep content-bearing
        // reasoning blocks (from chat_thinking) so they persist as a collapsible
        // trace directly above the streamed answer.
        if (state.messages.some(m => m.role === 'thinking' && !m.content)) {
          state.messages = state.messages.filter(m => !(m.role === 'thinking' && !m.content))
        }
        // Accumulate reasoning text into activity timeline
        const last = state.toolLog[state.toolLog.length - 1]
        if (last?.type === 'reasoning') {
          last.text += content
        } else {
          state.toolLog.push({ type: 'reasoning', text: content, ts: Date.now() })
        }
        let streamIdx = -1
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') { streamIdx = i; break }
        }
        if (streamIdx >= 0) {
          const msg = state.messages[streamIdx]
          // Defensive non-batched gap detection. The live WS path always sets
          // `batched` — the useWebSocket flush buffer owns gap detection across
          // the chunks it merges and inlines the marker itself — so this branch
          // only runs for a direct (test/legacy) non-batched chunk dispatch. It
          // shares missedChunkMarker with the buffer so the two cannot drift.
          if (!batched && seq !== undefined && state.lastChunkSeq !== undefined) {
            msg.content += missedChunkMarker(state.lastChunkSeq, seq)
          }
          msg.content += content
          msg.rawText = msg.content
        } else {
          state.messages.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content, meta: { clientTs: mintMsgId() } })
        }
        if (seq !== undefined) state.lastChunkSeq = seq
        return
      }
      // WS done — finalize streaming into assistant, rawText preserved for reparse
      if (role === '_done') {
        state.slotState = 'idle'
        state.lastChunkSeq = undefined
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            const msg = state.messages[i]
            msg.role = 'assistant'
            msg.rawText = msg.content
            break
          }
        }
        state.slotRunning = false
        state.slotStopping = false
        state.slotState = 'idle'
        state.pendingTurnSlot = null
        return
      }
      // Compacting — block input, show footer indicator (no visible message)
      if (role === 'compacting') {
        if (action.payload.slot && action.payload.slot !== state.activeSlot) return
        state.slotState = 'compacting'
        state.slotRunning = true
        return
      }
      // Permission messages carry request_id/tool_input in cls (JSON) — lift into
      // meta here, BEFORE the guard, so the identity comparison sees the same
      // `tool_call_id` the stored row has.
      let effectiveMeta = meta
      if (role === 'permission' && !meta?.approval_id && cls) {
        try {
          const parsed = JSON.parse(cls)
          if (parsed.request_id) {
            effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
          }
        } catch { /* not JSON cls, ignore */ }
      }
      // If this permission's tool was already rejected/stopped, mark it resolved immediately
      if (role === 'permission') {
        const tcid = (effectiveMeta?.tool_call_id as string) || ''
        if (tcid) {
          const entry = state.toolLog.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
          if (entry?.rejected) effectiveMeta = { ...effectiveMeta, resolved: 'rejected' }
        }
      }
      // Idempotent append — ONE chokepoint that dominates every branch below,
      // which is the point: each of those branches creates or MUTATES a row and
      // returns, so a guard placed after any of them is a guard some frame slips
      // past. The `assistant` branch is the sharpest case: it overwrites the
      // trailing `streaming` row, so a late redelivery of an OLD assistant frame
      // would clobber the live content of a NEW segment already streaming.
      if (isRedeliveredMessage(state.messages, effectiveMeta)) { state._redeliveredFramesDropped += 1; return }
      // A turn-consuming frame makes a pending stateless question card stale —
      // placed after the redelivery guard so a replayed frame cannot clear a
      // live card (see dropStaleStatelessQuestion).
      dropStaleStatelessQuestion(state, slot, role)
      // Tool call — update state, insert before streaming message
      if (role === 'tool') {
        state.slotState = 'tool_running'
        // Insert tool before any trailing streaming message so
        // chat_segment can still find and finalize it with redacted text.
        let insertIdx = state.messages.length
        if (insertIdx > 0 && state.messages[insertIdx - 1]?.role === 'streaming') {
          insertIdx--
        }
        state.messages.splice(insertIdx, 0, ensureMsgId({ role, content, cls: cls || '', ts, meta }))
        return
      }
      // Thinking — deduplicate, only keep one
      if (role === 'thinking') {
        if (state.messages.some(m => m.role === 'thinking')) return
        state.messages.push({ role: 'thinking', content: '', cls: '', meta: { clientTs: mintMsgId() } })
        return
      }
      // Replace streaming placeholder with final assistant message
      if (role === 'assistant') {
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            state.messages[i].role = 'assistant'; state.messages[i].content = content; if (ts) state.messages[i].ts = ts
            // Carry the frame's meta — crucially `mid`, this row's server
            // identity. The row was minted client-side by the first `chunk` and
            // has none until now; without it a later redelivery of THIS frame is
            // unrecognisable and would overwrite whatever is streaming then.
            if (meta) state.messages[i].meta = { ...(state.messages[i].meta || {}), ...meta }
            return
          }
        }
      }
      // New user message = new turn — clear activity log
      if (role === 'user') {
        // A steered message does not start a new turn — skip the "stale permissions"
        // cleanup so the approval bar remains visible and answerable (#1667).
        if (!meta?.steer) {
          state.toolLog = []
          // Auto-resolve any stale permissions from previous turn so they don't block the new turn
          for (const m of state.messages) {
            if (m.role === 'permission' && !m.meta?.resolved) {
              if (m.meta) m.meta.resolved = 'rejected'
              else m.meta = { resolved: 'rejected' }
            }
          }
        }
        // Reconcile the optimistic user bubble rather than pushing a duplicate
        // when the server echoes the user frame (#2845). Uses shared helper that
        // scans past non-matching pipelined sends (#3898).
        const echoSendId = meta?.sendId as string | undefined
        if (echoSendId && meta?.mid) {
          if (reconcileOptimisticEcho(state.messages, echoSendId, meta as Record<string, unknown>, ts)) return
        } else if (meta?.mid) {
          // Fallback: no sendId on the echo — use tail content match for paths
          // that don't generate a sendId (split-pane, queued promotions).
          const last = state.messages[state.messages.length - 1]
          if (last?.role === 'user' && last.content === content && !last.meta?.mid) {
            if (ts) last.ts = ts
            if (meta) last.meta = { ...(last.meta || {}), ...meta }
            return
          }
        }
      }
      state.messages.push(ensureMsgId({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind }))
    },
    /** Patch an existing message, identified by `mid` when the server sends one and
     * by `ts` otherwise. Used by the `chat_message_update` server event to flip an
     * mcp_oauth banner from "needs auth" to "authenticated" after kiro-cli emits
     * server_initialized, and to retire a banner a newer request superseded.
     * Patches both the active messages array and the slotMessages cache so a slot
     * the user isn't currently viewing still shows the correct banner state on
     * switch-back.
     *
     * `ts` is NOT a row identity — two restored rows can carry the same one (see
     * `meta.mid`, which exists for exactly this reason) — so a ts-keyed lookup
     * resolves the first match and two patches for two colliding rows would both
     * land on one of them, leaving the other stale. `mid` is preferred where
     * present; `ts` stays as the fallback for legacy rows written before the id
     * existed and for callers that do not send one. */
    sseChatMessagePatchByTs(state, action: PayloadAction<{ slot: string; ts: string; mid?: string; meta?: Record<string, unknown>; content?: string }>) {
      const { slot, ts, mid, meta, content } = action.payload
      if (!slot || (!ts && !mid)) return
      const apply = (msgs: ChatMessage[]) => {
        const idx = mid
          ? msgs.findIndex(m => m.meta?.mid === mid)
          : msgs.findIndex(m => m.ts === ts)
        if (idx < 0) return
        const target = msgs[idx]
        if (meta) target.meta = { ...(target.meta || {}), ...meta }
        if (content !== undefined) target.content = content
      }
      if (slot === state.activeSlot) apply(state.messages)
      const cached = state.slotMessages[slot]
      if (cached) apply(cached)
    },
    /** Remove the first queued message matching content and append a user bubble at the end. */
    removeQueuedMessage(state, action: PayloadAction<{ slot: string; content: string; queue_id?: string }>) {
      const { slot, content, queue_id } = action.payload
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const idx = queue_id
        ? msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
        : msgs.findIndex(m => m.role === 'queued' && m.content === content)
      if (idx >= 0) {
        const ts = msgs[idx].ts
        msgs.splice(idx, 1)
        msgs.push({ role: 'user', content, cls: 'msg msg-u', ts })
        // Deliberately NO card retirement here. Three review rounds each found
        // a different way this path could retire the wrong card (system queue
        // items hydrated as indistinguishable rows; duplicate rows from the
        // hydration/queue_push race; and a queued answer for card A landing
        // after a newer card B arrived — per-delivery cardId comparison would
        // be required, but the queued row cannot carry a trustworthy capture
        // across reloads). The cost of NOT retiring is bounded and local: the
        // answering device keeps the card until the popped turn's next
        // turn-consuming frame retires it via the frame applier — the core
        // fix — exactly like every other device. Sender-side instant
        // retirement for queued answers is deferred to the server-side
        // lifecycle owner (#2290), which can compare identities authoritatively.
      }
    },
    /** Cancel a queued message: remove from messages. pendingInput is set locally by the initiating client. */
    cancelQueuedMessage(state, action: PayloadAction<{ slot: string; queue_id: string }>) {
      const { slot, queue_id } = action.payload
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
      if (idx >= 0) msgs.splice(idx, 1)
    },
    /** Edit a queued message in place (from backend queue_edit WS event or optimistic local update). */
    editQueuedMessage(state, action: PayloadAction<{ slot: string; queue_id: string; content: string }>) {
      const { slot, queue_id, content } = action.payload
      if (isUnsafeKey(slot)) return
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
      if (idx >= 0) msgs[idx].content = content
    },
    /** Reorder queued messages to match the given queue-id sequence (from the
     *  backend queue_reorder WS event or an optimistic local update). Queued
     *  messages are re-slotted in place - the positions they occupy in the
     *  message list stay fixed, only which queued message sits at each
     *  position changes. Ids missing from `order` keep their relative order
     *  after the ordered ones (mirrors the backend's semantics). */
    reorderQueuedMessages(state, action: PayloadAction<{ slot: string; order: string[] }>) {
      const { slot, order } = action.payload
      if (isUnsafeKey(slot)) return
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const queuedIdx: number[] = []
      msgs.forEach((m, i) => { if (m.role === 'queued' && (m.meta?.queueId as string)) queuedIdx.push(i) })
      if (queuedIdx.length < 2) return
      const byId = new Map(queuedIdx.map(i => [msgs[i].meta?.queueId as string, msgs[i]]))
      const ordered = order.filter(id => byId.has(id)).map(id => byId.get(id)!)
      const orderedSet = new Set(order)
      const remaining = queuedIdx.map(i => msgs[i]).filter(m => !orderedSet.has(m.meta?.queueId as string))
      const next = [...ordered, ...remaining]
      queuedIdx.forEach((msgIdx, k) => { msgs[msgIdx] = next[k] })
    },
    /** Add a queued message (from backend queue_push WS event). */
    appendQueuedMessage: {
      reducer(state, action: PayloadAction<{ slot: string; content: string; ts: string; queueId: string }>) {
        const { slot, content, ts, queueId } = action.payload
        const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[safeKey(slot)] ??= [])
        // A row with this queueId may ALREADY exist: slot-detail hydration
        // can land before a delayed `queue_push` for the same entry. Appending
        // blindly would duplicate the row; keep the existing one.
        if (msgs.some(m => m.role === 'queued' && (m.meta?.queueId as string) === queueId)) return
        msgs.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId } })
      },
      prepare(payload: { slot: string; content: string; ts: string; queue_id?: string }) {
        return { payload: { ...payload, queueId: payload.queue_id || crypto.randomUUID() } }
      },
    },
  },
  extraReducers: (builder) => {
    builder
      /** A reconnect starts a new snapshot cycle: the gateway can restart before
       *  session restore and emit an empty slots frame, so the bit must go back
       *  to unseen or that frame reads as an authoritative empty list and tears
       *  down every background slot. Mirrors `dashboardSlice`, where
       *  `sseConnected` clears `slotsLoaded` for the same reason — reading the
       *  bit without resetting it is what made this a defect. */
      .addCase(sseConnected, (state) => {
        state.slotsSnapshotSeen = false
      })
      /** Reconcile per-slot caches against the authoritative slots list.
       *  Sessions that close/archive/delete vanish from the SSE `slots` REPLACE;
       *  without this reconcile their transcripts stay resident for the tab's
       *  lifetime — the dominant retention class behind multi-GB heaps on
       *  long-lived dashboard tabs.
       *  Guards: an empty payload is a no-op only until the first real snapshot
       *  has been seen, because a reconnect can deliver one before it. Once seen,
       *  an empty list is authoritative — the last slot was deleted, possibly by
       *  another client — and skipping teardown there would strand this slice's
       *  transcripts and MCP payloads, the expensive half. This slice tracks the
       *  bit itself rather than reading the dashboard's, which its reducer cannot
       *  see. The active slot is never pruned (its live `messages`/optimistic
       *  state must not be dropped out from under the open pane). */
      .addCase(sseSlots, (state, action) => {
        const seenSnapshot = state.slotsSnapshotSeen === true
        if (action.payload.length > 0) state.slotsSnapshotSeen = true
        // An empty frame before the first real snapshot is a reconnect artifact.
        // The authoritative empty case is not lost by skipping it: every
        // reconnect dispatches `fetchSlots` right after `sseConnected`
        // (`hooks/useWebSocket.ts`), and the case below reconciles that reply
        // even when it is empty.
        if (action.payload.length === 0 && !seenSnapshot) return
        reconcileSlotResidue(state, action.payload)
      })
      /** The other authoritative slot-list writer. A request's reply is
       *  authoritative even when empty — nothing to disambiguate — so this is
       *  where "every slot was deleted while disconnected" is torn down. But a
       *  reply in flight can be OLDER than the live frames that arrived while it
       *  travelled, so it may omit a slot the stream has since created: evict
       *  from here only while no live frame has been seen. Before that there is
       *  no fresher state to destroy; after it the live frame owns teardown. */
      .addCase(fetchSlots.fulfilled, (state, action) => {
        if (state.slotsSnapshotSeen === true) return
        reconcileSlotResidue(state, action.payload)
      })
      .addCase(fetchHistory.fulfilled, (state, action) => {
        const { sessions, hasMore, offset, append } = action.payload
        state.history = append ? [...state.history, ...sessions] : sessions
        state.historyHasMore = hasMore
        state.historyOffset = offset + sessions.length
      })
      .addCase(switchSlot.pending, (state, action) => {
        const target = switchSlotKey(action.meta.arg)
        // Must precede the reassignment below: true while the active slot's own
        // switch is in flight, i.e. while `slotHasMore` is still the old chat's.
        const viewIsProvisional = state.slotSwitchRequestId !== null && state.slotSwitchTarget === state.activeSlot
        // Remember the outgoing selection BEFORE the cursor is voided below, so
        // `rejected` can restore it when the target turns out to be gone (#6309).
        // A PROVISIONAL view (its own switch never settled) is not a selection
        // worth restoring -- falling back to a half-loaded slot re-creates the
        // empty-pane failure -- so the previous settled origin is kept instead:
        // a rapid A→B→C chain whose C 404s falls back to A. The cursor is
        // captured only when it still describes the outgoing slot; otherwise
        // null keeps the restore honest about never having had one.
        if (!viewIsProvisional) {
          state.slotSwitchOrigin = state.activeSlot === null ? null : {
            key: state.activeSlot,
            cursor: state.slotCursorKey === state.activeSlot
              ? { hasMore: state.slotHasMore, nextBefore: state.slotOldestIndex, olderError: state.slotOlderError }
              : null,
            run: { state: state.slotState, running: state.slotRunning, stopping: state.slotStopping },
          }
        }
        // This fetch replaces the cursor, so it is stale from here until it lands
        // -- including a same-key switch, where the key alone still looks valid.
        state.slotCursorKey = null
        state.slotSwitchRequestId = action.meta?.requestId ?? null
        state.slotSwitchTarget = target
        // Save current slot's activity
        if (state.activeSlot) {
          state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
        }
        // Cache current slot's messages before switching
        if (state.activeSlot && state.messages.length > 0) {
          // Once its switch has landed the view is the whole transcript, so its own
          // has_more is the marker; before that, preserve what the pane already had.
          const k = safeKey(state.activeSlot)
          writeSlotPage(state, state.activeSlot, state.messages,
            viewIsProvisional ? undefined : state.slotHasMore,
            viewIsProvisional ? state.slotPaneBounded?.[k] : undefined)
        }
        // Always strip target from history: activeSlot ∉ slotHistory
        state.slotHistory = state.slotHistory.filter(k => k !== target)
        // A PROVISIONAL outgoing view is pushed too: the MRU records where the
        // user aimed, not what finished loading (pinned by the navigation-stack
        // suite), and an MRU jump dispatches a fresh switchSlot that loads the
        // slot regardless. Only a GONE key must stay off the stack, which the
        // rejected-restore below owns.
        if (state.activeSlot && state.activeSlot !== target) {
          state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        }
        // Restore target slot's activity (or empty)
        loadSlotActivity(state, target)
        // Set activeSlot immediately so WS events for the new slot are accepted.
        // Restore cached messages if available (instant switch), otherwise show loading.
        state.activeSlot = target
        // The older-history error belongs to the outgoing chat and ownership moves
        // here, so it must clear now rather than when the fetch settles.
        state.slotOlderError = false
        const cachedMsgs = state.slotMessages[target]
        if (cachedMsgs) {
          state.messages = cachedMsgs
          state.slotLoading = false
        } else {
          state.messages = []
          state.slotLoading = true
        }
        state._wsChunkedDuringFetch = false
      })
      .addCase(switchSlot.fulfilled, (state, action) => {
        // Before the guards below, so an early return still ends this claim. Keyed
        // on requestId, which a hand-rolled dispatch may omit, so read it safely.
        if (state.slotSwitchRequestId !== null && state.slotSwitchRequestId === action.meta?.requestId) { state.slotSwitchRequestId = null; state.slotSwitchTarget = null; state.slotSwitchOrigin = null }
        const { key, messages, running, hasMore, queue, nextBefore } = action.payload
        if (isUnsafeKey(key)) return
        if (state.activeSlot !== key) return  // user switched away during fetch
        // A payload carrying `comparableTotal` came from the coverage retry: its
        // own `total` is the raw unbounded count, the carried one is the settled
        // bounded count, and only the latter may become the baseline.
        const comparable = (action.payload as { comparableTotal?: number }).comparableTotal
        retainServerTotal(state, key, comparable ?? action.payload.total, running,
          undefined, comparable !== undefined || action.payload.boundedRead)
        state.slotState = running ? 'streaming' : 'idle'
        // Mark stale permissions as resolved so ApprovalBar ignores them
        if (!running) {
          for (const m of messages) {
            if (m.role === 'permission' && !m.meta?.resolved) m.meta = { ...m.meta, resolved: 'stale' }
          }
        }
        // If WS already delivered newer streaming content, append it to fetched messages
        const lastLocal = state.messages[state.messages.length - 1]
        const preserved = mergePreservedPastes(state.messages, messages)
        // Does the fetched history already contain the local trailing reply?
        // The server row id answers it exactly, so when the local reply HAS one
        // that is the only test — falling back to content as well would let a
        // stale snapshot row with identical text (a different row, different id)
        // match and drop the newest reply. Content equality is only for a reply
        // that has no id yet: streamed in this session and never reloaded, so the
        // server history cannot hold it under a different id anyway.
        //
        // Preferring the id also survives the redaction asymmetry: this endpoint
        // redacts on emit (chat_utils._prepare_messages) while the streamed copy
        // is raw, so one row legitimately arrives with different bytes.
        const localMid = lastLocal?.meta?.mid
        const serverHasLastLocal = !!lastLocal && (
          typeof localMid === 'string' && !!localMid
            ? preserved.some(m => m.role === 'assistant' && m.meta?.mid === localMid)
            : preserved.some(m => m.role === 'assistant' && m.content === lastLocal.content)
        )
        // Hold the pre-fetch array so the assignment below can be skipped when
        // the fetched history turns out to be redundant (see sameTranscript).
        const existing = state.messages
        let next: ChatMessage[]
        if (
          state._wsChunkedDuringFetch
          && lastLocal?.role === 'streaming'
          && lastLocal.content.length > 0
        ) {
          // WS chunks arrived during fetch — use fetched history + local streaming
          next = [...preserved.filter(m => m.role !== 'streaming'), lastLocal]
        } else if (
          lastLocal
          && (lastLocal.role === 'assistant' || lastLocal.role === 'streaming')
          && !!lastLocal.content && lastLocal.content.length > 0
          && !serverHasLastLocal
        ) {
          // The HTTP fetch resolved with a history that predates the reply we
          // already finalized locally (via applyNonActiveFrame while this slot
          // was backgrounded). Blindly replacing with the server response here
          // is the "switch away and back drops the latest response" regression.
          // Keep the server history but re-attach the local trailing reply.
          // Guarded by serverHasLastLocal above (row id, else exact content) so
          // we never duplicate a reply the server already returned, and never
          // drop a genuinely newer one: a different row has a different id, and
          // the content fallback stays EXACT rather than fuzzy.
          //
          // Only finalize a still-'streaming' partial to 'assistant' when the
          // turn is NOT still running. If the slot is still streaming
          // (running=true — e.g. switching back to a background slot whose
          // reply is mid-flight), coercing to 'assistant' freezes the partial:
          // the resuming `chunk` handler finds no trailing 'streaming' message
          // and pushes a NEW one, splitting the single reply across two bubbles
          // until chat_done heals it. Keep it 'streaming' so the stream resumes
          // into the same bubble.
          const finalized: ChatMessage = (lastLocal.role === 'streaming' && !running)
            ? { ...lastLocal, role: 'assistant' }
            : lastLocal
          next = [...preserved.filter(m => m.role !== 'streaming'), finalized]
        } else {
          next = preserved
        }
        /* switchSlot fetches a BOUNDED page (OLDER_PAGE_LIMIT), and `pending`
         * restored this slot's cached transcript into `state.messages`, so
         * assigning the page wholesale collapsed a window the reader had paged in
         * to the newest page -- recoverable only by re-paging. Keep any prior head
         * that sits above the page's first row, through the one shared cut
         * `warmSlotCache` uses, so the two cannot diverge again.
         *
         * `thinking` is held out of the cut (no identity, broadcast-only) and
         * re-placed by `mergePreservedThinking` below. Stale queued rows kept in
         * the head are collapsed by the `hydrateQueuedBubbles` call below, which
         * strips every queued row before re-adding the authoritative server set.
         */
        const priorServerRows = existing.filter(m => m.role !== 'thinking')
        const { olderHead } = olderHeadAbovePage(priorServerRows, preserved)
        if (olderHead.length) next = [...olderHead, ...next]
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        /* The cursor is a row OFFSET, not the array's first row, so keeping a head
         * above the page without shifting it made the next "load earlier" re-fetch
         * exactly the rows just kept. `loadOlderMessages` dedupes them, so the
         * cost is a DEAD CLICK rather than duplicate rows -- still a defect, and
         * the same dead-click shape this affordance is meant to avoid.
         *
         * The shift itself has two boundaries a clamp would conflate, one of which
         * makes that dead click PERMANENT; `pagingCursorAfterKeptHead` owns both.
         */
        const keptCursor = pagingCursorAfterKeptHead(
          hasMore, nextBefore, serverRowCount(olderHead))
        setPagingCursor(state, keptCursor.hasMore, keptCursor.nextBefore)
        // Hydrate queued messages from the backend queue field through the
        // single shared path (hydrateQueuedBubbles) so this reducer cannot drift
        // from warmSlotCache/refreshSlot. It strips any WS-delivered queued
        // bubbles first (a queue_push may have arrived during the fetch) so the
        // server queue set stays canonical and non-duplicated.
        // Thinking blocks are client-only (never persisted server-side); re-insert
        // them so a switchSlot refresh does not discard the collapsible reasoning
        // trace. Without this, switching tabs and back drops all thinking blocks.
        // Coverage from the PURE fetched page (`messages`): `next` carries the
        // re-attached finalized `lastLocal` reply, which must not vouch for
        // history the snapshot never covered.
        /* Both helpers take `windowComplete` about the LOADED window, not the fetch:
         * `mergePreservedThinking` parks a text-anchored block "until its anchor pages
         * in" (:1545) and `reinsertThinkingOrphans` needs a complete window to trust a
         * text anchor (:1560). `next` carries the retained head, so the loaded window is
         * wider than the page -- and once the head saturates the cursor NOTHING can page
         * in, so raw `hasMore` would park the reasoning permanently.
         */
        const windowComplete = !keptCursor.hasMore
        const orphaned: Array<{ msg: ChatMessage; anchor: ThinkingAnchor }> = []
        next = mergePreservedThinking(existing, next, messages, windowComplete, orphaned)
        // A reopen may load the anchor of a block parked by an earlier bounded reopen.
        // `??= {}` because a rehydrated state from a build without this field has none.
        const parked = (state.thinkingOrphans ??= {})
        const reseated = reinsertThinkingOrphans(next, parked[safeKey(key)] ?? [], windowComplete)
        next = reseated.list
        parked[safeKey(key)] = [...reseated.remaining, ...orphaned]
        next = hydrateQueuedBubbles(next, queue)
        next = deduplicateByMid(next)
        // Switching back to an already-loaded slot re-fetches a history that is
        // usually identical; skipping the write keeps every existing reference.
        if (!sameTranscript(existing, next)) state.messages = next
        // Update cache and clear loading state. This is the active view, so the
        // marker is slotHasMore -- writing the array alone left a stale flag.
        writeSlotPage(state, key, state.messages, hasMore)
        state.slotLoading = false
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(switchSlot.rejected, (state, action) => {
        // Only the CURRENT claim may unwind: a stale rejection (a newer switch
        // already took the requestId) must not fight the switch in flight.
        const target = switchSlotKey(action.meta.arg)
        const claimed = state.slotSwitchRequestId !== null && state.slotSwitchRequestId === action.meta?.requestId
        const origin = claimed ? state.slotSwitchOrigin : null
        if (claimed) { state.slotSwitchRequestId = null; state.slotSwitchTarget = null; state.slotSwitchOrigin = null }
        if (state.activeSlot !== target) return
        // A caller that just CREATED the target may opt out of the unwind: its
        // 404 is a create/fetch race on a slot that exists, and bouncing away
        // would hide the composer state seeded there (see SwitchSlotArg).
        const keepTarget = typeof action.meta.arg !== 'string' && action.meta.arg.keepTargetOnMissing === true
        // A 404 means the target is GONE (isMissingSlotError is authoritative on
        // a numeric status, #6199): keeping it selected would leave the store on
        // a slot that cannot exist, and the global shortcuts aiming at it. Put
        // the selection back where it was (#6309). Any other failure is treated
        // as transient below: the target is real, so keeping it selected with an
        // empty pane lets a retry succeed.
        if (!keepTarget && origin && origin.key !== target && isMissingSlotError(action.payload ?? action.error)) {
          state.activeSlot = origin.key
          // Re-hydrate the cached page when one exists, [] otherwise. The cache
          // can be older than the pane was (a cleared or transiently-failed pane
          // caches nothing but does not evict a prior entry) -- the older page
          // is still the closest honest answer, and the next refresh heals it.
          state.messages = state.slotMessages[safeKey(origin.key)] ?? []
          state.slotLoading = false
          // `pending` pushed the origin onto the MRU; take it back out so the
          // `activeSlot ∉ slotHistory` invariant holds again. Net effect of the
          // whole failed switch on the MRU: nothing, except the gone target
          // stays stripped -- restoring a deleted key onto the stack is the
          // regression #6260 shipped and this reducer exists to avoid.
          state.slotHistory = state.slotHistory.filter(k => k !== origin.key)
          // Swap the origin's cached activity back in (pending loaded the target's).
          loadSlotActivity(state, origin.key)
          // Run mirror: the snapshot applies verbatim. It was captured at
          // pending and kept CURRENT by `syncOriginRun` at every non-active
          // run write, so a transition mid-flight is already in it -- and a
          // same-value round trip (queued turn completing: idle over idle)
          // downgraded `running` at event time, which no after-the-fact
          // comparison of `slotRun` could have detected.
          state.slotState = origin.run.state
          state.slotRunning = origin.run.running
          state.slotStopping = origin.run.stopping
          // The local-turn guard: a send the origin made before leaving was
          // awaiting server confirmation. If that turn ENDED while the origin
          // was non-active (the event-synced snapshot says not running), the
          // guard must fall with it -- the active-path _done that normally
          // clears it never ran because the view was elsewhere, and left
          // standing it hides Continue and makes syncSlotRunningFromServer
          // ignore idle snapshots for this slot indefinitely. A still-running
          // (or still-unconfirmed) turn keeps its guard.
          if (state.pendingTurnSlot === origin.key && !origin.run.running) state.pendingTurnSlot = null
          // Re-key the paging cursor when the captured one described the origin;
          // no valid cursor existed otherwise, and guessing pages the wrong chat.
          if (origin.cursor) {
            setPagingCursor(state, origin.cursor.hasMore, origin.cursor.nextBefore)
            // setPagingCursor clears the flag for a fresh fetch; this is a
            // RESTORE, so the origin's real retry-bar state comes back instead.
            state.slotOlderError = origin.cursor.olderError
          }
          return
        }
        state.messages = []
        state.slotRunning = false
        state.slotStopping = false
        setPagingCursor(state, false, 0)
        state.slotLoading = false
      })
      .addCase(refreshSlot.fulfilled, (state, action) => {
        if (!action.payload) return
        const { key, messages, running, hasMore, queue, nextBefore } = action.payload
        if (isUnsafeKey(key)) return
        if (state.activeSlot !== key) return  // user switched away
        retainServerTotal(state, key, action.payload.total, running, undefined, action.payload.boundedRead)
        // Merge permission messages: prefer state perms (have frontend resolved flags)
        // but include API perms for any we don't have locally (e.g. arrived while disconnected)
        const statePerms = new Map<string, typeof state.messages[0]>()
        for (const m of state.messages) {
          if (m.role === 'permission' && m.meta?.approval_id) statePerms.set(m.meta.approval_id as string, m)
        }
        const apiPerms = messages.filter(m => m.role === 'permission')
        for (const m of apiPerms) {
          const aid = m.meta?.approval_id as string | undefined
          if (aid && !statePerms.has(aid)) statePerms.set(aid, m)
        }
        // Sort key from a transcript ts via the ONE shared parser (#6004).
        // `?? 0` keeps unreadable/absent ts sorting first, as before. The
        // comparator only needs a monotonic key, so the parser's native epoch
        // ms works directly (the old local copy returned epoch seconds —
        // scaling every readable key by 1000 preserves the order for every
        // reachable timestamp).
        const tsNum = (v: unknown): number => {
          const s = v == null ? '' : String(v)
          return transcriptTsMs(s) ?? 0
        }
        /* This page is now COUNT-MATCHED (see refreshSlot), not the whole
         * transcript, so the window it returns can SLIDE: when the server gained
         * rows while this client was away -- which is precisely the reconnect this
         * refresh exists to recover from -- the most-recent-N slice begins NEWER
         * than the transcript's own oldest loaded row, and assigning it wholesale
         * would delete that scrollback. Matching the count keeps the row COUNT, not
         * the row IDENTITIES.
         *
         * So keep any prior head sitting above the page's first row, through the
         * one shared cut `switchSlot`/`warmSlotCache` use, so a third reducer
         * cannot re-derive it and diverge (:1360). `thinking` is held out (no
         * identity, broadcast-only) and re-placed by `mergePreservedThinking`
         * below; `permission` is held out because `statePerms` re-injects the
         * client's own copies with their resolved flags, and keeping them here too
         * would seat each card twice. Identity is `meta.mid` only, so an
         * unidentified page declines the cut rather than guessing -- the same
         * boundary the two existing head-keeping reducers already stand on.
         */
        const priorServerRows = state.messages.filter(m => m.role !== 'thinking' && m.role !== 'permission')
        const { olderHead } = olderHeadAbovePage(priorServerRows, messages)
        /* The cursor is a row OFFSET, so a kept head shifts it down by its own
         * server-row count. Both boundary cases (head proves completeness / the two
         * counts disagree) are owned by `pagingCursorAfterKeptHead`, not clamped. */
        const keptCursor = pagingCursorAfterKeptHead(
          hasMore, nextBefore, serverRowCount(olderHead))
        const merged = [...olderHead, ...messages.filter(m => m.role !== 'permission'), ...statePerms.values()]
        const mergedWithPastes = mergePreservedPastes(state.messages, merged)
        // Only sort if permissions were re-injected (they need positional merge).
        // Backend messages arrive in order; sorting with mixed ts formats reorders them.
        const sorted = statePerms.size > 0
          ? mergedWithPastes.sort((a, b) => tsNum(a.ts) - tsNum(b.ts))
          : mergedWithPastes
        // Reasoning is client-only (never persisted server-side); re-insert it so
        // a finished turn's thinking block survives this refresh.
        // Coverage from the PURE fetched page (`messages`): `sorted` carries
        // re-injected preserved permission cards, which must not vouch for
        // history the snapshot never covered.
        /* `windowComplete` defaults to TRUE, which was accurate while this fetch was
         * unbounded and is a claim a count-matched page cannot make. It gates the
         * `ambiguous()` skip, so an over-claim lets a text-anchored block seat on a
         * duplicate answer inside the window while its real anchor sits above it.
         * Same value as the `reinsertThinkingOrphans` call below and as
         * `switchSlot.fulfilled` -- the retained head is part of the loaded window,
         * so raw `hasMore` would park reasoning whose anchor is already on screen. */
        state.messages = deduplicateByMid(mergePreservedThinking(state.messages, mergePreservedClientTs(state.messages, sorted), messages, !keptCursor.hasMore))
        // A refresh rebuilds `messages` wholesale, so parked reasoning has to be re-seated
        // here too — otherwise it stays invisible until the next slot switch.
        // (Re-seating only ADDS client-only thinking rows, which by contract
        // never carry a server-minted mid, so the deduplicateByMid pass above
        // stays authoritative for the rebuilt history.)
        const parkedOnRefresh = (state.thinkingOrphans ??= {})
        // `windowComplete` describes the LOADED window, not the fetch: `messages`
        // now carries the retained head, so a raw `hasMore` would park reasoning
        // whose anchor is already on screen.
        const seatedOnRefresh = reinsertThinkingOrphans(state.messages, parkedOnRefresh[safeKey(key)] ?? [], !keptCursor.hasMore)
        state.messages = seatedOnRefresh.list
        parkedOnRefresh[safeKey(key)] = seatedOnRefresh.remaining
        // Re-hydrate queued bubbles through the SAME shared path as
        // switchSlot/warmSlotCache. The merge above is rebuilt from server
        // history + preserved perms/thinking and carries no `queued` bubbles, so
        // without this a refresh (e.g. the one fired on chat_done) would vanish a
        // user's pending queued messages. Routing all three slot-detail reducers
        // through hydrateQueuedBubbles is what stops them drifting apart again.
        state.messages = hydrateQueuedBubbles(state.messages, queue)
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        setPagingCursor(state, keptCursor.hasMore, keptCursor.nextBefore)
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(warmSlotCache.fulfilled, (state, action) => {
        if (!action.payload) return
        const { key, messages, queue, hasMore, total, running, warmSeq } = action.payload
        if (isUnsafeKey(key)) return
        // Slot became active between dispatch and fulfilment — switchSlot now
        // owns its messages, so leave the cache for it to manage.
        if (state.activeSlot === key) return
        if (!state.slotMessages) state.slotMessages = {}
        if (!state.slotPaneHasMore) state.slotPaneHasMore = {}
        // Preserve permission flags resolved client-side but not yet reflected
        // in the refetched history (a grid pane can resolve an approval between
        // the server snapshot and this warm), then collapse the pane's
        // optimistic/streamed/echoed messages to the canonical history.
        const localResolved = new Map<string, unknown>()
        for (const m of (state.slotMessages[key] || [])) {
          if (m.role === 'permission' && m.meta?.approval_id && m.meta?.resolved) {
            localResolved.set(m.meta.approval_id as string, m.meta.resolved)
          }
        }
        const hydrated = messages.map(m => {
          const aid = m.role === 'permission' ? (m.meta?.approval_id as string | undefined) : undefined
          return aid && localResolved.has(aid)
            ? { ...m, meta: { ...m.meta, resolved: localResolved.get(aid) } }
            : m
        })
        // Hydrate queued bubbles through the single shared path
        // (hydrateQueuedBubbles). Without this, warming a background slot's cache
        // dropped its pending queued bubbles, so switching to that slot rendered
        // the completed history minus anything the user had queued behind the
        // in-flight turn (the bubbles only reappeared on a later full fetch).
        // Routing every slot-detail reducer through the one helper is what keeps
        // this from silently diverging from switchSlot/refreshSlot again.
        const warmed = hydrateQueuedBubbles(hydrated, queue)
        // A bounded warm replacing the array wholesale deletes scrollback under a
        // reader, so keep any older head that sits above the warm's first row.
        // The server queue is authoritative for every pane, so a branch that
        // keeps prior rows must not keep the stale queued ones alongside it.
        const priorAll = hydrateQueuedBubbles(state.slotMessages[safeKey(key)] ?? [], queue)
        // Reasoning is broadcast-only and never persisted, so it is not a SERVER
        // row and must not drive this reconciliation: it carries no identity, so
        // the rescue below would keep it under "decline, not guess" and append a
        // second copy of a block the helper re-places at the end. Held out here
        // and restored by that helper, which appends any block it cannot anchor,
        // so holding it out cannot lose one.
        const prior = priorAll.filter(m => m.role !== 'thinking')
        // Identity is meta.mid only: two rows can share a ts, so a ts match can
        // cut at the wrong row and drop one. No mid means decline, not guess.
        const { cutIdx, olderHead } = olderHeadAbovePage(prior, warmed)
        // Disjoint-and-behind means a disconnect, not legacy rows: a strict ts
        // ORDER test on PARSED instants (not raw strings, not an identity match).
        const priorNewestTs = tsEpoch(prior[prior.length - 1]?.ts)
        const warmOldestTs = tsEpoch(warmed[0]?.ts)
        const longerPrior = cutIdx < 0 && prior.length > warmed.length
        const priorEndsBeforePage = longerPrior
          && priorNewestTs !== null && warmOldestTs !== null && priorNewestTs < warmOldestTs
        // No identity to cut on (legacy rows carry no mid), so replacing would drop
        // scrollback the pane already loaded -- keep the longer array instead.
        const keptPrior = longerPrior && !priorEndsBeforePage
        // Anchor on the newest prior row the warm still represents; rows after it
        // are newer than the page. The warm's own newest row can carry no identity.
        const warmIds = new Set<string>()
        for (const m of warmed) for (const id of rowIdentities(m)) warmIds.add(id)
        let anchorIdx = -1
        for (let i = prior.length - 1; i >= 0; i--) {
          if (rowIdentities(prior[i]).some(id => warmIds.has(id))) { anchorIdx = i; break }
        }
        // A fall in the server's own count means history was truncated between
        // that fetch and this one, so a row this pane still holds after the
        // anchor was DISCARDED rather than merely missed by an early page. No
        // retained count means no delta to read, so decline and keep the rescue.
        const priorTotal = state.slotServerTotal?.[safeKey(key)]
        // A count from a response that PREDATES the one which set the baseline is
        // stale, not a truncation. Unknown order still suppresses -- decline, not guess.
        const priorSeq = state.slotServerTotalSeq?.[safeKey(key)]
        const staleTotal = typeof warmSeq === 'number' && typeof priorSeq === 'number'
          && warmSeq < priorSeq
        const serverShrank = typeof priorTotal === 'number' && typeof total === 'number'
          && total < priorTotal && !staleTotal
        const rescuable = anchorIdx >= 0 && !serverShrank
          ? tailNotInPage(prior.slice(anchorIdx + 1), warmed)
          : []
        // A rewrite REPLACES a reply, so the count holds while the post-anchor rows
        // differ. Equal tail LENGTH is what separates that from a real newer row.
        const anchorIds = anchorIdx >= 0 ? rowIdentities(prior[anchorIdx]) : []
        const warmAnchorIdx = warmed.findIndex(m => rowIdentities(m).some(id => anchorIds.includes(id)))
        const sameCountRewrite = rescuable.length > 0 && warmAnchorIdx >= 0 && !staleTotal
          && typeof priorTotal === 'number' && typeof total === 'number' && total === priorTotal
          && prior.length - anchorIdx === warmed.length - warmAnchorIdx
        const newerTail = sameCountRewrite ? [] : rescuable
        // A confirmed shrink means those rows were REMOVED, so the disjoint branches
        // below would restore them. It sits after the head: `cutIdx > 0` vs `< 0`.
        const base = olderHead.length
          ? [...olderHead, ...warmed]
          : serverShrank
            ? warmed
            : priorEndsBeforePage
              ? [...prior, ...tailNotInPage(warmed, prior)]
              : keptPrior ? prior : warmed
        // The rescued tail recovers prior rows the base DROPPED, so a base already
        // carrying all of prior must not append it again -- that duplicates rows.
        const keepsAllPrior = keptPrior || priorEndsBeforePage
        const mergedRaw = newerTail.length && !keepsAllPrior ? [...base, ...newerTail] : base
        // A queued row has no identity, so both merge branches keep one the warm
        // already re-added; collapsing once dedupes it and restores queued-last.
        const merged = hydrateQueuedBubbles(mergedRaw, queue)
        // Restore the preserved reasoning onto the reconciled list. A slot the
        // user switched AWAY from mid-turn holds its blocks only in this cache
        // (switchSlot.pending caches `state.messages` wholesale) and this warm is
        // driven by that slot's own chat_done, so rebuilding from server history
        // -- which never holds a thinking row -- dropped every block instead of
        // only misplacing the later ones.
        // Coverage from the PURE fetched page (`hydrated` — the payload rows,
        // before hydrateQueuedBubbles re-attaches client queued bubbles):
        // `merged` can carry rescued prior-cache rows and queued bubbles, which
        // must not vouch for history the snapshot never covered.
        const revived = mergePreservedThinking(priorAll, merged, hydrated)
        // Omitting boundedLen DELETES the marker, while omitting hasMore keeps the
        // OLD value -- and its presence is what stops a late hydrate prepending.
        const warmIsPrefix = base === warmed
        // The marker is an INDEX INTO the array written, and reviving inserts rows
        // above it, so it is re-derived against `revived` rather than taken as
        // `warmed.length`. The helper pushes incoming rows by reference, so the
        // warm's own last row locates the boundary; a miss falls back to the
        // unrevived length rather than guessing.
        // Queued bubbles are not server page rows and the collapse above moves
        // them past the tail, so the boundary tracks the page's own last row.
        const pageRows = warmed.filter(m => m.role !== 'queued')
        const boundaryIdx = pageRows.length ? revived.indexOf(pageRows[pageRows.length - 1]) : -1
        const boundedLen = boundaryIdx >= 0 ? boundaryIdx + 1 : pageRows.length
        writeSlotPage(state, key, revived, warmIsPrefix ? hasMore : undefined,
          warmIsPrefix && hasMore ? boundedLen : undefined)
        retainServerTotal(state, key, total, running, warmSeq, action.payload.boundedRead)
        // Idle the per-slot run indicator only when the server says the turn is
        // NOT running. This is a pure non-regression gate for the reconnect
        // caller (which warms slots MID-TURN): idling is idempotent with the
        // _done frame — the turn-done caller's belt-and-braces contract for the
        // fetch-completes-after-_done ordering, unchanged — while the
        // unconditional write it replaces wiped a RUNNING background pane's
        // indicator with no server-side recovery until the next chunk frame.
        // Deliberately NO write in the running direction: the warm is a
        // point-in-time snapshot racing the ordered live-frame writers
        // (chunk -> streaming, _done -> idle), and any promotion policy has a
        // losing ordering (a late fulfillment resurrected a pane a _done had
        // already idled, wedging its composer locked with no healer inside the
        // reconnect suppression window). A turn that STARTED while the socket
        // was down therefore still reads idle until its first post-reconnect
        // frame — exactly as on main today, where reconnect never touches
        // background run state at all; closing that pre-existing gap needs an
        // ordering token on the run entry and is tracked separately.
        if (!running) {
          const run = (state.slotRun[safeKey(key)] ??= { state: 'idle' })
          run.state = 'idle'
          run.lastChunkSeq = undefined
          // Deliberately NOT synced into the failed-switch origin snapshot:
          // this write comes from a point-in-time HTTP snapshot racing the
          // ordered live-frame writers (the block comment above), so a stale
          // fulfillment landing mid-switch could mark a mid-turn origin idle
          // and the restore would unlock its composer. Only the ORDERED frame
          // writers in applyNonActiveFrame feed syncOriginRun.
        }
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(createSlot.pending, (state) => { state.creatingSlot = true })
      .addCase(createSlot.rejected, (state) => { state.creatingSlot = false })
      .addCase(createSlot.fulfilled, (state, action) => {
        // The create POST resolved, so clear the pending flag regardless of
        // whether we activate below. Otherwise the switched-away early-return
        // would strand the "Creating…" spinner on forever.
        state.creatingSlot = false
        // Switched-away guard: if the user moved to a different
        // session while this create was pending (a slow "Creating…" under memory
        // pressure), do NOT hijack the view. The new slot is already registered
        // via addSlotOptimistic; just leave the user where they are. Mirrors the
        // guard switchSlot/refreshSlot/warmSlotCache already have. `send()`'s
        // forceNew path and welcome-screen New Chat both leave activeSlot equal
        // to the origin, so they still activate normally.
        //
        // Conscious edge: a rapid double New Chat from the same slot makes both
        // creates capture the same origin; the first fulfilled activates its
        // slot (moving activeSlot), so the second sees activeSlot !== origin and
        // stays put. "First create wins" rather than the prior "last wins". Both
        // slots exist in the sidebar and both land the user on an empty chat, so
        // the outcomes are equivalent, accepted over re-stealing focus.
        // Caller asked for a background create (see `activate` above): the slot
        // is registered but focus stays put until the caller switches to it.
        if (action.meta.activate === false) return
        const origin = action.meta.originActiveSlot ?? null
        if (state.activeSlot !== origin) return
        if (state.activeSlot) {
          state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
          state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        }
        state.activeSlot = action.payload.key
        state.messages = []
        state.toolLog = []
        state.subagents = {}
        state.activityTab = 'changes'
        // A brand-new chat starts with the side panel CLOSED, like every other
        // slot-entry path (switchSlot / resumeFromHistory read `?? false` for a
        // slot they have no cached bucket for). Without this the panel state of
        // the chat being left leaked into the new one — and was not persisted
        // under the new slot's key either, so a reload silently closed it again.
        state.activityOpen = false
        state.slotRunning = false
        state.slotStopping = false
        state.slotState = 'idle'
        setPagingCursor(state, false, 0)
      })
      .addCase(deleteSlot.fulfilled, (state, action) => {
        evictSlotState(state, action.payload)
        if (state.activeSlot === action.payload) {
          state.activeSlot = null
          state.messages = []
          state.toolLog = []
          state.subagents = {}
        }
      })
      .addCase(resumeFromHistory.pending, (state, action) => {
        // A new attempt supersedes whatever the previous one narrated, and its
        // requestId becomes the only answer allowed to write the notice below.
        state.lastResumeRequestId = action.meta.requestId
        state.unresumableResume = null
      })
      .addCase(resumeFromHistory.fulfilled, (state, action) => {
        // A resume that resolved to a surface ChatPage cannot display must not
        // mutate this slice at all: consuming the history row while the notice
        // says "can't be opened" reads as data loss, and switching activeSlot
        // to an undisplayable slot is the silent bounce #3624 exists to stop.
        // The wire resume itself already happened; the row stays reachable in
        // Older Sessions.
        //
        // `!ok` shares this early return for the same reason -- nothing was
        // resumed, so nothing here may move -- but it is a DIFFERENT story to
        // tell, hence the `reason` split. Both are recorded rather than left
        // for each caller to re-derive: this is the one place that already
        // knows the resume did not leave the user in a usable session, so
        // every entry point -- sidebar row, ChatPage's "Continue a previous
        // chat" list, the notification panel, and the two palette providers
        // that have no component to render into -- reads the same answer
        // (#5925).
        if (!action.payload.ok || !isChatPageSurface(action.payload.surface)) {
          // Guarded on the ordering token so a stale answer cannot narrate a
          // row the user has moved past.
          if (action.meta.requestId === state.lastResumeRequestId) {
            state.unresumableResume = {
              key: action.meta.arg.key,
              title: action.meta.arg.title,
              surface: action.payload.surface ?? '',
              reason: action.payload.ok ? 'surface' : 'failed',
            }
          }
          return
        }
        if (action.payload.ok) {
          // The row just became an open tab, so it leaves the Older-sessions
          // pane — that pane is the complement of the tab list, and leaving the
          // row behind reproduces the listed-twice state via its own primary
          // action. Keyed on the history row the user clicked (`meta.arg.key`),
          // not on the slot key the resume returned: only the former is the
          // transcript name `state.history` is indexed by.
          const consumed = state.history.length
          state.history = state.history.filter(s => s.key !== action.meta.arg.key)
          if (state.history.length < consumed) {
            // `historyOffset` counts rows consumed from the SERVER's list, and the
            // server drops this row too now that a slot holds it. Leaving the
            // offset where it was would ask for a window one row past the end of a
            // list that just got shorter, so the next page would skip a row the
            // user has never seen. Guarded on an actual removal: a resume that
            // came from somewhere else (a search hit, the command palette) filters
            // nothing here and must not move the offset.
            state.historyOffset = Math.max(0, state.historyOffset - 1)
          }
          state.slotHistory = state.slotHistory.filter(k => k !== action.payload.key)
          if (state.activeSlot) {
            state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
            if (state.activeSlot !== action.payload.key) {
              state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
            }
          }
          const cached = state.slotActivity[action.payload.key]
          state.toolLog = cached?.toolLog ?? []
          state.subagents = cached?.subagents ?? {}
          // Legacy cached 'tools'/'nav'/'files' values fall back to 'changes'
          // (see switchSlot for why 'files' is no longer one of these tabs).
          state.activityTab = (cached?.activityTab && !['tools', 'nav', 'files'].includes(cached.activityTab as string)) ? cached.activityTab : 'changes'
          state.activityOpen = cached?.activityOpen ?? false
          state.activeSlot = action.payload.key
          state.messages = mergePreservedPastes(state.messages, action.payload.messages)
          state.slotState = 'idle'
          state.pendingTurnSlot = null
          setPagingCursor(state, action.payload.hasMore, action.payload.nextBefore)
        }
      })
      .addCase(resumeFromHistory.rejected, (state, action) => {
        // The likeliest failure of all: `api.resumeChatSlot` throws on any
        // non-2xx, so a 404/409/5xx or a dropped connection lands HERE, not on
        // the `ok: false` branch above. Every caller's handling of it was a
        // silent swallow -- ChatPage's `catch {}`, the palette providers'
        // `void dispatch`, the notification panel's console log -- so the click
        // looked exactly as dead as the bug this field exists to fix.
        if (action.meta.requestId !== state.lastResumeRequestId) return
        state.unresumableResume = {
          key: action.meta.arg.key,
          title: action.meta.arg.title,
          surface: '',
          reason: 'failed',
        }
      })
      .addCase(deleteHistorySession.fulfilled, (state, action) => {
        state.history = state.history.filter(s => s.key !== action.payload)
      })
      .addCase(loadOlderMessages.pending, (state) => {
        state.loadingOlder = true
        // A retry clears the red state without re-basing the cursor, so the helper cannot.
        state.slotOlderError = false
      })
      .addCase(loadOlderMessages.fulfilled, (state, action) => {
        state.loadingOlder = false
        if (action.payload && action.payload.slot === state.activeSlot) {
          // Merge paste state into the older messages first, then prepend so
          // historical pastes re-tokenize from localStorage instead of showing
          // as fully-expanded text.
          const merged = mergePreservedPastes(state.messages, action.payload.messages)
          // Invariant, not the fix: virtualKeyFor derives a row key from the
          // message ts, so an overlapping page would reach React as a duplicate
          // key. Identity is meta.mid only -- see isRedeliveredMessage on why a
          // ts tuple cannot express this without dropping legitimate rows.
          const fresh = merged.filter(m => !isRedeliveredMessage(state.messages, m.meta))
          state.messages = [...fresh, ...state.messages]
          // Paging older is exactly when a parked block's anchor becomes loaded.
          const parked = (state.thinkingOrphans ??= {})
          const key = safeKey(action.payload.slot)
          // The payload, not state: setPagingCursor runs below, so state still holds
          // the previous page's value -- true on any page-back.
          const seated = reinsertThinkingOrphans(state.messages, parked[key] ?? [], !action.payload.hasMore)
          state.messages = seated.list
          parked[key] = seated.remaining
          setPagingCursor(state, action.payload.hasMore, action.payload.nextBefore)
        }
      })
      .addCase(loadOlderMessages.rejected, (state, action) => {
        state.loadingOlder = false
        const failed = action.payload as { slot?: string } | undefined
        if (failed?.slot === state.activeSlot) state.slotOlderError = true
      })
  },
})

export const {
  setActiveSlot, clearSlotState, setPendingInput, setAgentSwitchNotice, clearUnresumableResume, setQuestionCard, retireStatelessQuestion, clearQuestionCard, setQuestionDraft, resolveQuestionCard, setFollowupCard, clearFollowupCard, dismissFollowupItem, setFolderSuggestion, clearFolderSuggestion, ageFolderSuggestion, appendMessage, appendSlotMessage, updateStreamingMessage, finalizeAssistant,
  removeThinking, confirmOptimisticSend, resolveOptimisticSteer, removeByApprovalId, resolveByApprovalId, clearPendingPermissions, setSlotRunning, setSlotStopping, startLocalTurn, endLocalTurn, syncSlotRunningFromServer, setSlotState, setSlotStatusDetail, setStopPressedAt, clearMessages, clearSlotCache, truncateAfterIndex, replaceMessages, hydrateSlotMessages, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage, reorderQueuedMessages,
  sseContextUsage, setVoicePlaying, setVoiceAudio,
  toggleActivity, openActivityToTab, openActivityPanel, openActivityToTool, clearFocusToolCallId, requestSlotReveal, clearSlotReveal, clearSubagentsForSnapshot, sseSubagentPending, markSubagentApproving, sseSubagentSpawn, sseSubagentTool, sseSubagentStalled, sseSubagentRetrying, sseSubagentDone, sseSubagentQueued,
  sseSubagentBatchUpdate, sseSubagentBatchChunks, selectSubagent, clearTerminalSubagents,
  setGoalLoops, sseGoalLoop,
  sseSubagentSnapshot, sseToolActivity, sseToolResult, sseActivityEvent,
  sseMcpAppRender,
  sseWorkflowEvent, clearWorkflowRun, reconcileWorkflowRuns,
  sseSideResult, sseSideQueue, sideReleaseConsumed, sideClose, sideOptimisticAppend, sideOptimisticRollback,
} = chatSlice.actions
export default chatSlice.reducer

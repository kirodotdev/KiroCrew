import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { addSlotOptimistic, updateSlot, removeSlotOptimistic, markSlotRead, fetchSlots, slotSurfaceKey, sseSlots, sseConnected } from './dashboardSlice'
import { resolveDefaultColor } from '../utils/sessionColors'
import { isSystemNoticeKind } from '../lib/systemNotice'
import { gcSessionStorage } from '../utils/storageGc'
import type { RootState } from './index'
import type { ChatMessage, ChatSlot, SessionInfo, SubagentActivity, ToolActivity } from '../types'
import { SOFT_STOP_DEBOUNCE_MS, SPAWN_LAUNCH_MARKER } from '../pages/chat/types'
import { mergePreservedPastes } from '../utils/pasteTokens'
import { safeSetItem } from '../utils/safeStorage'
import type { McpAppRenderPayload } from '../lib/mcpAppSrcdoc'
import { i18nT } from '../i18n/t'
import { secureRandomId } from '../utils/secureId'
import { mergeIntoDraft } from '../utils/chatDrafts'

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

/** Structural equality for the JSON-shaped message fields (`meta`, `variants`). */
function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false
  const aArr = Array.isArray(a), bArr = Array.isArray(b)
  if (aArr !== bArr) return false
  if (aArr && bArr) return a.length === b.length && a.every((v, i) => jsonEqual(v, b[i]))
  const ak = Object.keys(a), bk = Object.keys(b)
  if (ak.length !== bk.length) return false
  return ak.every(k => Object.prototype.hasOwnProperty.call(b, k)
    && jsonEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]))
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
   *  while the drawer is collapsed (and under preview focus / on mobile), and
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
  folderSuggestions: Record<string, { folderId: string; folderName: string; breadcrumb: string; ts: number }>
  // Slot with a locally-started turn awaiting server confirmation. While set,
  // the slots-sync ignores a server running=false for it (the snapshot may
  // predate the send). Cleared on server confirmation or turn end.
  pendingTurnSlot: string | null
}

const MAX_RETIRED_QUEUE_IDS = 50

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
  loadingOlder: false,
  slotOlderError: false,
  lastChunkSeq: undefined,
  _wsChunkedDuringFetch: false,
  _redeliveredFramesDropped: 0,
  history: [],
  historyHasMore: false,
  historyOffset: 0,
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
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') { msgs[i].role = 'assistant'; msgs[i].rawText = msgs[i].content; break }
    }
    return
  }
  if (role === 'compacting') { run.state = 'compacting'; return }
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
 *  and `sseSubagentChunk` bumps this reference per sub-agent chunk, so a
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

/** Rows to request per older-history page. */
const OLDER_PAGE_LIMIT = 100

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

async function fetchSlotDetail(key: string) {
  // No limit → backend returns all chained history (across gateway restarts).
  const d = await api.chatSlotDetail(key)
  type QueueItem = string | { content: string; id: string }
  return { key, nextBefore: d.next_before || 0, messages: filterMessages(d.messages || []), running: d.running || false, stopping: d.stopping || false, hasMore: d.has_more || false, total: d.total || 0, queue: ((d.queue || []) as QueueItem[]).map((q: QueueItem) => typeof q === 'string' ? { content: q, queueId: crypto.randomUUID(), ts: new Date().toISOString() } : { content: q.content, queueId: q.id, ts: new Date().toISOString() }), context: d.context_pct != null ? { pct: d.context_pct, used: d.context_used_tokens ?? undefined, window: d.context_window_tokens ?? undefined } : undefined }
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

export const switchSlot = createAsyncThunk(
  'chat/switchSlot',
  async (key: string, { dispatch }) => {
    // Safe unconditionally: this fetch resets the pane's messages and cursor, so
    // any older page still in flight is superseded even when the key is unchanged.
    _abortLoadOlder?.()
    dispatch(markSlotRead(key))
    return fetchSlotDetail(key)
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
 *  addressed by its content. */
type ThinkingAnchor = { tool: string; text?: undefined } | { tool?: undefined; text: string }

/** Re-insert client-only reasoning (`thinking`) messages into a server-refreshed
 *  message list. The backend never persists reasoning, so a refresh (e.g. the
 *  one fired on chat_done) would otherwise drop the thinking block the instant a
 *  turn finishes. Each preserved block is anchored to the first row that followed
 *  it in the old list and re-inserted just before that row again. Any block whose
 *  anchor isn't found is appended so it is never silently lost. Returns
 *  `incoming` unchanged (reference-equal) when there is nothing to preserve.
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
 *  row and so the correct side of the pair. */
function mergePreservedThinking<M extends { role: string; content: string; cls?: string; meta?: Record<string, unknown> }>(
  existing: M[],
  incoming: M[],
): M[] {
  const toolAnchorId = (m: M): string => {
    if (m.role !== 'tool') return ''
    const id = m.meta?.tool_call_id
    return typeof id === 'string' ? id : ''
  }
  const preserved: Array<{ msg: M; anchor: ThinkingAnchor | null }> = []
  for (let i = 0; i < existing.length; i++) {
    const m = existing[i]
    if (m.role !== 'thinking' || !m.content) continue
    let anchor: ThinkingAnchor | null = null
    for (let j = i + 1; j < existing.length; j++) {
      const cand = existing[j]
      const tid = toolAnchorId(cand)
      if (tid) { anchor = { tool: tid }; break }
      if (cand.role === 'assistant' || cand.role === 'streaming') { anchor = { text: cand.content.trimEnd() }; break }
      // A confirmed steer does not end this block's turn, so the row after it is
      // still its anchor. Breaking here instead leaves `anchor` null, and an
      // unanchored block is appended at the tail — below the answer and its
      // footer — where the forward scan can never reach an anchorable row again,
      // so it stays there and is re-appended on every later refresh.
      if (isTurnBoundaryUser(cand)) break
    }
    preserved.push({ msg: m, anchor })
  }
  if (!preserved.length) return incoming
  const used = new Set<number>()
  const result: M[] = []
  for (const item of incoming) {
    const tid = toolAnchorId(item)
    const isText = item.role === 'assistant' || item.role === 'streaming'
    if (tid || isText) {
      const c = isText ? item.content.trimEnd() : ''
      for (let p = 0; p < preserved.length; p++) {
        if (used.has(p)) continue
        const a = preserved[p].anchor
        if (!a) continue
        if (tid ? a.tool === tid : a.tool === undefined && a.text === c) {
          result.push({ ...preserved[p].msg }); used.add(p); break
        }
      }
    }
    result.push(item)
  }
  for (let p = 0; p < preserved.length; p++) {
    if (!used.has(p)) result.push({ ...preserved[p].msg })
  }
  return result
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

export const refreshSlot = createAsyncThunk(
  'chat/refreshSlot',
  async (key: string, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (state.activeSlot !== key) return null
    return fetchSlotDetail(key)
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
export const warmSlotCache = createAsyncThunk(
  'chat/warmSlotCache',
  async (key: string, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (state.activeSlot === key) return null
    return fetchSlotDetail(key)
  },
)

export const createSlot = createAsyncThunk<
  ChatSlot,
  { agent?: string; model?: string; mode?: string; memory_mode?: string; clean_mode?: boolean; folder_id?: string | null; title?: string; color_index?: number | null; color_hex?: string | null; project?: string | null; activate?: boolean } | string | undefined,
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
    const slot = await api.createChatSlot(undefined, agent, model, mode, memory_mode, title, clean_mode, undefined, folderId || undefined)
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
    return { ok: d.ok, key: d.key, nextBefore: cursor ?? 0, messages: filterMessages(d.messages || []), hasMore: cursor !== null && (d.has_more || false), total: d.total || 0 }
  },
)

export const forkSlot = createAsyncThunk(
  'chat/forkSlot',
  async (
    { slot, atIndex, prompt, mode, direction }: { slot: string; atIndex?: number; prompt?: string; mode?: string; direction?: 'head' | 'tail' },
    { dispatch },
  ) => {
    const d = await api.forkChatSlot(slot, atIndex, prompt, mode, direction)
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
      const d = await api.chatSlotDetail(slot, OLDER_PAGE_LIMIT, state.slotOldestIndex, controller.signal)
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
  const out = Object.values(subs).filter(a => a.status === 'pending' && !!a.approval_id)
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
    const countActive = (m?: Record<string, SubagentActivity>) => {
      if (!m) return 0
      let n = 0
      for (const a of Object.values(m)) {
        if (a.status === 'running' || a.status === 'tool' || a.status === 'pending') n++
      }
      return n
    }
    let total = activeSlot ? countActive(activeSubs) : 0
    for (const [slot, act] of Object.entries(slotActivity ?? {})) {
      // On switchSlot the active slot's map is aliased into both
      // state.subagents and slotActivity[active].subagents (same reference),
      // so this guard is what prevents double-counting it.
      if (slot === activeSlot) continue
      total += countActive(act.subagents)
    }
    for (const q of Object.values(queued ?? {})) total += q > 0 ? q : 0
    return total
  },
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
 * True when *m* is the card recorded because the USER pressed Stop.
 *
 * Two forms exist and both are load-bearing: the websocket path sets `kind` AND
 * `meta.kind` (see the stop_event branch in the message reducer), while a
 * transcript rehydrated from disk carries only the JSON-encoded `cls` that
 * `parse_cls_meta()` unpacks into `meta`. `ChatPage` and `ChatMessageList`
 * already test both; this is the same predicate named once so a fourth caller
 * cannot check only half of it.
 */
export const isStopEvent = (m: ChatMessage): boolean =>
  m.kind === 'stop_event' || (m.meta as { kind?: string } | undefined)?.kind === 'stop_event'

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
      state.folderSuggestions[slot] = { folderId, folderName, breadcrumb, ts: ts ?? Date.now() / 1000 }
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
      // running turn" card. Scan backwards (bounded) over optimistic STEER
      // bubbles only (a plain optimistic user message with coincidentally
      // identical text must never be consumed): prefer exactly matching
      // content (handles rapid back-to-back steers in order), else fall back
      // to the most recent one (server-side redaction can alter the echoed
      // content, so an exact match isn't guaranteed).
      if (message.role === 'user' && message.meta?.steer && !message.meta?.optimistic) {
        const floor = Math.max(0, msgs.length - 50)
        let target: ChatMessage | undefined
        let fallback: ChatMessage | undefined
        for (let i = msgs.length - 1; i >= floor; i--) {
          const m = msgs[i]
          if (m.role !== 'user' || !m.meta?.optimistic || !m.meta?.steer) continue
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
    confirmOptimisticSend(state, action: PayloadAction<{ slot: string; sendId: string }>) {
      const { slot, sendId } = action.payload
      if (isUnsafeKey(slot)) return
      const confirm = (msgs: ChatMessage[] | undefined): boolean => {
        if (!msgs) return false
        const floor = Math.max(0, msgs.length - RECONCILE_WINDOW)
        for (let i = msgs.length - 1; i >= floor; i--) {
          const m = msgs[i]
          if (m.role !== 'user' || m.meta?.sendId !== sendId) continue
          const meta = { ...(m.meta || {}) }
          delete meta.optimistic
          m.meta = meta
          return true
        }
        return false
      }
      if (!confirm(state.messages)) confirm(state.slotMessages[safeKey(slot)])
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
      // If rejected, mark the matching toolLog entry so the pill can show a rejection icon
      const toolCallId = m?.meta?.tool_call_id as string | undefined
      if (decision === 'rejected' && toolCallId) {
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
    clearMessages(state) { state.messages = []; setPagingCursor(state, false, 0); state.voiceAudio = null; state.voicePlaying = false; if (state.activeSlot) evictMcpApps(state, state.activeSlot) },
    truncateAfterIndex(state, action: PayloadAction<number>) { state.messages = state.messages.slice(0, action.payload) },
    replaceMessages(state, action: PayloadAction<ChatMessage[]>) { state.messages = action.payload },
    /** Path B: seed a non-active slot's message history into the per-slot store
     *  (one-time hydrate on pane mount). Prepends the server history BEFORE any
     *  frames that already arrived live: applyNonActiveFrame seeds slotMessages
     *  via `??= []` on the first WS frame, so `cur` can be non-empty before this
     *  hydrate fetch resolves. A dedicated `slotHydrated` flag makes it fire
     *  exactly once, so a racing frame can't make us silently drop history.
     *  No-op for the active slot (its mirror is already live). */
    hydrateSlotMessages(state, action: PayloadAction<{ slot: string; messages: ChatMessage[] }>) {
      const { slot, messages } = action.payload
      if (isUnsafeKey(slot)) return
      if (slot === state.activeSlot) return
      if (state.slotHydrated?.[slot]) return
      const cur = state.slotMessages[slot] ?? []
      state.slotMessages[safeKey(slot)] = [...messages, ...cur]
      if (!state.slotHydrated) state.slotHydrated = {}
      state.slotHydrated[safeKey(slot)] = true
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
    sseSubagentSpawn(state, action: PayloadAction<{ slot: string; id: string; task: string; agent: string }>) {
      if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[action.payload.id]
      if (existing?.status === 'pending') {
        existing.status = 'running'
        existing.agent = action.payload.agent || existing.agent || 'kirocrew'
        // The spawn event carries the authoritative task text (the pending
        // card's task is derived from the approval title, which may be empty
        // or just "spawn_run") — always prefer the spawn payload's task.
        if (action.payload.task) existing.task = action.payload.task
        return
      }
      subs[safeKey(action.payload.id)] = {
        id: action.payload.id, task: action.payload.task, agent: action.payload.agent || 'kirocrew',
        status: 'running', streaming: existing?.streaming || '', lastTool: '', startedAt: existing?.startedAt || Date.now(), elapsed: 0,
        toolCount: 0, stalled: false,
      }
    },
    sseSubagentChunk(state, action: PayloadAction<{ slot: string; id: string; text: string }>) {
      // Prototype-pollution guard is centralized in getSlotSub (fail-closed on
      // __proto__/constructor/prototype ids) so no call site can forget it.
      const a = getSlotSub(state, action.payload.slot, action.payload.id)
      if (a) {
        a.retrying = false
        a.streaming += action.payload.text
        if (a.streaming.length > 50_000) {
          a.streaming = i18nT('store.chatSlice.truncated') + '\n' + a.streaming.slice(-40_000)
        }
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
    /** One coalesced ~1s frame of concatenated streaming text per agent
     *  (subscriber-only, mirrors sseSubagentChunk semantics). */
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
    sseSubagentDone(state, action: PayloadAction<{ slot: string; id: string; elapsed: number; error?: string; stopped?: boolean; outcome?: 'completed' | 'failed' | 'stopped'; task?: string; agent?: string; result?: string }>) {
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
        if (isNative && action.payload.result !== undefined) a.result = action.payload.result
      }
      else {
        subs[action.payload.id] = {
          id: action.payload.id,
          task: action.payload.task || '',
          agent: action.payload.agent || 'kirocrew',
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
    sseSubagentSnapshot(state, action: PayloadAction<{ id: string; slot: string; task: string; agent: string; streaming: string; last_tool: string; started: number; tool_count?: number; stalled?: boolean }>) {
      const d = action.payload
      if (isUnsafeKey(d.slot) || isUnsafeKey(d.id)) return
      const subs = d.slot && d.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(d.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[d.id]
      // Live events can interleave with replay because subscription starts before
      // snapshots are sent. Never let a stale running snapshot demote a terminal card.
      if (existing?.status === 'done' || existing?.status === 'error') return
      subs[safeKey(d.id)] = {
        id: d.id, task: d.task, agent: d.agent || 'kirocrew',
        status: d.last_tool ? 'tool' : 'running', streaming: d.streaming, lastTool: d.last_tool,
        startedAt: d.started * 1000, elapsed: 0,
        toolCount: d.tool_count ?? 0, stalled: d.stalled ?? false,
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
          cur.name = (d.name as string) || cur.name || run_id
          cur.status = 'running'
          break
        case 'phase_started':
          cur.phase = (d.title as string) || cur.phase
          break
        case 'log': {
          const msg = (d.message as string) || ''
          if (msg) cur.lastLog = msg
          break
        }
        case 'run_finished':
          cur.status = 'finished'
          break
        case 'run_failed':
          cur.status = 'failed'
          cur.error = (d.error as string) || cur.error
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
    /** Patch an existing message identified by ts. Used by the `chat_message_update`
     * server event to flip an mcp_oauth banner from "needs auth" to "authenticated"
     * after kiro-cli emits server_initialized. Patches both the active messages
     * array and the slotMessages cache so a slot the user isn't currently
     * viewing still shows the correct banner state on switch-back. */
    sseChatMessagePatchByTs(state, action: PayloadAction<{ slot: string; ts: string; meta?: Record<string, unknown>; content?: string }>) {
      const { slot, ts, meta, content } = action.payload
      if (!slot || !ts) return
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
        // This fetch replaces the cursor, so it is stale from here until it lands
        // -- including a same-key switch, where the key alone still looks valid.
        state.slotCursorKey = null
        state.slotSwitchRequestId = action.meta?.requestId ?? null
        state.slotSwitchTarget = action.meta?.arg ?? null
        // Save current slot's activity
        if (state.activeSlot) {
          state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
        }
        // Cache current slot's messages before switching
        if (state.activeSlot && state.messages.length > 0) {
          state.slotMessages[state.activeSlot] = state.messages
        }
        // Always strip target from history: activeSlot ∉ slotHistory
        state.slotHistory = state.slotHistory.filter(k => k !== action.meta.arg)
        if (state.activeSlot && state.activeSlot !== action.meta.arg) {
          state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        }
        // Restore target slot's activity (or empty)
        const cached = state.slotActivity[action.meta.arg]
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
        // Set activeSlot immediately so WS events for the new slot are accepted.
        // Restore cached messages if available (instant switch), otherwise show loading.
        state.activeSlot = action.meta.arg
        // The older-history error belongs to the outgoing chat and ownership moves
        // here, so it must clear now rather than when the fetch settles.
        state.slotOlderError = false
        const cachedMsgs = state.slotMessages[action.meta.arg]
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
        if (state.slotSwitchRequestId !== null && state.slotSwitchRequestId === action.meta?.requestId) { state.slotSwitchRequestId = null; state.slotSwitchTarget = null }
        const { key, messages, running, hasMore, queue, nextBefore } = action.payload
        if (isUnsafeKey(key)) return
        if (state.activeSlot !== key) return  // user switched away during fetch
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
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        setPagingCursor(state, hasMore, nextBefore)
        // Hydrate queued messages from the backend queue field through the
        // single shared path (hydrateQueuedBubbles) so this reducer cannot drift
        // from warmSlotCache/refreshSlot. It strips any WS-delivered queued
        // bubbles first (a queue_push may have arrived during the fetch) so the
        // server queue set stays canonical and non-duplicated.
        // Thinking blocks are client-only (never persisted server-side); re-insert
        // them so a switchSlot refresh does not discard the collapsible reasoning
        // trace. Without this, switching tabs and back drops all thinking blocks.
        next = mergePreservedThinking(existing, next)
        next = hydrateQueuedBubbles(next, queue)
        // Switching back to an already-loaded slot re-fetches a history that is
        // usually identical; skipping the write keeps every existing reference.
        if (!sameTranscript(existing, next)) state.messages = next
        // Update cache and clear loading state
        state.slotMessages[safeKey(key)] = state.messages
        state.slotLoading = false
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(switchSlot.rejected, (state, action) => {
        if (state.slotSwitchRequestId !== null && state.slotSwitchRequestId === action.meta?.requestId) { state.slotSwitchRequestId = null; state.slotSwitchTarget = null }
        if (state.activeSlot !== action.meta.arg) return
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
        const tsNum = (v: unknown): number => {
          const s = v == null ? '' : String(v)
          if (!s) return 0
          const n = Number(s)
          if (Number.isFinite(n)) return n  // numeric epoch
          const p = Date.parse(s)
          return Number.isFinite(p) ? p / 1000 : 0  // ISO → epoch seconds
        }
        const merged = [...messages.filter(m => m.role !== 'permission'), ...statePerms.values()]
        const mergedWithPastes = mergePreservedPastes(state.messages, merged)
        // Only sort if permissions were re-injected (they need positional merge).
        // Backend messages arrive in order; sorting with mixed ts formats reorders them.
        const sorted = statePerms.size > 0
          ? mergedWithPastes.sort((a, b) => tsNum(a.ts) - tsNum(b.ts))
          : mergedWithPastes
        // Reasoning is client-only (never persisted server-side); re-insert it so
        // a finished turn's thinking block survives this refresh.
        state.messages = mergePreservedThinking(state.messages, mergePreservedClientTs(state.messages, sorted))
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
        setPagingCursor(state, hasMore, nextBefore)
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(warmSlotCache.fulfilled, (state, action) => {
        if (!action.payload) return
        const { key, messages, queue } = action.payload
        if (isUnsafeKey(key)) return
        // Slot became active between dispatch and fulfilment — switchSlot now
        // owns its messages, so leave the cache for it to manage.
        if (state.activeSlot === key) return
        if (!state.slotMessages) state.slotMessages = {}
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
        // mergePreservedThinking for the same reason: a slot the user switched
        // AWAY from mid-turn has its reasoning in this cache (switchSlot.pending
        // caches `state.messages` wholesale), and this warm is driven by that
        // slot's own chat_done — so overwriting with server history, which never
        // holds a thinking row, dropped every block instead of only misplacing
        // the later ones.
        state.slotMessages[safeKey(key)] = hydrateQueuedBubbles(
          mergePreservedThinking(state.slotMessages[key] || [], hydrated),
          queue,
        )
        // Clear the per-slot run indicator (the _done frame already idles it;
        // this is belt-and-braces for the fetch-completes-after-_done ordering).
        const run = (state.slotRun[safeKey(key)] ??= { state: 'idle' })
        run.state = 'idle'
        run.lastChunkSeq = undefined
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
      .addCase(resumeFromHistory.fulfilled, (state, action) => {
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
  setActiveSlot, clearSlotState, setPendingInput, setAgentSwitchNotice, setQuestionCard, retireStatelessQuestion, clearQuestionCard, setQuestionDraft, resolveQuestionCard, setFollowupCard, clearFollowupCard, dismissFollowupItem, setFolderSuggestion, clearFolderSuggestion, appendMessage, appendSlotMessage, updateStreamingMessage, finalizeAssistant,
  removeThinking, confirmOptimisticSend, removeByApprovalId, resolveByApprovalId, clearPendingPermissions, setSlotRunning, setSlotStopping, startLocalTurn, syncSlotRunningFromServer, setSlotState, setSlotStatusDetail, setStopPressedAt, clearMessages, truncateAfterIndex, replaceMessages, hydrateSlotMessages, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage, reorderQueuedMessages,
  sseContextUsage, setVoicePlaying, setVoiceAudio,
  toggleActivity, openActivityToTab, openActivityPanel, openActivityToTool, clearFocusToolCallId, requestSlotReveal, clearSlotReveal, clearSubagentsForSnapshot, sseSubagentPending, markSubagentApproving, sseSubagentSpawn, sseSubagentChunk, sseSubagentTool, sseSubagentStalled, sseSubagentRetrying, sseSubagentDone, sseSubagentQueued,
  sseSubagentBatchUpdate, sseSubagentBatchChunks, selectSubagent, clearTerminalSubagents,
  setGoalLoops, sseGoalLoop,
  sseSubagentSnapshot, sseToolActivity, sseToolResult, sseActivityEvent,
  sseMcpAppRender,
  sseWorkflowEvent, clearWorkflowRun,
  sseSideResult, sseSideQueue, sideReleaseConsumed, sideClose, sideOptimisticAppend, sideOptimisticRollback,
} = chatSlice.actions
export default chatSlice.reducer

/** Sub-agent cards per slot: the lifecycle and incremental progress frames,
 *  the queued-wait count and its reason, and the selectors that count running
 *  and approval-pending agents for the composer chip, the sidebar and the
 *  Sessions rail. The active slot's map is aliased into its `slotActivity`
 *  bucket, so every aggregate skips the active key there. */
import { createSelector, type PayloadAction } from '@reduxjs/toolkit'
import type { RootState } from '../index'
import type { SubagentActivity, SubagentInfo } from '../../types'
import { parseSubagentQueuedReason, type SubagentQueuedEvent } from '../../pages/chat/subagentQueuedReason'
import { i18nT } from '../../i18n/t'
import type { ChatState } from './state'
import { isUnsafeKey, safeKey } from './wire'
import { jsonEqual } from '../../utils/structuralEqual'

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

/** Get subagents map for a slot (read-only lookup) */
function getSlotSubs(state: ChatState, slot: string) {
  return slot !== state.activeSlot ? state.slotActivity[slot]?.subagents : state.subagents
}

/** A replay item named this card: `subagentReplayDone` keeps it. */
function confirmReplayed(state: ChatState, id: string) {
  if (state.subagentReplayUnconfirmed?.[id]) delete state.subagentReplayUnconfirmed[id]
}

/** `a` without its undefined fields: a reducer that spells an absent field as
 *  `undefined` and one that leaves it out describe the same card. */
function withoutUndefined(a: SubagentActivity): Partial<SubagentActivity> {
  return Object.fromEntries(Object.entries(a).filter(([, v]) => v !== undefined))
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

/** `getSlotSub` for the reducers that must not LOSE a frame: it creates the
 *  entry when the wire names an agent this store holds none for, then returns it
 *  to be mutated.
 *
 *  A read-only accessor is the right shape for a reducer whose frame only
 *  decorates a card (an approval toggle has nothing to say about an agent it
 *  cannot find). It is the wrong shape for the incremental lifecycle frames --
 *  tool, streaming text, stalled, retrying -- because those are the ONLY
 *  evidence the panel gets between one spawn frame and one done frame. Dropping
 *  them when the container is missing makes the agent invisible for its whole
 *  run and then complete out of nowhere, and the gap is reachable in normal use:
 *  the subscription starts before the replay is sent, so a tab that connects
 *  while an agent runs can receive its incremental frames before the replay
 *  item that would have created its card.
 *
 *  A created entry is deliberately a MINIMUM: the frame that reaches here
 *  carries no task text or agent name, so those stay empty and a later frame
 *  that does carry them (`subagent_done`, a snapshot replay) fills them in. A
 *  card reading "running, last tool X" with no title is worth more to the
 *  operator than no card at all, which is the alternative.
 *
 *  Guards mirror the lifecycle reducers exactly, because this one WRITES:
 *  `isUnsafeKey` refuses a poisoned slot or id outright, and `safeKey` reroutes
 *  one to an inert own-property if it ever slips past. A hostile
 *  `__proto__`/`constructor`/`prototype` id therefore creates nothing and
 *  returns `undefined`, so the frame is dropped exactly as before. */
function upsertSlotSub(state: ChatState, slot: string, id: string): SubagentActivity | undefined {
  if (isUnsafeKey(slot) || isUnsafeKey(id)) return undefined
  const existing = getSlotSub(state, slot, id)
  if (existing) return existing
  // An OWNERLESS frame must not mint a bucket. `isUnsafeKey` does not cover this:
  // `isUnsafeKey('')` is false, and a degraded spawn (`parent_session_key: ''`)
  // reaches here carrying `slot: ''`. Creating `slotActivity['']` would be a
  // session bucket no session owns, which the global activity view then reports
  // as an owned running agent, and nothing later removes it -- a snapshot replay
  // refuses the same input, so it never overwrites the bucket.
  // :func:`sseSubagentSnapshot` fails closed on the same condition.
  //
  // Scoped to the bucket branch on purpose. A frame whose slot IS the active one
  // mints nothing: the write lands in `state.subagents`, which is where the
  // lifecycle reducers put it too, so refusing that path would change behaviour
  // this function did not introduce (the store's default `activeSlot` is `null`,
  // and frames carrying that same value legitimately target the active map).
  let subs: Record<string, SubagentActivity>
  if (slot !== state.activeSlot) {
    if (!slot) return undefined
    subs = (state.slotActivity[safeKey(slot)] ??= { toolLog: [], subagents: {} }).subagents
  } else {
    subs = state.subagents
  }
  return (subs[safeKey(id)] ??= {
    id, task: '', agent: '',
    status: 'running', streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
    // The frame that reached here carries no start time, so this instant is an
    // assumption and is flagged as one rather than rendered as fact.
    startedAtAssumed: true,
  })
}

/** Most tool calls a card keeps in its timeline (#13628). Older calls fall off
 *  the front; the card states how many it is not listing, from `toolCount`. */
export const SUBAGENT_TOOL_CALLS_CAP = 50

/** Record one `subagent_tool` frame in the card's tool-call timeline (#13628).
 *
 *  `lastTool` alone is overwritten by every frame, so a long run showed one
 *  tool name and nothing else. This keeps the recent calls in order.
 *
 *  One gated call sends TWO frames: the tool_call update (which bumps
 *  `tool_count`) and then its permission request. Both carry the call's
 *  `tool_call_id`, so a frame naming the newest entry's id updates that entry
 *  instead of listing the call again, and a frame with a DIFFERENT id is a new
 *  call even when `tool_count` did not move -- a backend child's permission
 *  request is counted only at approval, so its count stays put. A frame with no
 *  id (an older gateway) falls back to the count: an unmoved count is the
 *  second frame of the newest call. A frame with neither id nor count (native
 *  kiro-cli cards, which `_native_subagent_sync` re-sends on every
 *  `list_update`) is not listed again when its title equals the newest entry.
 *
 *  Above the coalescing threshold the backend merges frames (latest wins), so
 *  intermediate calls never reach the client; `toolCount` still counts them,
 *  and the card reports the difference rather than implying a complete list.
 *  Reconnect replay runs this same rule on a copied timeline, while
 *  `clearSubagentsForSnapshot` keeps the running card's held timeline.
 *  Must run BEFORE the caller stores the frame's `tool_count`. */
function recordToolCall(a: SubagentActivity, tool: string, toolCount: number | undefined, callId: string | undefined) {
  const calls = (a.toolCalls ??= [])
  const newest = calls[calls.length - 1]
  const id = callId || undefined
  let same: boolean
  if (id && newest?.id) same = newest.id === id
  // A replay from an older gateway can seed its one row without an id, so an
  // id-bearing frame after it is a call of its own: the count cannot tell it
  // apart, because a child's permission request does not move `tool_count`.
  else if (id && newest) same = false
  else if (typeof toolCount === 'number') same = !!newest && toolCount === a.toolCount
  else same = newest?.tool === tool
  if (same && newest) {
    newest.tool = tool
    if (id) newest.id = id
    return
  }
  calls.push(id ? { tool, id } : { tool })
  if (calls.length > SUBAGENT_TOOL_CALLS_CAP) calls.splice(0, calls.length - SUBAGENT_TOOL_CALLS_CAP)
}

/** A replayed snapshot is older than the held card when it carries a lower
 *  tool count: every live `tool_call` frame raises the count. A call id is no
 *  test of age, because a permission frame repeats an earlier call's id
 *  without moving the count, so one id can sit at several timeline rows. */
function isStaleSnapshot(a: SubagentActivity, toolCount: number | undefined) {
  return typeof toolCount === 'number' && typeof a.toolCount === 'number' && toolCount < a.toolCount
}

/**
 * The decision identified by this pending spawn is no longer actionable.
 * `approvalGone` describes the PENDING phase only: a real `subagent_spawn` or
 * tool frame moves the card to running/tool without clearing it, and a stale
 * marker must not keep a live agent classified as unresolved.
 */
export const isSpawnApprovalGone = (a: SubagentActivity) =>
  a.status === 'pending' && !!a.approval_id && a.approvalGone === a.approval_id

/** Reconciliation found no process for a gone approval and retired the card.
 *  Terminal-only, for the same reason: a later tool frame proves a launch. */
export const isSpawnApprovalRetired = (a: SubagentActivity) =>
  a.status === 'stopped' && !!a.approvalRetired

/**
 * Shared process-liveness semantics for busy state, rail/sidebar counts, and
 * renderers. A terminal approval refusal settles ACTIONABILITY, not liveness:
 * another surface may already have approved and launched the agent. Keep that
 * short unresolved window conservative until the authoritative spawn list
 * converts the card to running/terminal.
 */
export const isActiveSubagent = (a: SubagentActivity) =>
  a.status === 'running' || a.status === 'tool' || a.status === 'pending'

/**
 * Live "sub-agents running" signal for a slot, derived from the
 * subagent_spawn/tool/done WS events (the only real-time source — see the
 * ChatSidebar countActive note: dashboardSlice fields only refresh on a full
 * slots push). Uses the same active predicate as every aggregate count.
 */
export const selectSlotSubagentsActive = (state: RootState, slot: string): boolean => {
  const subs = getSlotSubs(state.chat, slot)
  if (!subs) return false
  for (const a of Object.values(subs)) {
    if (isActiveSubagent(a)) return true
  }
  return false
}

// Shared subagent-counting helpers — single implementations for both sidebar and aggregate selectors.

/** Counts active subagents in a subagent map. */
const countActiveSubagents = (m?: Record<string, SubagentActivity>) => {
  if (!m) return 0
  let n = 0
  for (const a of Object.values(m)) {
    if (isActiveSubagent(a)) n++
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
 *
 * A gone approval (`markSubagentApprovalGone`) is excluded: once a resolve has
 * proved it decided or expired, no surface can take the decision, so none may
 * say the user owes it. That verdict does not prove whether another surface
 * launched the run, so liveness readers keep it conservative until the spawn
 * inventory reconciles it.
 */
export const isAwaitingSpawnApproval = (a: SubagentActivity) =>
  a.status === 'pending' && !!a.approval_id && !isSpawnApprovalGone(a)

/**
 * Canonical terminal classification of a finished run: `outcome` is the single
 * source (spec: docs/system-specs/modules/subagent.md). The `stopped`/`error`
 * derivation is kept ONLY as a fallback for records that predate the field
 * (reconnect replays from a pre-upgrade gateway).
 */
const terminalStatus = (r: { outcome?: 'completed' | 'failed' | 'stopped'; stopped?: boolean; error?: string }): 'stopped' | 'error' | 'done' =>
  r.outcome === 'stopped' ? 'stopped'
    : r.outcome === 'failed' ? 'error'
      : r.outcome === 'completed' ? 'done'
        : r.stopped ? 'stopped' : (r.error ? 'error' : 'done')

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

function sameRecordValues(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const ka = Object.keys(a)
  if (ka.length !== Object.keys(b).length) return false
  return ka.every(k => Object.prototype.hasOwnProperty.call(b, k) && a[k] === b[k])
}

/** Each background slot's `subagents` map, keyed by slot. `slotActivity`
 *  changes identity on every background tool row (its `toolLog`), while the
 *  `subagents` maps inside it keep theirs, so this keeps returning the same
 *  object until one of those maps is replaced. The count selectors below read
 *  it instead of `slotActivity`, so a tool row does not recount them. */
export const selectSubagentsBySlot = createSelector(
  [(state: RootState) => state.chat.slotActivity],
  (slotActivity): Record<string, Record<string, SubagentActivity>> => {
    const out: Record<string, Record<string, SubagentActivity>> = {}
    for (const [slot, act] of Object.entries(slotActivity ?? {})) out[slot] = act.subagents
    return out
  },
  { memoizeOptions: { resultEqualityCheck: sameRecordValues } },
)

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
    selectSubagentsBySlot,
    (state: RootState) => state.chat.subagentQueued,
  ],
  (activeSlot, activeSubs, bySlot, queued) => {
    let total = activeSlot ? countActiveSubagents(activeSubs) : 0
    for (const [slot, subs] of Object.entries(bySlot)) {
      // On switchSlot the active slot's map is aliased into both
      // state.subagents and slotActivity[active].subagents (same reference),
      // so this guard is what prevents double-counting it.
      if (slot === activeSlot) continue
      total += countActiveSubagents(subs)
    }
    for (const q of Object.values(queued ?? {})) total += q > 0 ? q : 0
    return total
  },
)

/** Per-slot subagent counts for sidebar. Reuses shared counting helpers above. */

/** STARTED sub-agents per slot (running + tool + pending), never the queued
 *  count. This is the reading for anything that says a session is working: a
 *  child that has not started holds no process and makes no progress, so a
 *  queued-only parent whose own turn has ended is waiting, not working (the
 *  board's Working lane, and the row's "live work" checks). */
export const selectSidebarStartedSubagentCounts = createSelector(
  [
    (state: RootState) => state.chat.activeSlot,
    (state: RootState) => state.chat.subagents,
    selectSubagentsBySlot,
  ],
  (activeSlot, activeSubs, bySlot) => {
    const counts: Record<string, number> = {}
    if (activeSlot) {
      const n = countActiveSubagents(activeSubs)
      if (n > 0) counts[activeSlot] = n
    }
    for (const [slot, subs] of Object.entries(bySlot)) {
      // Load-bearing: active slot's map is aliased in both places; skip to avoid double-count.
      if (slot === activeSlot) continue
      const n = countActiveSubagents(subs)
      if (n > 0) counts[slot] = n
    }
    return counts
  },
)

/** Every sub-agent a slot has in flight: started (above) plus accepted-but-
 *  queued. The reading for "does this session still own sub-agent work" — the
 *  row's count label, the turn-done chime, stale collapse — never for Working. */
export const selectSidebarSubagentCounts = createSelector(
  [
    selectSidebarStartedSubagentCounts,
    (state: RootState) => state.chat.subagentQueued,
  ],
  (started, queued) => {
    const counts: Record<string, number> = { ...started }
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
    selectSubagentsBySlot,
  ],
  (activeSlot, activeSubs, bySlot) => {
    const approvalCounts: Record<string, number> = {}
    if (activeSlot) {
      const p = countPendingApprovals(activeSubs)
      if (p > 0) approvalCounts[activeSlot] = p
    }
    for (const [slot, subs] of Object.entries(bySlot)) {
      // Same aliasing guard as countActive above: the active slot's map is the
      // same object in both places, so skipping it here avoids double-counting.
      if (slot === activeSlot) continue
      const p = countPendingApprovals(subs)
      if (p > 0) approvalCounts[slot] = p
    }
    return approvalCounts
  },
)

export const subagentReducers = {
  /** Start reconciling the held subagent view against the replay the gateway
   *  sends after `subscribe_subagents`. Every card except a pending spawn
   *  approval is marked unconfirmed rather than dropped: the replay's snapshot
   *  and done items reconcile into the cards they name, so a reconnect that
   *  replays what the tab already shows changes no card, and a running card
   *  keeps its tool-call timeline. `subagentReplayDone`, sent after the last
   *  replay item, drops the cards still unconfirmed, so an empty replay from a
   *  restarted gateway still clears agents it no longer knows.
   *
   *  Pending spawn-approval cards are never marked: the replay only re-emits
   *  native + managed running/done agents, so a card still awaiting approval
   *  has no backend SubagentInfo to confirm it and would be lost (its
   *  approve/reject UI along with it) on a mid-approval reconnect. */
  clearSubagentsForSnapshot(state: ChatState) {
    const unconfirmed: Record<string, true> = { ...(state.subagentReplayUnconfirmed ?? {}) }
    const mark = (subs: Record<string, SubagentActivity> | undefined) => {
      if (!subs) return
      for (const [id, a] of Object.entries(subs)) {
        if (a.status === 'pending') continue
        unconfirmed[id] = true
        // The replay carries no retry state, and a recovery the old socket
        // missed would otherwise leave "retrying" on the card. A retry frame
        // on the new connection, which arrives after this, sets it again.
        if (a.retrying) a.retrying = false
      }
    }
    mark(state.subagents)
    for (const activity of Object.values(state.slotActivity)) mark(activity.subagents)
    state.subagentReplayUnconfirmed = unconfirmed
    // Queued counts are advisory and re-emitted on the next drain — reset to
    // avoid showing a stale "waiting" count for a wave that finished during
    // the disconnect (under-count self-heals on the next drain frame). Written
    // only when non-empty, so an idle reconnect keeps both maps' identity.
    if (Object.keys(state.subagentQueued ?? {}).length) state.subagentQueued = {}
    if (Object.keys(state.subagentQueuedReason ?? {}).length) state.subagentQueuedReason = {}
  },
  /** The gateway's `subagent_replay_done`: drop every card the replay did not
   *  name (see `clearSubagentsForSnapshot`). A marker with no replay in
   *  progress (a panel subscribe on a live socket) changes nothing. */
  subagentReplayDone(state: ChatState) {
    const unconfirmed = state.subagentReplayUnconfirmed
    if (!unconfirmed) return
    state.subagentReplayUnconfirmed = null
    const sweep = (subs: Record<string, SubagentActivity>) => {
      for (const id of Object.keys(subs)) {
        if (unconfirmed[id] && subs[id].status !== 'pending') delete subs[id]
      }
    }
    sweep(state.subagents)
    for (const activity of Object.values(state.slotActivity)) sweep(activity.subagents)
  },
  /** Aggregate "waiting to start" count for a slot. Agents queued behind the
   *  concurrency cap / stagger gate have no individual card; this count lets
   *  the chip appear immediately on spawn and show how many are pending
   *  start (issues: late chip, flicker, invisible queue). */
  sseSubagentQueued(state: ChatState, action: PayloadAction<SubagentQueuedEvent>) {
    if (isUnsafeKey(action.payload.slot)) return
    const n = Math.max(0, Math.floor(Number(action.payload.queued) || 0))
    // Tolerate a store built from partial preloaded state (test fixtures and
    // any consumer that predates this key): indexing an absent map throws and
    // would drop the queue update entirely.
    state.subagentQueued ??= {}
    state.subagentQueuedReason ??= {}
    const key = safeKey(action.payload.slot)
    if (n === 0) {
      delete state.subagentQueued[key]
      delete state.subagentQueuedReason[key]
      return
    }
    state.subagentQueued[key] = n
    // The reason travels with the count it explains. A frame without one is
    // either an older gateway or a wait nothing labelled: the default text.
    const reason = parseSubagentQueuedReason(action.payload)
    if (reason) state.subagentQueuedReason[key] = reason
    else delete state.subagentQueuedReason[key]
  },
  /** Reconcile the queued counts with a `slots` push or a `slot_patch`. Each
   *  full-list row, and each sub-agent patch row, carries the depth the gateway
   *  last published for its session (`subagents_queued`), so a count a missed or
   *  misapplied `subagent_queued` frame left behind is corrected by the next
   *  push, for every slot it names and not only the one on screen. A row without
   *  the field (a metadata `slot_patch` row, an older gateway) leaves its count
   *  alone. The wait label is kept while rows still wait: the
   *  push carries the count, and the label stays the one the frames gave it. */
  reconcileSubagentQueuedFromSlots(state: ChatState, action: PayloadAction<ReadonlyArray<{ key: string; subagents_queued?: unknown }>>) {
    state.subagentQueued ??= {}
    state.subagentQueuedReason ??= {}
    for (const row of action.payload) {
      const published = row.subagents_queued
      if (typeof published !== 'number' || !Number.isFinite(published) || isUnsafeKey(row.key)) continue
      const key = safeKey(row.key)
      const n = Math.max(0, Math.floor(published))
      if (n === 0) {
        delete state.subagentQueued[key]
        delete state.subagentQueuedReason[key]
      } else if (state.subagentQueued[key] !== n) {
        state.subagentQueued[key] = n
      }
    }
  },
  sseSubagentPending(state: ChatState, action: PayloadAction<{ slot: string; id: string; task: string; approval_id: string }>) {
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
  markSubagentApproving(state: ChatState, action: PayloadAction<{ id: string; approving: boolean }>) {
    if (isUnsafeKey(action.payload.id)) return
    const a = state.subagents[action.payload.id]
    if (a) { a.approving = action.payload.approving; return }
    for (const sa of Object.values(state.slotActivity)) {
      const b = sa.subagents[action.payload.id]
      if (b) { b.approving = action.payload.approving; return }
    }
  },
  /** One owner for a terminal refusal, so the side panel and the composer
   *  banner, which resolve the same id, both withdraw it (#11180). */
  markSubagentApprovalGone(state: ChatState, action: PayloadAction<{ id: string; approval_id: string }>) {
    if (isUnsafeKey(action.payload.id)) return
    const mark = (a: SubagentActivity | undefined) => {
      if (!a) return false
      // Only the approval that was refused: a newer one on this card stays live.
      if (a.approval_id === action.payload.approval_id) {
        a.approving = false
        a.approvalGone = action.payload.approval_id
      }
      return true
    }
    if (mark(state.subagents[action.payload.id])) return
    for (const sa of Object.values(state.slotActivity)) {
      if (mark(sa.subagents[action.payload.id])) return
    }
  },
  /**
   * Apply the authoritative spawn inventory after a terminal approval
   * refusal. Compare all three identities before mutating: a spawn frame or a
   * fresh approval id may have arrived while the GET was in flight.
   */
  reconcileSubagentApprovalGone(state: ChatState, action: PayloadAction<{
    slot: string
    id: string
    approval_id: string
    agent: SubagentInfo | null
  }>) {
    const { slot, id, approval_id, agent } = action.payload
    if (isUnsafeKey(slot) || isUnsafeKey(id)) return
    const current = getSlotSub(state, slot, id)
    if (current?.status !== 'pending'
      || current.approval_id !== approval_id
      || current.approvalGone !== approval_id) return

    // Matched by run id alone: ids are unique, and a row's `parent` is not the
    // tab key for a nested, cron- or channel-born run.
    if (agent && agent.id === id) {
      // The approval future can disappear just before the manager publishes
      // the terminal outcome. Its inventory row explicitly says it is still
      // parked and therefore is not proof of a launched process; leave the
      // conservative unresolved state for the next reconciliation tick.
      if (agent.awaiting_approval && !agent.done) return
      current.approving = false
      current.approvalGone = undefined
      current.approvalRetired = undefined
      current.approval_id = undefined
      if (agent.task) current.task = agent.task
      if (agent.agent) current.agent = agent.agent
      if (typeof agent.started === 'number' && agent.started > 0) {
        current.startedAt = agent.started * 1000
        current.startedAtAssumed = undefined
        // A finished row carries no elapsed of its own, and "now - started" is
        // wall time since registration, not the run's duration.
        current.elapsed = agent.done ? 0 : Math.max(0, Math.round(Date.now() / 1000 - agent.started))
      }
      current.lastTool = agent.last_tool || ''
      if (agent.done) {
        current.status = terminalStatus(agent)
        current.error = agent.error || undefined
      } else {
        current.status = agent.last_tool ? 'tool' : 'running'
        current.error = undefined
      }
      return
    }

    // No backend record with this id: it never launched, or it is no longer
    // retained, and the card claims only the absent record. Retire the card
    // instead of preserving a reconnect-proof pending zombie. Keep the scoped
    // approval verdict so no surface re-offers the dead decision; a later
    // sseSubagentPending overwrites the whole entry and re-arms a fresh id.
    current.status = 'stopped'
    current.approving = false
    current.approvalRetired = true
    current.elapsed = 0
  },
  sseSubagentSpawn(state: ChatState, action: PayloadAction<{ slot: string; id: string; task: string; agent: string; label?: string; model?: string; requested_model?: string; child_session?: string; batch_id?: string }>) {
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
      // Same guard for the wave id: only set when the frame names a wave, so a
      // later frame that omits it cannot blank a known batch.
      if (action.payload.batch_id) existing.batchId = action.payload.batch_id
      // The spawn event carries the authoritative task text (the pending
      // card's task is derived from the approval title, which may be empty
      // or just "spawn_run") — always prefer the spawn payload's task.
      if (action.payload.task) existing.task = action.payload.task
      if (action.payload.label) existing.label = action.payload.label
      return
    }
    subs[safeKey(action.payload.id)] = {
      id: action.payload.id, task: action.payload.task, agent: action.payload.agent || 'kirocrew',
      label: action.payload.label || existing?.label || undefined,
      model: action.payload.model || '',
      requestedModel: action.payload.requested_model || existing?.requestedModel || undefined,
      childSession: action.payload.child_session || undefined,
      batchId: action.payload.batch_id || existing?.batchId || undefined,
      status: 'running', streaming: existing?.streaming || '', lastTool: '', startedAt: existing?.startedAt || Date.now(), elapsed: 0,
      // Reusing an entry's start time inherits whether that time was ASSUMED.
      // Rebuilding the entry without this would silently promote an assumption
      // to an assertion, because a spawn frame carries no start time of its own
      // -- `Date.now()` is only genuine for an entry being created here.
      startedAtAssumed: existing?.startedAt ? existing.startedAtAssumed : undefined,
      toolCount: 0, stalled: false,
    }
  },
  sseSubagentTool(state: ChatState, action: PayloadAction<{ slot: string; id: string; tool: string; turns?: number; tool_count?: number; tool_call_id?: string }>) {
    const { slot, id } = action.payload
    // Prototype-pollution guard is centralized in upsertSlotSub, which also
    // creates the entry when this is the first frame naming the agent.
    const a = upsertSlotSub(state, slot, id)
    if (a) {
      if (action.payload.tool) recordToolCall(a, action.payload.tool, action.payload.tool_count, action.payload.tool_call_id)
      a.lastTool = action.payload.tool; a.status = 'tool'
      // A tool frame proves the run launched: a retired verdict must not
      // resurrect when the run later ends `stopped`.
      a.approvalRetired = undefined
      if (typeof action.payload.tool_count === 'number') a.toolCount = action.payload.tool_count
      a.stalled = false
      a.idleSecs = undefined
      a.stalledAt = undefined
      a.retrying = false
    }
  },
  sseSubagentRetrying(state: ChatState, action: PayloadAction<{ slot: string; id: string; attempt?: number }>) {
    // Fired for both transient-backend retries (subagent_retrying) and the
    // one-shot cancel auto-continue (subagent_recovering): the agent is
    // still alive and recovering — show ⟳ instead of letting it look hung.
    const { slot, id } = action.payload
    // Through the shared accessor, so this call site carries no hand-written
    // copy of the poisoned-key list that could drift from `isUnsafeKey`.
    const a = upsertSlotSub(state, slot, id)
    if (a) { a.retrying = true; a.stalled = false; a.idleSecs = undefined; a.stalledAt = undefined }
  },
  sseSubagentStalled(state: ChatState, action: PayloadAction<{ slot: string; id: string; stalled: boolean; idle_secs?: number }>) {
    const { slot, id } = action.payload
    // Prototype-pollution guard is centralized in upsertSlotSub, which also
    // creates the entry when this is the first frame naming the agent.
    const a = upsertSlotSub(state, slot, id)
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
  sseSubagentBatchUpdate(state: ChatState, action: PayloadAction<{ updates: { id: string; slot: string; tool?: string; tool_count?: number; tool_call_id?: string; stalled?: boolean; idle_secs?: number; attempt?: number }[] }>) {
    for (const u of action.payload.updates || []) {
      const a = upsertSlotSub(state, u.slot, u.id)
      if (!a) continue
      // Order matters: retrying (attempt) applies FIRST so a tool field in
      // the same merged entry — meaning work resumed — clears it last.
      if (typeof u.attempt === 'number') { a.retrying = true; a.stalled = false; a.idleSecs = undefined; a.stalledAt = undefined }
      if (typeof u.tool === 'string' && u.tool) { recordToolCall(a, u.tool, u.tool_count, u.tool_call_id); a.lastTool = u.tool; if (a.status === 'running') a.status = 'tool'; a.retrying = false }
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
  sseSubagentBatchChunks(state: ChatState, action: PayloadAction<{ chunks: { id: string; slot: string; text: string }[] }>) {
    for (const c of action.payload.chunks || []) {
      const a = upsertSlotSub(state, c.slot, c.id)
      if (!a) continue
      a.retrying = false
      a.streaming += c.text
      if (a.streaming.length > 50_000) {
        a.streaming = i18nT('store.chatSlice.truncated') + '\n' + a.streaming.slice(-40_000)
      }
    }
  },
  /** Chip row click → the Activity tab scrolls to/expands this agent. */
  selectSubagent(state: ChatState, action: PayloadAction<string | null>) {
    state.selectedSubagentId = action.payload
  },
  /** "Dismiss done": drop terminal cards for a slot (backend clear is the
   *  caller's job via per-id DELETE /api/spawn/{id}; this trims the local view). */
  clearTerminalSubagents(state: ChatState, action: PayloadAction<{ slot: string }>) {
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
  sseSubagentDone(state: ChatState, action: PayloadAction<{ slot: string; id: string; elapsed: number; credits?: number; error?: string; stopped?: boolean; outcome?: 'completed' | 'failed' | 'stopped'; task?: string; agent?: string; label?: string; model?: string; requested_model?: string; child_session?: string; batch_id?: string; result?: string }>) {
    if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
    confirmReplayed(state, action.payload.id)
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
    const credits = typeof action.payload.credits === 'number' && Number.isFinite(action.payload.credits)
      ? Math.max(0, action.payload.credits)
      : undefined
    const doneStatus = terminalStatus(action.payload)
    if (a) {
      // Every field below is written only when it moves: a reconnect replays
      // the done item of every card that already shows it, and an unchanged
      // card must keep its object.
      a.status = doneStatus
      if (a.retrying !== false) a.retrying = false
      a.elapsed = action.payload.elapsed
      if (credits !== undefined) a.credits = credits
      const error = doneStatus === 'stopped' ? undefined : action.payload.error
      // The wire sends no error as null; the card may hold it as undefined.
      if ((a.error ?? undefined) !== (error ?? undefined)) a.error = error
      a.streaming = ''
      if (action.payload.task && !a.task) a.task = action.payload.task
      if (action.payload.agent && !a.agent) a.agent = action.payload.agent
      // The done frame carries the authoritative served model (the CC path
      // has resolved it by completion). Prefer a known value, but never
      // clobber a prior known id back to '' if this frame omits it.
      if (action.payload.model) a.model = action.payload.model
      // Carry the requested pin so a card that subagent_done rebuilds (one a
      // tab opened after the run ended) keeps the live-downgrade amber chip.
      // Never clobber a known value to ''.
      if (action.payload.requested_model) a.requestedModel = action.payload.requested_model
      if (action.payload.child_session && !a.childSession) a.childSession = action.payload.child_session
      // Carry the wave id so a reconnect that rehydrates a completed card keeps
      // its batch chip. Never clobber a known value to ''.
      if (action.payload.batch_id && !a.batchId) a.batchId = action.payload.batch_id
      if (action.payload.label && !a.label) a.label = action.payload.label
      if (isNative && action.payload.result !== undefined) a.result = action.payload.result
      // A done frame carries authoritative `elapsed`, which reconstructs the
      // real start for an entry whose start was only ASSUMED -- the same
      // reconstruction the no-entry branch below already performs. Without it
      // the entry would keep claiming its start is unknown after the one frame
      // that settles it.
      if (a.startedAtAssumed) {
        a.startedAt = Date.now() - action.payload.elapsed * 1000
        a.startedAtAssumed = undefined
      }
    }
    else {
      subs[action.payload.id] = {
        id: action.payload.id,
        task: action.payload.task || '',
        agent: action.payload.agent || 'kirocrew',
        label: action.payload.label || undefined,
        model: action.payload.model || '',
        requestedModel: action.payload.requested_model || undefined,
        childSession: action.payload.child_session || undefined,
        batchId: action.payload.batch_id || undefined,
        status: doneStatus,
        // Spelled out as the branch above leaves it, so a replay of this done
        // item finds nothing to change.
        retrying: false,
        streaming: '',
        lastTool: '',
        startedAt: Date.now() - action.payload.elapsed * 1000,
        elapsed: action.payload.elapsed,
        credits,
        error: doneStatus === 'stopped' ? undefined : action.payload.error,
        result: isNative ? action.payload.result : undefined,
      }
    }
  },
  sseSubagentSnapshot(state: ChatState, action: PayloadAction<{ id: string; slot: string; task: string; agent: string; label?: string; model?: string; requested_model?: string; child_session?: string; batch_id?: string; streaming: string; last_tool: string; last_tool_call_id?: string; started: number; tool_count?: number; stalled?: boolean; idle_secs?: number }>) {
    const d = action.payload
    // A snapshot without an owning slot is an orphan, not evidence that it
    // belongs to whichever chat this browser happens to show. Popout windows
    // cold-subscribe to the complete replay after activating their own slot;
    // treating `slot: ''` as the active map made every such window adopt all
    // unresolved-parent agents. Fail closed: ownerless runs remain available
    // through the global spawn inventory, but never appear inside a chat.
    if (!d.slot || isUnsafeKey(d.slot) || isUnsafeKey(d.id)) return
    confirmReplayed(state, d.id)
    const subs = d.slot !== state.activeSlot
      ? (state.slotActivity[safeKey(d.slot)] ??= { toolLog: [], subagents: {} }).subagents
      : state.subagents
    const existing = subs[d.id]
    // Live events can interleave with replay because subscription starts before
    // snapshots are sent. Never let a stale running snapshot demote a terminal card.
    if (existing?.status === 'done' || existing?.status === 'error') return
    // For the same reason, a snapshot collected before a live tool frame the
    // card already applied carries a lower tool count. Its tool and progress
    // fields are older than the card's, so it only fills the identity a live
    // frame cannot carry (a card a live frame created has an empty task and an
    // assumed start time).
    if (existing && isStaleSnapshot(existing, d.tool_count)) {
      const hydrated: SubagentActivity = {
        ...existing,
        task: d.task || existing.task, agent: d.agent || existing.agent || 'kirocrew',
        label: d.label || existing.label, model: d.model || existing.model,
        requestedModel: d.requested_model || existing.requestedModel,
        childSession: d.child_session || existing.childSession,
        batchId: d.batch_id || existing.batchId,
        startedAt: d.started * 1000, startedAtAssumed: undefined,
      }
      if (!jsonEqual(withoutUndefined(existing), withoutUndefined(hydrated))) subs[safeKey(d.id)] = hydrated
      return
    }
    const stalled = d.stalled ?? false
    // A replay that repeats an unchanged stall keeps the instant the card
    // first saw it, so the advancing idle figure does not jump back.
    const sameStall = stalled && existing?.stalled === true && existing.idleSecs === d.idle_secs
    let replayedToolCalls = existing?.toolCalls
    if (replayedToolCalls?.length && d.last_tool) {
      const scratch = {
        toolCalls: replayedToolCalls.map(call => ({ ...call })),
        toolCount: existing.toolCount,
      } as SubagentActivity
      recordToolCall(scratch, d.last_tool, d.tool_count, d.last_tool_call_id)
      replayedToolCalls = jsonEqual(scratch.toolCalls, existing.toolCalls)
        ? existing.toolCalls
        : scratch.toolCalls
    }
    const next: SubagentActivity = {
      id: d.id, task: d.task, agent: d.agent || 'kirocrew',
      label: d.label || existing?.label || undefined,
      // Prefer the snapshot's model; fall back to any id a live frame already
      // set, so a reconnect that omits it does not blank the pill.
      model: d.model || existing?.model || '',
      // Same guard for requestedModel: prefer frame value, fall back to existing.
      requestedModel: d.requested_model || existing?.requestedModel || undefined,
      childSession: d.child_session || existing?.childSession || undefined,
      batchId: d.batch_id || existing?.batchId || undefined,
      status: d.last_tool ? 'tool' : 'running', streaming: d.streaming, lastTool: d.last_tool,
      // Replay applies the live tool-frame rule to a copy of a held timeline.
      // A card the replay creates seeds its timeline from the latest tool.
      toolCalls: existing?.toolCalls?.length
        ? replayedToolCalls
        : (d.last_tool
            ? [d.last_tool_call_id ? { tool: d.last_tool, id: d.last_tool_call_id } : { tool: d.last_tool }]
            : undefined),
      startedAt: d.started * 1000, elapsed: 0,
      toolCount: d.tool_count ?? 0, stalled,
      // Same pairing rule as sseSubagentStalled: the idle span lives and dies
      // with the flag it justifies, so a non-stalled snapshot can never carry
      // one. `stalledAt` is the receipt instant — the row advances the figure
      // from here rather than freezing it beside a live elapsed counter.
      // Both stay undefined when the gateway omits `idle_secs`, which keeps
      // the plain "no activity" fallback reachable for an older gateway.
      idleSecs: stalled ? d.idle_secs : undefined,
      stalledAt: stalled && typeof d.idle_secs === 'number' ? (sameStall ? existing.stalledAt : Date.now()) : undefined,
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
    // A reconnect replays every running card. One that repeats what the card
    // holds keeps the held object, so its card and every whole-map reader
    // stay as they were.
    if (existing && jsonEqual(withoutUndefined(existing), withoutUndefined(next))) return
    subs[safeKey(d.id)] = next
  },
}

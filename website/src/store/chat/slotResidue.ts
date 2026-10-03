/** Teardown of slots that left the authoritative list: which state maps are
 *  keyed per slot, the reconcile both authoritative writers (`sseSlots`,
 *  `fetchSlots.fulfilled`) run through, and the per-key eviction a
 *  `sseSlotPatch` frame applies to the keys it names as removed. */
import type { ActionReducerMapBuilder } from '@reduxjs/toolkit'
import { fetchSlots, sseConnected, sseSlotPatch, sseSlots } from '../dashboardSlice'
import type { ChatState } from './state'
import { safeKey } from './wire'
import { MCP_APP_KEY_SEP, evictMcpApps } from './mcpApps'
import { clearFiledFolderSuggestions } from './composerCards'

/** Chat state keyed by a slot.
 *
 *  Single owner of what is keyed per slot, read by every teardown path, so a new
 *  per-slot map registered here is reached by all of them. Request-keyed refresh
 *  bookkeeping is handled separately below because its values carry slot keys.
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
  state.pendingQuestions, state.subagentQueued, state.subagentQueuedReason,
  state.automations,
  // A surviving pane marker makes a recreated slot's hydrate early-return into
  // nothing, so these must die with the transcript they describe. The retained
  // server count belongs with them: kept past an eviction it would read as a
  // fall against a recreated slot's first fetch and drop a legitimate tail.
  state.slotPaneHasMore, state.slotPaneBounded, state.slotServerTotal,
  state.slotServerTotalSeq, state.refreshIssuedSeq, state.refreshAppliedSeq,
  state.thinkingOrphans,
].filter(Boolean)

/** Every slot key that still has residue anywhere in chat state.
 *
 *  A reconcile can only evict a slot it visits, so this has to cover the same
 *  ephemeral surfaces `evictSlotState` clears — including the ones that are not
 *  plain slot-keyed maps: `refreshIssueByRequest`, whose values carry the slot;
 *  `mcpApps`, whose keys carry it as a prefix; and `slotHistory`. */
const slotKeysWithResidue = (state: ChatState): Set<string> => new Set([
  ...slotKeyedMaps(state).flatMap(m => Object.keys(m)),
  ...Object.values(state.refreshIssueByRequest ?? {}).map(issue => issue.key),
  ...Object.keys(state.mcpApps ?? {}).map(k => k.split(MCP_APP_KEY_SEP)[0]),
  ...(state.slotHistory ?? []),
])

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

/** Drop every ephemeral trace of one slot from chat state.
 *
 *  A local delete and a reconcile against the authoritative slot list both end
 *  here, so the two cannot disagree about what a departing slot leaves behind.
 *  Both spellings are removed: `safeKey` is identity for ordinary slot names and
 *  a no-op on an already-rewritten key, so one pass covers a caller holding
 *  either form. Durable automation evidence remains on the server and is
 *  available through the per-slot projection while the session exists. */
export const evictSlotState = (state: ChatState, slotKey: string): void => {
  const spellings = [slotKey, safeKey(slotKey)]
  for (const m of slotKeyedMaps(state)) {
    for (const spelling of spellings) delete m[spelling]
  }
  for (const [requestId, issue] of Object.entries(state.refreshIssueByRequest ?? {})) {
    if (spellings.includes(issue.key) || spellings.includes(safeKey(issue.key))) {
      delete state.refreshIssueByRequest[requestId]
    }
  }
  evictMcpApps(state, slotKey)
  state.slotHistory = (state.slotHistory ?? []).filter(k => k !== slotKey)
  // An evicted slot cannot serve as the failed-switch fallback either: an
  // authoritative snapshot said it is gone, and restoring it would re-create
  // exactly the dead-slot selection the origin exists to unwind (#6309).
  if (state.slotSwitchOrigin && spellings.includes(state.slotSwitchOrigin.key)) state.slotSwitchOrigin = null
}

export function addSlotListCases(builder: ActionReducerMapBuilder<ChatState>): void {
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
      // (`runReconnectCatchUp` in `hooks/websocket/reconnectCatchUp.ts`), and
      // the case below reconciles that reply even when it is empty.
      if (action.payload.length === 0 && !seenSnapshot) return
      reconcileSlotResidue(state, action.payload)
      clearFiledFolderSuggestions(state, action.payload)
    })
    /** A `slot_patch` frame stands in for the full list after a metadata edit
     *  or a close, so it drives the same cleanup the list would, limited to
     *  the rows it names: a patched `folder_id` retires that slot's folder
     *  suggestion, and a removed key's residue is evicted. The active slot is
     *  never evicted, matching `reconcileSlotResidue`. */
    .addCase(sseSlotPatch, (state, action) => {
      const { slots: rows, removed } = action.payload
      if (rows?.length) clearFiledFolderSuggestions(state, rows)
      const active = state.activeSlot
      const protectedKeys = active ? new Set([active, safeKey(active)]) : new Set<string>()
      for (const key of removed ?? []) {
        if (protectedKeys.has(key) || protectedKeys.has(safeKey(key))) continue
        evictSlotState(state, key)
      }
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
      // Gated behind the snapshot bit like the residue reconcile above, and
      // for the same staleness reason: an HTTP reply can be OLDER than the WS
      // stream. A filed slot's key can be reused by a fresh session that has
      // already received its own suggestion card; a pre-reuse reply still
      // names the key with folder_id set, and clearing on it would delete the
      // replacement's one-shot card — which the backend never re-offers. The
      // WS path has no such window (suggestion frames and slots frames arrive
      // in order on one socket), so after the first live snapshot the frames
      // own this cleanup exclusively.
      clearFiledFolderSuggestions(state, action.payload)
    })
}

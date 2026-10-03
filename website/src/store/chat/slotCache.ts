/** What every slot-detail reducer writes besides the transcript itself: the
 *  active paging cursor, a pane's page with its has-more and bounded markers,
 *  the retained server-count baseline, and the context meter.
 *
 *  It also owns the kept-head staleness protocol. A long chat keeps rows above
 *  its newest page, so those rows must be re-served whenever the server would now
 *  serve them differently. The state, and the one rule each field carries:
 *
 *  - `redactionHostsGen`: the serving value (`serving_gen.py`) this document's
 *    rows were served under. A read reporting another value is outdated and is
 *    re-read (`servedUnderObsoleteGen`, `renewUntilCurrent`, at most
 *    `MAX_OBSOLETE_REREADS`, then `ObsoleteReadError`, never installed).
 *  - `loadedRowsEpoch`: bumped by every change that outdates loaded rows
 *    (`markLoadedRowsChanged`); `slotRowsChangedAt[slot]` is the epoch of the
 *    newest such change for that slot. A read dispatched before it is dropped
 *    (`markedSince`), since the change's own read may already have landed.
 *  - `slotHeadUnverified[slot]`: the mark. A marked slot keeps no head: its next
 *    read must re-serve every loaded row, and only a read that did so, under the
 *    same mark, clears it (`clearVerifiedHead`).
 *  - `slotHealFailed[slot]`: that read failed; the notice stays until a re-read
 *    lands and clears the mark.
 *  - `slotServerTotalRaw[slot]`: whether the retained count came from an
 *    unbounded read, whose units differ from a bounded page's.
 *
 *  Refresh, warm and switch all consult the same helpers here, so a fourth read
 *  path has one place to learn the protocol from. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatMessage } from '../../types'
import type { ChatState } from './state'
import { isUnsafeKey, safeKey } from './wire'

/**
 * Replaces the paging cursor as ONE unit: how far back history goes, the offset
 * to ask for next, and the slot both describe. These three must move together --
 * writing the offset without re-keying leaves paging refusing forever, and
 * re-keying without the offset pages the wrong chat at the wrong place.
 */
export function setPagingCursor(state: ChatState, hasMore: boolean, nextBefore: number): void {
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
export function writeSlotPage(
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

/** Cache the ACTIVE slot's on-screen transcript under its key before
 *  `activeSlot` moves off it, so returning to the slot restores what the
 *  reader had rather than an older snapshot.
 *
 *  Every writer that moves the active slot away has to call this, not only
 *  `switchSlot`: New Chat (`setActiveSlot(null)` and then `createSlot`) and a
 *  history resume used to skip it, so a chat left that way kept whatever
 *  `slotMessages` held from the LAST switch. Closing the new chat then landed
 *  back on the old one through `switchSlot.pending`, which paints that stale
 *  entry -- the first page from an earlier visit, missing everything paged in
 *  or streamed since -- and the bounded switch read only widens it again when
 *  the cache and the window happen not to overlap.
 *
 *  Call it before the switch fields are re-keyed. A view whose own switch is
 *  still in flight keeps the pane's existing marker and bounded length rather
 *  than guessing; once a switch has landed the view is that switch's result,
 *  so `slotHasMore` is its marker. */
export function parkActiveTranscript(state: ChatState): void {
  const slot = state.activeSlot
  if (!slot || isUnsafeKey(slot) || state.messages.length === 0) return
  const viewIsProvisional = state.slotSwitchRequestId !== null && state.slotSwitchTarget === slot
  const k = safeKey(slot)
  writeSlotPage(state, slot, state.messages,
    viewIsProvisional ? undefined : state.slotHasMore,
    viewIsProvisional ? state.slotPaneBounded?.[k] : undefined)
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
/** Whether a read's rows were served under a redaction allow-list value other
 *  than the one this document now holds as current. A read in flight when an
 *  allow or revoke landed answers with rows the change has already outdated;
 *  its caller re-reads rather than write them over the fresh read the change
 *  itself asked for. Unknown on either side is never obsolete. */
export function servedUnderObsoleteGen(page: { redactionGen?: string }, currentGen: string | null | undefined): boolean {
  return typeof page.redactionGen === 'string' && page.redactionGen !== ''
    && typeof currentGen === 'string' && currentGen !== '' && page.redactionGen !== currentGen
}

/** How many re-reads one read may spend chasing allow-list changes that keep
 *  landing while it is in flight. Each one needs a fresh allow or revoke inside
 *  one round trip, so the cap is a runaway backstop, not a tuned budget. */
export const MAX_OBSOLETE_REREADS = 3

/** A read still served under an outdated allow-list value once the re-read cap
 *  is spent. Thrown rather than returned: its rows must never be installed, and
 *  installs carry no ordering, so a late one could overwrite the fresh rows the
 *  newest change's own read already put on screen. That read is what heals the
 *  view -- every change dispatches one -- so rejecting this one loses nothing. */
export class ObsoleteReadError extends Error {
  constructor() {
    super('read outdated by an allow-list change that kept moving')
    this.name = 'ObsoleteReadError'
  }
}

/** Whether a rejection is a read outdated by allow-list changes: benign, since the
 *  newest change's own read heals the view, so callers must not report it. */
export function isObsoleteReadRejection(err: unknown): boolean {
  return !!err && typeof err === 'object' && (err as { name?: unknown }).name === 'ObsoleteReadError'
}

/** An extra "this read is outdated" test for `renewUntilCurrent`, for a caller
 *  that cannot simply drop an outdated read (a switch must finish): `outdated`
 *  says a change has marked the slot since the current read started, and `rearm`
 *  is called just before each re-read starts, so the next `outdated` measures
 *  from it. */
export interface ReadFence {
  outdated: () => boolean
  rearm: () => void
}

/** Re-read until the rows answer under the allow-list value this document holds
 *  (and, with a `fence`, until no change has marked the slot since the read began).
 *
 *  A re-read is checked again only when something moved WHILE it was in flight
 *  (an allow or revoke landed, or the fence's slot was marked), since its rows
 *  predate that change too. A re-read that still disagrees with an UNCHANGED
 *  value is the server's current answer -- this document has simply not seen that
 *  value's status frame yet, e.g. right after a gateway restart -- so it is kept
 *  rather than looped on. One owner for "re-read until current, cap, refuse". */
export async function renewUntilCurrent<T extends { redactionGen?: string }, U extends { redactionGen?: string }>(
  read: T,
  reRead: () => Promise<U>,
  currentGen: () => string | null | undefined,
  fence?: ReadFence,
): Promise<T | U> {
  const outdated = (page: T | U, gen: string | null | undefined) => servedUnderObsoleteGen(page, gen) || (fence?.outdated() ?? false)
  let page: T | U = read
  for (let i = 0; i < MAX_OBSOLETE_REREADS; i++) {
    const asked = currentGen()
    if (!outdated(page, asked)) return page
    fence?.rearm()
    page = await reRead()
    if (currentGen() === asked && !(fence?.outdated() ?? false)) return page
  }
  // The cap is spent with the value still moving: a page that is still outdated
  // is refused, never installed (see ObsoleteReadError).
  if (outdated(page, currentGen())) throw new ObsoleteReadError()
  return page
}

/** Seed the redaction allow-list baseline from the value a read's rows were
 *  served under, if nothing has set one yet. Without this a tab whose first
 *  transcript read lands before its first status frame would take that frame's
 *  value as the baseline -- and a revoke landing between the two would never
 *  count as a change, leaving the revoked host's links live in the loaded rows.
 *  Only a baseline: a later read never moves it (see `noteRedactionHostsGen`). */
export function seedRedactionHostsGen(state: ChatState, gen: unknown): void {
  if (state.redactionHostsGen == null && typeof gen === 'string' && gen !== '') state.redactionHostsGen = gen
}

/** Clear a slot's `slotHeadUnverified` mark after a read that re-served every
 *  loaded row -- but only the mark that read saw at dispatch. A change marking
 *  the slot again while the read was in flight leaves the newer mark standing. */
export function clearVerifiedHead(state: ChatState, key: string, seen: number | undefined): void {
  if (seen === undefined || !state.slotHeadUnverified) return
  const k = safeKey(key)
  if (state.slotHeadUnverified[k] !== seen) return
  delete state.slotHeadUnverified[k]
  // The re-read that failed has now landed: the notice saying the rows may be
  // out of date ends here, not when a retry starts, so it stays on screen for as
  // long as an outdated (possibly revoked) link may still be.
  if (state.slotHealFailed?.[k]) delete state.slotHealFailed[k]
}

/** Whether a read dispatched at `epochAtDispatch` predates a change that has
 *  marked `key` since: its rows are outdated by that change, and the read the
 *  change dispatched may already have installed fresh ones, so it is dropped. */
export function markedSince(state: ChatState, key: string, epochAtDispatch: number | undefined): boolean {
  if (typeof epochAtDispatch !== 'number') return false
  const at = state.slotRowsChangedAt?.[safeKey(key)]
  return typeof at === 'number' && at > epochAtDispatch
}

export function retainServerTotal(state: ChatState, key: string, total: number | undefined, running?: boolean, seq?: number, boundedRead?: boolean): void {
  if (running && !boundedRead) return
  if (typeof total !== 'number' || !Number.isFinite(total)) return
  if (!state.slotServerTotal) state.slotServerTotal = {}
  if (!state.slotServerTotalSeq) state.slotServerTotalSeq = {}
  const priorSeq = state.slotServerTotalSeq[safeKey(key)]
  // An older response must not lower the baseline a newer one already set, or
  // the next warm compares against a count that was never the newest view.
  if (typeof seq === 'number' && typeof priorSeq === 'number' && seq < priorSeq) return
  state.slotServerTotal[safeKey(key)] = total
  // Which units that count is in, written with it so the two cannot disagree. Only a
  // read that SAYS it was unbounded is marked raw; a caller that does not say keeps
  // the units every count was assumed to be in before the marker existed.
  if (!state.slotServerTotalRaw) state.slotServerTotalRaw = {}
  if (boundedRead === false) state.slotServerTotalRaw[safeKey(key)] = true
  else delete state.slotServerTotalRaw[safeKey(key)]
  // Only an ORDERED response moves the order: clearing it on an unordered write
  // erased the field the staleness check reads, so a late warm read as a truncation.
  if (typeof seq === 'number') state.slotServerTotalSeq[safeKey(key)] = seq
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
export function seedContextUsage(
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

export const slotCacheReducers = {
  sseContextUsage(state: ChatState, action: PayloadAction<{ slot: string; pct: number; used_tokens?: number; window_tokens?: number; reset?: boolean }>) {
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
}

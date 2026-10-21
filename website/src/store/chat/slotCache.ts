/** What every slot-detail reducer writes besides the transcript itself: the
 *  active paging cursor, a pane's page with its has-more and bounded markers,
 *  the retained server-count baseline, and the context meter. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatMessage } from '../../types'
import type { ChatState } from './state'
import { isUnsafeKey, safeKey } from './wire'
import { OLDER_PAGE_LIMIT } from './paging'
import { isDurableRow } from './transcript'
import { type ParkedThinking, mergePreservedThinking } from './thinking'

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
 *  page's own flag is not true of the result, or it is `boundSlotWindow`
 *  trimming a live-only array whose head no page ever described. The existing
 *  marker and cursor are left alone rather than guessed at.
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
  nextBefore?: number,
): void {
  const k = safeKey(key)
  state.slotMessages[k] = messages
  if (!state.slotPaneBounded) state.slotPaneBounded = {}
  if (boundedLen === undefined) delete state.slotPaneBounded[k]
  else state.slotPaneBounded[k] = boundedLen
  // An undefined marker means the array's head is retained older rows, so the
  // cursor that addressed them still does. Any other write re-describes the
  // head: it keeps a cursor only if it brought one, never the old head's.
  if (hasMore === undefined) return
  if (!state.slotPaneHasMore) state.slotPaneHasMore = {}
  state.slotPaneHasMore[k] = hasMore
  if (!state.slotPaneNextBefore) state.slotPaneNextBefore = {}
  if (hasMore && nextBefore !== undefined && nextBefore > 0) state.slotPaneNextBefore[k] = nextBefore
  else delete state.slotPaneNextBefore[k]
}

/** How many transcript rows a NON-active slot keeps in `slotMessages`.
 *
 *  `slotMessages` is the one lo-space retention site PR #10985 left unbounded:
 *  every non-active slot held its full transcript array for as long as the
 *  session existed, freed only by `evictSlotState` when the slot was closed
 *  (#11139). A background slot that completes a turn appends one assistant row
 *  per cycle (`applyNonActiveFrame` -> `settleEndedBackgroundTurn`), and a reader
 *  who pages back then parks all of it on switch-away, so a monitor loop or a
 *  scrolled-back pane grew its array without bound.
 *
 *  The value is `OLDER_PAGE_LIMIT` -- exactly page one, the size the slot
 *  re-hydrates with -- so a trimmed slot switched back into reads as a fresh
 *  bounded page with the walk available, not as a short transcript. The active
 *  pane (`state.messages`) is left unbounded: the virtualizer renders it lazily
 *  and the reader is looking at it. */
export const SLOT_RETAIN_LIMIT = OLDER_PAGE_LIMIT

/** Clamp a NON-active slot's live-appended growth so a background slot cannot
 *  retain history without bound. A no-op when the array already fits.
 *
 *  It bounds only a slot no pane has HYDRATED (`slotHydrated[k]` unset): a slot
 *  fed by background turns, the switch-away park, and the warm that follows each
 *  background `chat_done`. That is the monitor-loop leak -- a pane streaming turn
 *  after turn in the background, one assistant row per cycle, forever. A slot a
 *  pane hydrated is one the reader opened, so it is left whole: trimming it would
 *  shrink a transcript the reader holds, and which end to shed there is a
 *  retention policy this bound does not decide.
 *
 *  The flag means "a pane shows this slot now". `ChatPane` sets it at mount
 *  (`holdHydratedSlotPane`) or through its hydrate, and clears it once the last
 *  pane showing the slot unmounts (`clearSlotHydrated`), so a monitor pane
 *  opened once and then left in the background is bounded again. Split-view
 *  panes never route through `activeSlot`, which is why the gate reads this
 *  pane signal rather than the active slot.
 *
 *  Dropping leading rows changes the array's head, and the markers that index
 *  the head must move with it:
 *
 *   - `slotPaneBounded[k]` is CLEARED (passed as `undefined`): it counts LEADING
 *     rows that came from a bounded page, and the leading rows just changed.
 *   - An UNDESCRIBED head (`slotPaneHasMore[k]` undefined: rows only ever
 *     live-appended) stays undescribed. The trim writes no marker and no cursor,
 *     because the server's `before` cursor is an absolute row index and nothing
 *     here knows where in the session these rows start. A later
 *     `hydrateSlotMessages` therefore still runs and prepends the server's page
 *     onto the kept tail, which brings the dropped rows back with no gap.
 *   - A DESCRIBED head (a park or a warm wrote the marker) keeps a valid
 *     absolute cursor: the dropped head started at `slotPaneNextBefore[k]` (0
 *     when the marker says the array held the whole history), so the new head
 *     starts that many DURABLE rows later. The marker goes `true`, and
 *     `loadOlderSlotMessages` re-fetches exactly the dropped rows. Client-only
 *     rows (`thinking`, `permission`, `queued`, `streaming`) never reach disk,
 *     so they do not count toward the offset.
 *
 *  A dropped `thinking` row is client-only, so dropping it would lose its
 *  reasoning for good. Where it goes depends on where its anchor went:
 *
 *   - Anchor KEPT: the block stays INLINE, ahead of the kept rows. Every row
 *     between it and the cut is one its anchor scan steps over, so ahead of
 *     `kept` is exactly its original position relative to every retained row.
 *     Parking it instead hid the reasoning from a grid pane that mounts the
 *     slot later: that pane's hydrate returns early on the defined marker, and
 *     `reinsertThinkingOrphans` runs only when the slot is made active.
 *   - Anchor DROPPED: the block goes through `mergePreservedThinking`, which
 *     parks a server-anchored block in `thinkingOrphans[k]` for
 *     `reinsertThinkingOrphans` to re-seat once a page loads its anchor again,
 *     and keeps an unconfirmed one in the window, ahead of the kept rows. */
export function boundSlotWindow(state: ChatState, slot: string): void {
  if (isUnsafeKey(slot)) return
  const k = safeKey(slot)
  if (state.slotHydrated?.[k]) return
  const msgs = state.slotMessages[k]
  if (!msgs || msgs.length <= SLOT_RETAIN_LIMIT) return
  const dropped = msgs.slice(0, msgs.length - SLOT_RETAIN_LIMIT)
  const kept = msgs.slice(msgs.length - SLOT_RETAIN_LIMIT)

  // A dropped block whose anchor survived has no possible anchor and no
  // scan-ending row between it and the cut. Every anchor (a tool row, an
  // answer) and every turn-boundary user row is durable or `streaming`, so the
  // dropped blocks after the LAST such row are exactly those: they stay in the
  // window, in order. A block before that row goes to the merge below even when
  // the row is one its scan would step over (a confirmed steer), so a misread
  // there costs a park -- lossless -- never a misplaced block.
  let lastScanStop = -1
  dropped.forEach((m, i) => { if (isDurableRow(m) || m.role === 'streaming') lastScanStop = i })
  const inline = dropped.slice(lastScanStop + 1).filter(m => m.role === 'thinking')
  const settled = dropped.slice(0, lastScanStop + 1)

  // Only the remaining dropped blocks are preserved here: the kept window's own
  // thinking rows and the inline blocks stay where they are, so they are left
  // out of `existing` and cannot also be parked (a duplicate on re-seat).
  // Removing thinking rows does not move any block's anchor, because the anchor
  // scan steps over them.
  //
  // `incoming` is EMPTY and coverage is every durable row of the old window, so
  // every block with a server-confirmed anchor in the dropped head reads as
  // "covered, no position here" and is handed to `orphanSink` with the helper's
  // own anchor and occurrence record. The return value holds the blocks the
  // helper KEEPS rather than parks (an anchor with no server `ts` yet); they
  // stay in the window, ahead of the kept rows. Each dropped block lands in
  // exactly one of `inline`, the sink, the return value, or the helper's own
  // #5815 drop of a finished turn that names no anchor.
  const orphanSink: Array<ParkedThinking<ChatMessage>> = []
  const unparked = mergePreservedThinking(
    [...settled, ...kept.filter(m => m.role !== 'thinking')], [], msgs.filter(isDurableRow), false, orphanSink)
  if (orphanSink.length) {
    const parked = (state.thinkingOrphans ??= {})
    parked[k] = [...(parked[k] ?? []), ...orphanSink]
  }
  const head = [...unparked, ...inline]
  const retained = head.length ? [...head, ...kept] : kept

  const described = state.slotPaneHasMore?.[k]
  if (described === undefined) {
    writeSlotPage(state, slot, retained, undefined, undefined)
    return
  }
  // `false` means the head was row 0. A `true` marker with no cursor names no
  // position, so the trim keeps it cursorless and the walk stays refused.
  const droppedDurable = dropped.filter(isDurableRow).length
  const shift = (hasMore: boolean, prior: number | undefined): number | undefined => {
    const priorBefore = hasMore ? prior : 0
    return priorBefore === undefined || (hasMore && priorBefore <= 0) ? undefined : priorBefore + droppedDurable
  }
  writeSlotPage(state, slot, retained, true, undefined, shift(described, state.slotPaneNextBefore?.[k]))
  // A pending switch's origin snapshot is the other retained copy of this
  // slot's cursor: a rejected switch restores it onto the trimmed array, so it
  // moves with the head the same way. A cursorless result keeps `nextBefore`
  // at 0, which `loadOlderMessages` refuses, as the pane's cursorless marker does.
  const origin = state.slotSwitchOrigin
  if (origin?.cursor && !isUnsafeKey(origin.key) && safeKey(origin.key) === k) {
    origin.cursor.nextBefore = shift(origin.cursor.hasMore, origin.cursor.nextBefore) ?? 0
    origin.cursor.hasMore = true
  }
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
    viewIsProvisional ? state.slotPaneBounded?.[k] : undefined,
    // The active cursor travels with the view it addresses, so the parked
    // pane can keep paging from where the full page left off.
    !viewIsProvisional && state.slotCursorKey === slot ? state.slotOldestIndex : undefined)
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

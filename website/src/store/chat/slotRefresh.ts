/** Re-reading a slot's transcript without switching to it: `refreshSlot` for
 *  the open pane (reconnect, turn end, variant switch) and `warmSlotCache` for
 *  a background pane, each with the reducer that merges its count-matched page
 *  onto what the tab already holds. */
import { createAsyncThunk, type ActionReducerMapBuilder } from '@reduxjs/toolkit'
import type { ChatMessage } from '../../types'
import { mergePreservedPastes } from '../../utils/pasteTokens'
import type { ChatState } from './state'
import { fetchSlotDetail, isUnsafeKey, safeKey } from './wire'
import { deduplicateByMid, floorForGen, idAnchorsOneRow, isDurableRow, mergePreservedClientTs, midOccurrences, olderHeadAbovePage, raiseChunkSeq, rowIdentities, serverRowCount, snapshotChunkGen, snapshotChunkSeq, tailNotInPage, transcriptTsMs, tsEpoch } from './transcript'
import { PANE_HYDRATE_LIMIT, REFRESH_LIMIT_CEILING, SLOT_DETAIL_MAX_LIMIT, countMatchedFetchLimit, pagingCursorAfterKeptHead, slotCoverageShortfall } from './paging'
import { mergePreservedThinking, reinsertThinkingOrphans } from './thinking'
import { applyWarmRunState, bumpRunEpoch } from './runState'
import { retainServerTotal, seedContextUsage, setPagingCursor, writeSlotPage } from './slotCache'
import { hydrateQueuedBubbles } from './queue'

/** Re-fetch messages for a slot without changing activeSlot. Only applies if still active. */
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
    /* The bound itself lives in `countMatchedFetchLimit`, shared with the background
     * warm: which rows may be counted, why the floor declines on history it cannot
     * identify, and why an unidentified view takes the unbounded shape are all one
     * rule, stated once there. */
    const want = countMatchedFetchLimit({
      rows: view,
      floor: PANE_HYDRATE_LIMIT,
      ceiling: REFRESH_LIMIT_CEILING,
    })
    if (want === undefined) return fetchSlotDetail(key)
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

let warmSeqCounter = 0
const nextWarmSeq = (): number => ++warmSeqCounter

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
    // Captured BEFORE the fetch: two warms for one slot resolve in any order,
    // and the later-dispatched response is the newer view of the transcript.
    const warmSeq = nextWarmSeq()
    // Also captured BEFORE the fetch: the run entry's receipt tick. The
    // fulfilled reducer writes run state only while this still matches, so an
    // ordered live frame that reduces in between (a `_done`, a new turn's
    // first chunk) wins over the snapshot that predates it (see
    // `ChatState.slotRun`).
    const runTickAtDispatch = state.slotRun?.[safeKey(key)]?.tick ?? 0
    /* A populated cache is COUNT-MATCHED, by the same rule and the same owner
     * `refreshSlot` uses: ask for the span this pane already holds, never for the
     * whole chained transcript.
     *
     * Why this path must not ask unbounded whenever it holds anything: every
     * WebSocket reconnect warms each mounted pane, so one reconnect requests the
     * ENTIRE history of every session on screen at once. The handler answers each by
     * reading that session's whole corpus off disk, running its regex redaction
     * battery across it and serializing the result -- Python work holding the GIL on
     * one worker thread, so a handful of multi-MB sessions stalls every other request
     * and every WS frame queued behind them. This limit is the only cap on that work,
     * and the pane's own span is the honest size for it.
     *
     * A bounded page is safe for this reducer: `warmSlotCache.fulfilled` keeps any
     * older head sitting above the page's first row (`olderHeadAbovePage`), so a
     * window narrower than the pane's scrollback merges with it instead of replacing
     * it. A cache the bound cannot prove safe still takes the unbounded shape --
     * `countMatchedFetchLimit` answers `undefined` there.
     *
     * A STREAMING slot is no exception. The handler collapses chunk runs BEFORE it
     * slices. Its one folded streaming row carries no durable identity, so a
     * populated running cache asks for one extra row: the page still covers the same
     * number of durable rows instead of reporting a deterministic one-row shortfall.
     * The bounded read also leaves a comparable `total` behind for the next switch's
     * coverage check. Exempting streaming would apply the unbounded shape to the
     * panes most likely to be mid-turn when a socket drops. */
    const cache = state.slotMessages?.[safeKey(key)] ?? []
    const running = (state.slotRun[safeKey(key)]?.state ?? 'idle') !== 'idle'
    /* One guard this path needs beyond the shared rule, because it is the only one of
     * the three with no post-fetch validation in front of its reducer: a durable row
     * with no readable instant is invisible to BOTH safety nets. It can carry a `mid`,
     * so the count-matched bound admits it, and `slotCoverageShortfall` cannot place it
     * in time, so the coverage check below reports it as nothing to cover -- a window
     * that misses it then replaces the cache with no shortfall ever raised. Legacy
     * history written before the backend stamped a timestamp is the real case, and it
     * takes the unbounded shape. `refreshSlot` does not need this: it validates the
     * page it got against the view and retries, so an unplaceable row costs it a round
     * trip rather than a row. */
    const unplaceable = cache.some(m => isDurableRow(m) && transcriptTsMs(m.ts) === null)
    const matchedLimit = cache.length === 0
      ? PANE_HYDRATE_LIMIT
      : unplaceable
        ? undefined
        : countMatchedFetchLimit({
          rows: cache,
          floor: PANE_HYDRATE_LIMIT,
          ceiling: SLOT_DETAIL_MAX_LIMIT,
          span: 'placeable',
        })
    const limit = running && cache.length > 0 && matchedLimit !== undefined
      ? Math.min(SLOT_DETAIL_MAX_LIMIT, matchedLimit + 1)
      : matchedLimit
    const first = await fetchSlotDetail(key, limit)
    /* Coverage, MEASURED against the rows this pane holds -- the same check
     * `switchSlot` runs after its bounded read, for the same reason: the window
     * extends BACKWARD from the newest row, so a pane parked on a head it paged into
     * holds rows a newest-N window never reaches however exactly that window is sized
     * to the cache's count, and this reducer replaces rather than merges when nothing
     * anchors. A bare count cannot distinguish a true truncation from a bounded
     * snapshot that predates a concurrent sibling, and bounded and unbounded totals
     * do not even count the same corpus while streaming. Always close an observed
     * coverage hole, then let the reducer's ordered comparable-total check decide
     * whether rows were actually removed. */
    if (limit !== undefined && slotCoverageShortfall({ cached: cache, window: first.messages }) > 0) {
      const wide = await fetchSlotDetail(key)
      /* Carry the bounded read's total unconditionally, exactly as `switchSlot` does
       * after its own coverage retry. The unbounded handler counts the RAW window --
       * every per-turn `done` row included, rows the bounded path collapses away --
       * whether or not the slot is running, so `wide.total` is never in the same
       * units as the bounded counts the reducer compares it against. Storing it as
       * the baseline makes the next bounded warm's smaller collapsed count read as a
       * server shrink, which discards the mid-turn streaming row the page cannot
       * vouch for and restarts the in-flight reply mid-sentence. */
      return { ...wide, comparableTotal: first.total, warmSeq, runTickAtDispatch }
    }
    return { ...first, warmSeq, runTickAtDispatch }
  },
)

export function addSlotRefreshCases(builder: ActionReducerMapBuilder<ChatState>): void {
  builder
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
       * cannot re-derive it and diverge. `thinking` is held out (no
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
      // The active slot's server snapshot flipping to running is a turn
      // start (see `ChatState.runEpoch`), as it is in
      // syncSlotRunningFromServer; the hand-back in `enterActiveSlot` reads
      // the epoch to tell a turn that ran on screen from a slot that saw
      // nothing.
      if (running && !state.slotRunning) bumpRunEpoch(state, key)
      state.slotRunning = running
      state.slotStopping = action.payload.stopping ?? false
      state.pendingTurnSlot = null
      // Same seeding as switchSlot: this refresh is the reconnect recovery,
      // and the frames that raced it are exactly the ones it must not let
      // through a second time (the duplicated leading fragment).
      // A snapshot of a slot that is NOT running says no stream is in flight:
      // the floor is cleared, so a lost `_done` cannot leave the closed
      // turn's seq in place to swallow the next turn's opening chunks.
      if (running) {
        const snapGen = snapshotChunkGen(messages)
        state.lastChunkSeq = raiseChunkSeq(floorForGen(state.lastChunkSeq, state.lastChunkGen, snapGen), snapshotChunkSeq(messages))
        if (snapGen !== undefined) state.lastChunkGen = snapGen
      } else {
        state.lastChunkSeq = undefined
      }
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
      // The payload's own `total` counts the RAW window: a coverage retry answers
      // with the unbounded read, whose count includes every per-turn `done` row
      // and every unfolded chunk run, running or idle. The retained baseline is
      // the settled collapsed count, so a comparison against it must use the
      // collapsed count the retry carries (`comparableTotal`). ONE value, read
      // at every comparison site below and at the retain call: a raw count at
      // any one of them reads a rewind as growth and a same-count rewrite as a
      // newer row, restoring discarded rows and rendering a reply twice.
      const comparable = (action.payload as { comparableTotal?: number }).comparableTotal
      const cmpTotal = comparable ?? total
      const serverShrank = typeof priorTotal === 'number' && typeof cmpTotal === 'number'
        && cmpTotal < priorTotal && !staleTotal
      const anchorIds = anchorIdx >= 0 ? rowIdentities(prior[anchorIdx]) : []
      const warmAnchorIdx = warmed.findIndex(m => rowIdentities(m).some(id => anchorIds.includes(id)))
      // A `streaming` row is minted client-side by the first chunk and carries
      // no identity — and stays identity-less when a snapshot idles the slot
      // and finalizes it to `assistant` (syncSlotRunningFromServer), because
      // only the server's own assistant frame brings the `mid`. The rescue
      // keeps identity-less rows as "newer than the page". This one is not
      // when the page carries the same reply AT LEAST as far as the client
      // has it: the page's row IS that row, and keeping the copy renders the
      // reply twice — after a reconnect mid-turn, live chunks would then append
      // to the stale copy ("0..19 | 0..5 | 20..") until the end-of-turn warm;
      // after a turn that ended offline, the stale copy would sit under the
      // final reply for good. "At least as far" is proven, never assumed: a
      // final `assistant` row (no streaming row left on the page) folds every
      // chunk of the reply, and a page streaming row supersedes a client
      // streaming row only when its `seq` is at or past the client's replay
      // floor (same generation). A client copy the page cannot vouch for — a
      // chunk raced the fetch, the page carries no `seq`, the page has no reply
      // row past the anchor yet, or a client-finalized copy meets a page that
      // still says streaming — is kept: decline, not guess. Kept copies keep
      // the pre-existing behavior (the end-of-turn warm reconciles them).
      const pageTail = warmAnchorIdx >= 0 ? warmed.slice(warmAnchorIdx + 1) : warmed
      const pageStreamSeq = snapshotChunkSeq(warmed)
      const pageFinalReply = !warmed.some(m => m.role === 'streaming') && pageTail.some(m => m.role === 'assistant')
      const priorRun = state.slotRun[safeKey(key)]
      const clientSeq = floorForGen(priorRun?.lastChunkSeq, priorRun?.lastChunkGen, snapshotChunkGen(warmed))
      const pageStreamCoversClient = pageStreamSeq !== undefined
        && (clientSeq === undefined || pageStreamSeq >= clientSeq)
      const supersededByPage = (m: ChatMessage) => rowIdentities(m).length === 0 && (
        (m.role === 'streaming' && (pageFinalReply || pageStreamCoversClient))
        || (m.role === 'assistant' && pageFinalReply))
      // The page's reply row answers the page's LAST turn, so only the copy
      // that sits before the next turn boundary in the prior tail can be a
      // copy of it. A user or inject row past the anchor starts a turn the
      // page predates (a send that landed while the fetch was in flight): that
      // turn's live streaming row is not on the page at all and is kept
      // whole, whatever the page says about the earlier reply.
      const tail = prior.slice(anchorIdx + 1)
      const nextTurnAt = tail.findIndex(m => m.role === 'user' || m.role === 'inject')
      const beforeNextTurn = new Set(tail.slice(0, nextTurnAt >= 0 ? nextTurnAt : tail.length))
      const rescuable = anchorIdx >= 0 && !serverShrank
        ? tailNotInPage(tail, warmed).filter(m => !(beforeNextTurn.has(m) && supersededByPage(m)))
        : []
      // A rewrite REPLACES a reply, so the count holds while the post-anchor rows
      // differ. Equal tail LENGTH is what separates that from a real newer row.
      const sameCountRewrite = rescuable.length > 0 && warmAnchorIdx >= 0 && !staleTotal
        && typeof priorTotal === 'number' && typeof cmpTotal === 'number' && cmpTotal === priorTotal
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
      // The baseline retained for the next warm is the same collapsed count the
      // comparisons above read (`cmpTotal`), never the raw wide count: the raw
      // one is not comparable with the collapsed counts later pages report.
      retainServerTotal(
        state, key, cmpTotal, running, warmSeq,
        comparable !== undefined || action.payload.boundedRead,
      )
      // The run-state write is ORDERED against the live frame writers by the
      // entry's receipt tick (`ChatState.slotRun`). The warm is a
      // point-in-time snapshot, and the frame writers (chunk -> streaming,
      // _done -> idle) are ordered, so the question is whether any of them
      // ran between the snapshot and this reducer. The reconnect caller
      // dispatches this warm only after `ws.onopen`, so every frame the tab
      // missed while the socket was down is OLDER than the snapshot by
      // construction; the only frames that can be newer are the ones this
      // tab applied after the thunk captured `runTickAtDispatch`, and each
      // of those bumped the tick. An unchanged tick therefore makes the
      // snapshot the newest view of the run state in BOTH directions:
      //   - `running: false` idles the entry: the turn ended while the socket
      //     was down (idempotent with the `_done` frame, the turn-done
      //     caller's belt-and-braces contract);
      //   - `running: true` promotes an idle entry to streaming: the turn
      //     STARTED while the socket was down, and without this write the
      //     pane read idle -- composer unlocked, no indicator -- until its
      //     first post-reconnect frame, minutes in a quiet phase.
      // A changed tick means an ordered writer won and the snapshot is
      // stale: a `_done` that idled the pane before a `running: true`
      // snapshot reduced is kept (a promotion there resurrected a finished
      // pane, wedging its composer locked with no healer inside the
      // reconnect suppression window), and so is a new turn's first chunk
      // that landed before a `running: false` snapshot reduced. The thunk
      // always stamps the tick (an absent entry reads 0), so a payload
      // without one is not a production shape and is treated as stale.
      // Warm against warm is a separate order: two warms for one slot
      // resolve in any order and neither consumes the tick, so the one with
      // the higher `warmSeq` is the newer snapshot whichever lands first --
      // an older warm landing after a newer one's verdict declines
      // (`runWarmSeq`), and a newer one landing after an older one's still
      // applies.
      const runAtFulfil = state.slotRun[safeKey(key)]
      const olderThanApplied = typeof warmSeq === 'number' && (runAtFulfil?.runWarmSeq ?? 0) >= warmSeq
      const ordered = !olderThanApplied && (runAtFulfil?.tick ?? 0) === action.payload.runTickAtDispatch
      if (!running) {
        if (ordered) {
          const run = (state.slotRun[safeKey(key)] ??= { state: 'idle' })
          applyWarmRunState(run, 'idle', warmSeq)
          run.lastChunkSeq = undefined
          // Deliberately NOT synced into the failed-switch origin snapshot:
          // this write comes from an HTTP snapshot, not a live frame, and
          // the origin snapshot is fed only by the ORDERED frame writers in
          // applyNonActiveFrame so a fulfillment landing mid-switch cannot
          // mark a mid-turn origin idle and have the restore unlock its
          // composer.
        }
      } else {
        if (ordered) {
          const run = (state.slotRun[safeKey(key)] ??= { state: 'idle' })
          // Promote only FROM idle: a busier ordered state (tool_running,
          // compacting) already says the turn is live and is not downgraded
          // to streaming. The verdict is recorded either way so an older
          // warm cannot land after it.
          if (run.state === 'idle') {
            // The FIRST busy signal after idle counts a turn start, as a
            // chunk does (see `ChatState.runEpoch`).
            bumpRunEpoch(state, key)
            applyWarmRunState(run, 'streaming', warmSeq)
            // Not synced into the origin snapshot, for the reason given on
            // the idle write above.
          } else {
            applyWarmRunState(run, null, warmSeq)
          }
        }
        // A running pane's warm carries the newest chunk seq its streaming
        // row stands for; raise (never lower) the background replay floor so
        // a live chunk that raced this warm is not applied a second time.
        // The floor is monotonic, so it needs no tick guard.
        const seeded = snapshotChunkSeq(messages)
        if (seeded !== undefined) {
          const run = (state.slotRun[safeKey(key)] ??= { state: 'idle' })
          const snapGen = snapshotChunkGen(messages)
          run.lastChunkSeq = raiseChunkSeq(floorForGen(run.lastChunkSeq, run.lastChunkGen, snapGen), seeded)
          if (snapGen !== undefined) run.lastChunkGen = snapGen
        }
      }
      seedContextUsage(state, key, action.payload.context)
    })
}

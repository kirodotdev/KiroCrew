/** Switching the active pane to a slot: the `switchSlot` thunk (a bounded,
 *  coverage-checked slot-detail read), its pending / fulfilled / rejected
 *  reducers (the atomic handover, the merge onto the cached transcript, and the
 *  unwind to the pre-switch selection when the target is gone), and the
 *  pane-level notice a failed user gesture raises. */
import { createAction, createAsyncThunk, type ActionReducerMapBuilder } from '@reduxjs/toolkit'
import type { RootState } from '../index'
import type { ChatMessage } from '../../types'
import { emitSlotRead } from '../../lib/slotReadRelay'
import { devLog, inspectorOn } from '../../dev/scrollInspector'
import { armConfirmedCloseHold, markSlotRead, removeSlotOptimistic } from '../dashboardSlice'
import { mergePreservedPastes } from '../../utils/pasteTokens'
import { errMessage, isMissingSlotError, type StatusRejection } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import { recentErrors, recordError, redactSecrets, type ErrorReport } from '../../utils/errorReport'
import { chatSlotDetailPath } from '../../api/chatSlotPaths'
import type { ChatState } from './state'
import { fetchSlotDetail, isUnsafeKey, safeKey } from './wire'
import { createSegmentComparer, deduplicateByMid, floorForGen, isRealAssistantReplySegment, mergeConfirmedUserTailAfterPage, midOccurrences, olderHeadAbovePage, raiseChunkSeq, readPageSegment, rowIdentities, sameTranscript, serverRowCount } from './transcript'
import { abortActiveOlderFetch, pagingCursorAfterKeptHead, slotCoverageShortfall, slotSwitchFetchLimit } from './paging'
import { mergePreservedThinking, reinsertThinkingOrphans, type ThinkingAnchor } from './thinking'
import { bumpRunEpoch, enterActiveSlot, pushHistory } from './runState'
import { parkActiveTranscript, retainServerTotal, seedContextUsage, setPagingCursor, writeSlotPage } from './slotCache'
import { hydrateQueuedBubbles } from './queue'
import { loadSlotActivity } from './activity'
import { walkWindowBackTo } from './windowWalk'

/** `switchSlot`'s argument. The plain-string spelling is the overwhelmingly
 *  common one; the object form exists for caller classes that must opt out of a
 *  default or opt into a surface:
 *
 *  - `keepTargetOnMissing`: the ONE caller class that must NOT have a 404
 *    unwound — a switch into a slot the caller just created (e.g. the error
 *    handoff), where a 404 is a create/fetch race on a slot that exists and
 *    the seeded composer must stay visible. Handling it as a per-call option
 *    keeps the decision inside the reducer's atomic unwind instead of a caller
 *    patching half the state back afterwards -- the exact #6260 failure class
 *    this fix removes.
 *  - `announceOnMissing`: a USER-FACING gesture on a reference to a listed
 *    session — the sidebar rows, the command palette recents, the command
 *    bar's session picker, the notification panel's go-to-chat buttons, the
 *    keyboard session jump, the worlds scene. On a 404 the thunk then says
 *    why the gesture did nothing and evicts the gone entry synchronously via
 *    `removeSlotOptimistic` (#6372) — but only when the selection escapes the
 *    gone key (the rejected reducer restores a differing `slotSwitchOrigin`);
 *    evicting the session the user was already in would leave `activeSlot`
 *    naming a row the sidebar no longer lists. It is opt-IN because the remaining
 *    caller classes self-handle their 404 — the side-chat re-bind and
 *    worktree-open paths render their own in-page error, creation and
 *    recovery paths (auto-improvement, issue-radar, cold-boot restore, the
 *    Slack-token reconnect) silently fall back to a fresh session — so
 *    announcing there would double-report or contradict a successful
 *    recovery. */
export type SwitchSlotArg = string | { key: string; keepTargetOnMissing?: boolean; announceOnMissing?: boolean }

/** The slot key of a `switchSlot` argument, in either spelling. Non-object
 *  values pass through untouched: a hand-rolled test dispatch can omit
 *  `meta.arg` entirely (see the fulfilled reducer's requestId note), and the
 *  reducers' pre-existing tolerance of that must survive this indirection. */
const switchSlotKey = (arg: SwitchSlotArg): string => typeof arg === 'object' && arg !== null ? arg.key : arg

/** The structured report behind a localized switch failure, so the pane notice's
 *  "ask the agent" hand-off carries the request and the real error, not just the
 *  sentence the user read.
 *
 *  Two sources, in order:
 *
 *  1. The transport journal. A non-2xx passed through `apiFailure`, which
 *     recorded status, endpoint, backend `code` and body under the exact
 *     message the `ApiError` carries. Matched on message AND this request's
 *     endpoint, not `findReport`'s message-only lookup: two sessions failing
 *     with the same words ("Failed to fetch", "HTTP 502") are two requests,
 *     and the message-only match hands the second one the FIRST one's
 *     endpoint — a prompt then names a session the user did not click.
 *  2. Recorded HERE, when the journal has nothing. A fetch that REJECTED
 *     (`TypeError: Failed to fetch` on a dropped connection, a body that was
 *     not JSON) never reached `apiFailure`, so nothing journaled it — and the
 *     notice's hand-off then shipped a prompt with only the localized
 *     "could not be opened" line: no route, no endpoint, no underlying error,
 *     which is exactly the dead end the journal exists to prevent.
 *
 *     The entry keeps the journal's own key contract: `message` is the sentence
 *     the notice SHOWS (`switchSlotNoticeCopy`), and the raw error — class and
 *     text, `TypeError: Failed to fetch` — travels in `detail`. Recording the
 *     raw text as the message would make this entry the newest `"Failed to
 *     fetch"` in a journal every other surface still searches by message alone,
 *     so a different surface's Ask-agent prompt would name a session-open
 *     request it never made. Wrong context is worse than the empty prompt this
 *     replaces. The endpoint comes from `chatSlotDetailPath`, the same owner
 *     the request itself uses. A status-less report has no `status` — the
 *     prompt says what failed without inventing an HTTP code for a request
 *     that got none.
 *
 *  Returns a spread-friendly shape so journal-less reducer fixtures and the
 *  serialized rejection contract stay untouched. */
const switchSlotFailureReport = (
  error: unknown,
  key: string,
  shown: { kind: 'gone' | 'failed'; name: string },
): { report?: ErrorReport } => {
  const raw = errMessage(error)
  const endpoint = chatSlotDetailPath(key)
  // Same key normalization `findReport` applies (the journal stores redacted
  // messages), newest first.
  const needle = redactSecrets(raw).trim()
  const found = needle ? recentErrors().find(r => r.endpoint === endpoint && r.message.trim() === needle) : undefined
  if (found) return { report: found }
  const status = (error as { status?: unknown } | null)?.status
  const cls = (error as { name?: unknown } | null)?.name
  const detail = typeof cls === 'string' && cls && cls !== raw ? (raw ? `${cls}: ${raw}` : cls) : raw
  return {
    report: recordError({
      source: 'api',
      message: switchSlotNoticeCopy(shown.kind, shown.name),
      status: typeof status === 'number' ? status : undefined,
      endpoint,
      detail: detail || undefined,
    }),
  }
}

/** The sentence the pane notice shows for a `switchSlotGone` record. ONE owner
 *  for ChatPage (which re-resolves it on a locale switch) and the journal entry
 *  `switchSlotFailureReport` records under it — the journal is keyed by the
 *  message as the UI shows it, so the two must be the same words. */
export function switchSlotNoticeCopy(kind: 'gone' | 'failed', name: string): string {
  if (kind === 'failed') {
    return name
      ? i18nT('store.chatSlice.session_open_error_named', { name })
      : i18nT('store.chatSlice.session_open_error')
  }
  return name
    ? i18nT('store.chatSlice.session_gone_open_failed_named', { name })
    : i18nT('store.chatSlice.session_gone_open_failed')
}

/** See `switchSlotGone` on ChatState. Set by `switchSlot`'s catch for an
 *  `announceOnMissing` caller whose target 404ed. */
export const setSwitchSlotGone = createAction<{ name: string; kind: 'gone' | 'failed'; report?: ErrorReport }>('chat/setSwitchSlotGone')
export const clearSwitchSlotGone = createAction('chat/clearSwitchSlotGone')

export const switchSlot = createAsyncThunk<
  Awaited<ReturnType<typeof fetchSlotDetail>>,
  SwitchSlotArg,
  { rejectValue: StatusRejection }
>(
  'chat/switchSlot',
  async (arg, { dispatch, getState, rejectWithValue, requestId }) => {
    const key = switchSlotKey(arg)
    // Row-identity snapshot for the 404 eviction below. The authoritative slot
    // writers (`sseSlots`, `fetchSlots.fulfilled`) rebuild `dashboard.slots`
    // with fresh objects on every frame, so this reference doubles as a
    // request-scoped token: if ANY frame lands between this dispatch and the
    // catch — including one delivering a same-key replacement session — the
    // identity check below fails and the eviction is skipped. The stale row
    // then lingers exactly as it did pre-change, and the next frame owns it.
    const rowAtDispatch = (getState() as RootState).dashboard?.slots?.find(s => s.key === key)
    // Safe unconditionally: this fetch resets the pane's messages and cursor, so
    // any older page still in flight is superseded even when the key is unchanged.
    abortActiveOlderFetch()
    dispatch(markSlotRead(key))
    // Opening a session is the canonical read gesture: relay it so every
    // other open dashboard window retires this slot's unread bubble too —
    // but only AFTER the transcript fetch succeeds (see the emits by the
    // return paths below). A failed load displays no transcript, and a
    // pre-fetch relay would clear sibling badges for messages this window
    // never showed. Watermark = the slot's server-minted last_ts when
    // known, read AT EMIT TIME — after the fetch — so messages that arrived
    // while the transcript loaded (a reconnect window) are covered by the
    // relayed watermark instead of a stale pre-fetch capture. When none is
    // known the relay goes out with NO watermark — receivers then keep any
    // badge that recorded a watermark of its own (covering nothing is the
    // conservative default). Client time is never minted here: windows
    // disagreeing about the same message would strand badges against valid
    // relays. Optional-chained like the slotRun guard below: a partial
    // preloaded test state can omit the dashboard slice, and throwing here
    // would abort the switch fetch itself.
    const _newestSlotTs = () => (getState() as RootState).dashboard?.slots?.find(s => s.key === key)?.last_ts
    // Bounded to the page size so opening a long session costs one page, not the
    // whole chained transcript; `loadOlderMessages` walks back from the cursor
    // this fetch returns, and a window that misses rows this tab already holds
    // is extended older by `walkWindowBackTo` before it replaces the view.
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
        // A hole was OBSERVED between the cache and the window, not merely assumed
        // for want of an earlier total. Close it by extending the window OLDER
        // until it anchors a cached row -- `switchSlot.fulfilled` then keeps the
        // cache above that anchor (`olderHeadAbovePage`), so replacing the view
        // with the walked window deletes nothing. The shortfall count itself
        // stays the conservative multiset it is: it cannot tell "above the
        // anchor" from "in a hole", and does not have to -- the walk answers that.
        // Carry the bounded read's count forward: it is the baseline the next
        // switch compares against.
        if (inspectorOn()) {
          devLog('SWITCH', `short=${shortfall} lim=${limit ?? '-'} cached=${cached} total=${first.total ?? '?'}`)
        }
        const walked = await walkWindowBackTo(key, first, cachedRows)
        // Emit only while this request still owns the slot switch: a rapid
        // A->B switch leaves A's fetch resolving after B took over, and A's
        // transcript never rendered — relaying its read would clear sibling
        // badges for messages nobody displayed. `pending` assigns activeSlot
        // atomically before this thunk body runs, so a superseded request
        // observes someone else's key here.
        if ((getState() as { chat: ChatState }).chat.activeSlot === key) emitSlotRead(key, _newestSlotTs())
        return { ...walked, comparableTotal: first.total }
      }
      if ((getState() as { chat: ChatState }).chat.activeSlot === key) emitSlotRead(key, _newestSlotTs())
      return first
    } catch (e) {
      // A later-issued refresh can settle while this switch fetch is still in
      // flight. Its transcript/cursor settlement out-ranks this request (the
      // reducers apply the same claim test below), so the catch must not first
      // announce a stale failure or evict a row the refreshed active slot still
      // names. No application dispatch can interleave while this catch publishes
      // or suppresses its synchronous notice/eviction side effects.
      const chatAtFailure = (getState() as RootState).chat
      const claimAtFailure = chatAtFailure.slotSwitchChunkClaim
      const refreshOutranksSwitch = !isUnsafeKey(key)
        && claimAtFailure?.requestId === requestId
        && claimAtFailure.target === key
        && (chatAtFailure.refreshAppliedSeq?.[safeKey(key)] ?? 0) > claimAtFailure.refreshIssuedSeq
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
      if (typeof status === 'number') {
        const payload: StatusRejection = { status, message: errMessage(e) }
        // Let the rejected reducer consume and settle this request, but do not
        // publish side effects from a switch the applied refresh superseded.
        if (refreshOutranksSwitch) return rejectWithValue(payload)
        // A 404 means the target is GONE — classified on the STRUCTURED payload
        // with the same `isMissingSlotError` the rejected reducer applies, so
        // the two ends of this thunk cannot disagree about what a 404 is. The
        // reducer restores the pre-switch selection but cannot dispatch, which
        // made the recovery SILENT: nothing told the user why the click did
        // nothing, and the dead entry stayed listed until the next
        // authoritative refresh, inviting the same wordless bounce again
        // (#6372). For an `announceOnMissing` caller — a user-facing gesture on
        // a listed session, see SwitchSlotArg for why it is opt-in — surface
        // both halves here, BEFORE rejecting so the payload reaches
        // `.unwrap()` consumers and the reducer unchanged.
        // The eviction is `removeSlotOptimistic`: the 404 is exactly the
        // server-confirmed deletion that reducer asks its callers for, it
        // drops the row and its unread state synchronously with no network
        // round-trip, and the next authoritative slots write reconciles either
        // way.
        const announce = typeof arg === 'object' && arg !== null && arg.announceOnMissing === true
        if (announce && isMissingSlotError(payload)) {
          // Read BEFORE the eviction below removes the row. Optional-chained
          // like the other dashboard reads in this thunk: a partial preloaded
          // test state can omit the slice.
          const name = (getState() as RootState).dashboard?.slots?.find(s => s.key === key)?.title
          const chat = (getState() as RootState).chat
          // The announcement is ESCAPES-ONLY: re-activating the session the
          // user is already in (the live-claimed origin names the gone key)
          // stays silent, the pre-change behavior for exactly that gesture.
          // The intent's scenario is a click on a LISTED (other) session; a
          // notice over the still-open pane ships three contradicting signals
          // (deleted-notice, kept row, composer inviting input). Gated on the
          // live claim: a stale 404 that lost its claim to a newer gesture
          // cannot trust `slotSwitchOrigin` (the newer `pending` overwrote it).
          const reactivation = chat.slotSwitchRequestId === requestId && chat.slotSwitchOrigin !== null && chat.slotSwitchOrigin.key === key
          if (!reactivation) {
            // The page-level acknowledgment: ChatPage renders this
            // through its pane ErrorNotice above the composer (the
            // errors-use-error-notice surface), with the agent hand-off on. The
            // NAME is stored, not the sentence, so the copy re-resolves on a
            // locale switch. Cleared by the next `switchSlot.pending` or the
            // notice's own dismiss.
            dispatch(setSwitchSlotGone({
              name: name ?? '',
              kind: 'gone',
              ...switchSlotFailureReport(e, key, { kind: 'gone', name: name ?? '' }),
            }))
          }
          // Evict only when the selection will ESCAPE the evicted key. The
          // rejected reducer restores `slotSwitchOrigin` only when it differs
          // from the target (chat's `deleteSlot` states the invariant: the
          // active slot must already name a surviving peer by the time a slot
          // leaves the list). When the gone session IS the origin — the user
          // re-activated the session they were already in — no restore runs,
          // so evicting here would leave `activeSlot` naming a key no sidebar
          // row lists: the pane stays open, the header chips render blank
          // (`currentSlot` is undefined), and nothing heals it because an
          // authoritative write will not re-add a deleted slot. Keeping the
          // row for that one case is the pre-change behaviour, the notice
          // still explains the failure, and the next authoritative slots
          // frame retires the row once the user navigates away.
          // `keepTargetOnMissing` keeps the selection ON the target by the
          // reducer's own contract, so the selection never escapes there.
          const keepTarget = typeof arg === 'object' && arg !== null && arg.keepTargetOnMissing === true
          const escapes = !keepTarget && chat.slotSwitchOrigin !== null && chat.slotSwitchOrigin.key !== key
          // Freshness conditions on the DESTRUCTIVE half only (the notice above
          // stays: it truthfully explains the dead click even when stale).
          // (1) The row must still be the OBJECT captured at dispatch (see
          // `rowAtDispatch`): any authoritative frame that changed row `key` in
          // ANY way — a replacement session included — breaks the identity and
          // disarms the eviction. `applySlots` reuses a row's identity only
          // when it is jsonEqual, and a genuinely recreated session cannot be
          // byte-identical (its message count and last_ts differ from the dead
          // one's), so identity is honest about content freshness.
          // (2) This switch must still be the LIVE one: `pending` stored this
          // thunk's requestId in `slotSwitchRequestId` and any newer switch
          // overwrote it, so a stale 404 that lost a race to a newer gesture —
          // a successful same-key re-open included — cannot evict.
          const rowNow = (getState() as RootState).dashboard?.slots?.find(s => s.key === key)
          if (escapes && rowAtDispatch !== undefined && rowNow === rowAtDispatch && chat.slotSwitchRequestId === requestId) {
            dispatch(armConfirmedCloseHold(key))
            dispatch(removeSlotOptimistic(key))
          }
        } else if (typeof arg === 'object' && arg !== null && arg.announceOnMissing === true) {
          // A non-404 failure on the SAME user gesture (a 5xx, a proxy error)
          // is just as silent by default: the rejected reducer keeps the
          // target selected with an empty pane (the transient-failure branch),
          // and nothing says why the transcript did not load. Announced
          // callers get the same pane ErrorNotice with failure copy — no
          // eviction (the session exists) and no new affordance: the row and
          // composer already invite the natural retry. Gated on the live
          // claim, UNLIKE the gone notice above: "was deleted" stays true
          // whenever the 404 lands, but "could not be opened" describes THIS
          // attempt — a superseded rejection reporting it would overwrite the
          // notice belonging to the user's current gesture with one about a
          // click they already moved past.
          if ((getState() as RootState).chat.slotSwitchRequestId === requestId) {
            const name = (getState() as RootState).dashboard?.slots?.find(s => s.key === key)?.title
            dispatch(setSwitchSlotGone({
              name: name ?? '',
              kind: 'failed',
              ...switchSlotFailureReport(e, key, { kind: 'failed', name: name ?? '' }),
            }))
          }
        }
        return rejectWithValue(payload)
      }
      // Status-less errors (a transport failure, a thrown TypeError) cross the
      // boundary as miniSerializeError. The same announced-gesture contract
      // applies: say the open failed where the user is looking — gated on the
      // live claim like the numeric branch above, so a superseded rejection
      // cannot overwrite the current gesture's notice.
      if (!refreshOutranksSwitch
          && typeof arg === 'object' && arg !== null && arg.announceOnMissing === true
          && (getState() as RootState).chat.slotSwitchRequestId === requestId) {
        const name = (getState() as RootState).dashboard?.slots?.find(s => s.key === key)?.title
        dispatch(setSwitchSlotGone({
          name: name ?? '',
          kind: 'failed',
          ...switchSlotFailureReport(e, key, { kind: 'failed', name: name ?? '' }),
        }))
      }
      throw e
    }
  },
)

export function addSlotSwitchCases(builder: ActionReducerMapBuilder<ChatState>): void {
  builder
    .addCase(setSwitchSlotGone, (state, action) => { state.switchSlotGone = action.payload })
    .addCase(clearSwitchSlotGone, (state) => { state.switchSlotGone = null })
    .addCase(switchSlot.pending, (state, action) => {
      // A new USER gesture supersedes the previous gone-notice — and only a
      // user gesture: `announceOnMissing` is exactly the user-facing-gesture
      // marker (see SwitchSlotArg). Programmatic switches (route sync, the
      // rejected restore's follow-ups, creation flows) must not eat a notice
      // the user has not seen.
      if (typeof action.meta.arg === 'object' && action.meta.arg !== null && action.meta.arg.announceOnMissing === true) state.switchSlotGone = null
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
      // Cache the outgoing slot's messages before the switch fields below are
      // re-keyed (see parkActiveTranscript).
      parkActiveTranscript(state)
      // This fetch replaces the cursor, so it is stale from here until it lands
      // -- including a same-key switch, where the key alone still looks valid.
      state.slotCursorKey = null
      state.slotSwitchRequestId = action.meta?.requestId ?? null
      state.slotSwitchTarget = target
      // Save current slot's activity
      if (state.activeSlot) {
        state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
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
      // The replay floor is per slot (each slot numbers its own chunks). Park
      // the outgoing slot's floor on its background run entry (raise, never
      // lower, so a frame that moved it past an earlier snapshot is not
      // undone) and take over the target's, which its background frames
      // maintain: carrying A's higher floor into a running B would drop B's
      // opening chunks as replays.
      const runs = (state.slotRun ??= {})
      const outgoingSlot = state.activeSlot
      if (outgoingSlot !== null && outgoingSlot !== target && !isUnsafeKey(outgoingSlot)) {
        const outgoing = (runs[safeKey(outgoingSlot)] ??= { state: 'idle' })
        outgoing.lastChunkSeq = raiseChunkSeq(floorForGen(outgoing.lastChunkSeq, outgoing.lastChunkGen, state.lastChunkGen), state.lastChunkSeq)
        if (state.lastChunkGen !== undefined) outgoing.lastChunkGen = state.lastChunkGen
      }
      // Move `activeSlot` NOW -- before the mirrors below are re-seeded for
      // the target -- so the hand-back inside reads the mirror while it still
      // describes the outgoing slot. WS events for the new slot are accepted
      // from here on.
      enterActiveSlot(state, target)
      if (target !== outgoingSlot) {
        state.lastChunkSeq = runs[safeKey(target)]?.lastChunkSeq
        state.lastChunkGen = runs[safeKey(target)]?.lastChunkGen
        // The run mirrors describe the slot ON SCREEN, and from this reducer
        // on that is the target: `activeSlot` moved above and the cached
        // transcript is restored with it, so a mirror still carrying the
        // outgoing slot's run state hands every reader of it -- the
        // transcript's fold, the composer's busy rule, the Stop affordance --
        // the wrong session until `fulfilled` lands. Take the target's keyed
        // entry, which its background frames maintained while it was not
        // active; `fulfilled` overwrites this from the server, and
        // `rejected` restores the origin snapshot captured above, before this
        // write. A turn that started in the background but has not yet sent
        // its first frame reads idle here, exactly as its pane did while it
        // was in the background (the keyed entry is promoted only by ordered
        // frames or a tick-ordered warm; see warmSlotCache.fulfilled).
        const incoming = runs[safeKey(target)]?.state ?? 'idle'
        state.slotState = incoming
        state.slotRunning = incoming !== 'idle'
        state.slotStopping = incoming === 'stopping'
      }
      // Restore cached messages if available (instant switch), otherwise show loading.
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
      const retainedTotal = isUnsafeKey(target) ? undefined : state.slotServerTotal?.[safeKey(target)]
      const refreshIssuedSeq = isUnsafeKey(target)
        ? 0
        : state.refreshIssuedSeq?.[safeKey(target)] ?? 0
      state.slotSwitchChunkClaim = {
        requestId: action.meta.requestId,
        target,
        ...(typeof retainedTotal === 'number' ? { serverTotal: retainedTotal } : {}),
        refreshIssuedSeq,
        clientTs: [],
        finalizers: [],
        rowlessFinalizer: null,
        settled: false,
      }
      state._wsChunkedDuringFetch = false
    })
    .addCase(switchSlot.fulfilled, (state, action) => {
      const { key, messages, running, hasMore, queue, nextBefore } = action.payload
      const claim = state.slotSwitchChunkClaim
      const requestId = action.meta?.requestId ?? (
        claim?.target === key && state.slotSwitchRequestId === claim.requestId
          ? claim.requestId
          : undefined
      )
      const ownsClaim = claim != null
        && claim.requestId === requestId && claim.target === key
      if (!ownsClaim || claim.settled) return
      const appliedRefreshSeq = isUnsafeKey(key)
        ? 0
        : state.refreshAppliedSeq?.[safeKey(key)] ?? 0
      if (ownsClaim && claim && appliedRefreshSeq > claim.refreshIssuedSeq) {
        // A refresh issued after this switch began has applied and already owns
        // every active-slot projection. Settle only this request, before touching
        // the response page, count, queue, cursor, context, replay floor, or run
        // state. An uncached switch stays loading until this decline closes it.
        claim.settled = true
        if (state.slotSwitchRequestId === requestId) {
          state.slotSwitchRequestId = null
          state.slotSwitchTarget = null
          state.slotSwitchOrigin = null
          if (state.activeSlot === key) state.slotLoading = false
        }
        return
      }
      const claimedClientTs = new Set(ownsClaim ? (claim?.clientTs ?? []) : [])
      const finalizers = ownsClaim ? (claim?.finalizers ?? []) : []
      const finalizerByClientTs = new Map(
        finalizers.map(finalizer => [finalizer.clientTs, finalizer] as const),
      )
      const rowlessFinalizer = ownsClaim ? claim?.rowlessFinalizer ?? null : null
      // The baseline THIS request was dispatched against, never the global entry
      // read now: a warm or refresh settling mid-flight moves that entry, and a
      // later value is unordered relative to this response.
      const baselineTotal = ownsClaim ? claim?.serverTotal : undefined
      if (ownsClaim && claim) claim.settled = true
      // Before the guards below, so an early return still ends this claim. Keyed
      // on requestId, which a hand-rolled dispatch may omit, so read it safely.
      if (state.slotSwitchRequestId !== null && state.slotSwitchRequestId === requestId) { state.slotSwitchRequestId = null; state.slotSwitchTarget = null; state.slotSwitchOrigin = null }
      if (isUnsafeKey(key)) return
      if (state.activeSlot !== key) return  // user switched away during fetch
      // A payload carrying `comparableTotal` came from the coverage walk: the
      // carried count is the first bounded read's settled one, and only that
      // may become the baseline.
      const comparable = (action.payload as { comparableTotal?: number }).comparableTotal
      const responseTotal = comparable ?? action.payload.total
      const responseComparable = comparable !== undefined || action.payload.boundedRead
      retainServerTotal(state, key, responseTotal, running, undefined, responseComparable)
      // The warm-cache invariant (slotRefresh.ts): a FALL in the server's own
      // comparable count means rows were REMOVED remotely between the read that
      // set the baseline and this one, so a cached row this response lacks was
      // discarded, not merely predated. Read only under the conditions
      // `retainServerTotal` accepts for establishing a baseline (idle, or an
      // explicitly comparable/bounded running read); an absent count on either
      // side, or a running non-comparable response, declines rather than guesses.
      const serverShrank = (!running || responseComparable === true)
        && typeof baselineTotal === 'number'
        && typeof responseTotal === 'number' && Number.isFinite(responseTotal)
        && responseTotal < baselineTotal
      state.slotState = running ? 'streaming' : 'idle'
      // Mark stale permissions as resolved so ApprovalBar ignores them
      if (!running) {
        for (const m of messages) {
          if (m.role === 'permission' && !m.meta?.resolved) m.meta = { ...m.meta, resolved: 'stale' }
        }
      }
      const existing = state.messages
      // A lower comparable count proves a remote rewind. Do not reinsert a
      // confirmed user suffix the authoritative page deliberately removed.
      const reconciledPage = serverShrank
        ? messages
        : mergeConfirmedUserTailAfterPage(existing, messages, running)
      const preserved = mergePreservedPastes(existing, reconciledPage)
      const pageSegment = readPageSegment(preserved)
      const snapGen = pageSegment.status === 'open' ? pageSegment.gen : undefined
      const snapSeq = pageSegment.status === 'open' ? pageSegment.seq : undefined
      const localMidCounts = midOccurrences(existing)
      const pageMidCounts = midOccurrences(preserved)
      // ONE exact segment identity for every local-vs-page comparison below:
      // the nearest shared anchor row (user send, dispatched inject, any row both
      // sides hold under one id) plus the segment ordinal after it. See
      // createSegmentComparer for why neither content nor timestamps take part.
      const segments = createSegmentComparer(preserved, existing)
      const compareSegments = segments.compare
      /* The retained head: rows this tab holds ABOVE the page's oldest row (see
       * the `olderHead` prepend below). They re-enter `next` verbatim, so they are
       * not candidates here -- a mid-less reply above the window would otherwise
       * be inserted once and prepended once. */
      const priorServerRows = existing.filter(m => m.role !== 'thinking')
      const { olderHead } = olderHeadAbovePage(priorServerRows, preserved)
      const headRows = new Set<ChatMessage>(olderHead)
      type LocalSegment = { index: number; message: ChatMessage }
      const isClaimed = (message: ChatMessage): boolean => {
        const clientTs = message.meta?.clientTs
        return typeof clientTs === 'string' && claimedClientTs.has(clientTs)
      }
      // Every local reply row below the retained head is a candidate: one a
      // post-dispatch frame claimed (it carries finalizer evidence), and every
      // content-bearing cached row no frame claimed -- a reply that streamed and
      // finalized while the slot was in the background, which carries only a
      // `clientTs`. Both reconcile through the same exact identity
      // (`pageProofIndex`): a row is dropped only when the page POSITIVELY holds
      // it, and kept otherwise. The unclaimed rows are the established
      // conservative fallback, widened from the newest row to every row so a
      // stale page that ends after segment 1 of a three-segment turn loses
      // neither segment 2 nor segment 3. "Kept otherwise" cannot duplicate a
      // fresh page's copy: the switch read covers every cached row it can
      // identify (`slotCoverageShortfall` + `walkWindowBackTo`), so a fresh page
      // holds the anchor of every identified local segment and the key matches;
      // a cached anchor the page lacks means the page predates it.
      const candidateSegments: LocalSegment[] = existing.flatMap((message, index) => {
        if (headRows.has(message)) return []
        if (!isRealAssistantReplySegment(message) && message.role !== 'streaming') return []
        return isClaimed(message) || message.content.length > 0 ? [{ index, message }] : []
      })
      const localMid = (local: LocalSegment): string | undefined => {
        const mid = local.message.meta?.mid
        return typeof mid === 'string' && mid.length > 0 ? mid : undefined
      }
      const finalizerFor = (local: LocalSegment) => {
        const clientTs = local.message.meta?.clientTs
        return typeof clientTs === 'string' ? finalizerByClientTs.get(clientTs) : undefined
      }
      const isFinalizer = (local: LocalSegment): boolean => finalizerFor(local) !== undefined
      const pageProofIndex = (
        local: LocalSegment,
        role: 'assistant' | 'streaming',
      ): number => {
        const hasRole = (message: ChatMessage): boolean => role === 'assistant'
          ? isRealAssistantReplySegment(message)
          : message.role === 'streaming'
        const mid = localMid(local)
        if (mid !== undefined) {
          if (localMidCounts.get(mid) !== 1) return -1
          const exact = pageMidCounts.get(mid) === 1
            ? preserved.findIndex(message => hasRole(message) && message.meta?.mid === mid)
            : -1
          if (exact >= 0 || role !== 'streaming') return exact
        }
        return preserved.findIndex((message, index) =>
          hasRole(message)
          && !(role === 'streaming' && typeof message.meta?.mid === 'string' && message.meta.mid)
          && compareSegments(index, local.index) === 'same',
        )
      }
      const usedFinalized = new Set<number>()
      // An unmatched candidate stays only while the page has not proven a
      // remote rewind. A request claim proves the frame arrived after switch
      // dispatch, not after the server's final slot-detail snapshot: that read
      // can retry and observe a later rewind. A confirmed comparable shrink is
      // therefore authoritative over claimed and unclaimed rows alike. A truly
      // post-snapshot finalizer is recovered by its own turn-completion refresh;
      // guessing here would permanently resurrect deleted history.
      //
      // Without shrink proof, a claimed row stays. An unclaimed row stays when
      // the page has no assistant statement at all, when it is the open accumulator the
      // open-page rules below order by sequence, or when the page provably
      // PREDATES it: the page's range reaches above the row (the page is the
      // whole transcript, or holds an identified local row before it -- a fresh
      // page would then carry this segment under the same key, and does not),
      // and the row is placeable (it carries a server id, or an identified local
      // row precedes it). A mid-less row with no identified row before it is
      // placeable by neither side, and a page sharing no identity with the rows
      // before it says nothing about them; the page keeps its authority in both,
      // as it does for every other unidentifiable row (coverage and the retained
      // head decline the same way).
      const pageReachesAbove = (local: LocalSegment): boolean =>
        !hasMore || segments.leftHoldsRowBefore(local.index)
      const sameCountReplacement = (local: LocalSegment): boolean => {
        const mid = localMid(local)
        if (isClaimed(local.message)
            || !isRealAssistantReplySegment(local.message)
            || mid === undefined
            || localMidCounts.get(mid) !== 1
            || !(!running || responseComparable === true)
            || typeof baselineTotal !== 'number'
            || typeof responseTotal !== 'number'
            || responseTotal !== baselineTotal) return false
        // A rewrite can replace one identified reply without changing the
        // collapsed row count. Exact mid lookup then misses by design, but a
        // different unique server row under the same shared-anchor ordinal is
        // positive replacement proof. Claimed post-dispatch rows and unknown
        // segment comparisons stay conservative.
        return preserved.some((message, index) => {
          if (!isRealAssistantReplySegment(message)) return false
          const pageMid = message.meta?.mid
          return typeof pageMid === 'string'
            && pageMid.length > 0
            && pageMid !== mid
            && pageMidCounts.get(pageMid) === 1
            && compareSegments(index, local.index) === 'same'
        })
      }
      const unmatchedStays = (local: LocalSegment): boolean => !serverShrank
        && !sameCountReplacement(local) && (
        isClaimed(local.message)
        || pageSegment.status === 'absent'
        || (pageSegment.status === 'open' && local.message.role === 'streaming')
        || (pageReachesAbove(local)
          && (localMid(local) !== undefined || segments.rightIdentifiesRowBefore(local.index))))
      let localSegments = candidateSegments.filter(local => {
        const pageIndex = pageProofIndex(local, 'assistant')
        if (pageIndex >= 0 && !usedFinalized.has(pageIndex)) {
          usedFinalized.add(pageIndex)
          return false
        }
        return pageIndex >= 0 || unmatchedStays(local)
      }).map(local => (
        // An unclaimed background stream the slot has since stopped is a
        // finished reply (its `_done` ran on the background path); a claimed
        // stream keeps its live role for the finalizer rules below.
        local.message.role === 'streaming' && !running && !isClaimed(local.message)
          ? { ...local, message: { ...local.message, role: 'assistant' as const, rawText: local.message.content } }
          : local
      ))
      const identityCounts = (rows: ChatMessage[]): Map<string, number> => {
        const counts = new Map<string, number>()
        for (const row of rows) {
          for (const identity of rowIdentities(row)) {
            counts.set(identity, (counts.get(identity) ?? 0) + 1)
          }
        }
        return counts
      }
      const existingIdentityCounts = identityCounts(existing)
      const insertLocalSegments = (
        base: ChatMessage[],
        locals: LocalSegment[],
      ): ChatMessage[] => {
        let result = base
        for (const local of locals) {
          const resultIdentityCounts = identityCounts(result)
          const suffixIds = new Set(
            existing.slice(local.index + 1)
              .flatMap(rowIdentities)
              .filter(identity => existingIdentityCounts.get(identity) === 1),
          )
          const insertionIndex = result.findIndex(message =>
            rowIdentities(message).some(identity =>
              suffixIds.has(identity) && resultIdentityCounts.get(identity) === 1,
            ),
          )
          const at = insertionIndex < 0 ? result.length : insertionIndex
          result = [
            ...result.slice(0, at), local.message, ...result.slice(at),
          ]
        }
        return result
      }
      const finalizePageOpen = (base: ChatMessage[]): ChatMessage[] => {
        if (pageSegment.status !== 'open') return base
        const result = [...base]
        result[pageSegment.index] = {
          ...pageSegment.message,
          role: 'assistant',
          rawText: pageSegment.message.content,
        }
        return result
      }
      const localSeqAtPageGen = floorForGen(
        state.lastChunkSeq, state.lastChunkGen, snapGen,
      )
      const localOpenIsAtLeastAsNew = (snapGen === undefined || snapGen === state.lastChunkGen)
        && (snapSeq === undefined
          || (localSeqAtPageGen !== undefined && localSeqAtPageGen >= snapSeq))
      const pageGenerationIsNew = snapGen !== undefined
        && snapGen !== state.lastChunkGen
      const finalizerIsAtLeastAsNew = (
        finalizer: { seq?: number; gen?: string } | null | undefined,
      ): boolean => {
        if (finalizer == null || snapSeq === undefined) return false
        const finalizerSeqAtPageGen = floorForGen(
          finalizer.seq, finalizer.gen, snapGen,
        )
        return finalizerSeqAtPageGen !== undefined
          && finalizerSeqAtPageGen >= snapSeq
      }
      const finalizerCanReplacePageOpen = (local: LocalSegment): boolean => {
        const mid = localMid(local)
        const exactMid = mid !== undefined
          && localMidCounts.get(mid) === 1
          && pageMidCounts.get(mid) === 1
          && pageSegment.status === 'open'
          && pageSegment.message.meta?.mid === mid
        return exactMid || finalizerIsAtLeastAsNew(finalizerFor(local))
      }
      // This gate belongs to the matching request/target claim, not the
      // candidates left after exact-mid rows are removed. Otherwise a rowless
      // receipt following an exact canonical assistant can finalize the next
      // open segment in the fetched page.
      const hasClaimedFinalizer = finalizers.length > 0
      let nextBase = pageSegment.status === 'finalized'
        ? preserved.filter(message => message.role !== 'streaming')
        : preserved
      const rowlessClosesPageOpen = pageSegment.status === 'open'
        && rowlessFinalizer !== null
        && !hasClaimedFinalizer
        && finalizerIsAtLeastAsNew(rowlessFinalizer)
      if (rowlessClosesPageOpen) {
        nextBase = finalizePageOpen(preserved)
      }
      // Once a rowless boundary closes the fetched segment, every claimed
      // local row was created after that boundary. Append those later segments;
      // do not re-enter open-row replacement and erase the finalized segment.
      if (pageSegment.status === 'open'
          && localSegments.length > 0
          && !rowlessClosesPageOpen) {
        const insertBeforePageOpen = (
          base: ChatMessage[], locals: LocalSegment[],
        ): ChatMessage[] => locals.length === 0 ? base : [
          ...base.slice(0, pageSegment.index),
          ...locals.map(local => local.message),
          ...base.slice(pageSegment.index),
        ]
        const matchingIndex = localSegments.findIndex(local =>
          pageProofIndex(local, 'streaming') === pageSegment.index,
        )
        if (matchingIndex >= 0) {
          const local = localSegments[matchingIndex]
          nextBase = [...preserved]
          if (local.message.role === 'assistant') {
            // Same segment, proven by exact `mid` or by shared anchor and
            // ordinal. A claimed finalizer still needs its own ordering
            // evidence to replace the open row (a lower sequence means the
            // page saw chunks this tab did not). A finalized row with NO
            // finalizer evidence -- a reply the background path finalized on
            // the server's own `_done` -- is the completed copy of a segment
            // the page still projects open, so it converges onto that row
            // whenever the generations are compatible; a page from another
            // gateway process keeps the retain-before rule.
            if (finalizerCanReplacePageOpen(local)
                || (!isFinalizer(local) && !pageGenerationIsNew)) {
              nextBase[pageSegment.index] = local.message
              const earlier = localSegments.slice(0, matchingIndex)
              nextBase = insertBeforePageOpen(nextBase, earlier)
            } else {
              nextBase = insertBeforePageOpen(
                preserved, localSegments.slice(0, matchingIndex + 1),
              )
            }
          } else {
            nextBase[pageSegment.index] = localOpenIsAtLeastAsNew
              ? local.message
              : pageSegment.message
            const earlier = localSegments.slice(0, matchingIndex)
            nextBase = insertBeforePageOpen(nextBase, earlier)
          }
          localSegments = localSegments.slice(matchingIndex + 1)
        } else {
          const causalFinalizerIndex = localSegments.findIndex(local =>
            local.message.role === 'assistant' && isFinalizer(local),
          )
          if (causalFinalizerIndex >= 0) {
            const local = localSegments[causalFinalizerIndex]
            if (finalizerCanReplacePageOpen(local)) {
              nextBase = [...preserved]
              nextBase[pageSegment.index] = local.message
              const earlier = localSegments.slice(0, causalFinalizerIndex)
              nextBase = insertBeforePageOpen(nextBase, earlier)
            } else {
              // The page may already contain a later segment. Keep the older
              // locally finalized rows in transcript order without granting
              // them authority over that open row.
              nextBase = insertBeforePageOpen(
                preserved, localSegments.slice(0, causalFinalizerIndex + 1),
              )
            }
            localSegments = localSegments.slice(causalFinalizerIndex + 1)
          } else {
            const activeLocal = localSegments[localSegments.length - 1]
            if (pageGenerationIsNew) {
              const beforeOpen = activeLocal.message.role === 'streaming'
                ? localSegments.slice(0, -1)
                : localSegments
              nextBase = insertBeforePageOpen(preserved, beforeOpen)
              localSegments = []
            } else if (activeLocal.message.role === 'streaming' && localMid(activeLocal) === undefined) {
              const anchor = compareSegments(pageSegment.index, activeLocal.index)
              if (anchor === 'same') {
                nextBase = [...preserved]
                nextBase[pageSegment.index] = localOpenIsAtLeastAsNew
                  ? activeLocal.message
                  : pageSegment.message
                const earlier = localSegments.slice(0, -1)
                nextBase = insertBeforePageOpen(nextBase, earlier)
                localSegments = []
              } else {
                nextBase = finalizePageOpen(preserved)
              }
            } else {
              nextBase = insertBeforePageOpen(preserved, localSegments)
              localSegments = []
            }
          }
        }
      }
      let next = insertLocalSegments(nextBase, localSegments)
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
      if (olderHead.length) next = [...olderHead, ...next]
      // The active slot's server snapshot flipping to running is a turn
      // start (see `ChatState.runEpoch`), as it is in
      // syncSlotRunningFromServer; the hand-back in `enterActiveSlot` reads
      // the epoch to tell a turn that ran on screen from a slot that saw
      // nothing.
      if (running && !state.slotRunning) bumpRunEpoch(state, key)
      state.slotRunning = running
      state.slotStopping = action.payload.stopping ?? false
      state.pendingTurnSlot = null
      // Seed the replay guard from the PURE fetched page: its trailing
      // streaming row carries the newest chunk seq the server folded into
      // it, so a live chunk racing this snapshot is dropped, not re-appended.
      // Seqs are the slot's and never restart, so a snapshot from an earlier
      // turn can only sit at or below the live floor (raise, never lower). A
      // snapshot of a slot that is NOT running says no stream is in flight:
      // the floor is cleared, so a gateway restart (which does restart the
      // counter) cannot leave a stale floor over the next turn's chunks.
      if (running) {
        state.lastChunkSeq = raiseChunkSeq(floorForGen(state.lastChunkSeq, state.lastChunkGen, snapGen), snapSeq)
        if (snapGen !== undefined) state.lastChunkGen = snapGen
      } else {
        state.lastChunkSeq = undefined
      }
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
       * in" and `reinsertThinkingOrphans` needs a complete window to trust a
       * text anchor. `next` carries the retained head, so the loaded window is
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
      const requestId = action.meta?.requestId
      const chunkClaim = state.slotSwitchChunkClaim
      const ownsChunkClaim = chunkClaim != null
        && chunkClaim.requestId === requestId && chunkClaim.target === target
      const appliedRefreshSeq = isUnsafeKey(target)
        ? 0
        : state.refreshAppliedSeq?.[safeKey(target)] ?? 0
      if (ownsChunkClaim && chunkClaim && appliedRefreshSeq > chunkClaim.refreshIssuedSeq) {
        // Same authority rule as fulfilled: a refresh issued after this switch
        // has already installed the active transcript and cursor. A rejection
        // from the older switch settles only its request; unwinding here would
        // erase the newer refresh with no response page at all.
        chunkClaim.settled = true
        if (state.slotSwitchRequestId === requestId) {
          state.slotSwitchRequestId = null
          state.slotSwitchTarget = null
          state.slotSwitchOrigin = null
          if (state.activeSlot === target) state.slotLoading = false
        }
        return
      }
      if (chunkClaim?.target === target && (!ownsChunkClaim || chunkClaim.settled)) return
      if (ownsChunkClaim && chunkClaim) chunkClaim.settled = true
      const claimed = state.slotSwitchRequestId !== null && state.slotSwitchRequestId === requestId
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
        // The floor is per slot: park whatever the target accrued on its run
        // entry and take the origin's back from where `pending` parked it.
        const runs = (state.slotRun ??= {})
        if (!isUnsafeKey(target)) {
          const gone = (runs[safeKey(target)] ??= { state: 'idle' })
          gone.lastChunkSeq = raiseChunkSeq(floorForGen(gone.lastChunkSeq, gone.lastChunkGen, state.lastChunkGen), state.lastChunkSeq)
          if (state.lastChunkGen !== undefined) gone.lastChunkGen = state.lastChunkGen
        }
        state.lastChunkSeq = runs[safeKey(origin.key)]?.lastChunkSeq
        state.lastChunkGen = runs[safeKey(origin.key)]?.lastChunkGen
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
}

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
import { deduplicateByMid, floorForGen, olderHeadAbovePage, raiseChunkSeq, sameTranscript, serverRowCount, snapshotChunkGen, snapshotChunkSeq } from './transcript'
import { abortActiveOlderFetch, pagingCursorAfterKeptHead, slotCoverageShortfall, slotSwitchFetchLimit } from './paging'
import { mergePreservedThinking, reinsertThinkingOrphans, type ThinkingAnchor } from './thinking'
import { bumpRunEpoch, enterActiveSlot, pushHistory } from './runState'
import { retainServerTotal, seedContextUsage, setPagingCursor, writeSlotPage } from './slotCache'
import { hydrateQueuedBubbles } from './queue'
import { loadSlotActivity } from './activity'

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
        // Emit only while this request still owns the slot switch: a rapid
        // A->B switch leaves A's fetch resolving after B took over, and A's
        // transcript never rendered — relaying its read would clear sibling
        // badges for messages nobody displayed. `pending` assigns activeSlot
        // atomically before this thunk body runs, so a superseded request
        // observes someone else's key here.
        if ((getState() as { chat: ChatState }).chat.activeSlot === key) emitSlotRead(key, _newestSlotTs())
        return { ...wide, comparableTotal: first.total }
      }
      if ((getState() as { chat: ChatState }).chat.activeSlot === key) emitSlotRead(key, _newestSlotTs())
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
      if (typeof status === 'number') {
        const payload: StatusRejection = { status, message: errMessage(e) }
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
      if (typeof arg === 'object' && arg !== null && arg.announceOnMissing === true
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
        const snapGen = snapshotChunkGen(messages)
        state.lastChunkSeq = raiseChunkSeq(floorForGen(state.lastChunkSeq, state.lastChunkGen, snapGen), snapshotChunkSeq(messages))
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

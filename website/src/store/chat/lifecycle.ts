/** Opening and closing sessions around the open tabs: the Older-sessions
 *  history list and its paging, resume and delete of a history row (with the
 *  notices a failed attempt leaves), and creating or forking a slot. `deleteSlot`
 *  stays in chatSlice.ts, where its fallback navigation dispatches `switchSlot`. */
import { createAsyncThunk, type ActionReducerMapBuilder } from '@reduxjs/toolkit'
import type { RootState } from '../index'
import type { ChatSlot, SessionInfo } from '../../types'
import { api } from '../../api/client'
import { resolveDefaultMemoryMode } from '../../api/queryClient'
import { addSlotOptimistic, updateSlot } from '../dashboardSlice'
import { resolveDefaultColor } from '../../utils/sessionColors'
import { isChatPageSurface } from '../../utils/channelOrigin'
import { mergePreservedPastes } from '../../utils/pasteTokens'
import { findReport, parseErrorCode } from '../../utils/errorReport'
import type { HistoryDeleteRefusal } from '../../utils/historyDeleteRefusal'
import type { ChatState } from './state'
import { filterMessages, safeKey } from './wire'
import { enterActiveSlot, pushHistory } from './runState'
import { parkActiveTranscript, setPagingCursor } from './slotCache'

export const fetchHistory = createAsyncThunk(
  'chat/fetchHistory',
  async (append: boolean, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    const offset = append ? state.historyOffset : 0
    // Older sessions is the complement of the open tabs listed above it, so the
    // server drops anything a live slot already holds. `user_only` drops the
    // machine namespaces on top of that: a subagent or workflow transcript is
    // not a conversation the reader ever addressed, and having no title it would
    // render its own storage key as the row label. Both excluded server-side
    // because `historyOffset` advances by the row count received: dropping rows
    // here would desynchronise the offset and skip or repeat rows on the next page.
    const d = await api.sessions(30, offset, false, true, true)
    return { sessions: (d.sessions || d) as SessionInfo[], hasMore: d.has_more || false, offset, append }
  },
)

const configuredDefaultMemoryMode = () =>
  resolveDefaultMemoryMode(() => api.dashboardConfig())

export const createSlot = createAsyncThunk<
  ChatSlot,
  { agent?: string; agent_kind?: 'member' | 'template'; model?: string; mode?: string; memory_mode?: string; folder_id?: string | null; title?: string; color_index?: number | null; color_hex?: string | null; project?: string | null; activate?: boolean; instanceId?: string; adoptRemoteSlot?: string } | string | undefined,
  { fulfilledMeta: { originActiveSlot: string | null; activate: boolean } }
>(
  'chat/createSlot',
  async (opts, { getState, fulfillWithValue }) => {
    const agent = typeof opts === 'string' ? opts : opts?.agent
    // The namespace the agent was picked from; rides with the name so a
    // same-name member and template create different sessions.
    const agentKind = typeof opts === 'string' ? undefined : opts?.agent_kind
    const model = typeof opts === 'string' ? undefined : opts?.model
    const mode = typeof opts === 'string' ? undefined : opts?.mode
    const requestedMemoryMode = typeof opts === 'string' ? undefined : opts?.memory_mode
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
    // ADOPT an EXISTING peer session instead of minting a new one on the peer: the
    // value is that session's own slot key, as listed by
    // `GET /api/instances/{id}/chat-slots`. The local slot created here is fresh
    // either way — only what it binds to changes — so this rides the same create
    // round-trip rather than a second route. Meaningless without `instanceId`
    // (the peer that owns the key), which the backend refuses with
    // `400 adopt_needs_instance` rather than guessing an owner.
    const adoptRemoteSlot = typeof opts === 'string' ? undefined : opts?.adoptRemoteSlot
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
    // An explicit Incognito/Temporary menu choice wins. All other dashboard chat
    // entry points resolve the persisted preference here, before the first turn
    // can read or write memory. An ADOPT skips the resolution entirely: the
    // adopted slot inherits the PEER session's mode (see `api.createChatSlot`),
    // and resolving a local default here would only race it.
    const memory_mode = requestedMemoryMode
      || (adoptRemoteSlot ? undefined : await configuredDefaultMemoryMode())
    const slot = await api.createChatSlot(undefined, agent, model, mode, memory_mode, title, undefined, folderId || undefined, instanceId, adoptRemoteSlot, agentKind)
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
      // (dashboardSlice's createSlot.fulfilled matcher) makes the slot
      // selectable (and, when activated,
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
    // No `addSlotOptimistic` here: dashboardSlice registers the slot on this
    // thunk's `fulfilled` action, so the row and the activation land in one
    // commit. Everything that must precede publication (colour, project
    // scope) has already been awaited above.
    // Carry the origin slot in the action meta (fulfillWithValue) rather than on
    // the payload, so it can never leak into the persisted slot object. The
    // fulfilled reducer reads action.meta.originActiveSlot to decide whether
    // activating the new slot is safe.
    return fulfillWithValue(slot, { originActiveSlot, activate })
  },
)

export const resumeFromHistory = createAsyncThunk(
  'chat/resumeFromHistory',
  async ({ key, title, expectedCreatedAt }: { key: string; title: string; expectedCreatedAt?: string }, { dispatch }) => {
    const d = await (expectedCreatedAt === undefined
      ? api.resumeChatSlot(key, title)
      : api.resumeChatSlot(key, title, expectedCreatedAt))
    if (d.ok) {
      dispatch(addSlotOptimistic({ key: d.key, title: title || d.key, messages: 0, running: false, memory_mode: d.memory_mode, mode: d.mode, surface: d.surface ?? d.mode, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }))
      dispatch(updateSlot({ key: d.key, mode: d.mode, surface: d.surface ?? d.mode }))
    }
    // Without a cursor this response cannot be paged, so do not advertise more:
    // a zero cursor beside hasMore renders an affordance that loads nothing.
    const cursor = typeof d.next_before === 'number' ? d.next_before : null
    // `surface` (falling back to `mode`) is returned so a caller resuming from
    // a surface that cannot display every slot (ChatPage's unified view only
    // shows default/orchestrator, see isChatPageSurface) can tell a
    // silently-unusable resume apart from a genuinely failed one (#3624) --
    // the request succeeds either way, so `ok` alone cannot distinguish them.
    return { ok: d.ok, key: d.key, surface: d.surface ?? d.mode, nextBefore: cursor ?? 0, messages: filterMessages(d.messages || []), hasMore: cursor !== null && (d.has_more || false), total: d.total || 0 }
  },
)

export const forkSlot = createAsyncThunk(
  'chat/forkSlot',
  async (
    { slot, atIndex, messageId, prompt, mode, direction }: { slot: string; atIndex?: number; messageId?: string; prompt?: string; mode?: string; direction?: 'head' | 'tail' },
    { dispatch },
  ) => {
    const d = messageId
      ? await api.forkChatSlot(slot, atIndex, prompt, mode, direction, messageId)
      : await api.forkChatSlot(slot, atIndex, prompt, mode, direction)
    if (d.ok) {
      // memory_mode is the parent's, echoed by the server; without it the new
      // tab would read as persistent until the next slots refresh.
      dispatch(addSlotOptimistic({ key: d.key, title: d.title || d.key, messages: d.messages || 0, running: false, folder_id: d.folder_id, memory_mode: d.memory_mode }))
    }
    return d
  },
)

/** Delete a history row. A refusal REJECTS WITH A VALUE rather than throwing:
 *  `api.deleteSession` throws an `ApiError` on any non-2xx, and the thunk
 *  boundary's `miniSerializeError` keeps string fields only, so a rethrow
 *  would reach the reducer as a bare message with the status and body gone
 *  (see utils/thunkError). The payload carries what the notice renders from:
 *  the row's key and title, plus the gateway's machine-readable `code` (`''`
 *  when the body carried none -- a dropped connection, a 5xx). The title is
 *  read from `history` HERE, while the row is still there to read. */
export const deleteHistorySession = createAsyncThunk<
  string,
  string,
  { rejectValue: HistoryDeleteRefusal }
>(
  'chat/deleteHistorySession',
  async (key, { getState, rejectWithValue }) => {
    try {
      await api.deleteSession(key)
      return key
    } catch (e) {
      // Duck-typed on `body`, not `instanceof ApiError`, so a mocked transport
      // (`Object.assign(new Error(), { status, body })`) reads the same way.
      const body = (e as { body?: unknown } | null)?.body
      const title = (getState() as { chat: ChatState }).chat.history.find(s => s.key === key)?.title ?? ''
      const code = parseErrorCode(typeof body === 'string' ? body : undefined) ?? ''
      const report = findReport(e instanceof Error ? e.message : '')
      const refusal: HistoryDeleteRefusal = { key, title, code }
      return rejectWithValue(report ? { ...refusal, report } : refusal)
    }
  },
)

export const historyNoticeReducers = {
  /** Dismiss the unresumable-surface notice (#5925). Deliberately does NOT
   *  clear `lastResumeRequestId`: that ordering token belongs to the resume
   *  in flight, and forgetting it would let an older resume's late answer
   *  re-open a notice the user just closed. */
  clearUnresumableResume(state: ChatState) { state.unresumableResume = null },
  /** Dismiss the refused-delete notice. The row stays in `history`: nothing
   *  was deleted, and the user retries from the sidebar as before. */
  clearUndeletableHistory(state: ChatState) { state.undeletableHistory = null },
}


export function addLifecycleCases(builder: ActionReducerMapBuilder<ChatState>): void {
  builder
    .addCase(fetchHistory.fulfilled, (state, action) => {
      const { sessions, hasMore, offset, append } = action.payload
      state.history = append ? [...state.history, ...sessions] : sessions
      state.historyHasMore = hasMore
      state.historyOffset = offset + sessions.length
    })
    .addCase(createSlot.pending, (state, action) => {
      state.creatingSlot = true
      const arg = action.meta.arg
      if (typeof arg === 'string' || arg?.activate !== false) {
        state.foregroundCreateId = action.meta.requestId
        state.lastCreatedActivation = null
      }
    })
    .addCase(createSlot.rejected, (state, action) => {
      state.creatingSlot = false
      if (state.foregroundCreateId === action.meta.requestId) state.foregroundCreateId = null
    })
    .addCase(createSlot.fulfilled, (state, action) => {
      // The create POST resolved, so clear the pending flag regardless of
      // whether we activate below. Otherwise the switched-away early-return
      // would strand the "Creating…" spinner on forever.
      state.creatingSlot = false
      if (state.foregroundCreateId === action.meta.requestId) state.foregroundCreateId = null
      // Switched-away guard: if the user moved to a different
      // session while this create was pending (a slow "Creating…" under memory
      // pressure), do NOT hijack the view. The new slot is registered by
      // dashboardSlice on this same action; just leave the user where they are. Mirrors the
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
        parkActiveTranscript(state)
      }
      enterActiveSlot(state, action.payload.key)
      state.lastCreatedActivation = { slot: action.payload.key, requestId: action.meta.requestId }
      // The replay floor belongs to the slot that was streaming, not to this
      // one. `state.lastChunkSeq` is the ACTIVE slot's floor, and a brand-new
      // chat has no replay history at all — carrying the outgoing slot's floor
      // in makes this slot's own opening chunks look like replays (they share
      // the process generation, so `floorForGen` keeps the floor) and the
      // reducer drops them. Cleared rather than parked-and-restored, because
      // there is nothing to restore for a slot that has never streamed.
      state.lastChunkSeq = undefined
      state.lastChunkGen = undefined
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
            parkActiveTranscript(state)
          }
        }
        const cached = state.slotActivity[action.payload.key]
        state.toolLog = cached?.toolLog ?? []
        state.subagents = cached?.subagents ?? {}
        // Legacy cached 'tools'/'nav'/'files' values fall back to 'changes'
        // (see switchSlot for why 'files' is no longer one of these tabs).
        state.activityTab = (cached?.activityTab && !['tools', 'nav', 'files'].includes(cached.activityTab as string)) ? cached.activityTab : 'changes'
        state.activityOpen = cached?.activityOpen ?? false
        // Same handover switchSlot performs: the floor is per-slot, so entering
        // a slot restores ITS parked floor (undefined when it has none) instead
        // of inheriting the one belonging to the slot being left. Without this a
        // resume into a quiet slot kept the streaming slot's floor and discarded
        // the resumed slot's first chunks.
        const resumedRun = state.slotRun[safeKey(action.payload.key)]
        state.lastChunkSeq = resumedRun?.lastChunkSeq
        state.lastChunkGen = resumedRun?.lastChunkGen
        enterActiveSlot(state, action.payload.key)
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
    .addCase(deleteHistorySession.pending, (state) => {
      // A fresh attempt supersedes the last refusal's notice, whichever row it
      // named: the outcome of THIS click is what the user is now waiting on.
      state.undeletableHistory = null
    })
    .addCase(deleteHistorySession.rejected, (state, action) => {
      // The row is deliberately NOT filtered out: the gateway kept the file,
      // so the sidebar must keep the row. Only the notice changes.
      if (action.payload) state.undeletableHistory = action.payload
    })
}

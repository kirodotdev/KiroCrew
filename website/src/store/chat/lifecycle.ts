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
import { errMessage } from '../../utils/thunkError'
import { writePrefill } from '../../utils/navIntent'
import type { HistoryDeleteRefusal } from '../../utils/historyDeleteRefusal'
import { entryActivityOpen, type ChatState } from './state'
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
  { agent?: string; agent_kind?: 'member' | 'template'; appAgentOwner?: string; model?: string; mode?: string; memory_mode?: string; folder_id?: string | null; title?: string; color_index?: number | null; color_hex?: string | null; project?: string | null; activate?: boolean; sessionFolder?: boolean; defaultFolder?: boolean; prefill?: string; automatic?: boolean } | string | undefined,
  { fulfilledMeta: Required<OriginMeta> & { activate: boolean; prefillStaged?: boolean; automatic?: boolean } }
>(
  'chat/createSlot',
  async (opts, { getState, fulfillWithValue }) => {
    const agent = typeof opts === 'string' ? opts : opts?.agent
    // The namespace the agent was picked from; rides with the name so a
    // same-name member and template create different sessions.
    const agentKind = typeof opts === 'string' ? undefined : opts?.agent_kind
    // The app that contributed the command naming this agent; see api.createChatSlot.
    const appAgentOwner = typeof opts === 'string' ? undefined : opts?.appAgentOwner
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
    // `activate: false` creates the session WITHOUT stealing focus, so a caller
    // that must finish setting the slot up (e.g. scoping it to a worktree) can
    // do so before the user is able to type into it. Defaults to true — every
    // existing caller keeps the create-and-focus behaviour.
    const activate = typeof opts === 'string' ? true : opts?.activate !== false
    // `automatic` is the chat page's own create for an empty list. Its
    // activation counts in `activeSlotAutoMoves`, like the page's auto-select,
    // so a create or fork the user started meanwhile may still open.
    const automatic = typeof opts === 'object' && opts.automatic === true
    // `sessionFolder: false` is for a purpose-built chat (an app's session, an
    // intake, a hand-off) that should keep the default project even when the
    // owner turned on a fresh folder per new chat. A carried project opts out
    // too: the server would otherwise make a folder the carry then replaces.
    const skipSessionFolder = !!project || (typeof opts !== 'string' && opts?.sessionFolder === false)
    // The person's default sidebar folder is OPT-IN: only the plain New entry
    // points pass `defaultFolder: true`, so a create path that has not chosen
    // it (an app's launch, a hand-off, a recreated slot) keeps today's unfiled
    // placement instead of moving into another folder and its project. A
    // folder or project named on the same call still wins over the default.
    const useDefaultFolder = typeof opts === 'object' && opts.defaultFolder === true
      && opts.folder_id === undefined && !project
    // Capture the active slot and its move count BEFORE the (potentially slow)
    // create round-trip. The fulfilled reducer checks both with `forkMayOpen`:
    // if the user opened another session while the create was pending (e.g.
    // New Chat spun on "Creating" under memory pressure and they moved to
    // another tab), or went to one and came back, the new slot must NOT
    // take the view.
    const origin = captureForkOrigin(getState() as RootState)
    // An explicit Incognito/Temporary menu choice wins. All other dashboard chat
    // entry points resolve the persisted preference here, before the first turn
    // can read or write memory.
    const memory_mode = requestedMemoryMode || await configuredDefaultMemoryMode()
    const slot = await api.createChatSlot(undefined, agent, model, mode, memory_mode, title, undefined, folderId || undefined, agentKind, skipSessionFolder, appAgentOwner, useDefaultFolder)
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
    // fulfilled reducer reads action.meta.originActiveSlot and the two move
    // counts to decide whether activating the new slot is safe.
    //
    // `prefill` seeds the new session's composer. It is staged HERE, before the
    // fulfilled action, because the composer's slot-restore effect reads
    // `kirocrew_prefill` once, on the commit that activates the slot. A caller
    // writing it after awaiting this thunk lands after that read and is never
    // consumed. `prefillStaged` is false when storage refused the write, so the
    // caller can fall back to another channel.
    const prefill = typeof opts === 'string' ? undefined : opts?.prefill
    const prefillStaged = !!prefill && writePrefill(slot.key, prefill)
    return fulfillWithValue(slot, { ...originMeta(origin), activate, prefillStaged, automatic })
  },
)

/** How long a create, fork or resume started in this window counts as on its
 *  way. Every measured create and fork answered within 3.5 s; a request still
 *  open after this is treated as hung and keeps running. The chat page, having
 *  lost its chat, waits this long for such a request to open one before it
 *  auto-picks as usual, and a resume stops blocking a repeat of its row after
 *  it. */
export const PENDING_OPEN_LEASE_MS = 10_000

export const resumeFromHistory = createAsyncThunk<
  { ok: boolean; key: string; surface: string; nextBefore: number; messages: ReturnType<typeof filterMessages>; hasMore: boolean; total: number; folderId: string },
  { key: string; title: string },
  { pendingMeta: { startedAt: number }; fulfilledMeta: Required<OriginMeta> }
>(
  'chat/resumeFromHistory',
  async ({ key, title }, { dispatch, getState, fulfillWithValue }) => {
    // Where the user stands when the resume starts, as `createSlot` captures
    // it. A resume rebuilds the slot from its transcript and can take seconds;
    // the fulfilled reducer asks `forkMayOpen` before it opens the session.
    const origin = captureForkOrigin(getState() as RootState)
    const d = await api.resumeChatSlot(key, title)
    if (d.ok) {
      dispatch(addSlotOptimistic({ key: d.key, title: title || d.key, messages: 0, running: false, memory_mode: d.memory_mode, mode: d.mode, surface: d.surface ?? d.mode, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...(d.folder_id ? { folder_id: d.folder_id } : {}) }))
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
    return fulfillWithValue(
      { ok: d.ok, key: d.key, surface: d.surface ?? d.mode, nextBefore: cursor ?? 0, messages: filterMessages(d.messages || []), hasMore: cursor !== null && (d.has_more || false), total: d.total || 0, folderId: typeof d.folder_id === 'string' ? d.folder_id : '' },
      originMeta(origin),
    )
  },
  {
    // A repeat of a resume already on its way (a double click or a double
    // Enter on a slow row) is dropped here, for every entry point at once,
    // whatever other rows were resumed in between. Only for the lease: the
    // POST has no deadline, and a request that never answers must not leave
    // its row dead for good. The drop is dispatched as a rejection so the
    // reducer can hand the latest-click token back to the resume it repeats.
    condition: ({ key }, { getState }) => {
      const newest = newestResumeOf((getState() as RootState).chat, key)
      return !newest || Date.now() - newest.startedAt >= PENDING_OPEN_LEASE_MS
    },
    dispatchConditionRejection: true,
    getPendingMeta: () => ({ startedAt: Date.now() }),
  },
)

/** Whether a resume started in this window is still within its lease at
 *  `now`. An entry past the lease is a hung request, not one on its way. */
export function resumeStillOpening(chat: Pick<ChatState, 'pendingResumes'>, now: number): boolean {
  return Object.values(chat.pendingResumes ?? {}).some(p => now - p.startedAt < PENDING_OPEN_LEASE_MS)
}

/** The newest resume of `key` still on its way, if any. */
function newestResumeOf(chat: Pick<ChatState, 'pendingResumes'>, key: string): { requestId: string; startedAt: number } | null {
  let newest: { requestId: string; startedAt: number } | null = null
  for (const [requestId, p] of Object.entries(chat.pendingResumes ?? {})) {
    if (p.key === key && (!newest || p.startedAt >= newest.startedAt)) newest = { requestId, startedAt: p.startedAt }
  }
  return newest
}

/** The action that lists a successful fork in the sidebar. Every fork path
 *  dispatches this before activating the fork: the chat page clears an active
 *  session its slot list lacks. `openCreatedSlot` uses it the same way for a
 *  session another server route created. memory_mode is the parent's, echoed by the
 *  server; without it the new tab would read as persistent until the next
 *  slots refresh. */
export function listForkedSlot(d: Pick<ChatSlot, 'key'> & Partial<Pick<ChatSlot, 'title' | 'messages' | 'folder_id' | 'memory_mode' | 'pinned'>>) {
  return addSlotOptimistic({ key: d.key, title: d.title || d.key, messages: d.messages || 0, running: false, folder_id: d.folder_id, memory_mode: d.memory_mode, ...(d.pinned ? { pinned: true } : {}) })
}

/** Where the user stood when a fork or create started; see `forkMayOpen`. */
export interface ForkOrigin { activeSlot: string | null; moves: number; autoMoves: number; requestMoves: number; seq: number; burst: number }

export function captureForkOrigin(state: Pick<RootState, 'chat'>): ForkOrigin {
  const c = state.chat
  return { activeSlot: c.activeSlot, moves: c.activeSlotMoves ?? 0, autoMoves: c.activeSlotAutoMoves ?? 0, requestMoves: c.activeSlotRequestMoves ?? 0, seq: c.requestSeq ?? 0, burst: c.requestBurst ?? 0 }
}

/** The origin a create or resume thunk sent in its meta. An action without
 *  the request fields (a fixture, an older caller) reads as one that started
 *  now, after every other request. */
export function originFromMeta(state: ChatState, meta: OriginMeta): ForkOrigin {
  return {
    activeSlot: meta.originActiveSlot ?? null,
    moves: meta.originActiveSlotMoves ?? 0,
    autoMoves: meta.originActiveSlotAutoMoves ?? 0,
    requestMoves: meta.originActiveSlotRequestMoves ?? state.activeSlotRequestMoves ?? 0,
    seq: meta.originRequestSeq ?? state.requestSeq ?? 0,
    burst: meta.originRequestBurst ?? state.requestBurst ?? 0,
  }
}

export interface OriginMeta {
  originActiveSlot?: string | null
  originActiveSlotMoves?: number
  originActiveSlotAutoMoves?: number
  originActiveSlotRequestMoves?: number
  originRequestSeq?: number
  originRequestBurst?: number
}

function originMeta(origin: ForkOrigin): Required<OriginMeta> {
  return {
    originActiveSlot: origin.activeSlot,
    originActiveSlotMoves: origin.moves,
    originActiveSlotAutoMoves: origin.autoMoves,
    originActiveSlotRequestMoves: origin.requestMoves,
    originRequestSeq: origin.seq,
    originRequestBurst: origin.burst,
  }
}

/** Records that a request with `origin` opened its session. */
function noteRequestActivation(state: ChatState, origin: ForkOrigin): void {
  state.activatedRequestSeq = Math.max(state.activatedRequestSeq ?? 0, origin.seq)
}

/** Whether a fork or create that just resolved may open its new session.
 *  Every fork caller and `createSlot` capture the origin when the request
 *  starts and ask this before they switch: the request can take seconds, and
 *  a session the user opened in that time is a choice the late response must
 *  not undo. The move count also refuses a user who went to another session
 *  and came back, since coming back is a choice too; an equal `activeSlot`
 *  alone cannot tell that apart from never moving. A move the chat page made
 *  on its own, after it lost the origin from its list, is not a choice, and
 *  the new session still opens. Nor is a move made by another create, fork or
 *  resume opening its session: that request was clicked before this one, so
 *  this one still opens, while a request clicked after this one that has
 *  already opened its session keeps the view. The new session is listed in
 *  the sidebar either way; only the switch, and what the caller does on
 *  opening it, is skipped. */
export function forkMayOpen(state: Pick<RootState, 'chat'>, origin: ForkOrigin): boolean {
  const c = state.chat
  if ((c.activeSlotMoves ?? 0) !== origin.moves) return false
  if ((c.activatedRequestSeq ?? 0) > origin.seq) return false
  return c.activeSlot === origin.activeSlot
    || (c.activeSlotAutoMoves ?? 0) !== origin.autoMoves
    || (c.activeSlotRequestMoves ?? 0) !== origin.requestMoves
}

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
      // `miniSerializeError` at the thunk boundary drops the backend `code`, so
      // reject with it as a value. The check is STRUCTURAL (duck-typed on
      // `status`/`body`), so a mocked transport reads the same as an ApiError.
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
    if (d.ok) dispatch(listForkedSlot(d))
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

/** The Older Sessions "Delete all". It rejects with a value in BOTH failure
 *  shapes, so the same `undeletableHistory` notice narrates them: a refused
 *  request (`api.clearSessions` throws on any non-2xx, nothing was deleted),
 *  and a 200 whose body says `ok: false` because some rows stayed on disk.
 *  Titles are read from `history` HERE, before the refresh drops the rows
 *  that did go. */
export const clearHistorySessions = createAsyncThunk<
  void,
  void,
  { rejectValue: HistoryDeleteRefusal }
>(
  'chat/clearHistorySessions',
  async (_, { getState, rejectWithValue }) => {
    let body: unknown
    try {
      // `user_only`, the same flag `fetchHistory` lists with: Delete all removes
      // only rows this pane could show, never hidden machine transcripts.
      body = await api.clearSessions(true)
    } catch (e) {
      const errBody = (e as { body?: unknown } | null)?.body
      const code = parseErrorCode(typeof errBody === 'string' ? errBody : undefined) ?? ''
      const report = findReport(e instanceof Error ? e.message : '')
      const refusal: HistoryDeleteRefusal = { key: '', title: '', code, bulk: { left: null, cleared: 0, rows: [] } }
      return rejectWithValue(report ? { ...refusal, report } : refusal)
    }
    const outcome = (body ?? {}) as { ok?: unknown; cleared?: unknown; failed?: unknown; undeletable?: unknown }
    if (outcome.ok !== false) return
    const history = (getState() as { chat: ChatState }).chat.history
    const undeletable = Array.isArray(outcome.undeletable) ? outcome.undeletable : []
    // Every entry counts as left behind; only one with an id can be named.
    const rows = undeletable.flatMap(row => {
      const { id, code } = (row ?? {}) as { id?: unknown; code?: unknown }
      if (typeof id !== 'string' || !id) return []
      const title = history.find(s => s.key === id)?.title ?? ''
      return [{ key: id, title, code: typeof code === 'string' ? code : '' }]
    })
    const failed = typeof outcome.failed === 'number' && outcome.failed > 0 ? outcome.failed : 0
    const cleared = typeof outcome.cleared === 'number' && outcome.cleared > 0 ? outcome.cleared : 0
    return rejectWithValue({ key: '', title: '', code: '', bulk: { left: failed + undeletable.length, cleared, rows } })
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
      state.requestSeq = (state.requestSeq ?? 0) + 1
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
      // Switched-away guard, the same rule forks follow (`forkMayOpen`): if
      // the user opened another session while this create was pending (a slow
      // "Creating…" under memory pressure), do NOT hijack the view, even when
      // they have since come back to the origin. The new slot is registered by
      // dashboardSlice on this same action; just leave the user where they
      // are. `send()`'s forceNew path and welcome-screen New Chat both leave
      // the user on the origin with no move, so they still activate normally.
      //
      // Conscious edge: a rapid double New Chat is one request made twice, so
      // the first create to open keeps the view, and the text typed while
      // both ran stays where it was carried (create-carry in composerDrafts).
      // This create's pending action cleared `lastCreatedActivation`, so a
      // value naming another create means that one opened since this started.
      // Both slots exist in the sidebar. A resume or fork clicked after a
      // create still opens over it (`forkMayOpen`).
      // Caller asked for a background create (see `activate` above): the slot
      // is registered but focus stays put until the caller switches to it.
      if (action.meta.activate === false) return
      const origin = originFromMeta(state, action.meta)
      // Only for creates in one burst (no resume or fork clicked since the
      // first, however many creates came between), and only while the first
      // is still the newest request to have opened. A resume or fork clicked
      // between them, or opened after the first, makes this create the later
      // click, and it opens as any later click does.
      const opened = state.lastCreatedActivation
      if (opened && opened.requestId !== action.meta.requestId && opened.seq === (state.activatedRequestSeq ?? 0)
        && opened.burst !== undefined && opened.burst === origin.burst) return
      if (!forkMayOpen({ chat: state }, origin)) return
      if (state.activeSlot) {
        state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
        state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        parkActiveTranscript(state)
      }
      // An automatic create (#23412) is the page's own move, so it counts
      // as one and orders no request; any other create is a request opening.
      if (action.meta.automatic === true) enterActiveSlot(state, action.payload.key, true)
      else {
        enterActiveSlot(state, action.payload.key, 'request')
        noteRequestActivation(state, origin)
      }
      state.lastCreatedActivation = { slot: action.payload.key, requestId: action.meta.requestId, seq: origin.seq, burst: origin.burst }
      // A default-folder create learns its folder only from this response, so
      // no caller can open a collapsed default folder (or a collapsed
      // ancestor) before the create, the way a folder "+" create does. The
      // chat would then be active with its row hidden. Ask the sidebar to open
      // the row's folder path. `openFoldersOnly`: the user asked for a chat,
      // not a reveal, so their search and filters stay as they set them.
      // Placed after the activation guards above, so a background or
      // switched-away create never moves the sidebar.
      const arg = action.meta.arg
      if (typeof arg === 'object' && arg?.defaultFolder === true && action.payload.folder_id) {
        state.revealNonce += 1
        state.revealRequest = { kind: 'session', target: action.payload.key, nonce: state.revealNonce, openFoldersOnly: true, folderId: action.payload.folder_id }
      }
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
      // A brand-new chat enters like every other slot-entry path (switchSlot /
      // resumeFromHistory, which find no cached bucket for it): closed, or open
      // with Keep side panel open. Without this the panel state of the chat
      // being left leaked into the new one — and was not persisted under the new
      // slot's key either, so a reload silently closed it again.
      state.activityOpen = entryActivityOpen(state, true, undefined)
      state.slotRunning = false
      state.slotStopping = false
      state.slotState = 'idle'
      setPagingCursor(state, false, 0)
      // A switch still loading the chat being left, such as a close landing
      // made while this create was pending, no longer owns the pane. Retire
      // it: its answer is now superseded (`supersededSwitch`) and cannot
      // write this chat's view, and the new chat has nothing to load.
      state.slotLoading = false
      state.slotSwitchRequestId = null
      state.slotSwitchTarget = null
      state.slotSwitchOrigin = null
      state.slotSwitchLatestRequestId = action.meta.requestId
    })
    .addCase(resumeFromHistory.pending, (state, action) => {
      // A new attempt supersedes whatever the previous one narrated, and its
      // requestId becomes the only answer allowed to write the notice below.
      state.lastResumeRequestId = action.meta.requestId
      // Entries past the lease are hung requests that may never settle; drop
      // them so the map stays bounded. A late answer still lands, it only has
      // no entry left to remove.
      const startedAt = action.meta.startedAt
      const pendingResumes: NonNullable<ChatState['pendingResumes']> = {}
      for (const [id, p] of Object.entries(state.pendingResumes ?? {})) {
        if (startedAt - p.startedAt < PENDING_OPEN_LEASE_MS) pendingResumes[id] = p
      }
      state.requestSeq = (state.requestSeq ?? 0) + 1
      state.requestBurst = (state.requestBurst ?? 0) + 1
      pendingResumes[action.meta.requestId] = { key: action.meta.arg.key, startedAt, origin: captureForkOrigin({ chat: state }) }
      state.pendingResumes = pendingResumes
      state.unresumableResume = null
    })
    .addCase(resumeFromHistory.fulfilled, (state, action) => {
      const latest = action.meta.requestId === state.lastResumeRequestId
      const entry = state.pendingResumes?.[action.meta.requestId]
      if (entry) delete state.pendingResumes![action.meta.requestId]
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
        // The session is an open tab now either way; only taking the view is
        // conditional. A later resume is the user's newer pick, so an earlier
        // one answering after it must not open (latest click wins, the same
        // token that orders the notice). And a session the user opened while
        // this resume was pending, or a return to the origin, is a choice the
        // late answer must not undo: the rule creates and forks follow.
        // The entry's origin moves with a repeated click; the thunk's own
        // capture covers an answer whose entry was pruned as hung.
        const origin = entry?.origin ?? originFromMeta(state, action.meta)
        if (!latest || !forkMayOpen({ chat: state }, origin)) return
        noteRequestActivation(state, origin)
        state.lastResumeActivation = { requestId: action.meta.requestId, replaced: state.activeSlot }
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
        state.activityOpen = entryActivityOpen(state, state.activeSlot !== action.payload.key, cached)
        // Same handover switchSlot performs: the floor is per-slot, so entering
        // a slot restores ITS parked floor (undefined when it has none) instead
        // of inheriting the one belonging to the slot being left. Without this a
        // resume into a quiet slot kept the streaming slot's floor and discarded
        // the resumed slot's first chunks.
        const resumedRun = state.slotRun[safeKey(action.payload.key)]
        state.lastChunkSeq = resumedRun?.lastChunkSeq
        state.lastChunkGen = resumedRun?.lastChunkGen
        enterActiveSlot(state, action.payload.key, 'request')
        // A history session keeps the folder it was filed in, which can be
        // collapsed: open that path so the row of the session now in view
        // shows. Placed on the activation, so a resume that does not take the
        // view asks for nothing.
        if (action.payload.folderId) {
          state.revealNonce += 1
          state.revealRequest = { kind: 'session', target: action.payload.key, nonce: state.revealNonce, openFoldersOnly: true, folderId: action.payload.folderId }
        }
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
      if (action.meta.condition) {
        // A repeat dropped by `condition`: nothing was sent and nothing
        // failed. The click is still the user's newest pick, so the resume it
        // repeats becomes the latest again and opens when it answers, judged
        // from where the user stands now, even when another row was resumed
        // in between.
        const repeated = newestResumeOf(state, action.meta.arg.key)
        if (repeated) {
          state.lastResumeRequestId = repeated.requestId
          // The repeat is a click of its own, after every request started so far.
          state.requestSeq = (state.requestSeq ?? 0) + 1
          state.requestBurst = (state.requestBurst ?? 0) + 1
          state.pendingResumes![repeated.requestId].origin = captureForkOrigin({ chat: state })
          // A notice an in-between resume raised names a row the user has
          // moved past, as a fresh click would clear it.
          state.unresumableResume = null
        }
        return
      }
      if (state.pendingResumes) delete state.pendingResumes[action.meta.requestId]
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
    // "Delete all" shares the notice: a new attempt clears the last one, and a
    // refused or partial clear sets it. The caller refetches history either way.
    .addCase(clearHistorySessions.pending, (state) => {
      state.undeletableHistory = null
    })
    .addCase(clearHistorySessions.rejected, (state, action) => {
      if (action.payload) state.undeletableHistory = action.payload
    })
}

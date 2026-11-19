/**
 * The `chatSlice` module's public surface.
 *
 * Every consumer imports the chat state through this one module, so what it
 * exports is a contract: every name another module imports from it must resolve,
 * every action creator's `type` string is `chat/<name>`, every thunk keeps a
 * `chat/` `typePrefix` (the dashboard slice matches `chat/createSlot/fulfilled`
 * and the `deleteSlot` lifecycle by string, so each such literal must name a real
 * action), the exported constants keep the values the backend shares, and the
 * default reducer produces the declared initial state. The sets are read from the
 * module and from its importers, so adding an export does not edit this file.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as chatSlice from './chatSlice'
import reducer, {
  isAnsweredQuestionEnding,
  markQuestionSettled,
  selectSidebarApprovalCounts,
  selectSidebarAutomationRunningKeys,
  selectSidebarSubagentCounts,
  selectSidebarWorkflowActive,
  selectSidebarWorkflowActiveKeys,
  selectSlotMessages,
  selectSlotPendingSpawnApprovals,
  selectSlotSubagents,
  selectSlotToolLog,
  selectSubagentActivityCount,
  setPendingInput,
  toggleActivity,
} from './chatSlice'
import type { RootState } from './index'
import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

// Values the backend and the transcript share: page caps the server enforces,
// the quiet-end tool name, and the workflow terminal statuses on the wire.
const CONSTANTS: Record<string, unknown> = {
  FOLDER_SUGGESTION_MAX_TURNS: 3,
  OLDER_PAGE_LIMIT: 100,
  OLDER_WALK_PAGE_LIMIT: 100,
  PANE_HYDRATE_LIMIT: 50,
  PENDING_OPEN_LEASE_MS: 10_000,
  QUIET_END_SERVER: 'kirocrew-core',
  QUIET_END_TOOL: 'nothing_to_do',
  REFRESH_LIMIT_CEILING: 500,
  SLOT_DETAIL_MAX_LIMIT: 500,
  TOOL_OUTPUT_MAX_CHARS: 64000,
  WINDOW_WALK_MAX_PAGES: 8,
  WORKFLOW_TERMINAL_STATUSES: ['finished', 'failed', 'cancelled'],
}

const surface = chatSlice as unknown as Record<string, unknown>
type Creator = { type: string; match: (a: unknown) => boolean }
type Thunk = { typePrefix: string; pending: { type: string }; fulfilled: { type: string }; rejected: { type: string } }
const exported = Object.entries(surface).filter(([name]) => name !== 'default')
const isCreator = (v: unknown): v is Creator =>
  typeof v === 'function' && typeof (v as Creator).type === 'string' && typeof (v as Creator).match === 'function'
const isThunk = (v: unknown): v is Thunk => typeof v === 'function' && typeof (v as Thunk).typePrefix === 'string'
const ACTION_CREATORS = exported.filter(([, v]) => isCreator(v)) as [string, Creator][]
const THUNKS = exported.filter(([, v]) => isThunk(v)) as [string, Thunk][]

const SRC = resolve(process.cwd(), 'src')
const sourceFiles = (readdirSync(SRC, { recursive: true }) as string[])
  .filter((f) => /\.tsx?$/.test(f))
  .map((f) => join(SRC, f))

/** Contract: the reducer's defaults, which the user sees on a fresh load, in their
 *  declared key order. A changed default is a behaviour change and edits this on
 *  purpose. `lastChunkSeq` and `lastChunkGen` are present as `undefined`, which JSON
 *  would hide. */
const INITIAL_STATE = {
  activeSlot: null, activeSlotMoves: 0, activeSlotAutoMoves: 0, activeSlotRequestMoves: 0, requestSeq: 0, requestBurst: 0, activatedRequestSeq: 0, lastResumeActivation: null, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
  slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, slotCursorKey: null,
  slotSwitchRequestId: null, slotSwitchTarget: null, slotSwitchLatestRequestId: null, slotSwitchOrigin: null, switchSlotGone: null,
  loadingOlder: false, slotOlderError: false, lastChunkSeq: undefined, lastChunkGen: undefined,
  _wsChunkedDuringFetch: false, liveFrameSeq: 0, refreshAppliedSeq: {}, _redeliveredFramesDropped: 0, history: [], historyHasMore: false,
  historyOffset: 0, unresumableResume: null, lastResumeRequestId: null, pendingResumes: {}, undeletableHistory: null,
  pendingInput: null, pendingInputSend: false, mainComposerAppend: null, agentSwitchNotice: null, creatingSlot: false, foregroundCreateId: null, pendingForkOpens: 0,
  lastCreatedActivation: null, slotContextPct: {}, slotContextTokens: {}, voicePlaying: false,
  voicePreparing: false, voicePaused: false, voiceAudio: null, subagents: {}, subagentQueued: {}, subagentQueuedReason: {}, subagentReplayUnconfirmed: null, automations: {},
  selectedSubagentId: null, toolLog: [], workflowRuns: {}, activityOpen: false, sidePanelAlwaysOpen: false, sidePanelAlwaysOpenSetting: null, sidePanelReopenSlot: null,
  activityTab: 'changes', activityTabRequest: 0, revealRequest: null, revealNonce: 0,
  focusToolCallId: null, mcpApps: {}, slotActivity: {}, slotMessages: {}, slotPaneHasMore: {},
  slotPaneBounded: {}, slotServerTotal: {}, slotServerTotalSeq: {}, thinkingOrphans: {},
  slotRun: {}, slotHydrated: {}, slotLoading: false, slotSide: {}, slotSideClosed: {}, slotSideRestored: {},
  slotHistory: [], slotsSnapshotSeen: false, slotListPartial: false, slotListHeldKeys: [], slotListHoldChannels: false, pendingQuestions: {}, questionRequestsInFlight: {}, questionsSettled: {}, restoredQuestionNotices: {}, followups: {},
  folderSuggestions: {}, stopPressedAt: {}, runEpoch: {}, activeRunEpochAtEntry: 0,
  endedTurn: {}, pendingTurnSlot: null,
  closeRefused: null,
}

describe('chatSlice public surface', () => {
  it('resolves every runtime name another module imports from it', () => {
    const wanted = new Map<string, string>()
    for (const file of sourceFiles) {
      const text = readFileSync(file, 'utf-8')
      for (const [, clause] of text.matchAll(/import\s+(?!type\b)((?:[\w$]+\s*,\s*)?\{[^}]*\}|[\w$]+)\s+from\s+'(?:\.\.?\/)+(?:store\/)?chatSlice'/g)) {
        const named = clause.match(/\{([^}]*)\}/)?.[1] ?? ''
        for (const raw of named.split(',')) {
          const part = raw.trim()
          if (!part || part.startsWith('type ')) continue
          wanted.set(part.split(/\s+as\s+/)[0], file)
        }
      }
    }
    expect(wanted.size, 'no importer of chatSlice found under src').toBeGreaterThan(0)
    for (const [name, file] of wanted) {
      expect(name in surface, `${file} imports ${name}, which chatSlice does not export`).toBe(true)
    }
  })

  it('every action creator carries its chat/<name> type and matches its own action', () => {
    expect(ACTION_CREATORS.length).toBeGreaterThan(0)
    for (const [name, creator] of ACTION_CREATORS) {
      expect(creator.type, name).toBe(`chat/${name}`)
      expect(creator.match({ type: `chat/${name}` }), name).toBe(true)
    }
  })

  it('every thunk keeps a unique chat/ typePrefix and its lifecycle action types', () => {
    expect(THUNKS.length).toBeGreaterThan(0)
    const prefixes = new Set<string>()
    for (const [name, thunk] of THUNKS) {
      const prefix = thunk.typePrefix
      expect(prefix, name).toMatch(/^chat\/\w+$/)
      expect(prefixes.has(prefix), `${name} reuses ${prefix}`).toBe(false)
      prefixes.add(prefix)
      expect(thunk.pending.type, name).toBe(`${prefix}/pending`)
      expect(thunk.fulfilled.type, name).toBe(`${prefix}/fulfilled`)
      expect(thunk.rejected.type, name).toBe(`${prefix}/rejected`)
    }
  })

  it('every chat action type another slice matches by string is one chatSlice dispatches', () => {
    const known = new Set([
      ...ACTION_CREATORS.map(([, c]) => c.type),
      ...THUNKS.flatMap(([, t]) => [t.pending.type, t.fulfilled.type, t.rejected.type]),
    ])
    const chatDir = join(SRC, 'store', 'chat')
    let seen = 0
    for (const file of sourceFiles) {
      if (/\.test\.tsx?$/.test(file) || file.startsWith(chatDir) || file.endsWith('chatSlice.ts')) continue
      if (!file.startsWith(join(SRC, 'store'))) continue
      for (const [, type] of readFileSync(file, 'utf-8').matchAll(/'(chat\/\w+(?:\/(?:pending|fulfilled|rejected))?)'/g)) {
        seen += 1
        expect(known.has(type), `${file} matches '${type}', which no chat action or thunk dispatches`).toBe(true)
      }
    }
    expect(seen, 'no cross-slice chat action literal found under src/store').toBeGreaterThan(0)
  })

  it('exported helpers and selectors are plain functions, constants keep their values', () => {
    for (const [name, value] of exported) {
      if (typeof value !== 'function' || isCreator(value) || isThunk(value)) continue
      expect((value as { type?: unknown }).type, name).toBeUndefined()
    }
    for (const [name, value] of Object.entries(CONSTANTS)) expect(surface[name], name).toEqual(value)
  })

  it('the default export is the reducer and produces the pinned initial state', () => {
    const init = reducer(undefined, { type: '@@INIT' })
    expect(Object.keys(init)).toEqual(Object.keys(INITIAL_STATE))
    expect(init).toStrictEqual(INITIAL_STATE)
  })

  it.each(['answered', 'composer', 'queued'])('treats %s as an answered question ending', reason => {
    expect(isAnsweredQuestionEnding(reason)).toBe(true)
  })

  it.each(['dismissed', 'withdrawn', 'expired', 'no_client', undefined])(
    'treats %s as an unanswered or unknown question ending',
    reason => {
      expect(isAnsweredQuestionEnding(reason)).toBe(false)
    },
  )

  it('keeps only the newest settled question ids', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    for (let i = 0; i < 201; i++) state = reducer(state, markQuestionSettled({ ask_id: `ask-${i}` }))
    expect(Object.keys(state.questionsSettled)).toHaveLength(200)
    expect(state.questionsSettled['ask-0']).toBeUndefined()
    expect(state.questionsSettled['ask-200']).toBe(true)

    state = reducer(state, markQuestionSettled({ ask_id: 'ask-1' }))
    state = reducer(state, markQuestionSettled({ ask_id: 'ask-new' }))
    expect(state.questionsSettled['ask-1']).toBe(true)
    expect(state.questionsSettled['ask-2']).toBeUndefined()
  })

  it('an unrelated action returns the state object itself', () => {
    const init = reducer(undefined, { type: '@@INIT' })
    expect(reducer(init, { type: 'chat/unknownActionType' })).toBe(init)
    expect(reducer(init, { type: 'dashboard/somethingElse' })).toBe(init)
  })
})

describe('persisted activity-panel keys', () => {
  afterEach(() => {
    localStorage.clear()
    vi.resetModules()
  })

  it('seeds slotActivity from every mc-activity-open:<slot> key present at module load', async () => {
    localStorage.setItem('mc-activity-open:alpha', 'true')
    localStorage.setItem('mc-activity-open:beta', 'false')
    localStorage.setItem('mc-activity-open:', 'true')
    localStorage.setItem('unrelated', 'true')
    vi.resetModules()
    const fresh = await import('./chatSlice')
    const init = fresh.default(undefined, { type: '@@INIT' })
    expect(init.slotActivity).toEqual({
      alpha: { toolLog: [], subagents: {}, activityOpen: true },
      beta: { toolLog: [], subagents: {}, activityOpen: false },
    })
  })

  it('writes the open/closed choice under mc-activity-open:<activeSlot>', () => {
    const init = reducer(undefined, { type: '@@INIT' })
    const active = reducer(init, { type: 'chat/setActiveSlot', payload: 'gamma' })
    const opened = reducer(active, toggleActivity())
    expect(opened.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:gamma')).toBe('true')
    const closed = reducer(opened, toggleActivity())
    expect(closed.activityOpen).toBe(false)
    expect(localStorage.getItem('mc-activity-open:gamma')).toBe('false')
  })
})

describe('selector identity', () => {
  const root = (chat: ReturnType<typeof reducer>): RootState => ({ chat, dashboard: { slots: [] } } as unknown as RootState)

  it('empty per-slot reads return one shared reference', () => {
    const state = root(reducer(undefined, { type: '@@INIT' }))
    expect(selectSlotMessages(state, 'nope')).toBe(selectSlotMessages(state, 'other'))
    expect(selectSlotToolLog(state, 'nope')).toBe(selectSlotToolLog(state, 'other'))
    expect(selectSlotSubagents(state, 'nope')).toBe(selectSlotSubagents(state, 'other'))
    expect(selectSlotPendingSpawnApprovals(state, 'nope')).toBe(selectSlotPendingSpawnApprovals(state, null))
  })

  it('memoized aggregates do not recompute across an unrelated state change', () => {
    const a = root(reducer(undefined, { type: '@@INIT' }))
    const b = root(reducer(a.chat, setPendingInput('draft')))
    expect(b.chat).not.toBe(a.chat)
    const memoized = {
      selectSidebarSubagentCounts, selectSidebarApprovalCounts, selectSidebarWorkflowActive,
      selectSidebarWorkflowActiveKeys, selectSidebarAutomationRunningKeys, selectSubagentActivityCount,
    }
    for (const [name, selector] of Object.entries(memoized)) {
      const first = selector(a)
      const before = selector.recomputations()
      // A number result cannot show a recompute by identity, so count them.
      expect(selector(b), name).toBe(first)
      expect(selector.recomputations() - before, name).toBe(0)
    }
  })
})

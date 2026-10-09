/**
 * The `chatSlice` module's public surface, pinned name by name.
 *
 * Every consumer imports the chat state through this one module, so what it
 * exports is a contract: the runtime names, what kind of value each one is,
 * every action creator's `type` string, every thunk's `typePrefix` (the
 * dashboard slice matches `chat/createSlot/fulfilled` and the `deleteSlot`
 * lifecycle by string), the exported constants, and the initial state the
 * default reducer produces. A name that moves between modules must still
 * resolve here with the same value.
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

const ACTION_CREATORS = [
  'ageFolderSuggestion', 'appendMessage', 'appendQueuedMessage', 'appendSlotMessage',
  'cancelQueuedMessage', 'clearFocusToolCallId', 'clearFolderSuggestion', 'clearFollowupCard',
  'clearMessages', 'clearPendingPermissions', 'clearQuestionCard', 'clearSlotCache',
  'clearSlotReveal', 'clearSlotState', 'clearSubagentsForSnapshot', 'clearSwitchSlotGone',
  'clearTerminalSubagents', 'clearUndeletableHistory', 'clearUnresumableResume',
  'clearWorkflowRun', 'confirmOptimisticSend', 'dismissFollowupItem', 'editQueuedMessage',
  'endLocalTurn', 'finalizeAssistant', 'hydrateSlotMessages', 'markQuestionSettled', 'markSendUnconfirmed', 'markSubagentApproving',
  'openActivityPanel', 'openActivityToTab', 'openActivityToTool', 'reconcileSubagentQueuedFromSlots', 'reconcileWorkflowRuns',
  'removeAutomation', 'removeByApprovalId', 'removeQueuedMessage', 'removeThinking',
  'reorderQueuedMessages', 'replaceMessages', 'requestFolderReveal', 'requestSlotReveal',
  'resolveByApprovalId', 'resolveOptimisticSteer', 'resolveQuestionCard', 'selectSubagent',
  'setActiveSlot', 'setAgentSwitchNotice', 'setAutomations', 'setCloseRefused', 'setFolderSuggestion',
  'setFollowupCard', 'setPendingInput', 'setQuestionCard', 'setQuestionDraft', 'setQuestionNotice', 'setQuestionRequestInFlight', 'setSlotRunning',
  'setSlotState', 'setSlotStatusDetail', 'setSlotStopping', 'setStopPressedAt', 'setVoiceAudio',
  'setVoicePlaying', 'settleStopNotRunning', 'sideClose', 'sideOptimisticAppend',
  'sideOptimisticRollback', 'sideReleaseConsumed', 'sseActivityEvent', 'sseAutomation',
  'sseChatMessage', 'sseChatMessagePatchByTs', 'sseChatMessageUpdate', 'sseContextUsage',
  'sseMcpAppRender', 'sseSideQueue', 'sseSideResult', 'sseSubagentBatchChunks',
  'sseSubagentBatchUpdate', 'sseSubagentDone', 'sseSubagentPending', 'sseSubagentQueued',
  'sseSubagentRetrying', 'sseSubagentSnapshot', 'sseSubagentSpawn', 'sseSubagentStalled',
  'sseSubagentTool', 'sseThinkingChunk', 'sseToolActivity', 'sseToolResult', 'sseWorkflowEvent',
  'stageToMainComposer', 'startLocalTurn', 'startServerTurn', 'syncSlotRunningFromServer', 'toggleActivity', 'truncateAfterIndex',
  'updateStreamingMessage',
]

const THUNKS: Record<string, string> = {
  createSlot: 'chat/createSlot',
  deleteHistorySession: 'chat/deleteHistorySession',
  deleteSlot: 'chat/deleteSlot',
  fetchHistory: 'chat/fetchHistory',
  forkSlot: 'chat/forkSlot',
  loadOlderMessages: 'chat/loadOlder',
  loadOlderSlotMessages: 'chat/loadOlderSlot',
  refreshSlot: 'chat/refreshSlot',
  requestStop: 'chat/requestStop',
  resumeFromHistory: 'chat/resumeFromHistory',
  switchSlot: 'chat/switchSlot',
  warmSlotCache: 'chat/warmSlotCache',
}

const FUNCTIONS = [
  'abortActiveOlderFetch', 'batchedTextAboveFloor', 'capturePendingAskId', 'clampToolOutput',
  'countMatchedFetchLimit', 'floorForGen', 'hasUnidentifiedDurableRow', 'isAnsweredQuestionEnding', 'isAwaitingSpawnApproval',
  'isSupersededPagingRejection', 'isTerminalWorkflowStatus', 'mcpAppKey', 'missedChunkMarker',
  'pendingQuestionFor', 'queueEditBroadcastAt', 'queueEntryAttachments', 'queueEntryQuote', 'raiseChunkSeq',
  'selectActiveSlotProject', 'selectAutomationForSlot', 'selectComposerBusy', 'selectContinuable',
  'selectSendConfirmed', 'selectSidebarApprovalCounts', 'selectSidebarAutomationRunningKeys',
  'selectSidebarStartedSubagentCounts', 'selectSidebarSubagentCounts', 'selectSidebarWorkflowActive', 'selectSidebarWorkflowActiveKeys',
  'selectSlotMessages', 'selectSlotPendingApproval', 'selectSlotPendingSpawnApprovals',
  'selectSlotRunEpoch', 'selectSlotStreamState', 'selectSlotSubagents',
  'selectSlotSubagentsActive', 'selectSlotToolLog', 'selectSubagentActivityCount', 'selectTrailingSendUnconfirmed',
  'selectTurnInterrupted', 'shouldResolveAskOnSend', 'slotCoverageShortfall',
  'slotSwitchFetchLimit', 'snapshotChunkGen', 'snapshotChunkSeq', 'switchSlotNoticeCopy',
  'transcriptTsMs',
]

const CONSTANTS: Record<string, unknown> = {
  FOLDER_SUGGESTION_MAX_TURNS: 3,
  OLDER_PAGE_LIMIT: 100,
  OLDER_WALK_PAGE_LIMIT: 100,
  PANE_HYDRATE_LIMIT: 50,
  QUIET_END_SERVER: 'kirocrew-core',
  QUIET_END_TOOL: 'nothing_to_do',
  REFRESH_LIMIT_CEILING: 500,
  SLOT_DETAIL_MAX_LIMIT: 500,
  TOOL_OUTPUT_MAX_CHARS: 64000,
  WINDOW_WALK_MAX_PAGES: 8,
  WORKFLOW_TERMINAL_STATUSES: ['finished', 'failed', 'cancelled'],
}

/** The initial state in its declared key order. `lastChunkSeq` and
 *  `lastChunkGen` are present as `undefined`, which JSON would hide. */
const INITIAL_STATE = {
  activeSlot: null, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
  slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, slotCursorKey: null,
  slotSwitchRequestId: null, slotSwitchTarget: null, slotSwitchOrigin: null, switchSlotGone: null,
  loadingOlder: false, slotOlderError: false, lastChunkSeq: undefined, lastChunkGen: undefined,
  _wsChunkedDuringFetch: false, liveFrameSeq: 0, refreshAppliedSeq: {}, _redeliveredFramesDropped: 0, history: [], historyHasMore: false,
  historyOffset: 0, unresumableResume: null, lastResumeRequestId: null, undeletableHistory: null,
  pendingInput: null, mainComposerAppend: null, agentSwitchNotice: null, creatingSlot: false, foregroundCreateId: null,
  lastCreatedActivation: null, slotContextPct: {}, slotContextTokens: {}, voicePlaying: false,
  voiceAudio: null, subagents: {}, subagentQueued: {}, subagentQueuedReason: {}, automations: {},
  selectedSubagentId: null, toolLog: [], workflowRuns: {}, activityOpen: false,
  activityTab: 'changes', activityTabRequest: 0, revealRequest: null, revealNonce: 0,
  focusToolCallId: null, mcpApps: {}, slotActivity: {}, slotMessages: {}, slotPaneHasMore: {},
  slotPaneBounded: {}, slotServerTotal: {}, slotServerTotalSeq: {}, thinkingOrphans: {},
  slotRun: {}, slotHydrated: {}, slotLoading: false, slotSide: {}, slotSideClosed: {},
  slotHistory: [], slotsSnapshotSeen: false, pendingQuestions: {}, questionRequestsInFlight: {}, questionsSettled: {}, restoredQuestionNotices: {}, followups: {},
  folderSuggestions: {}, stopPressedAt: {}, runEpoch: {}, activeRunEpochAtEntry: 0,
  pendingTurnSlot: null,
  closeRefused: null,
}

const surface = chatSlice as unknown as Record<string, unknown>

describe('chatSlice public surface', () => {
  it('exports exactly the pinned runtime names', () => {
    const expected = [...ACTION_CREATORS, ...Object.keys(THUNKS), ...FUNCTIONS, ...Object.keys(CONSTANTS), 'default'].sort()
    expect(Object.keys(chatSlice).sort()).toEqual(expected)
  })

  it('every action creator carries its chat/<name> type and matches its own action', () => {
    for (const name of ACTION_CREATORS) {
      const creator = surface[name] as { type: string; match: (a: unknown) => boolean }
      expect(typeof creator, name).toBe('function')
      expect(creator.type, name).toBe(`chat/${name}`)
      expect(creator.match({ type: `chat/${name}` }), name).toBe(true)
    }
  })

  it('every thunk keeps its typePrefix and its lifecycle action types', () => {
    for (const [name, prefix] of Object.entries(THUNKS)) {
      const thunk = surface[name] as { typePrefix: string; pending: { type: string }; fulfilled: { type: string }; rejected: { type: string } }
      expect(thunk.typePrefix, name).toBe(prefix)
      expect(thunk.pending.type, name).toBe(`${prefix}/pending`)
      expect(thunk.fulfilled.type, name).toBe(`${prefix}/fulfilled`)
      expect(thunk.rejected.type, name).toBe(`${prefix}/rejected`)
    }
  })

  it('exported helpers and selectors are plain functions, constants keep their values', () => {
    for (const name of FUNCTIONS) {
      const fn = surface[name] as { type?: unknown; typePrefix?: unknown }
      expect(typeof fn, name).toBe('function')
      expect(fn.type, name).toBeUndefined()
      expect(fn.typePrefix, name).toBeUndefined()
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

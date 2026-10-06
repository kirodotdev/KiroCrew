/** What a finished turn (`chat_done`) means once its row has been finalized:
 *  the turn-done chime, the opt-in native toast, the unread badge or read
 *  relay, the run status, the slot refresh, the pull-request refresh and the
 *  session-control status refresh. */
import { useMemo, type MutableRefObject } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import { markSlotUnread } from '../../store/dashboardSlice'
import { setSlotStatusDetail, refreshSlot, warmSlotCache, selectSidebarSubagentCounts, selectSidebarWorkflowActive, selectSidebarAutomationRunningKeys } from '../../store/chatSlice'
import { dispatchMcNotification, TURN_DONE_KIND, shouldChimeOnTurnDone } from '../notificationEvent'
import { shouldNotifyOnChatComplete } from '../chatCompleteNotify'
import { isSlotMutedByCreator } from '../sessionMute'
import { postNativeNotification } from '../../lib/nativeNotify'
import { normalizeRunSessionKey } from '../../apps/workflows/runModel'
import { dashboardAutomationSlotKey } from '../../monitoring/automation'
import { i18nT } from '../../i18n/t'
import { attendArrival } from './attention'
import { refreshPullRequestsAfterTurn, refreshSessionControlStatusesAfterTurn } from './serverState'
import type { FrameData } from './frames'

export interface TurnCompletionDeps {
  dispatch: AppDispatch
  queryClient: QueryClient
  reconnectingRef: MutableRefObject<boolean>
}

export interface TurnCompletion {
  /** A `chat_done`'s attention and refresh work, after its `_done` row. */
  afterDone(data: FrameData): void
}

export function useTurnCompletion({ dispatch, queryClient, reconnectingRef }: TurnCompletionDeps): TurnCompletion {
  return useMemo<TurnCompletion>(() => ({
    afterDone(data) {
      let completionNeedsAttention = false
      let completionNeedsInput = false
      let questionPending = false
      // Muted-by-creator short-circuits every attention signal below
      // (chime, toast, unread). Function-scoped so the unread dispatch after
      // the attention block can read it too.
      let muted = false
      // Keep transcript finalization independent from attention: a parent
      // can finish a turn while its children or workflow still owe work.
      // A frame's activity hint wins over coalesced snapshots; older
      // frames fall back to the existing per-session activity selectors.
      if (data.slot) {
        const soundState = store.getState()
        const soundSlot = soundState.dashboard.slots.find(s => s.key === data.slot)
        // A session muted by its creator's "mute sessions it opens"
        // rule produces no turn-done chime, no background-finished toast and no
        // unread badge, from its first turn. A tool-approval prompt is NOT this
        // path (it flows through approvals.ts) and stays exempt, as criterion 2
        // requires. The creator keeps all of its own signals.
        muted = isSlotMutedByCreator(soundState.dashboard.slots, data.slot)
        const workflows = selectSidebarWorkflowActive(soundState)
        const workflowActive = !!(
          workflows[normalizeRunSessionKey(data.slot)]
          || (soundSlot?.linked_session_key && workflows[normalizeRunSessionKey(soundSlot.linked_session_key)])
        )
        const continuing = data.continuing ?? !!(
          workflowActive
          || selectSidebarSubagentCounts(soundState)[data.slot]
          || soundSlot?.subagents_running
          || (soundSlot?.queue_depth ?? 0) > 0
          || selectSidebarAutomationRunningKeys(soundState).includes(dashboardAutomationSlotKey(data.slot))
        )
        questionPending = !!soundState.chat.pendingQuestions?.[data.slot]
        // An authoritative frame hint (explicit question) or a
        // live question card both mean the conversation paused for the
        // user rather than finished; the toast wording reads this too.
        completionNeedsInput = data.needs_input === true || questionPending
        completionNeedsAttention = shouldChimeOnTurnDone({
          slot: data.slot,
          reconnecting: reconnectingRef.current,
          continuing,
          needsInput: completionNeedsInput,
        }) && !muted
        // A live question card already requested audio. Keep its named
        // desktop toast eligible, but do not request a second chime.
        if (completionNeedsAttention && !questionPending) dispatchMcNotification(TURN_DONE_KIND)
      }
      // Native notifications can carry an OS sound too, so they share
      // the attention gate before applying the opt-in and away checks.
      if (completionNeedsAttention && shouldNotifyOnChatComplete({
        slot: data.slot,
        reconnecting: reconnectingRef.current,
      })) {
        const doneSlot = data.slot as string
        const doneTitle = store.getState().dashboard.slots
          .find(s => s.key === doneSlot)?.title || doneSlot
        // A toast that reads "Response ready" while the agent is waiting
        // on the user misdescribes the handoff; two literal keys keep the
        // reference statically checkable (see check-i18n-keys.mjs).
        const doneBody = completionNeedsInput
          ? i18nT('hooks.useWebSocket.waiting_for_input')
          : i18nT('hooks.useWebSocket.response_ready')
        // Best-effort (same as approval): the helper swallows Android
        // Chrome's "Illegal constructor" and relays to the parent frame
        // when this dashboard is an embedded instance pane.
        postNativeNotification(doneTitle, { body: doneBody, tag: `kirocrew-chat-done:${doneSlot}`, silent: questionPending })
      }
      // Off screen: badge the session, and warm its cache so switching to
      // it renders the finished answer instantly (no on-switch fetch). In
      // this window's visible active slot: relay the read, like an arriving
      // message (a hidden window relays on reveal instead).
      attendArrival(data.slot, (data as { ts?: string }).ts, reconnectingRef.current, slot => {
        // Criterion 7: a muted session never becomes unread from its own
        // activity -- no row dot, no folder rollup, nothing in the nav/tab/relay
        // counts. Cache is still warmed so switching to the row renders the
        // finished answer instantly (criterion 5: rows stay readable).
        if (!muted) dispatch(markSlotUnread({ slot, ts: (data as { ts?: string }).ts || undefined }))
        dispatch(warmSlotCache(slot))
      })
      if (data.slot) {
        dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'idle', ts: Date.now() }))
      }
      if (data.slot) dispatch(refreshSlot(data.slot))
      if (data.slot) {
        const isActive = data.slot === store.getState().chat.activeSlot
        refreshPullRequestsAfterTurn(
          queryClient,
          store.getState().dashboard.slots,
          data.slot,
          isActive,
        )
        refreshSessionControlStatusesAfterTurn(queryClient, data.slot, isActive)
      }
    },
  }), [dispatch, queryClient, reconnectingRef])
}

/** What a finished turn (`chat_done`) means once its row has been finalized:
 *  the turn-done chime, the opt-in native toast, the unread badge or read
 *  relay, the run status, the slot refresh and the pull-request refresh. */
import { useMemo, type MutableRefObject } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import { markSlotUnread } from '../../store/dashboardSlice'
import { setSlotStatusDetail, refreshSlot, warmSlotCache, selectSidebarSubagentCounts, selectSidebarWorkflowActive, selectSidebarAutomationRunningKeys } from '../../store/chatSlice'
import { dispatchMcNotification, TURN_DONE_KIND, shouldChimeOnTurnDone } from '../notificationEvent'
import { shouldNotifyOnChatComplete } from '../chatCompleteNotify'
import { postNativeNotification } from '../../lib/nativeNotify'
import { normalizeRunSessionKey } from '../../apps/workflows/runModel'
import { dashboardAutomationSlotKey } from '../../monitoring/automation'
import { i18nT } from '../../i18n/t'
import { attendArrival } from './attention'
import { refreshPullRequestsAfterTurn } from './serverState'
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
      // Keep transcript finalization independent from attention: a parent
      // can finish a turn while its children or workflow still owe work.
      // A frame's activity hint wins over coalesced snapshots; older
      // frames fall back to the existing per-session activity selectors.
      if (data.slot) {
        const soundState = store.getState()
        const soundSlot = soundState.dashboard.slots.find(s => s.key === data.slot)
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
        })
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
        dispatch(markSlotUnread({ slot, ts: (data as { ts?: string }).ts || undefined }))
        dispatch(warmSlotCache(slot))
      })
      if (data.slot) {
        dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'idle', ts: Date.now() }))
      }
      if (data.slot) dispatch(refreshSlot(data.slot))
      if (data.slot) {
        refreshPullRequestsAfterTurn(
          queryClient,
          store.getState().dashboard.slots,
          data.slot,
          data.slot === store.getState().chat.activeSlot,
        )
      }
    },
  }), [dispatch, queryClient, reconnectingRef])
}

/** Coordinator-registry approvals as the socket sees them.
 *
 *  Owns the provenance of every approval the coordinator registry raised in
 *  this tab, its retirement (from `approval_resolved`, or as stale when the
 *  authority no longer lists it), the attention and feed half of a live
 *  `approval` frame, and the snapshot rules the reconnect reconcile applies.
 *  The permission row an approval injects into its owning chat is
 *  synthesized by `useWebSocket` beside the frame routing. */
import { useMemo, useRef } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import { markSlotUnread } from '../../store/dashboardSlice'
import { addNotification, removeNotificationByTs } from '../../store/notificationsSlice'
import { resolveByApprovalId, sseActivityEvent, sseSubagentSpawn, sseSubagentDone } from '../../store/chatSlice'
import { dispatchMcNotification, dispatchLiveNotification, APPROVAL_KIND } from '../notificationEvent'
import { loadUnreadOnAttention } from '../unreadOnAttention'
import { approvalNotificationBody } from '../../lib/approvalNotificationBody'
import { i18nT } from '../../i18n/t'
import { api } from '../../api/client'
import type { Notification } from '../../types'
import { isSlotOnScreen } from './attention'
import { recordInBoundedLog, resolvedSince } from './retiredIds'
import type { FrameData } from './frames'

type AuthorityApproval = Awaited<ReturnType<typeof api.approvals>>[number]

export interface ApprovalRegistry {
  /** Reconcile against `GET /api/approvals`: retire every approval held
   *  before the read that the answer no longer lists, then adopt each listed
   *  one this tab lacks (not retired while the read was in flight, not
   *  already in the feed, not already a permission row in its slot):
   *  register it, add its feed note, and hand it to `writeRow` for its
   *  transcript row. Rejects when the read does; the caller swallows. */
  reconcilePending(writeRow: (approval: AuthorityApproval, slot: string) => void): Promise<void>
  /** A live `approval` frame's attention and feed half; returns the owning
   *  slot ('' when the approval has none). */
  onApprovalFrame(data: FrameData, reconnecting: boolean): string
  onApprovalResolved(data: FrameData): void
}

export function useApprovalRegistry(dispatch: AppDispatch, queryClient: QueryClient): ApprovalRegistry {
  // Only coordinator-registry approvals enter this map. Chat-runner permission
  // rows can share an id, so registry provenance is required before a
  // reconnect reconcile may retire a client-injected row.
  const coordinatorApprovalsRef = useRef<Map<string, string>>(new Map())
  // Coordinator ids retired by live frames, keyed by monotonic sequence.
  // Reconnect snapshots consult this log so an authority response cannot
  // revive an entry retired while that response was in flight.
  const retiredApprovalIdsRef = useRef<Map<string, number>>(new Map())
  const retiredApprovalSeqRef = useRef(0)

  return useMemo<ApprovalRegistry>(() => {
    const retireApproval = (
      id: string,
      slot: string | undefined,
      approved: boolean,
      decision?: string,
      slotlessResolution = false,
    ) => {
      if (!id) return
      const coordinatorSlot = coordinatorApprovalsRef.current.get(id)
      const targetSlot = slot || undefined
      const chatState = store.getState().chat
      const targetMessages = targetSlot === chatState.activeSlot
        ? chatState.messages
        : targetSlot && Object.prototype.hasOwnProperty.call(chatState.slotMessages, targetSlot)
          ? chatState.slotMessages[targetSlot] ?? []
          : []
      // Settled rows no longer compete for a resolution. A still-pending unmarked
      // row is a chat-runner collision, so the frame cannot safely claim the
      // coordinator registry even when its id and slot match the provenance map.
      const pendingMatches = targetMessages.filter(message =>
        message.role === 'permission'
        && message.meta?.approval_id === id
        && !message.meta?.resolved)
      const ownsCoordinatorEntry = coordinatorApprovalsRef.current.has(id)
        && (slotlessResolution || slot === coordinatorSlot)
        && pendingMatches.some(message => message.meta?.registry === 'coordinator')
        && !pendingMatches.some(message => message.meta?.registry == null)
      queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
      if (ownsCoordinatorEntry) {
        const items = store.getState().notifications.items
        const match = items.find((n: Notification) => n.approval_id === id)
        if (match) dispatch(removeNotificationByTs(match.ts))
      }
      const displayDecision = decision === 'expired' ? 'stale' : decision
      dispatch(resolveByApprovalId({
        id,
        slot: targetSlot,
        decision: displayDecision ?? (approved ? 'approved' : 'rejected'),
        ...(ownsCoordinatorEntry ? { registry: 'coordinator' } : {}),
      }))
      // A stale retirement carries no outcome: the approval may have been
      // answered either way where this client could not see it (another window,
      // a channel, or auto-approve). An explicit expiry is known to be denied,
      // but keeps the stale chat vocabulary while terminating a spawn card.
      const outcomeKnown = decision !== 'stale'
      // Resolve only in the slot that raised the card. Guessing activeSlot here
      // writes subagent spawn/done entries into an unrelated conversation.
      const resolvedType = id.startsWith('spawn:') ? 'spawn' : 'chat'
      if (targetSlot) {
        const chatState = store.getState().chat
        const log = targetSlot === chatState.activeSlot
          ? chatState.toolLog
          : chatState.slotActivity[targetSlot]?.toolLog ?? []
        const hasMatchingApproval = log.some(e => e.approval_id === id && e.type === 'approval')
        if (hasMatchingApproval || resolvedType === 'spawn') {
          dispatch(sseActivityEvent({
            slot: targetSlot,
            kind: 'approval_resolved',
            text: '',
            approval_id: id,
            approval_type: resolvedType,
          }))
        }
        if (id.startsWith('spawn:') && outcomeKnown) {
          const agentId = id.replace('spawn:', '')
          if (approved) {
            dispatch(sseSubagentSpawn({ slot: targetSlot, id: agentId, task: '', agent: '' }))
          } else {
            // The spawn card renders this value verbatim under its error label,
            // so both denials have to arrive as a sentence: the raw `expired` or
            // `rejected` token read as a subagent crash when it was a decision.
            dispatch(sseSubagentDone({
              slot: targetSlot,
              id: agentId,
              elapsed: 0,
              error: decision === 'expired'
                ? i18nT('hooks.useWebSocket.approval_wait_expired')
                : i18nT('hooks.useWebSocket.approval_rejected'),
            }))
          }
        }
      }
      recordInBoundedLog(retiredApprovalIdsRef.current, retiredApprovalSeqRef, id)
      if (ownsCoordinatorEntry) {
        coordinatorApprovalsRef.current.delete(id)
      }
    }

    return {
      async reconcilePending(writeRow) {
        // Only approvals held before this authority read may be retired from its
        // answer. A live frame can inject another approval while the read is in
        // flight, and that approval is outside this snapshot's ordering boundary.
        const before = new Map(coordinatorApprovalsRef.current)
        const retiredSeen = retiredApprovalSeqRef.current
        const approvals = await api.approvals()
        const retiredDuringFetch = new Set(resolvedSince(retiredApprovalIdsRef.current, retiredSeen))
        const pendingIds = new Set(approvals.map(a => a.id))
        for (const [id, slot] of before) {
          if (!pendingIds.has(id)) retireApproval(id, slot, false, 'stale')
        }
        const existing = store.getState().notifications.items
        const currentChat = store.getState().chat
        for (const a of approvals) {
          if (retiredDuringFetch.has(a.id)) continue
          const slot = a.slot || ''
          const slotMessages = slot === currentChat.activeSlot
            ? currentChat.messages
            : slot && Object.prototype.hasOwnProperty.call(currentChat.slotMessages, slot)
              ? currentChat.slotMessages[slot] ?? []
              : []
          if (existing.some((n: Notification) => n.approval_id === a.id)) continue
          if (slotMessages.some(message =>
            message.role === 'permission' && message.meta?.approval_id === a.id)) continue
          coordinatorApprovalsRef.current.set(a.id, slot)
          dispatch(addNotification({
            kind: 'approval',
            title: i18nT('hooks.useWebSocket.tool_approval', { name: a.tool || i18nT('hooks.useWebSocket.unknown') }),
            body: approvalNotificationBody(a.source, a.tool_input),
            ts: String(a.ts || Date.now() / 1000),
            approval_id: a.id,
          } as Notification))
          writeRow(a, slot)
        }
      },
      onApprovalFrame(data, reconnecting) {
        queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
        if (typeof data.id === 'string') {
          coordinatorApprovalsRef.current.set(
            data.id,
            typeof data.slot === 'string' ? data.slot : '',
          )
        }
        // Approval-blocked chime: the agent is stuck until the user acts.
        // Suppressed during reconnect catch-up (same policy as turn-done).
        if (!reconnecting) {
          dispatchMcNotification(APPROVAL_KIND)
          // Blocked on the user: badge it under the done-or-waiting opt-in.
          if (typeof data.slot === 'string' && data.slot && loadUnreadOnAttention() && !isSlotOnScreen(data.slot)) {
            dispatch(markSlotUnread({ slot: data.slot }))
          }
        }
        // No OS toast here. The addNotification below is what reaches the
        // OS: useNativeNotification watches the unacked count and posts ONE
        // toast per new note, tagged with its approval_id, only while the
        // user is away from the window. A second constructor on this path
        // carried a different tag, so the OS showed two banners for one
        // approval.
        //
        // The owning slot rides on the note so the in-app banner's
        // `targetsCurrentView` gate can tell "this chat is on screen" (the
        // inline permission card already shows it there) from "the user is
        // on another surface" (the banner is the interrupt).
        const approvalSlot = typeof data.slot === 'string' && data.slot ? data.slot : ''
        const approvalNote = {
          kind: 'approval',
          title: i18nT('hooks.useWebSocket.tool_approval', { name: data.tool || i18nT('hooks.useWebSocket.unknown') }),
          // A contested run's prompt reaches the feed with no slot and
          // leads with its purpose: the card excerpt is all a reader gets
          // before Review, and here the purpose is the system's account of
          // why there is no tab. The backend says so explicitly -- a
          // slotless subagent prompt is NOT thereby contested (a run rooted
          // in a cron, the CLI or a channel has no tab either, and its
          // purpose is the run's own words), so those keep the command first.
          body: approvalNotificationBody(data.source, data.tool_input, data.tool_purpose, {
            purposeFirst: data.contested === true,
          }),
          ts: String(data.ts || Date.now() / 1000),
          approval_id: data.id,
          ...(approvalSlot ? { slot: approvalSlot } : {}),
          // The Review panel reads this to make Approve the secondary
          // choice: the copy's safe action is to start again from one chat.
          ...(data.contested === true ? { contested: true } : {}),
        } as Notification
        dispatch(addNotification(approvalNote))
        // The in-app banner hears LIVE arrivals only, same as the
        // `notification` frame: a reconnect catch-up replays approvals the
        // bell already holds. While the window is focused this banner is the
        // visible interrupt for a blocking approval (the OS toast stays
        // quiet for a focused window); away from the window the toast takes
        // over and `shouldBannerNote` skips it.
        if (!reconnecting) dispatchLiveNotification(approvalNote)
        return approvalSlot
      },
      onApprovalResolved(data) {
        const id = typeof data.id === 'string' ? data.id : ''
        const frameSlot = typeof data.slot === 'string' ? data.slot : undefined
        const targetSlot = frameSlot ?? coordinatorApprovalsRef.current.get(id)
        // A decided frame carries no decision: approved/rejected derives
        // from `approved`. An explicit expiry is a known auto-denial, but
        // retireApproval keeps its chat-row display token as `stale`.
        const decision = data.decision === 'expired' ? 'expired' : undefined
        retireApproval(id, targetSlot, !!data.approved, decision, frameSlot === undefined)
      },
    }
  }, [dispatch, queryClient])
}

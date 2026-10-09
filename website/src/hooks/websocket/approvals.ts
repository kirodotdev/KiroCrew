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
import { addNotification, endApprovalRow, liveApprovalRows } from '../../store/notificationsSlice'
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
  // The server-issued instance of the request each entry above names. The id
  // is the caller's and recurs; the instance tells this request's feed row
  // apart from an earlier one's under the same id.
  const coordinatorInstancesRef = useRef<Map<string, string>>(new Map())
  // Coordinator ids retired by live frames, keyed by monotonic sequence.
  // Reconnect snapshots consult this log so an authority response cannot
  // revive an entry retired while that response was in flight.
  const retiredApprovalIdsRef = useRef<Map<string, number>>(new Map())
  const retiredApprovalSeqRef = useRef(0)
  // Coordinator ids a live `approval` frame raised, on the same sequence. A
  // snapshot fetched before that frame describes an older request under the
  // id, so its listing must neither replace the newer row nor retire it.
  const liveApprovalIdsRef = useRef<Map<string, number>>(new Map())

  return useMemo<ApprovalRegistry>(() => {
    const retireApproval = (
      id: string,
      slot: string | undefined,
      approved: boolean,
      decision?: string,
      slotlessResolution = false,
      instance = '',
    ) => {
      if (!id) return
      const coordinatorSlot = coordinatorApprovalsRef.current.get(id)
      const provenance = coordinatorApprovalsRef.current.has(id)
        && (slotlessResolution || slot === coordinatorSlot)
      const targetSlot = slot || undefined
      const chatState = store.getState().chat
      const targetMessages = targetSlot === chatState.activeSlot
        ? chatState.messages
        : targetSlot && Object.hasOwn(chatState.slotMessages, targetSlot)
          ? chatState.slotMessages[targetSlot] ?? []
          : []
      // Settled rows no longer compete for a resolution. A still-pending unmarked
      // row is a chat-runner collision, so the frame cannot safely claim the
      // coordinator registry even when its id and slot match the provenance map.
      const pendingMatches = targetMessages.filter(message =>
        message.role === 'permission'
        && message.meta?.approval_id === id
        && !message.meta?.resolved)
      const coordinatorRow = pendingMatches.some(message => message.meta?.registry === 'coordinator')
      // A frame naming the instance this tab tracks under the id is the
      // coordinator's own resolution: that is ownership outright, and a
      // colliding chat-runner row in the same slot is not what it settled.
      // A frame naming none keeps the collision check.
      const tracked = !!instance && coordinatorInstancesRef.current.get(id) === instance
      const ownsCoordinatorEntry = tracked || (provenance && coordinatorRow
        && !pendingMatches.some(message => message.meta?.registry == null))
      queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
      // Only rows still live compete: the id recurs, so a retired row for an
      // earlier request can share it. A row bound to an instance goes only
      // with a frame naming that instance (an approval with no owning slot,
      // such as a cron job's, has no chat row to check). A row that predates
      // instances goes only on ownership as decided above.
      const rows = liveApprovalRows(store.getState().notifications, id).filter((n: Notification) =>
        n.approval_instance ? !!instance && n.approval_instance === instance : ownsCoordinatorEntry)
      // The request ended, so its row leaves the feed, as a chat-owned row
      // always has. A row with a decision still in flight stays until that
      // response settles (`endApprovalRow`), so a refusal can still say why.
      for (const row of rows) dispatch(endApprovalRow(row.ts))
      const displayDecision = decision === 'expired' ? 'stale' : decision
      // Tracked but with no coordinator row in the slot: the only pending row
      // under the id is a chat runner's, which this resolution did not settle.
      if (!tracked || coordinatorRow) dispatch(resolveByApprovalId({
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
        coordinatorInstancesRef.current.delete(id)
      }
    }

    return {
      async reconcilePending(writeRow) {
        // Only approvals held before this authority read may be retired from its
        // answer. A live frame can inject another approval while the read is in
        // flight, and that approval is outside this snapshot's ordering boundary.
        // Each entry keeps the instance it held then: the id recurs, so a
        // request that replaced it during the read must not be retired as it.
        const instances = coordinatorInstancesRef.current
        const before = [...coordinatorApprovalsRef.current].map(([id, slot]) => [id, slot, instances.get(id)] as const)
        const retiredSeen = retiredApprovalSeqRef.current
        const approvals = await api.approvals()
        const retiredDuringFetch = new Set(resolvedSince(retiredApprovalIdsRef.current, retiredSeen))
        // The live stream is newer than this answer for every id it raised
        // after the read began: those ids keep what the frame left.
        const raisedDuringFetch = new Set(resolvedSince(liveApprovalIdsRef.current, retiredSeen))
        const pendingIds = new Set(approvals.map(a => a.id))
        // A live frame that raised an id during the read also took it over
        // from whatever this snapshot held, so those ids are skipped here.
        for (const [id, slot, instance] of before) {
          if (pendingIds.has(id) || raisedDuringFetch.has(id)) continue
          retireApproval(id, slot, false, 'stale', false, instance)
        }
        for (const a of approvals) {
          if (retiredDuringFetch.has(a.id) || raisedDuringFetch.has(a.id)) continue
          // A live feed row bound to another instance than the one listed is
          // an ended request's: the server holds a different request under
          // the id now.
          if (a.instance) {
            for (const row of liveApprovalRows(store.getState().notifications, a.id)) {
              if (row.approval_instance && row.approval_instance !== a.instance) dispatch(endApprovalRow(row.ts))
            }
          }
          const slot = a.slot || ''
          // Read per entry: the retirement above and earlier adoptions in this
          // loop change both.
          const currentChat = store.getState().chat
          const slotMessages = slot === currentChat.activeSlot
            ? currentChat.messages
            : slot && Object.hasOwn(currentChat.slotMessages, slot)
              ? currentChat.slotMessages[slot] ?? []
              : []
          // Already in the feed as THIS request: the same instance, or (for a
          // listing without one) a live row under the id. A retired row for an
          // earlier request under a recurring id is not this one.
          const notifications = store.getState().notifications
          if (a.instance
            ? notifications.items.some((n: Notification) => n.approval_id === a.id && n.approval_instance === a.instance)
            : liveApprovalRows(notifications, a.id).length > 0) continue
          if (slotMessages.some(message =>
            message.role === 'permission' && message.meta?.approval_id === a.id)) continue
          coordinatorApprovalsRef.current.set(a.id, slot)
          if (a.instance) coordinatorInstancesRef.current.set(a.id, a.instance)
          else coordinatorInstancesRef.current.delete(a.id)
          dispatch(addNotification({
            kind: 'approval',
            title: i18nT('hooks.useWebSocket.tool_approval', { name: a.tool || i18nT('hooks.useWebSocket.unknown') }),
            body: approvalNotificationBody(a.source, a.tool_input),
            ts: String(a.ts || Date.now() / 1000),
            approval_id: a.id,
            ...(a.instance ? { approval_instance: a.instance } : {}),
            _local: true,
            ...(slot ? { slot } : {}),
          } as Notification))
          writeRow(a, slot)
        }
      },
      onApprovalFrame(data, reconnecting) {
        queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
        if (typeof data.id === 'string') {
          recordInBoundedLog(liveApprovalIdsRef.current, retiredApprovalSeqRef, data.id)
          coordinatorApprovalsRef.current.set(
            data.id,
            typeof data.slot === 'string' ? data.slot : '',
          )
          if (typeof data.instance === 'string' && data.instance) {
            coordinatorInstancesRef.current.set(data.id, data.instance)
          } else {
            coordinatorInstancesRef.current.delete(data.id)
          }
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
        const approvalInstance = typeof data.instance === 'string' ? data.instance : ''
        const approvalNote = {
          kind: 'approval',
          title: i18nT('hooks.useWebSocket.tool_approval', { name: data.tool || i18nT('hooks.useWebSocket.unknown') }),
          body: approvalNotificationBody(data.source, data.tool_input, data.tool_purpose),
          ts: String(data.ts || Date.now() / 1000),
          approval_id: data.id,
          ...(approvalInstance ? { approval_instance: approvalInstance } : {}),
          _local: true,
          ...(approvalSlot ? { slot: approvalSlot } : {}),
        } as Notification
        // A request that takes over the id ends the one it replaced, which
        // gets no frame of its own: a live feed row bound to another instance
        // under this id is that ended request's, so it leaves the feed.
        if (approvalInstance && typeof data.id === 'string') {
          for (const row of liveApprovalRows(store.getState().notifications, data.id)) {
            if (row.approval_instance && row.approval_instance !== approvalInstance) dispatch(endApprovalRow(row.ts))
          }
        }
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
        const instance = typeof data.instance === 'string' ? data.instance : ''
        retireApproval(id, targetSlot, !!data.approved, decision, frameSlot === undefined, instance)
      },
    }
  }, [dispatch, queryClient])
}

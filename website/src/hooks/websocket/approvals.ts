/** Coordinator-registry approvals as the socket sees them.
 *
 *  Every approval here is handled as the request its target names
 *  (types/approvalTarget), never by its id: the coordinator's id is the
 *  caller's and recurs, and a chat runner's request id can collide with it.
 *  This module owns the targets of the coordinator approvals this tab shows,
 *  their retirement (from `approval_resolved`, or as stale when the authority
 *  no longer lists them), the attention and feed half of a live `approval`
 *  frame, and the snapshot rules the boot and reconnect reconcile applies.
 *  The permission row an approval injects into its owning chat is
 *  synthesized by `useWebSocket` beside the frame routing. */
import { useMemo, useRef } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import { markSlotUnread } from '../../store/dashboardSlice'
import { addNotification, endApprovalRow, isLocalApprovalRow, liveApprovalRows, approvalDecideTarget } from '../../store/notificationsSlice'
import { resolveApprovalRow, resolveNativeApprovalRows, sseActivityEvent, sseSubagentSpawn, sseSubagentDone } from '../../store/chatSlice'
import { dispatchMcNotification, dispatchLiveNotification, APPROVAL_KIND } from '../notificationEvent'
import { loadUnreadOnAttention } from '../unreadOnAttention'
import { approvalNotificationBody } from '../../lib/approvalNotificationBody'
import { i18nT } from '../../i18n/t'
import { api } from '../../api/client'
import type { ChatMessage, Notification } from '../../types'
import {
  approvalTargetKey, coordinatorTarget, nativeTarget, permissionRowTarget, sameApprovalTarget,
  type ApprovalTarget, type CoordinatorApprovalTarget,
} from '../../types/approvalTarget'
import { isSlotOnScreen } from './attention'
import { recordInBoundedLog, resolvedSince } from './retiredIds'
import type { FrameData } from './frames'

type AuthorityApproval = Awaited<ReturnType<typeof api.approvals>>[number]

export interface ApprovalRegistry {
  /** Reconcile against `GET /api/approvals`, the authority on which requests
   *  are still pending. Every coordinator request this tab shows (a feed row,
   *  a chat row, or one it tracks) that the answer does not list is retired as
   *  stale, unless a live frame raised it while the read was in flight. Rows
   *  that name no request at all are retired too: nothing can decide them.
   *  Each listed request this tab lacks is adopted: its feed note is added and
   *  `writeRow` writes its transcript row. When reconciles overlap, only the
   *  newest one started applies its answer; an older one resolves without
   *  effect. Rejects when the read does; the caller swallows. */
  reconcilePending(writeRow: (approval: AuthorityApproval, slot: string, target: CoordinatorApprovalTarget) => void): Promise<void>
  /** A live `approval` frame's attention and feed half. Returns the owning
   *  slot ('' when the approval has none) and the request's target, null
   *  when the frame names no request. */
  onApprovalFrame(data: FrameData, reconnecting: boolean): { slot: string; target: CoordinatorApprovalTarget | null }
  onApprovalResolved(data: FrameData): void
}

/** The permission rows a slot holds, wherever the store keeps them. */
function slotRows(slot: string): ChatMessage[] {
  const chat = store.getState().chat
  if (!slot) return []
  if (slot === chat.activeSlot) return chat.messages
  return Object.hasOwn(chat.slotMessages, slot) ? chat.slotMessages[slot] ?? [] : []
}

/** Every coordinator request a pending chat row names, across loaded slots. */
function pendingChatTargets(): CoordinatorApprovalTarget[] {
  const chat = store.getState().chat
  const out: CoordinatorApprovalTarget[] = []
  const scan = (slot: string, rows: ChatMessage[]) => {
    for (const m of rows) {
      if (m.role !== 'permission' || m.meta?.resolved) continue
      const t = permissionRowTarget(m.meta, slot)
      if (t?.origin === 'coordinator') out.push(t)
    }
  }
  if (chat.activeSlot) scan(chat.activeSlot, chat.messages)
  for (const slot of Object.keys(chat.slotMessages)) {
    if (slot !== chat.activeSlot) scan(slot, chat.slotMessages[slot] ?? [])
  }
  return out
}

export function useApprovalRegistry(dispatch: AppDispatch, queryClient: QueryClient): ApprovalRegistry {
  // The coordinator requests this tab shows, by target key. A decided frame
  // names no slot, so this is where the owning slot is found again.
  const targetsRef = useRef<Map<string, CoordinatorApprovalTarget>>(new Map())
  // Targets retired, and targets raised, by live frames, keyed by monotonic
  // sequence. A reconcile consults both from its watermark on: an authority
  // answer cannot revive a request retired while it was in flight, nor
  // retire one raised after it was taken.
  const retiredRef = useRef<Map<string, number>>(new Map())
  const raisedRef = useRef<Map<string, number>>(new Map())
  // The ids those raised targets carry, on the same sequence. The id recurs:
  // a listing read before a live frame raised a request under it can name
  // the earlier request the frame's one replaced on the server.
  const raisedIdsRef = useRef<Map<string, number>>(new Map())
  const seqRef = useRef(0)
  // The generation of the newest reconcile started. Reconnects can overlap,
  // and their reads can answer out of order: only the newest read's answer is
  // applied, so an older one cannot retire a request a newer one adopted.
  const reconcileGenRef = useRef(0)

  return useMemo<ApprovalRegistry>(() => {
    /** Side effects a settled request has beyond its own rows: the activity
     *  log entry and a spawn card. Keyed by the target, in the slot that
     *  raised it; guessing activeSlot writes into an unrelated conversation. */
    const settleActivity = (target: ApprovalTarget, approved: boolean, decision?: string) => {
      const slot = target.slot
      if (!slot) return
      const id = target.id
      // A stale retirement carries no outcome: the approval may have been
      // answered either way where this client could not see it (another window,
      // a channel, or auto-approve). An explicit expiry is known to be denied,
      // but keeps the stale chat vocabulary while terminating a spawn card.
      const outcomeKnown = decision !== 'stale'
      const resolvedType = id.startsWith('spawn:') ? 'spawn' : 'chat'
      const chatState = store.getState().chat
      const log = slot === chatState.activeSlot
        ? chatState.toolLog
        : chatState.slotActivity[slot]?.toolLog ?? []
      const hasMatchingApproval = log.some(e => e.type === 'approval' && sameApprovalTarget(e.approval_target, target))
      if (hasMatchingApproval || resolvedType === 'spawn') {
        dispatch(sseActivityEvent({
          slot,
          kind: 'approval_resolved',
          text: '',
          approval_id: id,
          approval_type: resolvedType,
          approval_target: target,
        }))
      }
      if (target.origin === 'coordinator' && id.startsWith('spawn:') && outcomeKnown) {
        const agentId = id.replace('spawn:', '')
        if (approved) {
          dispatch(sseSubagentSpawn({ slot, id: agentId, task: '', agent: '' }))
        } else {
          // The spawn card renders this value verbatim under its error label,
          // so both denials have to arrive as a sentence: the raw `expired` or
          // `rejected` token read as a subagent crash when it was a decision.
          dispatch(sseSubagentDone({
            slot,
            id: agentId,
            elapsed: 0,
            error: decision === 'expired'
              ? i18nT('hooks.useWebSocket.approval_wait_expired')
              : i18nT('hooks.useWebSocket.approval_rejected'),
          }))
        }
      }
    }

    /** Retire the coordinator request *target* names, and only it. */
    const retireCoordinator = (target: CoordinatorApprovalTarget, approved: boolean, decision?: string) => {
      queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
      // The request ended, so its live row leaves the feed, as a chat-owned
      // row always has. A row with a decision still in flight stays until that
      // response settles (`endApprovalRow`), so a refusal can still say why.
      for (const row of liveApprovalRows(store.getState().notifications, target)) {
        dispatch(endApprovalRow(row.ts))
      }
      const displayDecision = decision === 'expired' ? 'stale' : decision
      if (target.slot) {
        dispatch(resolveApprovalRow({ target, decision: displayDecision ?? (approved ? 'approved' : 'rejected') }))
      }
      settleActivity(target, approved, decision)
      const key = approvalTargetKey(target)
      recordInBoundedLog(retiredRef.current, seqRef, key)
      targetsRef.current.delete(key)
    }

    /** The tracked target for a frame's id and instance: its slot is the one
     *  the request was raised in, which a decided frame does not repeat. */
    const trackedTarget = (id: string, instance: string, frameSlot: string | undefined): CoordinatorApprovalTarget | null => {
      const probe = coordinatorTarget(id, frameSlot ?? '', instance)
      if (!probe) return null
      return targetsRef.current.get(approvalTargetKey(probe)) ?? probe
    }

    return {
      async reconcilePending(writeRow) {
        const gen = ++reconcileGenRef.current
        const watermark = seqRef.current
        const approvals = await api.approvals()
        // A newer reconcile started while this read was in flight: its answer
        // is the later one, so this one is dropped unapplied.
        if (gen !== reconcileGenRef.current) return
        // A request this tab has retired is gone for good (an instance is never
        // reissued), whether the retirement landed before this read or during
        // it, so the answer can neither revive nor retire it again.
        const retiredHere = retiredRef.current
        const raisedDuringFetch = new Set(resolvedSince(raisedRef.current, watermark))
        const idsRaisedDuringFetch = new Set(resolvedSince(raisedIdsRef.current, watermark))
        const pending = new Map<string, { approval: AuthorityApproval; target: CoordinatorApprovalTarget }>()
        for (const a of approvals) {
          const target = coordinatorTarget(a.id, a.slot || '', a.instance)
          if (target) pending.set(approvalTargetKey(target), { approval: a, target })
        }
        const stillPending = (t: CoordinatorApprovalTarget) => {
          const key = approvalTargetKey(t)
          return pending.has(key) || raisedDuringFetch.has(key)
        }
        // Retire what this tab shows that the authority no longer lists:
        // tracked requests, chat rows, and feed rows. The feed is the case a
        // fresh tab needs: its rows come from the stored notifications, and an
        // approval that expired before the tab opened is not pending.
        const shown = new Map<string, CoordinatorApprovalTarget>()
        for (const t of targetsRef.current.values()) shown.set(approvalTargetKey(t), t)
        for (const t of pendingChatTargets()) shown.set(approvalTargetKey(t), t)
        const notifications = store.getState().notifications
        const retired = notifications.retiredApprovals ?? {}
        for (const n of notifications.items) {
          // Only this tab's own approval rows are the authority's to settle. A
          // stored note can carry the `approval` kind (an app channel named
          // `approval`) but names no approval id, and is never deleted here.
          if (!isLocalApprovalRow(n) || Object.hasOwn(retired, n.ts)) continue
          const t = approvalDecideTarget(n)
          if (!t) {
            // A row from an older build names no request, so it cannot be
            // decided. When the authority still lists its id in its slot, the
            // adoption below raises a live row for that request: this one
            // would only duplicate it, and when it does not the request has
            // ended: either way the row leaves this tab. The server holds no
            // copy of it, so nothing is sent.
            dispatch(endApprovalRow(n.ts))
            continue
          }
          const key = approvalTargetKey(t)
          if (!shown.has(key)) shown.set(key, t)
        }
        for (const [key, t] of shown) {
          if (stillPending(t) || retiredHere.has(key)) continue
          // The slot this tab tracked it under, when it did.
          retireCoordinator(targetsRef.current.get(key) ?? t, false, 'stale')
        }
        // Adopt each listed request this tab lacks.
        const items = store.getState().notifications.items
        for (const [key, { approval: a, target }] of pending) {
          if (retiredHere.has(key)) continue
          // A live frame raised another request under this id after the read
          // began: this listing names the one it replaced, so it is not adopted.
          if (idsRaisedDuringFetch.has(target.id) && !raisedDuringFetch.has(key)) continue
          targetsRef.current.set(key, target)
          // Recorded as raised, as a live frame records one: a reconcile whose
          // read began before this adoption keeps the request.
          recordInBoundedLog(raisedRef.current, seqRef, key)
          if (!items.some((n: Notification) => sameApprovalTarget(approvalDecideTarget(n), target))) {
            dispatch(addNotification({
              kind: 'approval',
              title: i18nT('hooks.useWebSocket.tool_approval', { name: a.tool || i18nT('hooks.useWebSocket.unknown') }),
              body: approvalNotificationBody(a.source, a.tool_input),
              ts: String(a.ts || Date.now() / 1000),
              approval_id: a.id,
              approval_instance: target.instance,
              _local: true,
              ...(target.slot ? { slot: target.slot } : {}),
            } as Notification))
          }
          if (!slotRows(target.slot).some(m => m.role === 'permission'
            && sameApprovalTarget(permissionRowTarget(m.meta, target.slot), target))) {
            writeRow(a, target.slot, target)
          }
        }
      },
      onApprovalFrame(data, reconnecting) {
        queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
        const approvalSlot = typeof data.slot === 'string' && data.slot ? data.slot : ''
        const target = coordinatorTarget(data.id, approvalSlot, data.instance)
        if (target) {
          const key = approvalTargetKey(target)
          // A request that took over a live request's id arrives after the
          // server's `approval_resolved` frame retiring that request, which
          // `onApprovalResolved` has already applied.
          targetsRef.current.set(key, target)
          recordInBoundedLog(raisedRef.current, seqRef, key)
          recordInBoundedLog(raisedIdsRef.current, seqRef, target.id)
        }
        // Approval-blocked chime: the agent is stuck until the user acts.
        // Suppressed during reconnect catch-up (same policy as turn-done).
        if (!reconnecting) {
          dispatchMcNotification(APPROVAL_KIND)
          // Blocked on the user: badge it under the done-or-waiting opt-in.
          if (approvalSlot && loadUnreadOnAttention() && !isSlotOnScreen(approvalSlot)) {
            dispatch(markSlotUnread({ slot: approvalSlot }))
          }
        }
        // A frame that names no request raises no row: nothing could decide it.
        if (!target) return { slot: approvalSlot, target: null }
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
        const approvalNote = {
          kind: 'approval',
          title: i18nT('hooks.useWebSocket.tool_approval', { name: data.tool || i18nT('hooks.useWebSocket.unknown') }),
          body: approvalNotificationBody(data.source, data.tool_input, data.tool_purpose),
          ts: String(data.ts || Date.now() / 1000),
          approval_id: target.id,
          approval_instance: target.instance,
          _local: true,
          ...(approvalSlot ? { slot: approvalSlot } : {}),
        } as Notification
        dispatch(addNotification(approvalNote))
        // The in-app banner hears LIVE arrivals only, same as the
        // `notification` frame: a reconnect catch-up replays approvals the
        // bell already holds. While the window is focused this banner is the
        // visible interrupt for a blocking approval (the OS toast stays
        // quiet for a focused window); away from the window the toast takes
        // over and `shouldBannerNote` skips it.
        if (!reconnecting) dispatchLiveNotification(approvalNote)
        return { slot: approvalSlot, target }
      },
      onApprovalResolved(data) {
        const id = typeof data.id === 'string' ? data.id : ''
        const frameSlot = typeof data.slot === 'string' ? data.slot : undefined
        const instance = typeof data.instance === 'string' ? data.instance : ''
        const target = data.origin === 'coordinator' || instance ? trackedTarget(id, instance, frameSlot) : null
        // Whatever the frame settles, the shared inventory the command center
        // reads is out of date (a coordinator retirement refreshes it itself).
        if (!target) queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
        if (!id) return
        if (data.origin === 'coordinator' || instance) {
          // A coordinator frame that names no request settles nothing.
          if (!target) return
          // A decided frame carries no decision: approved/rejected derives
          // from `approved`. An explicit expiry is a known auto-denial, but
          // its chat row keeps the `stale` display token. A takeover ended the
          // replaced request unanswered, so it settles as stale: no outcome.
          const decision = data.decision === 'expired' ? 'expired'
            : data.decision === 'superseded' ? 'stale'
              : undefined
          retireCoordinator(target, !!data.approved, decision)
          return
        }
        // A chat runner's resolution. It never touches a coordinator row or a
        // feed note (the runner raises none), whatever their id.
        if (!frameSlot) return
        const decision = data.approved ? 'approved' : 'rejected'
        const named = nativeTarget(id, frameSlot, data.mid)
        if (named) {
          dispatch(resolveApprovalRow({ target: named, decision }))
          settleActivity(named, !!data.approved)
        } else {
          // A frame without the row's mid: the runner's own pending rows under
          // the id in that slot, never a coordinator row.
          dispatch(resolveNativeApprovalRows({ id, slot: frameSlot, decision }))
        }
      },
    }
  }, [dispatch, queryClient])
}

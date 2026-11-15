/**
 * The person's sessions for an app page (`@kirocrew/app-sdk`): the dashboard's
 * live session list, and the acts a person takes on a session from it (reply or
 * steer, decide its pending tool approval, start a new session).
 *
 * It is host code. Every member reads the dashboard's own store or calls the
 * dashboard's own client, so a session an app shows reads and behaves exactly as
 * the sidebar's row does: the same title, agent label, live state, pending
 * approval and recency, the same send receipt and the same decide. An app with
 * only the scoped transport could not do that: `POST /api/chat/slots` through it
 * creates a session the app owns, not the person's, and the live list would be a
 * full re-read on every slot change.
 *
 * What a member may do is decided by the identity the host published, the way
 * `ProjectPicker` is:
 *
 * - An EXTERNAL app gets a member once its manifest's `permissions.api` admits
 *   every path that member sends, each checked on its own (`HOST_SESSIONS_PATHS`).
 *   A member not admitted sends nothing: `sessions` is null, and `send`,
 *   `decide` and `create` reject with `AppApiPermissionError`.
 * - A BUILTIN page is host code and gets every member.
 * - With no app identity at all every member fails closed, like an external app
 *   with nothing declared.
 *
 * That grants nothing the scoped transport would not: a scoped request carries
 * the dashboard user's own authority too (the frontend scoping is a guardrail,
 * see the trust model), and the matcher is the one `useAppApi()` uses. A
 * pending approval is decided only through the object this hook handed out, so
 * an app cannot name a request the dashboard did not show it.
 */
import { useCallback, useContext, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { decideApprovalPath } from '../api/client/approvals'
import { ApiError, isTerminalApprovalRefusal } from '../api/apiError'
import { defaultAgentQuery } from '../api/defaultAgentQuery'
import { queryClient as dashboardQueryClient } from '../api/queryClient'
import { sendTurn, type SendReceiptStatus } from '../chat-core/transport/sendTurn'
import { recencyEpoch } from '../components/commandPalette/providers/recentsProvider'
import { alreadyResolvedDecision, settledElsewhereLine } from '../components/chat-input/sharedDecision'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { useAppSelector } from '../store'
import { selectSlotsWithLiveLastTs } from '../store/slotLastTs'
import type { ApprovalTarget, ChatSlot } from '../types'
import { approvalGoneKey, coordinatorTarget, nativeTarget } from '../types/approvalTarget'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import { reportForError, type ErrorReport } from '../utils/errorReport'
import { sessionDisplayTitle } from '../utils/sessionDisplayTitle'
import { useAppIdentity } from './identity'
import { AppSdkContext, apiPathAllowed, permissionDenied } from './scopedApi'

/** Every path a member sends a request to, for an app author to declare. A bare
 *  manifest entry covers its children, so `/api/chat/slots` admits the list, a
 *  create and a session's approve route; `/api/approvals` admits a background
 *  request's decide. `send` posts `/api/chat` alone, so `=/api/chat` is enough. */
export const HOST_SESSIONS_PATHS = {
  /** The list, and the default agent an agent-less session is labelled with. */
  sessions: ['/api/chat/slots', '/api/config/default-agent'],
  send: ['/api/chat'],
  /** A session's own request, and a background (coordinator) request. The decide
   *  checks the one path it sends, with the request's id in it. */
  decide: ['/api/chat/slots/{key}/approve', '/api/approvals/{id}/{action}'],
  /** The create, and the default memory mode a new session starts with. */
  create: ['/api/chat/slots', '/api/dashboard/config'],
} as const

/** A tool approval a session is waiting on. Decide it by passing this object back
 *  to `decide`; one the hook did not hand out decides nothing. */
export interface HostSessionApproval {
  /** The session that holds the request. */
  readonly sessionKey: string
  /** The tool asking to run. */
  readonly tool: string
  /** One string per request: equal for the same request across re-renders,
   *  different for a later request under the same id. */
  readonly key: string
}

export interface HostSession {
  readonly key: string
  /** The title the session list shows, in the active language. */
  readonly title: string
  /** The agent the session runs, labelled as the session list labels it. */
  readonly agentLabel: string
  readonly running: boolean
  readonly messageCount: number
  /** The newest message's preview, or '' when there is none. */
  readonly lastMessage: string
  /** True while the session waits on the person (a question it asked). */
  readonly waitingForInput: boolean
  /** The tool approval the session waits on, or null. */
  readonly pendingApproval: HostSessionApproval | null
  /** When the session last moved, in epoch milliseconds; the list is newest first by it. */
  readonly lastActive: number
}

/** What a send came to. `refused` carries the server's reason when it gave one;
 *  `unknown` is an accepted send whose answer could not be read, so it may well
 *  be running; `response-late` got no answer in time, so nothing proves it
 *  arrived; `transport-error` never left. */
export interface HostSessionSendResult {
  readonly status: SendReceiptStatus
  readonly reason?: string
}

/** What a decide came to, with the sentence the dashboard's own approval card
 *  shows for it. `settled`: another person decided it first. `gone`: the request
 *  is no longer pending (a status, not a failure). `failed`: nothing was
 *  recorded; `report` is the dashboard's error report for it, when it has one. */
export type HostSessionDecision =
  | { readonly outcome: 'decided' }
  | { readonly outcome: 'settled' | 'gone'; readonly message: string }
  | { readonly outcome: 'failed'; readonly message: string; readonly report?: ErrorReport }

export interface HostSessions {
  /** The dashboard's live sessions, newest first, or null when not admitted. */
  readonly sessions: readonly HostSession[] | null
  /** Which members this page may use, so a page can hide a control that would refuse. */
  readonly can: { readonly sessions: boolean; readonly send: boolean; readonly decide: boolean; readonly create: boolean }
  /** Send `message` to the session, as the person typing it there would. With
   *  `steer` on a running session it is injected into the running turn. */
  send(sessionKey: string, message: string, opts?: { steer?: boolean }): Promise<HostSessionSendResult>
  /** Approve or reject a pending tool approval this hook handed out. */
  decide(approval: HostSessionApproval, action: 'approve' | 'reject'): Promise<HostSessionDecision>
  /** Start a new session for the person, as the sidebar's New session does,
   *  without leaving the page. */
  create(): Promise<{ key: string }>
}

const NO_PATHS: readonly string[] = []
const NO_SLOTS: readonly ChatSlot[] = []
/** The store read for a page the list is not admitted to: a constant, so the
 *  page does not re-render on every slot change it may not see. */
const selectNoSlots = (): readonly ChatSlot[] => NO_SLOTS

/** The decide target behind each approval object handed out. Keyed by the object,
 *  so an app holds no reference that names a request by itself. */
const approvalTargets = new WeakMap<HostSessionApproval, ApprovalTarget>()

function slotApprovalTarget(sl: ChatSlot): ApprovalTarget | null {
  const info = sl.pending_approval_info
  if (!info) return null
  if (info.origin === 'native') return nativeTarget(info.request_id, sl.key, info.request_mid)
  if (info.origin === 'coordinator') return coordinatorTarget(info.request_id, sl.key, info.request_instance)
  return null
}

function decisionFor(err: unknown): HostSessionDecision {
  const settled = alreadyResolvedDecision(err)
  if (settled) return { outcome: 'settled', message: settledElsewhereLine(settled) }
  if (isTerminalApprovalRefusal(err)) {
    return { outcome: 'gone', message: i18nT('components.approvalCard.approval_no_longer_pending') as string }
  }
  const message = err instanceof ApiError && err.message
    ? i18nT('components.approvalCard.decision_not_recorded_error', { error: err.message }) as string
    : i18nT('components.approvalCard.decision_failed') as string
  const report = reportForError(err)
  return report ? { outcome: 'failed', message, report } : { outcome: 'failed', message }
}

export function useHostSessions(): HostSessions {
  const identity = useAppIdentity()
  const sdk = useContext(AppSdkContext)
  const builtin = identity?.origin === 'builtin'
  // Read off the context directly, not through useAppApi(), which throws with
  // no provider: a surface with no provider must fail CLOSED.
  const declared = sdk?.info.permissions.api ?? NO_PATHS
  const appLabel = sdk?.info.name ?? identity?.appId ?? 'app'
  const admits = useCallback(
    (paths: readonly string[]) => builtin || paths.every((path) => apiPathAllowed(declared, path)),
    [builtin, declared],
  )
  const refuse = useCallback(
    (path: string) => permissionDenied(appLabel, path, declared, appLabel),
    [appLabel, declared],
  )
  const firstRefused = useCallback(
    (paths: readonly string[]) => (builtin ? undefined : paths.find((path) => !apiPathAllowed(declared, path))),
    [builtin, declared],
  )

  const listAdmitted = admits(HOST_SESSIONS_PATHS.sessions)
  const slots = useAppSelector(listAdmitted ? selectSlotsWithLiveLastTs : selectNoSlots)
  // The dashboard's own shared ['default-agent'] entry, which its socket keeps
  // current; the app's query client would hold a copy nothing refreshes.
  const { data: defaultAgent = '' } = useQuery(
    { ...defaultAgentQuery, select: (d) => d.default_template, enabled: listAdmitted },
    dashboardQueryClient,
  )
  const langGen = useLanguageGeneration()
  const sessions = useMemo<readonly HostSession[] | null>(() => {
    void langGen
    if (!listAdmitted) return null
    return [...slots]
      .sort((a, b) => recencyEpoch(b) - recencyEpoch(a))
      .map((sl) => {
        const target = slotApprovalTarget(sl)
        let pendingApproval: HostSessionApproval | null = null
        if (sl.pending_approval_info) {
          pendingApproval = Object.freeze({
            sessionKey: sl.key,
            tool: sl.pending_approval_info.tool,
            key: approvalGoneKey(sl.pending_approval_info.request_id, target),
          })
          if (target) approvalTargets.set(pendingApproval, target)
        }
        return Object.freeze({
          key: sl.key,
          title: sessionDisplayTitle(sl),
          agentLabel: agentOrDefaultLabel(sl.agent, defaultAgent, sl.agent_kind),
          running: !!sl.running,
          messageCount: sl.messages ?? 0,
          lastMessage: sl.last_message || '',
          waitingForInput: !!sl.waiting_for_input,
          pendingApproval,
          lastActive: recencyEpoch(sl),
        })
      })
  }, [listAdmitted, slots, defaultAgent, langGen])

  const send = useCallback(async (sessionKey: string, message: string, opts?: { steer?: boolean }) => {
    const refused = firstRefused(HOST_SESSIONS_PATHS.send)
    if (refused) throw refuse(refused)
    const receipt = await sendTurn({ message, slot: sessionKey, steer: !!opts?.steer })
    return receipt.reason !== undefined ? { status: receipt.status, reason: receipt.reason } : { status: receipt.status }
  }, [firstRefused, refuse])

  const decide = useCallback(async (approval: HostSessionApproval, action: 'approve' | 'reject') => {
    const target = approvalTargets.get(approval)
    // No target: the session sent no owner-bound identity for the request (or the
    // object is not one this hook handed out), so nothing can name it.
    if (!target) return decisionFor(new ApiError(404, i18nT('components.approvalCard.approval_no_longer_pending') as string))
    const path = decideApprovalPath(target, action)
    if (!builtin && !apiPathAllowed(declared, path)) throw refuse(path)
    try {
      await api.decideApproval(target, action)
      return { outcome: 'decided' } as const
    } catch (err) {
      return decisionFor(err)
    }
  }, [builtin, declared, refuse])

  const create = useCallback(async () => {
    const refused = firstRefused(HOST_SESSIONS_PATHS.create)
    if (refused) throw refuse(refused)
    const slot = await api.createChatSlot()
    return { key: slot.key }
  }, [firstRefused, refuse])

  const can = useMemo(() => ({
    sessions: listAdmitted,
    send: admits(HOST_SESSIONS_PATHS.send),
    // Both decide routes, with a placeholder where the request names itself.
    decide: admits(['/api/chat/slots/session/approve', '/api/approvals/request/approve']),
    create: admits(HOST_SESSIONS_PATHS.create),
  }), [listAdmitted, admits])

  return useMemo(() => ({ sessions, can, send, decide, create }), [sessions, can, send, decide, create])
}

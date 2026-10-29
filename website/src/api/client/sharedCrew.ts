/**
 * Shared Crew reads, the cancel write, and the owner's Tailscale invite calls.
 *
 * Every read except the mode degrades: a gateway that does not serve a route
 * answers 404 and the method resolves `null`, which the UI renders as "not
 * available on this Crew" rather than as an error. Any other failure, and any
 * failure of the mode read, throws through `j` as usual.
 * Filtering, paging and counting happen on the server; the client sends the
 * query and renders the page it gets back.
 */
import type { AcceptedWork, CapacityResponse, CreatorFilter, PublicPrincipal, SendState, SharedCrewCapabilities, TypingLease } from '../sharedCrew'
import { isAcceptedWork } from '../sharedCrewUi'
import type {
  DecisionsResponse,
  Maybe,
  PeopleResponse,
  PresenceResponse,
  SharedCrewSessionPage,
  WorkListResponse,
} from '../sharedCrewUi'
import type { CrewModeResponse } from '../sharedCrewMode'
import type { CreateInvitesBody, TailscaleInvite, TailscaleInvitesStatus } from '../sharedCrewInvites'
import { CONFIRM_IRREVERSIBLE_HEADER, type AccessView, type ApprovedPerson } from '../sharedCrewAccess'
import type { ClientTransport } from './transport'

export interface SharedCrewSessionQuery {
  creator: CreatorFilter
  q?: string
  cursor?: string
  limit?: number
}

/** A cancel answer: the next state, or the server's current state on a stale revision. */
export type CancelWorkResult =
  | { ok: true; work: AcceptedWork | null }
  | { ok: false; conflict: true; current: AcceptedWork | null; code: string }

export function createSharedCrewEndpoints({ get, post, del, j }: ClientTransport) {
  const read = async <T,>(url: string): Promise<Maybe<T>> => {
    const r = await get(url)
    if (r.status === 404) return null
    return await j(r) as T
  }
  const requests = {
    sharedCrewCapabilities: () => read<SharedCrewCapabilities>('/api/shared-crew/capabilities'),
    sharedCrewSessions: (query: SharedCrewSessionQuery) => {
      const params = new URLSearchParams({ creator: query.creator })
      if (query.q) params.set('q', query.q)
      if (query.cursor) params.set('cursor', query.cursor)
      if (query.limit) params.set('limit', String(query.limit))
      return read<SharedCrewSessionPage>('/api/shared-crew/sessions?' + params)
    },
    sharedCrewPeople: () => read<PeopleResponse>('/api/shared-crew/people'),
    /** The mode route is the one read that never degrades: a 404 throws like any
     *  other failure, because an unreadable mode is an error, never a personal Crew. */
    sharedCrewMode: async (): Promise<CrewModeResponse> => await j(await get('/api/shared-crew/mode')) as CrewModeResponse,
    /** The viewer's server-derived principal from `/api/auth/me`, or `null` when
     *  this gateway does not report one. Never sent back as "who I am". */
    sharedCrewViewer: async (): Promise<Maybe<PublicPrincipal>> => {
      const r = await get('/api/auth/me')
      if (r.status === 404) return null
      const body = await j(r) as { principal?: PublicPrincipal } | null
      return body?.principal?.id ? body.principal : null
    },
    sharedCrewCapacity: () => read<CapacityResponse>('/api/shared-crew/capacity'),
    sharedCrewWork: (sessionKey: string) =>
      read<WorkListResponse>('/api/shared-crew/work?' + new URLSearchParams({ session_key: sessionKey })),
    sharedCrewPresence: (sessionKey: string) =>
      read<PresenceResponse>('/api/shared-crew/presence?' + new URLSearchParams({ session_key: sessionKey })),
    sharedCrewDecisions: (sessionKey: string) =>
      read<DecisionsResponse>('/api/shared-crew/decisions?' + new URLSearchParams({ session_key: sessionKey })),
    sharedCrewSendState: (sessionKey: string) =>
      read<SendState>('/api/shared-crew/send-state?' + new URLSearchParams({ session_key: sessionKey })),
    sharedCrewTypingLease: (sessionKey: string) =>
      read<TypingLease>('/api/shared-crew/typing-lease?' + new URLSearchParams({ session_key: sessionKey })),
    /** In-Crew Tailscale invites. Owner-only on the server; a 404 means this
     *  gateway has no invite routes, read as "not available". */
    tailscaleInvites: () => read<TailscaleInvitesStatus>('/api/shared-crew/tailscale/invites'),
    tailscaleConnect: async (key: string): Promise<TailscaleInvitesStatus> =>
      await j(await post('/api/shared-crew/tailscale/credential', { key })) as TailscaleInvitesStatus,
    /** Removing the key cannot be undone: callers confirm first, and the header says so. */
    tailscaleDisconnect: async (): Promise<TailscaleInvitesStatus> =>
      await j(await del('/api/shared-crew/tailscale/credential', undefined, undefined, CONFIRM_IRREVERSIBLE_HEADER)) as TailscaleInvitesStatus,
    tailscaleCreateInvites: async (body: CreateInvitesBody): Promise<{ invites: TailscaleInvite[] }> =>
      await j(await post('/api/shared-crew/tailscale/invites', body)) as { invites: TailscaleInvite[] },
    tailscaleResendInvite: async (id: string): Promise<void> => {
      await j(await post('/api/shared-crew/tailscale/invites/' + encodeURIComponent(id) + '/resend'))
    },
    tailscaleRevokeInvite: async (id: string): Promise<{ revoked: string; member_removed: boolean }> =>
      await j(await del('/api/shared-crew/tailscale/invites/' + encodeURIComponent(id))) as { revoked: string; member_removed: boolean },
    tailscaleUploadAvatar: async (dataBase64: string): Promise<{ avatar_id: string }> =>
      await j(await post('/api/shared-crew/tailscale/avatars', { data_base64: dataBase64 })) as { avatar_id: string },
    /** Who may open this Crew: waiting, approved and the first-share warning. Owner-only. */
    sharedCrewAccess: () => read<AccessView>('/api/shared-crew/access'),
    sharedCrewAckFirstShare: async (): Promise<void> => {
      await j(await post('/api/shared-crew/access/first-share/ack'))
    },
    sharedCrewApprove: async (userId: string): Promise<{ approved: ApprovedPerson }> =>
      await j(await post('/api/shared-crew/access/waiting/' + encodeURIComponent(userId) + '/approve')) as { approved: ApprovedPerson },
    sharedCrewDismiss: async (userId: string): Promise<void> => {
      await j(await post('/api/shared-crew/access/waiting/' + encodeURIComponent(userId) + '/dismiss'))
    },
    sharedCrewRemoveApproved: async (userId: string): Promise<{ removed: string; stopped_running: number; cancelled_queued: number; closed_connections: number }> =>
      await j(await del('/api/shared-crew/access/approved/' + encodeURIComponent(userId))) as { removed: string; stopped_running: number; cancelled_queued: number; closed_connections: number },
    /** Cancel exactly one work item at the revision the UI showed. A 409 is not
     *  an error: it carries the item's current state, which the UI adopts. */
    sharedCrewCancelWork: async (workId: string, expectedRevision: number): Promise<CancelWorkResult> => {
      const r = await post('/api/shared-crew/work/' + encodeURIComponent(workId) + '/cancel', { expected_revision: expectedRevision })
      if (r.status === 409) {
        const body = await r.json().catch(() => ({})) as { current?: AcceptedWork; code?: string }
        return { ok: false, conflict: true, current: body.current ?? null, code: body.code ?? 'stale_revision' }
      }
      // Accepted with no item in the body: the caller re-reads the list.
      const body = await j(r) as Partial<AcceptedWork> & { work?: AcceptedWork }
      const work = isAcceptedWork(body) ? body : isAcceptedWork(body.work) ? body.work : null
      return { ok: true, work }
    },
  }
  return { requests }
}

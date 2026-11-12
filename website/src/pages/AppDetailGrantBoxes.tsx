import { MessagesSquare, ShieldAlert } from 'lucide-react'
import { i18nT } from '../i18n/t'

// The declared-grant boxes of the detail page's permissions card, and the
// reach lines of its "approve new permissions" notice. They live here because
// `AppDetailPage.tsx` is at the file line cap.

/** `permissions.workerAutoApprove`, marked while an update holds it back. */
export function WorkerAutoApproveBox({ staged }: { staged: boolean }) {
  return (
    <div className="rounded-md border border-warn/30 bg-warn-subtle px-2.5 py-2">
      <div className="flex items-start gap-2 text-text">
        <ShieldAlert size={13} className="mt-[2px] shrink-0 text-warn" />
        <span>{i18nT('pages.appDetailPage.worker_auto_approve_desc')}</span>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>workerAutoApprove</code>
        {staged && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
      </div>
    </div>
  )
}

const REACH_VERBS = ['read', 'send'] as const

/** The known verbs in a `permissions.reachableByUserSessions` value. Anything else grants nothing. */
export function reachVerbs(value: unknown): string[] {
  return Array.isArray(value) ? REACH_VERBS.filter(verb => value.includes(verb)) : []
}

function reachLine(verb: string): string {
  return verb === 'send'
    ? i18nT('pages.appDetailPage.user_session_reach_send')
    : i18nT('pages.appDetailPage.user_session_reach_read')
}

/**
 * What `permissions.reachableByUserSessions` lets the owner's chats do. Each verb an
 * update holds back carries its own "not approved" tag, so an approved verb never
 * reads as waiting.
 */
export function UserSessionReachBox({ declared, staged }: { declared: string[]; staged: string[] }) {
  if (declared.length === 0) return null
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="user-session-reach">
      <div className="flex items-start gap-2 text-text">
        <MessagesSquare size={13} className="mt-[2px] shrink-0 text-muted" />
        <div className="flex flex-col gap-0.5">
          {declared.map(verb => (
            <span key={verb} data-testid={`user-session-reach-${verb}`}>
              {reachLine(verb)}
              {staged.includes(verb) && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>reachableByUserSessions</code>
      </div>
    </div>
  )
}

/** The reach verbs an update holds back, one line each, for the approval notice. */
export function UserSessionReachStaged({ staged }: { staged: string[] }) {
  return (
    <>
      {staged.map(verb => (
        <div key={verb} className="text-[13px]" data-testid="staged-user-session-reach">{reachLine(verb)}</div>
      ))}
    </>
  )
}

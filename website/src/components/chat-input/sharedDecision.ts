/** Shared approvals and questions: the per-click response id, and reading the
 *  server's "someone else answered first" reply.
 *
 *  Every collaborator sees the same pending card and any one may answer. The
 *  server settles it once; a different click then gets 409 `already_resolved`
 *  with the winning decision, which the card adopts instead of reporting a
 *  failure. A retry of the same click carries the same response id and replays
 *  its own success. */
import type { PublicPrincipal, SharedDecision } from '../../api/sharedCrew'
import { displayName } from '../../api/sharedCrewMode'
import { i18nT } from '../../i18n/t'

/** One id per user click on a decision card; never derived from content. */
export function mintResponseId(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') return 'r_' + c.randomUUID()
  return 'r_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10)
}

/** The resolved decision a 409 `already_resolved` reply carries, else `null`. */
export function alreadyResolvedDecision(err: unknown): SharedDecision | null {
  if (!err || typeof err !== 'object') return null
  const e = err as { status?: unknown; body?: unknown }
  if (e.status !== 409 || typeof e.body !== 'string') return null
  try {
    const body = JSON.parse(e.body) as { code?: unknown; decision?: SharedDecision }
    if (body.code !== 'already_resolved' || !body.decision?.decision_id) return null
    return body.decision
  } catch {
    return null
  }
}

/** The card outcome a resolved decision maps to. */
export function decisionVerdict(decision: SharedDecision): 'approved' | 'rejected' {
  return decision.outcome === 'approved' || decision.outcome === 'approved_trust_reads' ? 'approved' : 'rejected'
}

type Who = { self: true } | { self: false; name: string }

/** Whom a decision line names: the viewer reads as "you", the Crew owner by
 *  this machine's name, anyone else by name or Unattributed. */
function who(p: PublicPrincipal | null | undefined, viewerId: string): Who {
  if (p && viewerId && p.id === viewerId) return { self: true }
  return { self: false, name: displayName(p) || i18nT('sharedCrew.unattributed') }
}

/** "Alice approved" / "You approved": who settled the decision. */
export function resolvedByLine(decision: SharedDecision, viewerId = ''): string {
  const w = who(decision.responder, viewerId)
  if (w.self) {
    if (decision.kind === 'question') return i18nT('sharedCrew.answered_by_you')
    return decisionVerdict(decision) === 'approved' ? i18nT('sharedCrew.approved_by_you') : i18nT('sharedCrew.rejected_by_you')
  }
  const { name } = w
  if (decision.kind === 'question') return i18nT('sharedCrew.answered_by_person', { name })
  return decisionVerdict(decision) === 'approved'
    ? i18nT('sharedCrew.approved_by_person', { name })
    : i18nT('sharedCrew.rejected_by_person', { name })
}

/** What a pending decision waits on, named by the person whose turn raised it:
 *  "Alice's action awaits approval" / "Kiro is waiting for your answer". */
export function waitingLine(decision: SharedDecision, viewerId = ''): string {
  const w = who(decision.initiator, viewerId)
  if (decision.kind === 'question') {
    return w.self ? i18nT('sharedCrew.question_waiting_you') : i18nT('sharedCrew.question_waiting', { name: w.name })
  }
  return w.self ? i18nT('sharedCrew.approval_waiting_you') : i18nT('sharedCrew.approval_waiting', { name: w.name })
}

/** "Already approved elsewhere": a lost race told without naming anyone. */
export function settledElsewhereLine(decision: SharedDecision): string {
  return decisionVerdict(decision) === 'approved' ? i18nT('sharedCrew.already_approved') : i18nT('sharedCrew.already_rejected')
}

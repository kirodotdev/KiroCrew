/** What to tell the sender when the Crew refuses or queues their work: whose
 *  limit it is (theirs or the whole Crew's), the usage behind it, and the next
 *  step. Never a wait estimate: the scheduler publishes none. */
import type { CapacityResponse, ReasonCode, ScopeUsage } from '../../../api/sharedCrew'
import { asReasonCode, limitScope } from '../../../api/sharedCrewUi'
import { fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

function usage(scope: ScopeUsage | undefined, field: 'active' | 'queued'): { n: string; limit: string } | null {
  if (!scope) return null
  const limit = field === 'active' ? scope.active_limit : scope.queue_limit
  if (limit == null) return null
  return { n: fmtNumber(scope[field]), limit: fmtNumber(limit) }
}

/** The refusal line for a send the Crew did not accept, or `null` when the
 *  body is not a Shared Crew refusal (the caller keeps its own copy). */
export function refusalMessage(body: { code?: unknown; capacity?: CapacityResponse }): string | null {
  const code: ReasonCode | null = asReasonCode(body.code)
  if (!code) return null
  const cap = body.capacity
  switch (code) {
    case 'principal_active_limit': {
      const u = usage(cap?.principal, 'active')
      return u ? i18nT('sharedCrew.refused_personal_active_counts', u) : i18nT('sharedCrew.refused_personal_active')
    }
    case 'principal_queue_full': {
      const u = usage(cap?.principal, 'queued')
      return u ? i18nT('sharedCrew.refused_personal_queue_counts', u) : i18nT('sharedCrew.refused_personal_queue')
    }
    case 'crew_active_limit': {
      const u = usage(cap?.crew, 'active')
      return u ? i18nT('sharedCrew.refused_crew_active_counts', u) : i18nT('sharedCrew.refused_crew_active')
    }
    case 'crew_queue_full': {
      const u = usage(cap?.crew, 'queued')
      return u ? i18nT('sharedCrew.refused_crew_queue_counts', u) : i18nT('sharedCrew.refused_crew_queue')
    }
    case 'session_create_rate': return i18nT('sharedCrew.refused_rate')
    case 'request_too_large': return i18nT('sharedCrew.refused_too_large')
    case 'storage_limit': return i18nT('sharedCrew.refused_storage')
    case 'containment_unavailable': return i18nT('sharedCrew.refused_containment')
    default: return null
  }
}

/** Why a queued item is waiting: the sender's own limit or the Crew's. */
export function queuedReason(reason: ReasonCode | null, capacity: CapacityResponse | null | undefined): string {
  const scope = limitScope(reason, capacity)
  if (scope === 'principal') return i18nT('sharedCrew.queued_personal')
  if (scope === 'crew') return i18nT('sharedCrew.queued_crew')
  return i18nT('sharedCrew.queued_generic')
}

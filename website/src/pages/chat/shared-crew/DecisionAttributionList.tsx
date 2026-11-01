/** Who a shared approval or question is waiting on, and who settled it, as one
 *  sentence per row. A pending one names the person whose turn raised it; a
 *  resolved one names the person who settled it. The server's decision frames
 *  are the only source, so every collaborator reads the same line. A
 *  personal Crew shows none of it (`useTeamAttribution`), and neither does a
 *  session only the viewer is in (`involvesOthers`). */
import { MessageCircleQuestionMark, ShieldCheck } from 'lucide-react'
import type { SharedDecision } from '../../../api/sharedCrew'
import { isUnknownPrincipal } from '../../../api/sharedCrewUi'
import { i18nT } from '../../../i18n/t'
import { resolvedByLine, waitingLine } from '../../../components/chat-input/sharedDecision'

/** Decisions a person made or is asked to make. One the gateway settled by its
 *  approval mode (`settled_by: 'host'`) has no one to name. */
function personDecisions(items: readonly SharedDecision[]): SharedDecision[] {
  return items.filter(d => d.settled_by !== 'host')
}

/** The pending ones and the three most recently settled, newest last. */
export function visibleDecisions(items: readonly SharedDecision[]): SharedDecision[] {
  const own = personDecisions(items)
  const pending = own.filter(d => d.state === 'pending')
  const settled = own
    .filter(d => d.state === 'resolved')
    .sort((a, b) => (a.resolved_at ?? 0) - (b.resolved_at ?? 0))
    .slice(-3)
  return [...settled, ...pending]
}

/** Whether a decision's initiator or responder is someone other than the viewer.
 *  Until the viewer is known no one can be told apart from them, so none is. */
export function involvesOthers(items: readonly SharedDecision[], viewerId: string): boolean {
  if (!viewerId) return false
  const other = (p: SharedDecision['initiator'] | null) => !!p && !isUnknownPrincipal(p) && p.id !== viewerId
  return personDecisions(items).some(d => other(d.initiator) || other(d.responder))
}

/** `viewerId` makes the viewer's own turns and answers read as "you". */
export function DecisionAttributionList({ items, viewerId = '' }: { items: readonly SharedDecision[]; viewerId?: string }) {
  const shown = visibleDecisions(items)
  if (shown.length === 0) return null
  return (
    <ul className="flex flex-col gap-0.5 min-w-0" aria-label={i18nT('sharedCrew.decisions_list')} aria-live="polite" data-testid="decision-attribution">
      {shown.map(d => {
        const Icon = d.kind === 'question' ? MessageCircleQuestionMark : ShieldCheck
        const line = d.state === 'pending' ? waitingLine(d, viewerId) : resolvedByLine(d, viewerId)
        return (
          <li key={d.decision_id} className="flex items-center gap-1.5 min-w-0 text-[12px] leading-5" data-state={d.state}>
            <Icon size={12} className="shrink-0 text-muted" aria-hidden />
            <span className="text-text truncate min-w-0" title={line}>{line}</span>
          </li>
        )
      })}
    </ul>
  )
}

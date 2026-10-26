/** Who asked and who answered, for this session's shared approvals and
 *  questions. A pending one says it is waiting and whose turn raised it; a
 *  resolved one names the person who settled it. The server's decision frames
 *  are the only source, so every collaborator reads the same line. */
import { MessageCircleQuestionMark, ShieldCheck } from 'lucide-react'
import type { SharedDecision } from '../../../api/sharedCrew'
import { channelName, principalVia } from '../../../api/sharedCrewUi'
import { ChannelBrandIcon } from '../../../components/ChannelBrandIcon'
import { i18nT } from '../../../i18n/t'
import { requestedByLine, resolvedByLine } from '../../../components/chat-input/sharedDecision'

/** The pending ones and the three most recently settled, newest last. */
export function visibleDecisions(items: readonly SharedDecision[]): SharedDecision[] {
  const pending = items.filter(d => d.state === 'pending')
  const settled = items
    .filter(d => d.state === 'resolved')
    .sort((a, b) => (a.resolved_at ?? 0) - (b.resolved_at ?? 0))
    .slice(-3)
  return [...settled, ...pending]
}

export function DecisionAttributionList({ items }: { items: readonly SharedDecision[] }) {
  const shown = visibleDecisions(items)
  if (shown.length === 0) return null
  return (
    <ul className="flex flex-col gap-0.5 min-w-0" aria-label={i18nT('sharedCrew.decisions_list')} aria-live="polite" data-testid="decision-attribution">
      {shown.map(d => {
        const Icon = d.kind === 'question' ? MessageCircleQuestionMark : ShieldCheck
        const head = d.state === 'pending'
          ? (d.kind === 'question' ? i18nT('sharedCrew.question_waiting') : i18nT('sharedCrew.approval_waiting'))
          : resolvedByLine(d)
        // Answered in a channel (a Slack click): its mark beside the name, as on a byline.
        const via = d.state === 'pending' ? '' : principalVia(d.responder)
        return (
          <li key={d.decision_id} className="flex items-center gap-1.5 min-w-0 text-[12px] leading-5" data-state={d.state}>
            <Icon size={12} className="shrink-0 text-muted" aria-hidden />
            <span className="text-text shrink-0">{head}</span>
            {via && (
              <span className="inline-flex shrink-0" data-testid="decision-via" data-via={via}>
                <ChannelBrandIcon channel={via} size={12} />
                <span className="sr-only">{i18nT('sharedCrew.via_channel', { channel: channelName(via) })}</span>
              </span>
            )}
            <span className="text-muted truncate min-w-0" title={requestedByLine(d)}>{'\u00A0·\u00A0'}{requestedByLine(d)}</span>
          </li>
        )
      })}
    </ul>
  )
}

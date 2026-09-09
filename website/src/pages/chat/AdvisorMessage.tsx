import { useEffect, useRef, useState } from 'react'
import { ChevronRight, ShieldCheck } from 'lucide-react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import { Badge } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'
import { useRowDisclosure } from './rowDisclosure'

type AdvisorSeverity = 'nit' | 'concern' | 'blocker'

const severityTone: Record<AdvisorSeverity, { badge: 'muted' | 'warn' | 'err'; icon: string }> = {
  nit: { badge: 'muted', icon: 'text-muted' },
  concern: { badge: 'warn', icon: 'text-warn' },
  blocker: { badge: 'err', icon: 'text-danger' },
}

const FINDING_COLLAPSED_LINES = 6
const FINDING_COLLAPSED_CHARS = 800

interface AdvisorDisplay {
  text: string
  evidence: string
}

function advisorDisplay(message: ChatMessage): AdvisorDisplay {
  // The backend always writes the display fields alongside the row; the raw
  // content is the model-facing steer text and is only a last-resort fallback.
  const structuredText = message.meta?.advisorText
  return {
    text: (typeof structuredText === 'string' ? structuredText : message.content ?? '').trim(),
    evidence: typeof message.meta?.advisorEvidence === 'string'
      ? message.meta.advisorEvidence.trim()
      : '',
  }
}

export default function AdvisorMessage({ message, disclosureKey }: { message: ChatMessage; disclosureKey?: string }) {
  const rawSeverity = message.meta?.advisorSeverity
  const severity: AdvisorSeverity = rawSeverity === 'concern' || rawSeverity === 'blocker' ? rawSeverity : 'nit'
  const tone = severityTone[severity]
  const severityLabel = severity === 'blocker'
    ? i18nT('pages.chat.advisorMessage.blocker')
    : severity === 'concern'
      ? i18nT('pages.chat.advisorMessage.concern')
      : i18nT('pages.chat.advisorMessage.nit')
  const preserved = message.meta?.advisorState === 'preserved'
  const discarded = message.meta?.advisorState === 'discarded'
  const stateLabel = discarded
    ? i18nT('pages.chat.advisorMessage.dropped_by_stop')
    : preserved
    ? i18nT('pages.chat.advisorMessage.saved_for_next_turn')
    : i18nT('pages.chat.advisorMessage.sent_mid_turn')
  const { text, evidence } = advisorDisplay(message)
  const [findingExpanded, setFindingExpanded] = useRowDisclosure(
    disclosureKey ? `${disclosureKey}:finding` : undefined,
    false,
  )
  const [evidenceExpanded, setEvidenceExpanded] = useRowDisclosure(
    disclosureKey ? `${disclosureKey}:evidence` : undefined,
    false,
  )
  const findingRef = useRef<HTMLDivElement | null>(null)
  const [measuredOverflow, setMeasuredOverflow] = useState(false)

  useEffect(() => {
    const el = findingRef.current
    if (!el || findingExpanded) return
    setMeasuredOverflow(el.scrollHeight > el.clientHeight + 1)
  }, [text, findingExpanded])

  const findingOverflows = text.length > FINDING_COLLAPSED_CHARS
    || text.split('\n').length > FINDING_COLLAPSED_LINES
    || measuredOverflow

  return (
    <div data-role="advisor" className="msg msg-advisor message-bubble group/msg max-w-full">
      <div className="msg-content min-w-0 overflow-hidden rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in">
        <div className="flex flex-wrap items-center gap-2 px-3 pt-2 text-[13px] leading-5">
          <span className="inline-flex items-center gap-1.5 font-medium text-text">
            <ShieldCheck size={13} className={`lucide-inline shrink-0 ${tone.icon}`} aria-hidden="true" />
            {i18nT('pages.chat.advisorMessage.advisor')}
          </span>
          <Badge variant={tone.badge} className="text-[11px] leading-4 px-1.5 py-0.5">{severityLabel}</Badge>
          <Badge variant={discarded || preserved ? 'muted' : 'ok'} className="text-[11px] leading-4 px-1.5 py-0.5">{stateLabel}</Badge>
        </div>

        <div className="px-3 pb-2 pt-1 text-[13px] leading-5 text-text min-w-0">
          <div
            ref={findingRef}
            className={findingExpanded
              ? 'max-h-[24rem] overflow-y-auto overflow-x-hidden'
              : 'max-h-40 overflow-hidden'}
            data-testid="advisor-finding"
          >
            <MessageErrorBoundary rawContent={text}>
              <MarkdownRenderer content={text} softBreaks />
            </MessageErrorBoundary>
          </div>
          {findingOverflows && (
            <button
              type="button"
              onClick={() => setFindingExpanded(value => !value)}
              aria-expanded={findingExpanded}
              className="mt-1 inline-flex items-center gap-0.5 rounded px-1 py-0.5 text-[11px] leading-4 text-muted hover:bg-bg-hover hover:text-text transition-colors"
            >
              <ChevronRight
                size={11}
                className={`transition-transform ${findingExpanded ? 'rotate-90' : ''}`}
                aria-hidden="true"
              />
              {findingExpanded
                ? i18nT('appSdk.chatMessageList.show_less')
                : i18nT('appSdk.chatMessageList.show_more')}
            </button>
          )}
        </div>

        {evidence && (
          <>
            <button
              type="button"
              onClick={() => setEvidenceExpanded(value => !value)}
              aria-expanded={evidenceExpanded}
              className="w-full flex items-center gap-1.5 border-t border-border px-3 py-2 text-left text-[12px] leading-5 text-muted hover:bg-bg-hover hover:text-text transition-colors"
              data-testid="advisor-evidence-toggle"
            >
              <ChevronRight
                size={12}
                className={`shrink-0 transition-transform ${evidenceExpanded ? 'rotate-90' : ''}`}
                aria-hidden="true"
              />
              <span className="font-medium">{i18nT('pages.chat.advisorMessage.evidence')}</span>
            </button>
            {evidenceExpanded && (
              <div
                className="max-h-[24rem] overflow-y-auto overflow-x-hidden border-t border-border px-3 py-2 text-[12px] leading-5 text-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
                data-testid="advisor-evidence"
                role="region"
                aria-label={i18nT('pages.chat.advisorMessage.evidence')}
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
                tabIndex={0}
              >
                <MessageErrorBoundary rawContent={evidence}>
                  <MarkdownRenderer content={evidence} softBreaks />
                </MessageErrorBoundary>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
